from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from eventharbor.deliveries.state_machine import DeliveryAttemptStatus
from eventharbor.schemas import (
    DeliveryAttemptResponse,
    EndpointCreateRequest,
    EventPublishRequest,
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
