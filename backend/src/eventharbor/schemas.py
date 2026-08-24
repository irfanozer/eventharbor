"""Validated HTTP request and response contracts for the vertical slice."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from eventharbor.deliveries.retry import DeliveryDisposition
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
