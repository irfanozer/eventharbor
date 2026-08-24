import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import {
  ensureReceiverLabEndpoint,
  getDeliveryAttempts,
  getEvent,
  getOverview,
  getReceiverLab,
  publishDemoEvent,
  replayDelivery,
  setReceiverLabPreset,
} from "../api";
import { EventTimeline } from "../components/EventTimeline";
import { ErrorState, LoadingState } from "../components/QueryState";
import {
  DemoNarrative,
  EngineeringDecisions,
  PlainEnglishFlow,
  ProofReceipt,
  StoryProgress,
  type ReliabilityStoryPhase,
} from "../components/ReliabilityProof";
import { ReplayConfirmation } from "../components/ReplayConfirmation";
import { formatDate, shortId } from "../format";
import {
  runReliabilityTour,
  type ReliabilityTourProgress,
  type ReliabilityTourResult,
} from "../reliabilityTour";
import type { Delivery, DeliveryAttempt, EventDetail } from "../types";

const STORY_EVENT_KEY = "eventharbor.control-room.event-id";

type DemoMode = "guided" | "operator";

interface TourStart {
  runId: string;
  eventId?: string;
  replayDeliveryId?: string;
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
    // The live proof still works when browser storage is disabled.
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

function storyPhase(event: EventDetail | undefined, progress: ReliabilityTourProgress | null): ReliabilityStoryPhase {
  if (progress) {
    if (progress.phase === "accepted") return "accepted";
    if (progress.phase === "failing") return "retrying";
    if (progress.phase === "contained" || progress.phase === "repairing") return "dead_lettered";
    if (progress.phase === "replaying") return "replaying";
    if (progress.phase === "verified") return "verified";
  }

  const current = latestDelivery(event?.deliveries);
  if (!current) return "ready";
  if (current.replay_generation > 0 && current.status === "delivered") return "verified";
  if (current.replay_generation > 0) return "replaying";
  if (current.status === "dead_lettered") return "dead_lettered";
  if (current.attempt_count > 0 || current.status === "retry_wait" || current.status === "in_progress") return "retrying";
  return "accepted";
}

function attemptOutcome(attempt: DeliveryAttempt): "failed" | "delivered" | "waiting" {
  if (attempt.status === "in_progress") return "waiting";
  if (attempt.disposition === "succeeded") return "delivered";
  return "failed";
}

function attemptResult(attempt: DeliveryAttempt): string {
  if (attempt.http_status_code) return `HTTP ${attempt.http_status_code}`;
  if (attempt.status === "in_progress") return "HTTP request active";
  return attempt.error_type?.replaceAll("_", " ") ?? "Network failure";
}

export function ControlRoomPage() {
  const queryClient = useQueryClient();
  const tourAbort = useRef<AbortController | null>(null);
  const [mode, setMode] = useState<DemoMode>("guided");
  const [storyEventId, setStoryEventId] = useState<string | null>(() => storedStoryEvent());
  const [tourProgress, setTourProgress] = useState<ReliabilityTourProgress | null>(null);
  const [replayOpen, setReplayOpen] = useState(false);

  const overview = useQuery({
    queryKey: ["overview"],
    queryFn: getOverview,
    refetchInterval: 3_000,
  });
  const receiver = useQuery({
    queryKey: ["receiver-lab"],
    queryFn: getReceiverLab,
    refetchInterval: storyEventId ? 2_000 : false,
  });
  const story = useQuery({
    queryKey: ["event", storyEventId],
    queryFn: () => getEvent(storyEventId!),
    enabled: Boolean(storyEventId),
    refetchInterval: (query) => {
      const latest = latestDelivery(query.state.data?.deliveries);
      if (latest?.replay_generation && (latest.status === "delivered" || latest.status === "dead_lettered")) return false;
      if (mode === "operator" && latest?.status === "dead_lettered") return false;
      return 900;
    },
  });

  const sourceDelivery = deliveryAtGeneration(story.data?.deliveries, 0);
  const replayGeneration = story.data?.deliveries.find((delivery) => delivery.replay_generation > 0);
  const currentDelivery = latestDelivery(story.data?.deliveries);
  const sourceAttempts = useQuery({
    queryKey: ["delivery-attempts", sourceDelivery?.id],
    queryFn: () => getDeliveryAttempts(sourceDelivery!.id),
    enabled: Boolean(sourceDelivery),
    refetchInterval: sourceDelivery && sourceDelivery.status !== "dead_lettered" && sourceDelivery.status !== "delivered" ? 700 : false,
  });
  const replayAttempts = useQuery({
    queryKey: ["delivery-attempts", replayGeneration?.id],
    queryFn: () => getDeliveryAttempts(replayGeneration!.id),
    enabled: Boolean(replayGeneration),
    refetchInterval: replayGeneration && replayGeneration.status !== "dead_lettered" && replayGeneration.status !== "delivered" ? 700 : false,
  });

  const receiverReady = receiver.data?.preset === "success";
  const storyComplete = currentDelivery?.status === "delivered" && currentDelivery.replay_generation > 0;
  const storyDeadLettered = currentDelivery?.status === "dead_lettered";

  const guidedTour = useMutation<ReliabilityTourResult, Error, TourStart>({
    mutationFn: async ({ runId, eventId, replayDeliveryId }) => {
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
          pollIntervalMs: 450,
          presentationPauseMs: 650,
          timeoutMs: 90_000,
          signal: controller.signal,
          onProgress: (progress) => {
            setTourProgress(progress);
            if (progress.eventId) {
              rememberStoryEvent(progress.eventId);
              setStoryEventId(progress.eventId);
            }
            if (progress.event) queryClient.setQueryData(["event", progress.event.id], progress.event);
          },
        },
      );
    },
    onSuccess: async (result) => {
      setTourProgress(result);
      if (result.event) queryClient.setQueryData(["event", result.event.id], result.event);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["overview"] }),
        queryClient.invalidateQueries({ queryKey: ["receiver-lab"] }),
        queryClient.invalidateQueries({ queryKey: ["events"] }),
        queryClient.invalidateQueries({ queryKey: ["dead-letters"] }),
        queryClient.invalidateQueries({ queryKey: ["delivery-attempts"] }),
      ]);
    },
  });

  const runOperatorStory = useMutation({
    mutationFn: async () => {
      const runId = crypto.randomUUID();
      const receiverState = await getReceiverLab();
      if (receiverState.preset !== "dead_letter") await setReceiverLabPreset("dead_letter");
      const endpoint = await ensureReceiverLabEndpoint();
      return publishDemoEvent(endpoint.id, runId, `control-room-story-${runId}`);
    },
    onSuccess: async (accepted) => {
      rememberStoryEvent(accepted.event_id);
      setStoryEventId(accepted.event_id);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["overview"] }),
        queryClient.invalidateQueries({ queryKey: ["receiver-lab"] }),
        queryClient.invalidateQueries({ queryKey: ["events"] }),
      ]);
    },
  });

  const repairReceiver = useMutation({
    mutationFn: async () => {
      const receiverState = await getReceiverLab();
      return receiverState.preset === "success" ? receiverState : setReceiverLabPreset("success");
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["receiver-lab"] });
    },
  });

  const approveReplay = useMutation({
    mutationFn: (deliveryId: string) => replayDelivery(deliveryId),
    onSuccess: async () => {
      setReplayOpen(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["event", storyEventId] }),
        queryClient.invalidateQueries({ queryKey: ["overview"] }),
        queryClient.invalidateQueries({ queryKey: ["dead-letters"] }),
      ]);
    },
  });

  useEffect(() => () => tourAbort.current?.abort(), []);

  function clearBrowserStory() {
    tourAbort.current?.abort();
    rememberStoryEvent(null);
    setStoryEventId(null);
    setTourProgress(null);
    setReplayOpen(false);
    guidedTour.reset();
    runOperatorStory.reset();
    approveReplay.reset();
  }

  function startGuidedTour() {
    if (guidedTour.isPending) return;
    clearBrowserStory();
    setMode("guided");
    guidedTour.mutate({ runId: crypto.randomUUID() });
    window.requestAnimationFrame(() => {
      document.getElementById("live-proof")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  function resumeGuidedTour() {
    const prior = guidedTour.data;
    if (!prior) return;
    guidedTour.mutate({
      runId: prior.runId,
      eventId: prior.eventId ?? undefined,
      replayDeliveryId: prior.replayDeliveryId ?? undefined,
    });
  }

  const visiblePhase = storyPhase(story.data, tourProgress);
  const failedAttemptCount = sourceAttempts.data?.attempts.length ?? sourceDelivery?.attempt_count ?? 0;
  const recoveredAttempt = replayAttempts.data?.attempts.at(-1);
  const allAttempts = useMemo(
    () => [
      ...(sourceAttempts.data?.attempts.map((attempt) => ({ attempt, generation: 0 })) ?? []),
      ...(replayAttempts.data?.attempts.map((attempt) => ({ attempt, generation: replayGeneration?.replay_generation ?? 1 })) ?? []),
    ],
    [replayAttempts.data?.attempts, replayGeneration?.replay_generation, sourceAttempts.data?.attempts],
  );

  const guidedStopped = guidedTour.data?.phase === "paused" || guidedTour.data?.phase === "failed";
  const guidedCanResume = guidedTour.data?.phase === "paused";
  const activeMessage = tourProgress?.message ?? "Ready to create one controlled failure and prove that recovery preserves its history.";

  return (
    <>
      <section className="control-hero">
        <div className="hero-copy">
          <p className="eyebrow">Working reliability system</p>
          <h1>When delivery fails,<br />the evidence shouldn&apos;t.</h1>
          <p className="lede">EventHarbor protects important messages when another system is unavailable. It records every attempt, stops retrying safely, and recovers without erasing what failed.</p>
          <p className="jargon-bridge"><strong>In plain English:</strong> a webhook is one application sending an HTTP message to another, like a payment service announcing that an invoice was paid.</p>
          <div className="plain-flow" aria-label="An application sends an important event through EventHarbor to a customer system">
            <span>Your app</span><i aria-hidden="true">→</i><strong>EventHarbor</strong><i aria-hidden="true">→</i><span>Customer system</span>
          </div>
        </div>

        <aside className="scenario-panel" aria-labelledby="hero-proof-title">
          <div className="section-index">LIVE PROOF / REAL SYSTEM</div>
          <h2 id="hero-proof-title">One event. Four failures. One clean recovery.</h2>
          <p>The guided demo creates a real outage, captures every HTTP 503, stops at a safe boundary, repairs the receiver, and delivers a traceable replay.</p>
          <ol className="hero-proof-sequence">
            <li><span>01</span>Saved before sending</li><li><span>02</span>Failures recorded</li><li><span>03</span>Retries stopped</li><li><span>04</span>History preserved</li>
          </ol>
          <button className="story-action" type="button" disabled={guidedTour.isPending} onClick={startGuidedTour}>
            <span>{guidedTour.isPending ? "Live proof running…" : storyComplete ? "Run another live proof" : "Run the 15-second proof"}</span><span aria-hidden="true">→</span>
          </button>
          <small>Real API calls · real PostgreSQL state · real HTTP attempts · no mocked statuses</small>
        </aside>
      </section>

      <section className="reliability-demo" id="live-proof" aria-labelledby="live-proof-title">
        <header className="reliability-demo-header">
          <div><p className="eyebrow">See the system earn the claim</p><h2 id="live-proof-title">A complete reliability story, in one click.</h2></div>
          <div className="demo-mode-control">
            <span>Demo control</span>
            <div className="demo-mode-switch" aria-label="Choose demonstration mode">
              <button type="button" aria-pressed={mode === "guided"} disabled={guidedTour.isPending} onClick={() => setMode("guided")}>Guided</button>
              <button type="button" aria-pressed={mode === "operator"} disabled={guidedTour.isPending} onClick={() => setMode("operator")}>Operator</button>
            </div>
          </div>
        </header>

        <StoryProgress current={visiblePhase} />
        <div className="reliability-demo-stage">
          <div className="reliability-demo-copy">
            <DemoNarrative phase={visiblePhase} eventLabel={storyEventId ? shortId(storyEventId) : undefined} attemptNumber={sourceDelivery?.attempt_count} maximumAttempts={4} />
            <div className="reliability-demo-actions">
              {mode === "guided" ? (
                <>
                  {!guidedTour.isPending && !guidedStopped && !storyComplete ? <button className="story-action" type="button" onClick={startGuidedTour}><span>Run the live recovery demo</span><span aria-hidden="true">→</span></button> : null}
                  {guidedTour.isPending ? <><button className="story-action" type="button" disabled><span>Following durable system state…</span><span aria-hidden="true">●</span></button><button className="reliability-abandon" type="button" onClick={() => tourAbort.current?.abort()}>Pause demo</button></> : null}
                  {guidedCanResume ? <button className="story-action" type="button" onClick={resumeGuidedTour}><span>Resume from preserved evidence</span><span aria-hidden="true">→</span></button> : null}
                  {guidedTour.data?.phase === "failed" ? <button className="story-action story-action-repair" type="button" onClick={startGuidedTour}><span>Start a fresh controlled proof</span><span aria-hidden="true">↻</span></button> : null}
                  {storyComplete && !guidedTour.isPending ? <button className="story-action story-action-complete" type="button" onClick={startGuidedTour}><span>Run another event</span><span aria-hidden="true">↻</span></button> : null}
                  <small>{activeMessage}</small>
                </>
              ) : (
                <div className="operator-actions">
                  {!storyEventId ? <button className="story-action" type="button" disabled={runOperatorStory.isPending} onClick={() => runOperatorStory.mutate()}><span>{runOperatorStory.isPending ? "Preparing failure…" : "1. Create controlled failure"}</span><span aria-hidden="true">→</span></button> : null}
                  {storyDeadLettered && !receiverReady ? <button className="story-action story-action-repair" type="button" disabled={repairReceiver.isPending} onClick={() => repairReceiver.mutate()}><span>{repairReceiver.isPending ? "Repairing receiver…" : "2. Repair receiver"}</span><span aria-hidden="true">→</span></button> : null}
                  {storyDeadLettered && receiverReady && sourceDelivery && !approveReplay.isSuccess ? <button className="story-action" type="button" onClick={() => setReplayOpen(true)}><span>3. Review and approve replay</span><span aria-hidden="true">→</span></button> : null}
                  {storyComplete ? <button className="story-action story-action-complete" type="button" onClick={clearBrowserStory}><span>Prepare another operator run</span><span aria-hidden="true">↻</span></button> : null}
                  <small>Operator mode pauses at the failure boundary so you can inspect, repair, and authorize recovery yourself.</small>
                </div>
              )}
              {guidedTour.isError ? <ErrorState error={guidedTour.error} /> : null}
              {runOperatorStory.isError ? <ErrorState error={runOperatorStory.error} /> : null}
              {repairReceiver.isError ? <ErrorState error={repairReceiver.error} /> : null}
              {approveReplay.isError ? <ErrorState error={approveReplay.error} /> : null}
              {story.isError && storyEventId ? <ErrorState error={story.error} retry={() => void story.refetch()} /> : null}
            </div>
          </div>

          <aside className="reliability-evidence" aria-label="Live delivery evidence">
            <header className="reliability-evidence__header"><span>PostgreSQL-backed evidence</span><strong>{storyEventId ? "Live" : "Ready"}</strong></header>
            <div className="reliability-evidence__event"><div><span>Event record</span><strong>{storyEventId ?? "Created when the proof begins"}</strong></div><em>{storyEventId ? "202 accepted" : "waiting"}</em></div>
            <div className="attempt-proof">
              <header><h3>Persisted HTTP attempts</h3><span>{allAttempts.length} / expected 5</span></header>
              {allAttempts.length ? (
                <ol className="attempt-proof-list">
                  {allAttempts.map(({ attempt, generation }) => <li key={attempt.id} data-outcome={attemptOutcome(attempt)}><span>G{generation}.{attempt.attempt_number}</span><strong>{attemptResult(attempt)}</strong><code>{attemptOutcome(attempt) === "delivered" ? "delivered" : attempt.status === "in_progress" ? "reserved first" : "recorded"}</code></li>)}
                </ol>
              ) : <div className="attempt-proof-empty">No decorative counters.<br />Real worker attempts appear here as PostgreSQL confirms them.</div>}
            </div>
            <div className="generation-proof" aria-label="Delivery generation lineage">
              <div><span>Generation 0</span><strong>{sourceDelivery?.status.replaceAll("_", " ") ?? "Not created"}</strong><small>{sourceDelivery ? `${sourceDelivery.attempt_count} attempts retained` : "Original delivery"}</small></div>
              <i aria-hidden="true">→</i>
              <div><span>Generation 1</span><strong>{replayGeneration?.status.replaceAll("_", " ") ?? "Not created"}</strong><small>{replayGeneration ? "Appended recovery" : "Original history stays intact"}</small></div>
            </div>
          </aside>
        </div>
      </section>

      {storyComplete && storyEventId && sourceDelivery && replayGeneration && sourceAttempts.data && replayAttempts.data && recoveredAttempt ? (
        <ProofReceipt eventId={storyEventId} eventLabel={shortId(storyEventId)} failedAttempts={failedAttemptCount} retryStoppedSafely={sourceDelivery.status === "dead_lettered"} originalGenerationPreserved={sourceDelivery.status === "dead_lettered" && replayGeneration.replayed_from_delivery_id === sourceDelivery.id} replayGeneration={replayGeneration.replay_generation} failedHttpStatus={sourceAttempts.data.attempts.at(-1)?.http_status_code ?? undefined} recoveredHttpStatus={recoveredAttempt.http_status_code ?? undefined} />
      ) : null}

      <PlainEnglishFlow />
      <EngineeringDecisions />

      {story.data ? <details className="technical-evidence"><summary>Inspect the complete delivery timeline</summary><EventTimeline deliveries={story.data.deliveries} /></details> : null}

      <section className="overview-section" aria-labelledby="overview-title">
        <header className="section-heading-row"><div><p className="eyebrow">Live system status</p><h2 id="overview-title">The database, not a mock dashboard.</h2></div>{overview.data ? <time dateTime={overview.data.generated_at}>Snapshot {formatDate(overview.data.generated_at)}</time> : null}</header>
        {overview.isPending ? <LoadingState /> : null}
        {overview.isError ? <ErrorState error={overview.error} retry={() => void overview.refetch()} /> : null}
        {overview.data ? <div className="metric-grid"><article className="metric metric-primary"><span>Events accepted</span><strong>{overview.data.events_total}</strong><small>Immutable source records</small></article><article><span>Delivered</span><strong>{overview.data.deliveries.delivered}</strong><small>Latest generations</small></article><article className={overview.data.actionable_dead_letters ? "metric-alert" : ""}><span>Action required</span><strong>{overview.data.actionable_dead_letters}</strong><small>Latest-generation dead letters</small></article><article><span>Active endpoints</span><strong>{overview.data.endpoints_enabled}<i>/{overview.data.endpoints_total}</i></strong><small>Enabled / registered</small></article></div> : null}
      </section>

      <section className="receiver-strip" aria-labelledby="receiver-title">
        <div><p className="eyebrow">Deterministic test dependency</p><h2 id="receiver-title">Receiver Lab</h2></div>
        {receiver.isPending ? <span>Connecting…</span> : null}
        {receiver.isError ? <span className="receiver-down">Unavailable</span> : null}
        {receiver.data ? <dl><div><dt>Scenario</dt><dd>{receiver.data.preset?.replaceAll("_", " ") ?? receiver.data.configuration.mode}</dd></div><div><dt>Requests observed</dt><dd>{receiver.data.attempts}</dd></div><div><dt>Last signature</dt><dd>{receiver.data.requests.at(-1)?.signature_present ? "Present" : "No request yet"}</dd></div></dl> : null}
      </section>

      {sourceDelivery ? <ReplayConfirmation open={replayOpen} deliveryId={sourceDelivery.id} busy={approveReplay.isPending} onCancel={() => setReplayOpen(false)} onConfirm={() => approveReplay.mutate(sourceDelivery.id)} /> : null}
      {storyEventId ? <Link className="event-reference control-room-event-link" to={`/events/${storyEventId}`}>Open event evidence / {shortId(storyEventId)} →</Link> : null}
    </>
  );
}
