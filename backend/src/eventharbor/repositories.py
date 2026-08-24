"""PostgreSQL repositories that flush work but never own transactions."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from eventharbor.deliveries.state_machine import DeliveryStatus
from eventharbor.models import Delivery, DeliveryAttempt, Endpoint, Event


@dataclass(frozen=True, slots=True)
class NewEvent:
    """All immutable values required for an idempotent event insert."""

    id: UUID
    source: str
    event_type: str
    idempotency_key: str
    payload: dict[str, Any]
    payload_bytes: bytes
    request_fingerprint_sha256: str
    payload_sha256: str
    created_at: datetime


class EndpointRepository:
    """Persistence operations for webhook destinations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, endpoint: Endpoint) -> Endpoint:
        self._session.add(endpoint)
        await self._session.flush()
        return endpoint

    async def get(self, endpoint_id: UUID) -> Endpoint | None:
        return await self._session.get(Endpoint, endpoint_id)


class EventRepository:
    """Persistence operations for immutable, idempotently accepted events."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_if_absent(self, event: NewEvent) -> bool:
        """Insert once per ``(source, idempotency_key)`` without races."""

        statement = (
            insert(Event)
            .values(
                id=event.id,
                source=event.source,
                event_type=event.event_type,
                idempotency_key=event.idempotency_key,
                payload=event.payload,
                payload_bytes=event.payload_bytes,
                request_fingerprint_sha256=event.request_fingerprint_sha256,
                payload_sha256=event.payload_sha256,
                created_at=event.created_at,
            )
            .on_conflict_do_nothing(index_elements=[Event.source, Event.idempotency_key])
            .returning(Event.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def get_by_idempotency_key(self, source: str, idempotency_key: str) -> Event | None:
        statement = select(Event).where(
            Event.source == source,
            Event.idempotency_key == idempotency_key,
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get(self, event_id: UUID) -> Event | None:
        return await self._session.get(Event, event_id)

    async def get_for_update(self, event_id: UUID) -> Event | None:
        """Lock the stable event row used to serialize delivery generations."""

        statement = select(Event).where(Event.id == event_id).with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()


class DeliveryRepository:
    """Persistence and read-model operations for delivery generations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, delivery: Delivery) -> Delivery:
        self._session.add(delivery)
        await self._session.flush()
        return delivery

    async def get_initial(self, event_id: UUID, endpoint_id: UUID) -> Delivery | None:
        statement = select(Delivery).where(
            Delivery.event_id == event_id,
            Delivery.endpoint_id == endpoint_id,
            Delivery.replay_generation == 0,
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_for_event(self, event_id: UUID) -> Sequence[Delivery]:
        statement = (
            select(Delivery)
            .where(Delivery.event_id == event_id)
            .order_by(Delivery.replay_generation, Delivery.created_at, Delivery.id)
        )
        return (await self._session.scalars(statement)).all()

    async def get(self, delivery_id: UUID) -> Delivery | None:
        return await self._session.get(Delivery, delivery_id)

    async def get_for_update(self, delivery_id: UUID) -> Delivery | None:
        statement = select(Delivery).where(Delivery.id == delivery_id).with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_replay_request(
        self,
        source_delivery_id: UUID,
        idempotency_key: str,
    ) -> Delivery | None:
        statement = select(Delivery).where(
            Delivery.replayed_from_delivery_id == source_delivery_id,
            Delivery.replay_idempotency_key == idempotency_key,
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_latest(self, event_id: UUID, endpoint_id: UUID) -> Delivery | None:
        statement = (
            select(Delivery)
            .where(
                Delivery.event_id == event_id,
                Delivery.endpoint_id == endpoint_id,
            )
            .order_by(Delivery.replay_generation.desc())
            .limit(1)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_active(self, event_id: UUID, endpoint_id: UUID) -> Delivery | None:
        statement = select(Delivery).where(
            Delivery.event_id == event_id,
            Delivery.endpoint_id == endpoint_id,
            Delivery.status.in_(
                (
                    DeliveryStatus.PENDING,
                    DeliveryStatus.IN_PROGRESS,
                    DeliveryStatus.RETRY_WAIT,
                )
            ),
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_with_attempts(self, delivery_id: UUID) -> Delivery | None:
        """Load one delivery and its ordered timeline in a single SQL snapshot."""

        statement = (
            select(Delivery)
            .options(joinedload(Delivery.attempts))
            .where(Delivery.id == delivery_id)
        )
        result = await self._session.execute(statement)
        return result.unique().scalar_one_or_none()

    async def list_attempts(self, delivery_id: UUID) -> Sequence[DeliveryAttempt]:
        statement = (
            select(DeliveryAttempt)
            .where(DeliveryAttempt.delivery_id == delivery_id)
            .order_by(DeliveryAttempt.attempt_number)
        )
        return (await self._session.scalars(statement)).all()
