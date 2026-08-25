import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { demoScenario } from "../demoScenarios";
import type { Delivery, DeliveryAttempt, EventDetail, ReceiverLabRequest } from "../types";
import { EventJourney } from "./EventJourney";

const now = "2026-08-24T14:03:01.000Z";
const hash = "a".repeat(64);

function delivery(
  id: string,
  generation: 0 | 1,
  status: Delivery["status"],
  attemptCount: number,
): Delivery {
  return {
    id,
    endpoint_id: "endpoint-1",
    replay_generation: generation,
    replayed_from_delivery_id: generation ? "delivery-0" : null,
    status,
    attempt_count: attemptCount,
    next_attempt_at: status === "retry_wait" ? now : null,
    lease_owner: null,
    lease_expires_at: null,
    delivered_at: status === "delivered" ? now : null,
    last_error: status === "delivered" ? null : "HTTP response",
    created_at: now,
    updated_at: now,
  };
}

function attempt(
  id: string,
  number: number,
  status: number,
  disposition: DeliveryAttempt["disposition"] = status === 200 ? "succeeded" : "retry",
  responseBody: string | null = null,
): DeliveryAttempt {
  return {
    id,
    attempt_number: number,
    status: "completed",
    disposition,
    http_status_code: status,
    error_type: null,
    error_message: null,
    response_body_excerpt: responseBody,
    duration_ms: 7,
    request_timestamp: 1_777_070_400 + number,
    retry_scheduled_for: null,
    started_at: now,
    finished_at: now,
    resolved_at: now,
    created_at: now,
  };
}

function event(...deliveries: Delivery[]): EventDetail {
  return {
    id: "event-7f2a",
    source: "local-api",
    type: "order.paid",
    data: {
      run_id: "run-7f2a",
      scenario: "outage_replay",
      order_id: "ORDER-7F2A",
      customer_id: "CUS-7F2A",
      amount_cents: 12_900,
      currency: "USD",
    },
    payload_sha256: hash,
    request_fingerprint_sha256: "b".repeat(64),
    idempotency_key: "story-run-7f2a",
    created_at: now,
    deliveries,
  };
}

function observation(
  sequence: number,
  deliveryId: string,
  attemptNumber: number,
  status: number,
): ReceiverLabRequest {
  return {
    sequence,
    attempt: attemptNumber,
    event_id: "event-7f2a",
    delivery_id: deliveryId,
    event_type: "order.paid",
    delivery_attempt: attemptNumber,
    request_timestamp: 1_777_070_400 + attemptNumber,
    received_at: now,
    response_status_code: status,
    receiver_mode: status === 200 ? "success" : "fail_then_succeed",
    signature_present: true,
    body_preview: "{}",
    body_sha256: hash,
  };
}

function commonProps() {
  return {
    demoMode: "guided" as const,
    tourPhase: null,
    tourMessage: "Observed from durable state.",
    isStarting: false,
    runId: "run-7f2a",
    endpointName: "Orders receiver · Receiver Lab",
    endpointUrl: "http://receiver-lab:8100/webhooks/orders",
    receiverControlStatus: "ready" as const,
    maxAttempts: 4,
  };
}

describe("EventJourney", () => {
  it("shows one continuous delivered story and explains the separate recovery replay", () => {
    const original = delivery("delivery-0", 0, "dead_lettered", 4);
    const replay = delivery("delivery-1", 1, "delivered", 1);
    const originalAttempts = [1, 2, 3, 4].map((number) => attempt(`g0-${number}`, number, 503));

    render(
      <EventJourney
        {...commonProps()}
        scenario={demoScenario("outage_replay")}
        event={event(original, replay)}
        originalDelivery={original}
        replayDelivery={replay}
        originalAttempts={originalAttempts}
        replayAttempts={[attempt("g1-1", 1, 200)]}
        receiverObservations={[
          ...originalAttempts.map((item, index) => observation(index + 1, original.id, item.attempt_number, 503)),
          observation(5, replay.id, 1, 200),
        ]}
        repairOccurred
        replayOccurred
      />,
    );

    expect(screen.getByRole("heading", { name: /recovery replay reached receiver lab/i })).toBeVisible();
    expect(screen.getByText(/replay request 1 received HTTP 200/i)).toBeVisible();
    expect(screen.getByText(/original evidence remains unchanged/i)).toBeVisible();
    expect(screen.getByText(/dead-lettered.*does not mean deleted/i)).toBeVisible();
    const originalHistory = screen.getByLabelText("Original delivery actual request history");
    expect(within(originalHistory).getAllByText("HTTP 503")).toHaveLength(4);
    expect(screen.getByText(/HTTP INTAKE · POST \/api\/v1\/events/i)).toBeVisible();
    expect(screen.getByText("Docker service · postgres:5432")).toBeVisible();
    expect(screen.queryByText(/generation 0|generation 1|\bG0\b|\bG1\b/i)).not.toBeInTheDocument();
    expect(screen.getByText("✓ Exact byte match")).toBeVisible();
  });

  it("makes rate limiting the dominant live response without inventing a replay", () => {
    const original = delivery("delivery-0", 0, "retry_wait", 2);
    render(
      <EventJourney
        {...commonProps()}
        scenario={demoScenario("rate_limit_recovery")}
        event={event(original)}
        originalDelivery={original}
        replayDelivery={null}
        originalAttempts={[attempt("g0-1", 1, 429), attempt("g0-2", 2, 429)]}
        replayAttempts={[]}
        receiverObservations={[
          observation(1, original.id, 1, 429),
          observation(2, original.id, 2, 429),
        ]}
        repairOccurred={false}
        replayOccurred={false}
      />,
    );

    expect(screen.getByRole("heading", { name: /returned HTTP 429 rate limited/i })).toBeVisible();
    expect(screen.getByText(/follows Retry-After/i)).toBeVisible();
    expect(screen.getByText("LAST HTTP 429 · Rate Limited")).toBeVisible();
    expect(screen.queryByText(/G1 request 1 is a new delivery/i)).not.toBeInTheDocument();
  });

  it("explains why a permanent HTTP 400 stops after one request", () => {
    const original = delivery("delivery-0", 0, "dead_lettered", 1);
    render(
      <EventJourney
        {...commonProps()}
        scenario={demoScenario("permanent_rejection")}
        event={event(original)}
        originalDelivery={original}
        replayDelivery={null}
        originalAttempts={[attempt(
          "g0-1",
          1,
          400,
          "terminal_failure",
          '{"code":"missing_customer_id","detail":"data.customer_id is required."}',
        )]}
        replayAttempts={[]}
        receiverObservations={[observation(1, original.id, 1, 400)]}
        repairOccurred={false}
        replayOccurred={false}
      />,
    );

    expect(screen.getByRole("heading", { name: /HTTP 400: data.customer_id was missing/i })).toBeVisible();
    expect(screen.getByText(/contract requires data.customer_id/i)).toBeVisible();
    expect(screen.getAllByText("data.customer_id is required.")[0]).toBeVisible();
    expect(screen.getByText("Stopped · not retryable")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /correctly stopped. no useless retries/i })).toBeVisible();
    expect(screen.getByText(/unchanged replay is blocked/i)).toBeVisible();
    expect(screen.queryByText(/new replay/i)).not.toBeInTheDocument();
  });

  it("asks for evidence review instead of mislabeling an unrelated HTTP 400", () => {
    const original = delivery("delivery-0", 0, "dead_lettered", 1);
    render(
      <EventJourney
        {...commonProps()}
        scenario={demoScenario("permanent_rejection")}
        event={event(original)}
        originalDelivery={original}
        replayDelivery={null}
        originalAttempts={[attempt(
          "g0-1",
          1,
          400,
          "terminal_failure",
          '{"code":"invalid_signature","detail":"signature mismatch"}',
        )]}
        replayAttempts={[]}
        receiverObservations={[observation(1, original.id, 1, 400)]}
        repairOccurred={false}
        replayOccurred={false}
      />,
    );

    expect(screen.getByRole("heading", { name: /not with the expected schema proof/i })).toBeVisible();
    expect(screen.getByText(/Open every HTTP attempt/i)).toBeVisible();
    expect(screen.queryByText(/data.customer_id was missing/i)).not.toBeInTheDocument();
  });
});
