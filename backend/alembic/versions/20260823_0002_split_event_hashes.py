"""Separate request idempotency fingerprints from outbound payload digests.

Revision ID: 20260823_0002
Revises: 20260823_0001
Create Date: 2026-08-23 17:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260823_0002"
down_revision: str | None = "20260823_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Give each distinct hash its own accurately named column."""

    op.drop_constraint(op.f("ck_events_payload_sha256_length"), "events", type_="check")
    op.alter_column(
        "events",
        "payload_sha256",
        new_column_name="request_fingerprint_sha256",
    )
    op.create_check_constraint(
        op.f("ck_events_request_fingerprint_sha256_length"),
        "events",
        "length(request_fingerprint_sha256) = 64",
    )

    op.add_column("events", sa.Column("payload_sha256", sa.String(length=64), nullable=True))
    # PostgreSQL's built-in sha256(bytea) hashes the exact stored outbound bytes. Keeping
    # the column nullable during this single set-based update makes the revision safe for
    # databases that already contain events, then the invariant is restored immediately.
    op.execute(
        "UPDATE events "
        "SET payload_sha256 = encode(sha256(payload_bytes), 'hex') "
        "WHERE payload_sha256 IS NULL"
    )
    op.alter_column("events", "payload_sha256", nullable=False)
    op.create_check_constraint(
        op.f("ck_events_payload_sha256_length"),
        "events",
        "length(payload_sha256) = 64",
    )


def downgrade() -> None:
    """Restore the original request-fingerprint-only schema."""

    op.drop_constraint(op.f("ck_events_payload_sha256_length"), "events", type_="check")
    op.drop_column("events", "payload_sha256")
    op.drop_constraint(
        op.f("ck_events_request_fingerprint_sha256_length"),
        "events",
        type_="check",
    )
    op.alter_column(
        "events",
        "request_fingerprint_sha256",
        new_column_name="payload_sha256",
    )
    op.create_check_constraint(
        op.f("ck_events_payload_sha256_length"),
        "events",
        "length(payload_sha256) = 64",
    )
