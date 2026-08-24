import json
import math
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from eventharbor.deliveries.state_machine import DeliveryStatus
from eventharbor.errors import DomainError
from eventharbor.models import Delivery, Endpoint, Event
from eventharbor.schemas import EndpointCreateRequest, EventPublishRequest
from eventharbor.serialization import canonical_sha256
from eventharbor.services import EndpointService, EventService, QueryService


def endpoint(*, enabled: bool = True) -> Endpoint:
    now = datetime.now(UTC)
    return Endpoint(
        id=uuid4(),
        name="Receiver Lab",
        target_url="http://receiver-lab:8100/webhooks",
        signing_secret=b"secret",
        secret_version=1,
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )


def event_for(request: EventPublishRequest, key: str, *, fingerprint: str | None = None) -> Event:
    now = datetime.now(UTC)
    event_id = uuid4()
    return Event(
        id=event_id,
        source="local-api",
        event_type=request.type,
        idempotency_key=key,
        payload=request.data,
        payload_bytes=b"{}",
        request_fingerprint_sha256=fingerprint
        or canonical_sha256(
            {"endpoint_id": str(request.endpoint_id), "type": request.type, "data": request.data}
        ),
        payload_sha256=sha256(b"{}").hexdigest(),
        created_at=now,
    )


def delivery_for(event: Event, target: Endpoint) -> Delivery:
    now = datetime.now(UTC)
    return Delivery(
        id=uuid4(),
        event_id=event.id,
        endpoint_id=target.id,
        replay_generation=0,
        status=DeliveryStatus.PENDING,
        attempt_count=0,
        next_attempt_at=now,
        created_at=now,
        updated_at=now,
    )


class FakeEndpointRepository:
    def __init__(self, target: Endpoint | None = None) -> None:
        self.target = target
        self.created: Endpoint | None = None

    async def create(self, value: Endpoint) -> Endpoint:
        self.created = value
        return value

    async def get(self, endpoint_id: UUID) -> Endpoint | None:
        if self.target is not None and self.target.id == endpoint_id:
            return self.target
        return None


class FakeEventRepository:
    def __init__(self, *, inserted: bool, existing: Event | None = None) -> None:
        self.inserted = inserted
        self.existing = existing
        self.candidate = None

    async def insert_if_absent(self, candidate):
        self.candidate = candidate
        if self.inserted:
            self.existing = Event(
                id=candidate.id,
                source=candidate.source,
                event_type=candidate.event_type,
                idempotency_key=candidate.idempotency_key,
                payload=candidate.payload,
                payload_bytes=candidate.payload_bytes,
                request_fingerprint_sha256=candidate.request_fingerprint_sha256,
                payload_sha256=candidate.payload_sha256,
                created_at=candidate.created_at,
            )
        return self.inserted

    async def get_by_idempotency_key(self, source: str, key: str) -> Event | None:
        return self.existing

    async def get(self, event_id: UUID) -> Event | None:
        if self.existing is not None and self.existing.id == event_id:
            return self.existing
        return None


class FakeDeliveryRepository:
    def __init__(self, initial: Delivery | None = None) -> None:
        self.initial = initial
        self.created: Delivery | None = None
        self.attempts = []

    async def create(self, delivery: Delivery) -> Delivery:
        delivery.status = DeliveryStatus.PENDING
        self.created = delivery
        return delivery

    async def get_initial(self, event_id: UUID, endpoint_id: UUID) -> Delivery | None:
        return self.initial

    async def list_for_event(self, event_id: UUID):
        return [self.initial] if self.initial is not None else []

    async def get(self, delivery_id: UUID) -> Delivery | None:
        if self.initial is not None and self.initial.id == delivery_id:
            return self.initial
        return None

    async def get_with_attempts(self, delivery_id: UUID) -> Delivery | None:
        if self.initial is not None and self.initial.id == delivery_id:
            self.initial.attempts = list(self.attempts)
            return self.initial
        return None

    async def list_attempts(self, delivery_id: UUID):
        return self.attempts


@pytest.mark.asyncio
async def test_endpoint_service_reveals_generated_secret_once() -> None:
    repository = FakeEndpointRepository()
    service = EndpointService(
        repository,
        "http://receiver-lab:8100/webhooks",
        secret_factory=lambda: "ehsec_test",
    )

    result = await service.create(
        EndpointCreateRequest(name="Lab", url="http://receiver-lab:8100/webhooks")
    )

    assert result.plaintext_signing_secret == "ehsec_test"
    assert result.endpoint.signing_secret == b"ehsec_test"
    assert repository.created is result.endpoint


@pytest.mark.asyncio
async def test_endpoint_service_rejects_every_unconfigured_url() -> None:
    service = EndpointService(FakeEndpointRepository(), "http://receiver-lab:8100/webhooks")

    with pytest.raises(DomainError) as raised:
        await service.create(EndpointCreateRequest(name="Other", url="https://example.com/hook"))

    assert raised.value.status_code == 422
    assert raised.value.code == "receiver_url_not_allowed"


@pytest.mark.asyncio
async def test_publish_creates_exact_outbound_envelope_and_delivery() -> None:
    target = endpoint()
    request = EventPublishRequest(endpoint_id=target.id, type="invoice.paid", data={"amount": 42})
    events = FakeEventRepository(inserted=True)
    deliveries = FakeDeliveryRepository()
    service = EventService(FakeEndpointRepository(target), events, deliveries)

    result = await service.publish(request, "key-1")

    envelope = json.loads(result.event.payload_bytes)
    assert envelope == {
        "created_at": envelope["created_at"],
        "data": {"amount": 42},
        "id": str(result.event.id),
        "type": "invoice.paid",
    }
    assert envelope["created_at"].endswith("Z")
    assert result.event.payload_sha256 == sha256(result.event.payload_bytes).hexdigest()
    assert result.event.request_fingerprint_sha256 == canonical_sha256(
        {"endpoint_id": str(target.id), "type": "invoice.paid", "data": {"amount": 42}}
    )
    assert result.event.payload_sha256 != result.event.request_fingerprint_sha256
    assert result.delivery.endpoint_id == target.id
    assert result.idempotent_replay is False
    assert deliveries.created is result.delivery


@pytest.mark.asyncio
async def test_publish_returns_the_original_event_for_an_identical_retry() -> None:
    target = endpoint()
    request = EventPublishRequest(endpoint_id=target.id, type="invoice.paid", data={"amount": 42})
    existing = event_for(request, "same-key")
    initial = delivery_for(existing, target)
    service = EventService(
        FakeEndpointRepository(target),
        FakeEventRepository(inserted=False, existing=existing),
        FakeDeliveryRepository(initial),
    )

    result = await service.publish(request, "same-key")

    assert result.event is existing
    assert result.delivery is initial
    assert result.idempotent_replay is True


@pytest.mark.asyncio
async def test_publish_rejects_idempotency_key_reuse_with_different_request() -> None:
    target = endpoint()
    request = EventPublishRequest(endpoint_id=target.id, type="invoice.paid", data={"amount": 42})
    existing = event_for(request, "same-key", fingerprint="0" * 64)
    service = EventService(
        FakeEndpointRepository(target),
        FakeEventRepository(inserted=False, existing=existing),
        FakeDeliveryRepository(),
    )

    with pytest.raises(DomainError) as raised:
        await service.publish(request, "same-key")

    assert raised.value.status_code == 409
    assert raised.value.code == "idempotency_key_reused"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "disabled", "invalid_json"])
async def test_publish_rejects_invalid_targets_and_data(case: str) -> None:
    target = endpoint(enabled=case != "disabled")
    repository_target = None if case == "missing" else target
    data = {"bad": math.nan} if case == "invalid_json" else {}
    request = EventPublishRequest(endpoint_id=target.id, type="test.event", data=data)
    service = EventService(
        FakeEndpointRepository(repository_target),
        FakeEventRepository(inserted=True),
        FakeDeliveryRepository(),
    )

    with pytest.raises(DomainError) as raised:
        await service.publish(request, "key")

    expected = {
        "missing": "endpoint_not_found",
        "disabled": "endpoint_disabled",
        "invalid_json": "invalid_event_data",
    }
    assert raised.value.code == expected[case]


@pytest.mark.asyncio
async def test_query_service_returns_details_and_reports_missing_resources() -> None:
    target = endpoint()
    request = EventPublishRequest(endpoint_id=target.id, type="test.event", data={})
    existing = event_for(request, "key")
    initial = delivery_for(existing, target)
    events = FakeEventRepository(inserted=False, existing=existing)
    deliveries = FakeDeliveryRepository(initial)
    service = QueryService(events, deliveries)

    details = await service.event_details(existing.id)
    attempts = await service.delivery_attempts(initial.id)
    assert details.deliveries == [initial]
    assert attempts.delivery is initial
    assert attempts.attempts == []

    with pytest.raises(DomainError, match="does not exist"):
        await service.event_details(uuid4())
    with pytest.raises(DomainError, match="does not exist"):
        await service.delivery_attempts(uuid4())
