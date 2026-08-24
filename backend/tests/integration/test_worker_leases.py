"""Real-PostgreSQL proofs for worker leases and competing claimers."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventharbor.config import Environment, Settings
from eventharbor.deliveries.retry import DeliveryDisposition
from eventharbor.deliveries.state_machine import DeliveryAttemptStatus, DeliveryStatus
from eventharbor.deliveries.transport import AttemptResult, OutboundWebhook
from eventharbor.deliveries.worker import DeliveryResolution, DeliveryWorker
from eventharbor.models import Delivery, DeliveryAttempt, Endpoint, Event
from eventharbor.repositories import DeliveryRepository, EventRepository
from eventharbor.services import QueryService

RECEIVER_URL = "http://receiver-lab.test/webhooks"


def _settings(database_url: str, *, max_attempts: int = 8) -> Settings:
    return Settings(
        environment=Environment.TEST,
        database_url=database_url,
        receiver_lab_url=RECEIVER_URL,
        worker_http_timeout_seconds=1,
        worker_lease_seconds=5,
        worker_max_attempts=max_attempts,
        worker_base_delay_seconds=0.01,
        worker_max_delay_seconds=0.1,
        _env_file=None,
    )


async def _seed_delivery(
    sessions: async_sessionmaker[AsyncSession],
    *,
    status: DeliveryStatus,
    attempt_count: int,
    lease_owner: str | None = None,
    lease_token: UUID | None = None,
    lease_expires_at: datetime | None = None,
    endpoint_enabled: bool = True,
) -> UUID:
    endpoint = Endpoint(
        name="Lease Test Receiver",
        target_url=RECEIVER_URL,
        signing_secret=b"lease-test-secret",
        enabled=endpoint_enabled,
    )
    event = Event(
        source="lease-integration-test",
        event_type="order.ready",
        idempotency_key="lease-test-event",
        payload={"type": "order.ready", "data": {"order_id": "order-1"}},
        payload_bytes=b'{"data":{"order_id":"order-1"},"type":"order.ready"}',
        request_fingerprint_sha256="b" * 64,
        payload_sha256="a" * 64,
    )
    delivery = Delivery(
        event=event,
        endpoint=endpoint,
        status=status,
        attempt_count=attempt_count,
        next_attempt_at=datetime.now(UTC) - timedelta(minutes=1),
        lease_owner=lease_owner,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
    )
    async with sessions() as session, session.begin():
        session.add_all([endpoint, event, delivery])
        await session.flush()
        evidence_time = datetime.now(UTC) - timedelta(minutes=2)
        for attempt_number in range(1, attempt_count + 1):
            is_current_lease = (
                status == DeliveryStatus.IN_PROGRESS and attempt_number == attempt_count
            )
            if is_current_lease and lease_token is None:
                raise ValueError("in-progress seed requires a lease token")
            token = lease_token if is_current_lease else uuid4()
            if token is None:
                raise RuntimeError("attempt seed is missing a token")
            session.add(
                DeliveryAttempt(
                    id=uuid4(),
                    delivery_id=delivery.id,
                    attempt_number=attempt_number,
                    status=(
                        DeliveryAttemptStatus.IN_PROGRESS
                        if is_current_lease
                        else DeliveryAttemptStatus.COMPLETED
                    ),
                    lease_token=token,
                    disposition=None if is_current_lease else DeliveryDisposition.RETRY,
                    http_status_code=None if is_current_lease else 503,
                    error_type=None,
                    error_message=None,
                    response_body_excerpt=None if is_current_lease else "retry",
                    duration_ms=None if is_current_lease else 5,
                    request_timestamp=None if is_current_lease else int(evidence_time.timestamp()),
                    retry_scheduled_for=None,
                    started_at=None if is_current_lease else evidence_time,
                    finished_at=None if is_current_lease else evidence_time,
                    resolved_at=None if is_current_lease else evidence_time,
                )
            )
        await session.flush()
        delivery_id = delivery.id
    return delivery_id


def _success_result() -> AttemptResult:
    started_at = datetime.now(UTC)
    return AttemptResult(
        disposition=DeliveryDisposition.SUCCEEDED,
        http_status_code=200,
        error_type=None,
        error_message=None,
        response_body_excerpt="ok",
        request_timestamp=int(started_at.timestamp()),
        retry_after_seconds=None,
        started_at=started_at,
        finished_at=started_at + timedelta(milliseconds=5),
        duration_ms=5,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_stale_token_cannot_finalize(
    database_sessions: async_sessionmaker[AsyncSession],
    migrated_test_database: str,
) -> None:
    stale_token = UUID("00000000-0000-0000-0000-000000000099")
    delivery_id = await _seed_delivery(
        database_sessions,
        status=DeliveryStatus.IN_PROGRESS,
        attempt_count=1,
        lease_owner="reused-worker-id",
        lease_token=stale_token,
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    settings = _settings(migrated_test_database)

    async with database_sessions() as session:
        stale_attempt_id = await session.scalar(
            select(DeliveryAttempt.id).where(DeliveryAttempt.delivery_id == delivery_id)
        )
    assert stale_attempt_id is not None

    async with httpx.AsyncClient() as client:
        recovering_worker = DeliveryWorker(
            database_sessions,
            client,
            settings,
            worker_id="reused-worker-id",
        )
        stale_worker = DeliveryWorker(
            database_sessions,
            client,
            settings,
            worker_id="reused-worker-id",
        )

        reclaimed = await recovering_worker.claim_one()
        assert isinstance(reclaimed, OutboundWebhook)
        assert reclaimed.delivery_id == delivery_id
        assert reclaimed.attempt_number == 2
        assert reclaimed.lease_token != stale_token

        stale_snapshot = OutboundWebhook(
            attempt_id=stale_attempt_id,
            delivery_id=reclaimed.delivery_id,
            event_id=reclaimed.event_id,
            event_type=reclaimed.event_type,
            endpoint_url=reclaimed.endpoint_url,
            signing_secret=reclaimed.signing_secret,
            payload_bytes=reclaimed.payload_bytes,
            attempt_number=1,
            lease_token=stale_token,
        )
        assert await stale_worker.finalize(stale_snapshot, _success_result()) is False

    async with database_sessions() as session:
        persisted = await session.get(Delivery, delivery_id)
        attempts = (
            await session.scalars(
                select(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_id == delivery_id)
                .order_by(DeliveryAttempt.attempt_number)
            )
        ).all()

    assert persisted is not None
    assert persisted.status == DeliveryStatus.IN_PROGRESS
    assert persisted.attempt_count == 2
    assert persisted.lease_owner == "reused-worker-id"
    assert persisted.lease_token == reclaimed.lease_token
    assert [attempt.status for attempt in attempts] == [
        DeliveryAttemptStatus.INDETERMINATE,
        DeliveryAttemptStatus.IN_PROGRESS,
    ]
    assert attempts[0].error_type == "lease_expired"
    assert attempts[0].resolved_at is not None
    assert attempts[1].lease_token == reclaimed.lease_token


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint_enabled", "max_attempts", "attempt_count", "expected_reason"),
    [
        (True, 3, 3, "maximum attempt count"),
        (False, 8, 1, "endpoint disabled"),
    ],
)
async def test_expired_lease_is_dead_lettered_without_an_extra_attempt(
    database_sessions: async_sessionmaker[AsyncSession],
    migrated_test_database: str,
    endpoint_enabled: bool,
    max_attempts: int,
    attempt_count: int,
    expected_reason: str,
) -> None:
    token = uuid4()
    delivery_id = await _seed_delivery(
        database_sessions,
        status=DeliveryStatus.IN_PROGRESS,
        attempt_count=attempt_count,
        lease_owner="expired-worker",
        lease_token=token,
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=1),
        endpoint_enabled=endpoint_enabled,
    )

    async with httpx.AsyncClient() as client:
        worker = DeliveryWorker(
            database_sessions,
            client,
            _settings(migrated_test_database, max_attempts=max_attempts),
            worker_id="recovery-worker",
        )
        resolution = await worker.claim_one()

    assert isinstance(resolution, DeliveryResolution)
    assert expected_reason in resolution.reason
    async with database_sessions() as session:
        delivery = await session.get(Delivery, delivery_id)
        attempts = (
            await session.scalars(
                select(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_id == delivery_id)
                .order_by(DeliveryAttempt.attempt_number)
            )
        ).all()

    assert delivery is not None
    assert delivery.status == DeliveryStatus.DEAD_LETTERED
    assert delivery.attempt_count == attempt_count
    assert delivery.lease_token is None
    assert len(attempts) == attempt_count
    assert attempts[-1].status == DeliveryAttemptStatus.INDETERMINATE
    assert expected_reason in (attempts[-1].error_message or "")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "attempt_count", "endpoint_enabled", "max_attempts", "expected_reason"),
    [
        (DeliveryStatus.PENDING, 0, False, 3, "endpoint is disabled"),
        (DeliveryStatus.RETRY_WAIT, 3, True, 2, "maximum attempt count"),
    ],
)
async def test_due_unclaimable_delivery_is_dead_lettered_without_http_or_new_evidence(
    database_sessions: async_sessionmaker[AsyncSession],
    migrated_test_database: str,
    status: DeliveryStatus,
    attempt_count: int,
    endpoint_enabled: bool,
    max_attempts: int,
    expected_reason: str,
) -> None:
    delivery_id = await _seed_delivery(
        database_sessions,
        status=status,
        attempt_count=attempt_count,
        endpoint_enabled=endpoint_enabled,
    )

    def unexpected_request(_: httpx.Request) -> httpx.Response:
        pytest.fail("database-only resolution must not send an HTTP request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
        worker = DeliveryWorker(
            database_sessions,
            client,
            _settings(migrated_test_database, max_attempts=max_attempts),
            worker_id="budget-and-endpoint-worker",
        )
        assert await worker.run_once() is True

    async with database_sessions() as session:
        delivery = await session.get(Delivery, delivery_id)
        attempts = (
            await session.scalars(
                select(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_id == delivery_id)
                .order_by(DeliveryAttempt.attempt_number)
            )
        ).all()

    assert delivery is not None
    assert delivery.status == DeliveryStatus.DEAD_LETTERED
    assert delivery.attempt_count == attempt_count
    assert delivery.next_attempt_at is None
    assert delivery.lease_token is None
    assert expected_reason in (delivery.last_error or "")
    assert len(attempts) == attempt_count
    assert all(attempt.status == DeliveryAttemptStatus.COMPLETED for attempt in attempts)


class _PausingClaimSession(AsyncSession):
    """Pause after the delivery row is locked, before the claim transaction commits."""

    claim_has_lock: asyncio.Event
    release_claim: asyncio.Event

    async def get(self, entity: Any, ident: Any, **kwargs: Any) -> Any:
        if entity is Event:
            self.claim_has_lock.set()
            await self.release_claim.wait()
        return await super().get(entity, ident, **kwargs)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_competing_claimers_cannot_both_claim_one_pending_delivery(
    database_sessions: async_sessionmaker[AsyncSession],
    migrated_test_database: str,
) -> None:
    delivery_id = await _seed_delivery(
        database_sessions,
        status=DeliveryStatus.PENDING,
        attempt_count=0,
    )
    settings = _settings(migrated_test_database)
    _PausingClaimSession.claim_has_lock = asyncio.Event()
    _PausingClaimSession.release_claim = asyncio.Event()
    pausing_sessions = async_sessionmaker(
        database_sessions.kw["bind"],
        class_=_PausingClaimSession,
        expire_on_commit=False,
    )

    async with httpx.AsyncClient() as client:
        first_worker = DeliveryWorker(
            pausing_sessions,
            client,
            settings,
            worker_id="first-worker",
        )
        competing_worker = DeliveryWorker(
            database_sessions,
            client,
            settings,
            worker_id="competing-worker",
        )

        first_claim_task = asyncio.create_task(first_worker.claim_one())
        try:
            await asyncio.wait_for(_PausingClaimSession.claim_has_lock.wait(), timeout=2)
            competing_claim = await asyncio.wait_for(competing_worker.claim_one(), timeout=2)
        finally:
            _PausingClaimSession.release_claim.set()
        first_claim = await asyncio.wait_for(first_claim_task, timeout=2)

    assert isinstance(first_claim, OutboundWebhook)
    assert first_claim.delivery_id == delivery_id
    assert competing_claim is None

    async with database_sessions() as session:
        persisted = await session.get(Delivery, delivery_id)
        attempts = (
            await session.scalars(
                select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == delivery_id)
            )
        ).all()
    assert persisted is not None
    assert persisted.status == DeliveryStatus.IN_PROGRESS
    assert persisted.attempt_count == 1
    assert persisted.lease_owner == "first-worker"
    assert persisted.lease_token == first_claim.lease_token
    assert len(attempts) == 1
    assert attempts[0].status == DeliveryAttemptStatus.IN_PROGRESS
    assert attempts[0].lease_token == first_claim.lease_token


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delivery_timeline_uses_one_joined_snapshot_without_async_lazy_loading(
    database_sessions: async_sessionmaker[AsyncSession],
) -> None:
    delivery_id = await _seed_delivery(
        database_sessions,
        status=DeliveryStatus.RETRY_WAIT,
        attempt_count=2,
    )
    engine = database_sessions.kw["bind"]
    statements: list[str] = []

    def record_statement(*args: Any) -> None:
        statements.append(str(args[2]))

    sa_event.listen(engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        async with database_sessions() as session, session.begin():
            timeline = await QueryService(
                EventRepository(session),
                DeliveryRepository(session),
            ).delivery_attempts(delivery_id)
    finally:
        sa_event.remove(engine.sync_engine, "before_cursor_execute", record_statement)

    # These relationship values remain available after the AsyncSession is closed,
    # proving that the service did not leave an async lazy-load boundary behind.
    assert timeline.delivery.id == delivery_id
    assert [attempt.attempt_number for attempt in timeline.attempts] == [1, 2]
    assert len(statements) == 1
    assert "JOIN delivery_attempts" in statements[0]
