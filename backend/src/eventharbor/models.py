"""Authoritative persistence models for the first EventHarbor vertical slice."""

from collections.abc import Callable
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import (
    Enum as SqlEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from eventharbor.database import Base
from eventharbor.deliveries.retry import DeliveryDisposition
from eventharbor.deliveries.state_machine import DeliveryAttemptStatus, DeliveryStatus


def _enum_values(enum_class: type[Enum]) -> list[str]:
    """Persist StrEnum values rather than Python member names."""

    return [str(member.value) for member in enum_class]


enum_values: Callable[[type[Enum]], list[str]] = _enum_values


class Endpoint(Base):
    """A destination that can receive signed outbound webhooks."""

    __tablename__ = "endpoints"
    __table_args__ = (
        CheckConstraint("length(name) > 0", name="name_not_empty"),
        CheckConstraint("length(target_url) > 0", name="target_url_not_empty"),
        CheckConstraint("octet_length(signing_secret) > 0", name="signing_secret_not_empty"),
        CheckConstraint("secret_version >= 1", name="secret_version_positive"),
        Index("ix_endpoints_enabled_created_at", "enabled", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120))
    target_url: Mapped[str] = mapped_column(Text)
    # Local milestone only. Replace with an encrypted secret reference before deployment.
    signing_secret: Mapped[bytes] = mapped_column(LargeBinary)
    secret_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    deliveries: Mapped[list["Delivery"]] = relationship(
        back_populates="endpoint", passive_deletes=True
    )


class Event(Base):
    """An immutable business fact accepted idempotently from one source."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("source", "idempotency_key"),
        CheckConstraint("length(source) > 0", name="source_not_empty"),
        CheckConstraint("length(event_type) > 0", name="event_type_not_empty"),
        CheckConstraint("length(idempotency_key) > 0", name="idempotency_key_not_empty"),
        CheckConstraint("octet_length(payload_bytes) > 0", name="payload_bytes_not_empty"),
        CheckConstraint(
            "length(request_fingerprint_sha256) = 64",
            name="request_fingerprint_sha256_length",
        ),
        CheckConstraint("length(payload_sha256) = 64", name="payload_sha256_length"),
        Index("ix_events_event_type_created_at", "event_type", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source: Mapped[str] = mapped_column(String(120))
    event_type: Mapped[str] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Canonical outbound body; the worker transmits these bytes without re-serializing JSON.
    payload_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    # Hash of the publish request fields that define idempotency equality.
    request_fingerprint_sha256: Mapped[str] = mapped_column(String(64))
    # Hash of payload_bytes, used to verify the exact body delivered by the worker.
    payload_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    deliveries: Mapped[list["Delivery"]] = relationship(
        back_populates="event", passive_deletes=True
    )


class Delivery(Base):
    """Durable work to deliver one event to one endpoint generation."""

    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "endpoint_id", "replay_generation"),
        CheckConstraint("replay_generation >= 0", name="replay_generation_nonnegative"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL) "
            "AND (lease_owner IS NULL) = (lease_token IS NULL)",
            name="lease_fields_set_together",
        ),
        CheckConstraint(
            "(status = 'in_progress') = (lease_owner IS NOT NULL)",
            name="lease_matches_status",
        ),
        CheckConstraint(
            "status != 'delivered' OR delivered_at IS NOT NULL",
            name="delivered_has_timestamp",
        ),
        Index(
            "ix_deliveries_due",
            "next_attempt_at",
            "id",
            postgresql_where=text("status IN ('pending', 'retry_wait')"),
        ),
        Index("ix_deliveries_expired_leases", "lease_expires_at"),
        Index("ix_deliveries_event_id_created_at", "event_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("events.id", ondelete="RESTRICT"), nullable=False
    )
    endpoint_id: Mapped[UUID] = mapped_column(
        ForeignKey("endpoints.id", ondelete="RESTRICT"), nullable=False
    )
    replay_generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[DeliveryStatus] = mapped_column(
        SqlEnum(
            DeliveryStatus,
            name="delivery_status",
            values_callable=enum_values,
            validate_strings=True,
        ),
        default=DeliveryStatus.PENDING,
        server_default=DeliveryStatus.PENDING.value,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column()
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    event: Mapped[Event] = relationship(back_populates="deliveries")
    endpoint: Mapped[Endpoint] = relationship(back_populates="deliveries")
    attempts: Mapped[list["DeliveryAttempt"]] = relationship(
        back_populates="delivery",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DeliveryAttempt.attempt_number",
    )


class DeliveryAttempt(Base):
    """Durable evidence reserved before one outbound request can begin."""

    __tablename__ = "delivery_attempts"
    __table_args__ = (
        UniqueConstraint("delivery_id", "attempt_number"),
        UniqueConstraint("delivery_id", "lease_token"),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint("duration_ms >= 0", name="duration_ms_nonnegative"),
        CheckConstraint("request_timestamp >= 0", name="request_timestamp_nonnegative"),
        CheckConstraint(
            "http_status_code IS NULL OR http_status_code BETWEEN 100 AND 599",
            name="http_status_code_valid",
        ),
        CheckConstraint("finished_at >= started_at", name="timestamps_ordered"),
        CheckConstraint(
            "(status = 'in_progress' AND disposition IS NULL "
            "AND http_status_code IS NULL AND error_type IS NULL "
            "AND error_message IS NULL AND response_body_excerpt IS NULL "
            "AND duration_ms IS NULL AND request_timestamp IS NULL "
            "AND retry_scheduled_for IS NULL AND started_at IS NULL "
            "AND finished_at IS NULL AND resolved_at IS NULL) OR "
            "(status = 'completed' AND disposition IS NOT NULL "
            "AND duration_ms IS NOT NULL AND request_timestamp IS NOT NULL "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND resolved_at IS NOT NULL) OR "
            "(status = 'indeterminate' AND disposition IS NULL "
            "AND http_status_code IS NULL AND error_type IS NOT NULL "
            "AND response_body_excerpt IS NULL AND duration_ms IS NULL "
            "AND request_timestamp IS NULL AND retry_scheduled_for IS NULL "
            "AND started_at IS NULL AND finished_at IS NULL "
            "AND resolved_at IS NOT NULL)",
            name="lifecycle_fields",
        ),
        Index(
            "uq_delivery_attempts_one_in_progress",
            "delivery_id",
            unique=True,
            postgresql_where=text("status = 'in_progress'"),
        ),
        Index("ix_delivery_attempts_delivery_id_started_at", "delivery_id", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[DeliveryAttemptStatus] = mapped_column(
        SqlEnum(
            DeliveryAttemptStatus,
            name="delivery_attempt_status",
            values_callable=enum_values,
            validate_strings=True,
        ),
        default=DeliveryAttemptStatus.IN_PROGRESS,
        server_default=DeliveryAttemptStatus.IN_PROGRESS.value,
    )
    lease_token: Mapped[UUID] = mapped_column(nullable=False)
    disposition: Mapped[DeliveryDisposition | None] = mapped_column(
        SqlEnum(
            DeliveryDisposition,
            name="delivery_disposition",
            values_callable=enum_values,
            validate_strings=True,
        )
    )
    http_status_code: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)
    response_body_excerpt: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    request_timestamp: Mapped[int | None] = mapped_column(BigInteger)
    retry_scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    delivery: Mapped[Delivery] = relationship(back_populates="attempts")
