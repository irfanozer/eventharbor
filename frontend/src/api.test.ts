import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ensureReceiverLabEndpoint,
  publishDemoEvent,
  RECEIVER_LAB_URL,
  replayDelivery,
} from "./api";

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
    const endpoint = {
      id: "endpoint-1",
      name: "Receiver Lab",
      url: RECEIVER_LAB_URL,
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
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ items: [], next_cursor: null }))
      .mockResolvedValueOnce(jsonResponse({
        id: "endpoint-2",
        name: "Receiver Lab · Control Room",
        url: RECEIVER_LAB_URL,
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
      name: "Receiver Lab · Control Room",
      url: RECEIVER_LAB_URL,
    });
  });
});

describe("Reliability story idempotency", () => {
  it("publishes a deterministic run with the caller's stable key", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      event_id: "event-1",
      delivery_id: "delivery-0",
      endpoint_id: "endpoint-1",
      type: "demo.order.ready",
      status: "pending",
      created_at: now,
    }, 202));
    vi.stubGlobal("fetch", fetchMock);

    await publishDemoEvent("endpoint-1", "stable-run", "stable-publish-key");

    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(request.headers).toEqual(expect.objectContaining({
      "Idempotency-Key": "stable-publish-key",
    }));
    expect(JSON.parse(String(request.body))).toEqual(expect.objectContaining({
      endpoint_id: "endpoint-1",
      data: expect.objectContaining({ run_id: "stable-run" }),
    }));
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
