"""Add bounded Control Room query indexes.

Revision ID: 20260824_0005
Revises: 20260823_0004
Create Date: 2026-08-24 15:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260824_0005"
down_revision: str | None = "20260823_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Index the stable sort and filter keys used by the Control Room."""

    op.create_index(
        "ix_events_created_at_id",
        "events",
        ["created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_endpoints_created_at_id",
        "endpoints",
        ["created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_deliveries_status_updated_at_id",
        "deliveries",
        ["status", "updated_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove Control Room query indexes without changing stored data."""

    op.drop_index("ix_deliveries_status_updated_at_id", table_name="deliveries")
    op.drop_index("ix_endpoints_created_at_id", table_name="endpoints")
    op.drop_index("ix_events_created_at_id", table_name="events")
