from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from eventharbor.control_room import (
    ControlRoomQueryService,
    Page,
    PageCursor,
    decode_cursor,
    encode_cursor,
)
from eventharbor.deliveries.state_machine import DeliveryStatus
from eventharbor.errors import DomainError
from eventharbor.models import Endpoint


def test_cursor_round_trip_preserves_utc_sort_keys() -> None:
    cursor = PageCursor(
        timestamp=datetime(2026, 8, 24, 16, 45, 12, 345678, tzinfo=UTC),
        row_id=UUID("00000000-0000-0000-0000-000000000321"),
    )

    encoded = encode_cursor(cursor)

    assert "=" not in encoded
    assert decode_cursor(encoded) == cursor


def test_cursor_normalizes_non_utc_timestamp_without_losing_instant() -> None:
    cursor = PageCursor(
        timestamp=datetime(2026, 8, 24, 8, 0, tzinfo=timezone(-timedelta(hours=4))),
        row_id=uuid4(),
    )

    decoded = decode_cursor(encode_cursor(cursor))

    assert decoded is not None
    assert decoded.timestamp.tzinfo is UTC
    assert decoded.timestamp == cursor.timestamp


@pytest.mark.parametrize(
    "value",
    [
        "not-base64!",
        "e30",  # {}
        "eyJ2IjoyLCJ0aW1lc3RhbXAiOiIyMDI2LTA4LTI0VDEyOjAwOjAwKzAwOjAwIiwiaWQiOiIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDEifQ",  # noqa: E501
        "eyJ2IjoxLCJ0aW1lc3RhbXAiOiIyMDI2LTA4LTI0VDEyOjAwOjAwIiwiaWQiOiIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDEifQ",  # noqa: E501
        "eyJ2IjoxLCJ0aW1lc3RhbXAiOiJub3QtYS10aW1lIiwiaWQiOiIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDEifQ",  # noqa: E501
        "eyJ2IjoxLCJ0aW1lc3RhbXAiOiIyMDI2LTA4LTI0VDEyOjAwOjAwKzAwOjAwIiwiaWQiOiJub3QtYS11dWlkIn0",  # noqa: E501
    ],
)
def test_invalid_cursor_is_reported_as_stable_domain_error(value: str) -> None:
    with pytest.raises(DomainError) as raised:
        decode_cursor(value)

    assert raised.value.status_code == 422
    assert raised.value.code == "invalid_cursor"
    assert "pagination cursor" in raised.value.detail.lower()


def test_absent_cursor_decodes_to_none() -> None:
    assert decode_cursor(None) is None


class RecordingRepository:
    def __init__(self) -> None:
        self.events_arguments: dict[str, object] | None = None
        self.endpoints_arguments: dict[str, object] | None = None
        self.dead_letters_arguments: dict[str, object] | None = None
        self.endpoint_result: Endpoint | None = None

    async def list_events(self, **kwargs):
        self.events_arguments = kwargs
        return Page(items=[], next_cursor=None)

    async def list_endpoints(self, **kwargs):
        self.endpoints_arguments = kwargs
        return Page(items=[], next_cursor=None)

    async def list_dead_letters(self, **kwargs):
        self.dead_letters_arguments = kwargs
        return Page(items=[], next_cursor=None)

    async def get_endpoint(self, endpoint_id):
        return self.endpoint_result


@pytest.mark.asyncio
async def test_query_service_decodes_cursors_and_forwards_filters() -> None:
    repository = RecordingRepository()
    service = ControlRoomQueryService(repository)  # type: ignore[arg-type]
    marker = PageCursor(datetime(2026, 8, 24, 12, 0, tzinfo=UTC), uuid4())
    encoded = encode_cursor(marker)
    endpoint_id = uuid4()

    await service.events(
        limit=17,
        cursor=encoded,
        event_type="invoice.paid",
        endpoint_id=endpoint_id,
        delivery_status=DeliveryStatus.DEAD_LETTERED,
    )
    await service.endpoints(limit=9, cursor=encoded)
    await service.dead_letters(limit=5, cursor=encoded, endpoint_id=endpoint_id)

    assert repository.events_arguments == {
        "limit": 17,
        "cursor": marker,
        "event_type": "invoice.paid",
        "endpoint_id": endpoint_id,
        "delivery_status": DeliveryStatus.DEAD_LETTERED,
    }
    assert repository.endpoints_arguments == {"limit": 9, "cursor": marker}
    assert repository.dead_letters_arguments == {
        "limit": 5,
        "cursor": marker,
        "endpoint_id": endpoint_id,
    }


@pytest.mark.asyncio
async def test_query_service_reports_missing_endpoint_without_leaking_storage_details() -> None:
    repository = RecordingRepository()
    service = ControlRoomQueryService(repository)  # type: ignore[arg-type]
    endpoint_id = uuid4()

    with pytest.raises(DomainError) as raised:
        await service.endpoint(endpoint_id)

    assert raised.value.status_code == 404
    assert raised.value.code == "endpoint_not_found"
    assert str(endpoint_id) in raised.value.detail
