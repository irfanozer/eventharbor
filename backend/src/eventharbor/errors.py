"""Domain failures rendered as stable Problem Details responses."""

from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse


@dataclass(eq=False)
class DomainError(Exception):
    """A safe, intentional failure that can be returned to an API client."""

    status_code: int
    code: str
    title: str
    detail: str

    def __post_init__(self) -> None:
        super().__init__(self.detail)


async def domain_error_handler(request: Request, error: Exception) -> JSONResponse:
    """Render a domain failure using an RFC 9457-compatible JSON shape."""

    if not isinstance(error, DomainError):
        raise error
    return JSONResponse(
        status_code=error.status_code,
        media_type="application/problem+json",
        content={
            "type": f"urn:eventharbor:problem:{error.code}",
            "title": error.title,
            "status": error.status_code,
            "detail": error.detail,
            "instance": request.url.path,
            "code": error.code,
        },
    )
