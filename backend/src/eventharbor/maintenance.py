"""Bounded retention cleanup for the synthetic public-demo event history."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventharbor.config import Settings, get_settings
from eventharbor.database import engine, session_factory
from eventharbor.deliveries.state_machine import DeliveryStatus
from eventharbor.models import Delivery, Event

logger = logging.getLogger(__name__)
MAINTENANCE_LOCK_ID = 4_815_162_342
ACTIVE_DELIVERY_STATUSES = (
    DeliveryStatus.PENDING,
    DeliveryStatus.IN_PROGRESS,
    DeliveryStatus.RETRY_WAIT,
)


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Summary of one bounded cleanup execution."""

    lock_acquired: bool
    deleted_events: int
    batches: int


async def _delete_terminal_event_batch(
    session: AsyncSession,
    *,
    cutoff: datetime,
    limit: int,
) -> int:
    """Delete one oldest batch only when every delivery is terminal."""

    has_active_delivery = exists(
        select(Delivery.id).where(
            Delivery.event_id == Event.id,
            Delivery.status.in_(ACTIVE_DELIVERY_STATUSES),
        )
    )
    candidate_statement = (
        select(Event.id)
        .where(Event.created_at < cutoff, ~has_active_delivery)
        .order_by(Event.created_at, Event.id)
        .limit(limit)
    )
    event_ids = list((await session.scalars(candidate_statement)).all())
    if not event_ids:
        return 0

    # Delivery attempts cascade from deliveries. Every generation for a candidate
    # event is removed in one statement, so replay self-references are removed as
    # one atomic set before the immutable event row is deleted.
    await session.execute(delete(Delivery).where(Delivery.event_id.in_(event_ids)))
    deleted = await session.scalars(
        delete(Event).where(Event.id.in_(event_ids)).returning(Event.id)
    )
    return len(deleted.all())


async def purge_expired_demo_events(
    sessions: async_sessionmaker[AsyncSession],
    *,
    cutoff: datetime,
    batch_size: int,
    max_events: int,
) -> CleanupResult:
    """Delete bounded terminal history while leaving active work untouched."""

    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    if batch_size < 1:
        raise ValueError("batch_size must be at least one")
    if max_events < 1:
        raise ValueError("max_events must be at least one")

    deleted_events = 0
    batches = 0
    async with sessions() as session, session.begin():
        lock_acquired = bool(
            await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
                {"lock_id": MAINTENANCE_LOCK_ID},
            )
        )
        if not lock_acquired:
            return CleanupResult(lock_acquired=False, deleted_events=0, batches=0)

        while deleted_events < max_events:
            limit = min(batch_size, max_events - deleted_events)
            deleted = await _delete_terminal_event_batch(
                session,
                cutoff=cutoff,
                limit=limit,
            )
            if deleted == 0:
                break
            deleted_events += deleted
            batches += 1

    return CleanupResult(
        lock_acquired=True,
        deleted_events=deleted_events,
        batches=batches,
    )


async def run_cleanup(settings: Settings | None = None) -> CleanupResult:
    """Run retention with the process configuration used by the scheduled job."""

    resolved = settings or get_settings()
    cutoff = datetime.now(UTC) - timedelta(days=resolved.retention_days)
    return await purge_expired_demo_events(
        session_factory,
        cutoff=cutoff,
        batch_size=resolved.retention_batch_size,
        max_events=resolved.retention_max_events_per_run,
    )


async def _main() -> None:
    result = await run_cleanup()
    logger.info(
        "retention cleanup finished",
        extra={
            "lock_acquired": result.lock_acquired,
            "deleted_events": result.deleted_events,
            "batches": result.batches,
        },
    )
    await engine.dispose()


def main() -> None:
    """Execute the scheduled retention command."""

    logging.basicConfig(level=get_settings().log_level.upper())
    asyncio.run(_main())


if __name__ == "__main__":
    main()
