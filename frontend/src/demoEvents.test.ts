import { describe, expect, it } from "vitest";

import { isExpectedSchemaRejection } from "./demoEvents";
import type { Delivery, DeliveryAttempt } from "./types";

const now = "2026-08-25T12:00:00Z";

function stoppedDelivery(): Delivery {
  return {
    id: "delivery-1",
    endpoint_id: "endpoint-1",
    replay_generation: 0,
    replayed_from_delivery_id: null,
    status: "dead_lettered",
    attempt_count: 1,
    next_attempt_at: null,
    lease_owner: null,
    lease_expires_at: null,
    delivered_at: null,
    last_error: "HTTP 400",
    created_at: now,
    updated_at: now,
  };
}

function missingCustomerAttempt(attemptNumber = 1): DeliveryAttempt {
  return {
    id: "attempt-1",
    attempt_number: attemptNumber,
    status: "completed",
    disposition: "terminal_failure",
    http_status_code: 400,
    error_type: null,
    error_message: null,
    response_body_excerpt: '{"code":"missing_customer_id"}',
    duration_ms: 2,
    request_timestamp: 1_777_070_400,
    retry_scheduled_for: null,
    started_at: now,
    finished_at: now,
    resolved_at: now,
    created_at: now,
  };
}

describe("schema rejection evidence", () => {
  it("accepts only the exact first-attempt rejection for a known event type", () => {
    expect(isExpectedSchemaRejection(
      "order.paid",
      stoppedDelivery(),
      [missingCustomerAttempt()],
    )).toBe(true);
  });

  it("does not fall back to the order contract for an unknown event type", () => {
    expect(isExpectedSchemaRejection(
      "legacy.unknown",
      stoppedDelivery(),
      [missingCustomerAttempt()],
    )).toBe(false);
  });

  it("rejects evidence whose persisted attempt number is not one", () => {
    expect(isExpectedSchemaRejection(
      "order.paid",
      stoppedDelivery(),
      [missingCustomerAttempt(2)],
    )).toBe(false);
  });
});
