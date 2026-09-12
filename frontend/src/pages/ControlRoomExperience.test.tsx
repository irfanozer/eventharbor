import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Link, MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import type { Delivery, DeliveryAttempt, EventAccepted, EventDetail, ReceiverLabRequest } from "../types";
import { ControlRoomExperience } from "./ControlRoomExperience";

vi.mock("../api", () => ({
  ApiError: class extends Error {},
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
    event_type: "order.paid",
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
    type: "order.paid",
    data: {
      run_id: runId,
      scenario: "outage_replay",
      order_id: "ORDER-PRESERVED",
      customer_id: "CUS-PRESERVED",
      amount_cents: 12_900,
      currency: "USD",
    },
    payload_sha256: hash,
    request_fingerprint_sha256: "b".repeat(64),
    idempotency_key: "story-run-7f2a",
    created_at: now,
    deliveries: [original, replay],
  };

  beforeEach(() => {
    vi.resetAllMocks();
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    sessionStorage.clear();
    sessionStorage.setItem("eventharbor.control-room.event-id", eventId);
    sessionStorage.setItem("eventharbor.control-room.mode", "guided");
    sessionStorage.setItem("eventharbor.control-room.tour", JSON.stringify({
      runId,
      eventId,
      replayDeliveryId: replay.id,
      payload: {
        type: "order.paid",
        data: {
          order_id: "ORDER-PRESERVED",
          customer_id: "CUS-PRESERVED",
          amount_cents: 12_900,
          currency: "USD",
        },
      },
      scenarioId: "outage_replay",
    }));

    vi.mocked(api.getEvent).mockResolvedValue(completedEvent);
    vi.mocked(api.getEndpoint).mockResolvedValue({
      id: "endpoint-1",
      name: "Orders receiver · Receiver Lab",
      url: "http://receiver-lab:8100/webhooks/orders",
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

    expect(screen.getByRole("heading", { name: /what happens at the receiver/i })).toBeVisible();
    expect(screen.getByRole("group", { name: "Receiver incident" })).toBeVisible();
    expect(await screen.findByRole("heading", { name: "The saved event was delivered." })).toBeVisible();
    expect(screen.getAllByText("ORDER-PRESERVED").length).toBeGreaterThan(0);
    const rows = await screen.findByRole("list", { name: "Recorded delivery attempts" });
    expect(within(rows).getAllByRole("listitem")).toHaveLength(5);

    fireEvent.click(screen.getByRole("button", { name: /try another case/i }));

    expect(screen.getByText("ORDER-PRESERVED")).toBeVisible();
    expect(screen.getByRole("heading", { name: "The saved event was delivered." })).toBeVisible();
    expect(screen.getByText(/the last result stays visible/i)).toBeVisible();
    expect(screen.getByRole("button", { name: /a temporary outage/i })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByText(/change the sample event/i));
    expect(screen.getByLabelText(/Order ID/i)).not.toHaveValue("ORDER-PRESERVED");
    expect(sessionStorage.getItem("eventharbor.control-room.event-id")).toBe(eventId);
    expect(sessionStorage.getItem("eventharbor.control-room.tour")).toContain(eventId);

    fireEvent.click(screen.getByRole("button", { name: /a required field is missing/i }));
    expect(screen.getAllByText("data.customer_id").length).toBeGreaterThan(0);
    expect(screen.getByText(/intentionally omitted from the JSON body/i)).toBeVisible();
    expect(within(rows).getAllByRole("listitem")).toHaveLength(5);
    expect(api.publishDemoEvent).not.toHaveBeenCalled();
    await waitFor(() => expect(api.getEvent).toHaveBeenCalledWith(eventId));
  });

  it("opens a correction link as a fresh valid draft instead of restoring the stopped event", () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/?event_type=shipment.dispatched&new_event=1"]}>
          <ControlRoomExperience />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByText(/change the sample event/i));
    expect(screen.getByRole("combobox", { name: /business event type/i })).toHaveValue("shipment.dispatched");
    expect(screen.getByLabelText(/Tracking number/i)).not.toHaveValue("");
    expect(screen.queryByRole("heading", { name: "ORDER-PRESERVED" })).not.toBeInTheDocument();
    expect(api.getEvent).not.toHaveBeenCalled();
    expect(sessionStorage.getItem("eventharbor.control-room.event-id")).toBeNull();
    expect(sessionStorage.getItem("eventharbor.control-room.tour")).toBeNull();
  });

  function mount() {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const rendered = render(<QueryClientProvider client={client}><MemoryRouter><ControlRoomExperience /><Link to="/?event_type=shipment.dispatched&new_event=1">Navigate to a fresh draft</Link></MemoryRouter></QueryClientProvider>);
    return { client, ...rendered };
  }

  function stoppedRun() {
    const saved = JSON.parse(sessionStorage.getItem("eventharbor.control-room.tour")!);
    delete saved.replayDeliveryId;
    sessionStorage.setItem("eventharbor.control-room.tour", JSON.stringify(saved));
    vi.mocked(api.getEvent).mockResolvedValue({ ...completedEvent, deliveries: [original] });
  }

  it("shows four cases and an empty request log without starting anything automatically", () => {
    sessionStorage.clear();
    mount();
    expect(within(screen.getByRole("group", { name: "Receiver incident" })).getAllByRole("button")).toHaveLength(4);
    expect(screen.getByRole("button", { name: /send test event/i })).toBeEnabled();
    expect(screen.getByRole("heading", { name: "Ready when you are." })).toBeVisible();
    expect(screen.queryByRole("list", { name: "Recorded delivery attempts" })).not.toBeInTheDocument();
    expect(screen.getByText(/see what happened behind the scenes/i).closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText(/change the sample event/i).closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText("Edit the event type and its fields")).toBeVisible();
    fireEvent.click(screen.getByText(/change the sample event/i));
    expect(screen.getByLabelText(/Order ID/i)).toBeVisible();
    expect(screen.getByText(/nothing is sent until you press/i)).toBeVisible();
    expect(screen.queryByRole("group", { name: "Demo mode" })).not.toBeInTheDocument();
    expect(api.publishDemoEvent).not.toHaveBeenCalled();
    expect(api.setReceiverLabPreset).not.toHaveBeenCalled();
    expect(api.replayDelivery).not.toHaveBeenCalled();
  });

  it("opens the technical evidence for the same run without sending or replaying anything", async () => {
    mount();
    await screen.findByRole("heading", { name: "The saved event was delivered." });
    fireEvent.click(screen.getByText(/see what happened behind the scenes/i));
    expect(await screen.findByRole("link", { name: /open the full event record/i })).toHaveAttribute("href", "/events/" + eventId);
    expect(document.querySelectorAll("#live-proof")).toHaveLength(1);
    expect(api.publishDemoEvent).not.toHaveBeenCalled();
    expect(api.setReceiverLabPreset).not.toHaveBeenCalled();
    expect(api.replayDelivery).not.toHaveBeenCalled();
  });

  it("waits for receiver repair and explicit approval, reusing one replay key after a lost response", async () => {
    stoppedRun();
    let healthy = false;
    const state = () => ({ preset: healthy ? "success" as const : "dead_letter" as const, configuration: { mode: healthy ? "success" : "fail", failures_before_success: 0, delay_ms: 0 }, attempts: 4, requests: [] });
    vi.mocked(api.getReceiverLab).mockImplementation(async () => state());
    vi.mocked(api.setReceiverLabPreset).mockImplementation(async () => { healthy = true; return state(); });
    const accepted = { source_delivery_id: original.id, delivery_id: replay.id, event_id: eventId, endpoint_id: original.endpoint_id, replay_generation: 1, status: "pending" as const, created_at: now };
    vi.mocked(api.replayDelivery).mockRejectedValueOnce(new Error("Acknowledgement lost"))
      .mockImplementationOnce(async () => { vi.mocked(api.getEvent).mockResolvedValue(completedEvent); return accepted; });
    mount();
    const restore = await screen.findByRole("button", { name: /bring the test receiver back online/i });
    await waitFor(() => expect(restore).toBeEnabled());
    expect(api.setReceiverLabPreset).not.toHaveBeenCalled();
    expect(api.replayDelivery).not.toHaveBeenCalled();
    fireEvent.click(restore);
    const review = await screen.findByRole("button", { name: /review and resend/i });
    expect(api.setReceiverLabPreset).toHaveBeenCalledWith("success", runId);
    expect(api.replayDelivery).not.toHaveBeenCalled();
    fireEvent.click(review);
    const dialog = await screen.findByRole("alertdialog");
    expect(api.replayDelivery).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: /approve and replay/i }));
    await screen.findByText("Acknowledgement lost");
    fireEvent.click(within(dialog).getByRole("button", { name: /approve and replay/i }));
    await screen.findByRole("heading", { name: "The saved event was delivered." });
    expect(api.replayDelivery).toHaveBeenCalledTimes(2);
    expect(api.replayDelivery).toHaveBeenNthCalledWith(1, original.id, "control-room-replay-" + runId);
    expect(api.replayDelivery).toHaveBeenNthCalledWith(2, original.id, "control-room-replay-" + runId);
    expect(api.publishDemoEvent).not.toHaveBeenCalled();
  });

  it("retries an unconfirmed publish with the same payload and key without resetting the receiver", async () => {
    sessionStorage.clear();
    vi.mocked(api.getReceiverLab).mockResolvedValue({ preset: "dead_letter", configuration: { mode: "fail", failures_before_success: 0, delay_ms: 0 }, attempts: 0, requests: [] });
    vi.mocked(api.ensureReceiverLabEndpoint).mockResolvedValue(await api.getEndpoint("endpoint-1"));
    const accepted = { event_id: eventId, delivery_id: original.id, endpoint_id: original.endpoint_id, type: "order.paid", status: "pending" as const, created_at: now };
    vi.mocked(api.publishDemoEvent).mockRejectedValueOnce(new Error("Connection interrupted")).mockResolvedValueOnce(accepted);
    mount();
    fireEvent.click(screen.getByRole("button", { name: /send test event/i }));
    await screen.findByText("Connection interrupted");
    const originalRequest = vi.mocked(api.publishDemoEvent).mock.calls[0];
    expect(screen.getByRole("button", { name: /a temporary outage/i })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /continue sending this event/i }));
    await screen.findByRole("heading", { name: "The saved event was delivered." });
    expect(api.publishDemoEvent).toHaveBeenCalledTimes(2);
    expect(vi.mocked(api.publishDemoEvent).mock.calls[1]).toEqual(originalRequest);
    expect(api.ensureReceiverLabEndpoint).toHaveBeenCalledTimes(1);
    expect(api.setReceiverLabPreset).not.toHaveBeenCalled();
  });

  it("restores an event-ID-only session by reading its saved run, without publishing", async () => {
    sessionStorage.removeItem("eventharbor.control-room.tour");
    mount();
    await screen.findByRole("heading", { name: "The saved event was delivered." });
    await waitFor(() => expect(api.getReceiverLab).toHaveBeenCalledWith(runId));
    expect(api.getReceiverLab).not.toHaveBeenCalledWith("");
    expect(api.publishDemoEvent).not.toHaveBeenCalled();
    expect(sessionStorage.getItem("eventharbor.control-room.tour")).toContain(runId);
  });

  it("does not call an unrelated HTTP 400 the expected missing-field outcome", async () => {
    stoppedRun();
    const rejected = { ...original, attempt_count: 1 };
    vi.mocked(api.getEvent).mockResolvedValue({ ...completedEvent, data: { ...completedEvent.data, scenario: "permanent_rejection" }, deliveries: [rejected] });
    vi.mocked(api.getDeliveryAttempts).mockResolvedValue({ delivery: rejected, attempts: [{ ...attempt("bad-request", 1, 400), disposition: "terminal_failure", response_body_excerpt: JSON.stringify({ code: "some_other_error" }) }] });
    mount();
    await screen.findByRole("heading", { name: "Delivery stopped. Review the response." });
    expect(await screen.findByText("400 · Request rejected")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Invalid data. No pointless retries." })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /start a corrected event/i })).not.toBeInTheDocument();
    expect(api.replayDelivery).not.toHaveBeenCalled();
  });

  it("reads the final attempt when a delivery becomes terminal", async () => {
    stoppedRun();
    const waiting = { ...original, status: "retry_wait" as const, attempt_count: 1 };
    vi.mocked(api.getEvent).mockResolvedValue({ ...completedEvent, deliveries: [waiting] });
    vi.mocked(api.getDeliveryAttempts).mockResolvedValue({ delivery: waiting, attempts: [originalAttempts[0]!] });
    const { client } = mount();
    const rows = await screen.findByRole("list", { name: "Recorded delivery attempts" });
    expect(within(rows).getAllByRole("listitem")).toHaveLength(1);
    const callsBefore = vi.mocked(api.getDeliveryAttempts).mock.calls.length;
    vi.mocked(api.getDeliveryAttempts).mockResolvedValue({ delivery: original, attempts: originalAttempts });
    await act(async () => { client.setQueryData(["event", eventId], { ...completedEvent, deliveries: [original] }); });
    await waitFor(() => expect(within(rows).getAllByRole("listitem")).toHaveLength(4));
    expect(vi.mocked(api.getDeliveryAttempts).mock.calls.length).toBeGreaterThan(callsBefore);
  });
  it("shows the complete history without reading controls or new delivery actions", async () => {
    mount();
    await screen.findByRole("heading", { name: "The saved event was delivered." });
    const list = await screen.findByRole("list", { name: "Recorded delivery attempts" });
    await waitFor(() => expect(within(list).getAllByRole("listitem")).toHaveLength(5));
    expect(list.closest("details")).toBeNull();
    expect(screen.getByText("Saved by EventHarbor")).toBeVisible();
    expect(screen.queryByRole("button", { name: /next request|back/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/jump to a request/i)).not.toBeInTheDocument();
    expect(within(list).queryByRole("button")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /try another case/i }));
    expect(within(list).getAllByRole("listitem")).toHaveLength(5);
    expect(within(list).getByText("200 · Accepted")).toBeVisible();
    expect(api.publishDemoEvent).not.toHaveBeenCalled();
    expect(api.setReceiverLabPreset).not.toHaveBeenCalled();
    expect(api.replayDelivery).not.toHaveBeenCalled();
  });

  it("does not restore an old pending send into a fresh draft after navigation", async () => {
    sessionStorage.clear();
    vi.mocked(api.getReceiverLab).mockResolvedValue({ preset: "dead_letter", configuration: { mode: "fail", failures_before_success: 0, delay_ms: 0 }, attempts: 0, requests: [] });
    vi.mocked(api.ensureReceiverLabEndpoint).mockResolvedValue(await api.getEndpoint("endpoint-1"));
    let finish!: (accepted: EventAccepted) => void;
    vi.mocked(api.publishDemoEvent).mockImplementationOnce(() => new Promise<EventAccepted>((resolve) => { finish = resolve; }));
    mount();
    fireEvent.click(screen.getByRole("button", { name: /send test event/i }));
    await waitFor(() => expect(api.publishDemoEvent).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("link", { name: "Navigate to a fresh draft" }));
    await screen.findByRole("heading", { name: "Ready when you are." });
    await act(async () => { finish({ event_id: eventId, delivery_id: original.id, endpoint_id: original.endpoint_id, type: "order.paid", status: "pending", created_at: now }); });
    expect(screen.getByRole("heading", { name: "Ready when you are." })).toBeVisible();
    expect(sessionStorage.getItem("eventharbor.control-room.tour")).toBeNull();
    expect(sessionStorage.getItem("eventharbor.control-room.event-id")).toBeNull();
    expect(api.getEvent).not.toHaveBeenCalled();
  });
});
