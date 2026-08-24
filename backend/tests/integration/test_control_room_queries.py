"""PostgreSQL proofs for bounded Control Room read models."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventharbor.control_room import ControlRoomRepository, decode_cursor
from eventharbor.deliveries.state_machine import DeliveryStatus
from eventharbor.models import Delivery, Endpoint, Event

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _event(*, created_at: datetime, number: int) -> Event:
    payload_bytes = f'{{"number":{number}}}'.encode()
    return Event(
        id=UUID(f"00000000-0000-0000-0001-{number:012d}"),
        source="control-room-integration",
        event_type="invoice.failed" if number % 2 == 0 else "invoice.paid",
        idempotency_key=f"control-room-event-{number}",
        payload={"number": number},
        payload_bytes=payload_bytes,
        request_fingerprint_sha256=sha256(f"request-{number}".encode()).hexdigest(),
        payload_sha256=sha256(payload_bytes).hexdigest(),
        created_at=created_at,
    )


def _delivery(
    *,
    event: Event,
    endpoint: Endpoint,
    created_at: datetime,
    status: DeliveryStatus,
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
        attempt_count=1 if status is not DeliveryStatus.PENDING else 0,
        next_attempt_at=created_at if status is DeliveryStatus.PENDING else None,
        delivered_at=created_at if status is DeliveryStatus.DELIVERED else None,
        last_error=("seeded failure" if status is DeliveryStatus.DEAD_LETTERED else None),
        created_at=created_at,
        updated_at=created_at,
    )


async def _seed_control_room_history(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[list[Endpoint], list[Event], list[Delivery]]:
    base = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    primary = Endpoint(
        id=UUID("00000000-0000-0000-0002-000000000001"),
        name="Primary receiver",
        target_url="http://receiver.test/primary",
        signing_secret=b"never-return-this-primary-secret",
        enabled=True,
        created_at=base,
        updated_at=base,
    )
    disabled = Endpoint(
        id=UUID("00000000-0000-0000-0002-000000000002"),
        name="Disabled receiver",
        target_url="http://receiver.test/disabled",
        signing_secret=b"never-return-this-disabled-secret",
        enabled=False,
        created_at=base + timedelta(seconds=1),
        updated_at=base + timedelta(seconds=1),
    )
    events = [
        _event(created_at=base + timedelta(minutes=number), number=number) for number in range(1, 5)
    ]

    superseded_dead_letter = _delivery(
        event=events[0],
        endpoint=primary,
        created_at=events[0].created_at,
        status=DeliveryStatus.DEAD_LETTERED,
    )
    latest_delivered = _delivery(
        event=events[0],
        endpoint=primary,
        created_at=events[0].created_at + timedelta(seconds=30),
        status=DeliveryStatus.DELIVERED,
        generation=1,
        source=superseded_dead_letter,
    )
    latest_dead_letter = _delivery(
        event=events[1],
        endpoint=primary,
        created_at=events[1].created_at,
        status=DeliveryStatus.DEAD_LETTERED,
    )
    latest_pending = _delivery(
        event=events[2],
        endpoint=disabled,
        created_at=events[2].created_at,
        status=DeliveryStatus.PENDING,
    )
    disabled_dead_letter = _delivery(
        event=events[3],
        endpoint=disabled,
        created_at=events[3].created_at,
        status=DeliveryStatus.DEAD_LETTERED,
    )

    async with sessions() as session, session.begin():
        session.add_all([primary, disabled, *events, superseded_dead_letter])
        await session.flush()
        session.add_all(
            [latest_delivered, latest_dead_letter, latest_pending, disabled_dead_letter]
        )

    return (
        [primary, disabled],
        events,
        [
            superseded_dead_letter,
            latest_delivered,
            latest_dead_letter,
            latest_pending,
            disabled_dead_letter,
        ],
    )


async def test_overview_counts_only_latest_delivery_generations(
    database_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_control_room_history(database_sessions)

    async with database_sessions() as session:
        snapshot = await ControlRoomRepository(session).overview()

    assert snapshot.generated_at.tzinfo is not None
    assert snapshot.events_total == 4
    assert snapshot.endpoints_total == 2
    assert snapshot.endpoints_enabled == 1
    assert snapshot.deliveries_by_status == {
        DeliveryStatus.PENDING: 1,
        DeliveryStatus.DELIVERED: 1,
        DeliveryStatus.DEAD_LETTERED: 2,
    }
    assert snapshot.actionable_dead_letters == 2


async def test_event_keyset_pagination_is_stable_and_excludes_superseded_generation(
    database_sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, events, deliveries = await _seed_control_room_history(database_sessions)

    async with database_sessions() as session:
        repository = ControlRoomRepository(session)
        first = await repository.list_events(
            limit=2,
            cursor=None,
            event_type=None,
            endpoint_id=None,
            delivery_status=None,
        )
        assert first.next_cursor is not None
        second = await repository.list_events(
            limit=2,
            cursor=decode_cursor(first.next_cursor),
            event_type=None,
            endpoint_id=None,
            delivery_status=None,
        )
        dead_letters = await repository.list_events(
            limit=10,
            cursor=None,
            event_type=None,
            endpoint_id=None,
            delivery_status=DeliveryStatus.DEAD_LETTERED,
        )
        paid = await repository.list_events(
            limit=10,
            cursor=None,
            event_type="invoice.paid",
            endpoint_id=None,
            delivery_status=None,
        )

    assert [record.event.id for record in first.items] == [events[3].id, events[2].id]
    assert [record.event.id for record in second.items] == [events[1].id, events[0].id]
    assert second.next_cursor is None
    assert [record.event.id for record in dead_letters.items] == [events[3].id, events[1].id]
    assert [record.event.id for record in paid.items] == [events[2].id, events[0].id]
    event_one = next(record for record in second.items if record.event.id == events[0].id)
    assert event_one.latest_delivery.id == deliveries[1].id
    assert event_one.latest_delivery.replay_generation == 1
    assert event_one.latest_delivery.status is DeliveryStatus.DELIVERED


async def test_dead_letter_and_endpoint_pages_use_stable_cursors(
    database_sessions: async_sessionmaker[AsyncSession],
) -> None:
    endpoints, events, _ = await _seed_control_room_history(database_sessions)

    async with database_sessions() as session:
        repository = ControlRoomRepository(session)
        dead_letter_first = await repository.list_dead_letters(
            limit=1,
            cursor=None,
            endpoint_id=None,
        )
        assert dead_letter_first.next_cursor is not None
        dead_letter_second = await repository.list_dead_letters(
            limit=1,
            cursor=decode_cursor(dead_letter_first.next_cursor),
            endpoint_id=None,
        )
        primary_only = await repository.list_dead_letters(
            limit=10,
            cursor=None,
            endpoint_id=endpoints[0].id,
        )
        endpoint_first = await repository.list_endpoints(limit=1, cursor=None)
        assert endpoint_first.next_cursor is not None
        endpoint_second = await repository.list_endpoints(
            limit=1,
            cursor=decode_cursor(endpoint_first.next_cursor),
        )

    assert [record.event.id for record in dead_letter_first.items] == [events[3].id]
    assert [record.event.id for record in dead_letter_second.items] == [events[1].id]
    assert dead_letter_second.next_cursor is None
    assert [record.event.id for record in primary_only.items] == [events[1].id]
    assert [endpoint.id for endpoint in endpoint_first.items] == [endpoints[1].id]
    assert [endpoint.id for endpoint in endpoint_second.items] == [endpoints[0].id]
    assert endpoint_second.next_cursor is None
