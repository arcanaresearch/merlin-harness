from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SECRET_KEYS = frozenset(
    {"api_key", "authorization", "credential", "credentials", "password", "secret", "token"}
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


def sanitize(value: Any, *, key: str = "") -> Any:
    """Return a portable value with common secret material removed."""
    normalized = key.casefold().replace("-", "_")
    if any(part in normalized for part in _SECRET_KEYS):
        return "[redacted]"
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize(item, key=str(item_key)) for item_key, item in value.items()
        }
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [sanitize(item) for item in value]
    return value


def _sanitize_text(value: str) -> str:
    redacted = _BEARER.sub("Bearer [redacted]", value)
    try:
        parsed = urlsplit(redacted)
    except ValueError:
        return redacted
    if (
        parsed.scheme
        and parsed.hostname
        and (parsed.username is not None or parsed.password is not None)
    ):
        host = parsed.hostname
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        parsed = parsed._replace(netloc=host)
    if parsed.scheme and parsed.hostname and parsed.query:
        # ``urlencode`` accepted an arbitrary iterable on older Python releases,
        # but Python 3.13 requires a concrete sequence or mapping.  Readiness
        # projections routinely sanitize target URLs while resuming a session,
        # so a generator here made any URL with a query string abort resume.
        query = urlencode(
            [(name, sanitize(item, key=name)) for name, item in parse_qsl(parsed.query)]
        )
        parsed = parsed._replace(query=query)
    return urlunsplit(parsed) if parsed.scheme and parsed.hostname else redacted


@dataclass(frozen=True, slots=True)
class LogEntry:
    occurred_at: datetime
    level: str
    message: str
    context: Mapping[str, Any]

    @classmethod
    def create(
        cls, occurred_at: datetime, level: str, message: str, context: Mapping[str, Any] = ()
    ) -> LogEntry:
        return cls(occurred_at, level, sanitize(message), sanitize(context))


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    occurred_at: datetime
    kind: str
    summary: str
    metadata: Mapping[str, Any]

    @classmethod
    def create(
        cls, occurred_at: datetime, kind: str, summary: str, metadata: Mapping[str, Any] = ()
    ) -> HistoryEntry:
        return cls(occurred_at, kind, sanitize(summary), sanitize(metadata))
