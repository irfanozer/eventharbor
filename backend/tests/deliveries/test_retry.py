import pytest
from hypothesis import given
from hypothesis import strategies as st

from eventharbor.deliveries.retry import (
    DeliveryDisposition,
    RetryPolicy,
    classify_status_code,
)


@pytest.mark.parametrize("status_code", [200, 201, 204, 299])
def test_successful_responses_are_terminal_successes(status_code: int) -> None:
    assert classify_status_code(status_code) == DeliveryDisposition.SUCCEEDED


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 503, 599])
def test_transient_responses_are_retryable(status_code: int) -> None:
    assert classify_status_code(status_code) == DeliveryDisposition.RETRY


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
def test_other_client_errors_are_terminal(status_code: int) -> None:
    assert classify_status_code(status_code) == DeliveryDisposition.TERMINAL_FAILURE


def test_backoff_is_bounded_and_reproducible() -> None:
    policy = RetryPolicy(base_delay_seconds=10, max_delay_seconds=60)

    assert policy.delay_seconds(completed_attempts=1, random_fraction=0.5) == 5
    assert policy.delay_seconds(completed_attempts=4, random_fraction=1.0) == 60
    assert policy.delay_seconds(completed_attempts=12, random_fraction=1.0) == 60


@given(
    completed_attempts=st.integers(min_value=1, max_value=100),
    random_fraction=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
def test_backoff_never_exceeds_its_configured_cap(
    completed_attempts: int,
    random_fraction: float,
) -> None:
    policy = RetryPolicy(base_delay_seconds=2, max_delay_seconds=60)

    delay = policy.delay_seconds(completed_attempts, random_fraction)

    assert 0 <= delay <= policy.max_delay_seconds


@pytest.mark.parametrize("status_code", [0, 99, 600, 1_000])
def test_invalid_http_status_codes_are_rejected(status_code: int) -> None:
    with pytest.raises(ValueError, match="status_code"):
        classify_status_code(status_code)


@pytest.mark.parametrize(
    "arguments",
    [
        {"max_attempts": 0},
        {"base_delay_seconds": 0},
        {"base_delay_seconds": 10, "max_delay_seconds": 5},
    ],
)
def test_invalid_retry_policies_are_rejected(arguments: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**arguments)


def test_attempt_bounds_are_explicit() -> None:
    policy = RetryPolicy(max_attempts=3)

    assert policy.has_attempt_remaining(0)
    assert policy.has_attempt_remaining(2)
    assert not policy.has_attempt_remaining(3)

    with pytest.raises(ValueError, match="negative"):
        policy.has_attempt_remaining(-1)


@pytest.mark.parametrize(
    ("completed_attempts", "random_fraction"),
    [(0, 0.5), (1, -0.1), (1, 1.1)],
)
def test_invalid_backoff_inputs_are_rejected(
    completed_attempts: int,
    random_fraction: float,
) -> None:
    with pytest.raises(ValueError):
        RetryPolicy().delay_seconds(completed_attempts, random_fraction)
