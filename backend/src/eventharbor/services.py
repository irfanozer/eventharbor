"""Application services for endpoint registration, ingestion, and queries."""

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from eventharbor.errors import DomainError
from eventharbor.models import Delivery, DeliveryAttempt, Endpoint, Event
from eventharbor.repositories import (
    DeliveryRepository,
    EndpointRepository,
    EventRepository,
    NewEvent,
)
from eventharbor.schemas import EndpointCreateRequest, EventPublishRequest
from eventharbor.serialization import CanonicalJSONError, canonical_json_bytes, canonical_sha256

LOCAL_SOURCE = "local-api"


def _new_signing_secret() -> str:
    return f"ehsec_{secrets.token_urlsafe(32)}"


def _json_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class CreatedEndpoint:
    endpoint: Endpoint
    plaintext_signing_secret: str


@dataclass(frozen=True, slots=True)
class PublishedEvent:
    event: Event
    delivery: Delivery
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class EventDetails:
    event: Event
    deliveries: Sequence[Delivery]


@dataclass(frozen=True, slots=True)
class DeliveryAttempts:
    delivery: Delivery
    attempts: Sequence[DeliveryAttempt]


class EndpointService:
    """Register only the intentionally configured local demonstration target."""

    def __init__(
        self,
        repository: EndpointRepository,
        receiver_lab_url: str,
        secret_factory: Callable[[], str] = _new_signing_secret,
    ) -> None:
        self._repository = repository
        self._receiver_lab_url = receiver_lab_url
        self._secret_factory = secret_factory

    async def create(self, request: EndpointCreateRequest) -> CreatedEndpoint:
        if request.url != self._receiver_lab_url:
            raise DomainError(
                status_code=422,
                code="receiver_url_not_allowed",
                title="Endpoint URL is not allowed",
                detail=(
                    "This local milestone accepts only the configured Receiver Lab URL: "
                    f"{self._receiver_lab_url}"
                ),
            )

        plaintext_secret = self._secret_factory()
        endpoint = Endpoint(
            id=uuid4(),
            name=request.name,
            target_url=request.url,
            signing_secret=plaintext_secret.encode("utf-8"),
            enabled=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        await self._repository.create(endpoint)
        return CreatedEndpoint(endpoint=endpoint, plaintext_signing_secret=plaintext_secret)


class EventService:
    """Accept an event and its initial delivery in the caller's transaction."""

    def __init__(
        self,
        endpoints: EndpointRepository,
        events: EventRepository,
        deliveries: DeliveryRepository,
    ) -> None:
        self._endpoints = endpoints
        self._events = events
        self._deliveries = deliveries

    async def publish(
        self,
        request: EventPublishRequest,
        idempotency_key: str,
    ) -> PublishedEvent:
        endpoint = await self._endpoints.get(request.endpoint_id)
        if endpoint is None:
            raise DomainError(
                status_code=404,
                code="endpoint_not_found",
                title="Endpoint not found",
                detail=f"Endpoint {request.endpoint_id} does not exist.",
            )

        try:
            fingerprint = canonical_sha256(
                {
                    "endpoint_id": str(request.endpoint_id),
                    "type": request.type,
                    "data": request.data,
                }
            )
        except CanonicalJSONError as exc:
            raise DomainError(
                status_code=422,
                code="invalid_event_data",
                title="Event data is not valid JSON",
                detail="Event data must contain only finite, JSON-compatible values.",
            ) from exc

        event_id = uuid4()
        delivery_id = uuid4()
        created_at = datetime.now(UTC)
        payload_bytes = canonical_json_bytes(
            {
                "id": str(event_id),
                "type": request.type,
                "created_at": _json_timestamp(created_at),
                "data": request.data,
            }
        )
        candidate = NewEvent(
            id=event_id,
            source=LOCAL_SOURCE,
            event_type=request.type,
            idempotency_key=idempotency_key,
            payload=request.data,
            payload_bytes=payload_bytes,
            request_fingerprint_sha256=fingerprint,
            payload_sha256=sha256(payload_bytes).hexdigest(),
            created_at=created_at,
        )

        inserted = await self._events.insert_if_absent(candidate)
        if not inserted:
            return await self._resolve_idempotent_replay(
                endpoint_id=request.endpoint_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )

        if not endpoint.enabled:
            raise DomainError(
                status_code=409,
                code="endpoint_disabled",
                title="Endpoint is disabled",
                detail=f"Endpoint {request.endpoint_id} cannot accept new deliveries.",
            )

        delivery = Delivery(
            id=delivery_id,
            event_id=event_id,
            endpoint_id=endpoint.id,
            replay_generation=0,
            attempt_count=0,
            created_at=created_at,
            updated_at=created_at,
        )
        await self._deliveries.create(delivery)
        event = await self._events.get(event_id)
        if event is None:
            raise RuntimeError("inserted event could not be read in its transaction")
        return PublishedEvent(event=event, delivery=delivery, idempotent_replay=False)

    async def _resolve_idempotent_replay(
        self,
        endpoint_id: UUID,
        idempotency_key: str,
        fingerprint: str,
    ) -> PublishedEvent:
        event = await self._events.get_by_idempotency_key(LOCAL_SOURCE, idempotency_key)
        if event is None:
            raise RuntimeError("conflicting event could not be read after ON CONFLICT")
        if event.request_fingerprint_sha256 != fingerprint:
            raise DomainError(
                status_code=409,
                code="idempotency_key_reused",
                title="Idempotency key was reused",
                detail="The same Idempotency-Key was already used with a different request.",
            )

        delivery = await self._deliveries.get_initial(event.id, endpoint_id)
        if delivery is None:
            raise RuntimeError("accepted event is missing its initial delivery")
        return PublishedEvent(event=event, delivery=delivery, idempotent_replay=True)


class QueryService:
    """Read event and attempt timelines without triggering lazy ORM loading."""

    def __init__(self, events: EventRepository, deliveries: DeliveryRepository) -> None:
        self._events = events
        self._deliveries = deliveries

    async def event_details(self, event_id: UUID) -> EventDetails:
        event = await self._events.get(event_id)
        if event is None:
            raise DomainError(
                status_code=404,
                code="event_not_found",
                title="Event not found",
                detail=f"Event {event_id} does not exist.",
            )
        deliveries = await self._deliveries.list_for_event(event_id)
        return EventDetails(event=event, deliveries=deliveries)

    async def delivery_attempts(self, delivery_id: UUID) -> DeliveryAttempts:
        delivery = await self._deliveries.get_with_attempts(delivery_id)
        if delivery is None:
            raise DomainError(
                status_code=404,
                code="delivery_not_found",
                title="Delivery not found",
                detail=f"Delivery {delivery_id} does not exist.",
            )
        return DeliveryAttempts(delivery=delivery, attempts=list(delivery.attempts))
