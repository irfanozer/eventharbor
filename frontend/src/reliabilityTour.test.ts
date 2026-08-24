import { describe, expect, it, vi } from "vitest";

import {
  runReliabilityTour,
  type ReliabilityTourDependencies,
  type ReliabilityTourProgress,
} from "./reliabilityTour";
import type {
  Delivery,
  DemoEventPayload,
  Endpoint,
  EventAccepted,
  EventDetail,
  ReceiverLabPreset,
  ReceiverLabState,
  ReplayAccepted,
} from "./types";

const now = "2026-08-24T12:00:00Z";

function delivery(
  id: string,
  generation: number,
  status: Delivery["status"],
  attemptCount: number,
): Delivery {
  return {
    id,
    endpoint_id: "endpoint-1",
    replay_generation: generation,
    replayed_from_delivery_id: generation === 0 ? null : "delivery-0",
    status,
    attempt_count: attemptCount,
    next_attempt_at: status === "retry_wait" ? now : null,
    lease_owner: null,
    lease_expires_at: null,
    delivered_at: status === "delivered" ? now : null,
    last_error: status === "dead_lettered" ? "HTTP 503" : null,
    created_at: now,
    updated_at: now,
  };
}

function event(...deliveries: Delivery[]): EventDetail {
  return {
    id: "event-1",
    source: "local-api",
    type: "demo.order.paid",
    data: { run_id: "run-1" },
    payload_sha256: "a".repeat(64),
    request_fingerprint_sha256: "b".repeat(64),
    idempotency_key: "control-room-story-run-1",
    created_at: now,
    deliveries,
  };
}

function receiver(preset: ReceiverLabPreset): ReceiverLabState {
  return {
    preset,
    configuration: {
      mode: preset === "success" ? "success" : "fail_then_succeed",
      failures_before_success: preset === "dead_letter" ? 20 : 0,
      delay_ms: 0,
    },
    attempts: 0,
    requests: [],
  };
}

function endpoint(): Endpoint {
  return {
    id: "endpoint-1",
    name: "Receiver Lab",
    url: "http://receiver-lab:8100/webhooks",
    enabled: true,
    secret_version: 1,
    created_at: now,
    updated_at: now,
  };
}

function accepted(): EventAccepted {
  return {
    event_id: "event-1",
    delivery_id: "delivery-0",
    endpoint_id: "endpoint-1",
    type: "demo.order.paid",
    status: "pending",
    created_at: now,
  };
}

function replayAccepted(): ReplayAccepted {
  return {
    source_delivery_id: "delivery-0",
    delivery_id: "delivery-1",
    event_id: "event-1",
    endpoint_id: "endpoint-1",
    replay_generation: 1,
    status: "pending",
    created_at: now,
  };
}

function dependencies(
  events: EventDetail[],
  receiverStates: ReceiverLabState[] = [receiver("dead_letter"), receiver("dead_letter")],
) {
  let clock = 0;
  const queue = [...events];
  const receiverQueue = [...receiverStates];
  const setReceiverLabPreset = vi.fn(async (preset: ReceiverLabPreset) => receiver(preset));
  const deps: ReliabilityTourDependencies = {
    getReceiverLab: vi.fn(async () => receiverQueue.shift() ?? receiver("success")),
    setReceiverLabPreset,
    ensureReceiverLabEndpoint: vi.fn(async () => endpoint()),
    publishDemoEvent: vi.fn(async () => accepted()),
    getEvent: vi.fn(async () => {
      const next = queue.shift();
      if (!next) throw new Error("test event queue exhausted");
      return next;
    }),
    replayDelivery: vi.fn(async () => replayAccepted()),
    wait: vi.fn(async (durationMs: number) => {
      clock += durationMs;
    }),
    now: () => clock,
  };
  return { deps, setReceiverLabPreset };
}

describe("runReliabilityTour", () => {
  it("runs one deterministic story with stable keys and server-driven phases", async () => {
    const acceptedEvent = event(delivery("delivery-0", 0, "pending", 0));
    const retryingEvent = event(delivery("delivery-0", 0, "retry_wait", 2));
    const containedEvent = event(delivery("delivery-0", 0, "dead_lettered", 4));
    const replayingEvent = event(
      delivery("delivery-0", 0, "dead_lettered", 4),
      delivery("delivery-1", 1, "pending", 0),
    );
    const verifiedEvent = event(
      delivery("delivery-0", 0, "dead_lettered", 4),
      delivery("delivery-1", 1, "delivered", 1),
    );
    const { deps, setReceiverLabPreset } = dependencies([
      acceptedEvent,
      retryingEvent,
      containedEvent,
      replayingEvent,
      verifiedEvent,
    ]);
    const updates: ReliabilityTourProgress[] = [];
    const payload: DemoEventPayload = {
      order_id: "ORDER-RECRUITER-7",
      amount_cents: 54_321,
      note: "Leave with the front desk",
    };

    const result = await runReliabilityTour(deps, {
      runId: "run-1",
      payload,
      pollIntervalMs: 10,
      presentationPauseMs: 5,
      timeoutMs: 1_000,
      onProgress: (update) => updates.push(update),
    });

    expect(result.phase).toBe("verified");
    expect(result.event).toEqual(verifiedEvent);
    expect(result.attemptCount).toBe(5);
    expect(result.sourceDeliveryId).toBe("delivery-0");
    expect(result.replayDeliveryId).toBe("delivery-1");
    expect(result.payload).toEqual(payload);
    expect(deps.publishDemoEvent).toHaveBeenCalledWith(
      "endpoint-1",
      "run-1",
      "control-room-story-run-1",
      payload,
      "outage_replay",
    );
    expect(deps.replayDelivery).toHaveBeenCalledWith(
      "delivery-0",
      "control-room-replay-run-1",
    );
    // A fresh run re-applies the outage preset to reset its scenario counter,
    // then the recovery step explicitly changes the receiver to success.
    expect(setReceiverLabPreset).toHaveBeenCalledTimes(2);
    expect(setReceiverLabPreset).toHaveBeenNthCalledWith(1, "dead_letter", "run-1");
    expect(setReceiverLabPreset).toHaveBeenCalledWith("success", "run-1");
    expect(updates.map((update) => update.phase)).toEqual(expect.arrayContaining([
      "idle",
      "preparing",
      "accepted",
      "failing",
      "contained",
      "repairing",
      "replaying",
      "verified",
    ]));
    expect(updates.some((update) => update.phase === "failing" && update.attemptCount === 2)).toBe(true);
  });

  it("resumes from durable evidence without republishing or changing its payload", async () => {
    const containedEvent = event(delivery("delivery-0", 0, "dead_lettered", 4));
    const replayingEvent = event(
      delivery("delivery-0", 0, "dead_lettered", 4),
      delivery("delivery-1", 1, "pending", 0),
    );
    const verifiedEvent = event(
      delivery("delivery-0", 0, "dead_lettered", 4),
      delivery("delivery-1", 1, "delivered", 1),
    );
    const { deps } = dependencies([containedEvent, replayingEvent, verifiedEvent]);
    const payload: DemoEventPayload = {
      order_id: "ORDER-RESUME-9",
      amount_cents: 8_765,
      note: "Keep this exact input",
    };

    const result = await runReliabilityTour(deps, {
      runId: "run-1",
      eventId: "event-1",
      payload,
      pollIntervalMs: 10,
      presentationPauseMs: 0,
      timeoutMs: 1_000,
    });

    expect(result.phase).toBe("verified");
    expect(result.payload).toEqual(payload);
    expect(deps.publishDemoEvent).not.toHaveBeenCalled();
    expect(deps.replayDelivery).toHaveBeenCalledWith(
      "delivery-0",
      "control-room-replay-run-1",
    );
  });

  it("fails if generation 0 succeeds instead of reaching containment", async () => {
    const { deps } = dependencies([
      event(delivery("delivery-0", 0, "delivered", 1)),
    ]);

    const result = await runReliabilityTour(deps, {
      runId: "run-1",
      pollIntervalMs: 10,
      presentationPauseMs: 0,
      timeoutMs: 100,
    });

    expect(result.phase).toBe("failed");
    expect(result.message).toContain("succeeded unexpectedly");
    expect(deps.replayDelivery).not.toHaveBeenCalled();
    expect(result.event?.deliveries[0]?.status).toBe("delivered");
  });

  it.each([
    ["transient_recovery", "retry_then_recover"],
    ["rate_limit_recovery", "rate_limit_then_recover"],
  ] as const)(
    "finishes the %s scenario inside generation 0 without creating a replay",
    async (scenarioId, expectedPreset) => {
      const acceptedEvent = event(delivery("delivery-0", 0, "pending", 0));
      const retryingEvent = event(delivery("delivery-0", 0, "retry_wait", 2));
      const deliveredEvent = event(delivery("delivery-0", 0, "delivered", 3));
      const { deps, setReceiverLabPreset } = dependencies([
        acceptedEvent,
        retryingEvent,
        deliveredEvent,
      ]);

      const result = await runReliabilityTour(deps, {
        runId: "run-1",
        scenarioId,
        pollIntervalMs: 10,
        presentationPauseMs: 0,
        timeoutMs: 1_000,
      });

      expect(result.phase).toBe("verified");
      expect(result.scenarioId).toBe(scenarioId);
      expect(result.event).toEqual(deliveredEvent);
      expect(setReceiverLabPreset).toHaveBeenCalledTimes(1);
      expect(setReceiverLabPreset).toHaveBeenCalledWith(expectedPreset, "run-1");
      expect(deps.replayDelivery).not.toHaveBeenCalled();
    },
  );

  it("classifies HTTP 400 as a verified terminal outcome without retry or replay", async () => {
    const acceptedEvent = event(delivery("delivery-0", 0, "pending", 0));
    const rejectedEvent = event(delivery("delivery-0", 0, "dead_lettered", 1));
    const { deps, setReceiverLabPreset } = dependencies([acceptedEvent, rejectedEvent]);

    const result = await runReliabilityTour(deps, {
      runId: "run-1",
      scenarioId: "permanent_rejection",
      pollIntervalMs: 10,
      presentationPauseMs: 0,
      timeoutMs: 1_000,
    });

    expect(result.phase).toBe("verified");
    expect(result.attemptCount).toBe(1);
    expect(result.message).toContain("stopped after one request");
    expect(setReceiverLabPreset).toHaveBeenCalledWith("permanent_failure", "run-1");
    expect(deps.replayDelivery).not.toHaveBeenCalled();
  });

  it("fails without erasing either generation when the replay dead-letters", async () => {
    const acceptedEvent = event(delivery("delivery-0", 0, "dead_lettered", 4));
    const replayDeadLettered = event(
      delivery("delivery-0", 0, "dead_lettered", 4),
      delivery("delivery-1", 1, "dead_lettered", 4),
    );
    const { deps } = dependencies([acceptedEvent, replayDeadLettered]);

    const result = await runReliabilityTour(deps, {
      runId: "run-1",
      pollIntervalMs: 10,
      presentationPauseMs: 0,
      timeoutMs: 100,
    });

    expect(result.phase).toBe("failed");
    expect(result.message).toContain("recovery replay also reached");
    expect(result.event?.deliveries).toHaveLength(2);
    expect(result.attemptCount).toBe(8);
  });

  it("pauses on timeout and returns the latest durable evidence", async () => {
    const pending = event(delivery("delivery-0", 0, "retry_wait", 2));
    const { deps } = dependencies([pending, pending, pending]);

    const result = await runReliabilityTour(deps, {
      runId: "run-1",
      pollIntervalMs: 10,
      presentationPauseMs: 0,
      timeoutMs: 15,
    });

    expect(result.phase).toBe("paused");
    expect(result.message).toContain("timed out");
    expect(result.event).toEqual(pending);
    expect(result.attemptCount).toBe(2);
  });
});
