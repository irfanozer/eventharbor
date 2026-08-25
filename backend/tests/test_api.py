import asyncio

import httpx
import pytest

import eventharbor.api as api_module
from eventharbor.api import app
from eventharbor.config import Settings, get_settings


class DisposableEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.asyncio
async def test_health_reports_service_identity() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["service"] == "eventharbor-api"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_lifespan_disposes_database_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    disposable_engine = DisposableEngine()
    monkeypatch.setattr(api_module, "engine", disposable_engine)

    async with api_module.lifespan(app):
        assert disposable_engine.disposed is False

    assert disposable_engine.disposed is True


@pytest.mark.asyncio
async def test_ready_reports_database_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    async def database_is_ready() -> None:
        return None

    monkeypatch.setattr(api_module, "_check_database_connection", database_is_ready)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "service": "eventharbor-api",
        "dependency": "postgresql",
    }
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_ready_times_out_without_leaking_database_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def database_never_responds() -> None:
        await asyncio.sleep(1)

    monkeypatch.setattr(api_module, "_check_database_connection", database_never_responds)
    app.dependency_overrides[get_settings] = lambda: Settings(
        readiness_timeout_seconds=0.01,
        _env_file=None,
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "service": "eventharbor-api",
        "dependency": "postgresql",
    }
    assert response.headers["cache-control"] == "no-store"
