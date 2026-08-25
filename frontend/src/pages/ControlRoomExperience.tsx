import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import {
  ensureReceiverLabEndpoint,
  getDeliveryAttempts,
  getEndpoint,
  getEvent,
  getReceiverLab,
  publishDemoEvent,
  replayDelivery,
  setReceiverLabPreset,
} from "../api";
import { EventJourney } from "../components/EventJourney";
import { EventTimeline } from "../components/EventTimeline";
import { ErrorState } from "../components/QueryState";
import { ReplayConfirmation } from "../components/ReplayConfirmation";
import {
  DEFAULT_DEMO_SCENARIO_ID,
  DEMO_SCENARIOS,
  demoScenario,
  isDemoScenarioId,
} from "../demoScenarios";
import { shortId } from "../format";
import {
  runReliabilityTour,
  type ReliabilityTourProgress,
  type ReliabilityTourResult,
} from "../reliabilityTour";
import type { Delivery, DemoEventPayload, DemoScenarioId, EventAccepted } from "../types";

const STORY_EVENT_KEY = "eventharbor.control-room.event-id";
const STORY_TOUR_KEY = "eventharbor.control-room.tour";
const STORY_MODE_KEY = "eventharbor.control-room.mode";
const LOCAL_MAX_ATTEMPTS = 4;

type DemoMode = "guided" | "operator";

interface TourStart {
  runId: string;
  eventId?: string;
  replayDeliveryId?: string;
  payload: DemoEventPayload;
  scenarioId: DemoScenarioId;
}

interface EventDraft {
  orderId: string;
  amount: string;
  note: string;
}

function draftFromPayload(payload: DemoEventPayload): EventDraft {
  return {
    orderId: payload.order_id,
    amount: (payload.amount_cents / 100).toFixed(2),
    note: payload.note,
  };
}

function initialEventDraft(): EventDraft {
  const suffix = crypto.randomUUID().slice(0, 8).toUpperCase();
  return {
    orderId: `ORDER-${suffix}`,
    amount: "129.00",
    note: "Paid order ready for fulfillment",
  };
}

function payloadFromDraft(draft: EventDraft): DemoEventPayload {
  return {
    order_id: draft.orderId.trim(),
    amount_cents: Math.round(Number.parseFloat(draft.amount) * 100),
    note: draft.note.trim(),
  };
}

function storedTourStart(): TourStart | null {
  try {
    const raw = sessionStorage.getItem(STORY_TOUR_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<TourStart>;
    if (
      typeof value.runId !== "string" ||
      typeof value.payload?.order_id !== "string" ||
      typeof value.payload.amount_cents !== "number" ||
      typeof value.payload.note !== "string" ||
      (value.eventId !== undefined && typeof value.eventId !== "string") ||
      (value.replayDeliveryId !== undefined && typeof value.replayDeliveryId !== "string")
    ) return null;
    return {
      ...value,
      scenarioId: isDemoScenarioId(value.scenarioId)
        ? value.scenarioId
        : DEFAULT_DEMO_SCENARIO_ID,
    } as TourStart;
  } catch {
    return null;
  }
}

function rememberTourStart(tour: TourStart | null): void {
  try {
    if (tour) sessionStorage.setItem(STORY_TOUR_KEY, JSON.stringify(tour));
    else sessionStorage.removeItem(STORY_TOUR_KEY);
  } catch {
    // Reload recovery is optional when browser storage is disabled.
  }
}

function storedStoryMode(): DemoMode | null {
  try {
    const value = sessionStorage.getItem(STORY_MODE_KEY);
    return value === "guided" || value === "operator" ? value : null;
  } catch {
    return null;
  }
}

function rememberStoryMode(mode: DemoMode | null): void {
  try {
    if (mode) sessionStorage.setItem(STORY_MODE_KEY, mode);
    else sessionStorage.removeItem(STORY_MODE_KEY);
  } catch {
    // Actor provenance is best-effort when browser storage is disabled.
  }
}

function storedStoryEvent(): string | null {
  try {
    return sessionStorage.getItem(STORY_EVENT_KEY);
  } catch {
    return null;
  }
}

function rememberStoryEvent(eventId: string | null): void {
  try {
    if (eventId) sessionStorage.setItem(STORY_EVENT_KEY, eventId);
    else sessionStorage.removeItem(STORY_EVENT_KEY);
  } catch {
    // The live journey still works when browser storage is disabled.
  }
}

function latestDelivery(deliveries: Delivery[] | undefined): Delivery | undefined {
  return deliveries?.reduce<Delivery | undefined>((latest, delivery) => {
    if (!latest || delivery.replay_generation > latest.replay_generation) return delivery;
    return latest;
  }, undefined);
}

function deliveryAtGeneration(deliveries: Delivery[] | undefined, generation: number): Delivery | undefined {
  return deliveries?.find((delivery) => delivery.replay_generation === generation);
}

function waitWithAbort(durationMs: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new Error("The reliability tour was paused."));
      return;
    }
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new Error("The reliability tour was paused."));
    };
    const timer = window.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, durationMs);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export function ControlRoomExperience() {
  const initialTour = storedTourStart();
  const queryClient = useQueryClient();
  const tourAbort = useRef<AbortController | null>(null);
  const [mode, setMode] = useState<DemoMode>(() => storedStoryMode() ?? "guided");
  const [storyMode, setStoryMode] = useState<DemoMode | null>(() => storedStoryMode());
  const [draftScenarioId, setDraftScenarioId] = useState<DemoScenarioId>(
    () => initialTour?.scenarioId ?? DEFAULT_DEMO_SCENARIO_ID,
  );
  const [restoredTour, setRestoredTour] = useState<TourStart | null>(initialTour);
  const [eventDraft, setEventDraft] = useState<EventDraft>(
    () => initialTour ? draftFromPayload(initialTour.payload) : initialEventDraft(),
  );
  const [storyEventId, setStoryEventId] = useState<string | null>(() => storedStoryEvent());
  const [tourProgress, setTourProgress] = useState<ReliabilityTourProgress | null>(null);
  const [replayOpen, setReplayOpen] = useState(false);
  const [preparingNext, setPreparingNext] = useState(false);
  const draftScenario = demoScenario(draftScenarioId);

  const story = useQuery({
    queryKey: ["event", storyEventId],
    queryFn: () => getEvent(storyEventId!),
    enabled: Boolean(storyEventId),
    refetchInterval: (query) => {
      const current = latestDelivery(query.state.data?.deliveries);
      const storedScenarioId = query.state.data?.data.scenario;
      const queryScenario = demoScenario(
        isDemoScenarioId(storedScenarioId)
          ? storedScenarioId
          : restoredTour?.scenarioId ?? DEFAULT_DEMO_SCENARIO_ID,
      );
      if (current?.status === "delivered") return false;
      if (current?.status === "dead_lettered" && ((storyMode ?? mode) === "operator" || queryScenario.strategy === "terminal")) return false;
      return 700;
    },
  });
  const storedStoryScenarioId = story.data?.data.scenario;
  const activeScenarioId = isDemoScenarioId(storedStoryScenarioId)
    ? storedStoryScenarioId
    : restoredTour?.scenarioId ?? draftScenarioId;
  const activeScenario = demoScenario(activeScenarioId);
  const storyRunId = typeof story.data?.data.run_id === "string" ? story.data.data.run_id : undefined;
  const activeRunId = restoredTour?.runId ?? storyRunId;
  const receiver = useQuery({
    queryKey: ["receiver-lab", activeRunId ?? "inactive"],
    queryFn: () => getReceiverLab(activeRunId),
    enabled: Boolean(activeRunId),
    refetchInterval: activeRunId ? 600 : false,
  });

  const sourceDelivery = deliveryAtGeneration(story.data?.deliveries, 0);
  const replayGeneration = deliveryAtGeneration(story.data?.deliveries, 1);
  const currentDelivery = latestDelivery(story.data?.deliveries);
  const endpointEvidence = useQuery({
    queryKey: ["endpoint", sourceDelivery?.endpoint_id],
    queryFn: () => getEndpoint(sourceDelivery!.endpoint_id),
    enabled: Boolean(sourceDelivery),
  });
  const sourceAttempts = useQuery({
    queryKey: ["delivery-attempts", sourceDelivery?.id],
    queryFn: () => getDeliveryAttempts(sourceDelivery!.id),
    enabled: Boolean(sourceDelivery),
    refetchInterval: sourceDelivery && !["dead_lettered", "delivered"].includes(sourceDelivery.status) ? 450 : false,
  });
  const replayAttempts = useQuery({
    queryKey: ["delivery-attempts", replayGeneration?.id],
    queryFn: () => getDeliveryAttempts(replayGeneration!.id),
    enabled: Boolean(replayGeneration),
    refetchInterval: replayGeneration && !["dead_lettered", "delivered"].includes(replayGeneration.status) ? 450 : false,
  });

  const storyComplete = Boolean(
    currentDelivery?.status === "delivered" ||
    (activeScenario.strategy === "terminal" && currentDelivery?.status === "dead_lettered"),
  );
  const storyDeadLettered = currentDelivery?.status === "dead_lettered";
  const receiverReady = receiver.data?.preset === "success";

  const guidedTour = useMutation<ReliabilityTourResult, Error, TourStart>({
    mutationFn: async ({ runId, eventId, replayDeliveryId, payload, scenarioId: activeScenarioId }) => {
      const controller = new AbortController();
      tourAbort.current?.abort();
      tourAbort.current = controller;
      return runReliabilityTour(
        {
          getReceiverLab,
          setReceiverLabPreset,
          ensureReceiverLabEndpoint,
          publishDemoEvent,
          getEvent,
          replayDelivery,
          wait: waitWithAbort,
          now: Date.now,
        },
        {
          runId,
          eventId,
          replayDeliveryId,
          payload,
          scenarioId: activeScenarioId,
          pollIntervalMs: 350,
          presentationPauseMs: 550,
          timeoutMs: 90_000,
          signal: controller.signal,
          onProgress: (progress) => {
            setTourProgress(progress);
            if (progress.eventId) {
              rememberStoryEvent(progress.eventId);
              setStoryEventId(progress.eventId);
            }
            const resumable: TourStart = {
              runId,
              eventId: progress.eventId ?? eventId,
              replayDeliveryId,
              payload,
              scenarioId: activeScenarioId,
            };
            rememberTourStart(resumable);
            setRestoredTour(resumable);
            if (progress.event) queryClient.setQueryData(["event", progress.event.id], progress.event);
          },
        },
      );
    },
    onSuccess: async (result, variables) => {
      setTourProgress(result);
      const resumable: TourStart = {
        runId: result.runId,
        eventId: result.eventId ?? variables.eventId,
        replayDeliveryId: result.replayDeliveryId ?? variables.replayDeliveryId,
        payload: result.payload ?? variables.payload,
        scenarioId: result.scenarioId,
      };
      rememberTourStart(resumable);
      setRestoredTour(resumable);
      if (result.event) queryClient.setQueryData(["event", result.event.id], result.event);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["receiver-lab"] }),
        queryClient.invalidateQueries({ queryKey: ["events"] }),
        queryClient.invalidateQueries({ queryKey: ["dead-letters"] }),
        queryClient.invalidateQueries({ queryKey: ["delivery-attempts"] }),
      ]);
    },
  });

  const runOperatorStory = useMutation<EventAccepted, Error, TourStart>({
    mutationFn: async (start) => {
      const scenario = demoScenario(start.scenarioId);
      await setReceiverLabPreset(scenario.preset, start.runId);
      const endpoint = await ensureReceiverLabEndpoint();
      return publishDemoEvent(
        endpoint.id,
        start.runId,
        `control-room-story-${start.runId}`,
        start.payload,
        start.scenarioId,
      );
    },
    onSuccess: async (accepted, start) => {
      const active: TourStart = { ...start, eventId: accepted.event_id };
      rememberTourStart(active);
      setRestoredTour(active);
      rememberStoryEvent(accepted.event_id);
      setStoryEventId(accepted.event_id);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["receiver-lab"] }),
        queryClient.invalidateQueries({ queryKey: ["events"] }),
      ]);
    },
  });

  const repairReceiver = useMutation({
    mutationFn: async () => {
      const receiverState = await getReceiverLab(activeRunId);
      return receiverState.preset === "success"
        ? receiverState
        : setReceiverLabPreset("success", activeRunId);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["receiver-lab"] });
    },
  });

  const approveReplay = useMutation({
    mutationFn: (deliveryId: string) => replayDelivery(deliveryId),
    onSuccess: async (accepted) => {
      setReplayOpen(false);
      if (restoredTour) {
        const resumable = { ...restoredTour, replayDeliveryId: accepted.delivery_id };
        rememberTourStart(resumable);
        setRestoredTour(resumable);
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["event", storyEventId] }),
        queryClient.invalidateQueries({ queryKey: ["dead-letters"] }),
      ]);
    },
  });

  useEffect(() => () => tourAbort.current?.abort(), []);

  function clearBrowserStory(): void {
    tourAbort.current?.abort();
    rememberStoryEvent(null);
    rememberTourStart(null);
    rememberStoryMode(null);
    setRestoredTour(null);
    setStoryMode(null);
    setStoryEventId(null);
    setTourProgress(null);
    setReplayOpen(false);
    setPreparingNext(false);
    guidedTour.reset();
    runOperatorStory.reset();
    repairReceiver.reset();
    approveReplay.reset();
  }

  function startGuidedTour(): void {
    if (guidedTour.isPending) return;
    const next: TourStart = {
      runId: crypto.randomUUID(),
      payload: payloadFromDraft(eventDraft),
      scenarioId: draftScenarioId,
    };
    clearBrowserStory();
    setMode("guided");
    setStoryMode("guided");
    rememberStoryMode("guided");
    rememberTourStart(next);
    setRestoredTour(next);
    guidedTour.mutate(next);
    window.requestAnimationFrame(() => {
      document.getElementById("live-proof")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  function resumeGuidedTour(): void {
    const prior = guidedTour.data;
    if (!prior && !restoredTour) return;
    guidedTour.mutate({
      runId: prior?.runId ?? restoredTour!.runId,
      eventId: prior?.eventId ?? restoredTour?.eventId,
      replayDeliveryId: prior?.replayDeliveryId ?? restoredTour?.replayDeliveryId,
      payload: prior?.payload ?? restoredTour?.payload ?? payloadFromDraft(eventDraft),
      scenarioId: prior?.scenarioId ?? restoredTour?.scenarioId ?? draftScenarioId,
    });
  }

  function startOperatorStory(): void {
    if (runOperatorStory.isPending) return;
    const next: TourStart = {
      runId: crypto.randomUUID(),
      payload: payloadFromDraft(eventDraft),
      scenarioId: draftScenarioId,
    };
    clearBrowserStory();
    setMode("operator");
    setStoryMode("operator");
    rememberStoryMode("operator");
    rememberTourStart(next);
    setRestoredTour(next);
    runOperatorStory.mutate(next);
    window.requestAnimationFrame(() => {
      document.getElementById("live-proof")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  function prepareAnother(): void {
    const currentIndex = DEMO_SCENARIOS.findIndex((scenario) => scenario.id === activeScenario.id);
    const nextScenario = DEMO_SCENARIOS[(currentIndex + 1) % DEMO_SCENARIOS.length];
    if (nextScenario) setDraftScenarioId(nextScenario.id);
    setEventDraft(initialEventDraft());
    setPreparingNext(true);
    window.requestAnimationFrame(() => {
      document.getElementById("demo-input")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  const currentReceiverObservations = receiver.data?.requests.filter(
    (observation) => observation.event_id === storyEventId,
  ) ?? [];
  const destinationRepaired = Boolean(
    sourceDelivery?.status === "dead_lettered" &&
    (
      replayGeneration ||
      receiverReady ||
      currentReceiverObservations.some((observation) => observation.response_status_code === 200)
    ),
  );
  const receiverControlStatus = !activeRunId
    ? "idle"
    : receiver.isPending
      ? "loading"
      : receiver.isError
        ? "unavailable"
        : "ready";
  const amount = Number.parseFloat(eventDraft.amount);
  const eventDraftValid = eventDraft.orderId.trim().length > 0 &&
    eventDraft.note.trim().length > 0 &&
    Number.isFinite(amount) && amount > 0;
  const restoredCanResume = Boolean(
    restoredTour &&
    !guidedTour.isPending &&
    (restoredTour.eventId ? story.data && !storyComplete : !storyEventId),
  );
  const guidedCanResume = guidedTour.data?.phase === "paused" || restoredCanResume;
  const guidedFailed = guidedTour.data?.phase === "failed";
  const activeMessage = tourProgress?.message
    ?? (storyEventId
      ? "Reading durable state and independent receiver evidence."
      : "Ready. The live journey will update only from observed system state.");
  const journeyStarted = Boolean(
    guidedTour.isPending ||
    runOperatorStory.isPending ||
    storyEventId ||
    restoredTour,
  );
  const journeyLoading = Boolean(
    guidedTour.isPending ||
    runOperatorStory.isPending ||
    (storyEventId && !story.data),
  );
  const activeMode = storyMode ?? mode;
  const runFailed = guidedFailed || guidedTour.isError || runOperatorStory.isError;
  const formLocked = journeyStarted && !storyComplete && !preparingNext && !runFailed;
  const activeOrderId = typeof story.data?.data.order_id === "string"
    ? story.data.data.order_id
    : restoredTour?.payload.order_id ?? "Accepting order…";
  const activeAmountCents = typeof story.data?.data.amount_cents === "number"
    ? story.data.data.amount_cents
    : restoredTour?.payload.amount_cents;
  const activeAmount = activeAmountCents === undefined ? "—" : (activeAmountCents / 100).toFixed(2);
  const journeyActionMessage = storyComplete
    ? "This completed event stays on screen while you prepare the next failure scenario above."
    : activeMessage;

  return (
    <>
      <section className="control-hero" id="demo-input" data-running={formLocked}>
        <div className="hero-copy">
          <p className="eyebrow">Live webhook journey · real API, database, and HTTP</p>
          <h1>Watch EventHarbor save, retry, and recover one webhook.</h1>
          <p className="lede">
            Send an order from this browser. EventHarbor stores it in PostgreSQL before a background worker
            sends it to a separate Receiver Lab service. The receiver&apos;s actual HTTP responses drive the result.
          </p>
          <div className="plain-flow" aria-label="The order moves from this browser through EventHarbor to Receiver Lab">
            <span>Browser UI</span><i aria-hidden="true">→</i><strong>Store + deliver</strong><i aria-hidden="true">→</i><span>Receiver Lab</span>
          </div>
          <ul className="hero-trust-strip" aria-label="Live demonstration guarantees">
            <li>Real API call</li><li>Stored before delivery</li><li>Separate receiver service</li><li>Independent receipts</li>
          </ul>
        </div>

        <form
          className="scenario-panel event-composer"
          aria-labelledby="event-composer-title"
          onSubmit={(event) => {
            event.preventDefault();
            if (formLocked) return;
            if (mode === "guided") startGuidedTour();
            else startOperatorStory();
          }}
        >
          <div className="composer-topline">
            <span className="section-index">1 / Choose how the receiver behaves</span>
            <div className="demo-mode-control demo-mode-control--compact" role="group" aria-label="Demo mode">
              <button type="button" disabled={formLocked} aria-label="Guided run: automatically demonstrate failure and recovery" aria-pressed={mode === "guided"} onClick={() => setMode("guided")}>Guided</button>
              <button type="button" disabled={formLocked} aria-label="Manual controls: you trigger receiver restoration and replay" aria-pressed={mode === "operator"} onClick={() => setMode("operator")}>Manual</button>
            </div>
          </div>
          <h2 id="event-composer-title">Choose the failure. Then send the order.</h2>

          {journeyStarted ? (
            <div className="composer-current-story" data-complete={storyComplete}>
              <span>{storyComplete ? "Completed story preserved below" : runFailed ? "Previous attempt preserved below" : "Current story running below"}</span>
              <strong>{activeOrderId}</strong>
              <small>{storyComplete || runFailed ? "You can prepare the next event without removing this result." : "Finish or pause this event before replacing it."}</small>
            </div>
          ) : null}

          <div className="scenario-picker" role="group" aria-label="Receiver incident">
            {DEMO_SCENARIOS.map((scenario) => (
              <button
                type="button"
                disabled={formLocked}
                aria-pressed={scenario.id === draftScenarioId}
                className="scenario-choice"
                key={scenario.id}
                onClick={() => setDraftScenarioId(scenario.id)}
              >
                <strong>{scenario.label}</strong>
                <span>{scenario.sequence.map((step) => step.toUpperCase()).join(" → ")}</span>
              </button>
            ))}
          </div>

          <fieldset disabled={formLocked}>
            <label className="event-composer__type"><span>Event type</span><code>demo.order.paid</code></label>
            <label><span>Order ID</span><input value={eventDraft.orderId} maxLength={80} onChange={(event) => setEventDraft((current) => ({ ...current, orderId: event.target.value }))} /></label>
            <label><span>Amount</span><span className="event-composer__money"><i aria-hidden="true">$</i><input type="number" min="0.01" step="0.01" value={eventDraft.amount} onChange={(event) => setEventDraft((current) => ({ ...current, amount: event.target.value }))} /></span></label>
            <label className="event-composer__note"><span>Note</span><textarea value={eventDraft.note} maxLength={160} rows={2} onChange={(event) => setEventDraft((current) => ({ ...current, note: event.target.value }))} /></label>
          </fieldset>

          <div className="receiver-contract">
            <span>What will happen in Receiver Lab</span>
            <strong>{draftScenario.contract}</strong>
            <p><b>Concrete cause</b>{draftScenario.cause}</p>
            <small>{draftScenario.takeaway}</small>
          </div>
          <button className="story-action" type="submit" disabled={formLocked || !eventDraftValid}>
            <span>{formLocked ? "Current event is still running" : journeyStarted ? "Send this next event" : "Send this event and watch it move"}</span><span aria-hidden="true">→</span>
          </button>
          <small>Synthetic order data. Real API transaction, database state, worker requests, responses, and receiver receipts.</small>
        </form>
      </section>

      {journeyStarted ? (
        <section className="active-run-banner" aria-label="Active demonstration">
          <div><p className="eyebrow">Following this order</p><h1>{activeOrderId}</h1></div>
          <dl>
            <div><dt>Receiver behavior</dt><dd>{activeScenario.label}</dd></div>
            <div><dt>Order amount</dt><dd>${activeAmount}</dd></div>
            <div><dt>Event</dt><dd><code>{storyEventId ? shortId(storyEventId) : "Accepting…"}</code></dd></div>
          </dl>
        </section>
      ) : null}

      <EventJourney
        scenario={activeScenario}
        demoMode={activeMode}
        tourPhase={tourProgress?.phase ?? null}
        tourMessage={journeyActionMessage}
        isStarting={journeyLoading}
        runId={activeRunId}
        event={story.data ?? null}
        endpointName={endpointEvidence.data?.name ?? "Receiver Lab"}
        endpointUrl={endpointEvidence.data?.url ?? "http://receiver-lab:8100/webhooks"}
        originalDelivery={sourceDelivery ?? null}
        replayDelivery={replayGeneration ?? null}
        originalAttempts={sourceAttempts.data?.attempts ?? []}
        replayAttempts={replayAttempts.data?.attempts ?? []}
        receiverObservations={currentReceiverObservations}
        receiverControlStatus={receiverControlStatus}
        repairOccurred={destinationRepaired}
        replayOccurred={Boolean(replayGeneration)}
        maxAttempts={LOCAL_MAX_ATTEMPTS}
      >
        {journeyStarted ? (
          <>
            {activeMode === "guided" ? (
              <>
                {guidedTour.isPending ? (
                  <><button className="story-action" type="button" disabled><span>Following live system state…</span><span aria-hidden="true">●</span></button><button className="reliability-abandon" type="button" onClick={() => tourAbort.current?.abort()}>Pause demo</button></>
                ) : null}
                {guidedCanResume ? <button className="story-action" type="button" onClick={resumeGuidedTour}><span>Resume from stored evidence</span><span aria-hidden="true">→</span></button> : null}
                {guidedFailed ? <button className="story-action story-action-repair" type="button" onClick={prepareAnother}><span>Prepare a fresh event</span><span aria-hidden="true">↻</span></button> : null}
                {storyComplete && !guidedTour.isPending ? <button className="story-action story-action-complete" type="button" onClick={prepareAnother}><span>Set up another failure scenario</span><span aria-hidden="true">↗</span></button> : null}
              </>
            ) : (
              <>
                {runOperatorStory.isPending || (storyEventId && !storyDeadLettered && !storyComplete) ? <button className="story-action" type="button" disabled><span>Following live system state…</span><span aria-hidden="true">●</span></button> : null}
                {storyDeadLettered && activeScenario.strategy === "replay" && !receiverReady ? (
                  <>
                    <div className="recovery-action-explainer">
                      <span>What this control changes</span>
                      <strong>Only this Receiver Lab run: HTTP 503 → HTTP 200</strong>
                      <p>This is controlled fault injection, like the destination recovering after a restart or fixed deployment. It does not change EventHarbor, erase the failures, or resend the webhook.</p>
                    </div>
                    <button className="story-action story-action-repair" type="button" disabled={repairReceiver.isPending} onClick={() => repairReceiver.mutate()}><span>{repairReceiver.isPending ? "Changing the next response to HTTP 200…" : "Switch this test receiver to HTTP 200"}</span><span aria-hidden="true">→</span></button>
                  </>
                ) : null}
                {storyDeadLettered && activeScenario.strategy === "replay" && receiverReady && sourceDelivery && !replayGeneration ? (
                  <>
                    <div className="recovery-action-explainer" data-restored="true">
                      <span>Test receiver restored</span>
                      <strong>Receiver Lab now returns HTTP 200 for this run.</strong>
                      <p>The original delivery is still stopped and preserved. Nothing is resent until you create the separate replay below.</p>
                    </div>
                    <button className="story-action" type="button" onClick={() => setReplayOpen(true)}><span>Review and create a separate replay</span><span aria-hidden="true">→</span></button>
                  </>
                ) : null}
                {storyComplete ? <button className="story-action story-action-complete" type="button" onClick={prepareAnother}><span>Set up another failure scenario</span><span aria-hidden="true">↗</span></button> : null}
              </>
            )}
            {guidedTour.isError ? <ErrorState error={guidedTour.error} /> : null}
            {runOperatorStory.isError ? <ErrorState error={runOperatorStory.error} /> : null}
            {repairReceiver.isError ? <ErrorState error={repairReceiver.error} /> : null}
            {approveReplay.isError ? <ErrorState error={approveReplay.error} /> : null}
            {story.isError && storyEventId ? <ErrorState error={story.error} retry={() => void story.refetch()} /> : null}
          </>
        ) : null}
      </EventJourney>

      {story.data ? (
        <details className="technical-evidence journey-postgres-lineage">
          <summary>View PostgreSQL delivery lineage</summary>
          <EventTimeline deliveries={story.data.deliveries} />
        </details>
      ) : null}

      {sourceDelivery ? (
        <ReplayConfirmation
          open={replayOpen}
          deliveryId={sourceDelivery.id}
          busy={approveReplay.isPending}
          onCancel={() => setReplayOpen(false)}
          onConfirm={() => approveReplay.mutate(sourceDelivery.id)}
        />
      ) : null}
      {storyEventId ? <Link className="event-reference control-room-event-link" to={`/events/${storyEventId}`}>Open the complete event record / {shortId(storyEventId)} →</Link> : null}
    </>
  );
}
