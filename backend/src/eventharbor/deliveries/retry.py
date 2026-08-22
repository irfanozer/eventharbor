"""Retry classification and deterministic backoff calculations."""

from dataclasses import dataclass
from enum import StrEnum


class DeliveryDisposition(StrEnum):
    """The worker's decision after a delivery attempt."""

    SUCCEEDED = "succeeded"
    RETRY = "retry"
    TERMINAL_FAILURE = "terminal_failure"


RETRYABLE_STATUS_CODES = frozenset({408, 425, 429})


def classify_status_code(status_code: int) -> DeliveryDisposition:
    """Classify an HTTP response without mutating delivery state."""

    if not 100 <= status_code <= 599:
        raise ValueError("status_code must be between 100 and 599")
    if 200 <= status_code <= 299:
        return DeliveryDisposition.SUCCEEDED
    if status_code in RETRYABLE_STATUS_CODES or status_code >= 500:
        return DeliveryDisposition.RETRY
    return DeliveryDisposition.TERMINAL_FAILURE


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """A bounded exponential-backoff policy using full jitter."""

    max_attempts: int = 8
    base_delay_seconds: float = 10.0
    max_delay_seconds: float = 3600.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be positive")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be at least base_delay_seconds")

    def has_attempt_remaining(self, completed_attempts: int) -> bool:
        if completed_attempts < 0:
            raise ValueError("completed_attempts cannot be negative")
        return completed_attempts < self.max_attempts

    def delay_seconds(self, completed_attempts: int, random_fraction: float) -> float:
        """Return full-jitter delay after ``completed_attempts`` failures.

        ``random_fraction`` is injected so tests and failure demonstrations can
        use a reproducible seed. Production code will supply a secure random
        value in the inclusive range from zero to one.
        """

        if completed_attempts < 1:
            raise ValueError("completed_attempts must be at least 1")
        if not 0.0 <= random_fraction <= 1.0:
            raise ValueError("random_fraction must be between 0 and 1")

        exponential_delay = self.base_delay_seconds * (2.0 ** (completed_attempts - 1))
        ceiling = min(self.max_delay_seconds, exponential_delay)
        return ceiling * random_fraction
