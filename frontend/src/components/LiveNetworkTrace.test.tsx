import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Delivery, DeliveryAttempt } from "../types";
import { LiveNetworkTrace, type LiveTraceReceiverObservation } from "./LiveNetworkTrace";

const originalDelivery: Delivery = {
  id: "delivery-original",
  endpoint_id: "endpoint-1",
  replay_generation: 0,
  replayed_from_delivery_id: null,
  status: "dead_lettered",
  attempt_count: 4,
  next_attempt_at: null,
  lease_owner: null,
  lease_expires_at: null,
  delivered_at: null,
  last_error: "HTTP 503",
  created_at: "2026-08-24T14:03:01.000Z",
  updated_at: "2026-08-24T14:03:11.000Z",
};

const replayDelivery: Delivery = {
  ...originalDelivery,
  id: "delivery-replay",
  replay_generation: 1,
  replayed_from_delivery_id: originalDelivery.id,
  status: "delivered",
  attempt_count: 1,
  delivered_at: "2026-08-24T14:03:14.000Z",
  last_error: null,
  created_at: "2026-08-24T14:03:13.000Z",
  updated_at: "2026-08-24T14:03:14.000Z",
};

function attempt(number: number, status: number, duration: number, generation: 0 | 1): DeliveryAttempt {
  const second = generation === 0 ? number + 1 : 14;
  return {
    id: `attempt-${generation}-${number}`,
    attempt_number: number,
    status: "completed",
    disposition: status === 200 ? "succeeded" : "retry",
    http_status_code: status,
    error_type: null,
    error_message: null,
    response_body_excerpt: status === 200 ? "accepted" : "temporarily unavailable",
    duration_ms: duration,
    request_timestamp: Date.parse(`2026-08-24T14:03:${String(second).padStart(2, "0")}.000Z`) / 1_000,
    retry_scheduled_for: null,
    started_at: null,
    finished_at: null,
    resolved_at: null,
    created_at: `2026-08-24T14:03:${String(second).padStart(2, "0")}.000Z`,
  };
}

const payload = {
  id: "event-7f2a",
  type: "order.paid",
  source: "portfolio-demo",
  data: { order_id: "ORD-4821", amount: 129 },
};

const payloadHash = "9fd29a1c";

const receiverObservation: LiveTraceReceiverObservation = {
  sequence: 5,
  event_id: "event-7f2a",
  delivery_id: replayDelivery.id,
  event_type: "order.paid",
  delivery_attempt: 1,
  request_timestamp: Date.parse("2026-08-24T14:03:14.000Z") / 1_000,
  received_at: "2026-08-24T14:03:14.020Z",
  response_status_code: 200,
  receiver_mode: "success",
  signature_present: true,
  body_sha256: payloadHash,
  body_preview: JSON.stringify(payload),
};

describe("LiveNetworkTrace", () => {
  it("shows the real route, separates replay from automatic retries, and compares both receipts", () => {
    render(
      <LiveNetworkTrace
        demoMode="operator"
        runId="run-7f2a"
        eventId="event-7f2a"
        eventType="order.paid"
        eventSource="portfolio-demo"
        eventCreatedAt="2026-08-24T14:03:01.000Z"
        payloadHash={payloadHash}
        payload={payload}
        endpointName="Receiver Lab"
        endpointUrl="http://receiver-lab:8100/webhooks"
        originalDelivery={originalDelivery}
        replayDelivery={replayDelivery}
        originalAttempts={[
          attempt(1, 503, 8, 0),
          attempt(2, 503, 6, 0),
          attempt(3, 503, 5, 0),
          attempt(4, 503, 7, 0),
        ]}
        replayAttempts={[attempt(1, 200, 12, 1)]}
        receiverObservations={[receiverObservation]}
        receiverStatus="online"
        repairOccurred
        replayOccurred
      />,
    );

    const route = screen.getByRole("list", { name: /actual event route/i });
    expect(within(route).getByRole("heading", { name: /browser demo client/i })).toBeVisible();
    expect(within(route).getByRole("heading", { name: /eventharbor/i })).toBeVisible();
    expect(within(route).getByRole("heading", { name: /receiver lab/i })).toBeVisible();
    expect(within(route).getByText("http://receiver-lab:8100/webhooks")).toBeVisible();

    const generationZero = screen.getByRole("article", { name: /generation 0 automatic delivery/i });
    expect(within(generationZero).getAllByText(/automatic attempt/i)).toHaveLength(4);
    // Each outcome appears in the desktop facts and the mobile compact summary.
    expect(within(generationZero).getAllByText("HTTP 503")).toHaveLength(8);
    expect(within(generationZero).getAllByText("8 ms")).toHaveLength(2);
    expect(within(generationZero).getAllByText("2026-08-24 14:03:02.000 UTC")).toHaveLength(2);

    const boundary = screen.getByRole("status", { name: /operator recovery boundary/i });
    expect(within(boundary).getByRole("heading", { name: /no fifth automatic retry/i })).toBeVisible();
    expect(within(boundary).getByText("PUT /api/v1/demo/receiver-lab?run_id=run-7f2a")).toBeVisible();
    expect(boundary).toHaveTextContent(/changed this run from HTTP 503 to HTTP 200/i);
    expect(within(boundary).getByText(/replay POST accepted/i)).toBeVisible();

    const generationOne = screen.getByRole("article", { name: /generation 1 operator-approved replay/i });
    expect(within(generationOne).getByText("Replay request")).toBeVisible();
    expect(within(generationOne).getAllByText("HTTP 200")).toHaveLength(2);
    expect(within(generationOne).queryByText(/automatic attempt/i)).not.toBeInTheDocument();

    const receipt = screen.getByRole("heading", { name: /successful replay delivered the exact stored payload/i });
    const receiptSection = receipt.closest("section");
    expect(receiptSection).not.toBeNull();
    expect(within(receiptSection!).getAllByText("Exact match")).toHaveLength(3);
    expect(within(receiptSection!).getByText("Signature header observed")).toBeVisible();
    expect(within(receiptSection!).getByText("HTTP 200 verified")).toBeVisible();
    expect(within(receiptSection!).queryByText(/signature.*verified/i)).not.toBeInTheDocument();
  });

  it("treats a matching rejected request as network evidence, not successful delivery", () => {
    const rejectedObservation: LiveTraceReceiverObservation = {
      ...receiverObservation,
      sequence: 1,
      delivery_id: originalDelivery.id,
      response_status_code: 503,
      receiver_mode: "fail_then_succeed",
    };

    render(
      <LiveNetworkTrace
        demoMode="guided"
        eventId="event-7f2a"
        eventType="order.paid"
        eventSource="portfolio-demo"
        eventCreatedAt="2026-08-24T14:03:01.000Z"
        payloadHash={payloadHash}
        payload={payload}
        endpointName="Receiver Lab"
        endpointUrl="http://receiver-lab:8100/webhooks"
        originalDelivery={{ ...originalDelivery, status: "retry_wait", attempt_count: 1 }}
        replayDelivery={null}
        originalAttempts={[attempt(1, 503, 8, 0)]}
        replayAttempts={[]}
        receiverObservations={[rejectedObservation]}
        receiverStatus="offline"
        repairOccurred={false}
        replayOccurred={false}
      />,
    );

    expect(screen.getByRole("heading", { name: /captured this request and returned HTTP 503/i })).toBeVisible();
    expect(screen.queryByText(/successful replay delivered/i)).not.toBeInTheDocument();
    expect(screen.getByText("HTTP 503 observed")).toBeVisible();
  });
});
