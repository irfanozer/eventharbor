import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import {
  ensureReceiverLabEndpoint, getDeliveryAttempts, getEndpoint, getEvent, getReceiverLab,
  publishDemoEvent, replayDelivery, setReceiverLabPreset,
} from "../api";
import { DeliveryStory } from "../components/DeliveryStory";
import { EventJourney } from "../components/EventJourney";
import { EventTimeline } from "../components/EventTimeline";
import { ErrorState } from "../components/QueryState";
import { ReplayConfirmation } from "../components/ReplayConfirmation";
import { DEFAULT_DEMO_SCENARIO_ID, DEMO_SCENARIOS, demoScenario, isDemoScenarioId } from "../demoScenarios";
import {
  DEFAULT_DEMO_EVENT_TYPE, DEMO_EVENT_TYPES, demoEventDefinition, demoEventPayload,
  demoEventReference, initialDemoEventValues, isDemoEventTypeId, isExpectedSchemaRejection,
} from "../demoEvents";
import type { Delivery, DemoEventPayload, DemoEventTypeId, DemoScenarioId } from "../types";

const EVENT_KEY = "eventharbor.control-room.event-id";
const RUN_KEY = "eventharbor.control-room.tour";
interface Run {
  runId: string;
  eventId?: string;
  replayDeliveryId?: string;
  endpointId?: string;
  payload: DemoEventPayload;
  scenarioId: DemoScenarioId;
}
interface Draft { type: DemoEventTypeId; values: Record<string, string> }

function restoreRun(): Run | null {
  try {
    const run = JSON.parse(sessionStorage.getItem(RUN_KEY) ?? "null") as Run | null;
    const eventId = sessionStorage.getItem(EVENT_KEY);
    if (!run) return eventId ? { runId: "", eventId, scenarioId: DEFAULT_DEMO_SCENARIO_ID, payload: { type: DEFAULT_DEMO_EVENT_TYPE, data: {} } } : null;
    const oldPayload = run.payload as unknown as { order_id?: string; amount_cents?: number };
    if (oldPayload && typeof oldPayload.order_id === "string" && typeof oldPayload.amount_cents === "number") {
      run.payload = { type: DEFAULT_DEMO_EVENT_TYPE, data: { order_id: oldPayload.order_id, amount_cents: oldPayload.amount_cents, customer_id: "CUS-MIGRATED", currency: "USD" } };
    }
    if (typeof run.runId !== "string" ||
      !isDemoEventTypeId(run.payload?.type) || !run.payload.data || typeof run.payload.data !== "object" ||
      (run.eventId !== undefined && typeof run.eventId !== "string") ||
      (run.endpointId !== undefined && typeof run.endpointId !== "string")) return null;
    return { ...run, eventId: run.eventId ?? eventId ?? undefined, scenarioId: isDemoScenarioId(run.scenarioId) ? run.scenarioId : DEFAULT_DEMO_SCENARIO_ID };
  } catch { return null; }
}
function remember(run: Run | null): void {
  try {
    if (run) sessionStorage.setItem(RUN_KEY, JSON.stringify(run));
    else sessionStorage.removeItem(RUN_KEY);
    if (run?.eventId) sessionStorage.setItem(EVENT_KEY, run.eventId);
    else sessionStorage.removeItem(EVENT_KEY);
    sessionStorage.removeItem("eventharbor.control-room.mode");
  } catch { /* The current run remains usable if browser storage is unavailable. */ }
}
function freshDraft(type: DemoEventTypeId): Draft { return { type, values: initialDemoEventValues(type) }; }
function draftFromRun(run: Run): Draft {
  return { type: run.payload.type, values: Object.fromEntries(demoEventDefinition(run.payload.type).fields.map((field) => {
    const value = run.payload.data[field.key];
    return [field.key, field.kind === "money" && typeof value === "number" ? (value / 100).toFixed(2) : String(value ?? "")];
  })) };
}
function terminal(delivery: Delivery | undefined): boolean {
  return delivery?.status === "delivered" || delivery?.status === "dead_lettered";
}
const caseCopy: Record<DemoScenarioId, { title: string; hint: string; expectation: string }> = {
  outage_replay: { title: "Receiver stays offline", hint: "You decide when to resend", expectation: "The test receiver keeps saying unavailable. EventHarbor retries, then stops and keeps the event. You bring the receiver back and approve a resend." },
  transient_recovery: { title: "A temporary outage", hint: "Retries recover automatically", expectation: "The test receiver rejects two attempts, then accepts the next one. EventHarbor retries automatically." },
  rate_limit_recovery: { title: "The receiver is busy", hint: "Wait 5 seconds before retrying", expectation: "The test receiver asks for a five-second wait twice, then accepts the event. Watch the scheduled retries in the attempt log." },
  permanent_rejection: { title: "A required field is missing", hint: "Stop instead of retrying", expectation: "This sample deliberately leaves out a required field. The receiver rejects it, and EventHarbor stops instead of sending the same invalid data again." },
};

export function ControlRoomExperience() {
  const [params, setParams] = useSearchParams();
  const [initial] = useState(() => {
    const fresh = params.get("new_event") === "1";
    const run = fresh ? null : restoreRun();
    const type = params.get("event_type");
    return { run, draft: run ? draftFromRun(run) : freshDraft(isDemoEventTypeId(type) ? type : DEFAULT_DEMO_EVENT_TYPE) };
  });
  const [run, setRun] = useState<Run | null>(initial.run);
  const runRef = useRef(run);
  const [draft, setDraft] = useState(initial.draft);
  const [caseId, setCaseId] = useState(initial.run?.scenarioId ?? DEFAULT_DEMO_SCENARIO_ID);
  const [replayOpen, setReplayOpen] = useState(false);
  const [technicalOpen, setTechnicalOpen] = useState(false);
  const client = useQueryClient();
  function saveRun(next: Run | null) { runRef.current = next; setRun(next); remember(next); }
  useEffect(() => {
    if (params.get("new_event") !== "1") return;
    saveRun(null);
    const type = params.get("event_type");
    setDraft(freshDraft(isDemoEventTypeId(type) ? type : DEFAULT_DEMO_EVENT_TYPE));
    setCaseId(DEFAULT_DEMO_SCENARIO_ID);
    setReplayOpen(false);
    send.reset(); repair.reset(); resend.reset();
    const next = new URLSearchParams(params);
    next.delete("new_event");
    setParams(next, { replace: true });
  }, [params, setParams]);

  const story = useQuery({
    queryKey: ["event", run?.eventId], queryFn: () => getEvent(run!.eventId!), enabled: Boolean(run?.eventId),
    refetchInterval: (query) => {
      if (run?.replayDeliveryId && !query.state.data?.deliveries.some((item) => item.id === run.replayDeliveryId)) return 700;
      return terminal(query.state.data?.deliveries.reduce<Delivery | undefined>((last, item) =>
        !last || item.replay_generation > last.replay_generation ? item : last, undefined)) ? false : 700;
    },
  });
  useEffect(() => {
    if (!run || run.runId || !story.data) return;
    const runId = story.data.data.run_id;
    if (typeof runId !== "string" || !runId) return;
    const restored: Run = { ...run, runId, scenarioId: isDemoScenarioId(story.data.data.scenario) ? story.data.data.scenario : DEFAULT_DEMO_SCENARIO_ID,
      payload: { type: demoEventDefinition(story.data.type).type, data: story.data.data as DemoEventPayload["data"] } };
    saveRun(restored); setDraft(draftFromRun(restored)); setCaseId(restored.scenarioId);
  }, [run, story.data]);
  const original = story.data?.deliveries.find((item) => item.replay_generation === 0);
  const replay = story.data?.deliveries.filter((item) => item.replay_generation > 0)
    .sort((a, b) => b.replay_generation - a.replay_generation)[0];
  const current = replay ?? original;
  const scenario = demoScenario(isDemoScenarioId(story.data?.data.scenario) ? story.data.data.scenario : run?.scenarioId ?? caseId);
  const receiver = useQuery({
    queryKey: ["receiver-lab", run?.runId], queryFn: () => getReceiverLab(run!.runId), enabled: Boolean(run?.runId),
    refetchInterval: run?.runId ? 1500 : false,
  });
  const originalEvidence = useQuery({
    queryKey: ["delivery-attempts", original?.id, original?.status, original?.attempt_count],
    queryFn: () => getDeliveryAttempts(original!.id), enabled: Boolean(original),
    placeholderData: (previous, query) => query?.queryKey[1] === original?.id ? previous : undefined,
    refetchInterval: terminal(original) ? false : 500,
  });
  const replayEvidence = useQuery({
    queryKey: ["delivery-attempts", replay?.id, replay?.status, replay?.attempt_count],
    queryFn: () => getDeliveryAttempts(replay!.id), enabled: Boolean(replay),
    placeholderData: (previous, query) => query?.queryKey[1] === replay?.id ? previous : undefined,
    refetchInterval: terminal(replay) ? false : 500,
  });
  const endpoint = useQuery({ queryKey: ["endpoint", original?.endpoint_id], queryFn: () => getEndpoint(original!.endpoint_id), enabled: Boolean(original) });
  // A terminal transition always reads final evidence, even if polling just finished.
  const originalAttempts = originalEvidence.data?.attempts ?? [];
  const replayAttempts = replayEvidence.data?.attempts ?? [];
  const receipts = receiver.data?.requests.filter((item) => item.event_id === run?.eventId) ?? [];
  const receiverReady = receiver.data?.preset === "success" && !receiver.isError;
  const rejectedAsExpected = scenario.strategy === "terminal" && isExpectedSchemaRejection(story.data?.type, original, originalAttempts);

  const send = useMutation({
    mutationFn: async (start: Run) => {
      let prepared = start;
      if (!prepared.endpointId) {
        const selected = demoScenario(prepared.scenarioId);
        const state = await getReceiverLab(prepared.runId);
        if (state.preset !== selected.preset) {
          const configured = await setReceiverLabPreset(selected.preset, prepared.runId);
          if (configured.preset !== selected.preset) throw new Error("The test receiver did not confirm this case. Please try again.");
        }
        const destination = await ensureReceiverLabEndpoint(prepared.payload.type);
        prepared = { ...prepared, endpointId: destination.id };
        if (runRef.current?.runId !== start.runId) throw new Error("This event is no longer the active run.");
        saveRun(prepared);
      }
      // A lost acknowledgement reuses the exact payload and key without resetting receiver behavior.
      const accepted = await publishDemoEvent(prepared.endpointId!, prepared.runId, "control-room-story-" + prepared.runId, prepared.payload, prepared.scenarioId);
      return { ...prepared, eventId: accepted.event_id };
    },
    onSuccess: (accepted) => { if (runRef.current?.runId === accepted.runId) saveRun(accepted); void client.invalidateQueries({ queryKey: ["events"] }); },
  });
  const repair = useMutation({
    mutationFn: async () => {
      if (!run?.runId) throw new Error("No active receiver to restore.");
      const state = await getReceiverLab(run.runId);
      const restored = state.preset === "success" ? state : await setReceiverLabPreset("success", run.runId);
      if (restored.preset !== "success") throw new Error("The receiver has not confirmed recovery yet.");
      return { state: restored, runId: run.runId };
    },
    onSuccess: ({ state, runId }) => client.setQueryData(["receiver-lab", runId], state),
  });
  const resend = useMutation({
    mutationFn: async () => {
      if (!run?.runId || !original) throw new Error("No stored delivery to resend.");
      const accepted = await replayDelivery(original.id, "control-room-replay-" + run.runId);
      return { ...accepted, runId: run.runId };
    },
    onSuccess: async (accepted) => {
      if (runRef.current?.runId === accepted.runId && runRef.current.eventId === accepted.event_id) {
        saveRun({ ...runRef.current, replayDeliveryId: accepted.delivery_id });
        setReplayOpen(false);
      }
      await client.invalidateQueries({ queryKey: ["event", accepted.event_id] });
    },
  });
  const busy = send.isPending || repair.isPending || resend.isPending;
  const waitingForReplay = Boolean(run?.replayDeliveryId && !replay);
  const locked = busy || waitingForReplay || Boolean(run && (!run.eventId || !terminal(current)));
  const definition = demoEventDefinition(draft.type);
  const activeDefinition = demoEventDefinition(story.data?.type ?? run?.payload.type ?? draft.type);
  const valid = definition.fields.every((field) => {
    if (caseId === "permanent_rejection" && field.key === definition.invalidField) return true;
    const value = draft.values[field.key] ?? "";
    if (field.kind === "money") return Number.isFinite(Number(value)) && Number(value) > 0;
    if (field.kind === "integer") return Number.isInteger(Number(value)) && Number(value) > 0;
    return field.key === "currency" ? value.trim().length === 3 : Boolean(value.trim());
  });
  const payload = demoEventPayload(draft.type, draft.values);
  const reference = story.data ? demoEventReference({ type: activeDefinition.type, data: story.data.data as DemoEventPayload["data"] }) : run ? demoEventReference(run.payload) : null;
  function begin() {
    if (locked || !valid) return;
    const next = { runId: crypto.randomUUID(), scenarioId: caseId, payload };
    saveRun(next); send.reset(); repair.reset(); resend.reset(); setReplayOpen(false);
    send.mutate(next);
    window.requestAnimationFrame(() => {
      if (window.matchMedia?.("(max-width: 760px)").matches) {
        document.getElementById("live-proof")?.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  }
  function prepareNext() {
    const index = DEMO_SCENARIOS.findIndex((item) => item.id === scenario.id);
    setCaseId(DEMO_SCENARIOS[(index + 1) % DEMO_SCENARIOS.length]!.id);
    setDraft(freshDraft(draft.type));
    document.getElementById("demo-input")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  return <div className="simple-demo">
    <header className="demo-intro">
      <p className="eyebrow">Reliable messages between services</p>
      <h1>What if the other service can’t accept your message?</h1>
      <p>EventHarbor saves it, tries to deliver it, and keeps a record of every attempt. That message is a <strong>webhook</strong>. Try a failure below.</p>
    </header>
    <div className="simple-demo-grid">
      <form className="demo-controls" id="demo-input" onSubmit={(event) => { event.preventDefault(); begin(); }}>
        <p className="eyebrow">1 / Choose a case</p>
        <h2>What happens at the receiver?</h2>
        <div className="simple-case-picker" role="group" aria-label="Receiver incident">
          {DEMO_SCENARIOS.map((item, index) => <button type="button" key={item.id} disabled={locked} aria-pressed={caseId === item.id} onClick={() => setCaseId(item.id)}>
            <span className="case-index">0{index + 1}</span><span><strong>{caseCopy[item.id].title}</strong><small>{caseCopy[item.id].hint}</small></span><span aria-hidden="true">{caseId === item.id ? "●" : "○"}</span>
          </button>)}
        </div>
        <p className="case-expectation"><strong>What to expect</strong>{caseCopy[caseId].expectation}</p>
        <div className="sample-event"><span>Sample event</span><strong>{definition.label}</strong><code>{demoEventReference(payload)}</code></div>
        {caseId === "permanent_rejection" && <p className="missing-field-note">This sends the sample without <code>data.{definition.invalidField}</code>. That missing field causes the rejection.</p>}
        <details className="sample-editor">
          <summary><span><strong>Change the sample event</strong>{" "}<small>Edit the event type and its fields</small></span><span className="sample-editor__icon" aria-hidden="true">+</span></summary>
          <fieldset disabled={locked}>
            <p className="demo-fineprint">{locked ? "Editing is paused while this delivery is active. You can change the next event when it finishes." : "Changes apply to your next test event. Nothing is sent until you press Send test event."}</p>
            <label>Business event type<select value={draft.type} onChange={(event) => { if (isDemoEventTypeId(event.target.value)) setDraft(freshDraft(event.target.value)); }}>{DEMO_EVENT_TYPES.map((item) => <option key={item.type} value={item.type}>{item.label}</option>)}</select></label>
            <div className="sample-fields">{definition.fields.map((field) => caseId === "permanent_rejection" && field.key === definition.invalidField
              ? <p className="missing-field-note" key={field.key}>{field.label}: intentionally omitted from the JSON body.</p>
              : <label key={field.key}>{field.label}<input value={draft.values[field.key] ?? ""} type={field.kind === "text" ? "text" : "number"} min={field.kind === "money" ? ".01" : field.kind === "integer" ? 1 : undefined} step={field.kind === "money" ? ".01" : field.kind === "integer" ? 1 : undefined} maxLength={field.kind === "text" ? 80 : undefined} onChange={(event) => setDraft((previous) => ({ ...previous, values: { ...previous.values, [field.key]: event.target.value } }))} /></label>)}</div>
          </fieldset>
        </details>
        <button className="demo-primary" type="submit" disabled={locked || !valid}>{send.isPending ? "Sending…" : locked ? "Follow the current event →" : "Send test event →"}</button>
        <p className="demo-fineprint">Sample business data. Real requests to a separate test service, not a real customer or fulfillment system.</p>
        {run && terminal(current) && <p className="demo-fineprint">The last result stays visible until you send another event.</p>}
      </form>

      <div className="demo-results">
        {reference && <div className="active-sample"><div><span>Following this event</span><strong>{reference}</strong></div><span>{activeDefinition.label}</span></div>}
        <DeliveryStory scenario={scenario} event={story.data ?? null} original={original} replay={replay} originalAttempts={originalAttempts} replayAttempts={replayAttempts} receipts={receipts} receiverReady={receiverReady} starting={send.isPending} reading={Boolean(run?.eventId && story.isPending)} evidenceUnavailable={originalEvidence.isError || replayEvidence.isError}>
          {run && !run.eventId && !send.isPending && <div className="demo-next-action"><p>The send has not been confirmed. Continue with the same event, without creating a duplicate.</p><button className="demo-primary" type="button" onClick={() => send.mutate(runRef.current!)}>Continue sending this event</button></div>}
          {original?.status === "dead_lettered" && scenario.strategy === "replay" && !replay && !waitingForReplay && <div className="demo-next-action">
            {!receiverReady ? <button className="demo-primary" type="button" disabled={busy || receiver.isPending || receiver.isError} onClick={() => repair.mutate()}>{repair.isPending ? "Restoring…" : "1. Bring the test receiver back online"}</button>
              : <button className="demo-primary" type="button" disabled={busy} onClick={() => setReplayOpen(true)}>2. Review and resend the saved event</button>}
            <small>{receiverReady ? "A resend is a new delivery of the same stored event." : "This changes only the test receiver. It does not resend the event."}</small>
          </div>}
          {waitingForReplay && <p className="demo-fineprint">The resend was accepted. Waiting for its stored delivery…</p>}
          {terminal(current) && !locked && (current?.status === "delivered" || scenario.strategy !== "replay" || replay) && <div className="demo-next-action">
            {rejectedAsExpected && <Link className="demo-secondary" to={"/?event_type=" + activeDefinition.type + "&new_event=1"}>Start a corrected event →</Link>}
            <button className="demo-secondary" type="button" onClick={prepareNext}>Try another case →</button>
          </div>}
          {send.isError && <ErrorState error={send.error} />}
          {repair.isError && <ErrorState error={repair.error} />}
          {resend.isError && <ErrorState error={resend.error} />}
          {story.isError && run?.eventId && <ErrorState error={story.error} retry={() => void story.refetch()} />}
          {receiver.isError && <ErrorState error={receiver.error} retry={() => void receiver.refetch()} />}
        </DeliveryStory>
      </div>
    </div>

    <details className="demo-technical" open={technicalOpen} onToggle={(event) => setTechnicalOpen(event.currentTarget.open)}>
      <summary><span className="technical-icon" aria-hidden="true">{technicalOpen ? "−" : "+"}</span><span><strong>See what happened behind the scenes</strong><small>Request body, HTTP responses, receiver receipts, and saved delivery history</small></span><span className="technical-toggle">{technicalOpen ? "Hide details" : "Show details"}</span></summary>
      {technicalOpen && <div className="demo-technical__content">
        <p>These details come from the same run above. The database keeps the event and delivery attempts. The test receiver’s separate receipt log is temporary and can be cleared by a restart.</p>
        {run?.eventId && <Link className="demo-secondary" to={"/events/" + run.eventId}>Open the full event record →</Link>}
        <EventJourney scenario={scenario} demoMode="operator" tourPhase={null} tourMessage="The same recorded event, with its technical details expanded." isStarting={send.isPending} runId={run?.runId} event={story.data ?? null} endpointName={endpoint.data?.name ?? activeDefinition.destinationName} endpointUrl={endpoint.data?.url ?? activeDefinition.destinationUrl} originalDelivery={original ?? null} replayDelivery={replay ?? null} originalAttempts={originalAttempts} replayAttempts={replayAttempts} receiverObservations={receipts} receiverControlStatus={!run ? "idle" : receiver.isError ? "unavailable" : receiver.isPending ? "loading" : "ready"} repairOccurred={Boolean(original?.status === "dead_lettered" && scenario.strategy === "replay" && (receiverReady || replay))} replayOccurred={Boolean(replay)} maxAttempts={4} />
        {story.data && <EventTimeline deliveries={story.data.deliveries} />}
      </div>}
    </details>
    {original && <ReplayConfirmation open={replayOpen} deliveryId={original.id} busy={resend.isPending} onCancel={() => setReplayOpen(false)} onConfirm={() => { if (!resend.isPending) resend.mutate(); }} />}
  </div>;
}
