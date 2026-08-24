"""Add durable, idempotent manual delivery replays.

Revision ID: 20260823_0004
Revises: 20260823_0003
Create Date: 2026-08-23 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260823_0004"
down_revision: str | None = "20260823_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Record which dead-letter delivery and request created each replay."""

    op.add_column(
        "deliveries",
        sa.Column("replayed_from_delivery_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "deliveries",
        sa.Column("replay_idempotency_key", sa.String(length=255), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_deliveries_replayed_from_delivery_id_deliveries"),
        "deliveries",
        "deliveries",
        ["replayed_from_delivery_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        op.f("uq_deliveries_replayed_from_delivery_id_replay_idempotency_key"),
        "deliveries",
        ["replayed_from_delivery_id", "replay_idempotency_key"],
    )
    op.create_check_constraint(
        op.f("ck_deliveries_replay_metadata_set_together"),
        "deliveries",
        "(replayed_from_delivery_id IS NULL) = (replay_idempotency_key IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_deliveries_replay_idempotency_key_not_empty"),
        "deliveries",
        "replay_idempotency_key IS NULL OR length(replay_idempotency_key) > 0",
    )
    op.create_index(
        "uq_deliveries_one_active_per_event_endpoint",
        "deliveries",
        ["event_id", "endpoint_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'in_progress', 'retry_wait')"),
    )


def downgrade() -> None:
    """Remove replay provenance while preserving delivery generations."""

    op.drop_constraint(
        op.f("ck_deliveries_replay_idempotency_key_not_empty"),
        "deliveries",
        type_="check",
    )
    op.drop_index("uq_deliveries_one_active_per_event_endpoint", table_name="deliveries")
    op.drop_constraint(
        op.f("ck_deliveries_replay_metadata_set_together"),
        "deliveries",
        type_="check",
    )
    op.drop_constraint(
        op.f("uq_deliveries_replayed_from_delivery_id_replay_idempotency_key"),
        "deliveries",
        type_="unique",
    )
    op.drop_constraint(
        op.f("fk_deliveries_replayed_from_delivery_id_deliveries"),
        "deliveries",
        type_="foreignkey",
    )
    op.drop_column("deliveries", "replay_idempotency_key")
    op.drop_column("deliveries", "replayed_from_delivery_id")
