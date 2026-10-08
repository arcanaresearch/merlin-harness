from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .sanitize import sanitize


class OperationalJournal:
    """Owner-only, local, sanitized JSONL diagnostics with bounded retention."""

    def __init__(
        self,
        path: Path,
        *,
        max_entries: int = 10_000,
        max_age_days: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 100 <= max_entries <= 1_000_000:
            raise ValueError("operational log retention entries must be between 100 and 1000000")
        if not 1 <= max_age_days <= 365:
            raise ValueError("operational log retention days must be between 1 and 365")
        self.path = path
        self.max_entries = max_entries
        self.max_age_days = max_age_days
        self._clock = clock or (lambda: datetime.now(UTC))

    def append(self, level: str, message: str, context: Mapping[str, Any] = ()) -> None:
        now = self._clock()
        row = sanitize(
            {
                "occurred_at": now.isoformat(),
                "level": level,
                "message": message,
                "context": dict(context),
            }
        )
        rows = [*self._read(), row]
        self._write(self._retained(rows, now))

    def list(self, *, limit: int = 100) -> list[Mapping[str, Any]]:
        if not 1 <= limit <= 10_000:
            raise ValueError("operational log limit must be between 1 and 10000")
        now = self._clock()
        retained = self._retained(self._read(), now)
        self._write(retained)
        return retained[-limit:]

    def _read(self) -> list[Mapping[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[Mapping[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(sanitize(value))
        return rows

    def _retained(self, rows: list[Mapping[str, Any]], now: datetime) -> list[Mapping[str, Any]]:
        cutoff = now - timedelta(days=self.max_age_days)
        retained = []
        for row in rows:
            try:
                occurred = datetime.fromisoformat(str(row["occurred_at"]))
            except (KeyError, TypeError, ValueError):
                continue
            if occurred.tzinfo is not None and occurred >= cutoff:
                retained.append(row)
        return retained[-self.max_entries :]

    def _write(self, rows: list[Mapping[str, Any]]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
