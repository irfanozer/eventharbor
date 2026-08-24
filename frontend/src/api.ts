import type {
  DeadLetterListResponse,
  DeliveryAttemptsResponse,
  DeliveryStatus,
  Endpoint,
  EndpointListResponse,
  EventAccepted,
  EventDetail,
  EventListResponse,
  Overview,
  ProblemDetails,
  ReceiverLabPreset,
  ReceiverLabState,
  ReplayAccepted,
} from "./types";

const API_ROOT = "/api/v1";
export const RECEIVER_LAB_URL = "http://receiver-lab:8100/webhooks";

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;

  constructor(status: number, message: string, code?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    let problem: ProblemDetails | undefined;
    try {
      problem = (await response.json()) as ProblemDetails;
    } catch {
      // Some infrastructure errors do not have a JSON response body.
    }
    throw new ApiError(
      response.status,
      problem?.detail ?? problem?.title ?? `Request failed with status ${response.status}.`,
      problem?.code,
    );
  }

  return (await response.json()) as T;
}

function queryString(values: Record<string, string | number | undefined>): string {
  const parameters = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== "") parameters.set(key, String(value));
  }
  const encoded = parameters.toString();
  return encoded ? `?${encoded}` : "";
}

export function getOverview(): Promise<Overview> {
  return request("/control-room/overview");
}

export function getEvents(filters: {
  limit?: number;
  cursor?: string;
  eventType?: string;
  endpointId?: string;
  deliveryStatus?: DeliveryStatus | "";
} = {}): Promise<EventListResponse> {
  return request(
    `/events${queryString({
      limit: filters.limit ?? 25,
      cursor: filters.cursor,
      event_type: filters.eventType,
      endpoint_id: filters.endpointId,
      delivery_status: filters.deliveryStatus,
    })}`,
  );
}

export function getEvent(eventId: string): Promise<EventDetail> {
  return request(`/events/${encodeURIComponent(eventId)}`);
}

export function getDeliveryAttempts(deliveryId: string): Promise<DeliveryAttemptsResponse> {
  return request(`/deliveries/${encodeURIComponent(deliveryId)}/attempts`);
}

export function getEndpoints(options: { limit?: number; cursor?: string } = {}): Promise<EndpointListResponse> {
  return request(
    `/endpoints${queryString({ limit: options.limit ?? 100, cursor: options.cursor })}`,
  );
}

export function getEndpoint(endpointId: string): Promise<Endpoint> {
  return request(`/endpoints/${encodeURIComponent(endpointId)}`);
}

export function getDeadLetters(options: { limit?: number; cursor?: string } = {}): Promise<DeadLetterListResponse> {
  return request(
    `/dead-letters${queryString({ limit: options.limit ?? 25, cursor: options.cursor })}`,
  );
}

export function getReceiverLab(): Promise<ReceiverLabState> {
  return request("/demo/receiver-lab");
}

export function setReceiverLabPreset(preset: ReceiverLabPreset): Promise<ReceiverLabState> {
  return request("/demo/receiver-lab", {
    method: "PUT",
    body: JSON.stringify({ preset }),
  });
}

interface CreatedEndpoint extends Endpoint {
  signing_secret: string;
}

async function createReceiverLabEndpoint(): Promise<Endpoint> {
  const created = await request<CreatedEndpoint>("/endpoints", {
    method: "POST",
    body: JSON.stringify({
      name: "Receiver Lab · Control Room",
      url: RECEIVER_LAB_URL,
    }),
  });

  // Creation secrets are intentionally not retained by the browser.
  return {
    id: created.id,
    name: created.name,
    url: created.url,
    enabled: created.enabled,
    secret_version: created.secret_version,
    created_at: created.created_at,
    updated_at: created.updated_at ?? created.created_at,
  };
}

export async function ensureReceiverLabEndpoint(): Promise<Endpoint> {
  const endpoints = await getEndpoints({ limit: 100 });
  const existing = endpoints.items.find(
    (endpoint) => endpoint.url === RECEIVER_LAB_URL && endpoint.enabled,
  );
  return existing ?? createReceiverLabEndpoint();
}

export function publishDemoEvent(
  endpointId: string,
  runId: string = crypto.randomUUID(),
  idempotencyKey: string = `control-room-story-${runId}`,
): Promise<EventAccepted> {
  return request("/events", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({
      endpoint_id: endpointId,
      type: "demo.order.ready",
      data: {
        run_id: runId,
        order_id: `ORDER-${runId.slice(0, 8).toUpperCase()}`,
        purpose: "reliability-story",
      },
    }),
  });
}

export function replayDelivery(
  deliveryId: string,
  idempotencyKey: string = `control-room-replay-${crypto.randomUUID()}`,
): Promise<ReplayAccepted> {
  return request(`/deliveries/${encodeURIComponent(deliveryId)}/replays`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
  });
}
