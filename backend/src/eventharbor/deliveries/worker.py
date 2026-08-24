"""PostgreSQL-backed delivery worker and executable process entry point."""

import asyncio
import logging
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from random import SystemRandom
from uuid import UUID, uuid4

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventharbor.config import Settings, get_settings
from eventharbor.database import engine, session_factory
from eventharbor.deliveries.retry import DeliveryDisposition, RetryPolicy
from eventharbor.deliveries.state_machine import (
    DeliveryAttemptStatus,
    DeliveryStatus,
    ensure_transition,
)
from eventharbor.deliveries.transport import AttemptResult, OutboundWebhook, send_webhook
from eventharbor.demo_runs import normalized_demo_run_id
from eventharbor.models import Delivery, DeliveryAttempt, Endpoint, Event

logger = logging.getLogger(__name__)
_system_random = SystemRandom()


def _random_fraction() -> float:
    return _system_random.random()


@dataclass(frozen=True, slots=True)
class DeliveryResolution:
    """Database-only work completed without reserving another HTTP request."""

    delivery_id: UUID
    reason: str


class DeliveryWorker:
    """Coordinate short database transactions around untrusted network I/O."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
        settings: Settings,
        *,
        worker_id: str,
        random_fraction: Callable[[], float] = _random_fraction,
    ) -> None:
        self._sessions = sessions
        self._client = client
        self._settings = settings
        self._worker_id = worker_id[:160]
        self._random_fraction = random_fraction
        self._retry_policy = RetryPolicy(
            max_attempts=settings.worker_max_attempts,
            base_delay_seconds=settings.worker_base_delay_seconds,
            max_delay_seconds=settings.worker_max_delay_seconds,
        )

    async def claim_one(self) -> OutboundWebhook | DeliveryResolution | None:
        """Resolve or lease one delivery without holding a lock during HTTP."""

        async with self._sessions() as session, session.begin():
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            statement = (
                select(Delivery)
                .join(Endpoint, Endpoint.id == Delivery.endpoint_id)
                .where(
                    or_(
                        and_(
                            Delivery.status.in_(
                                [DeliveryStatus.PENDING, DeliveryStatus.RETRY_WAIT]
                            ),
                            Delivery.next_attempt_at.is_not(None),
                            Delivery.next_attempt_at <= func.clock_timestamp(),
                            Delivery.lease_token.is_(None),
                        ),
                        and_(
                            Delivery.status == DeliveryStatus.IN_PROGRESS,
                            Delivery.lease_expires_at.is_not(None),
                            Delivery.lease_expires_at <= func.clock_timestamp(),
                        ),
                    )
                )
                .order_by(
                    func.coalesce(
                        Delivery.lease_expires_at,
                        Delivery.next_attempt_at,
                    ),
                    Delivery.id,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            delivery = (await session.scalars(statement)).one_or_none()
            if delivery is None:
                return None

            endpoint = await session.get(Endpoint, delivery.endpoint_id)
            if endpoint is None:
                raise RuntimeError("delivery references missing endpoint")

            if delivery.status == DeliveryStatus.IN_PROGRESS:
                resolution = await self._resolve_expired_attempt(
                    session,
                    delivery,
                    endpoint_enabled=endpoint.enabled,
                    now=now,
                )
                if resolution is not None:
                    return resolution
                # Flush the old attempt's terminal status before inserting the new
                # in-progress row guarded by the partial unique index.
                await session.flush()
            else:
                reason = self._unclaimable_due_reason(delivery, endpoint_enabled=endpoint.enabled)
                if reason is not None:
                    return await self._dead_letter_without_attempt(
                        session,
                        delivery,
                        reason=reason,
                        now=now,
                    )
                ensure_transition(delivery.status, DeliveryStatus.IN_PROGRESS)

            lease_token = uuid4()
            attempt_id = uuid4()
            delivery.status = DeliveryStatus.IN_PROGRESS
            delivery.attempt_count += 1
            delivery.next_attempt_at = None
            delivery.lease_owner = self._worker_id
            delivery.lease_token = lease_token
            delivery.lease_expires_at = now + timedelta(seconds=self._settings.worker_lease_seconds)
            delivery.updated_at = now

            event = await session.get(Event, delivery.event_id)
            if event is None:
                raise RuntimeError("delivery references missing event")

            session.add(
                DeliveryAttempt(
                    id=attempt_id,
                    delivery_id=delivery.id,
                    attempt_number=delivery.attempt_count,
                    status=DeliveryAttemptStatus.IN_PROGRESS,
                    lease_token=lease_token,
                    disposition=None,
                    http_status_code=None,
                    error_type=None,
                    error_message=None,
                    response_body_excerpt=None,
                    duration_ms=None,
                    request_timestamp=None,
                    retry_scheduled_for=None,
                    started_at=None,
                    finished_at=None,
                    resolved_at=None,
                    created_at=now,
                )
            )
            await session.flush()

            return OutboundWebhook(
                attempt_id=attempt_id,
                delivery_id=delivery.id,
                event_id=event.id,
                event_type=event.event_type,
                endpoint_url=endpoint.target_url,
                signing_secret=endpoint.signing_secret,
                payload_bytes=event.payload_bytes,
                attempt_number=delivery.attempt_count,
                lease_token=lease_token,
                demo_run_id=normalized_demo_run_id(event.payload.get("run_id")),
            )

    def _unclaimable_due_reason(
        self,
        delivery: Delivery,
        *,
        endpoint_enabled: bool,
    ) -> str | None:
        """Explain why due work must terminate before another attempt is reserved."""

        exhausted = not self._retry_policy.has_attempt_remaining(delivery.attempt_count)
        if not endpoint_enabled and exhausted:
            return "endpoint is disabled and the maximum attempt count is already exhausted"
        if not endpoint_enabled:
            return "endpoint is disabled before the delivery can be claimed"
        if exhausted:
            return "maximum attempt count is already exhausted before claim"
        return None

    @staticmethod
    async def _dead_letter_without_attempt(
        session: AsyncSession,
        delivery: Delivery,
        *,
        reason: str,
        now: datetime,
    ) -> DeliveryResolution:
        """Terminate due work without implying that another HTTP attempt occurred."""

        ensure_transition(delivery.status, DeliveryStatus.DEAD_LETTERED)
        delivery.status = DeliveryStatus.DEAD_LETTERED
        delivery.next_attempt_at = None
        delivery.last_error = reason[:2_048]
        delivery.lease_owner = None
        delivery.lease_token = None
        delivery.lease_expires_at = None
        delivery.updated_at = now
        await session.flush()
        return DeliveryResolution(delivery_id=delivery.id, reason=reason)

    async def _resolve_expired_attempt(
        self,
        session: AsyncSession,
        delivery: Delivery,
        *,
        endpoint_enabled: bool,
        now: datetime,
    ) -> DeliveryResolution | None:
        """Resolve an expired claim and stop when it must not be redelivered."""

        statement = (
            select(DeliveryAttempt)
            .where(
                DeliveryAttempt.delivery_id == delivery.id,
                DeliveryAttempt.attempt_number == delivery.attempt_count,
                DeliveryAttempt.lease_token == delivery.lease_token,
                DeliveryAttempt.status == DeliveryAttemptStatus.IN_PROGRESS,
            )
            .with_for_update()
        )
        attempt = (await session.scalars(statement)).one_or_none()
        if attempt is None:
            raise RuntimeError(
                f"delivery {delivery.id} has an expired lease without in-progress evidence"
            )

        exhausted = not self._retry_policy.has_attempt_remaining(delivery.attempt_count)
        if not endpoint_enabled:
            reason = "endpoint disabled while the worker lease was outstanding"
        elif exhausted:
            reason = "worker lease expired and the maximum attempt count was exhausted"
        else:
            reason = "worker lease expired before its outcome was committed; redelivery reserved"

        attempt.status = DeliveryAttemptStatus.INDETERMINATE
        attempt.error_type = "lease_expired"
        attempt.error_message = (f"{reason}; the endpoint may have received the request")[:2_048]
        attempt.resolved_at = now
        delivery.last_error = f"lease_expired: {reason}"[:2_048]

        if endpoint_enabled and not exhausted:
            return None

        ensure_transition(delivery.status, DeliveryStatus.DEAD_LETTERED)
        delivery.status = DeliveryStatus.DEAD_LETTERED
        delivery.next_attempt_at = None
        delivery.lease_owner = None
        delivery.lease_token = None
        delivery.lease_expires_at = None
        delivery.updated_at = now
        await session.flush()
        return DeliveryResolution(delivery_id=delivery.id, reason=reason)

    async def finalize(self, webhook: OutboundWebhook, result: AttemptResult) -> bool:
        """Complete reserved attempt evidence only while this worker owns the lease."""

        retry_delay_seconds = None
        target_status = DeliveryStatus.DEAD_LETTERED
        if result.disposition == DeliveryDisposition.SUCCEEDED:
            target_status = DeliveryStatus.DELIVERED
        elif (
            result.disposition == DeliveryDisposition.RETRY
            and self._retry_policy.has_attempt_remaining(webhook.attempt_number)
        ):
            delay_seconds = self._retry_policy.delay_seconds(
                webhook.attempt_number,
                self._random_fraction(),
            )
            if result.retry_after_seconds is not None:
                delay_seconds = max(delay_seconds, result.retry_after_seconds)
            retry_delay_seconds = delay_seconds
            target_status = DeliveryStatus.RETRY_WAIT

        async with self._sessions() as session, session.begin():
            database_now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            statement = (
                select(Delivery)
                .where(
                    Delivery.id == webhook.delivery_id,
                    Delivery.status == DeliveryStatus.IN_PROGRESS,
                    Delivery.lease_owner == self._worker_id,
                    Delivery.lease_token == webhook.lease_token,
                )
                .with_for_update()
            )
            delivery = (await session.scalars(statement)).one_or_none()
            if delivery is None:
                logger.warning(
                    "ignored stale delivery result",
                    extra={"delivery_id": str(webhook.delivery_id)},
                )
                return False

            # The claim increments once and the immutable snapshot carries that number.
            # Comparing it is an additional fence if a lease is ever reclaimed.
            if delivery.attempt_count != webhook.attempt_number:
                logger.warning(
                    "ignored result for an obsolete attempt",
                    extra={"delivery_id": str(webhook.delivery_id)},
                )
                return False

            attempt_statement = (
                select(DeliveryAttempt)
                .where(
                    DeliveryAttempt.id == webhook.attempt_id,
                    DeliveryAttempt.delivery_id == webhook.delivery_id,
                    DeliveryAttempt.attempt_number == webhook.attempt_number,
                    DeliveryAttempt.lease_token == webhook.lease_token,
                    DeliveryAttempt.status == DeliveryAttemptStatus.IN_PROGRESS,
                )
                .with_for_update()
            )
            attempt = (await session.scalars(attempt_statement)).one_or_none()
            if attempt is None:
                raise RuntimeError(
                    f"delivery {delivery.id} owns a lease without matching attempt evidence"
                )

            ensure_transition(delivery.status, target_status)
            retry_scheduled_for = (
                database_now + timedelta(seconds=retry_delay_seconds)
                if retry_delay_seconds is not None
                else None
            )
            attempt.status = DeliveryAttemptStatus.COMPLETED
            attempt.disposition = result.disposition
            attempt.http_status_code = result.http_status_code
            attempt.error_type = result.error_type
            attempt.error_message = result.error_message
            attempt.response_body_excerpt = result.response_body_excerpt
            attempt.duration_ms = result.duration_ms
            attempt.request_timestamp = result.request_timestamp
            attempt.retry_scheduled_for = retry_scheduled_for
            attempt.started_at = result.started_at
            attempt.finished_at = result.finished_at
            attempt.resolved_at = database_now

            delivery.status = target_status
            delivery.next_attempt_at = retry_scheduled_for
            delivery.delivered_at = (
                database_now if target_status == DeliveryStatus.DELIVERED else None
            )
            delivery.last_error = self._last_error(result)
            delivery.lease_owner = None
            delivery.lease_token = None
            delivery.lease_expires_at = None
            delivery.updated_at = database_now
            await session.flush()
            return True

    @staticmethod
    def _last_error(result: AttemptResult) -> str | None:
        if result.disposition == DeliveryDisposition.SUCCEEDED:
            return None
        if result.error_type is not None:
            detail = result.error_message or "outbound request failed"
            return f"{result.error_type}: {detail}"[:2_048]
        if result.http_status_code is not None:
            return f"HTTP {result.http_status_code}"
        return "outbound request failed"

    async def run_once(self) -> bool:
        """Process at most one delivery and report whether work was claimed."""

        claim = await self.claim_one()
        if claim is None:
            return False
        if isinstance(claim, DeliveryResolution):
            logger.info(
                "resolved delivery without outbound request",
                extra={"delivery_id": str(claim.delivery_id), "reason": claim.reason},
            )
            return True
        result = await send_webhook(
            claim,
            self._client,
            retry_after_cap_seconds=self._settings.worker_retry_after_cap_seconds,
        )
        await self.finalize(claim, result)
        return True

    async def run_forever(self) -> None:
        """Poll until the process is cancelled by its supervisor."""

        while True:
            try:
                worked = await self.run_once()
            except Exception:
                logger.exception("worker iteration failed; retrying after poll interval")
                await asyncio.sleep(self._settings.worker_poll_interval_seconds)
                continue
            if not worked:
                await asyncio.sleep(self._settings.worker_poll_interval_seconds)


def _worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid4()}"


async def _run_process() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    timeout = httpx.Timeout(settings.worker_http_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        worker = DeliveryWorker(
            session_factory,
            client,
            settings,
            worker_id=_worker_id(),
        )
        try:
            await worker.run_forever()
        finally:
            await engine.dispose()


def main() -> None:
    """Run the worker as ``python -m eventharbor.deliveries.worker``."""

    asyncio.run(_run_process())


if __name__ == "__main__":
    main()
