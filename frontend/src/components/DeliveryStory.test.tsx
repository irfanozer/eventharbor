import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { demoScenario } from "../demoScenarios";
import type { Delivery, DeliveryAttempt, EventDetail } from "../types";
import { DeliveryStory, responseLabel, type DeliveryStoryProps } from "./DeliveryStory";

const attempt: DeliveryAttempt = {
  id: "attempt-1", attempt_number: 1, status: "in_progress", disposition: null,
  http_status_code: null, error_type: null, error_message: null, response_body_excerpt: null,
  duration_ms: null, request_timestamp: null, retry_scheduled_for: null,
  started_at: null, finished_at: null, resolved_at: null, created_at: "2026-09-10T12:00:00Z",
};
const delivery: Delivery = {
  id: "delivery-1", endpoint_id: "endpoint-1", replay_generation: 0, replayed_from_delivery_id: null,
  status: "retry_wait", attempt_count: 1, next_attempt_at: null, lease_owner: null, lease_expires_at: null,
  delivered_at: null, last_error: "HTTP 503", created_at: attempt.created_at, updated_at: attempt.created_at,
};
const event: EventDetail = {
  id: "event-1", source: "test", type: "order.paid", data: { order_id: "ORDER-1" },
  payload_sha256: "a".repeat(64), request_fingerprint_sha256: "b".repeat(64),
  idempotency_key: "key-1", created_at: attempt.created_at, deliveries: [delivery],
};
const failure: DeliveryAttempt = {
  ...attempt, status: "completed", http_status_code: 503, disposition: "retry",
  retry_scheduled_for: "2026-09-10T12:00:02Z",
};
const accepted: DeliveryAttempt = {
  ...attempt, id: "attempt-2", attempt_number: 2, status: "completed", http_status_code: 200, disposition: "succeeded",
};
function props(overrides: Partial<DeliveryStoryProps> = {}): DeliveryStoryProps {
  return {
    scenario: demoScenario("transient_recovery"), event, original: delivery, replay: undefined,
    originalAttempts: [], replayAttempts: [], receipts: [], receiverReady: false,
    starting: false, reading: false, evidenceUnavailable: false, ...overrides,
  };
}
function history() { return screen.getByRole("list", { name: "Recorded delivery attempts" }); }

describe("visible delivery history", () => {
  it("does not present reserved or indeterminate attempts as successful responses", () => {
    expect(responseLabel(attempt)).toBe("Waiting for a response");
    expect(responseLabel({ ...attempt, status: "indeterminate", http_status_code: 200 })).toBe("Outcome unknown");
    expect(responseLabel({ ...attempt, status: "completed", error_type: "connection_error" })).toBe("connection error");
    expect(responseLabel({ ...attempt, status: "completed", http_status_code: 400 })).toBe("400 · Request rejected");
  });

  it("does not pre-fill attempts before an event is sent", () => {
    render(<DeliveryStory {...props({ event: null, original: undefined })} />);
    expect(screen.getByRole("heading", { name: "Ready when you are." })).toBeVisible();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
    expect(screen.queryByText(/503|200|saved by eventharbor/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /next|back/i })).not.toBeInTheDocument();
  });

  it("shows the outcome and every finished request without expanding or advancing anything", () => {
    render(<DeliveryStory {...props({ original: { ...delivery, status: "delivered", attempt_count: 2 }, originalAttempts: [failure, accepted] })} />);
    const result = screen.getByRole("heading", { name: "The event was delivered." });
    expect(result).toBeVisible();
    expect(screen.getByRole("heading", { name: "What happened to this event" })).toBeVisible();
    expect(screen.getByText("Saved by EventHarbor")).toBeVisible();
    expect(result.compareDocumentPosition(history()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const rows = within(history()).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText("503 · Service unavailable")).toBeVisible();
    expect(within(rows[0]!).getByText(/a retry was scheduled/i)).toBeVisible();
    expect(within(rows[1]!).getByText("200 · Accepted")).toBeVisible();
    expect(within(rows[1]!).getByText(/this delivery is complete/i)).toBeVisible();
    expect(rows.every((row) => row.querySelector("time")?.hasAttribute("datetime"))).toBe(true);
    expect(history().closest("details")).toBeNull();
    expect(within(history()).queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /next|back/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/jump to a request|read at your pace|step 1 of/i)).not.toBeInTheDocument();
  });

  it("adds incoming results without hiding the earlier failures", () => {
    const { rerender } = render(<DeliveryStory {...props({ originalAttempts: [failure] })} />);
    const firstRow = within(history()).getAllByRole("listitem")[0];
    rerender(<DeliveryStory {...props({ original: { ...delivery, status: "delivered", attempt_count: 2 }, originalAttempts: [failure, accepted] })} />);
    const rows = within(history()).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toBe(firstRow);
    expect(screen.getByText("503 · Service unavailable")).toBeVisible();
    expect(screen.getByText("200 · Accepted")).toBeVisible();
    expect(screen.getByRole("heading", { name: "The event was delivered." })).toBeVisible();
  });

  it("keeps original failures and distinguishes a successful resend of the same event", () => {
    const replay = { ...delivery, id: "delivery-2", replay_generation: 1, status: "delivered" as const };
    render(<DeliveryStory {...props({
      original: { ...delivery, status: "dead_lettered" }, originalAttempts: [failure],
      replay, replayAttempts: [{ ...accepted, id: "resend-1", attempt_number: 1 }],
    })} />);
    const rows = within(history()).getAllByRole("listitem");
    expect(within(rows[0]!).getByText("Try 1")).toBeVisible();
    expect(within(rows[0]!).getByText(/automatic retries ended here/i)).toBeVisible();
    expect(within(rows[1]!).getByText("Resend 1")).toBeVisible();
    expect(within(rows[1]!).getByText("A new delivery of the same saved event")).toBeVisible();
    expect(rows[0]).toHaveAttribute("data-tone", "attention");
    expect(rows[1]).toHaveAttribute("data-tone", "success");
    expect(screen.getByRole("heading", { name: "The saved event was delivered." })).toBeVisible();
  });

  it("shows in-progress and unknown outcomes without inventing an accepted response", () => {
    const { rerender } = render(<DeliveryStory {...props({ originalAttempts: [{ ...attempt, http_status_code: 200 }] })} />);
    expect(screen.getByText("Waiting for a response")).toBeVisible();
    expect(screen.getByText(/no final response has been recorded yet/i)).toBeVisible();
    expect(screen.queryByText("200 · Accepted")).not.toBeInTheDocument();
    rerender(<DeliveryStory {...props({ originalAttempts: [{ ...attempt, status: "indeterminate", http_status_code: 200 }] })} />);
    expect(screen.getByText("Outcome unknown")).toBeVisible();
    expect(screen.getByText(/the receiver may have received it/i)).toBeVisible();
    expect(within(history()).getByRole("listitem")).toHaveAttribute("data-tone", "attention");
  });

  it("keeps loaded rows visible during evidence errors", () => {
    render(<DeliveryStory {...props({ originalAttempts: [failure], evidenceUnavailable: true })} />);
    expect(screen.getByText("503 · Service unavailable")).toBeVisible();
    expect(screen.getByText(/previously loaded records may be out of date/i)).toBeVisible();
    expect(screen.queryByText(/waiting for the first/i)).not.toBeInTheDocument();
  });

  it("distinguishes loading terminal records from waiting for another delivery attempt", () => {
    const stopped = { ...delivery, status: "dead_lettered" as const, attempt_count: 2 };
    const { rerender } = render(<DeliveryStory {...props({ original: stopped, originalAttempts: [failure] })} />);
    expect(screen.getByRole("heading", { name: "Delivery stopped. Loading the recorded response." })).toBeVisible();
    expect(screen.getByText("Loading the remaining attempt records…")).toBeVisible();
    expect(screen.queryByText(/did not complete this case as expected/i)).not.toBeInTheDocument();
    rerender(<DeliveryStory {...props({ original: stopped, originalAttempts: [failure, { ...failure, id: "attempt-2", attempt_number: 2 }] })} />);
    expect(screen.queryByText("Loading the remaining attempt records…")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Delivery stopped. Review the response." })).toBeVisible();
  });

  it("waits for terminal rows from both original and resend deliveries", () => {
    render(<DeliveryStory {...props({
      original: { ...delivery, status: "dead_lettered", attempt_count: 2 }, originalAttempts: [failure],
      replay: { ...delivery, id: "delivery-2", replay_generation: 1, status: "delivered" },
      replayAttempts: [{ ...accepted, id: "resend-1", attempt_number: 1 }],
    })} />);
    expect(screen.getByRole("heading", { name: "The saved event was delivered." })).toBeVisible();
    expect(screen.getByText("Loading the remaining attempt records…")).toBeVisible();
    expect(screen.getByText("200 · Accepted")).toBeVisible();
  });

  it("removes the previous event's history when the new event replaces it", () => {
    const { rerender } = render(<DeliveryStory {...props({ originalAttempts: [failure] })} />);
    rerender(<DeliveryStory {...props({ event: { ...event, id: "new-event" }, original: undefined })} />);
    expect(screen.queryByText("503 · Service unavailable")).not.toBeInTheDocument();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
    expect(screen.getByText("Waiting for the first recorded attempt…")).toBeVisible();
  });
  it.each(["dead_lettered", "delivered"] as const)("does not wait for a request after a %s delivery with no recorded attempts", (status) => {
    render(<DeliveryStory {...props({ original: { ...delivery, status, attempt_count: 0 } })} />);
    expect(screen.getByText("No HTTP attempts were recorded for this delivery.")).toBeVisible();
    expect(screen.queryByText("Waiting for the first recorded attempt…")).not.toBeInTheDocument();
  });
  it("keeps an earlier unknown request distinct from a later successful delivery", () => {
    render(<DeliveryStory {...props({
      original: { ...delivery, status: "delivered", attempt_count: 2 },
      originalAttempts: [{ ...attempt, status: "indeterminate" }, accepted],
    })} />);
    expect(screen.getByText("Outcome unknown")).toBeVisible();
    expect(screen.getByText(/this attempt's outcome was not confirmed/i)).toBeVisible();
    expect(screen.getByText("200 · Accepted")).toBeVisible();
    expect(screen.getByRole("heading", { name: "The event was delivered." })).toBeVisible();
  });
});
