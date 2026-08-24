import pytest

from eventharbor.deliveries.state_machine import (
    DeliveryStatus,
    InvalidDeliveryTransition,
    ensure_transition,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (DeliveryStatus.PENDING, DeliveryStatus.IN_PROGRESS),
        (DeliveryStatus.PENDING, DeliveryStatus.DEAD_LETTERED),
        (DeliveryStatus.IN_PROGRESS, DeliveryStatus.DELIVERED),
        (DeliveryStatus.IN_PROGRESS, DeliveryStatus.RETRY_WAIT),
        (DeliveryStatus.IN_PROGRESS, DeliveryStatus.DEAD_LETTERED),
        (DeliveryStatus.RETRY_WAIT, DeliveryStatus.IN_PROGRESS),
        (DeliveryStatus.RETRY_WAIT, DeliveryStatus.DEAD_LETTERED),
    ],
)
def test_documented_transitions_are_allowed(
    current: DeliveryStatus,
    target: DeliveryStatus,
) -> None:
    ensure_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (DeliveryStatus.PENDING, DeliveryStatus.DELIVERED),
        (DeliveryStatus.DELIVERED, DeliveryStatus.IN_PROGRESS),
        (DeliveryStatus.DEAD_LETTERED, DeliveryStatus.PENDING),
    ],
)
def test_shortcuts_and_terminal_state_mutation_are_rejected(
    current: DeliveryStatus,
    target: DeliveryStatus,
) -> None:
    with pytest.raises(InvalidDeliveryTransition):
        ensure_transition(current, target)
