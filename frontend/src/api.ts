import type {
  DeadLetterListResponse,
  DeliveryAttemptsResponse,
  DeliveryStatus,
  DemoScenarioId,
  DemoEventPayload,
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
import type { DemoEventTypeId } from "./types";
import {
  DEFAULT_DEMO_EVENT_TYPE,
  RECEIVER_LAB_BASE_URL,
  demoEventDefinition,
} from "./demoEvents";

const API_ROOT = "/api/v1";
/** Legacy generic Receiver Lab route retained for older stored demonstrations. */
export const RECEIVER_LAB_URL = RECEIVER_LAB_BASE_URL;

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

export function getReceiverLab(runId?: string): Promise<ReceiverLabState> {
  return request(`/demo/receiver-lab${queryString({ run_id: runId })}`);
}

export function setReceiverLabPreset(preset: ReceiverLabPreset, runId?: string): Promise<ReceiverLabState> {
  return request(`/demo/receiver-lab${queryString({ run_id: runId })}`, {
    method: "PUT",
    body: JSON.stringify({ preset }),
  });
}

interface CreatedEndpoint extends Endpoint {
  signing_secret: string;
}

async function createReceiverLabEndpoint(eventType: DemoEventTypeId): Promise<Endpoint> {
  const definition = demoEventDefinition(eventType);
  const created = await request<CreatedEndpoint>("/endpoints", {
    method: "POST",
    body: JSON.stringify({
      name: definition.destinationName,
      url: definition.destinationUrl,
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

export async function ensureReceiverLabEndpoint(
  eventType: DemoEventTypeId = DEFAULT_DEMO_EVENT_TYPE,
): Promise<Endpoint> {
  const definition = demoEventDefinition(eventType);
  const endpoints = await getEndpoints({ limit: 100 });
  const existing = endpoints.items.find(
    (endpoint) => endpoint.url === definition.destinationUrl && endpoint.enabled,
  );
  return existing ?? createReceiverLabEndpoint(eventType);
}

export function publishDemoEvent(
  endpointId: string,
  runId: string = crypto.randomUUID(),
  idempotencyKey: string = `control-room-story-${runId}`,
  payload?: DemoEventPayload,
  scenarioId: DemoScenarioId = "outage_replay",
): Promise<EventAccepted> {
  const suffix = runId.slice(0, 8).toUpperCase();
  const resolvedPayload: DemoEventPayload = payload ?? {
    type: DEFAULT_DEMO_EVENT_TYPE,
    data: {
      order_id: `ORDER-${suffix}`,
      customer_id: `CUS-${suffix.slice(0, 6)}`,
      amount_cents: 12_900,
      currency: "USD",
    },
  };
  const definition = demoEventDefinition(resolvedPayload.type);
  const businessData: Record<string, string | number> = { ...resolvedPayload.data };

  // This scenario demonstrates a real schema rejection. The field is visibly
  // present in the editor, then deliberately omitted from the outbound event.
  if (scenarioId === "permanent_rejection") {
    delete businessData[definition.invalidField];
  }

  return request("/events", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({
      endpoint_id: endpointId,
      type: resolvedPayload.type,
      data: {
        run_id: runId,
        scenario: scenarioId,
        purpose: "reliability-story",
        ...businessData,
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
