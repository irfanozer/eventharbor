import math

import pytest

from eventharbor.serialization import CanonicalJSONError, canonical_json_bytes, canonical_sha256


def test_canonical_json_is_order_independent_and_utf8() -> None:
    first = {"z": 1, "message": "İstanbul", "nested": {"b": True, "a": None}}
    second = {"nested": {"a": None, "b": True}, "message": "İstanbul", "z": 1}

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert canonical_json_bytes(first).decode() == (
        '{"message":"İstanbul","nested":{"a":null,"b":true},"z":1}'
    )
    assert canonical_sha256(first) == canonical_sha256(second)


@pytest.mark.parametrize("value", [{"bad": math.nan}, {"bad": object()}])
def test_canonical_json_rejects_non_json_values(value: object) -> None:
    with pytest.raises(CanonicalJSONError, match="valid JSON"):
        canonical_json_bytes(value)
