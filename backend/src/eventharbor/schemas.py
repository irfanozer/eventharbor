"""Validated HTTP request and response contracts for the vertical slice."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from eventharbor.deliveries.retry import DeliveryDisposition, ReplayBlockCode
from eventharbor.deliveries.state_machine import DeliveryAttemptStatus, DeliveryStatus


class EndpointCreateRequest(BaseModel):
    """Register the deterministic Receiver Lab as a local destination."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2_048)

    @field_validator("name", mode="before")
    @classmethod
    def name_must_not_be_whitespace(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized


class EndpointCreatedResponse(BaseModel):
    """Endpoint metadata plus the plaintext secret shown only at creation."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    url: str
    enabled: bool
    signing_secret: str
    secret_version: int
    created_at: datetime


class EndpointPublicResponse(BaseModel):
    """Safe endpoint metadata for Control Room reads."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    name: str
    url: str
    enabled: bool
    secret_version: int
    created_at: datetime
    updated_at: datetime


class EventPublishRequest(BaseModel):
    """One event and the explicit endpoint that should receive it."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: UUID
    type: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    data: dict[str, Any]

    @field_validator("type", mode="before")
    @classmethod
    def type_must_not_be_whitespace(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("type must not be blank")
        return normalized


class EventAcceptedResponse(BaseModel):
    """Stable identifiers returned after durable ingestion."""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    delivery_id: UUID
    endpoint_id: UUID
    type: str
    status: DeliveryStatus
    created_at: datetime


class ReplayAcceptedResponse(BaseModel):
    """Identifiers for a newly accepted or previously accepted manual replay."""

    model_config = ConfigDict(extra="forbid")

    source_delivery_id: UUID
    delivery_id: UUID
    event_id: UUID
    endpoint_id: UUID
    replay_generation: int
    status: DeliveryStatus
    created_at: datetime


class DeliverySummaryResponse(BaseModel):
    """Current state of one event-to-endpoint delivery generation."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    endpoint_id: UUID
    replay_generation: int
    replayed_from_delivery_id: UUID | None
    status: DeliveryStatus
    attempt_count: int
    next_attempt_at: datetime | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    delivered_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class DeliveryStatusCounts(BaseModel):
    """Explicit aggregate counts for every delivery state."""

    model_config = ConfigDict(extra="forbid")

    pending: int = 0
    in_progress: int = 0
    retry_wait: int = 0
    delivered: int = 0
    dead_lettered: int = 0


class ControlRoomOverviewResponse(BaseModel):
    """Bounded aggregate snapshot for the recruiter-facing dashboard."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    events_total: int
    endpoints_total: int
    endpoints_enabled: int
    deliveries: DeliveryStatusCounts
    actionable_dead_letters: int


class EventListItemResponse(BaseModel):
    """Compact event row that intentionally omits payload and idempotency data."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    source: str
    type: str
    created_at: datetime
    endpoint: EndpointPublicResponse
    latest_delivery: DeliverySummaryResponse
    generation_count: int


class EventListResponse(BaseModel):
    """A bounded, cursor-paginated recent-event page."""

    model_config = ConfigDict(extra="forbid")

    items: list[EventListItemResponse]
    next_cursor: str | None


class EndpointListResponse(BaseModel):
    """A bounded, cursor-paginated endpoint page."""

    model_config = ConfigDict(extra="forbid")

    items: list[EndpointPublicResponse]
    next_cursor: str | None


class DeadLetterItemResponse(BaseModel):
    """A latest-generation dead letter that may require operator action."""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_type: str
    event_created_at: datetime
    endpoint: EndpointPublicResponse
    delivery: DeliverySummaryResponse
    replayable: bool
    blocked_code: ReplayBlockCode | None
    blocked_reason: str | None


class DeadLetterListResponse(BaseModel):
    """A bounded, cursor-paginated actionable dead-letter page."""

    model_config = ConfigDict(extra="forbid")

    items: list[DeadLetterItemResponse]
    next_cursor: str | None


class EventDetailResponse(BaseModel):
    """An accepted event plus every delivery generation created from it."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    source: str
    type: str
    data: dict[str, Any]
    payload_sha256: str
    request_fingerprint_sha256: str
    idempotency_key: str
    created_at: datetime
    deliveries: list[DeliverySummaryResponse]


class DeliveryAttemptResponse(BaseModel):
    """Durable evidence for a reserved outbound HTTP attempt."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    attempt_number: int
    status: DeliveryAttemptStatus
    disposition: DeliveryDisposition | None
    http_status_code: int | None
    error_type: str | None
    error_message: str | None
    response_body_excerpt: str | None
    duration_ms: int | None
    request_timestamp: int | None
    retry_scheduled_for: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime


class DeliveryAttemptsResponse(BaseModel):
    """A delivery's current state and chronological attempt history."""

    model_config = ConfigDict(extra="forbid")

    delivery: DeliverySummaryResponse
    attempts: list[DeliveryAttemptResponse]


class ReceiverLabPreset(StrEnum):
    """Server-owned deterministic Receiver Lab scenarios."""

    SUCCESS = "success"
    RETRY_THEN_RECOVER = "retry_then_recover"
    RATE_LIMITED = "rate_limited"
    RATE_LIMIT_THEN_RECOVER = "rate_limit_then_recover"
    TIMEOUT = "timeout"
    PERMANENT_FAILURE = "permanent_failure"
    DEAD_LETTER = "dead_letter"


class ReceiverLabPresetRequest(BaseModel):
    """Select one safe Receiver Lab scenario without accepting arbitrary URLs."""

    model_config = ConfigDict(extra="forbid")

    preset: ReceiverLabPreset


class ReceiverLabConfigurationResponse(BaseModel):
    """Current deterministic behavior of Receiver Lab."""

    model_config = ConfigDict(extra="forbid")

    mode: str
    failures_before_success: int
    delay_ms: int


class ReceiverLabRequestResponse(BaseModel):
    """Bounded receiver-side evidence for one observed webhook."""

    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    attempt: int = Field(ge=1)
    event_id: str | None
    delivery_id: str | None
    event_type: str | None
    receiver_route: str = "legacy-generic"
    delivery_attempt: int | None = Field(ge=1)
    request_timestamp: int | None = Field(ge=0)
    received_at: datetime
    response_status_code: int = Field(ge=100, le=599)
    receiver_mode: str
    signature_present: bool
    body_preview: str
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReceiverLabStateResponse(BaseModel):
    """Control Room-safe Receiver Lab state and recent observations."""

    model_config = ConfigDict(extra="forbid")

    preset: ReceiverLabPreset | None
    configuration: ReceiverLabConfigurationResponse
    attempts: int
    requests: list[ReceiverLabRequestResponse]
