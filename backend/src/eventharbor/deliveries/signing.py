"""HMAC helpers for proving webhook authenticity."""

import hashlib
import hmac

SIGNATURE_VERSION = "v1"


def sign_payload(secret: bytes, timestamp: int, payload: bytes) -> str:
    """Sign the exact payload bytes and timestamp with HMAC-SHA256."""

    if not secret:
        raise ValueError("secret cannot be empty")
    if timestamp < 0:
        raise ValueError("timestamp cannot be negative")

    signed_content = str(timestamp).encode("ascii") + b"." + payload
    digest = hmac.new(secret, signed_content, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def verify_signature(secret: bytes, timestamp: int, payload: bytes, signature: str) -> bool:
    """Verify a signature using constant-time comparison."""

    expected = sign_payload(secret, timestamp, payload)
    return hmac.compare_digest(expected, signature)
