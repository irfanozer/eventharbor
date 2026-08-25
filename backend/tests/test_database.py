import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import eventharbor.database as database_module
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


def test_build_engine_passes_explicit_tls_mode_to_asyncpg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_create_async_engine(url: str, **options: object) -> object:
        captured["url"] = url
        captured.update(options)
        return object()

    monkeypatch.setattr(database_module, "create_async_engine", fake_create_async_engine)

    result = database_module.build_engine(
        Settings(
            database_url="postgresql+asyncpg://app:password@database/eventharbor",
            database_ssl_mode="verify-full",
            _env_file=None,
        )
    )

    assert result is not None
    assert captured["connect_args"] == {"ssl": "verify-full"}


@pytest.mark.asyncio
async def test_session_dependency_yields_an_uncommitted_async_session() -> None:
    dependency = get_session()
    session = await anext(dependency)

    assert isinstance(session, AsyncSession)
    assert session.sync_session.expire_on_commit is False

    await dependency.aclose()
    await engine.dispose()
