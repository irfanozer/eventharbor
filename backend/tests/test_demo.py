import json

import httpx
import pytest
import pytest_asyncio

from eventharbor.demo import SCENARIOS, ReceiverLabDemoService
from eventharbor.errors import DomainError
from eventharbor.receiver_lab.api import app as receiver_app
from eventharbor.receiver_lab.api import receiver
from eventharbor.schemas import ReceiverLabPreset


@pytest_asyncio.fixture(autouse=True)
async def reset_receiver_state():
    await receiver.reset()
    yield
    await receiver.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preset", "expected"),
    [
        (ReceiverLabPreset.SUCCESS, ("success", 0, 0)),
        (ReceiverLabPreset.RETRY_THEN_RECOVER, ("fail_then_succeed", 2, 0)),
        (ReceiverLabPreset.RATE_LIMITED, ("rate_limited", 0, 0)),
        (ReceiverLabPreset.RATE_LIMIT_THEN_RECOVER, ("rate_limited", 2, 0)),
        (ReceiverLabPreset.TIMEOUT, ("timeout", 0, 7_000)),
        (ReceiverLabPreset.PERMANENT_FAILURE, ("permanent_failure", 0, 0)),
        (ReceiverLabPreset.DEAD_LETTER, ("fail_then_succeed", 20, 0)),
    ],
)
async def test_named_preset_configures_receiver_lab_with_server_owned_values(
    preset: ReceiverLabPreset,
    expected: tuple[str, int, int],
) -> None:
    transport = httpx.ASGITransport(app=receiver_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://receiver.test") as client:
        state = await ReceiverLabDemoService(client).configure(preset)

    assert state.preset is preset
    assert (
        state.configuration.mode,
        state.configuration.failures_before_success,
        state.configuration.delay_ms,
    ) == expected
    assert state.attempts == 0
    assert state.requests == []
    assert SCENARIOS[preset].as_payload() == {
        "mode": expected[0],
        "failures_before_success": expected[1],
        "delay_ms": expected[2],
    }


@pytest.mark.asyncio
async def test_state_returns_recent_safe_receiver_evidence_and_unknown_custom_preset() -> None:
    transport = httpx.ASGITransport(app=receiver_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://receiver.test") as client:
        configured = await client.put(
            "/control",
            json={"mode": "fail_then_succeed", "failures_before_success": 3, "delay_ms": 0},
        )
        assert configured.status_code == 200
        for attempt in range(22):
            await client.post(
                "/webhooks",
                content=json.dumps({"attempt": attempt}),
                headers={
                    "X-EventHarbor-Event-Id": f"event-{attempt}",
                    "X-EventHarbor-Signature": "v1=test",
                },
            )
        state = await ReceiverLabDemoService(client).state()

    assert state.preset is None
    assert state.attempts == 22
    assert len(state.requests) == 20
    assert state.requests[0].sequence == 3
    assert state.requests[0].attempt == 3
    assert state.requests[0].response_status_code == 503
    assert state.requests[0].receiver_mode == "fail_then_succeed"
    assert state.requests[-1].event_id == "event-21"
    assert state.requests[-1].response_status_code == 200
    assert state.requests[-1].signature_present is True
    assert len(state.requests[-1].body_sha256) == 64


@pytest.mark.asyncio
async def test_facade_preserves_receiver_evidence_across_repair() -> None:
    transport = httpx.ASGITransport(app=receiver_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://receiver.test") as client:
        service = ReceiverLabDemoService(client)
        await service.configure(ReceiverLabPreset.DEAD_LETTER)
        failed = await client.post(
            "/webhooks",
            content=b'{"order_id":"ORDER-1"}',
            headers={
                "X-EventHarbor-Event-Id": "event-1",
                "X-EventHarbor-Delivery-Id": "delivery-0",
                "X-EventHarbor-Event-Type": "demo.order.ready",
                "X-EventHarbor-Attempt": "1",
                "X-EventHarbor-Timestamp": "1700000000",
                "X-EventHarbor-Signature": "v1=test",
            },
        )
        await service.configure(ReceiverLabPreset.SUCCESS)
        delivered = await client.post(
            "/webhooks",
            content=b'{"order_id":"ORDER-1"}',
            headers={
                "X-EventHarbor-Event-Id": "event-1",
                "X-EventHarbor-Delivery-Id": "delivery-1",
                "X-EventHarbor-Event-Type": "demo.order.ready",
                "X-EventHarbor-Attempt": "1",
                "X-EventHarbor-Timestamp": "1700000001",
                "X-EventHarbor-Signature": "v1=test",
            },
        )
        state = await service.state()

    assert failed.status_code == 503
    assert delivered.status_code == 200
    assert state.attempts == 1
    assert [request.delivery_id for request in state.requests] == ["delivery-0", "delivery-1"]
    assert [request.response_status_code for request in state.requests] == [503, 200]
    assert state.requests[0].event_id == state.requests[1].event_id == "event-1"


@pytest.mark.asyncio
async def test_demo_service_scopes_control_calls_and_reads_by_run_id() -> None:
    transport = httpx.ASGITransport(app=receiver_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://receiver.test") as client:
        service = ReceiverLabDemoService(client)
        await service.configure(ReceiverLabPreset.DEAD_LETTER, "run-a")
        await service.configure(ReceiverLabPreset.SUCCESS, "run-b")
        failed = await client.post(
            "/webhooks",
            content=b"{}",
            headers={"X-EventHarbor-Demo-Run-Id": "run-a"},
        )
        delivered = await client.post(
            "/webhooks",
            content=b"{}",
            headers={"X-EventHarbor-Demo-Run-Id": "run-b"},
        )
        run_a = await service.state("run-a")
        run_b = await service.state("run-b")

    assert failed.status_code == 503
    assert delivered.status_code == 200
    assert run_a.preset is ReceiverLabPreset.DEAD_LETTER
    assert run_b.preset is ReceiverLabPreset.SUCCESS
    assert run_a.attempts == run_b.attempts == 1
    assert run_a.requests[0].response_status_code == 503
    assert run_b.requests[0].response_status_code == 200


@pytest.mark.asyncio
async def test_demo_service_rejects_unsafe_run_id_before_network_io() -> None:
    def unexpected_network_call(_: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid run_id should not reach Receiver Lab")

    async with _failing_client(unexpected_network_call) as client:
        with pytest.raises(ValueError, match="bounded, header-safe"):
            await ReceiverLabDemoService(client).state("not safe")


def _failing_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://receiver.test"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused", request=request)),
        lambda request: httpx.Response(500, request=request),
        lambda request: httpx.Response(200, content=b"not-json", request=request),
        lambda request: httpx.Response(200, json=["wrong-shape"], request=request),
        lambda request: httpx.Response(
            200,
            json={"configuration": {"mode": "success"}, "attempts": 0},
            request=request,
        ),
    ],
)
async def test_receiver_failures_collapse_to_safe_service_unavailable(handler) -> None:
    async with _failing_client(handler) as client:
        with pytest.raises(DomainError) as raised:
            await ReceiverLabDemoService(client).state()

    assert raised.value.status_code == 503
    assert raised.value.code == "receiver_lab_unavailable"
    assert "receiver lab" in raised.value.detail.lower()
    assert "refused" not in raised.value.detail.lower()
