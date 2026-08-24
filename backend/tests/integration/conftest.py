"""Dedicated PostgreSQL fixtures for cross-process delivery tests."""

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DATABASE_ENVIRONMENT_VARIABLE = "EVENTHARBOR_TEST_DATABASE_URL"
TABLES_IN_DELETE_ORDER = "delivery_attempts, deliveries, events, endpoints"


def _validated_test_database_url() -> str:
    database_url = os.environ.get(TEST_DATABASE_ENVIRONMENT_VARIABLE)
    if database_url is None:
        pytest.skip(
            f"set {TEST_DATABASE_ENVIRONMENT_VARIABLE} to run PostgreSQL integration tests",
            allow_module_level=True,
        )

    parsed = make_url(database_url)
    database_name = parsed.database or ""
    if parsed.get_backend_name() != "postgresql":
        pytest.fail(f"{TEST_DATABASE_ENVIRONMENT_VARIABLE} must use PostgreSQL")
    if not database_name.lower().endswith("_test"):
        pytest.fail(
            f"{TEST_DATABASE_ENVIRONMENT_VARIABLE} must name a dedicated database ending in _test"
        )
    if parsed.drivername != "postgresql+asyncpg":
        pytest.fail(f"{TEST_DATABASE_ENVIRONMENT_VARIABLE} must use the asyncpg driver")
    return database_url


@pytest.fixture(scope="session")
def migrated_test_database() -> Iterator[str]:
    """Upgrade only a clearly named test database through the production migrations."""

    database_url = _validated_test_database_url()
    backend_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["EVENTHARBOR_ENVIRONMENT"] = "test"
    environment["EVENTHARBOR_DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=backend_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            "Alembic could not prepare the integration database.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    yield database_url


async def _truncate_database(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session, session.begin():
        await session.execute(
            text(f"TRUNCATE TABLE {TABLES_IN_DELETE_ORDER} RESTART IDENTITY CASCADE")
        )


@pytest_asyncio.fixture
async def database_sessions(
    migrated_test_database: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Give API and worker separate sessions against one empty durable database."""

    engine = create_async_engine(migrated_test_database, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await _truncate_database(sessions)
    try:
        yield sessions
    finally:
        await _truncate_database(sessions)
        await engine.dispose()
