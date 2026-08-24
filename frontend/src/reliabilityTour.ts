import type {
  DemoScenarioId,
  DemoEventPayload,
  Endpoint,
  EventAccepted,
  EventDetail,
  ReceiverLabPreset,
  ReceiverLabState,
  ReplayAccepted,
} from "./types";
import { DEFAULT_DEMO_SCENARIO_ID, demoScenario, isDemoScenarioId } from "./demoScenarios";

export type ReliabilityTourPhase =
  | "idle"
  | "preparing"
  | "accepted"
  | "failing"
  | "contained"
  | "repairing"
  | "replaying"
  | "verified"
  | "paused"
  | "failed";

export interface ReliabilityTourProgress {
  phase: ReliabilityTourPhase;
  runId: string;
  eventId: string | null;
  event: EventDetail | null;
  /** Total reserved attempts across every delivery generation in the event. */
  attemptCount: number;
  scenarioId: DemoScenarioId;
  message: string;
}

export interface ReliabilityTourResult extends ReliabilityTourProgress {
  publishIdempotencyKey: string;
  replayIdempotencyKey: string;
  endpointId: string | null;
  sourceDeliveryId: string | null;
  replayDeliveryId: string | null;
  /** Retained so a paused pre-acceptance run can resume with identical business data. */
  payload?: DemoEventPayload;
}

export interface ReliabilityTourDependencies {
  getReceiverLab: (runId?: string) => Promise<ReceiverLabState>;
  setReceiverLabPreset: (preset: ReceiverLabPreset, runId?: string) => Promise<ReceiverLabState>;
  ensureReceiverLabEndpoint: () => Promise<Endpoint>;
  publishDemoEvent: (
    endpointId: string,
    runId: string,
    idempotencyKey: string,
    payload?: DemoEventPayload,
    scenarioId?: DemoScenarioId,
  ) => Promise<EventAccepted>;
  getEvent: (eventId: string) => Promise<EventDetail>;
  replayDelivery: (
    deliveryId: string,
    idempotencyKey: string,
  ) => Promise<ReplayAccepted>;
  /** Injected so the tour has no dependency on React or a particular timer implementation. */
  wait: (durationMs: number, signal?: AbortSignal) => Promise<void>;
  now: () => number;
  createRunId?: () => string;
}

export interface ReliabilityTourOptions {
  runId?: string;
  /** Supply these identifiers to resume a previously paused tour without clearing evidence. */
  eventId?: string;
  replayDeliveryId?: string;
  /** Exact synthetic order data to publish. Omit it to use the API helper defaults. */
  payload?: DemoEventPayload;
  /** Server-owned Receiver Lab behavior selected before this event is published. */
  scenarioId?: DemoScenarioId;
  pollIntervalMs?: number;
  presentationPauseMs?: number;
  timeoutMs?: number;
  signal?: AbortSignal;
  onProgress?: (progress: ReliabilityTourProgress) => void;
}

const DEFAULT_POLL_INTERVAL_MS = 500;
const DEFAULT_PRESENTATION_PAUSE_MS = 350;
const DEFAULT_TIMEOUT_MS = 90_000;

class TourPause extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TourPause";
  }
}

function attemptsIn(event: EventDetail | null): number {
  return event?.deliveries.reduce((total, delivery) => total + delivery.attempt_count, 0) ?? 0;
}

function messageFrom(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return "The reliability tour could not reach the next durable state.";
}

function assertDuration(name: string, value: number, allowZero: boolean): void {
  if (!Number.isFinite(value) || (allowZero ? value < 0 : value <= 0)) {
    throw new RangeError(`${name} must be ${allowZero ? "non-negative" : "positive"}.`);
  }
}

/**
 * Run one server-owned receiver scenario and follow only durable system state.
 *
 * Mutations use keys derived from one stable run ID. Progress is driven only by
 * fresh EventDetail responses; injected waits pace polling and presentation but
 * never manufacture a delivery transition.
 */
export async function runReliabilityTour(
  dependencies: ReliabilityTourDependencies,
  options: ReliabilityTourOptions = {},
): Promise<ReliabilityTourResult> {
  const pollIntervalMs = options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
  const presentationPauseMs =
    options.presentationPauseMs ?? DEFAULT_PRESENTATION_PAUSE_MS;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const selectedScenario = demoScenario(options.scenarioId ?? DEFAULT_DEMO_SCENARIO_ID);
  assertDuration("pollIntervalMs", pollIntervalMs, false);
  assertDuration("presentationPauseMs", presentationPauseMs, true);
  assertDuration("timeoutMs", timeoutMs, false);

  const runId = options.runId ?? dependencies.createRunId?.();
  if (!runId?.trim()) {
    throw new Error("A stable runId or createRunId dependency is required.");
  }

  const publishIdempotencyKey = `control-room-story-${runId}`;
  const replayIdempotencyKey = `control-room-replay-${runId}`;
  const deadline = dependencies.now() + timeoutMs;
  let phase: ReliabilityTourPhase = "idle";
  let eventId: string | null = options.eventId ?? null;
  let endpointId: string | null = null;
  let sourceDeliveryId: string | null = null;
  let replayDeliveryId: string | null = options.replayDeliveryId ?? null;
  let event: EventDetail | null = null;
  let currentMessage = "Ready to run the reliability tour.";
  // Snapshot editable input once so callers cannot change a running or resumable tour.
  const payload = options.payload ? { ...options.payload } : undefined;

  const progress = (): ReliabilityTourProgress => ({
    phase,
    runId,
    eventId,
    event,
    attemptCount: attemptsIn(event),
    scenarioId: selectedScenario.id,
    message: currentMessage,
  });

  const report = (
    nextPhase: ReliabilityTourPhase,
    message: string,
    nextEvent?: EventDetail,
  ): void => {
    phase = nextPhase;
    currentMessage = message;
    if (nextEvent) {
      event = nextEvent;
      eventId = nextEvent.id;
    }
    options.onProgress?.(progress());
  };

  const result = (): ReliabilityTourResult => ({
    ...progress(),
    publishIdempotencyKey,
    replayIdempotencyKey,
    endpointId,
    sourceDeliveryId,
    replayDeliveryId,
    payload,
  });

  const assertRunning = (): void => {
    if (options.signal?.aborted) throw new TourPause("The reliability tour was paused.");
    if (dependencies.now() >= deadline) {
      throw new TourPause("The reliability tour timed out while waiting for durable state.");
    }
  };

  const wait = async (durationMs: number): Promise<void> => {
    assertRunning();
    await dependencies.wait(durationMs, options.signal);
    assertRunning();
  };

  const readEventAfterPoll = async (): Promise<EventDetail> => {
    if (!eventId) throw new Error("Cannot poll before an event has been accepted.");
    await wait(pollIntervalMs);
    const fresh = await dependencies.getEvent(eventId);
    event = fresh;
    return fresh;
  };

  const pauseForPresentation = async (): Promise<void> => {
    if (presentationPauseMs > 0) await wait(presentationPauseMs);
  };

  const fail = (message: string, failedEvent = event): ReliabilityTourResult => {
    report("failed", message, failedEvent ?? undefined);
    return result();
  };

  const sourceDelivery = (detail: EventDetail) =>
    detail.deliveries.find((delivery) => delivery.replay_generation === 0);

  const replayDelivery = (detail: EventDetail, deliveryId: string) =>
    detail.deliveries.find((delivery) => delivery.id === deliveryId);

  report("idle", currentMessage);

  try {
    assertRunning();

    if (eventId) {
      event = await dependencies.getEvent(eventId);
      eventId = event.id;
      endpointId = sourceDelivery(event)?.endpoint_id ?? null;
    } else {
      report(
        "preparing",
        `Configuring the isolated ${selectedScenario.shortLabel.toLowerCase()} scenario and locating Receiver Lab.`,
      );

      // A new event is a new scenario. Re-applying the preset resets the
      // receiver's scenario-local counter while preserving its evidence log.
      const configured = await dependencies.setReceiverLabPreset(selectedScenario.preset, runId);
      if (configured.preset !== selectedScenario.preset) {
        throw new TourPause(`Receiver Lab did not enter the ${selectedScenario.preset} preset.`);
      }

      const endpoint = await dependencies.ensureReceiverLabEndpoint();
      endpointId = endpoint.id;
      const accepted = payload
        ? await dependencies.publishDemoEvent(
            endpoint.id,
            runId,
            publishIdempotencyKey,
            payload,
            selectedScenario.id,
          )
        : await dependencies.publishDemoEvent(
            endpoint.id,
            runId,
            publishIdempotencyKey,
            undefined,
            selectedScenario.id,
          );
      eventId = accepted.event_id;
      event = await dependencies.getEvent(accepted.event_id);
    }

    if (!event) throw new Error("The accepted event could not be loaded.");
    const persistedScenario = event.data.scenario;
    if (isDemoScenarioId(persistedScenario) && persistedScenario !== selectedScenario.id) {
      return fail(
        `This event belongs to the ${persistedScenario} scenario, not ${selectedScenario.id}.`,
        event,
      );
    }
    report("accepted", "The event and its original delivery are safely stored.", event);
    await pauseForPresentation();

    let source = sourceDelivery(event);
    if (!source) return fail("The accepted event is missing its original delivery.", event);
    sourceDeliveryId = source.id;
    endpointId ??= source.endpoint_id;

    const existingReplays = event.deliveries.filter(
      (delivery) => delivery.replay_generation > 0,
    );
    if (existingReplays.some((delivery) => delivery.replay_generation > 1)) {
      return fail("The event contains an unexpected extra recovery replay.", event);
    }

    if (selectedScenario.strategy !== "replay") {
      if (existingReplays.length > 0) {
        return fail("This automatic scenario unexpectedly contains a recovery replay.", event);
      }

      while (source.status !== "delivered" && source.status !== "dead_lettered") {
        report(
          "failing",
          selectedScenario.id === "rate_limit_recovery"
            ? "Receiver Lab is returning HTTP 429 while the worker follows Retry-After."
            : "Receiver Lab is returning a real failure while the worker records and classifies it.",
          event,
        );
        event = await readEventAfterPoll();
        source = sourceDelivery(event);
        if (!source) return fail("The original delivery disappeared from the event.", event);
        sourceDeliveryId = source.id;
      }

      if (selectedScenario.strategy === "automatic" && source.status === "delivered") {
        report(
          "verified",
          selectedScenario.id === "rate_limit_recovery"
            ? "The original delivery succeeded after two real 429 responses and receiver-directed waits."
            : "The original delivery recovered automatically after two real 503 responses.",
          event,
        );
        return result();
      }

      if (selectedScenario.strategy === "terminal" && source.status === "dead_lettered") {
        report(
          "verified",
          "HTTP 400 was classified as permanent, so EventHarbor stopped after one request instead of retrying bad data.",
          event,
        );
        return result();
      }

      return fail(
        selectedScenario.strategy === "terminal"
          ? "The permanently rejected event did not stop as expected."
          : "The receiver did not recover during the original delivery as expected.",
        event,
      );
    }

    if (existingReplays.length > 0 && !replayDeliveryId) {
      // Recover an ambiguously acknowledged replay with the same stable key. The
      // backend returns the original generation or rejects an external replay.
      const recovered = await dependencies.replayDelivery(source.id, replayIdempotencyKey);
      replayDeliveryId = recovered.delivery_id;
    }

    if (!replayDeliveryId) {
      while (source.status !== "dead_lettered") {
        if (source.status === "delivered") {
          return fail(
            "The original delivery succeeded unexpectedly; the controlled failure was interrupted.",
            event,
          );
        }
        if (event.deliveries.some((delivery) => delivery.replay_generation > 0)) {
          return fail("A recovery replay appeared before this tour authorized it.", event);
        }

        report(
          "failing",
          "Receiver Lab is rejecting signed requests while the worker records each attempt.",
          event,
        );
        event = await readEventAfterPoll();
        source = sourceDelivery(event);
        if (!source) return fail("The original delivery disappeared from the event.", event);
        sourceDeliveryId = source.id;
      }

      report(
        "contained",
        "The retry budget is exhausted. The stopped original delivery and every failed request remain preserved.",
        event,
      );
      await pauseForPresentation();

      report("repairing", "Checking Receiver Lab before applying the success preset.", event);
      const receiver = await dependencies.getReceiverLab(runId);
      if (receiver.preset !== "success") {
        const repaired = await dependencies.setReceiverLabPreset("success", runId);
        if (repaired.preset !== "success") {
          throw new TourPause("Receiver Lab did not enter the success preset.");
        }
      }
      await pauseForPresentation();

      report("replaying", "Creating a separate, traceable recovery replay that starts at request 1.", event);
      const replay = await dependencies.replayDelivery(source.id, replayIdempotencyKey);
      replayDeliveryId = replay.delivery_id;
      event = await dependencies.getEvent(eventId);
    } else {
      const knownReplay = replayDelivery(event, replayDeliveryId);
      if (!knownReplay) {
        event = await dependencies.getEvent(eventId);
      }
      const currentReplay = replayDelivery(event, replayDeliveryId);
      if (!currentReplay) {
        return fail("The accepted replay is missing from the event lineage.", event);
      }
      if (currentReplay.replayed_from_delivery_id !== source.id) {
        return fail("The recovery replay is not linked to the original delivery.", event);
      }
      if (currentReplay.status !== "delivered" && currentReplay.status !== "dead_lettered") {
        report("repairing", "Verifying Receiver Lab before the active replay continues.", event);
        const receiver = await dependencies.getReceiverLab(runId);
        if (receiver.preset !== "success") {
          const repaired = await dependencies.setReceiverLabPreset("success", runId);
          if (repaired.preset !== "success") {
            throw new TourPause("Receiver Lab did not enter the success preset.");
          }
        }
      }
    }

    if (!replayDeliveryId) throw new Error("Replay acceptance did not return a delivery ID.");

    while (true) {
      const replay = replayDelivery(event, replayDeliveryId);
      if (!replay) {
        const otherReplay = event.deliveries.find(
          (delivery) => delivery.replay_generation > 0,
        );
        if (otherReplay) {
          return fail("A different recovery replay superseded this tour's replay.", event);
        }
      } else if (replay.status === "delivered") {
        report(
          "verified",
          "The recovery replay succeeded while the stopped original delivery remains intact as failure evidence.",
          event,
        );
        return result();
      } else if (replay.status === "dead_lettered") {
        return fail("The recovery replay also reached the dead-letter boundary.", event);
      }

      report(
        "replaying",
        "The repaired receiver is handling the separate recovery replay.",
        event,
      );
      event = await readEventAfterPoll();
    }
  } catch (error) {
    const message = error instanceof TourPause ? error.message : messageFrom(error);
    report("paused", message, event ?? undefined);
    return result();
  }
}
