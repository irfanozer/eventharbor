import httpx
import pytest

from eventharbor.receiver_lab.api import app


@pytest.mark.asyncio
async def test_receiver_can_fail_twice_then_succeed() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        configured = await client.put(
            "/control",
            json={"mode": "fail_then_succeed", "failures_before_success": 2, "delay_ms": 0},
        )

        assert configured.status_code == 200
        assert (await client.post("/webhooks", content=b"{}")).status_code == 503
        assert (await client.post("/webhooks", content=b"{}")).status_code == 503
        assert (await client.post("/webhooks", content=b"{}")).status_code == 200

        requests = (await client.get("/requests")).json()
    assert requests["count"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [
        ("success", 200),
        ("rate_limited", 429),
        ("timeout", 200),
        ("permanent_failure", 400),
    ],
)
async def test_receiver_exposes_each_seeded_behavior(mode: str, expected_status: int) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        configured = await client.put(
            "/control",
            json={"mode": mode, "failures_before_success": 0, "delay_ms": 0},
        )
        response = await client.post(
            "/webhooks",
            content=b'{"type":"invoice.paid"}',
            headers={
                "X-EventHarbor-Event-Id": "evt_demo",
                "X-EventHarbor-Signature": "v1=demo",
            },
        )
        recorded = (await client.get("/requests")).json()["requests"][0]

    assert configured.status_code == 200
    assert response.status_code == expected_status
    assert recorded["event_id"] == "evt_demo"
    assert recorded["signature_present"] is True


@pytest.mark.asyncio
async def test_receiver_health_reports_service_identity() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["service"] == "eventharbor-receiver-lab"


@pytest.mark.asyncio
async def test_receiver_control_get_returns_configuration_and_last_twenty_requests() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        configured = await client.put(
            "/control",
            json={"mode": "permanent_failure", "failures_before_success": 0, "delay_ms": 0},
        )
        assert configured.status_code == 200
        for attempt in range(23):
            await client.post(
                "/webhooks",
                content=f'{{"attempt":{attempt}}}',
                headers={"X-EventHarbor-Event-Id": f"evt-{attempt}"},
            )

        response = await client.get("/control")

    assert response.status_code == 200
    payload = response.json()
    assert payload["configuration"] == {
        "mode": "permanent_failure",
        "failures_before_success": 0,
        "delay_ms": 0,
    }
    assert payload["attempts"] == 23
    assert len(payload["requests"]) == 20
    assert payload["requests"][0]["attempt"] == 4
    assert payload["requests"][-1]["event_id"] == "evt-22"
