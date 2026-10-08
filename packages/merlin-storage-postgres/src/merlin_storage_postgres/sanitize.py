from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_NUL = "\x00"
_NUL_ESCAPE = "\\u0000"


def escape_nul(value: Any) -> Any:
    """Escape NUL (U+0000) so PostgreSQL text/jsonb parameters accept the value.

    PostgreSQL rejects NUL in both text parameters and jsonb strings; model
    tool-call arguments and tool results can legitimately carry it. The byte is
    preserved as its literal JSON escape text rather than dropped.
    """
    if isinstance(value, str):
        return value.replace(_NUL, _NUL_ESCAPE) if _NUL in value else value
    if isinstance(value, Mapping):
        return {escape_nul(key): escape_nul(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [escape_nul(item) for item in value]
    return value
