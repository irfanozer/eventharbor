"""HTTP API entry point."""

from fastapi import FastAPI

from eventharbor import __version__
from eventharbor.errors import DomainError, domain_error_handler
from eventharbor.routers.v1 import router as v1_router

app = FastAPI(
    title="EventHarbor API",
    description="Reliable and inspectable outbound webhook delivery.",
    version=__version__,
)
app.add_exception_handler(DomainError, domain_error_handler)
app.include_router(v1_router)


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    """Report process health without claiming dependency readiness."""

    return {"status": "healthy", "service": "eventharbor-api", "version": __version__}
