"""Safe, deterministic webhook receiver for development and demos."""

import asyncio
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Annotated

from fastapi import FastAPI, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from eventharbor.demo_runs import DEMO_RUN_ID_MAX_LENGTH, DEMO_RUN_ID_PATTERN

MAX_SCOPED_RUNS = 100
MAX_REQUESTS_PER_RUN = 100
CONTROL_REQUEST_LIMIT = 20


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


@dataclass(slots=True)
class ReceiverRunState:
    """Independent deterministic behavior and evidence for one demo run."""

    configuration: ReceiverConfiguration = field(default_factory=ReceiverConfiguration)
    scenario_attempts: int = 0
    sequence: int = 0
    requests: list[dict[str, object]] = field(default_factory=list)


class ReceiverState:
    """Bounded run registry plus a backward-compatible unscoped state."""

    def __init__(self) -> None:
        self.default = ReceiverRunState()
        self.scoped: OrderedDict[str, ReceiverRunState] = OrderedDict()
        self.lock = asyncio.Lock()

    async def reset(self) -> None:
        """Restore pristine in-memory state for tests or process-local tooling."""

        async with self.lock:
            self.default = ReceiverRunState()
            self.scoped.clear()

    def configured_state(self, run_id: str | None) -> ReceiverRunState:
        """Return or create an LRU-bounded state for a control-plane request."""

        if run_id is None:
            return self.default
        state = self.scoped.pop(run_id, None)
        if state is None:
            state = ReceiverRunState()
        self.scoped[run_id] = state
        while len(self.scoped) > MAX_SCOPED_RUNS:
            self.scoped.popitem(last=False)
        return state

    def webhook_state(self, run_id: str | None) -> ReceiverRunState:
        """Use an existing scoped state, falling back for legacy unscoped clients."""

        if run_id is None:
            return self.default
        state = self.scoped.pop(run_id, None)
        if state is None:
            return self.default
        self.scoped[run_id] = state
        return state


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
async def configure(
    configuration: ReceiverConfiguration,
    run_id: Annotated[
        str | None,
        Query(max_length=DEMO_RUN_ID_MAX_LENGTH, pattern=DEMO_RUN_ID_PATTERN),
    ] = None,
) -> dict[str, object]:
    async with receiver.lock:
        run = receiver.configured_state(run_id)
        run.configuration = configuration
        run.scenario_attempts = 0
    return {"configuration": configuration.model_dump(), "attempts": 0}


@app.get("/control", tags=["receiver lab"])
async def control_state(
    run_id: Annotated[
        str | None,
        Query(max_length=DEMO_RUN_ID_MAX_LENGTH, pattern=DEMO_RUN_ID_PATTERN),
    ] = None,
) -> dict[str, object]:
    """Expose bounded, non-secret evidence for the local Control Room."""

    async with receiver.lock:
        run = receiver.configured_state(run_id)
        return {
            "configuration": run.configuration.model_dump(),
            "attempts": run.scenario_attempts,
            "requests": list(run.requests[-CONTROL_REQUEST_LIMIT:]),
        }


@app.get("/requests", tags=["receiver lab"])
async def list_requests(
    run_id: Annotated[
        str | None,
        Query(max_length=DEMO_RUN_ID_MAX_LENGTH, pattern=DEMO_RUN_ID_PATTERN),
    ] = None,
) -> dict[str, object]:
    async with receiver.lock:
        run = receiver.configured_state(run_id)
        return {"count": len(run.requests), "requests": list(run.requests)}


def _payload_validation_error(body: bytes) -> tuple[str, str] | None:
    """Return the concrete Receiver Lab schema violation, if any."""

    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid_json", "The webhook body must be valid JSON."
    if not isinstance(payload, dict):
        return "invalid_payload", "The webhook body must be a JSON object."
    data = payload.get("data")
    if not isinstance(data, dict):
        return "invalid_data", "data must be a JSON object."
    customer_id = data.get("customer_id")
    if not isinstance(customer_id, str) or not customer_id.strip():
        return "missing_customer_id", "data.customer_id is required."
    return None


def _response_evidence(
    status_code: int,
    validation_error: tuple[str, str] | None,
) -> tuple[str, str]:
    if validation_error is not None:
        return validation_error
    if status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        return "rate_limited", "Receiver Lab asked the worker to retry after 2 seconds."
    if status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
        return "temporarily_unavailable", "Receiver Lab is in the controlled unavailable state."
    return "accepted", "Receiver Lab accepted the webhook."


@app.post("/webhooks", tags=["receiver lab"])
async def receive_webhook(
    request: Request,
    event_id: Annotated[str | None, Header(alias="X-EventHarbor-Event-Id")] = None,
    delivery_id: Annotated[str | None, Header(alias="X-EventHarbor-Delivery-Id")] = None,
    event_type: Annotated[str | None, Header(alias="X-EventHarbor-Event-Type")] = None,
    delivery_attempt: Annotated[int | None, Header(alias="X-EventHarbor-Attempt")] = None,
    request_timestamp: Annotated[
        int | None, Header(alias="X-EventHarbor-Timestamp")
    ] = None,
    signature: Annotated[str | None, Header(alias="X-EventHarbor-Signature")] = None,
    demo_run_id: Annotated[
        str | None,
        Header(
            alias="X-EventHarbor-Demo-Run-Id",
            max_length=DEMO_RUN_ID_MAX_LENGTH,
            pattern=DEMO_RUN_ID_PATTERN,
        ),
    ] = None,
) -> Response:
    body = await request.body()
    received_at = datetime.now(UTC)
    validation_error: tuple[str, str] | None = None

    async with receiver.lock:
        run = receiver.webhook_state(demo_run_id)
        run.scenario_attempts += 1
        run.sequence += 1
        attempt = run.scenario_attempts
        sequence = run.sequence
        configuration = run.configuration

        if configuration.mode == ReceiverMode.RATE_LIMITED:
            # A zero failure count preserves the sustained-rate-limit test mode.
            # A positive count creates a realistic, bounded 429 -> recovery story.
            rate_limit_active = (
                configuration.failures_before_success == 0
                or attempt <= configuration.failures_before_success
            )
            response_status_code = (
                status.HTTP_429_TOO_MANY_REQUESTS
                if rate_limit_active
                else status.HTTP_200_OK
            )
        elif configuration.mode == ReceiverMode.PERMANENT_FAILURE:
            validation_error = _payload_validation_error(body)
            response_status_code = (
                status.HTTP_400_BAD_REQUEST
                if validation_error is not None
                else status.HTTP_200_OK
            )
        elif (
            configuration.mode == ReceiverMode.FAIL_THEN_SUCCEED
            and attempt <= configuration.failures_before_success
        ):
            response_status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        else:
            response_status_code = status.HTTP_200_OK

        response_code, response_detail = _response_evidence(
            response_status_code,
            validation_error,
        )

        run.requests.append(
            {
                "sequence": sequence,
                "attempt": attempt,
                "event_id": event_id,
                "delivery_id": delivery_id,
                "event_type": event_type,
                "delivery_attempt": delivery_attempt,
                "request_timestamp": request_timestamp,
                "received_at": received_at,
                "response_status_code": response_status_code,
                "receiver_mode": configuration.mode.value,
                "signature_present": signature is not None,
                "body_preview": body[:1_024].decode("utf-8", errors="replace"),
                "body_sha256": sha256(body).hexdigest(),
            }
        )
        run.requests[:] = run.requests[-MAX_REQUESTS_PER_RUN:]

    if configuration.mode == ReceiverMode.TIMEOUT:
        await asyncio.sleep(configuration.delay_ms / 1_000)
    headers = (
        {"Retry-After": "2"}
        if response_status_code == status.HTTP_429_TOO_MANY_REQUESTS
        else None
    )
    return JSONResponse(
        status_code=response_status_code,
        content={"code": response_code, "detail": response_detail},
        headers=headers,
    )
