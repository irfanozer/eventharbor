"""Explicit delivery states and legal transitions."""

from enum import StrEnum


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RETRY_WAIT = "retry_wait"
    DELIVERED = "delivered"
    DEAD_LETTERED = "dead_lettered"


class DeliveryAttemptStatus(StrEnum):
    """Durable lifecycle of one reserved outbound attempt number."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    INDETERMINATE = "indeterminate"


class InvalidDeliveryTransition(ValueError):
    """Raised when code attempts a forbidden delivery transition."""


ALLOWED_TRANSITIONS: dict[DeliveryStatus, frozenset[DeliveryStatus]] = {
    DeliveryStatus.PENDING: frozenset({DeliveryStatus.IN_PROGRESS, DeliveryStatus.DEAD_LETTERED}),
    DeliveryStatus.IN_PROGRESS: frozenset(
        {
            DeliveryStatus.DELIVERED,
            DeliveryStatus.RETRY_WAIT,
            DeliveryStatus.DEAD_LETTERED,
        }
    ),
    DeliveryStatus.RETRY_WAIT: frozenset(
        {DeliveryStatus.IN_PROGRESS, DeliveryStatus.DEAD_LETTERED}
    ),
    DeliveryStatus.DELIVERED: frozenset(),
    DeliveryStatus.DEAD_LETTERED: frozenset(),
}


def ensure_transition(current: DeliveryStatus, target: DeliveryStatus) -> None:
    """Reject state changes that violate the documented delivery lifecycle."""

    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidDeliveryTransition(f"cannot transition from {current} to {target}")
