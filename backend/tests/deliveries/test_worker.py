"""Isolated orchestration tests for the PostgreSQL-backed delivery worker."""

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from eventharbor.config import Settings
from eventharbor.deliveries.retry import DeliveryDisposition
from eventharbor.deliveries.state_machine import DeliveryAttemptStatus, DeliveryStatus
from eventharbor.deliveries.transport import AttemptResult, OutboundWebhook
from eventharbor.deliveries.worker import DeliveryResolution, DeliveryWorker
from eventharbor.models import Delivery, DeliveryAttempt, Endpoint, Event

EVENT_ID = UUID("00000000-0000-0000-0000-000000000001")
DELIVERY_ID = UUID("00000000-0000-0000-0000-000000000002")
ENDPOINT_ID = UUID("00000000-0000-0000-0000-000000000003")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000004")
ATTEMPT_ID = UUID("00000000-0000-0000-0000-000000000005")
NOW = datetime(2026, 8, 23, 17, 0, tzinfo=UTC)


class _AsyncContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> bool:
        return False


class _ClockResult:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def scalar_one(self) -> datetime:
        return self._value


class _ScalarResult:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def one_or_none(self) -> object | None:
        return self._value


class _FakeSession:
    """The narrow AsyncSession behavior used by one worker transaction."""

    def __init__(
        self,
        delivery: Delivery | None,
        *,
        now: datetime = NOW,
        related: Mapping[type[object], object] | None = None,
        attempts: list[DeliveryAttempt | None] | None = None,
    ) -> None:
        self.delivery = delivery
        self.now = now
        self.related = related or {}
        self.scalar_values = iter([delivery, *(attempts or [])])
        self.added: list[object] = []
        self.flush_count = 0

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    def begin(self) -> _AsyncContext:
        return _AsyncContext()

    async def execute(self, _: object) -> _ClockResult:
        return _ClockResult(self.now)

    async def scalars(self, _: object) -> _ScalarResult:
        return _ScalarResult(next(self.scalar_values))

    async def get(self, model: type[object], _: object) -> object | None:
        return self.related.get(model)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flush_count += 1


class _SessionFactory:
    def __init__(self, *sessions: _FakeSession) -> None:
        self._sessions = iter(sessions)

    def __call__(self) -> _FakeSession:
        return next(self._sessions)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "worker_http_timeout_seconds": 1,
        "worker_lease_seconds": 5,
        "worker_max_attempts": 3,
        "worker_base_delay_seconds": 10,
        "worker_max_delay_seconds": 60,
        "worker_retry_after_cap_seconds": 30,
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def _event() -> Event:
    return Event(
        id=EVENT_ID,
        source="demo",
        event_type="order.shipped",
        idempotency_key="publish-1",
        payload={"type": "order.shipped", "data": {"order_id": "123"}},
        payload_bytes=b'{"data":{"order_id":"123"},"type":"order.shipped"}',
        request_fingerprint_sha256="b" * 64,
        payload_sha256="a" * 64,
        created_at=NOW,
    )


def _endpoint(*, enabled: bool = True) -> Endpoint:
    return Endpoint(
        id=ENDPOINT_ID,
        name="Receiver Lab",
        target_url="http://receiver-lab:8100/webhooks",
        signing_secret=b"local-test-secret",
        enabled=enabled,
        created_at=NOW,
        updated_at=NOW,
    )


def _delivery(
    *,
    status: DeliveryStatus = DeliveryStatus.IN_PROGRESS,
    attempt_count: int = 1,
) -> Delivery:
    return Delivery(
        id=DELIVERY_ID,
        event_id=EVENT_ID,
        endpoint_id=ENDPOINT_ID,
        replay_generation=0,
        status=status,
        attempt_count=attempt_count,
        next_attempt_at=NOW,
        lease_owner="worker-1" if status == DeliveryStatus.IN_PROGRESS else None,
        lease_token=LEASE_TOKEN if status == DeliveryStatus.IN_PROGRESS else None,
        lease_expires_at=(NOW + timedelta(seconds=5))
        if status == DeliveryStatus.IN_PROGRESS
        else None,
        delivered_at=None,
        last_error=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _webhook(*, attempt_number: int = 1) -> OutboundWebhook:
    event = _event()
    endpoint = _endpoint()
    return OutboundWebhook(
        attempt_id=ATTEMPT_ID,
        delivery_id=DELIVERY_ID,
        event_id=EVENT_ID,
        event_type=event.event_type,
        endpoint_url=endpoint.target_url,
        signing_secret=endpoint.signing_secret,
        payload_bytes=event.payload_bytes,
        attempt_number=attempt_number,
        lease_token=LEASE_TOKEN,
    )


def _attempt(
    *,
    attempt_number: int = 1,
    status: DeliveryAttemptStatus = DeliveryAttemptStatus.IN_PROGRESS,
    lease_token: UUID = LEASE_TOKEN,
    attempt_id: UUID = ATTEMPT_ID,
) -> DeliveryAttempt:
    resolved = NOW if status != DeliveryAttemptStatus.IN_PROGRESS else None
    completed = status == DeliveryAttemptStatus.COMPLETED
    return DeliveryAttempt(
        id=attempt_id,
        delivery_id=DELIVERY_ID,
        attempt_number=attempt_number,
        status=status,
        lease_token=lease_token,
        disposition=DeliveryDisposition.SUCCEEDED if completed else None,
        http_status_code=200 if completed else None,
        error_type="lease_expired" if status == DeliveryAttemptStatus.INDETERMINATE else None,
        error_message=None,
        response_body_excerpt="ok" if completed else None,
        duration_ms=25 if completed else None,
        request_timestamp=int(NOW.timestamp()) if completed else None,
        retry_scheduled_for=None,
        started_at=NOW if completed else None,
        finished_at=NOW if completed else None,
        resolved_at=resolved,
        created_at=NOW,
    )


def _result(
    disposition: DeliveryDisposition,
    *,
    status_code: int | None = None,
    retry_after_seconds: float | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> AttemptResult:
    return AttemptResult(
        disposition=disposition,
        http_status_code=status_code,
        error_type=error_type,
        error_message=error_message,
        response_body_excerpt="receiver response" if status_code is not None else None,
        request_timestamp=int(NOW.timestamp()),
        retry_after_seconds=retry_after_seconds,
        started_at=NOW,
        finished_at=NOW + timedelta(milliseconds=25),
        duration_ms=25,
    )


def _worker(session: _FakeSession, **setting_overrides: Any) -> DeliveryWorker:
    return DeliveryWorker(
        _SessionFactory(session),  # type: ignore[arg-type]
        AsyncMock(spec=httpx.AsyncClient),
        _settings(**setting_overrides),
        worker_id="worker-1",
        random_fraction=lambda: 0.5,
    )


@pytest.mark.asyncio
async def test_claim_one_leases_due_delivery_and_returns_immutable_snapshot() -> None:
    delivery = _delivery(status=DeliveryStatus.PENDING, attempt_count=0)
    event = _event()
    event.payload["run_id"] = "run-1"
    endpoint = _endpoint()
    session = _FakeSession(delivery, related={Event: event, Endpoint: endpoint})
    worker = _worker(session)

    claimed = await worker.claim_one()

    assert isinstance(claimed, OutboundWebhook)
    assert claimed.delivery_id == delivery.id
    assert claimed.event_id == event.id
    assert claimed.endpoint_url == endpoint.target_url
    assert claimed.payload_bytes == event.payload_bytes
    assert claimed.demo_run_id == "run-1"
    assert claimed.attempt_number == 1
    assert delivery.status == DeliveryStatus.IN_PROGRESS
    assert delivery.attempt_count == 1
    assert delivery.lease_owner == "worker-1"
    assert delivery.lease_token == claimed.lease_token
    assert delivery.lease_expires_at == NOW + timedelta(seconds=5)
    assert delivery.next_attempt_at is None
    [reserved] = session.added
    assert isinstance(reserved, DeliveryAttempt)
    assert reserved.id == claimed.attempt_id
    assert reserved.status == DeliveryAttemptStatus.IN_PROGRESS
    assert reserved.lease_token == claimed.lease_token
    assert reserved.disposition is None
    assert session.flush_count == 1


@pytest.mark.asyncio
async def test_claim_one_omits_unsafe_demo_run_identifier() -> None:
    delivery = _delivery(status=DeliveryStatus.PENDING, attempt_count=0)
    event = _event()
    event.payload["run_id"] = "not safe"
    endpoint = _endpoint()
    session = _FakeSession(delivery, related={Event: event, Endpoint: endpoint})

    claimed = await _worker(session).claim_one()

    assert isinstance(claimed, OutboundWebhook)
    assert claimed.demo_run_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "attempt_count", "endpoint_enabled", "max_attempts", "expected_reason"),
    [
        (DeliveryStatus.PENDING, 0, False, 3, "endpoint is disabled"),
        (DeliveryStatus.RETRY_WAIT, 3, True, 3, "maximum attempt count"),
        (DeliveryStatus.RETRY_WAIT, 4, True, 3, "maximum attempt count"),
    ],
)
async def test_due_unclaimable_delivery_is_dead_lettered_without_an_attempt(
    status: DeliveryStatus,
    attempt_count: int,
    endpoint_enabled: bool,
    max_attempts: int,
    expected_reason: str,
) -> None:
    delivery = _delivery(status=status, attempt_count=attempt_count)
    session = _FakeSession(
        delivery,
        related={Endpoint: _endpoint(enabled=endpoint_enabled)},
    )
    worker = _worker(session, worker_max_attempts=max_attempts)

    resolution = await worker.claim_one()

    assert isinstance(resolution, DeliveryResolution)
    assert expected_reason in resolution.reason
    assert delivery.status == DeliveryStatus.DEAD_LETTERED
    assert delivery.attempt_count == attempt_count
    assert delivery.next_attempt_at is None
    assert expected_reason in (delivery.last_error or "")
    assert session.added == []
    assert session.flush_count == 1


@pytest.mark.asyncio
async def test_claim_one_recovers_expired_lease_and_fences_previous_owner() -> None:
    delivery = _delivery(status=DeliveryStatus.IN_PROGRESS, attempt_count=1)
    delivery.lease_expires_at = NOW - timedelta(seconds=1)
    event = _event()
    endpoint = _endpoint()
    previous_attempt = _attempt()
    claim_session = _FakeSession(
        delivery,
        related={Event: event, Endpoint: endpoint},
        attempts=[previous_attempt],
    )
    finalize_session = _FakeSession(delivery)
    worker = DeliveryWorker(
        _SessionFactory(claim_session, finalize_session),  # type: ignore[arg-type]
        AsyncMock(spec=httpx.AsyncClient),
        _settings(),
        worker_id="worker-1",
        random_fraction=lambda: 0.5,
    )
    previous_webhook = _webhook(attempt_number=1)

    reclaimed = await worker.claim_one()
    stale_result_was_finalized = await worker.finalize(
        previous_webhook,
        _result(DeliveryDisposition.SUCCEEDED, status_code=200),
    )

    assert isinstance(reclaimed, OutboundWebhook)
    assert reclaimed.attempt_number == 2
    assert reclaimed.lease_token != previous_webhook.lease_token
    assert delivery.attempt_count == 2
    assert delivery.lease_owner == "worker-1"
    assert delivery.lease_token == reclaimed.lease_token
    assert delivery.lease_expires_at == NOW + timedelta(seconds=5)
    assert previous_attempt.status == DeliveryAttemptStatus.INDETERMINATE
    assert previous_attempt.error_type == "lease_expired"
    assert previous_attempt.resolved_at == NOW
    [replacement] = claim_session.added
    assert isinstance(replacement, DeliveryAttempt)
    assert replacement.attempt_number == 2
    assert replacement.status == DeliveryAttemptStatus.IN_PROGRESS
    assert stale_result_was_finalized is False
    assert finalize_session.added == []


@pytest.mark.asyncio
async def test_expired_final_attempt_is_indeterminate_and_dead_lettered_without_reclaim() -> None:
    delivery = _delivery(status=DeliveryStatus.IN_PROGRESS, attempt_count=3)
    delivery.lease_expires_at = NOW - timedelta(seconds=1)
    attempt = _attempt(attempt_number=3)
    session = _FakeSession(
        delivery,
        related={Endpoint: _endpoint()},
        attempts=[attempt],
    )
    worker = _worker(session, worker_max_attempts=3)

    resolution = await worker.claim_one()

    assert isinstance(resolution, DeliveryResolution)
    assert delivery.status == DeliveryStatus.DEAD_LETTERED
    assert delivery.attempt_count == 3
    assert delivery.lease_token is None
    assert attempt.status == DeliveryAttemptStatus.INDETERMINATE
    assert "maximum attempt count" in (attempt.error_message or "")
    assert session.added == []


@pytest.mark.asyncio
async def test_expired_lease_for_disabled_endpoint_is_resolved_without_redelivery() -> None:
    delivery = _delivery(status=DeliveryStatus.IN_PROGRESS, attempt_count=1)
    delivery.lease_expires_at = NOW - timedelta(seconds=1)
    attempt = _attempt()
    session = _FakeSession(
        delivery,
        related={Endpoint: _endpoint(enabled=False)},
        attempts=[attempt],
    )
    worker = _worker(session)

    resolution = await worker.claim_one()

    assert isinstance(resolution, DeliveryResolution)
    assert delivery.status == DeliveryStatus.DEAD_LETTERED
    assert delivery.attempt_count == 1
    assert attempt.status == DeliveryAttemptStatus.INDETERMINATE
    assert "endpoint disabled" in (attempt.error_message or "")
    assert session.added == []


@pytest.mark.asyncio
async def test_expired_lease_without_reserved_attempt_raises_invariant_error() -> None:
    delivery = _delivery(status=DeliveryStatus.IN_PROGRESS, attempt_count=1)
    delivery.lease_expires_at = NOW - timedelta(seconds=1)
    session = _FakeSession(
        delivery,
        related={Endpoint: _endpoint()},
        attempts=[None],
    )
    worker = _worker(session)

    with pytest.raises(RuntimeError, match="without in-progress evidence"):
        await worker.claim_one()

    assert delivery.status == DeliveryStatus.IN_PROGRESS
    assert delivery.attempt_count == 1


@pytest.mark.asyncio
async def test_success_finalization_persists_attempt_and_marks_delivery_delivered() -> None:
    delivery = _delivery()
    attempt = _attempt()
    session = _FakeSession(delivery, attempts=[attempt])
    worker = _worker(session)
    result = _result(DeliveryDisposition.SUCCEEDED, status_code=202)

    finalized = await worker.finalize(_webhook(), result)

    assert finalized is True
    assert delivery.status == DeliveryStatus.DELIVERED
    assert delivery.delivered_at == NOW
    assert delivery.next_attempt_at is None
    assert delivery.last_error is None
    assert delivery.lease_owner is None
    assert delivery.lease_token is None
    assert delivery.lease_expires_at is None
    assert session.flush_count == 1
    assert session.added == []
    assert attempt.status == DeliveryAttemptStatus.COMPLETED
    assert attempt.disposition == DeliveryDisposition.SUCCEEDED
    assert attempt.attempt_number == 1
    assert attempt.retry_scheduled_for is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_after_seconds", "base_delay", "max_delay"),
    [(12, 10, 60), (5, 1, 2)],
)
async def test_retry_finalization_uses_larger_of_full_jitter_and_retry_after(
    retry_after_seconds: int, base_delay: int, max_delay: int
) -> None:
    delivery = _delivery()
    attempt = _attempt()
    session = _FakeSession(delivery, attempts=[attempt])
    worker = _worker(
        session,
        worker_base_delay_seconds=base_delay,
        worker_max_delay_seconds=max_delay,
    )
    result = _result(
        DeliveryDisposition.RETRY,
        status_code=429,
        retry_after_seconds=retry_after_seconds,
    )

    finalized = await worker.finalize(_webhook(), result)

    expected_retry_at = NOW + timedelta(seconds=retry_after_seconds)
    assert finalized is True
    assert delivery.status == DeliveryStatus.RETRY_WAIT
    assert delivery.next_attempt_at == expected_retry_at
    assert delivery.delivered_at is None
    assert delivery.last_error == "HTTP 429"
    assert attempt.status == DeliveryAttemptStatus.COMPLETED
    assert attempt.retry_scheduled_for == expected_retry_at


@pytest.mark.asyncio
async def test_retry_on_last_allowed_attempt_is_dead_lettered() -> None:
    delivery = _delivery(attempt_count=3)
    attempt = _attempt(attempt_number=3)
    session = _FakeSession(delivery, attempts=[attempt])
    worker = _worker(session, worker_max_attempts=3)
    result = _result(
        DeliveryDisposition.RETRY,
        error_type="timeout",
        error_message="receiver did not answer",
    )

    finalized = await worker.finalize(_webhook(attempt_number=3), result)

    assert finalized is True
    assert delivery.status == DeliveryStatus.DEAD_LETTERED
    assert delivery.next_attempt_at is None
    assert delivery.last_error == "timeout: receiver did not answer"
    assert attempt.status == DeliveryAttemptStatus.COMPLETED
    assert attempt.retry_scheduled_for is None


@pytest.mark.asyncio
async def test_terminal_http_failure_is_dead_lettered_without_retry() -> None:
    delivery = _delivery()
    attempt = _attempt()
    session = _FakeSession(delivery, attempts=[attempt])
    worker = _worker(session)
    result = _result(DeliveryDisposition.TERMINAL_FAILURE, status_code=400)

    finalized = await worker.finalize(_webhook(), result)

    assert finalized is True
    assert delivery.status == DeliveryStatus.DEAD_LETTERED
    assert delivery.next_attempt_at is None
    assert delivery.last_error == "HTTP 400"


@pytest.mark.asyncio
async def test_stale_lease_result_is_ignored_without_attempt_evidence() -> None:
    session = _FakeSession(None)
    worker = _worker(session)

    finalized = await worker.finalize(
        _webhook(),
        _result(DeliveryDisposition.SUCCEEDED, status_code=200),
    )

    assert finalized is False
    assert session.added == []
    assert session.flush_count == 0


@pytest.mark.asyncio
async def test_owned_lease_without_exact_attempt_evidence_raises_and_preserves_delivery() -> None:
    delivery = _delivery()
    session = _FakeSession(delivery, attempts=[None])
    worker = _worker(session)

    with pytest.raises(RuntimeError, match="without matching attempt evidence"):
        await worker.finalize(
            _webhook(),
            _result(DeliveryDisposition.SUCCEEDED, status_code=200),
        )

    assert delivery.status == DeliveryStatus.IN_PROGRESS
    assert delivery.lease_token == LEASE_TOKEN
    assert session.flush_count == 0


@pytest.mark.asyncio
async def test_obsolete_attempt_number_is_ignored_as_an_extra_fence() -> None:
    delivery = _delivery(attempt_count=2)
    session = _FakeSession(delivery)
    worker = _worker(session)

    finalized = await worker.finalize(
        _webhook(attempt_number=1),
        _result(DeliveryDisposition.SUCCEEDED, status_code=200),
    )

    assert finalized is False
    assert delivery.status == DeliveryStatus.IN_PROGRESS
    assert session.added == []
    assert session.flush_count == 0


@pytest.mark.asyncio
async def test_run_once_returns_false_without_making_an_http_request_when_queue_is_empty() -> None:
    worker = _worker(_FakeSession(None))
    worker.claim_one = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with patch(
        "eventharbor.deliveries.worker.send_webhook",
        new=AsyncMock(),
    ) as send:
        worked = await worker.run_once()

    assert worked is False
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_once_reports_database_only_resolution_without_http() -> None:
    worker = _worker(_FakeSession(None))
    resolution = DeliveryResolution(DELIVERY_ID, "attempt budget exhausted")
    worker.claim_one = AsyncMock(return_value=resolution)  # type: ignore[method-assign]

    with patch(
        "eventharbor.deliveries.worker.send_webhook",
        new=AsyncMock(),
    ) as send:
        worked = await worker.run_once()

    assert worked is True
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_once_sends_then_finalizes_a_claimed_webhook() -> None:
    worker = _worker(_FakeSession(None))
    outbound = _webhook()
    result = _result(DeliveryDisposition.SUCCEEDED, status_code=204)
    worker.claim_one = AsyncMock(return_value=outbound)  # type: ignore[method-assign]
    worker.finalize = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with patch(
        "eventharbor.deliveries.worker.send_webhook",
        new=AsyncMock(return_value=result),
    ) as send:
        worked = await worker.run_once()

    assert worked is True
    send.assert_awaited_once_with(
        outbound,
        worker._client,  # noqa: SLF001 - verifies the orchestration boundary.
        retry_after_cap_seconds=30,
    )
    worker.finalize.assert_awaited_once_with(outbound, result)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_forever_logs_iteration_failure_then_retries_without_swallowing_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = _worker(_FakeSession(None), worker_poll_interval_seconds=0.25)
    worker.run_once = AsyncMock(  # type: ignore[method-assign]
        side_effect=[RuntimeError("temporary database failure"), asyncio.CancelledError()]
    )
    sleep = AsyncMock()

    with (
        patch("eventharbor.deliveries.worker.asyncio.sleep", new=sleep),
        caplog.at_level("ERROR", logger="eventharbor.deliveries.worker"),
        pytest.raises(asyncio.CancelledError),
    ):
        await worker.run_forever()

    assert worker.run_once.await_count == 2  # type: ignore[attr-defined]
    sleep.assert_awaited_once_with(0.25)
    assert "worker iteration failed; retrying after poll interval" in caplog.text


@pytest.mark.parametrize(
    ("lease_seconds", "timeout_seconds"),
    [(5, 5), (4, 5)],
)
def test_settings_require_lease_to_exceed_http_timeout(
    lease_seconds: float,
    timeout_seconds: float,
) -> None:
    with pytest.raises(
        ValidationError,
        match="worker_lease_seconds must exceed worker_http_timeout_seconds",
    ):
        _settings(
            worker_lease_seconds=lease_seconds,
            worker_http_timeout_seconds=timeout_seconds,
        )


def test_settings_reject_backoff_cap_below_base_delay() -> None:
    with pytest.raises(
        ValidationError,
        match="worker_max_delay_seconds must be at least worker_base_delay_seconds",
    ):
        _settings(
            worker_base_delay_seconds=20,
            worker_max_delay_seconds=10,
        )
