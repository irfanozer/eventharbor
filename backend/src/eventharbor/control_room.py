"""Bounded read models for the recruiter-facing Control Room."""

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from eventharbor.deliveries.state_machine import DeliveryStatus
from eventharbor.errors import DomainError
from eventharbor.models import Delivery, DeliveryAttempt, Endpoint, Event

PageItem = TypeVar("PageItem")


@dataclass(frozen=True, slots=True)
class PageCursor:
    """Stable descending keyset cursor shared by bounded list endpoints."""

    timestamp: datetime
    row_id: UUID


@dataclass(frozen=True, slots=True)
class Page[PageItem]:
    items: list[PageItem]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class OverviewSnapshot:
    generated_at: datetime
    events_total: int
    endpoints_total: int
    endpoints_enabled: int
    deliveries_by_status: dict[DeliveryStatus, int]
    actionable_dead_letters: int


@dataclass(frozen=True, slots=True)
class EventListRecord:
    event: Event
    latest_delivery: Delivery
    endpoint: Endpoint


@dataclass(frozen=True, slots=True)
class DeadLetterRecord:
    event: Event
    delivery: Delivery
    endpoint: Endpoint
    last_http_status_code: int | None = None
    last_response_body_excerpt: str | None = None


def encode_cursor(cursor: PageCursor) -> str:
    """Encode a non-secret cursor without leaking query implementation details."""

    payload = json.dumps(
        {
            "v": 1,
            "timestamp": cursor.timestamp.astimezone(UTC).isoformat(),
            "id": str(cursor.row_id),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(value: str | None) -> PageCursor | None:
    """Decode and validate an opaque cursor into strongly typed sort keys."""

    if value is None:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding)
        payload = json.loads(decoded.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ValueError("unsupported cursor version")
        timestamp_value = payload["timestamp"]
        row_id_value = payload["id"]
        if not isinstance(timestamp_value, str) or not isinstance(row_id_value, str):
            raise ValueError("cursor keys must be strings")
        timestamp = datetime.fromisoformat(timestamp_value)
        if timestamp.tzinfo is None:
            raise ValueError("cursor timestamp must include a timezone")
        return PageCursor(timestamp=timestamp, row_id=UUID(row_id_value))
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DomainError(
            status_code=422,
            code="invalid_cursor",
            title="Cursor is invalid",
            detail="The pagination cursor is malformed or uses an unsupported version.",
        ) from exc


def _latest_generation_condition(
    delivery_alias: type[Delivery],
) -> ColumnElement[bool]:
    newer = aliased(Delivery)
    return ~exists(
        select(newer.id).where(
            newer.event_id == delivery_alias.event_id,
            newer.endpoint_id == delivery_alias.endpoint_id,
            newer.replay_generation > delivery_alias.replay_generation,
        )
    )


class ControlRoomRepository:
    """PostgreSQL queries for dashboard aggregates and bounded collection reads."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def overview(self) -> OverviewSnapshot:
        generated_at = await self._session.scalar(select(func.clock_timestamp()))
        events_total = await self._session.scalar(select(func.count(Event.id)))
        endpoint_counts = (
            await self._session.execute(
                select(
                    func.count(Endpoint.id),
                    func.count(Endpoint.id).filter(Endpoint.enabled.is_(True)),
                )
            )
        ).one()
        status_rows = (
            await self._session.execute(
                select(Delivery.status, func.count(Delivery.id))
                .where(_latest_generation_condition(Delivery))
                .group_by(Delivery.status)
            )
        ).all()
        actionable_dead_letters = await self._session.scalar(
            select(func.count(Delivery.id)).where(
                Delivery.status == DeliveryStatus.DEAD_LETTERED,
                _latest_generation_condition(Delivery),
            )
        )
        if generated_at is None:
            raise RuntimeError("PostgreSQL did not return a dashboard timestamp")
        return OverviewSnapshot(
            generated_at=generated_at,
            events_total=int(events_total or 0),
            endpoints_total=int(endpoint_counts[0] or 0),
            endpoints_enabled=int(endpoint_counts[1] or 0),
            deliveries_by_status={status: int(count) for status, count in status_rows},
            actionable_dead_letters=int(actionable_dead_letters or 0),
        )

    async def list_events(
        self,
        *,
        limit: int,
        cursor: PageCursor | None,
        event_type: str | None,
        endpoint_id: UUID | None,
        delivery_status: DeliveryStatus | None,
    ) -> Page[EventListRecord]:
        conditions: list[ColumnElement[bool]] = [_latest_generation_condition(Delivery)]
        if cursor is not None:
            conditions.append(
                or_(
                    Event.created_at < cursor.timestamp,
                    and_(Event.created_at == cursor.timestamp, Event.id < cursor.row_id),
                )
            )
        if event_type is not None:
            conditions.append(Event.event_type == event_type)
        if endpoint_id is not None:
            conditions.append(Delivery.endpoint_id == endpoint_id)
        if delivery_status is not None:
            conditions.append(Delivery.status == delivery_status)

        statement = (
            select(Event, Delivery, Endpoint)
            .join(Delivery, Delivery.event_id == Event.id)
            .join(Endpoint, Endpoint.id == Delivery.endpoint_id)
            .where(*conditions)
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(limit + 1)
        )
        rows = (await self._session.execute(statement)).all()
        visible = rows[:limit]
        records = [
            EventListRecord(event=event, latest_delivery=delivery, endpoint=endpoint)
            for event, delivery, endpoint in visible
        ]
        next_cursor = None
        if len(rows) > limit and records:
            last = records[-1].event
            next_cursor = encode_cursor(PageCursor(last.created_at, last.id))
        return Page(items=records, next_cursor=next_cursor)

    async def list_endpoints(
        self,
        *,
        limit: int,
        cursor: PageCursor | None,
    ) -> Page[Endpoint]:
        statement = select(Endpoint)
        if cursor is not None:
            statement = statement.where(
                or_(
                    Endpoint.created_at < cursor.timestamp,
                    and_(Endpoint.created_at == cursor.timestamp, Endpoint.id < cursor.row_id),
                )
            )
        statement = statement.order_by(Endpoint.created_at.desc(), Endpoint.id.desc()).limit(
            limit + 1
        )
        rows = list((await self._session.scalars(statement)).all())
        visible = rows[:limit]
        next_cursor = None
        if len(rows) > limit and visible:
            last = visible[-1]
            next_cursor = encode_cursor(PageCursor(last.created_at, last.id))
        return Page(items=visible, next_cursor=next_cursor)

    async def get_endpoint(self, endpoint_id: UUID) -> Endpoint | None:
        return await self._session.get(Endpoint, endpoint_id)

    async def list_dead_letters(
        self,
        *,
        limit: int,
        cursor: PageCursor | None,
        endpoint_id: UUID | None,
    ) -> Page[DeadLetterRecord]:
        conditions: list[ColumnElement[bool]] = [
            Delivery.status == DeliveryStatus.DEAD_LETTERED,
            _latest_generation_condition(Delivery),
        ]
        if cursor is not None:
            conditions.append(
                or_(
                    Delivery.updated_at < cursor.timestamp,
                    and_(Delivery.updated_at == cursor.timestamp, Delivery.id < cursor.row_id),
                )
            )
        if endpoint_id is not None:
            conditions.append(Delivery.endpoint_id == endpoint_id)
        last_http_status_code = (
            select(DeliveryAttempt.http_status_code)
            .where(DeliveryAttempt.delivery_id == Delivery.id)
            .order_by(DeliveryAttempt.attempt_number.desc())
            .limit(1)
            .correlate(Delivery)
            .scalar_subquery()
        )
        last_response_body_excerpt = (
            select(DeliveryAttempt.response_body_excerpt)
            .where(DeliveryAttempt.delivery_id == Delivery.id)
            .order_by(DeliveryAttempt.attempt_number.desc())
            .limit(1)
            .correlate(Delivery)
            .scalar_subquery()
        )
        statement = (
            select(
                Event,
                Delivery,
                Endpoint,
                last_http_status_code.label("last_http_status_code"),
                last_response_body_excerpt.label("last_response_body_excerpt"),
            )
            .join(Event, Event.id == Delivery.event_id)
            .join(Endpoint, Endpoint.id == Delivery.endpoint_id)
            .where(*conditions)
            .order_by(Delivery.updated_at.desc(), Delivery.id.desc())
            .limit(limit + 1)
        )
        rows = (await self._session.execute(statement)).all()
        visible = rows[:limit]
        records = [
            DeadLetterRecord(
                event=event,
                delivery=delivery,
                endpoint=endpoint,
                last_http_status_code=http_status_code,
                last_response_body_excerpt=response_body_excerpt,
            )
            for event, delivery, endpoint, http_status_code, response_body_excerpt in visible
        ]
        next_cursor = None
        if len(rows) > limit and records:
            last = records[-1].delivery
            next_cursor = encode_cursor(PageCursor(last.updated_at, last.id))
        return Page(items=records, next_cursor=next_cursor)


class ControlRoomQueryService:
    """Coordinate bounded Control Room reads without exposing secret fields."""

    def __init__(self, repository: ControlRoomRepository) -> None:
        self._repository = repository

    async def overview(self) -> OverviewSnapshot:
        return await self._repository.overview()

    async def events(
        self,
        *,
        limit: int,
        cursor: str | None,
        event_type: str | None,
        endpoint_id: UUID | None,
        delivery_status: DeliveryStatus | None,
    ) -> Page[EventListRecord]:
        return await self._repository.list_events(
            limit=limit,
            cursor=decode_cursor(cursor),
            event_type=event_type,
            endpoint_id=endpoint_id,
            delivery_status=delivery_status,
        )

    async def endpoints(self, *, limit: int, cursor: str | None) -> Page[Endpoint]:
        return await self._repository.list_endpoints(
            limit=limit,
            cursor=decode_cursor(cursor),
        )

    async def endpoint(self, endpoint_id: UUID) -> Endpoint:
        endpoint = await self._repository.get_endpoint(endpoint_id)
        if endpoint is None:
            raise DomainError(
                status_code=404,
                code="endpoint_not_found",
                title="Endpoint not found",
                detail=f"Endpoint {endpoint_id} does not exist.",
            )
        return endpoint

    async def dead_letters(
        self,
        *,
        limit: int,
        cursor: str | None,
        endpoint_id: UUID | None,
    ) -> Page[DeadLetterRecord]:
        return await self._repository.list_dead_letters(
            limit=limit,
            cursor=decode_cursor(cursor),
            endpoint_id=endpoint_id,
        )
