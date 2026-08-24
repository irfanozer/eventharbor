from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from eventharbor.database import Base
from eventharbor.models import Delivery, DeliveryAttempt, Endpoint, Event


def test_initial_schema_registers_all_four_tables() -> None:
    assert set(Base.metadata.tables) == {
        "endpoints",
        "events",
        "deliveries",
        "delivery_attempts",
    }

    assert "payload_bytes" in Event.__table__.columns
    assert "request_fingerprint_sha256" in Event.__table__.columns
    assert "payload_sha256" in Event.__table__.columns
    assert "lease_token" in Delivery.__table__.columns
    assert "request_timestamp" in DeliveryAttempt.__table__.columns
    assert "retry_scheduled_for" in DeliveryAttempt.__table__.columns
    assert "status" in DeliveryAttempt.__table__.columns
    assert "resolved_at" in DeliveryAttempt.__table__.columns


def test_idempotency_and_delivery_generation_have_database_uniqueness() -> None:
    event_unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in Event.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    delivery_unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in Delivery.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    attempt_unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in DeliveryAttempt.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("source", "idempotency_key") in event_unique_columns
    assert ("event_id", "endpoint_id", "replay_generation") in delivery_unique_columns
    assert (
        "replayed_from_delivery_id",
        "replay_idempotency_key",
    ) in delivery_unique_columns
    assert ("delivery_id", "attempt_number") in attempt_unique_columns
    assert ("delivery_id", "lease_token") in attempt_unique_columns


def test_models_compile_to_postgresql_ddl() -> None:
    dialect = postgresql.dialect()

    for table in (
        Endpoint.__table__,
        Event.__table__,
        Delivery.__table__,
        DeliveryAttempt.__table__,
    ):
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert f"CREATE TABLE {table.name}" in ddl


def test_due_delivery_index_is_partial_and_attempt_fields_are_guarded() -> None:
    due_index = next(
        index for index in Delivery.__table__.indexes if index.name == "ix_deliveries_due"
    )
    index_ddl = str(CreateIndex(due_index).compile(dialect=postgresql.dialect()))
    attempt_checks = {
        constraint.name
        for constraint in DeliveryAttempt.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "WHERE status IN ('pending', 'retry_wait')" in index_ddl
    assert "ck_delivery_attempts_attempt_number_positive" in attempt_checks
    assert "ck_delivery_attempts_http_status_code_valid" in attempt_checks
    assert "ck_delivery_attempts_lifecycle_fields" in attempt_checks

    in_progress_index = next(
        index
        for index in DeliveryAttempt.__table__.indexes
        if index.name == "uq_delivery_attempts_one_in_progress"
    )
    in_progress_ddl = str(CreateIndex(in_progress_index).compile(dialect=postgresql.dialect()))
    assert "UNIQUE" in in_progress_ddl
    assert "WHERE status = 'in_progress'" in in_progress_ddl

    active_delivery_index = next(
        index
        for index in Delivery.__table__.indexes
        if index.name == "uq_deliveries_one_active_per_event_endpoint"
    )
    active_delivery_ddl = str(
        CreateIndex(active_delivery_index).compile(dialect=postgresql.dialect())
    )
    assert "UNIQUE" in active_delivery_ddl
    assert "WHERE status IN ('pending', 'in_progress', 'retry_wait')" in active_delivery_ddl


def test_event_hash_columns_have_independent_length_constraints() -> None:
    event_checks = {
        constraint.name
        for constraint in Event.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "ck_events_request_fingerprint_sha256_length" in event_checks
    assert "ck_events_payload_sha256_length" in event_checks
