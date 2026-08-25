import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import type { Delivery, DeliveryAttempt, EventDetail, ReceiverLabRequest } from "../types";
import { ControlRoomExperience } from "./ControlRoomExperience";

vi.mock("../api", () => ({
  ensureReceiverLabEndpoint: vi.fn(),
  getDeliveryAttempts: vi.fn(),
  getEndpoint: vi.fn(),
  getEvent: vi.fn(),
  getReceiverLab: vi.fn(),
  publishDemoEvent: vi.fn(),
  replayDelivery: vi.fn(),
  setReceiverLabPreset: vi.fn(),
}));

const now = "2026-08-24T14:03:01.000Z";
const eventId = "event-7f2a";
const runId = "run-7f2a";
const hash = "a".repeat(64);

function delivery(id: string, generation: 0 | 1, status: Delivery["status"], attemptCount: number): Delivery {
  return {
    id,
    endpoint_id: "endpoint-1",
    replay_generation: generation,
    replayed_from_delivery_id: generation === 0 ? null : "delivery-0",
    status,
    attempt_count: attemptCount,
    next_attempt_at: null,
    lease_owner: null,
    lease_expires_at: null,
    delivered_at: status === "delivered" ? now : null,
    last_error: status === "delivered" ? null : "HTTP 503",
    created_at: now,
    updated_at: now,
  };
}

function attempt(id: string, number: number, status: number): DeliveryAttempt {
  return {
    id,
    attempt_number: number,
    status: "completed",
    disposition: status === 200 ? "succeeded" : "retry",
    http_status_code: status,
    error_type: null,
    error_message: null,
    response_body_excerpt: null,
    duration_ms: 6,
    request_timestamp: 1_777_070_400 + number,
    retry_scheduled_for: null,
    started_at: now,
    finished_at: now,
    resolved_at: now,
    created_at: now,
  };
}

function observation(sequence: number, deliveryId: string, request: number, status: number): ReceiverLabRequest {
  return {
    sequence,
    attempt: request,
    event_id: eventId,
    delivery_id: deliveryId,
    event_type: "demo.order.paid",
    delivery_attempt: request,
    request_timestamp: 1_777_070_400 + request,
    received_at: now,
    response_status_code: status,
    receiver_mode: status === 200 ? "success" : "fail",
    signature_present: true,
    body_preview: "{}",
    body_sha256: hash,
  };
}

describe("ControlRoomExperience", () => {
  const original = delivery("delivery-0", 0, "dead_lettered", 4);
  const replay = delivery("delivery-1", 1, "delivered", 1);
  const originalAttempts = [1, 2, 3, 4].map((number) => attempt(`original-${number}`, number, 503));
  const replayAttempts = [attempt("replay-1", 1, 200)];
  const completedEvent: EventDetail = {
    id: eventId,
    source: "local-api",
    type: "demo.order.paid",
    data: {
      run_id: runId,
      scenario: "outage_replay",
      order_id: "ORDER-PRESERVED",
      amount_cents: 12_900,
      note: "Ready",
    },
    payload_sha256: hash,
    request_fingerprint_sha256: "b".repeat(64),
    idempotency_key: "story-run-7f2a",
    created_at: now,
    deliveries: [original, replay],
  };

  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    sessionStorage.clear();
    sessionStorage.setItem("eventharbor.control-room.event-id", eventId);
    sessionStorage.setItem("eventharbor.control-room.mode", "guided");
    sessionStorage.setItem("eventharbor.control-room.tour", JSON.stringify({
      runId,
      eventId,
      replayDeliveryId: replay.id,
      payload: { order_id: "ORDER-PRESERVED", amount_cents: 12_900, note: "Ready" },
      scenarioId: "outage_replay",
    }));

    vi.mocked(api.getEvent).mockResolvedValue(completedEvent);
    vi.mocked(api.getEndpoint).mockResolvedValue({
      id: "endpoint-1",
      name: "Receiver Lab",
      url: "http://receiver-lab:8100/webhooks",
      enabled: true,
      secret_version: 1,
      created_at: now,
      updated_at: now,
    });
    vi.mocked(api.getDeliveryAttempts).mockImplementation(async (deliveryId) => ({
      delivery: deliveryId === replay.id ? replay : original,
      attempts: deliveryId === replay.id ? replayAttempts : originalAttempts,
    }));
    vi.mocked(api.getReceiverLab).mockResolvedValue({
      preset: "success",
      configuration: { mode: "success", failures_before_success: 0, delay_ms: 0 },
      attempts: 5,
      requests: [
        ...originalAttempts.map((item, index) => observation(index + 1, original.id, item.attempt_number, 503)),
        observation(5, replay.id, 1, 200),
      ],
    });
  });

  it("keeps the completed event visible while preparing a different next scenario", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <ControlRoomExperience />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.getByRole("heading", { name: /choose the failure. then send the order/i })).toBeVisible();
    expect(await screen.findByRole("heading", { name: /recovery replay reached receiver lab/i })).toBeVisible();
    expect(screen.getByRole("heading", { name: "ORDER-PRESERVED" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /set up another failure scenario/i }));

    expect(screen.getByRole("heading", { name: "ORDER-PRESERVED" })).toBeVisible();
    expect(screen.getByRole("heading", { name: /recovery replay reached receiver lab/i })).toBeVisible();
    expect(screen.getByText(/completed story preserved below/i)).toBeVisible();
    expect(screen.getByRole("button", { name: /receiver recovers briefly/i })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Order ID")).not.toHaveValue("ORDER-PRESERVED");
    expect(sessionStorage.getItem("eventharbor.control-room.event-id")).toBe(eventId);
    expect(sessionStorage.getItem("eventharbor.control-room.tour")).toContain(eventId);

    fireEvent.click(screen.getByRole("button", { name: /receiver rejects invalid data/i }));
    expect(screen.getByText(/data.customer_id is missing/i)).toBeVisible();
    expect(screen.getByText(/intentionally has no data.customer_id/i)).toBeVisible();
    expect(screen.getByText(/original delivery stays stopped and preserved/i)).toBeVisible();
    await waitFor(() => expect(api.getEvent).toHaveBeenCalledWith(eventId));
  });
});
