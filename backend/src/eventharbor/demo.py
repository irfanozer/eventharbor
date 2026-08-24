"""Safe orchestration of deterministic Receiver Lab demo scenarios."""

from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from eventharbor.errors import DomainError
from eventharbor.schemas import (
    ReceiverLabConfigurationResponse,
    ReceiverLabPreset,
    ReceiverLabRequestResponse,
    ReceiverLabStateResponse,
)


@dataclass(frozen=True, slots=True)
class ReceiverScenario:
    """One server-owned Receiver Lab configuration."""

    mode: str
    failures_before_success: int
    delay_ms: int

    def as_payload(self) -> dict[str, str | int]:
        return {
            "mode": self.mode,
            "failures_before_success": self.failures_before_success,
            "delay_ms": self.delay_ms,
        }


SCENARIOS: dict[ReceiverLabPreset, ReceiverScenario] = {
    ReceiverLabPreset.SUCCESS: ReceiverScenario("success", 0, 0),
    ReceiverLabPreset.RETRY_THEN_RECOVER: ReceiverScenario("fail_then_succeed", 2, 0),
    ReceiverLabPreset.RATE_LIMITED: ReceiverScenario("rate_limited", 0, 0),
    ReceiverLabPreset.TIMEOUT: ReceiverScenario("timeout", 0, 7_000),
    ReceiverLabPreset.PERMANENT_FAILURE: ReceiverScenario("permanent_failure", 0, 0),
    # Receiver Lab permits at most twenty seeded failures. That is intentionally
    # greater than EventHarbor's local worker attempt budget, guaranteeing a DLQ item.
    ReceiverLabPreset.DEAD_LETTER: ReceiverScenario("fail_then_succeed", 20, 0),
}


class ReceiverLabDemoService:
    """Expose only named demo presets; never accept an operator-supplied URL."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def state(self) -> ReceiverLabStateResponse:
        payload = await self._request("GET", "/control")
        try:
            configuration = ReceiverLabConfigurationResponse.model_validate(
                payload["configuration"]
            )
            attempts = int(payload["attempts"])
            requests = [
                ReceiverLabRequestResponse.model_validate(item)
                for item in payload.get("requests", [])[-20:]
            ]
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise self._unavailable() from exc

        return ReceiverLabStateResponse(
            preset=self._identify_preset(configuration),
            configuration=configuration,
            attempts=attempts,
            requests=requests,
        )

    async def configure(self, preset: ReceiverLabPreset) -> ReceiverLabStateResponse:
        await self._request("PUT", "/control", json=SCENARIOS[preset].as_payload())
        return await self.state()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, json=json)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise self._unavailable() from exc
        if not isinstance(payload, dict):
            raise self._unavailable()
        return payload

    @staticmethod
    def _identify_preset(
        configuration: ReceiverLabConfigurationResponse,
    ) -> ReceiverLabPreset | None:
        for preset, scenario in SCENARIOS.items():
            if (
                configuration.mode == scenario.mode
                and configuration.failures_before_success == scenario.failures_before_success
                and configuration.delay_ms == scenario.delay_ms
            ):
                return preset
        return None

    @staticmethod
    def _unavailable() -> DomainError:
        return DomainError(
            status_code=503,
            code="receiver_lab_unavailable",
            title="Receiver Lab is unavailable",
            detail=(
                "The local Receiver Lab did not return a valid response. Check its service health."
            ),
        )
