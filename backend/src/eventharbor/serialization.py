"""Deterministic JSON encoding for hashes, storage, and webhook bodies."""

import json
from hashlib import sha256
from typing import Any


class CanonicalJSONError(ValueError):
    """Raised when a value cannot be represented as strict JSON."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return a stable UTF-8 representation of one JSON-compatible value.

    Sorting object keys and removing insignificant whitespace ensures that
    semantically identical objects have the same bytes regardless of the key
    order supplied by a publisher. Non-finite floats are rejected because they
    are not part of the JSON standard.
    """

    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CanonicalJSONError("value is not valid JSON") from exc
    return encoded.encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Return the lowercase SHA-256 hex digest of canonical JSON bytes."""

    return sha256(canonical_json_bytes(value)).hexdigest()
