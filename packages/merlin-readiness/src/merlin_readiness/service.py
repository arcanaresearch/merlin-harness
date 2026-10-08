from __future__ import annotations

import inspect
import json
import os
import zipfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .diagnostics import Doctor
from .export import export_config
from .resume import ResumeOrchestrator
from .sanitize import sanitize

Rows = Callable[..., Sequence[Any] | Awaitable[Sequence[Any]]]


class ReadinessService:
    """Sanitized diagnostics, operational history, and restart projection boundary."""

    def __init__(
        self,
        *,
        doctor: Doctor,
        resume: ResumeOrchestrator,
        config: Mapping[str, Any],
        logs: Rows | None = None,
        history: Rows | None = None,
        schedules: Rows | None = None,
        sessions: Rows | None = None,
        session_count: Callable[..., int | Awaitable[int]] | None = None,
        context: Callable[[str], Mapping[str, Any] | Awaitable[Mapping[str, Any]]] | None = None,
    ) -> None:
        self._doctor = doctor
        self._resume = resume
        self._config = dict(config)
        self._logs = logs
        self._history = history
        self._schedules = schedules
        self._sessions = sessions
        self._session_count = session_count
        self._context = context

    async def doctor(self) -> Mapping[str, Any]:
        report = await self._doctor.run()
        return {"healthy": report.healthy, "checks": [asdict(check) for check in report.checks]}

    async def logs(self, *, limit: int = 100) -> Sequence[Mapping[str, Any]]:
        return await self._rows(self._logs, limit=limit)

    async def history(
        self, session_id: str | None, *, limit: int = 100
    ) -> Sequence[Mapping[str, Any]]:
        return await self._rows(self._history, session_id, limit=limit)

    async def resume(self, session_id: str) -> Mapping[str, Any]:
        resumed = await self._resume.resume(session_id)
        schedules = await self._rows(self._schedules, session_id) if self._schedules else []
        return sanitize(
            {
                "session_id": resumed.session_id,
                "recovered": resumed.recovered,
                "messages": [_plain(message) for message in resumed.messages],
                "schedules": schedules,
            }
        )

    async def export_config(self) -> str:
        return export_config(self._config)

    async def support_bundle(self, output: str, session_id: str | None = None) -> str:
        """Write bounded sanitized diagnostics without transcripts or target evidence."""
        path = Path(output).expanduser().resolve()  # noqa: ASYNC240
        if path.suffix.casefold() != ".zip":
            path = path.with_suffix(".zip")
        if path.exists():
            raise FileExistsError(f"support bundle already exists: {path}")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        logs = await self.logs(limit=1_000)
        history = await self.history(session_id, limit=1_000) if session_id else []
        safe_history = [
            {
                key: row[key]
                for key in ("occurred_at", "kind", "summary", "level", "sequence")
                if key in row
            }
            for row in history
        ]
        payloads = {
            "manifest.json": {
                "created_at": datetime.now(UTC).isoformat(),
                "privacy": (
                    "local-only; sanitized; no transcripts, screenshots, credentials, "
                    "or target evidence"
                ),
                "retention": {"logs": len(logs), "history": len(safe_history)},
                "session_id": session_id,
            },
            "doctor.json": await self.doctor(),
            "config.json": json.loads(await self.export_config()),
            "logs.json": logs,
            "history.json": safe_history,
        }
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for name, value in payloads.items():
                    bundle.writestr(
                        name,
                        json.dumps(sanitize(value), indent=2, sort_keys=True) + "\n",
                    )
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return str(path)

    async def sessions(
        self, *, limit: int = 50, offset: int = 0, query: str = ""
    ) -> Sequence[Mapping[str, Any]]:
        if limit == 50 and offset == 0 and not query:
            return await self._rows(self._sessions)
        return await self._rows(self._sessions, limit=limit, offset=offset, query=query)

    async def session_page(
        self, *, limit: int = 20, offset: int = 0, query: str = ""
    ) -> Mapping[str, Any]:
        items = await self.sessions(limit=limit, offset=offset, query=query)
        if self._session_count is None:
            total = offset + len(items)
        else:
            counted = self._session_count(query=query)
            total = int(await counted) if inspect.isawaitable(counted) else int(counted)
        return sanitize(
            {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
                "query": query,
            }
        )

    async def context(self, session_id: str | None) -> Mapping[str, Any]:
        if not session_id:
            raise ValueError("an active session is required")
        if self._context is None:
            return sanitize(
                {
                    "session_id": session_id,
                    "projection_head": None,
                    "transcript_head": 0,
                    "messages": [],
                    "compactions": [],
                }
            )
        result = self._context(session_id)
        value = await result if inspect.isawaitable(result) else result
        return sanitize(dict(value))

    async def _rows(
        self, source: Rows | None, *args: Any, **kwargs: Any
    ) -> list[Mapping[str, Any]]:
        if source is None:
            return []
        result = source(*args, **kwargs)
        values = await result if inspect.isawaitable(result) else result
        return [sanitize(_plain(value)) for value in values]


def _plain(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    return {"value": str(value)}
