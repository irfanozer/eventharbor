"""End-to-end proof that manual replay appends history instead of rewriting it."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventharbor.api import app as api_app
from eventharbor.config import Environment, Settings, get_settings
from eventharbor.database import get_session
from eventharbor.deliveries.state_machine import DeliveryStatus
from eventharbor.deliveries.worker import DeliveryWorker
from eventharbor.models import Delivery, DeliveryAttempt, Endpoint, Event
from eventharbor.receiver_lab.api import app as receiver_app

RECEIVER_URL = "http://receiver-lab.test/webhooks"


async def _row_count(sessions: async_sessionmaker[AsyncSession], model: type[object]) -> int:
    async with sessions() as session:
        value = await session.scalar(select(func.count()).select_from(model))
    assert value is not None
    return value


async def _run_worker_once(worker: DeliveryWorker) -> None:
    for _ in range(20):
        if await worker.run_once():
            return
        await asyncio.sleep(0.01)
    pytest.fail("worker did not claim the due delivery")


async def _seed_dead_letter_chain(
    sessions: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
) -> Delivery:
    """Create a terminal source without introducing transport timing into lock tests."""

    now = datetime.now(UTC)
    payload_bytes = b"{}"
    endpoint = Endpoint(
        id=uuid4(),
        name=f"Replay concurrency receiver {suffix}",
        target_url=RECEIVER_URL,
        signing_secret=b"integration-signing-secret",
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    event = Event(
        id=uuid4(),
        source="replay-concurrency-test",
        event_type="test.replay.concurrent",
        idempotency_key=f"event-{suffix}",
        payload={},
        payload_bytes=payload_bytes,
        request_fingerprint_sha256=sha256(f"request-{suffix}".encode()).hexdigest(),
        payload_sha256=sha256(payload_bytes).hexdigest(),
        created_at=now,
    )
    source = Delivery(
        id=uuid4(),
        event_id=event.id,
        endpoint_id=endpoint.id,
        replay_generation=0,
        status=DeliveryStatus.DEAD_LETTERED,
        attempt_count=1,
        next_attempt_at=None,
        last_error="seeded terminal failure",
        created_at=now,
        updated_at=now,
    )
    async with sessions() as session, session.begin():
        session.add_all([endpoint, event, source])
    return source


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_replay_requests_create_exactly_one_successor(
    database_sessions: async_sessionmaker[AsyncSession],
    migrated_test_database: str,
) -> None:
    """Exercise the event-row mutex with independent, simultaneous API sessions."""

    same_key_source = await _seed_dead_letter_chain(database_sessions, suffix="same-key")
    different_key_source = await _seed_dead_letter_chain(
        database_sessions,
        suffix="different-key",
    )
    settings = Settings(
        environment=Environment.TEST,
        database_url=migrated_test_database,
        receiver_lab_url=RECEIVER_URL,
        _env_file=None,
    )

    async def integration_session() -> AsyncIterator[AsyncSession]:
        async with database_sessions() as session:
            yield session

    api_app.dependency_overrides[get_session] = integration_session
    api_app.dependency_overrides[get_settings] = lambda: settings
    transport = httpx.ASGITransport(app=api_app)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://eventharbor.test",
        ) as api:
            same_key_path = f"/v1/deliveries/{same_key_source.id}/replays"
            same_key_responses = await asyncio.gather(
                api.post(same_key_path, headers={"Idempotency-Key": "same-approval"}),
                api.post(same_key_path, headers={"Idempotency-Key": "same-approval"}),
            )
            assert [response.status_code for response in same_key_responses] == [202, 202]
            assert len({response.json()["delivery_id"] for response in same_key_responses}) == 1
            assert {
                response.headers["X-EventHarbor-Idempotent-Replay"]
                for response in same_key_responses
            } == {"false", "true"}

            different_key_path = f"/v1/deliveries/{different_key_source.id}/replays"
            different_key_responses = await asyncio.gather(
                api.post(
                    different_key_path,
                    headers={"Idempotency-Key": "approval-a"},
                ),
                api.post(
                    different_key_path,
                    headers={"Idempotency-Key": "approval-b"},
                ),
            )
            assert sorted(response.status_code for response in different_key_responses) == [
                202,
                409,
            ]
            conflict = next(
                response for response in different_key_responses if response.status_code == 409
            )
            assert conflict.json()["code"] == "replay_source_superseded"

        async with database_sessions() as session:
            all_deliveries = (
                await session.scalars(
                    select(Delivery).order_by(Delivery.event_id, Delivery.replay_generation)
                )
            ).all()
        for source in (same_key_source, different_key_source):
            chain = [
                delivery for delivery in all_deliveries if delivery.event_id == source.event_id
            ]
            assert [delivery.replay_generation for delivery in chain] == [0, 1]
            active = [
                delivery
                for delivery in chain
                if delivery.status
                in {
                    DeliveryStatus.PENDING,
                    DeliveryStatus.IN_PROGRESS,
                    DeliveryStatus.RETRY_WAIT,
                }
            ]
            assert len(active) == 1
            assert active[0].replayed_from_delivery_id == source.id
    finally:
        api_app.dependency_overrides.clear()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dead_letter_repair_and_idempotent_replay_preserve_source_history(
    database_sessions: async_sessionmaker[AsyncSession],
    migrated_test_database: str,
) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_url=migrated_test_database,
        receiver_lab_url=RECEIVER_URL,
        worker_http_timeout_seconds=1,
        worker_lease_seconds=5,
        worker_max_attempts=1,
        worker_base_delay_seconds=0.01,
        worker_max_delay_seconds=0.1,
        _env_file=None,
    )

    async def integration_session() -> AsyncIterator[AsyncSession]:
        async with database_sessions() as session:
            yield session

    api_app.dependency_overrides[get_session] = integration_session
    api_app.dependency_overrides[get_settings] = lambda: settings
    api_transport = httpx.ASGITransport(app=api_app)
    receiver_transport = httpx.ASGITransport(app=receiver_app)

    try:
        async with (
            httpx.AsyncClient(transport=api_transport, base_url="http://eventharbor.test") as api,
            httpx.AsyncClient(
                transport=receiver_transport,
                base_url="http://receiver-lab.test",
            ) as receiver,
        ):
            configured_failure = await receiver.put(
                "/control",
                json={
                    "mode": "fail_then_succeed",
                    "failures_before_success": 20,
                    "delay_ms": 0,
                },
            )
            assert configured_failure.status_code == 200

            endpoint_response = await api.post(
                "/v1/endpoints",
                json={"name": "Replay Integration Receiver", "url": RECEIVER_URL},
            )
            assert endpoint_response.status_code == 201
            endpoint = endpoint_response.json()

            published_response = await api.post(
                "/v1/events",
                headers={"Idempotency-Key": "replay-integration-event-1"},
                json={
                    "endpoint_id": endpoint["id"],
                    "type": "invoice.payment_failed",
                    "data": {"invoice_id": "inv_replay", "amount": 9900},
                },
            )
            assert published_response.status_code == 202
            published = published_response.json()

            worker = DeliveryWorker(
                database_sessions,
                receiver,
                settings,
                worker_id="replay-integration-worker",
                random_fraction=lambda: 0.0,
            )
            await _run_worker_once(worker)

            source_before_response = await api.get(
                f"/v1/deliveries/{published['delivery_id']}/attempts"
            )
            assert source_before_response.status_code == 200
            source_before = source_before_response.json()
            assert source_before["delivery"]["status"] == "dead_lettered"
            assert source_before["delivery"]["replay_generation"] == 0
            assert len(source_before["attempts"]) == 1
            assert source_before["attempts"][0]["disposition"] == "retry"
            assert source_before["attempts"][0]["http_status_code"] == 503
            assert json.loads(source_before["attempts"][0]["response_body_excerpt"]) == {
                "code": "temporarily_unavailable",
                "detail": "Receiver Lab is in the controlled unavailable state.",
            }

            missing = await api.post(
                f"/v1/deliveries/{uuid4()}/replays",
                headers={"Idempotency-Key": "missing-replay"},
            )
            blank_key = await api.post(
                f"/v1/deliveries/{published['delivery_id']}/replays",
                headers={"Idempotency-Key": "   "},
            )
            assert missing.status_code == 404
            assert missing.json()["code"] == "delivery_not_found"
            assert blank_key.status_code == 422
            assert blank_key.json()["code"] == "invalid_idempotency_key"

            endpoint_id = UUID(endpoint["id"])
            async with database_sessions() as session, session.begin():
                persisted_endpoint = await session.get(Endpoint, endpoint_id)
                assert persisted_endpoint is not None
                persisted_endpoint.enabled = False

            disabled = await api.post(
                f"/v1/deliveries/{published['delivery_id']}/replays",
                headers={"Idempotency-Key": "disabled-replay"},
            )
            assert disabled.status_code == 409
            assert disabled.json()["code"] == "endpoint_disabled"

            async with database_sessions() as session, session.begin():
                persisted_endpoint = await session.get(Endpoint, endpoint_id)
                assert persisted_endpoint is not None
                persisted_endpoint.enabled = True

            configured_success = await receiver.put(
                "/control",
                json={"mode": "success", "failures_before_success": 0, "delay_ms": 0},
            )
            assert configured_success.status_code == 200

            replay_path = f"/v1/deliveries/{published['delivery_id']}/replays"
            replay_headers = {"Idempotency-Key": "repair-invoice-replay-1"}
            first_replay_response = await api.post(replay_path, headers=replay_headers)
            duplicate_replay_response = await api.post(replay_path, headers=replay_headers)

            assert first_replay_response.status_code == 202
            assert duplicate_replay_response.status_code == 202
            replay = first_replay_response.json()
            assert duplicate_replay_response.json() == replay
            assert replay["source_delivery_id"] == published["delivery_id"]
            assert replay["event_id"] == published["event_id"]
            assert replay["endpoint_id"] == endpoint["id"]
            assert replay["replay_generation"] == 1
            assert replay["status"] == "pending"
            assert (
                first_replay_response.headers["Location"]
                == f"/v1/deliveries/{replay['delivery_id']}/attempts"
            )
            assert first_replay_response.headers["X-EventHarbor-Idempotent-Replay"] == "false"
            assert duplicate_replay_response.headers["X-EventHarbor-Idempotent-Replay"] == "true"

            superseded_while_active = await api.post(
                replay_path,
                headers={"Idempotency-Key": "different-approval-while-active"},
            )
            replay_of_pending = await api.post(
                f"/v1/deliveries/{replay['delivery_id']}/replays",
                headers={"Idempotency-Key": "pending-is-not-replayable"},
            )
            assert superseded_while_active.status_code == 409
            assert superseded_while_active.json()["code"] == "replay_source_superseded"
            assert replay_of_pending.status_code == 409
            assert replay_of_pending.json()["code"] == "delivery_not_replayable"

            await _run_worker_once(worker)

            replay_attempts_response = await api.get(
                f"/v1/deliveries/{replay['delivery_id']}/attempts"
            )
            source_after_response = await api.get(
                f"/v1/deliveries/{published['delivery_id']}/attempts"
            )
            history_response = await api.get(f"/v1/events/{published['event_id']}")
            receiver_requests_response = await receiver.get("/requests")

            assert replay_attempts_response.status_code == 200
            assert source_after_response.status_code == 200
            assert history_response.status_code == 200
            replay_attempts = replay_attempts_response.json()
            source_after = source_after_response.json()
            history = history_response.json()

            assert source_after == source_before
            assert replay_attempts["delivery"]["status"] == "delivered"
            assert replay_attempts["delivery"]["replay_generation"] == 1
            assert len(replay_attempts["attempts"]) == 1
            assert replay_attempts["attempts"][0]["attempt_number"] == 1
            assert replay_attempts["attempts"][0]["disposition"] == "succeeded"
            assert replay_attempts["attempts"][0]["http_status_code"] == 200
            assert [delivery["replay_generation"] for delivery in history["deliveries"]] == [0, 1]
            assert [delivery["status"] for delivery in history["deliveries"]] == [
                "dead_lettered",
                "delivered",
            ]
            assert history["deliveries"][0]["replayed_from_delivery_id"] is None
            assert history["deliveries"][1]["replayed_from_delivery_id"] == published["delivery_id"]

            receiver_requests = receiver_requests_response.json()
            assert receiver_requests["count"] == 2
            assert [request["event_id"] for request in receiver_requests["requests"]] == [
                published["event_id"],
                published["event_id"],
            ]
            assert [
                request["response_status_code"] for request in receiver_requests["requests"]
            ] == [503, 200]

            # Same request lookup happens before current eligibility checks, so a
            # lost 202 response remains safely recoverable after delivery succeeds.
            duplicate_after_success = await api.post(replay_path, headers=replay_headers)
            superseded_source = await api.post(
                replay_path,
                headers={"Idempotency-Key": "different-approval-after-success"},
            )
            delivered_not_replayable = await api.post(
                f"/v1/deliveries/{replay['delivery_id']}/replays",
                headers={"Idempotency-Key": "delivered-is-not-replayable"},
            )

            assert duplicate_after_success.status_code == 202
            replay_after_success = duplicate_after_success.json()
            assert replay_after_success == replay
            assert replay_after_success["status"] == "pending"
            assert duplicate_after_success.headers["X-EventHarbor-Idempotent-Replay"] == "true"
            assert superseded_source.status_code == 409
            assert superseded_source.json()["code"] == "replay_source_superseded"
            assert delivered_not_replayable.status_code == 409
            assert delivered_not_replayable.json()["code"] == "delivery_not_replayable"

        assert await _row_count(database_sessions, Event) == 1
        assert await _row_count(database_sessions, Endpoint) == 1
        assert await _row_count(database_sessions, Delivery) == 2
        assert await _row_count(database_sessions, DeliveryAttempt) == 2

        async with database_sessions() as session:
            deliveries = (
                await session.scalars(select(Delivery).order_by(Delivery.replay_generation))
            ).all()
        source_delivery, replay_delivery = deliveries
        assert source_delivery.replayed_from_delivery_id is None
        assert source_delivery.replay_idempotency_key is None
        assert replay_delivery.replayed_from_delivery_id == source_delivery.id
        assert replay_delivery.replay_idempotency_key == "repair-invoice-replay-1"
    finally:
        api_app.dependency_overrides.clear()
