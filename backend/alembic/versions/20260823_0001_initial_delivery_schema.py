"""Create the initial endpoint, event, delivery, and attempt schema.

Revision ID: 20260823_0001
Revises:
Create Date: 2026-08-23 16:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260823_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

delivery_status = postgresql.ENUM(
    "pending",
    "in_progress",
    "retry_wait",
    "delivered",
    "dead_lettered",
    name="delivery_status",
    create_type=False,
)
delivery_disposition = postgresql.ENUM(
    "succeeded",
    "retry",
    "terminal_failure",
    name="delivery_disposition",
    create_type=False,
)


def upgrade() -> None:
    """Create the first durable delivery schema."""

    bind = op.get_bind()
    delivery_status.create(bind, checkfirst=True)
    delivery_disposition.create(bind, checkfirst=True)

    op.create_table(
        "endpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("signing_secret", sa.LargeBinary(), nullable=False),
        sa.Column("secret_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(name) > 0", name=op.f("ck_endpoints_name_not_empty")),
        sa.CheckConstraint(
            "length(target_url) > 0", name=op.f("ck_endpoints_target_url_not_empty")
        ),
        sa.CheckConstraint(
            "octet_length(signing_secret) > 0",
            name=op.f("ck_endpoints_signing_secret_not_empty"),
        ),
        sa.CheckConstraint(
            "secret_version >= 1", name=op.f("ck_endpoints_secret_version_positive")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_endpoints"),
    )
    op.create_index("ix_endpoints_enabled_created_at", "endpoints", ["enabled", "created_at"])

    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("event_type", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(source) > 0", name=op.f("ck_events_source_not_empty")),
        sa.CheckConstraint("length(event_type) > 0", name=op.f("ck_events_event_type_not_empty")),
        sa.CheckConstraint(
            "length(idempotency_key) > 0",
            name=op.f("ck_events_idempotency_key_not_empty"),
        ),
        sa.CheckConstraint(
            "octet_length(payload_bytes) > 0",
            name=op.f("ck_events_payload_bytes_not_empty"),
        ),
        sa.CheckConstraint(
            "length(payload_sha256) = 64", name=op.f("ck_events_payload_sha256_length")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        sa.UniqueConstraint("source", "idempotency_key", name="uq_events_source_idempotency_key"),
    )
    op.create_index("ix_events_event_type_created_at", "events", ["event_type", "created_at"])

    op.create_table(
        "deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("replay_generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "status",
            delivery_status,
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("lease_owner", sa.String(length=160), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "replay_generation >= 0",
            name=op.f("ck_deliveries_replay_generation_nonnegative"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_deliveries_attempt_count_nonnegative")
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL) "
            "AND (lease_owner IS NULL) = (lease_token IS NULL)",
            name=op.f("ck_deliveries_lease_fields_set_together"),
        ),
        sa.CheckConstraint(
            "status != 'delivered' OR delivered_at IS NOT NULL",
            name=op.f("ck_deliveries_delivered_has_timestamp"),
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["endpoints.id"],
            name="fk_deliveries_endpoint_id_endpoints",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name="fk_deliveries_event_id_events", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deliveries"),
        sa.UniqueConstraint(
            "event_id",
            "endpoint_id",
            "replay_generation",
            name="uq_deliveries_event_id_endpoint_id_replay_generation",
        ),
    )
    op.create_index(
        "ix_deliveries_due",
        "deliveries",
        ["next_attempt_at", "id"],
        postgresql_where=sa.text("status IN ('pending', 'retry_wait')"),
    )
    op.create_index("ix_deliveries_event_id_created_at", "deliveries", ["event_id", "created_at"])
    op.create_index("ix_deliveries_expired_leases", "deliveries", ["lease_expires_at"])

    op.create_table(
        "delivery_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("disposition", delivery_disposition, nullable=False),
        sa.Column("http_status_code", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("response_body_excerpt", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("request_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("retry_scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name=op.f("ck_delivery_attempts_attempt_number_positive"),
        ),
        sa.CheckConstraint(
            "duration_ms >= 0",
            name=op.f("ck_delivery_attempts_duration_ms_nonnegative"),
        ),
        sa.CheckConstraint(
            "request_timestamp >= 0",
            name=op.f("ck_delivery_attempts_request_timestamp_nonnegative"),
        ),
        sa.CheckConstraint(
            "http_status_code IS NULL OR http_status_code BETWEEN 100 AND 599",
            name=op.f("ck_delivery_attempts_http_status_code_valid"),
        ),
        sa.CheckConstraint(
            "finished_at >= started_at",
            name=op.f("ck_delivery_attempts_timestamps_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["deliveries.id"],
            name="fk_delivery_attempts_delivery_id_deliveries",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_delivery_attempts"),
        sa.UniqueConstraint(
            "delivery_id",
            "attempt_number",
            name="uq_delivery_attempts_delivery_id_attempt_number",
        ),
    )
    op.create_index(
        "ix_delivery_attempts_delivery_id_started_at",
        "delivery_attempts",
        ["delivery_id", "started_at"],
    )


def downgrade() -> None:
    """Remove the first durable delivery schema."""

    op.drop_index("ix_delivery_attempts_delivery_id_started_at", table_name="delivery_attempts")
    op.drop_table("delivery_attempts")
    op.drop_index("ix_deliveries_expired_leases", table_name="deliveries")
    op.drop_index("ix_deliveries_event_id_created_at", table_name="deliveries")
    op.drop_index("ix_deliveries_due", table_name="deliveries")
    op.drop_table("deliveries")
    op.drop_index("ix_events_event_type_created_at", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_endpoints_enabled_created_at", table_name="endpoints")
    op.drop_table("endpoints")

    bind = op.get_bind()
    delivery_disposition.drop(bind, checkfirst=True)
    delivery_status.drop(bind, checkfirst=True)
