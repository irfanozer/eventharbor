import json

import pytest
from starlette.requests import Request

from eventharbor.errors import DomainError, domain_error_handler


@pytest.mark.asyncio
async def test_domain_error_uses_problem_details_shape() -> None:
    request = Request({"type": "http", "method": "GET", "path": "/v1/missing", "headers": []})
    error = DomainError(404, "thing_not_found", "Thing not found", "No such thing.")

    response = await domain_error_handler(request, error)

    assert response.status_code == 404
    assert response.media_type == "application/problem+json"
    assert json.loads(response.body) == {
        "type": "urn:eventharbor:problem:thing_not_found",
        "title": "Thing not found",
        "status": 404,
        "detail": "No such thing.",
        "instance": "/v1/missing",
        "code": "thing_not_found",
    }
    assert str(error) == "No such thing."


@pytest.mark.asyncio
async def test_handler_does_not_hide_unexpected_exceptions() -> None:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    error = RuntimeError("unexpected")

    with pytest.raises(RuntimeError, match="unexpected"):
        await domain_error_handler(request, error)
