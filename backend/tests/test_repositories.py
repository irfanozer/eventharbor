from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from eventharbor.models import Delivery, DeliveryAttempt, Endpoint, Event
from eventharbor.repositories import (
    DeliveryRepository,
    EndpointRepository,
    EventRepository,
    NewEvent,
)


@pytest.mark.asyncio
async def test_endpoint_repository_adds_flushes_and_gets() -> None:
    session = MagicMock(spec=AsyncSession)
    session.flush = AsyncMock()
    target = MagicMock(spec=Endpoint)
    session.get = AsyncMock(return_value=target)
    repository = EndpointRepository(session)

    assert await repository.create(target) is target
    assert await repository.get(uuid4()) is target
    session.add.assert_called_once_with(target)
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("inserted_id", [uuid4(), None])
async def test_event_repository_uses_conflict_safe_insert(inserted_id) -> None:
    now = datetime.now(UTC)
    result = MagicMock()
    result.scalar_one_or_none.return_value = inserted_id
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=result)
    repository = EventRepository(session)
    candidate = NewEvent(
        id=uuid4(),
        source="local-api",
        event_type="test.event",
        idempotency_key="key",
        payload={},
        payload_bytes=b"{}",
        request_fingerprint_sha256="1" * 64,
        payload_sha256="0" * 64,
        created_at=now,
    )

    assert await repository.insert_if_absent(candidate) is (inserted_id is not None)
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_event_repository_queries_by_key_and_id() -> None:
    found = MagicMock(spec=Event)
    result = MagicMock()
    result.scalar_one_or_none.return_value = found
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=result)
    session.get = AsyncMock(return_value=found)
    repository = EventRepository(session)

    assert await repository.get_by_idempotency_key("source", "key") is found
    assert await repository.get(uuid4()) is found


@pytest.mark.asyncio
async def test_delivery_repository_covers_create_and_read_models() -> None:
    delivery = MagicMock(spec=Delivery)
    attempt = MagicMock(spec=DeliveryAttempt)
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = delivery
    joined_result = MagicMock()
    joined_result.unique.return_value.scalar_one_or_none.return_value = delivery
    scalars = MagicMock()
    scalars.all.side_effect = [[delivery], [attempt]]
    session = MagicMock(spec=AsyncSession)
    session.flush = AsyncMock()
    session.execute = AsyncMock(side_effect=[scalar_result, joined_result])
    session.scalars = AsyncMock(return_value=scalars)
    session.get = AsyncMock(return_value=delivery)
    repository = DeliveryRepository(session)

    assert await repository.create(delivery) is delivery
    assert await repository.get_initial(uuid4(), uuid4()) is delivery
    assert await repository.list_for_event(uuid4()) == [delivery]
    assert await repository.get(uuid4()) is delivery
    assert await repository.get_with_attempts(uuid4()) is delivery
    assert await repository.list_attempts(uuid4()) == [attempt]
    session.add.assert_called_once_with(delivery)
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_replay_repository_queries_lock_and_order_the_delivery_chain() -> None:
    delivery = MagicMock(spec=Delivery)
    result = MagicMock()
    result.scalar_one_or_none.return_value = delivery
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=result)
    repository = DeliveryRepository(session)

    assert await repository.get_for_update(uuid4()) is delivery
    assert await repository.get_by_replay_request(uuid4(), "replay-key") is delivery
    assert await repository.get_latest(uuid4(), uuid4()) is delivery
    assert await repository.get_active(uuid4(), uuid4()) is delivery
    assert session.execute.await_count == 4


@pytest.mark.asyncio
async def test_event_repository_can_lock_the_replay_mutex() -> None:
    event = MagicMock(spec=Event)
    result = MagicMock()
    result.scalar_one_or_none.return_value = event
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=result)

    assert await EventRepository(session).get_for_update(uuid4()) is event
