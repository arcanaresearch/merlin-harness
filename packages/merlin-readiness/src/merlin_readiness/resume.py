from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class ResumeStore(Protocol):
    async def session_exists(self, session_id: str) -> bool: ...

    async def load_messages(self, session_id: str) -> Sequence[Any]: ...


@dataclass(frozen=True, slots=True)
class ResumeResult:
    session_id: str
    messages: tuple[Any, ...]
    recovered: int


class ResumeOrchestrator:
    """Validates durable state before handing it back to a live kernel."""

    def __init__(
        self,
        store: ResumeStore,
        recover: Callable[[str], int | Awaitable[int]] | None = None,
    ) -> None:
        self._store = store
        self._recover = recover

    async def resume(self, session_id: str) -> ResumeResult:
        if not session_id.strip():
            raise ValueError("session id is required")
        if not await self._store.session_exists(session_id):
            raise KeyError(f"unknown session: {session_id}")
        recovered = 0
        if self._recover is not None:
            result = self._recover(session_id)
            recovered = await result if inspect.isawaitable(result) else result
        messages = await self._store.load_messages(session_id)
        if not messages:
            raise ValueError("session has no conversation to resume")
        return ResumeResult(session_id, tuple(messages), recovered)
