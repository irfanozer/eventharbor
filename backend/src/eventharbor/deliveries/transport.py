"""Outbound HTTP transport for one signed webhook attempt."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from time import perf_counter
from uuid import UUID

import httpx

from eventharbor import __version__
from eventharbor.deliveries.retry import DeliveryDisposition, classify_status_code
from eventharbor.deliveries.signing import sign_payload

RESPONSE_BODY_EXCERPT_BYTES = 2_048
ERROR_MESSAGE_CHARACTERS = 1_024


@dataclass(frozen=True, slots=True)
class OutboundWebhook:
    """Immutable values claimed from PostgreSQL before network I/O begins."""

    attempt_id: UUID
    delivery_id: UUID
    event_id: UUID
    event_type: str
    endpoint_url: str
    signing_secret: bytes
    payload_bytes: bytes
    attempt_number: int
    lease_token: UUID


@dataclass(frozen=True, slots=True)
class AttemptResult:
    """Bounded evidence returned by the transport to the worker service."""

    disposition: DeliveryDisposition
    http_status_code: int | None
    error_type: str | None
    error_message: str | None
    response_body_excerpt: str | None
    request_timestamp: int
    retry_after_seconds: float | None
    started_at: datetime
    finished_at: datetime
    duration_ms: int


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_retry_after(
    value: str | None,
    cap_seconds: float,
    reference_time: datetime,
) -> float | None:
    """Parse Retry-After delta-seconds or an HTTP date and cap hostile values."""

    if value is None:
        return None
    stripped_value = value.strip()
    try:
        seconds = float(stripped_value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(stripped_value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at.astimezone(UTC) - reference_time.astimezone(UTC)).total_seconds()
        return min(max(seconds, 0.0), cap_seconds)
    if not isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, cap_seconds)


def _bounded_error_message(error: Exception) -> str:
    message = str(error)
    return message[:ERROR_MESSAGE_CHARACTERS]


async def send_webhook(
    webhook: OutboundWebhook,
    client: httpx.AsyncClient,
    *,
    retry_after_cap_seconds: float,
    clock: Callable[[], datetime] = _now,
    monotonic_clock: Callable[[], float] = perf_counter,
) -> AttemptResult:
    """Send one signed request without reading or writing the database."""

    started_at = clock()
    request_timestamp = int(started_at.timestamp())
    signature = sign_payload(
        webhook.signing_secret,
        request_timestamp,
        webhook.payload_bytes,
    )
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"EventHarbor/{__version__}",
        "X-EventHarbor-Event-Id": str(webhook.event_id),
        "X-EventHarbor-Delivery-Id": str(webhook.delivery_id),
        "X-EventHarbor-Event-Type": webhook.event_type,
        "X-EventHarbor-Attempt": str(webhook.attempt_number),
        "X-EventHarbor-Timestamp": str(request_timestamp),
        "X-EventHarbor-Signature": signature,
    }
    started_monotonic = monotonic_clock()

    try:
        response = await client.post(
            webhook.endpoint_url,
            content=webhook.payload_bytes,
            headers=headers,
        )
    except httpx.TimeoutException as exc:
        finished_at = clock()
        return AttemptResult(
            disposition=DeliveryDisposition.RETRY,
            http_status_code=None,
            error_type="timeout",
            error_message=_bounded_error_message(exc),
            response_body_excerpt=None,
            request_timestamp=request_timestamp,
            retry_after_seconds=None,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=max(0, round((monotonic_clock() - started_monotonic) * 1_000)),
        )
    except httpx.RequestError as exc:
        finished_at = clock()
        return AttemptResult(
            disposition=DeliveryDisposition.RETRY,
            http_status_code=None,
            error_type="network_error",
            error_message=_bounded_error_message(exc),
            response_body_excerpt=None,
            request_timestamp=request_timestamp,
            retry_after_seconds=None,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=max(0, round((monotonic_clock() - started_monotonic) * 1_000)),
        )

    finished_at = clock()
    disposition = classify_status_code(response.status_code)
    retry_after = None
    if disposition == DeliveryDisposition.RETRY:
        retry_after = _parse_retry_after(
            response.headers.get("Retry-After"),
            retry_after_cap_seconds,
            finished_at,
        )
    excerpt = response.content[:RESPONSE_BODY_EXCERPT_BYTES].decode("utf-8", errors="replace")
    return AttemptResult(
        disposition=disposition,
        http_status_code=response.status_code,
        error_type=None,
        error_message=None,
        response_body_excerpt=excerpt,
        request_timestamp=request_timestamp,
        retry_after_seconds=retry_after,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=max(0, round((monotonic_clock() - started_monotonic) * 1_000)),
    )
