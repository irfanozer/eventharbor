"""PostgreSQL proof for bounded public-demo retention cleanup."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventharbor.deliveries.state_machine import DeliveryStatus
from eventharbor.maintenance import purge_expired_demo_events
from eventharbor.models import Delivery, Endpoint, Event

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _event(*, created_at: datetime, label: str) -> Event:
    payload_bytes = f'{{"label":"{label}"}}'.encode()
    return Event(
        id=uuid4(),
        source="retention-integration",
        event_type="demo.retention",
        idempotency_key=f"retention-{label}",
        payload={"label": label},
        payload_bytes=payload_bytes,
        request_fingerprint_sha256=sha256(f"request-{label}".encode()).hexdigest(),
        payload_sha256=sha256(payload_bytes).hexdigest(),
        created_at=created_at,
    )


def _delivery(
    event: Event,
    endpoint: Endpoint,
    *,
    status: DeliveryStatus,
    created_at: datetime,
    generation: int = 0,
    source: Delivery | None = None,
) -> Delivery:
    return Delivery(
        id=uuid4(),
        event_id=event.id,
        endpoint_id=endpoint.id,
        replay_generation=generation,
        replayed_from_delivery_id=None if source is None else source.id,
        replay_idempotency_key=None if source is None else f"replay-{event.id}",
        status=status,
        attempt_count=0 if status is DeliveryStatus.PENDING else 1,
        next_attempt_at=created_at if status is DeliveryStatus.PENDING else None,
        delivered_at=created_at if status is DeliveryStatus.DELIVERED else None,
        created_at=created_at,
        updated_at=created_at,
    )


async def _count(
    sessions: async_sessionmaker[AsyncSession],
    model: type[object],
) -> int:
    async with sessions() as session:
        value = await session.scalar(select(func.count()).select_from(model))
    assert value is not None
    return value


async def test_cleanup_deletes_only_old_terminal_histories(
    database_sessions: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    endpoint = Endpoint(
        id=uuid4(),
        name="Retention Receiver",
        target_url="http://receiver.test/retention",
        signing_secret=b"retention-test-secret",
        enabled=True,
        created_at=old,
        updated_at=old,
    )
    old_terminal = _event(created_at=old, label="old-terminal")
    old_active = _event(created_at=old, label="old-active")
    recent_terminal = _event(created_at=now, label="recent-terminal")
    original = _delivery(
        old_terminal,
        endpoint,
        status=DeliveryStatus.DEAD_LETTERED,
        created_at=old,
    )
    replay = _delivery(
        old_terminal,
        endpoint,
        status=DeliveryStatus.DELIVERED,
        created_at=old + timedelta(minutes=1),
        generation=1,
        source=original,
    )
    pending = _delivery(
        old_active,
        endpoint,
        status=DeliveryStatus.PENDING,
        created_at=old,
    )
    recent = _delivery(
        recent_terminal,
        endpoint,
        status=DeliveryStatus.DELIVERED,
        created_at=now,
    )

    async with database_sessions() as session, session.begin():
        session.add_all([endpoint, old_terminal, old_active, recent_terminal, original])
        await session.flush()
        session.add_all([replay, pending, recent])

    result = await purge_expired_demo_events(
        database_sessions,
        cutoff=now - timedelta(days=7),
        batch_size=1,
        max_events=10,
    )

    assert result.lock_acquired is True
    assert result.deleted_events == 1
    assert result.batches == 1
    assert await _count(database_sessions, Event) == 2
    assert await _count(database_sessions, Delivery) == 2
    assert await _count(database_sessions, Endpoint) == 1

    async with database_sessions() as session:
        assert await session.get(Event, old_terminal.id) is None
        assert await session.get(Event, old_active.id) is not None
        assert await session.get(Event, recent_terminal.id) is not None


async def test_cleanup_rejects_naive_cutoff(
    database_sessions: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await purge_expired_demo_events(
            database_sessions,
            cutoff=datetime.now(),
            batch_size=100,
            max_events=100,
        )
