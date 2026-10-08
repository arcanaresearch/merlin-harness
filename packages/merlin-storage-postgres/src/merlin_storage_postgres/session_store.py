import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from merlin_contracts import AppEvent, ChatMessage, CompactionRecord, ContextProjection

from .repositories import (
    CompactionRepository,
    EventRepository,
    MessageRepository,
    SessionRepository,
)


class PostgresSessionStore:
    """Async adapter for the kernel's structural session-store protocol.

    One lock serializes use of the synchronous psycopg connection. Blocking
    calls run in worker threads so provider streaming does not stall the event
    loop.
    """

    def __init__(
        self,
        connection: Any,
        *,
        sessions: SessionRepository | None = None,
        messages: MessageRepository | None = None,
        events: EventRepository | None = None,
        compactions: CompactionRepository | None = None,
    ) -> None:
        self._sessions = sessions or SessionRepository(connection)
        self._messages = messages or MessageRepository(connection)
        self._events = events or EventRepository(connection)
        self._compactions = compactions or CompactionRepository(connection)
        self._lock = asyncio.Lock()

    async def _call(self, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(function, *args, **kwargs)

    async def create_session(self, *, model: str | None = None) -> str:
        # The selected model is application configuration, not conversation content.
        record = await self._call(self._sessions.create)
        return record.id

    async def list_sessions(
        self, *, limit: int = 50, offset: int = 0, query: str = ""
    ) -> Sequence[Mapping[str, Any]]:
        records = await self._call(self._sessions.list, limit=limit, offset=offset, query=query)
        return [record.model_dump(mode="json") for record in records]

    async def count_sessions(self, *, query: str = "") -> int:
        return int(await self._call(self._sessions.count, query=query))

    async def list_resumable_sessions(
        self, *, limit: int = 50, offset: int = 0, query: str = ""
    ) -> Sequence[Mapping[str, Any]]:
        records = await self._call(
            self._sessions.list_resumable, limit=limit, offset=offset, query=query
        )
        return [record.model_dump(mode="json") for record in records]

    async def count_resumable_sessions(self, *, query: str = "") -> int:
        return int(await self._call(self._sessions.count_resumable, query=query))

    async def session_exists(self, session_id: str) -> bool:
        return await self._call(self._sessions.get, session_id) is not None

    async def load_messages(self, session_id: str) -> Sequence[ChatMessage]:
        return await self._call(self._messages.list_chat, session_id)

    async def load_context(self, session_id: str, identity: ChatMessage) -> ContextProjection:
        return await self._call(self._messages.projection, session_id, identity)

    async def save_compaction(self, record: CompactionRecord) -> CompactionRecord:
        return await self._call(self._compactions.save, record)

    async def context_provenance(self, session_id: str) -> Mapping[str, Any]:
        if await self._call(self._sessions.get, session_id) is None:
            raise KeyError(f"session not found: {session_id}")
        messages = await self._call(self._messages.provenance, session_id)
        lineage_loader = getattr(self._compactions, "list_lineage", self._compactions.list)
        compactions = await self._call(lineage_loader, session_id)
        projection_head = await self._call(self._sessions.projection_head, session_id)
        return {
            "session_id": session_id,
            "projection_head": projection_head,
            "transcript_head": messages[-1]["sequence"] if messages else 0,
            "messages": messages,
            "compactions": [record.model_dump(mode="json") for record in compactions],
        }

    async def branch_session(self, session_id: str, sequence: int) -> str:
        record = await self._call(self._sessions.branch, session_id, sequence)
        return record.id

    async def rewind_session(self, session_id: str, sequence: int) -> None:
        await self._call(self._sessions.rewind, session_id, sequence)

    async def restore_session(self, session_id: str) -> None:
        await self._call(self._sessions.restore, session_id)

    async def append_message(self, session_id: str, message: ChatMessage) -> None:
        await self._call(
            self._messages.append,
            session_id,
            message.role,
            message.content,
            name=message.name,
            tool_call_id=message.tool_call_id,
            tool_calls=message.tool_calls,
            reasoning_content=message.reasoning_content,
        )

    async def append_event(self, session_id: str, event: AppEvent) -> None:
        await self._call(self._events.append, session_id, event.type, event.payload)

    async def list_events(self, session_id: str) -> Sequence[Mapping[str, Any]]:
        records = await self._call(self._events.list, session_id)
        return [record.model_dump(mode="json") for record in records]

    async def clear_session(self, session_id: str) -> None:
        await self._call(self._messages.clear, session_id)
