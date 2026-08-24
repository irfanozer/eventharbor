from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from eventharbor.deliveries.state_machine import DeliveryAttemptStatus
from eventharbor.schemas import (
    DeliveryAttemptResponse,
    EndpointCreateRequest,
    EventPublishRequest,
    ReceiverLabRequestResponse,
)


def test_endpoint_name_is_trimmed_and_extra_fields_are_forbidden() -> None:
    request = EndpointCreateRequest(name="  Receiver Lab  ", url="http://receiver/webhooks")
    assert request.name == "Receiver Lab"

    with pytest.raises(ValidationError):
        EndpointCreateRequest.model_validate(
            {"name": "Lab", "url": "http://receiver/webhooks", "unexpected": True}
        )


@pytest.mark.parametrize("name", ["", "   "])
def test_endpoint_name_must_not_be_blank(name: str) -> None:
    with pytest.raises(ValidationError):
        EndpointCreateRequest(name=name, url="http://receiver/webhooks")


@pytest.mark.parametrize("event_type", ["Invoice.Paid", "1invoice.paid", "invoice paid", ""])
def test_event_type_uses_machine_readable_contract(event_type: str) -> None:
    with pytest.raises(ValidationError):
        EventPublishRequest(endpoint_id=uuid4(), type=event_type, data={})


def test_event_type_is_trimmed_before_validation() -> None:
    request = EventPublishRequest(endpoint_id=uuid4(), type=" invoice.paid ", data={})
    assert request.type == "invoice.paid"


@pytest.mark.parametrize(
    "status",
    [DeliveryAttemptStatus.IN_PROGRESS, DeliveryAttemptStatus.INDETERMINATE],
)
def test_unresolved_attempt_evidence_allows_unknown_transport_fields(
    status: DeliveryAttemptStatus,
) -> None:
    now = datetime.now(UTC)
    response = DeliveryAttemptResponse(
        id=uuid4(),
        attempt_number=2,
        status=status,
        disposition=None,
        http_status_code=None,
        error_type="lease_expired" if status == DeliveryAttemptStatus.INDETERMINATE else None,
        error_message=None,
        response_body_excerpt=None,
        duration_ms=None,
        request_timestamp=None,
        retry_scheduled_for=None,
        started_at=None,
        finished_at=None,
        resolved_at=now if status == DeliveryAttemptStatus.INDETERMINATE else None,
        created_at=now,
    )

    assert response.status == status
    assert response.disposition is None
    assert response.started_at is None


def receiver_observation(**overrides) -> dict[str, object]:
    observation: dict[str, object] = {
        "sequence": 5,
        "attempt": 1,
        "event_id": "event-1",
        "delivery_id": "delivery-1",
        "event_type": "demo.order.ready",
        "delivery_attempt": 1,
        "request_timestamp": 1_700_000_000,
        "received_at": datetime.now(UTC),
        "response_status_code": 503,
        "receiver_mode": "fail_then_succeed",
        "signature_present": True,
        "body_preview": '{"order_id":"ORDER-1"}',
        "body_sha256": "a" * 64,
    }
    observation.update(overrides)
    return observation


def test_receiver_observation_validates_correlated_network_evidence() -> None:
    response = ReceiverLabRequestResponse.model_validate(receiver_observation())

    assert response.sequence == 5
    assert response.delivery_id == "delivery-1"
    assert response.delivery_attempt == 1
    assert response.response_status_code == 503
    assert response.received_at.tzinfo is not None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sequence", 0),
        ("attempt", 0),
        ("delivery_attempt", 0),
        ("request_timestamp", -1),
        ("response_status_code", 99),
        ("response_status_code", 600),
        ("body_sha256", "not-a-sha256"),
    ],
)
def test_receiver_observation_rejects_invalid_evidence(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ReceiverLabRequestResponse.model_validate(receiver_observation(**{field: value}))
