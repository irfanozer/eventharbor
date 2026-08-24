"""Data-preservation proofs for EventHarbor's data-bearing migrations."""

import os
import subprocess
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

V1 = "20260823_0001"
V2 = "20260823_0002"
V3 = "20260823_0003"
HEAD = "20260823_0004"

ENDPOINT_ID = UUID("00000000-0000-0000-0000-000000000101")
EVENT_ID = UUID("00000000-0000-0000-0000-000000000102")
COMPLETED_DELIVERY_ID = UUID("00000000-0000-0000-0000-000000000103")
LEASED_DELIVERY_ID = UUID("00000000-0000-0000-0000-000000000104")
COMPLETED_ATTEMPT_ID = UUID("00000000-0000-0000-0000-000000000105")
ACTIVE_LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000106")

CREATED_AT = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
ATTEMPT_STARTED_AT = datetime(2026, 8, 23, 12, 1, tzinfo=UTC)
ATTEMPT_FINISHED_AT = datetime(2026, 8, 23, 12, 1, 0, 125000, tzinfo=UTC)
DELIVERED_AT = datetime(2026, 8, 23, 12, 1, 1, tzinfo=UTC)
LEASE_UPDATED_AT = datetime(2026, 8, 23, 12, 2, tzinfo=UTC)
LEASE_EXPIRES_AT = datetime(2026, 8, 23, 12, 7, tzinfo=UTC)

# The spaces and trailing newline are intentional. They prove the migration hashes the
# stored outbound bytes instead of serializing the equivalent JSONB value again.
PAYLOAD_BYTES = b'{ "amount": 42, "event": "legacy" }\n'
REQUEST_FINGERPRINT = sha256(b"legacy publish request fingerprint").hexdigest()
PAYLOAD_DIGEST = sha256(PAYLOAD_BYTES).hexdigest()


def _alembic(database_url: str, command: str, revision: str) -> None:
    """Run one revision command with the already-validated test database URL."""

    parsed = make_url(database_url)
    if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").endswith("_test"):
        pytest.fail("migration tests may run only against a PostgreSQL *_test database")

    backend_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["EVENTHARBOR_ENVIRONMENT"] = "test"
    environment["EVENTHARBOR_DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", command, revision],
        cwd=backend_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"Alembic {command} {revision} failed.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


async def _execute(database_url: str, statement: str, parameters: dict[str, Any]) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement), parameters)
    finally:
        await engine.dispose()


async def _row(database_url: str, statement: str, parameters: dict[str, Any]) -> Any:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(statement), parameters)
            return result.mappings().one()
    finally:
        await engine.dispose()


async def _rows(database_url: str, statement: str, parameters: dict[str, Any]) -> list[Any]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(statement), parameters)
            return list(result.mappings().all())
    finally:
        await engine.dispose()


async def _truncate_head(database_url: str) -> None:
    await _execute(
        database_url,
        "TRUNCATE TABLE delivery_attempts, deliveries, events, endpoints CASCADE",
        {},
    )


async def _seed_v1(database_url: str) -> None:
    parameters = {
        "endpoint_id": ENDPOINT_ID,
        "event_id": EVENT_ID,
        "completed_delivery_id": COMPLETED_DELIVERY_ID,
        "leased_delivery_id": LEASED_DELIVERY_ID,
        "completed_attempt_id": COMPLETED_ATTEMPT_ID,
        "active_lease_token": ACTIVE_LEASE_TOKEN,
        "signing_secret": b"migration-test-secret",
        "payload": '{"event":"legacy","amount":42}',
        "payload_bytes": PAYLOAD_BYTES,
        "request_fingerprint": REQUEST_FINGERPRINT,
        "created_at": CREATED_AT,
        "attempt_started_at": ATTEMPT_STARTED_AT,
        "attempt_finished_at": ATTEMPT_FINISHED_AT,
        "delivered_at": DELIVERED_AT,
        "lease_updated_at": LEASE_UPDATED_AT,
        "lease_expires_at": LEASE_EXPIRES_AT,
    }
    statements = (
        """
        INSERT INTO endpoints
            (id, name, target_url, signing_secret, created_at, updated_at)
        VALUES
            (:endpoint_id, 'Legacy receiver', 'http://receiver-lab.test/webhooks',
             :signing_secret, :created_at, :created_at)
        """,
        """
        INSERT INTO events
            (id, source, event_type, idempotency_key, payload, payload_bytes,
             payload_sha256, created_at)
        VALUES
            (:event_id, 'migration-test', 'invoice.created', 'legacy-event-1',
             CAST(:payload AS jsonb), :payload_bytes, :request_fingerprint, :created_at)
        """,
        """
        INSERT INTO deliveries
            (id, event_id, endpoint_id, replay_generation, status, attempt_count,
             next_attempt_at, delivered_at, created_at, updated_at)
        VALUES
            (:completed_delivery_id, :event_id, :endpoint_id, 0, 'delivered', 1,
             NULL, :delivered_at, :created_at, :delivered_at)
        """,
        """
        INSERT INTO deliveries
            (id, event_id, endpoint_id, replay_generation, status, attempt_count,
             next_attempt_at, lease_owner, lease_expires_at, lease_token,
             created_at, updated_at)
        VALUES
            (:leased_delivery_id, :event_id, :endpoint_id, 1, 'in_progress', 1,
             NULL, 'legacy-worker', :lease_expires_at, :active_lease_token,
             :created_at, :lease_updated_at)
        """,
        """
        INSERT INTO delivery_attempts
            (id, delivery_id, attempt_number, disposition, http_status_code,
             response_body_excerpt, duration_ms, request_timestamp, started_at,
             finished_at, created_at)
        VALUES
            (:completed_attempt_id, :completed_delivery_id, 1, 'succeeded', 204,
             'accepted', 125, 1787486460, :attempt_started_at,
             :attempt_finished_at, :attempt_finished_at)
        """,
    )

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement), parameters)
    finally:
        await engine.dispose()


async def test_data_bearing_migrations_round_trip(
    migrated_test_database: str,
) -> None:
    """Preserve exact v1 data through upgrades and every representable downgrade."""

    database_url = migrated_test_database
    await _truncate_head(database_url)
    try:
        _alembic(database_url, "downgrade", V1)
        await _seed_v1(database_url)

        _alembic(database_url, "upgrade", V2)
        event_at_v2 = await _row(
            database_url,
            """
            SELECT payload_bytes, request_fingerprint_sha256, payload_sha256
            FROM events WHERE id = :event_id
            """,
            {"event_id": EVENT_ID},
        )
        assert bytes(event_at_v2["payload_bytes"]) == PAYLOAD_BYTES
        assert event_at_v2["request_fingerprint_sha256"] == REQUEST_FINGERPRINT
        assert event_at_v2["payload_sha256"] == PAYLOAD_DIGEST

        _alembic(database_url, "upgrade", HEAD)
        attempts_at_head = await _rows(
            database_url,
            """
            SELECT id, delivery_id, attempt_number, status, lease_token, disposition,
                   http_status_code, response_body_excerpt, duration_ms,
                   request_timestamp, started_at, finished_at, resolved_at, created_at
            FROM delivery_attempts
            ORDER BY delivery_id
            """,
            {},
        )
        assert len(attempts_at_head) == 2

        completed, leased = attempts_at_head
        assert completed["id"] == COMPLETED_ATTEMPT_ID
        assert completed["delivery_id"] == COMPLETED_DELIVERY_ID
        assert completed["attempt_number"] == 1
        assert completed["status"] == "completed"
        assert completed["lease_token"] == COMPLETED_ATTEMPT_ID
        assert completed["disposition"] == "succeeded"
        assert completed["http_status_code"] == 204
        assert completed["response_body_excerpt"] == "accepted"
        assert completed["duration_ms"] == 125
        assert completed["request_timestamp"] == 1787486460
        assert completed["started_at"] == ATTEMPT_STARTED_AT
        assert completed["finished_at"] == ATTEMPT_FINISHED_AT
        assert completed["resolved_at"] == ATTEMPT_FINISHED_AT
        assert completed["created_at"] == ATTEMPT_FINISHED_AT

        assert leased["id"] == ACTIVE_LEASE_TOKEN
        assert leased["delivery_id"] == LEASED_DELIVERY_ID
        assert leased["attempt_number"] == 1
        assert leased["status"] == "in_progress"
        assert leased["lease_token"] == ACTIVE_LEASE_TOKEN
        for empty_field in (
            "disposition",
            "http_status_code",
            "response_body_excerpt",
            "duration_ms",
            "request_timestamp",
            "started_at",
            "finished_at",
            "resolved_at",
        ):
            assert leased[empty_field] is None
        assert leased["created_at"] == LEASE_UPDATED_AT

        deliveries_at_head = await _rows(
            database_url,
            """
            SELECT id, replay_generation, replayed_from_delivery_id,
                   replay_idempotency_key
            FROM deliveries ORDER BY replay_generation
            """,
            {},
        )
        assert [row["id"] for row in deliveries_at_head] == [
            COMPLETED_DELIVERY_ID,
            LEASED_DELIVERY_ID,
        ]
        assert [row["replay_generation"] for row in deliveries_at_head] == [0, 1]
        assert all(row["replayed_from_delivery_id"] is None for row in deliveries_at_head)
        assert all(row["replay_idempotency_key"] is None for row in deliveries_at_head)

        replay_columns = await _rows(
            database_url,
            """
            SELECT column_name, data_type, character_maximum_length, is_nullable
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'deliveries'
              AND column_name IN ('replayed_from_delivery_id', 'replay_idempotency_key')
            ORDER BY column_name
            """,
            {},
        )
        assert replay_columns == [
            {
                "column_name": "replay_idempotency_key",
                "data_type": "character varying",
                "character_maximum_length": 255,
                "is_nullable": "YES",
            },
            {
                "column_name": "replayed_from_delivery_id",
                "data_type": "uuid",
                "character_maximum_length": None,
                "is_nullable": "YES",
            },
        ]

        delivery_constraints = await _rows(
            database_url,
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'deliveries'::regclass
              AND conname IN (
                'fk_deliveries_replayed_from_delivery_id_deliveries',
                'uq_deliveries_replayed_from_delivery_id_replay_idempotency_key',
                'ck_deliveries_replay_metadata_set_together',
                'ck_deliveries_replay_idempotency_key_not_empty'
              )
            ORDER BY conname
            """,
            {},
        )
        assert {row["conname"] for row in delivery_constraints} == {
            "fk_deliveries_replayed_from_delivery_id_deliveries",
            "uq_deliveries_replayed_from_delivery_id_replay_idempotency_key",
            "ck_deliveries_replay_metadata_set_together",
            "ck_deliveries_replay_idempotency_key_not_empty",
        }

        active_index = await _row(
            database_url,
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = 'deliveries'
              AND indexname = 'uq_deliveries_one_active_per_event_endpoint'
            """,
            {},
        )
        assert "CREATE UNIQUE INDEX" in active_index["indexdef"]
        assert "event_id, endpoint_id" in active_index["indexdef"]
        assert "pending" in active_index["indexdef"]
        assert "in_progress" in active_index["indexdef"]
        assert "retry_wait" in active_index["indexdef"]

        _alembic(database_url, "downgrade", V3)
        deliveries_at_v3 = await _rows(
            database_url,
            """
            SELECT id, replay_generation, status
            FROM deliveries ORDER BY replay_generation
            """,
            {},
        )
        assert [row["id"] for row in deliveries_at_v3] == [
            COMPLETED_DELIVERY_ID,
            LEASED_DELIVERY_ID,
        ]
        assert [row["status"] for row in deliveries_at_v3] == ["delivered", "in_progress"]
        replay_columns_after_downgrade = await _rows(
            database_url,
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'deliveries'
              AND column_name IN ('replayed_from_delivery_id', 'replay_idempotency_key')
            """,
            {},
        )
        assert replay_columns_after_downgrade == []

        _alembic(database_url, "downgrade", V2)
        attempts_at_v2 = await _rows(
            database_url,
            """
            SELECT id, delivery_id, attempt_number, disposition, http_status_code,
                   response_body_excerpt, duration_ms, request_timestamp,
                   started_at, finished_at, created_at
            FROM delivery_attempts ORDER BY delivery_id
            """,
            {},
        )
        assert len(attempts_at_v2) == 1
        retained = attempts_at_v2[0]
        assert retained["id"] == COMPLETED_ATTEMPT_ID
        assert retained["delivery_id"] == COMPLETED_DELIVERY_ID
        assert retained["attempt_number"] == 1
        assert retained["disposition"] == "succeeded"
        assert retained["http_status_code"] == 204
        assert retained["response_body_excerpt"] == "accepted"
        assert retained["duration_ms"] == 125
        assert retained["request_timestamp"] == 1787486460
        assert retained["started_at"] == ATTEMPT_STARTED_AT
        assert retained["finished_at"] == ATTEMPT_FINISHED_AT
        assert retained["created_at"] == ATTEMPT_FINISHED_AT

        _alembic(database_url, "downgrade", V1)
        event_at_v1 = await _row(
            database_url,
            "SELECT payload_bytes, payload_sha256 FROM events WHERE id = :event_id",
            {"event_id": EVENT_ID},
        )
        assert bytes(event_at_v1["payload_bytes"]) == PAYLOAD_BYTES
        assert event_at_v1["payload_sha256"] == REQUEST_FINGERPRINT
    finally:
        # A failed assertion must never strand the shared dedicated test database at an
        # old revision. Upgrade first, then remove only this test's deterministic rows.
        _alembic(database_url, "upgrade", HEAD)
        await _truncate_head(database_url)
