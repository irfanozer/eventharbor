import type {
  Endpoint,
  EventAccepted,
  EventDetail,
  ReceiverLabPreset,
  ReceiverLabState,
  ReplayAccepted,
} from "./types";

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
  message: string;
}

export interface ReliabilityTourResult extends ReliabilityTourProgress {
  publishIdempotencyKey: string;
  replayIdempotencyKey: string;
  endpointId: string | null;
  sourceDeliveryId: string | null;
  replayDeliveryId: string | null;
}

export interface ReliabilityTourDependencies {
  getReceiverLab: () => Promise<ReceiverLabState>;
  setReceiverLabPreset: (preset: ReceiverLabPreset) => Promise<ReceiverLabState>;
  ensureReceiverLabEndpoint: () => Promise<Endpoint>;
  publishDemoEvent: (
    endpointId: string,
    runId: string,
    idempotencyKey: string,
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
 * Run the complete failure -> containment -> repair -> replay story.
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

  const progress = (): ReliabilityTourProgress => ({
    phase,
    runId,
    eventId,
    event,
    attemptCount: attemptsIn(event),
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
        "Configuring a deterministic failure and locating the Receiver Lab endpoint.",
      );

      const receiver = await dependencies.getReceiverLab();
      if (receiver.preset !== "dead_letter") {
        const configured = await dependencies.setReceiverLabPreset("dead_letter");
        if (configured.preset !== "dead_letter") {
          throw new TourPause("Receiver Lab did not enter the dead-letter preset.");
        }
      }

      const endpoint = await dependencies.ensureReceiverLabEndpoint();
      endpointId = endpoint.id;
      const accepted = await dependencies.publishDemoEvent(
        endpoint.id,
        runId,
        publishIdempotencyKey,
      );
      eventId = accepted.event_id;
      event = await dependencies.getEvent(accepted.event_id);
    }

    if (!event) throw new Error("The accepted event could not be loaded.");
    report("accepted", "The event and generation 0 are durably accepted.", event);
    await pauseForPresentation();

    let source = sourceDelivery(event);
    if (!source) return fail("The accepted event is missing delivery generation 0.", event);
    sourceDeliveryId = source.id;
    endpointId ??= source.endpoint_id;

    const existingReplays = event.deliveries.filter(
      (delivery) => delivery.replay_generation > 0,
    );
    if (existingReplays.some((delivery) => delivery.replay_generation > 1)) {
      return fail("The event contains an unexpected replay generation.", event);
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
            "Generation 0 delivered unexpectedly; the controlled failure was interrupted.",
            event,
          );
        }
        if (event.deliveries.some((delivery) => delivery.replay_generation > 0)) {
          return fail("A replay generation appeared before this tour authorized it.", event);
        }

        report(
          "failing",
          "Receiver Lab is rejecting signed requests while the worker records each attempt.",
          event,
        );
        event = await readEventAfterPoll();
        source = sourceDelivery(event);
        if (!source) return fail("Delivery generation 0 disappeared from the event.", event);
        sourceDeliveryId = source.id;
      }

      report(
        "contained",
        "The retry budget is exhausted and generation 0 is preserved as a dead letter.",
        event,
      );
      await pauseForPresentation();

      report("repairing", "Checking Receiver Lab before applying the success preset.", event);
      const receiver = await dependencies.getReceiverLab();
      if (receiver.preset !== "success") {
        const repaired = await dependencies.setReceiverLabPreset("success");
        if (repaired.preset !== "success") {
          throw new TourPause("Receiver Lab did not enter the success preset.");
        }
      }
      await pauseForPresentation();

      report("replaying", "Creating a traceable replay generation with a stable key.", event);
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
        return fail("The replay does not descend from generation 0.", event);
      }
      if (currentReplay.status !== "delivered" && currentReplay.status !== "dead_lettered") {
        report("repairing", "Verifying Receiver Lab before the active replay continues.", event);
        const receiver = await dependencies.getReceiverLab();
        if (receiver.preset !== "success") {
          const repaired = await dependencies.setReceiverLabPreset("success");
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
          return fail("A different replay generation superseded this tour's replay.", event);
        }
      } else if (replay.status === "delivered") {
        report(
          "verified",
          "Generation 1 delivered while generation 0 remains intact as failure evidence.",
          event,
        );
        return result();
      } else if (replay.status === "dead_lettered") {
        return fail("The replay generation also reached the dead-letter boundary.", event);
      }

      report(
        "replaying",
        "The repaired receiver is handling the new delivery generation.",
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
