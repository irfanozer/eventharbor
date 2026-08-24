"""Shared validation for process-local Receiver Lab demo isolation."""

import re

DEMO_RUN_ID_MAX_LENGTH = 128
DEMO_RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_DEMO_RUN_ID = re.compile(DEMO_RUN_ID_PATTERN)


def normalized_demo_run_id(value: object) -> str | None:
    """Return a bounded header-safe run ID, or ``None`` for non-demo payloads."""

    if not isinstance(value, str) or _DEMO_RUN_ID.fullmatch(value) is None:
        return None
    return value
