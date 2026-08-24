"""Reserve delivery-attempt evidence before outbound network I/O.

Revision ID: 20260823_0003
Revises: 20260823_0002
Create Date: 2026-08-23 18:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260823_0003"
down_revision: str | None = "20260823_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

attempt_status = sa.Enum(
    "in_progress",
    "completed",
    "indeterminate",
    name="delivery_attempt_status",
)


def upgrade() -> None:
    """Turn completed-only attempt rows into a durable claim ledger."""

    bind = op.get_bind()
    attempt_status.create(bind, checkfirst=False)

    op.add_column(
        "delivery_attempts",
        sa.Column("status", attempt_status, nullable=True),
    )
    op.add_column("delivery_attempts", sa.Column("lease_token", sa.Uuid(), nullable=True))
    op.add_column(
        "delivery_attempts",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )

    for column_name in (
        "disposition",
        "duration_ms",
        "request_timestamp",
        "started_at",
        "finished_at",
    ):
        op.alter_column("delivery_attempts", column_name, nullable=True)

    # All pre-revision rows were written only after transport completed. Their row ID is
    # a deterministic, non-secret historical token because the original lease token was
    # not retained once finalization cleared it.
    op.execute(
        "UPDATE delivery_attempts "
        "SET status = 'completed', lease_token = id, resolved_at = created_at"
    )

    # Preserve any lease active during deployment. The old worker had already consumed
    # its attempt number but had not yet created evidence for it.
    op.execute(
        "INSERT INTO delivery_attempts "
        "(id, delivery_id, attempt_number, status, lease_token, created_at) "
        "SELECT d.lease_token, d.id, d.attempt_count, 'in_progress', "
        "d.lease_token, d.updated_at "
        "FROM deliveries AS d "
        "WHERE d.status = 'in_progress' "
        "AND d.lease_token IS NOT NULL "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM delivery_attempts AS a "
        "  WHERE a.delivery_id = d.id AND a.attempt_number = d.attempt_count"
        ")"
    )

    op.alter_column("delivery_attempts", "status", nullable=False)
    op.alter_column("delivery_attempts", "lease_token", nullable=False)
    op.alter_column(
        "delivery_attempts",
        "status",
        server_default=sa.text("'in_progress'::delivery_attempt_status"),
    )

    op.create_unique_constraint(
        op.f("uq_delivery_attempts_delivery_id_lease_token"),
        "delivery_attempts",
        ["delivery_id", "lease_token"],
    )
    op.create_index(
        "uq_delivery_attempts_one_in_progress",
        "delivery_attempts",
        ["delivery_id"],
        unique=True,
        postgresql_where=sa.text("status = 'in_progress'"),
    )
    op.create_check_constraint(
        op.f("ck_delivery_attempts_lifecycle_fields"),
        "delivery_attempts",
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
    )
    op.create_check_constraint(
        op.f("ck_deliveries_lease_matches_status"),
        "deliveries",
        "(status = 'in_progress') = (lease_owner IS NOT NULL)",
    )


def downgrade() -> None:
    """Restore completed-only evidence used before durable reservations."""

    # The earlier schema cannot represent unresolved or indeterminate outcomes.
    op.execute("DELETE FROM delivery_attempts WHERE status != 'completed'")
    op.drop_constraint(op.f("ck_deliveries_lease_matches_status"), "deliveries", type_="check")
    op.drop_constraint(
        op.f("ck_delivery_attempts_lifecycle_fields"),
        "delivery_attempts",
        type_="check",
    )
    op.drop_index("uq_delivery_attempts_one_in_progress", table_name="delivery_attempts")
    op.drop_constraint(
        op.f("uq_delivery_attempts_delivery_id_lease_token"),
        "delivery_attempts",
        type_="unique",
    )

    for column_name in (
        "disposition",
        "duration_ms",
        "request_timestamp",
        "started_at",
        "finished_at",
    ):
        op.alter_column("delivery_attempts", column_name, nullable=False)

    op.drop_column("delivery_attempts", "resolved_at")
    op.drop_column("delivery_attempts", "lease_token")
    op.drop_column("delivery_attempts", "status")
    attempt_status.drop(op.get_bind(), checkfirst=False)
