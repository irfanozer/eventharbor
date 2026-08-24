import asyncio
from datetime import datetime
from hashlib import sha256

import httpx
import pytest
import pytest_asyncio

from eventharbor.receiver_lab.api import app, receiver


@pytest_asyncio.fixture(autouse=True)
async def reset_receiver_state():
    await receiver.reset()
    yield
    await receiver.reset()


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
    assert [item["sequence"] for item in requests["requests"]] == [1, 2, 3]
    assert [item["attempt"] for item in requests["requests"]] == [1, 2, 3]
    assert [item["response_status_code"] for item in requests["requests"]] == [503, 503, 200]


@pytest.mark.asyncio
async def test_receiver_can_rate_limit_twice_then_recover() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/control?run_id=rate-limit-demo",
            json={"mode": "rate_limited", "failures_before_success": 2, "delay_ms": 0},
        )

        responses = [
            await client.post(
                "/webhooks",
                content=b"{}",
                headers={"X-EventHarbor-Demo-Run-Id": "rate-limit-demo"},
            )
            for _ in range(3)
        ]
        requests = (await client.get("/requests?run_id=rate-limit-demo")).json()

    assert [response.status_code for response in responses] == [429, 429, 200]
    assert [response.headers.get("Retry-After") for response in responses] == ["2", "2", None]
    assert [item["response_status_code"] for item in requests["requests"]] == [429, 429, 200]


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
    body = b'{"type":"invoice.paid"}'
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        configured = await client.put(
            "/control",
            json={"mode": mode, "failures_before_success": 0, "delay_ms": 0},
        )
        response = await client.post(
            "/webhooks",
            content=body,
            headers={
                "X-EventHarbor-Event-Id": "evt_demo",
                "X-EventHarbor-Delivery-Id": "delivery_demo",
                "X-EventHarbor-Event-Type": "invoice.paid",
                "X-EventHarbor-Attempt": "3",
                "X-EventHarbor-Timestamp": "1700000000",
                "X-EventHarbor-Signature": "v1=demo",
            },
        )
        recorded = (await client.get("/requests")).json()["requests"][0]

    assert configured.status_code == 200
    assert response.status_code == expected_status
    assert recorded["sequence"] == 1
    assert recorded["attempt"] == 1
    assert recorded["event_id"] == "evt_demo"
    assert recorded["delivery_id"] == "delivery_demo"
    assert recorded["event_type"] == "invoice.paid"
    assert recorded["delivery_attempt"] == 3
    assert recorded["request_timestamp"] == 1_700_000_000
    assert datetime.fromisoformat(recorded["received_at"]).tzinfo is not None
    assert recorded["response_status_code"] == expected_status
    assert recorded["receiver_mode"] == mode
    assert recorded["signature_present"] is True
    assert recorded["body_preview"] == body.decode()
    assert recorded["body_sha256"] == sha256(body).hexdigest()


@pytest.mark.asyncio
async def test_receiver_preserves_history_when_scenario_changes() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/control",
            json={"mode": "fail_then_succeed", "failures_before_success": 20, "delay_ms": 0},
        )
        failed = await client.post(
            "/webhooks",
            content=b'{"generation":0}',
            headers={
                "X-EventHarbor-Event-Id": "event-shared",
                "X-EventHarbor-Delivery-Id": "delivery-generation-0",
                "X-EventHarbor-Attempt": "1",
            },
        )
        repaired = await client.put(
            "/control",
            json={"mode": "success", "failures_before_success": 0, "delay_ms": 0},
        )
        delivered = await client.post(
            "/webhooks",
            content=b'{"generation":1}',
            headers={
                "X-EventHarbor-Event-Id": "event-shared",
                "X-EventHarbor-Delivery-Id": "delivery-generation-1",
                "X-EventHarbor-Attempt": "1",
            },
        )
        state = (await client.get("/control")).json()

    assert failed.status_code == 503
    assert repaired.json()["attempts"] == 0
    assert delivered.status_code == 200
    assert state["attempts"] == 1
    assert [item["sequence"] for item in state["requests"]] == [1, 2]
    # Receiver-local attempts restart for the repaired scenario; worker attempts
    # are independently correlated by delivery ID and delivery_attempt.
    assert [item["attempt"] for item in state["requests"]] == [1, 1]
    assert [item["delivery_id"] for item in state["requests"]] == [
        "delivery-generation-0",
        "delivery-generation-1",
    ]
    assert [item["delivery_attempt"] for item in state["requests"]] == [1, 1]
    assert [item["response_status_code"] for item in state["requests"]] == [503, 200]
    assert [item["receiver_mode"] for item in state["requests"]] == [
        "fail_then_succeed",
        "success",
    ]


@pytest.mark.asyncio
async def test_scoped_runs_keep_configuration_counters_and_evidence_isolated() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/control?run_id=run-a",
            json={"mode": "fail_then_succeed", "failures_before_success": 20, "delay_ms": 0},
        )
        await client.put(
            "/control?run_id=run-b",
            json={"mode": "rate_limited", "failures_before_success": 0, "delay_ms": 0},
        )

        run_a_failed, run_b_limited = await asyncio.gather(
            client.post(
                "/webhooks",
                content=b'{"run":"a"}',
                headers={
                    "X-EventHarbor-Demo-Run-Id": "run-a",
                    "X-EventHarbor-Event-Id": "event-a",
                },
            ),
            client.post(
                "/webhooks",
                content=b'{"run":"b"}',
                headers={
                    "X-EventHarbor-Demo-Run-Id": "run-b",
                    "X-EventHarbor-Event-Id": "event-b",
                },
            ),
        )
        await client.put(
            "/control?run_id=run-a",
            json={"mode": "success", "failures_before_success": 0, "delay_ms": 0},
        )
        run_a_delivered, run_b_still_limited = await asyncio.gather(
            client.post(
                "/webhooks",
                content=b'{"run":"a"}',
                headers={
                    "X-EventHarbor-Demo-Run-Id": "run-a",
                    "X-EventHarbor-Event-Id": "event-a",
                },
            ),
            client.post(
                "/webhooks",
                content=b'{"run":"b"}',
                headers={
                    "X-EventHarbor-Demo-Run-Id": "run-b",
                    "X-EventHarbor-Event-Id": "event-b",
                },
            ),
        )
        run_a = (await client.get("/control?run_id=run-a")).json()
        run_b = (await client.get("/control?run_id=run-b")).json()

    assert [
        run_a_failed.status_code,
        run_b_limited.status_code,
        run_a_delivered.status_code,
        run_b_still_limited.status_code,
    ] == [503, 429, 200, 429]
    assert run_a["attempts"] == 1
    assert run_b["attempts"] == 2
    assert [item["sequence"] for item in run_a["requests"]] == [1, 2]
    assert [item["sequence"] for item in run_b["requests"]] == [1, 2]
    assert {item["event_id"] for item in run_a["requests"]} == {"event-a"}
    assert {item["event_id"] for item in run_b["requests"]} == {"event-b"}
    assert [item["response_status_code"] for item in run_a["requests"]] == [503, 200]
    assert [item["response_status_code"] for item in run_b["requests"]] == [429, 429]


@pytest.mark.asyncio
async def test_unknown_scoped_webhook_falls_back_to_legacy_state() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/control",
            json={"mode": "permanent_failure", "failures_before_success": 0, "delay_ms": 0},
        )
        response = await client.post(
            "/webhooks",
            content=b"{}",
            headers={"X-EventHarbor-Demo-Run-Id": "old-client-run"},
        )
        legacy = (await client.get("/control")).json()

    assert response.status_code == 400
    assert legacy["attempts"] == 1
    assert legacy["requests"][0]["response_status_code"] == 400


@pytest.mark.asyncio
async def test_scoped_run_registry_is_bounded_and_evicts_least_recently_used_state() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/control",
            json={"mode": "permanent_failure", "failures_before_success": 0, "delay_ms": 0},
        )
        for number in range(101):
            await client.put(
                f"/control?run_id=run-{number}",
                json={"mode": "success", "failures_before_success": 0, "delay_ms": 0},
            )

        evicted_run = await client.post(
            "/webhooks",
            content=b"{}",
            headers={"X-EventHarbor-Demo-Run-Id": "run-0"},
        )

    assert evicted_run.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", ["", "contains space", "slash/not-safe", "a" * 129])
async def test_receiver_rejects_unsafe_scoped_run_identifiers(run_id: str) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        control = await client.get("/control", params={"run_id": run_id})
        webhook = await client.post(
            "/webhooks",
            content=b"{}",
            headers={"X-EventHarbor-Demo-Run-Id": run_id},
        )

    assert control.status_code == 422
    assert webhook.status_code == 422


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
    assert payload["requests"][0]["sequence"] == 4
    assert payload["requests"][0]["attempt"] == 4
    assert payload["requests"][-1]["event_id"] == "evt-22"


@pytest.mark.asyncio
async def test_receiver_history_is_bounded_to_one_hundred_observations() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for number in range(105):
            await client.post(
                "/webhooks",
                content=str(number).encode(),
                headers={"X-EventHarbor-Event-Id": f"event-{number}"},
            )
        history = (await client.get("/requests")).json()

    assert history["count"] == 100
    assert history["requests"][0]["sequence"] == 6
    assert history["requests"][-1]["sequence"] == 105
