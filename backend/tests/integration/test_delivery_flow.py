"""End-to-end proof of durable ingestion and background delivery."""

import asyncio
from collections.abc import AsyncIterator
from hashlib import sha256

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eventharbor.api import app as api_app
from eventharbor.config import Environment, Settings, get_settings
from eventharbor.database import get_session
from eventharbor.deliveries.worker import DeliveryWorker
from eventharbor.models import Delivery, DeliveryAttempt, Endpoint, Event
from eventharbor.receiver_lab.api import app as receiver_app

RECEIVER_URL = "http://receiver-lab.test/webhooks"


async def _row_count(sessions: async_sessionmaker[AsyncSession], model: type[object]) -> int:
    async with sessions() as session:
        value = await session.scalar(select(func.count()).select_from(model))
    assert value is not None
    return value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_publish_worker_success_and_idempotency_are_persisted(
    database_sessions: async_sessionmaker[AsyncSession],
    migrated_test_database: str,
) -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_url=migrated_test_database,
        receiver_lab_url=RECEIVER_URL,
        worker_http_timeout_seconds=1,
        worker_lease_seconds=5,
        worker_base_delay_seconds=0.01,
        worker_max_delay_seconds=0.1,
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
            configured = await receiver.put(
                "/control",
                json={"mode": "success", "failures_before_success": 0, "delay_ms": 0},
            )
            assert configured.status_code == 200

            endpoint_response = await api.post(
                "/v1/endpoints",
                json={"name": "Integration Receiver", "url": RECEIVER_URL},
            )
            assert endpoint_response.status_code == 201
            endpoint = endpoint_response.json()
            assert endpoint["signing_secret"].startswith("ehsec_")

            event_request = {
                "endpoint_id": endpoint["id"],
                "type": "invoice.paid",
                "data": {"invoice_id": "inv_integration", "amount": 4200},
            }
            first_publish = await api.post(
                "/v1/events",
                headers={"Idempotency-Key": "integration-invoice-paid-1"},
                json=event_request,
            )
            duplicate_publish = await api.post(
                "/v1/events",
                headers={"Idempotency-Key": "integration-invoice-paid-1"},
                json=event_request,
            )
            conflicting_publish = await api.post(
                "/v1/events",
                headers={"Idempotency-Key": "integration-invoice-paid-1"},
                json={**event_request, "data": {"invoice_id": "different"}},
            )

            assert first_publish.status_code == 202
            assert duplicate_publish.status_code == 202
            assert conflicting_publish.status_code == 409
            assert conflicting_publish.json()["code"] == "idempotency_key_reused"
            accepted = first_publish.json()
            replayed = duplicate_publish.json()
            assert replayed["event_id"] == accepted["event_id"]
            assert replayed["delivery_id"] == accepted["delivery_id"]
            assert first_publish.headers["X-EventHarbor-Idempotent-Replay"] == "false"
            assert duplicate_publish.headers["X-EventHarbor-Idempotent-Replay"] == "true"
            assert await _row_count(database_sessions, Event) == 1
            assert await _row_count(database_sessions, Delivery) == 1
            pending_event = await api.get(f"/v1/events/{accepted['event_id']}")
            assert pending_event.status_code == 200
            assert pending_event.json()["deliveries"][0]["status"] == "pending"
            async with database_sessions() as inspection_session:
                persisted_event = await inspection_session.get(Event, accepted["event_id"])
                persisted = await inspection_session.get(Delivery, accepted["delivery_id"])
            assert persisted_event is not None
            expected_payload_digest = sha256(persisted_event.payload_bytes).hexdigest()
            assert persisted_event.payload_sha256 == expected_payload_digest
            assert (
                pending_event.json()["request_fingerprint_sha256"]
                == persisted_event.request_fingerprint_sha256
            )
            assert pending_event.json()["payload_sha256"] == persisted_event.payload_sha256
            assert persisted_event.payload_sha256 != persisted_event.request_fingerprint_sha256
            assert persisted is not None
            assert persisted.next_attempt_at is not None
            assert persisted.lease_token is None

            worker = DeliveryWorker(
                database_sessions,
                receiver,
                settings,
                worker_id="integration-worker",
                random_fraction=lambda: 0.0,
            )
            worked = False
            for _ in range(20):
                worked = await worker.run_once()
                if worked:
                    break
                await asyncio.sleep(0.01)
            assert worked is True
            assert await worker.run_once() is False

            event_detail = await api.get(f"/v1/events/{accepted['event_id']}")
            attempt_detail = await api.get(f"/v1/deliveries/{accepted['delivery_id']}/attempts")
            receiver_requests = (await receiver.get("/requests")).json()

        assert event_detail.status_code == 200
        persisted_delivery = event_detail.json()["deliveries"][0]
        assert persisted_delivery["status"] == "delivered"
        assert persisted_delivery["attempt_count"] == 1
        assert persisted_delivery["delivered_at"] is not None

        assert attempt_detail.status_code == 200
        attempts = attempt_detail.json()["attempts"]
        assert len(attempts) == 1
        assert attempts[0]["attempt_number"] == 1
        assert attempts[0]["status"] == "completed"
        assert attempts[0]["disposition"] == "succeeded"
        assert attempts[0]["http_status_code"] == 200
        assert await _row_count(database_sessions, DeliveryAttempt) == 1
        assert await _row_count(database_sessions, Endpoint) == 1

        assert receiver_requests["count"] == 1
        received = receiver_requests["requests"][0]
        assert received["event_id"] == accepted["event_id"]
        assert received["signature_present"] is True
        assert '"invoice_id":"inv_integration"' in received["body_preview"]
    finally:
        api_app.dependency_overrides.clear()
