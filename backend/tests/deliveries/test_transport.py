"""Tests for the signed outbound HTTP boundary."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from eventharbor.deliveries.retry import DeliveryDisposition
from eventharbor.deliveries.signing import verify_signature
from eventharbor.deliveries.transport import OutboundWebhook, send_webhook


def webhook(*, demo_run_id: str | None = None) -> OutboundWebhook:
    return OutboundWebhook(
        attempt_id=UUID("00000000-0000-0000-0000-000000000004"),
        delivery_id=UUID("00000000-0000-0000-0000-000000000002"),
        event_id=UUID("00000000-0000-0000-0000-000000000001"),
        event_type="order.shipped",
        endpoint_url="https://receiver.example/webhooks",
        signing_secret=b"local-test-secret",
        payload_bytes=b'{"data":{"order_id":"123"},"type":"order.shipped"}',
        attempt_number=1,
        lease_token=UUID("00000000-0000-0000-0000-000000000003"),
        demo_run_id=demo_run_id,
    )


def clock(values: list[datetime]) -> Callable[[], datetime]:
    iterator: Iterator[datetime] = iter(values)
    return lambda: next(iterator)


def monotonic(values: list[float]) -> Callable[[], float]:
    iterator: Iterator[float] = iter(values)
    return lambda: next(iterator)


@pytest.mark.asyncio
async def test_success_sends_exact_bytes_and_verifiable_signature() -> None:
    outbound = webhook(demo_run_id="run-1")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.content == outbound.payload_bytes
        assert request.headers["X-EventHarbor-Event-Id"] == str(outbound.event_id)
        assert request.headers["X-EventHarbor-Delivery-Id"] == str(outbound.delivery_id)
        assert request.headers["X-EventHarbor-Demo-Run-Id"] == "run-1"
        timestamp = int(request.headers["X-EventHarbor-Timestamp"])
        assert verify_signature(
            outbound.signing_secret,
            timestamp,
            request.content,
            request.headers["X-EventHarbor-Signature"],
        )
        return httpx.Response(200, content=b"accepted")

    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await send_webhook(
            outbound,
            client,
            retry_after_cap_seconds=60,
            clock=clock([now, now]),
            monotonic_clock=monotonic([10.0, 10.025]),
        )

    assert result.disposition == DeliveryDisposition.SUCCEEDED
    assert result.http_status_code == 200
    assert result.response_body_excerpt == "accepted"
    assert result.duration_ms == 25


@pytest.mark.asyncio
async def test_non_demo_webhook_omits_internal_run_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "X-EventHarbor-Demo-Run-Id" not in request.headers
        return httpx.Response(200)

    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await send_webhook(
            webhook(),
            client,
            retry_after_cap_seconds=60,
            clock=clock([now, now]),
            monotonic_clock=monotonic([10.0, 10.001]),
        )

    assert result.disposition == DeliveryDisposition.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize(("requested", "cap", "expected"), [(120, 30, 30), (5, 5, 5)])
async def test_rate_limit_is_retryable_and_retry_after_is_capped(
    requested: int, cap: int, expected: int
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": str(requested)})

    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await send_webhook(
            webhook(),
            client,
            retry_after_cap_seconds=cap,
            clock=clock([now, now]),
            monotonic_clock=monotonic([1.0, 1.001]),
        )

    assert result.disposition == DeliveryDisposition.RETRY
    assert result.http_status_code == 429
    assert result.retry_after_seconds == expected


@pytest.mark.asyncio
async def test_retry_after_http_date_uses_response_time_as_reference() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "Sun, 23 Aug 2026 12:00:25 GMT"})

    started_at = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    finished_at = datetime(2026, 8, 23, 12, 0, 5, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await send_webhook(
            webhook(),
            client,
            retry_after_cap_seconds=60,
            clock=clock([started_at, finished_at]),
            monotonic_clock=monotonic([1.0, 1.001]),
        )

    assert result.disposition == DeliveryDisposition.RETRY
    assert result.retry_after_seconds == 20


@pytest.mark.asyncio
async def test_past_retry_after_http_date_means_retry_immediately() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "Sun, 23 Aug 2026 11:59:59 GMT"})

    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await send_webhook(
            webhook(),
            client,
            retry_after_cap_seconds=30,
            clock=clock([now, now]),
            monotonic_clock=monotonic([1.0, 1.001]),
        )

    assert result.retry_after_seconds == 0


@pytest.mark.asyncio
async def test_non_finite_retry_after_is_ignored() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "NaN"})

    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await send_webhook(
            webhook(),
            client,
            retry_after_cap_seconds=30,
            clock=clock([now, now]),
            monotonic_clock=monotonic([1.0, 1.001]),
        )

    assert result.disposition == DeliveryDisposition.RETRY
    assert result.retry_after_seconds is None


@pytest.mark.asyncio
async def test_permanent_client_failure_is_terminal() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "code": "missing_customer_id",
                "detail": "data.customer_id is required.",
            },
        )

    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await send_webhook(
            webhook(),
            client,
            retry_after_cap_seconds=60,
            clock=clock([now, now]),
            monotonic_clock=monotonic([1.0, 1.0]),
        )

    assert result.disposition == DeliveryDisposition.TERMINAL_FAILURE
    assert result.http_status_code == 400
    assert result.response_body_excerpt == (
        '{"code":"missing_customer_id","detail":"data.customer_id is required."}'
    )


@pytest.mark.asyncio
async def test_timeout_becomes_retry_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("receiver did not answer", request=request)

    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await send_webhook(
            webhook(),
            client,
            retry_after_cap_seconds=60,
            clock=clock([now, now]),
            monotonic_clock=monotonic([1.0, 1.25]),
        )

    assert result.disposition == DeliveryDisposition.RETRY
    assert result.http_status_code is None
    assert result.error_type == "timeout"
    assert result.duration_ms == 250
