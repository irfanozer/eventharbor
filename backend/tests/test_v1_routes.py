from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import Response

from eventharbor.config import Settings
from eventharbor.deliveries.retry import DeliveryDisposition
from eventharbor.deliveries.state_machine import DeliveryAttemptStatus, DeliveryStatus
from eventharbor.errors import DomainError
from eventharbor.models import Delivery, DeliveryAttempt, Endpoint, Event
from eventharbor.routers.v1 import (
    create_endpoint,
    get_delivery_attempts,
    get_event,
    publish_event,
    replay_delivery,
)
from eventharbor.schemas import EndpointCreateRequest, EventPublishRequest
from eventharbor.services import (
    CreatedEndpoint,
    DeliveryAttempts,
    EndpointService,
    EventDetails,
    EventService,
    PublishedEvent,
    QueryService,
    ReplayedDelivery,
    ReplayService,
)


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class Session:
    def begin(self):
        return Transaction()


def objects():
    now = datetime.now(UTC)
    target = Endpoint(
        id=uuid4(),
        name="Lab",
        target_url="http://receiver-lab:8100/webhooks",
        signing_secret=b"secret",
        secret_version=1,
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    event = Event(
        id=uuid4(),
        source="local-api",
        event_type="test.event",
        idempotency_key="key",
        payload={"ok": True},
        payload_bytes=b"{}",
        request_fingerprint_sha256="1" * 64,
        payload_sha256="0" * 64,
        created_at=now,
    )
    delivery = Delivery(
        id=uuid4(),
        event_id=event.id,
        endpoint_id=target.id,
        replay_generation=0,
        status=DeliveryStatus.PENDING,
        attempt_count=1,
        next_attempt_at=now,
        lease_owner=None,
        lease_expires_at=None,
        lease_token=None,
        delivered_at=None,
        last_error=None,
        created_at=now,
        updated_at=now,
    )
    attempt = DeliveryAttempt(
        id=uuid4(),
        delivery_id=delivery.id,
        attempt_number=1,
        status=DeliveryAttemptStatus.COMPLETED,
        lease_token=uuid4(),
        disposition=DeliveryDisposition.RETRY,
        http_status_code=503,
        error_type=None,
        error_message=None,
        response_body_excerpt="unavailable",
        duration_ms=12,
        request_timestamp=1_700_000_000,
        retry_scheduled_for=now,
        started_at=now,
        finished_at=now,
        resolved_at=now,
        created_at=now,
    )
    return target, event, delivery, attempt


@pytest.mark.asyncio
async def test_create_endpoint_route_maps_one_time_secret(monkeypatch) -> None:
    target, _, _, _ = objects()

    async def fake_create(service, request):
        return CreatedEndpoint(target, "ehsec_once")

    monkeypatch.setattr(EndpointService, "create", fake_create)
    result = await create_endpoint(
        EndpointCreateRequest(name="Lab", url=target.target_url),
        Session(),
        Settings(receiver_lab_url=target.target_url),
    )

    assert result.url == target.target_url
    assert result.signing_secret == "ehsec_once"


@pytest.mark.asyncio
async def test_publish_route_sets_idempotent_replay_header(monkeypatch) -> None:
    target, event, delivery, _ = objects()

    async def fake_publish(service, request, idempotency_key):
        assert idempotency_key == "key"
        return PublishedEvent(event, delivery, True)

    monkeypatch.setattr(EventService, "publish", fake_publish)
    response = Response()
    result = await publish_event(
        EventPublishRequest(endpoint_id=target.id, type="test.event", data={}),
        response,
        Session(),
        "key",
    )

    assert result.event_id == event.id
    assert result.delivery_id == delivery.id
    assert response.headers["X-EventHarbor-Idempotent-Replay"] == "true"
    assert response.headers["Location"] == f"/v1/events/{event.id}"


@pytest.mark.asyncio
async def test_publish_route_rejects_blank_idempotency_key() -> None:
    target, _, _, _ = objects()
    with pytest.raises(DomainError) as raised:
        await publish_event(
            EventPublishRequest(endpoint_id=target.id, type="test.event", data={}),
            Response(),
            Session(),
            "   ",
        )
    assert raised.value.code == "invalid_idempotency_key"


@pytest.mark.asyncio
async def test_replay_route_sets_location_and_idempotency_headers(monkeypatch) -> None:
    _, _, source, _ = objects()
    source.status = DeliveryStatus.DEAD_LETTERED
    replay = Delivery(
        id=uuid4(),
        event_id=source.event_id,
        endpoint_id=source.endpoint_id,
        replay_generation=1,
        replayed_from_delivery_id=source.id,
        replay_idempotency_key="replay-key",
        status=DeliveryStatus.PENDING,
        attempt_count=0,
        next_attempt_at=source.created_at,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )

    async def fake_replay(service, delivery_id, idempotency_key):
        assert delivery_id == source.id
        assert idempotency_key == "replay-key"
        return ReplayedDelivery(source, replay, True)

    monkeypatch.setattr(ReplayService, "replay", fake_replay)
    response = Response()

    result = await replay_delivery(source.id, response, Session(), "replay-key")

    assert result.source_delivery_id == source.id
    assert result.delivery_id == replay.id
    assert result.replay_generation == 1
    assert response.headers["X-EventHarbor-Idempotent-Replay"] == "true"
    assert response.headers["Location"] == f"/v1/deliveries/{replay.id}/attempts"


@pytest.mark.asyncio
async def test_replay_route_rejects_blank_idempotency_key() -> None:
    _, _, delivery, _ = objects()

    with pytest.raises(DomainError) as raised:
        await replay_delivery(delivery.id, Response(), Session(), "  ")

    assert raised.value.code == "invalid_idempotency_key"


@pytest.mark.asyncio
async def test_query_routes_render_events_and_attempts(monkeypatch) -> None:
    _, event, delivery, attempt = objects()

    async def fake_event_details(service, event_id):
        return EventDetails(event, [delivery])

    async def fake_delivery_attempts(service, delivery_id):
        return DeliveryAttempts(delivery, [attempt])

    monkeypatch.setattr(QueryService, "event_details", fake_event_details)
    monkeypatch.setattr(QueryService, "delivery_attempts", fake_delivery_attempts)

    event_response = await get_event(event.id, Session())
    attempts_response = await get_delivery_attempts(delivery.id, Session())

    assert event_response.id == event.id
    assert event_response.payload_sha256 == "0" * 64
    assert event_response.request_fingerprint_sha256 == "1" * 64
    assert event_response.deliveries[0].id == delivery.id
    assert attempts_response.delivery.id == delivery.id
    assert attempts_response.attempts[0].status == DeliveryAttemptStatus.COMPLETED
    assert attempts_response.attempts[0].http_status_code == 503
