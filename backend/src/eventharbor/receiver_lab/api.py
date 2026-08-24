"""Safe, deterministic webhook receiver for development and demos."""

import asyncio
from enum import StrEnum
from typing import Annotated

from fastapi import FastAPI, Header, Request, Response, status
from pydantic import BaseModel, Field


class ReceiverMode(StrEnum):
    SUCCESS = "success"
    FAIL_THEN_SUCCEED = "fail_then_succeed"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    PERMANENT_FAILURE = "permanent_failure"


class ReceiverConfiguration(BaseModel):
    mode: ReceiverMode = ReceiverMode.SUCCESS
    failures_before_success: int = Field(default=0, ge=0, le=20)
    delay_ms: int = Field(default=0, ge=0, le=30_000)


class ReceiverState:
    def __init__(self) -> None:
        self.configuration = ReceiverConfiguration()
        self.attempts = 0
        self.requests: list[dict[str, object]] = []
        self.lock = asyncio.Lock()


receiver = ReceiverState()
app = FastAPI(
    title="EventHarbor Receiver Lab",
    description="Deterministic webhook failures for local development and demonstrations.",
    version="0.1.0",
)


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": "eventharbor-receiver-lab"}


@app.put("/control", tags=["receiver lab"])
async def configure(configuration: ReceiverConfiguration) -> dict[str, object]:
    async with receiver.lock:
        receiver.configuration = configuration
        receiver.attempts = 0
        receiver.requests.clear()
    return {"configuration": configuration.model_dump(), "attempts": 0}


@app.get("/control", tags=["receiver lab"])
async def control_state() -> dict[str, object]:
    """Expose bounded, non-secret evidence for the local Control Room."""

    async with receiver.lock:
        return {
            "configuration": receiver.configuration.model_dump(),
            "attempts": receiver.attempts,
            "requests": list(receiver.requests[-20:]),
        }


@app.get("/requests", tags=["receiver lab"])
async def list_requests() -> dict[str, object]:
    async with receiver.lock:
        return {"count": len(receiver.requests), "requests": list(receiver.requests)}


@app.post("/webhooks", tags=["receiver lab"])
async def receive_webhook(
    request: Request,
    event_id: Annotated[str | None, Header(alias="X-EventHarbor-Event-Id")] = None,
    signature: Annotated[str | None, Header(alias="X-EventHarbor-Signature")] = None,
) -> Response:
    body = await request.body()

    async with receiver.lock:
        receiver.attempts += 1
        attempt = receiver.attempts
        configuration = receiver.configuration
        receiver.requests.append(
            {
                "attempt": attempt,
                "event_id": event_id,
                "signature_present": signature is not None,
                "body_preview": body[:1_024].decode("utf-8", errors="replace"),
            }
        )
        receiver.requests[:] = receiver.requests[-100:]

    if configuration.mode == ReceiverMode.TIMEOUT:
        await asyncio.sleep(configuration.delay_ms / 1_000)
        return Response(status_code=status.HTTP_200_OK)
    if configuration.mode == ReceiverMode.RATE_LIMITED:
        return Response(status_code=status.HTTP_429_TOO_MANY_REQUESTS, headers={"Retry-After": "2"})
    if configuration.mode == ReceiverMode.PERMANENT_FAILURE:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)
    if (
        configuration.mode == ReceiverMode.FAIL_THEN_SUCCEED
        and attempt <= configuration.failures_before_success
    ):
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response(status_code=status.HTTP_200_OK)
