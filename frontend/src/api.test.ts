import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ensureReceiverLabEndpoint,
  getReceiverLab,
  publishDemoEvent,
  replayDelivery,
  setReceiverLabPreset,
} from "./api";
import { demoEventDefinition } from "./demoEvents";

const now = "2026-08-24T12:00:00Z";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Receiver Lab endpoint setup", () => {
  it("reuses an enabled Receiver Lab endpoint", async () => {
    const definition = demoEventDefinition("order.paid");
    const endpoint = {
      id: "endpoint-1",
      name: "Earlier label for the same orders route",
      url: definition.destinationUrl,
      enabled: true,
      secret_version: 1,
      created_at: now,
      updated_at: now,
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [endpoint], next_cursor: null }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(ensureReceiverLabEndpoint()).resolves.toEqual(endpoint);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("registers the lab but never returns its one-time signing secret", async () => {
    const definition = demoEventDefinition("order.paid");
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ items: [], next_cursor: null }))
      .mockResolvedValueOnce(jsonResponse({
        id: "endpoint-2",
        name: definition.destinationName,
        url: definition.destinationUrl,
        enabled: true,
        signing_secret: "one-time-sensitive-value",
        secret_version: 1,
        created_at: now,
      }, 201));
    vi.stubGlobal("fetch", fetchMock);

    const endpoint = await ensureReceiverLabEndpoint();

    expect(endpoint).not.toHaveProperty("signing_secret");
    expect(endpoint.updated_at).toBe(now);
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/endpoints",
      expect.objectContaining({ method: "POST" }),
    );
    const request = fetchMock.mock.calls[1]?.[1] as RequestInit;
    expect(JSON.parse(String(request.body))).toEqual({
      name: definition.destinationName,
      url: definition.destinationUrl,
    });
  });
});

describe("Reliability story idempotency", () => {
  it("publishes a deterministic run with the caller's stable key", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      event_id: "event-1",
      delivery_id: "delivery-0",
      endpoint_id: "endpoint-1",
      type: "order.paid",
      status: "pending",
      created_at: now,
    }, 202));
    vi.stubGlobal("fetch", fetchMock);

    await publishDemoEvent("endpoint-1", "stable-run", "stable-publish-key");

    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(request.headers).toEqual(expect.objectContaining({
      "Idempotency-Key": "stable-publish-key",
    }));
    expect(JSON.parse(String(request.body))).toEqual({
      endpoint_id: "endpoint-1",
      type: "order.paid",
      data: {
        run_id: "stable-run",
        scenario: "outage_replay",
        purpose: "reliability-story",
        order_id: "ORDER-STABLE-R",
        customer_id: "CUS-STABLE",
        amount_cents: 12_900,
        currency: "USD",
      },
    });
  });

  it("publishes the caller's exact editable order payload", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      event_id: "event-1",
      delivery_id: "delivery-0",
      endpoint_id: "endpoint-1",
      type: "order.paid",
      status: "pending",
      created_at: now,
    }, 202));
    vi.stubGlobal("fetch", fetchMock);

    await publishDemoEvent("endpoint-1", "run-custom", "stable-publish-key", {
      type: "shipment.dispatched",
      data: {
        shipment_id: "SHIP-RECRUITER-7",
        order_id: "ORDER-RECRUITER-7",
        carrier: "DHL",
        tracking_number: "DHL-123-US",
      },
    });

    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(request.body))).toEqual({
      endpoint_id: "endpoint-1",
      type: "shipment.dispatched",
      data: {
        run_id: "run-custom",
        scenario: "outage_replay",
        purpose: "reliability-story",
        shipment_id: "SHIP-RECRUITER-7",
        order_id: "ORDER-RECRUITER-7",
        carrier: "DHL",
        tracking_number: "DHL-123-US",
      },
    });
  });

  it("sends the permanent-rejection demo without the receiver's required customer field", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      event_id: "event-1",
      delivery_id: "delivery-0",
      endpoint_id: "endpoint-1",
      type: "order.paid",
      status: "pending",
      created_at: now,
    }, 202));
    vi.stubGlobal("fetch", fetchMock);

    await publishDemoEvent(
      "endpoint-1",
      "validation-run",
      "validation-publish-key",
      {
        type: "order.paid",
        data: {
          order_id: "ORDER-INVALID",
          customer_id: "CUS-WILL-BE-OMITTED",
          amount_cents: 12_900,
          currency: "USD",
        },
      },
      "permanent_rejection",
    );

    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const body = JSON.parse(String(request.body)) as { data: Record<string, unknown> };
    expect(body.data.scenario).toBe("permanent_rejection");
    expect(body.data).not.toHaveProperty("customer_id");
    expect(body.data.order_id).toBe("ORDER-INVALID");
  });

  it("replays with the caller's stable key", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      source_delivery_id: "delivery-0",
      delivery_id: "delivery-1",
      event_id: "event-1",
      endpoint_id: "endpoint-1",
      replay_generation: 1,
      status: "pending",
      created_at: now,
    }, 202));
    vi.stubGlobal("fetch", fetchMock);

    await replayDelivery("delivery-0", "stable-replay-key");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/deliveries/delivery-0/replays",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "Idempotency-Key": "stable-replay-key" }),
      }),
    );
  });
});

describe("Receiver Lab evidence", () => {
  it("returns the independently observed request fields", async () => {
    const observation = {
      sequence: 5,
      attempt: 1,
      event_id: "event-1",
      delivery_id: "delivery-1",
      event_type: "order.paid",
      delivery_attempt: 1,
      request_timestamp: 1_777_070_400,
      received_at: now,
      response_status_code: 200,
      receiver_mode: "success",
      signature_present: true,
      body_preview: "{\"id\":\"event-1\"}",
      body_sha256: "c".repeat(64),
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      preset: "success",
      configuration: { mode: "success", failures_before_success: 0, delay_ms: 0 },
      attempts: 1,
      requests: [observation],
    }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getReceiverLab()).resolves.toEqual(expect.objectContaining({
      requests: [observation],
    }));
  });

  it("changes only the preset because Receiver Lab preserves evidence by default", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      preset: "success",
      configuration: { mode: "success", failures_before_success: 0, delay_ms: 0 },
      attempts: 0,
      requests: [],
    }));
    vi.stubGlobal("fetch", fetchMock);

    await setReceiverLabPreset("success", "run-1");

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/demo/receiver-lab?run_id=run-1");
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(request.body))).toEqual({ preset: "success" });
  });
});
