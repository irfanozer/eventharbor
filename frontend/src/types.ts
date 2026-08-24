export type DeliveryStatus =
  | "pending"
  | "in_progress"
  | "retry_wait"
  | "delivered"
  | "dead_lettered";

export type AttemptStatus = "in_progress" | "completed" | "indeterminate";

export type DeliveryDisposition = "succeeded" | "retry" | "terminal_failure";

export type ReceiverLabPreset =
  | "success"
  | "retry_then_recover"
  | "rate_limited"
  | "rate_limit_then_recover"
  | "timeout"
  | "permanent_failure"
  | "dead_letter";

export type DemoScenarioId =
  | "outage_replay"
  | "transient_recovery"
  | "rate_limit_recovery"
  | "permanent_rejection";

/** Editable business data used by the recruiter-facing reliability story. */
export interface DemoEventPayload {
  order_id: string;
  amount_cents: number;
  note: string;
}

export interface Endpoint {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  secret_version: number;
  created_at: string;
  updated_at: string;
}

export interface Delivery {
  id: string;
  endpoint_id: string;
  replay_generation: number;
  replayed_from_delivery_id: string | null;
  status: DeliveryStatus;
  attempt_count: number;
  next_attempt_at: string | null;
  lease_owner: string | null;
  lease_expires_at: string | null;
  delivered_at: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface DeliveryStatusCounts {
  pending: number;
  in_progress: number;
  retry_wait: number;
  delivered: number;
  dead_lettered: number;
}

export interface Overview {
  generated_at: string;
  events_total: number;
  endpoints_total: number;
  endpoints_enabled: number;
  deliveries: DeliveryStatusCounts;
  actionable_dead_letters: number;
}

export interface EventListItem {
  id: string;
  source: string;
  type: string;
  created_at: string;
  endpoint: Endpoint;
  latest_delivery: Delivery;
  generation_count: number;
}

export interface EventListResponse {
  items: EventListItem[];
  next_cursor: string | null;
}

export interface EventDetail {
  id: string;
  source: string;
  type: string;
  data: Record<string, unknown>;
  payload_sha256: string;
  request_fingerprint_sha256: string;
  idempotency_key: string;
  created_at: string;
  deliveries: Delivery[];
}

export interface DeliveryAttempt {
  id: string;
  attempt_number: number;
  status: AttemptStatus;
  disposition: DeliveryDisposition | null;
  http_status_code: number | null;
  error_type: string | null;
  error_message: string | null;
  response_body_excerpt: string | null;
  duration_ms: number | null;
  request_timestamp: number | null;
  retry_scheduled_for: string | null;
  started_at: string | null;
  finished_at: string | null;
  resolved_at: string | null;
  created_at: string;
}

export interface DeliveryAttemptsResponse {
  delivery: Delivery;
  attempts: DeliveryAttempt[];
}

export interface EndpointListResponse {
  items: Endpoint[];
  next_cursor: string | null;
}

export interface DeadLetterItem {
  event_id: string;
  event_type: string;
  event_created_at: string;
  endpoint: Endpoint;
  delivery: Delivery;
  replayable: boolean;
  blocked_reason: string | null;
}

export interface DeadLetterListResponse {
  items: DeadLetterItem[];
  next_cursor: string | null;
}

export interface ReceiverLabRequest {
  /** Monotonic position in Receiver Lab's evidence log, across scenario changes. */
  sequence: number;
  /** Attempt number within the receiver's current deterministic scenario. */
  attempt: number;
  event_id: string | null;
  delivery_id: string | null;
  event_type: string | null;
  /** Attempt number supplied by the EventHarbor worker. */
  delivery_attempt: number | null;
  /** Epoch seconds included in the signed EventHarbor request. */
  request_timestamp: number | null;
  received_at: string;
  response_status_code: number;
  receiver_mode: string;
  signature_present: boolean;
  body_preview: string;
  body_sha256: string;
}

export interface ReceiverLabState {
  preset: ReceiverLabPreset | null;
  configuration: {
    mode: string;
    failures_before_success: number;
    delay_ms: number;
  };
  attempts: number;
  requests: ReceiverLabRequest[];
}

export interface EventAccepted {
  event_id: string;
  delivery_id: string;
  endpoint_id: string;
  type: string;
  status: DeliveryStatus;
  created_at: string;
}

export interface ReplayAccepted {
  source_delivery_id: string;
  delivery_id: string;
  event_id: string;
  endpoint_id: string;
  replay_generation: number;
  status: DeliveryStatus;
  created_at: string;
}

export interface ProblemDetails {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  instance?: string;
  code?: string;
}
