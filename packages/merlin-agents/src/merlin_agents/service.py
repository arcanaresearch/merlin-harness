from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from uuid import uuid4

from merlin_contracts import (
    AgentProgress as ContractAgentProgress,
)
from merlin_contracts import (
    AgentSummary,
    ChatMessage,
    ChatRequest,
    ModelCompleted,
    ModelError,
    Provider,
    SwarmRequest,
    TextDelta,
)
from merlin_contracts import (
    AgentTask as ContractAgentTask,
)

from .models import AgentTask, TaskState
from .orchestrator import AgentOrchestrator


class AgentProgress(ContractAgentProgress):
    """Kernel progress contract with the worker alias used by the convenience API."""

    @property
    def worker_id(self) -> str | None:
        return self.agent_id


@dataclass(frozen=True, slots=True)
class SwarmHandle:
    id: str


@dataclass(frozen=True, slots=True)
class SwarmTask:
    task_key: str
    worker_id: str
    objective: str
    status: str
    content: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SwarmResult:
    id: str
    status: str
    tasks: tuple[SwarmTask, ...]


@dataclass(slots=True)
class _Swarm:
    id: str
    session_id: str
    objective: str
    orchestrator: AgentOrchestrator
    task_keys: dict[str, str]
    queue: asyncio.Queue[AgentProgress | None] = field(default_factory=asyncio.Queue)
    execution: asyncio.Task[None] | None = None


ProgressCallback = Callable[[AgentProgress], Awaitable[None]]


class AgentService:
    """Production in-memory service for one-level, provider-driven swarms."""

    def __init__(
        self,
        provider: Provider,
        *,
        model: str = "default",
        on_progress: ProgressCallback | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._on_progress = on_progress
        self._swarms: dict[str, _Swarm] = {}
        self._worker_swarms: dict[str, str] = {}
        self._sessions: dict[str, list[str]] = {}
        self._messages: dict[str, list[str]] = {}

    async def start(
        self, objective: str, *, workers: int = 3, session_id: str | None = None
    ) -> SwarmHandle:
        if not objective.strip():
            raise ValueError("objective must not be empty")
        if not 1 <= workers <= 3:
            raise ValueError("workers must be between 1 and 3")
        swarm_id = uuid4().hex
        session_id = session_id or swarm_id
        task_keys: dict[str, str] = {}
        swarm: _Swarm

        async def worker_event(task: AgentTask, state: str, text: str | None) -> None:
            await self._emit(
                swarm,
                AgentProgress(
                    kind="task",
                    state=state,
                    text=text,
                    agent_id=task.id,
                    task_id=task_keys.get(task.id),
                ),
            )

        orchestrator = AgentOrchestrator(
            self._provider,
            model=self._model,
            max_workers=workers,
            max_tasks=workers,
            on_event=worker_event,
        )
        swarm = _Swarm(swarm_id, session_id, objective, orchestrator, task_keys)
        self._swarms[swarm_id] = swarm
        self._sessions.setdefault(session_id, []).append(swarm_id)

        await self._emit(swarm, AgentProgress(kind="swarm", state="planning"))
        planned = await self._plan(objective, workers)
        for task_key, task_objective in planned:
            task = orchestrator.create_task(task_objective)
            task_keys[task.id] = task_key
            self._worker_swarms[task.id] = swarm_id
            self._messages[task.id] = []
            await self._emit(
                swarm,
                AgentProgress(
                    kind="agent",
                    state="started",
                    text=task_objective,
                    agent_id=task.id,
                    task_id=task_key,
                ),
            )
        swarm.execution = asyncio.create_task(self._run(swarm))
        return SwarmHandle(swarm_id)

    async def tasks(self, swarm_id: str) -> tuple[SwarmTask, ...]:
        swarm = self._get_swarm(swarm_id)
        return tuple(self._task_view(swarm, task) for task in swarm.orchestrator.tasks)

    async def message(self, worker_id: str, text: str) -> None:
        if not text.strip():
            raise ValueError("message must not be empty")
        swarm = self._worker_swarm(worker_id)
        self._messages[worker_id].append(text)
        task = next(task for task in swarm.orchestrator.tasks if task.id == worker_id)
        if task.state not in {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.INTERRUPTED,
            TaskState.CANCELLED,
        }:
            swarm.orchestrator.operator_message(worker_id, text)
        await self._emit(
            swarm,
            AgentProgress(kind="message", state="sent", text=text, agent_id=worker_id),
        )

    def messages(self, worker_id: str) -> tuple[str, ...]:
        if worker_id not in self._messages:
            raise KeyError(f"unknown worker {worker_id!r}")
        return tuple(self._messages[worker_id])

    async def stop(self, worker_id: str) -> None:
        swarm = self._worker_swarm(worker_id)
        swarm.orchestrator.stop(worker_id)
        await self._emit(
            swarm,
            AgentProgress(kind="agent", state="stopped", agent_id=worker_id),
        )

    async def wait(self, swarm_id: str) -> SwarmResult:
        swarm = self._get_swarm(swarm_id)
        assert swarm.execution is not None
        await swarm.execution
        tasks = await self.tasks(swarm_id)
        states = {task.status for task in tasks}
        if "interrupted" in states or "cancelled" in states:
            status = "completed_with_interruptions"
        elif "failed" in states:
            status = "completed_with_failures"
        else:
            status = "completed"
        return SwarmResult(swarm.id, status, tasks)

    async def list_agents(self, session_id: str) -> Sequence[AgentSummary]:
        summaries: list[AgentSummary] = []
        for swarm in self._session_swarms(session_id):
            for task in swarm.orchestrator.tasks:
                summaries.append(
                    AgentSummary(
                        id=task.id,
                        name=f"worker-{swarm.task_keys[task.id]}",
                        status=_contract_status(task.state),
                        task_id=swarm.task_keys[task.id],
                    )
                )
        return summaries

    async def list_tasks(self, session_id: str) -> Sequence[ContractAgentTask]:
        records: list[ContractAgentTask] = []
        for swarm in self._session_swarms(session_id):
            for task in swarm.orchestrator.tasks:
                records.append(
                    ContractAgentTask(
                        id=swarm.task_keys[task.id],
                        objective=task.prompt,
                        status=_contract_status(task.state),
                        agent_id=task.id,
                        result=task.result,
                    )
                )
        return records

    async def start_swarm(self, request: SwarmRequest) -> AsyncIterator[AgentProgress]:
        handle = await self.start(
            request.objective,
            workers=request.max_workers,
            session_id=request.session_id,
        )
        swarm = self._get_swarm(handle.id)
        while True:
            event = await swarm.queue.get()
            if event is None:
                break
            yield event

    async def stop_swarm(self, session_id: str) -> None:
        for swarm in self._session_swarms(session_id):
            for task in swarm.orchestrator.tasks:
                if task.state not in {TaskState.COMPLETED, TaskState.FAILED}:
                    swarm.orchestrator.stop(task.id)
            if swarm.execution is not None:
                await swarm.execution

    async def message_agent(self, session_id: str, agent_id: str, message: str) -> None:
        self._ensure_session_worker(session_id, agent_id)
        await self.message(agent_id, message)

    async def stop_agent(self, session_id: str, agent_id: str) -> None:
        self._ensure_session_worker(session_id, agent_id)
        await self.stop(agent_id)

    async def _plan(self, objective: str, workers: int) -> list[tuple[str, str]]:
        request = ChatRequest(
            model=self._model,
            messages=[
                ChatMessage(
                    role="system",
                    content=(
                        f"Split this assessment into at most {workers} independent worker tasks. "
                        'Return JSON only: {"tasks":[{"id":"short-id","objective":"..."}]}.'
                    ),
                ),
                ChatMessage(role="user", content=objective),
            ],
            workload="planner",
        )
        parts: list[str] = []
        completed = False
        async for event in self._provider.stream(request):
            if isinstance(event, TextDelta):
                parts.append(event.text)
            elif isinstance(event, ModelError):
                raise RuntimeError(f"lead planning failed: {event.message}")
            elif isinstance(event, ModelCompleted):
                completed = True
        if not completed:
            raise RuntimeError("lead planning stream ended without completion")
        return _parse_plan("".join(parts), workers)

    async def _run(self, swarm: _Swarm) -> None:
        try:
            await swarm.orchestrator.run_all()
            await self._emit(swarm, AgentProgress(kind="swarm", state="completed"))
        finally:
            await swarm.queue.put(None)

    async def _emit(self, swarm: _Swarm, event: AgentProgress) -> None:
        await swarm.queue.put(event)
        if self._on_progress is not None:
            await self._on_progress(event)

    def _task_view(self, swarm: _Swarm, task: AgentTask) -> SwarmTask:
        return SwarmTask(
            task_key=swarm.task_keys[task.id],
            worker_id=task.id,
            objective=task.prompt,
            status=task.state.value,
            content=task.result or "",
            error=task.error,
        )

    def _worker_swarm(self, worker_id: str) -> _Swarm:
        try:
            return self._swarms[self._worker_swarms[worker_id]]
        except KeyError as error:
            raise KeyError(f"unknown worker {worker_id!r}") from error

    def _get_swarm(self, swarm_id: str) -> _Swarm:
        try:
            return self._swarms[swarm_id]
        except KeyError as error:
            raise KeyError(f"unknown swarm {swarm_id!r}") from error

    def _session_swarms(self, session_id: str) -> list[_Swarm]:
        return [self._swarms[swarm_id] for swarm_id in self._sessions.get(session_id, ())]

    def _ensure_session_worker(self, session_id: str, worker_id: str) -> None:
        swarm = self._worker_swarm(worker_id)
        if swarm.session_id != session_id:
            raise KeyError(f"worker {worker_id!r} does not belong to session {session_id!r}")


def _parse_plan(text: str, limit: int) -> list[tuple[str, str]]:
    decoder = json.JSONDecoder()
    document = None
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("tasks"), list):
            document = candidate
            break
    if document is None:
        raise ValueError("lead response did not contain a tasks JSON object")
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in document["tasks"][:limit]:
        if not isinstance(item, dict):
            raise ValueError("lead task must be an object")
        task_id = item.get("id")
        objective = item.get("objective")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("lead task id must be a non-empty string")
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("lead task objective must be a non-empty string")
        if task_id in seen:
            raise ValueError(f"duplicate lead task id {task_id!r}")
        seen.add(task_id)
        result.append((task_id, objective))
    if not result:
        raise ValueError("lead must create at least one task")
    return result


def _contract_status(state: TaskState) -> str:
    return {
        TaskState.QUEUED: "queued",
        TaskState.RUNNING: "running",
        TaskState.COMPLETED: "completed",
        TaskState.FAILED: "failed",
        TaskState.INTERRUPTED: "interrupted",
        TaskState.CANCELLED: "cancelled",
    }[state]
