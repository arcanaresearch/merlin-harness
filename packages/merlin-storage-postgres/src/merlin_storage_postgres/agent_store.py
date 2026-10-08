from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

AgentTaskState = Literal["queued", "running", "completed", "failed", "interrupted", "cancelled"]


@dataclass(frozen=True, slots=True)
class AgentTaskRecord:
    id: str
    owner: str
    role: str
    description: str
    provider: str
    model: str
    state: AgentTaskState
    result: str | None
    error: str | None
    latest_event: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class AgentTaskEventRecord:
    task_id: str
    sequence: int
    type: str
    content: str
    created_at: datetime


def _task(row: dict[str, Any]) -> AgentTaskRecord:
    return AgentTaskRecord(**{**row, "id": str(row["id"])})


def _event(row: dict[str, Any]) -> AgentTaskEventRecord:
    return AgentTaskEventRecord(**{**row, "task_id": str(row["task_id"])})


class AgentTaskRepository:
    """Transactional persistence for tasks that never auto-resume after process loss."""

    _columns = (
        "id, owner, role, description, provider, model, state, result, error, "
        "latest_event, created_at, started_at, completed_at"
    )

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def create(
        self,
        *,
        owner: str,
        role: str,
        description: str,
        provider: str,
        model: str,
        task_id: str | None = None,
    ) -> AgentTaskRecord:
        row = self.connection.execute(
            f"""INSERT INTO merlin_storage.agent_tasks
                    (id, owner, role, description, provider, model, state)
                VALUES (%s, %s, %s, %s, %s, %s, 'queued')
                RETURNING {self._columns}""",
            (task_id or uuid4(), owner, role, description, provider, model),
        ).fetchone()
        self.connection.commit()
        return _task(row)

    def get(self, task_id: str) -> AgentTaskRecord | None:
        row = self.connection.execute(
            f"SELECT {self._columns} FROM merlin_storage.agent_tasks WHERE id = %s",
            (task_id,),
        ).fetchone()
        return _task(row) if row else None

    def list(self, *, limit: int = 50) -> list[AgentTaskRecord]:
        rows = self.connection.execute(
            f"""SELECT {self._columns} FROM merlin_storage.agent_tasks
                ORDER BY created_at DESC, id DESC LIMIT %s""",
            (limit,),
        ).fetchall()
        return [_task(row) for row in rows]

    def start(self, task_id: str) -> AgentTaskRecord:
        return self._transition(
            task_id,
            from_states=("queued",),
            state="running",
            assignments="started_at = now(), completed_at = NULL, result = NULL, error = NULL",
        )

    def complete(self, task_id: str, result: str) -> AgentTaskRecord:
        return self._transition(
            task_id,
            from_states=("running",),
            state="completed",
            assignments="result = %s, error = NULL, completed_at = now()",
            parameters=(result,),
        )

    def fail(self, task_id: str, error: str) -> AgentTaskRecord:
        return self._transition(
            task_id,
            from_states=("running",),
            state="failed",
            assignments="error = %s, completed_at = now()",
            parameters=(error,),
        )

    def interrupt(self, task_id: str, result: str | None = None) -> AgentTaskRecord:
        return self._transition(
            task_id,
            from_states=("running",),
            state="interrupted",
            assignments="result = %s, error = 'interrupted by operator', completed_at = now()",
            parameters=(result,),
        )

    def cancel(self, task_id: str) -> AgentTaskRecord:
        return self._transition(
            task_id,
            from_states=("queued",),
            state="cancelled",
            assignments="completed_at = now()",
        )

    def retry(self, task_id: str) -> AgentTaskRecord:
        """Explicitly requeue a terminal unsuccessful task; recovery never calls this."""
        return self._transition(
            task_id,
            from_states=("failed", "interrupted"),
            state="queued",
            assignments=(
                "result = NULL, error = NULL, latest_event = NULL, "
                "started_at = NULL, completed_at = NULL"
            ),
        )

    def interrupt_incomplete(self) -> int:
        """Mark work left by a previous process as interrupted, without retrying it."""
        cursor = self.connection.execute(
            """UPDATE merlin_storage.agent_tasks
               SET state = 'interrupted', error = 'interrupted by process restart',
                   completed_at = now()
               WHERE state IN ('queued', 'running')"""
        )
        self.connection.commit()
        return cursor.rowcount

    def append_event(self, task_id: str, event_type: str, content: str) -> AgentTaskEventRecord:
        with self.connection.transaction():
            row = self.connection.execute(
                """INSERT INTO merlin_storage.agent_task_events
                       (task_id, sequence, type, content)
                   SELECT %s, COALESCE(MAX(sequence) + 1, 0), %s, %s
                   FROM merlin_storage.agent_task_events WHERE task_id = %s
                   RETURNING task_id, sequence, type, content, created_at""",
                (task_id, event_type, content, task_id),
            ).fetchone()
            updated = self.connection.execute(
                "UPDATE merlin_storage.agent_tasks SET latest_event = %s WHERE id = %s",
                (content, task_id),
            )
            if updated.rowcount == 0:
                raise KeyError(f"unknown agent task: {task_id}")
        return _event(row)

    def list_events(self, task_id: str) -> list[AgentTaskEventRecord]:
        rows = self.connection.execute(
            """SELECT task_id, sequence, type, content, created_at
               FROM merlin_storage.agent_task_events
               WHERE task_id = %s ORDER BY sequence""",
            (task_id,),
        ).fetchall()
        return [_event(row) for row in rows]

    def _transition(
        self,
        task_id: str,
        *,
        from_states: tuple[AgentTaskState, ...],
        state: AgentTaskState,
        assignments: str,
        parameters: tuple[object, ...] = (),
    ) -> AgentTaskRecord:
        row = self.connection.execute(
            f"""UPDATE merlin_storage.agent_tasks
                SET state = %s, {assignments}
                WHERE id = %s AND state = ANY(%s)
                RETURNING {self._columns}""",
            (state, *parameters, task_id, list(from_states)),
        ).fetchone()
        if row is None:
            self.connection.rollback()
            raise ValueError(
                f"agent task {task_id!r} cannot transition to {state} from its current state"
            )
        self.connection.commit()
        return _task(row)


class PostgresAgentTaskStore:
    """Async application boundary around agent-task persistence."""

    def __init__(self, connection: Any) -> None:
        self._repository = AgentTaskRepository(connection)
        self._lock = asyncio.Lock()

    async def _call(self, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(function, *args, **kwargs)

    async def create(self, **fields: str) -> AgentTaskRecord:
        return await self._call(self._repository.create, **fields)

    async def get(self, task_id: str) -> AgentTaskRecord | None:
        return await self._call(self._repository.get, task_id)

    async def list(self, *, limit: int = 50) -> Sequence[AgentTaskRecord]:
        return await self._call(self._repository.list, limit=limit)

    async def start(self, task_id: str) -> AgentTaskRecord:
        return await self._call(self._repository.start, task_id)

    async def complete(self, task_id: str, result: str) -> AgentTaskRecord:
        return await self._call(self._repository.complete, task_id, result)

    async def fail(self, task_id: str, error: str) -> AgentTaskRecord:
        return await self._call(self._repository.fail, task_id, error)

    async def interrupt(self, task_id: str, result: str | None = None) -> AgentTaskRecord:
        return await self._call(self._repository.interrupt, task_id, result)

    async def cancel(self, task_id: str) -> AgentTaskRecord:
        return await self._call(self._repository.cancel, task_id)

    async def retry(self, task_id: str) -> AgentTaskRecord:
        return await self._call(self._repository.retry, task_id)

    async def recover(self) -> int:
        return await self._call(self._repository.interrupt_incomplete)

    async def append_event(
        self, task_id: str, event_type: str, content: str
    ) -> AgentTaskEventRecord:
        return await self._call(self._repository.append_event, task_id, event_type, content)

    async def list_events(self, task_id: str) -> Sequence[AgentTaskEventRecord]:
        return await self._call(self._repository.list_events, task_id)
