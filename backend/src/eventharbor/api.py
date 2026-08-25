"""HTTP API entry point."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from eventharbor import __version__
from eventharbor.config import Settings, get_settings
from eventharbor.database import engine
from eventharbor.errors import DomainError, domain_error_handler
from eventharbor.routers.v1 import router as v1_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Release pooled database connections during a graceful revision shutdown."""

    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(
    title="EventHarbor API",
    description="Reliable and inspectable outbound webhook delivery.",
    version=__version__,
    lifespan=lifespan,
)
app.add_exception_handler(DomainError, domain_error_handler)
app.include_router(v1_router)


@app.get("/health", tags=["operations"])
async def health(response: Response) -> dict[str, str]:
    """Report process health without claiming dependency readiness."""

    response.headers["Cache-Control"] = "no-store"
    return {"status": "healthy", "service": "eventharbor-api", "version": __version__}


async def _check_database_connection() -> None:
    """Execute the smallest useful database readiness check."""

    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


@app.get(
    "/ready",
    tags=["operations"],
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Database unavailable"}},
)
async def ready(settings: Annotated[Settings, Depends(get_settings)]) -> JSONResponse:
    """Report readiness only when PostgreSQL responds within the configured deadline."""

    try:
        async with asyncio.timeout(settings.readiness_timeout_seconds):
            await _check_database_connection()
    except Exception as exc:
        logger.warning(
            "database readiness check failed",
            extra={"error_type": type(exc).__name__},
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Cache-Control": "no-store"},
            content={
                "status": "unavailable",
                "service": "eventharbor-api",
                "dependency": "postgresql",
            },
        )

    return JSONResponse(
        headers={"Cache-Control": "no-store"},
        content={
            "status": "ready",
            "service": "eventharbor-api",
            "dependency": "postgresql",
        },
    )
