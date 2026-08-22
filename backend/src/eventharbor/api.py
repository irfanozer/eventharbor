"""HTTP API entry point."""

from fastapi import FastAPI

from eventharbor import __version__

app = FastAPI(
    title="EventHarbor API",
    description="Reliable and inspectable outbound webhook delivery.",
    version=__version__,
)


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    """Report process health without claiming dependency readiness."""

    return {"status": "healthy", "service": "eventharbor-api", "version": __version__}
