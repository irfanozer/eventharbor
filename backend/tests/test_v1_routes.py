from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import Response

from eventharbor.config import Settings
from eventharbor.control_room import (
    ControlRoomQueryService,
    DeadLetterRecord,
    EventListRecord,
    OverviewSnapshot,
    Page,
)
from eventharbor.deliveries.retry import DeliveryDisposition
from eventharbor.deliveries.state_machine import DeliveryAttemptStatus, DeliveryStatus
from eventharbor.demo import ReceiverLabDemoService
from eventharbor.errors import DomainError
from eventharbor.models import Delivery, DeliveryAttempt, Endpoint, Event
from eventharbor.routers.v1 import (
    configure_receiver_lab,
    create_endpoint,
    get_control_room_overview,
    get_delivery_attempts,
    get_endpoint,
    get_event,
    get_receiver_lab_state,
    list_dead_letters,
    list_endpoints,
    list_events,
    publish_event,
    replay_delivery,
)
from eventharbor.schemas import (
    EndpointCreateRequest,
    EventPublishRequest,
    ReceiverLabConfigurationResponse,
    ReceiverLabPreset,
    ReceiverLabPresetRequest,
    ReceiverLabRequestResponse,
    ReceiverLabStateResponse,
)
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


@pytest.mark.asyncio
async def test_control_room_overview_maps_all_statuses_and_disables_cache(monkeypatch) -> None:
    now = datetime.now(UTC)

    async def fake_overview(service):
        return OverviewSnapshot(
            generated_at=now,
            events_total=12,
            endpoints_total=3,
            endpoints_enabled=2,
            deliveries_by_status={
                DeliveryStatus.PENDING: 2,
                DeliveryStatus.RETRY_WAIT: 4,
                DeliveryStatus.DEAD_LETTERED: 1,
            },
            actionable_dead_letters=1,
        )

    monkeypatch.setattr(ControlRoomQueryService, "overview", fake_overview)
    response = Response()

    result = await get_control_room_overview(response, Session())

    assert response.headers["Cache-Control"] == "no-store"
    assert result.generated_at == now
    assert result.events_total == 12
    assert result.deliveries.model_dump() == {
        "pending": 2,
        "in_progress": 0,
        "retry_wait": 4,
        "delivered": 0,
        "dead_lettered": 1,
    }


@pytest.mark.asyncio
async def test_control_room_event_list_maps_latest_generation_without_secrets(monkeypatch) -> None:
    endpoint, event, delivery, _ = objects()
    delivery.replay_generation = 2

    async def fake_events(service, **kwargs):
        assert kwargs == {
            "limit": 10,
            "cursor": "opaque",
            "event_type": "test.event",
            "endpoint_id": endpoint.id,
            "delivery_status": DeliveryStatus.PENDING,
        }
        return Page(
            items=[EventListRecord(event=event, latest_delivery=delivery, endpoint=endpoint)],
            next_cursor="next-page",
        )

    monkeypatch.setattr(ControlRoomQueryService, "events", fake_events)
    response = Response()

    result = await list_events(
        response,
        Session(),
        limit=10,
        cursor="opaque",
        event_type="test.event",
        endpoint_id=endpoint.id,
        delivery_status=DeliveryStatus.PENDING,
    )

    assert response.headers["Cache-Control"] == "no-store"
    assert result.next_cursor == "next-page"
    assert result.items[0].id == event.id
    assert result.items[0].generation_count == 3
    assert result.items[0].endpoint.url == endpoint.target_url
    assert "signing_secret" not in result.items[0].endpoint.model_dump()


@pytest.mark.asyncio
async def test_control_room_endpoint_list_and_detail_never_expose_secret(monkeypatch) -> None:
    endpoint, _, _, _ = objects()

    async def fake_endpoints(service, **kwargs):
        assert kwargs == {"limit": 8, "cursor": None}
        return Page(items=[endpoint], next_cursor=None)

    async def fake_endpoint(service, endpoint_id):
        assert endpoint_id == endpoint.id
        return endpoint

    monkeypatch.setattr(ControlRoomQueryService, "endpoints", fake_endpoints)
    monkeypatch.setattr(ControlRoomQueryService, "endpoint", fake_endpoint)
    list_response = Response()
    detail_response = Response()

    endpoint_page = await list_endpoints(list_response, Session(), limit=8, cursor=None)
    endpoint_detail = await get_endpoint(endpoint.id, detail_response, Session())

    assert list_response.headers["Cache-Control"] == "no-store"
    assert detail_response.headers["Cache-Control"] == "no-store"
    assert endpoint_page.items[0] == endpoint_detail
    assert endpoint_detail.url == endpoint.target_url
    assert "signing_secret" not in endpoint_detail.model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "replayable", "blocked_reason"),
    [(True, True, None), (False, False, "Endpoint is disabled.")],
)
async def test_dead_letter_list_explains_replay_eligibility(
    monkeypatch,
    enabled: bool,
    replayable: bool,
    blocked_reason: str | None,
) -> None:
    endpoint, event, delivery, _ = objects()
    endpoint.enabled = enabled
    delivery.status = DeliveryStatus.DEAD_LETTERED
    delivery.next_attempt_at = None

    async def fake_dead_letters(service, **kwargs):
        assert kwargs == {"limit": 25, "cursor": None, "endpoint_id": endpoint.id}
        return Page(
            items=[DeadLetterRecord(event=event, delivery=delivery, endpoint=endpoint)],
            next_cursor=None,
        )

    monkeypatch.setattr(ControlRoomQueryService, "dead_letters", fake_dead_letters)
    response = Response()

    result = await list_dead_letters(
        response,
        Session(),
        limit=25,
        cursor=None,
        endpoint_id=endpoint.id,
    )

    assert response.headers["Cache-Control"] == "no-store"
    assert result.items[0].event_id == event.id
    assert result.items[0].replayable is replayable
    assert result.items[0].blocked_reason == blocked_reason


def receiver_state(preset: ReceiverLabPreset) -> ReceiverLabStateResponse:
    now = datetime.now(UTC)
    return ReceiverLabStateResponse(
        preset=preset,
        configuration=ReceiverLabConfigurationResponse(
            mode="success",
            failures_before_success=0,
            delay_ms=0,
        ),
        attempts=1,
        requests=[
            ReceiverLabRequestResponse(
                sequence=7,
                attempt=1,
                event_id="event-1",
                delivery_id="delivery-1",
                event_type="demo.order.ready",
                delivery_attempt=1,
                request_timestamp=1_700_000_000,
                received_at=now,
                response_status_code=503,
                receiver_mode="fail_then_succeed",
                signature_present=True,
                body_preview='{"order_id":"ORDER-1"}',
                body_sha256="a" * 64,
            )
        ],
    )


@pytest.mark.asyncio
async def test_receiver_lab_facade_routes_map_state_and_disable_cache(monkeypatch) -> None:
    async def fake_state(service, run_id):
        assert run_id == "run-1"
        return receiver_state(ReceiverLabPreset.SUCCESS)

    async def fake_configure(service, preset, run_id):
        assert preset is ReceiverLabPreset.DEAD_LETTER
        assert run_id == "run-1"
        return receiver_state(preset)

    monkeypatch.setattr(ReceiverLabDemoService, "state", fake_state)
    monkeypatch.setattr(ReceiverLabDemoService, "configure", fake_configure)
    settings = Settings(_env_file=None)
    get_response = Response()
    put_response = Response()

    read_result = await get_receiver_lab_state(get_response, settings, "run-1")
    configured_result = await configure_receiver_lab(
        ReceiverLabPresetRequest(preset=ReceiverLabPreset.DEAD_LETTER),
        put_response,
        settings,
        "run-1",
    )

    assert get_response.headers["Cache-Control"] == "no-store"
    assert put_response.headers["Cache-Control"] == "no-store"
    assert read_result.preset is ReceiverLabPreset.SUCCESS
    assert read_result.requests[0].delivery_id == "delivery-1"
    assert read_result.requests[0].response_status_code == 503
    assert configured_result.preset is ReceiverLabPreset.DEAD_LETTER
