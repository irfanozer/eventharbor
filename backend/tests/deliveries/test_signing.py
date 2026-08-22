import pytest
from hypothesis import given
from hypothesis import strategies as st

from eventharbor.deliveries.signing import sign_payload, verify_signature


def test_signature_round_trip_uses_exact_payload_bytes() -> None:
    secret = b"development-secret"
    payload = b'{"type":"invoice.paid"}'
    timestamp = 1_700_000_000

    signature = sign_payload(secret, timestamp, payload)

    assert signature.startswith("v1=")
    assert verify_signature(secret, timestamp, payload, signature)
    assert not verify_signature(secret, timestamp, payload + b" ", signature)


@given(payload=st.binary(max_size=4_096), timestamp=st.integers(min_value=0, max_value=2**63 - 1))
def test_arbitrary_payload_bytes_round_trip(payload: bytes, timestamp: int) -> None:
    secret = b"property-test-secret"

    signature = sign_payload(secret, timestamp, payload)

    assert verify_signature(secret, timestamp, payload, signature)


def test_signing_rejects_invalid_secret_and_timestamp() -> None:
    with pytest.raises(ValueError, match="secret"):
        sign_payload(b"", 1, b"payload")
    with pytest.raises(ValueError, match="timestamp"):
        sign_payload(b"secret", -1, b"payload")
