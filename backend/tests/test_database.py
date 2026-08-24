import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from eventharbor.config import Settings
from eventharbor.database import build_engine, engine, get_session


def test_build_engine_uses_configured_async_url() -> None:
    custom_engine = build_engine(
        Settings(
            database_url="postgresql+asyncpg://user:password@database:5432/test_database",
            database_pool_size=2,
            database_max_overflow=3,
            _env_file=None,
        )
    )

    assert custom_engine.url.host == "database"
    assert custom_engine.url.database == "test_database"


def test_build_engine_can_resolve_cached_process_settings() -> None:
    default_engine = build_engine()

    assert default_engine.url.drivername == "postgresql+asyncpg"


@pytest.mark.asyncio
async def test_session_dependency_yields_an_uncommitted_async_session() -> None:
    dependency = get_session()
    session = await anext(dependency)

    assert isinstance(session, AsyncSession)
    assert session.sync_session.expire_on_commit is False

    await dependency.aclose()
    await engine.dispose()
