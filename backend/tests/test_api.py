import httpx
import pytest

from eventharbor.api import app


@pytest.mark.asyncio
async def test_health_reports_service_identity() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["service"] == "eventharbor-api"
