"""Application services for endpoint registration, ingestion, and queries."""

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from eventharbor.deliveries.retry import replay_block_code
from eventharbor.deliveries.state_machine import DeliveryStatus
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
RECEIVER_LAB_ROUTES = ("orders", "shipping", "inventory")


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


@dataclass(frozen=True, slots=True)
class ReplayedDelivery:
    source_delivery: Delivery
    delivery: Delivery
    idempotent_replay: bool


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
        receiver_base = self._receiver_lab_url.rstrip("/")
        allowed_urls = {
            receiver_base,
            *(f"{receiver_base}/{route}" for route in RECEIVER_LAB_ROUTES),
        }
        if request.url.rstrip("/") not in allowed_urls:
            raise DomainError(
                status_code=422,
                code="receiver_url_not_allowed",
                title="Endpoint URL is not allowed",
                detail=(
                    "This local milestone accepts only the configured Receiver Lab "
                    f"routes under {receiver_base}."
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


class ReplayService:
    """Create one durable, idempotent successor for a dead-letter delivery."""

    def __init__(
        self,
        endpoints: EndpointRepository,
        events: EventRepository,
        deliveries: DeliveryRepository,
    ) -> None:
        self._endpoints = endpoints
        self._events = events
        self._deliveries = deliveries

    async def replay(self, delivery_id: UUID, idempotency_key: str) -> ReplayedDelivery:
        candidate = await self._deliveries.get(delivery_id)
        if candidate is None:
            raise DomainError(
                status_code=404,
                code="delivery_not_found",
                title="Delivery not found",
                detail=f"Delivery {delivery_id} does not exist.",
            )

        # Every generation in this event chain shares this stable mutex. The database
        # constraints remain the final safety net, while the lock makes all expected
        # conflicts deterministic instead of surfacing an IntegrityError.
        event = await self._events.get_for_update(candidate.event_id)
        if event is None:
            raise RuntimeError("delivery references an event that could not be locked")
        source = await self._deliveries.get_for_update(delivery_id)
        if source is None:
            raise RuntimeError("delivery disappeared after its event was locked")

        existing = await self._deliveries.get_by_replay_request(source.id, idempotency_key)
        if existing is not None:
            return ReplayedDelivery(
                source_delivery=source,
                delivery=existing,
                idempotent_replay=True,
            )

        latest = await self._deliveries.get_latest(source.event_id, source.endpoint_id)
        if latest is None:
            raise RuntimeError("delivery chain has no latest generation")
        if latest.id != source.id:
            raise DomainError(
                status_code=409,
                code="replay_source_superseded",
                title="Replay source has been superseded",
                detail=(
                    f"Delivery {delivery_id} has already been superseded by delivery {latest.id}."
                ),
            )
        if source.status != DeliveryStatus.DEAD_LETTERED:
            raise DomainError(
                status_code=409,
                code="delivery_not_replayable",
                title="Delivery is not replayable",
                detail=(
                    f"Delivery {delivery_id} has status {source.status.value}; only the latest "
                    "dead-lettered generation can be replayed."
                ),
            )

        attempts = await self._deliveries.list_attempts(source.id)
        block_code = (
            replay_block_code(
                attempts[-1].http_status_code,
                attempts[-1].response_body_excerpt,
            )
            if attempts
            else None
        )
        if block_code is not None:
            raise DomainError(
                status_code=409,
                code=block_code.value,
                title="Payload correction is required",
                detail=(
                    "The receiver permanently rejected this immutable payload. Publish a "
                    "corrected event instead of replaying the unchanged body."
                ),
            )

        active = await self._deliveries.get_active(source.event_id, source.endpoint_id)
        if active is not None:
            raise DomainError(
                status_code=409,
                code="active_delivery_exists",
                title="An active delivery already exists",
                detail=(f"Delivery {active.id} is already active for this event and endpoint."),
            )

        endpoint = await self._endpoints.get(source.endpoint_id)
        if endpoint is None:
            raise RuntimeError("delivery references an endpoint that does not exist")
        if not endpoint.enabled:
            raise DomainError(
                status_code=409,
                code="endpoint_disabled",
                title="Endpoint is disabled",
                detail=f"Endpoint {endpoint.id} cannot accept a replay delivery.",
            )

        now = datetime.now(UTC)
        replay = Delivery(
            id=uuid4(),
            event_id=source.event_id,
            endpoint_id=source.endpoint_id,
            replay_generation=source.replay_generation + 1,
            replayed_from_delivery_id=source.id,
            replay_idempotency_key=idempotency_key,
            status=DeliveryStatus.PENDING,
            attempt_count=0,
            created_at=now,
            updated_at=now,
        )
        await self._deliveries.create(replay)
        return ReplayedDelivery(
            source_delivery=source,
            delivery=replay,
            idempotent_replay=False,
        )


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
