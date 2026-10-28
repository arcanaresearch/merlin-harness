import asyncio
from collections.abc import Awaitable, Callable, Iterable
from uuid import uuid4

from merlin_contracts import (
    ChatMessage,
    ChatRequest,
    ModelCompleted,
    ModelError,
    Provider,
    TextDelta,
)

from .models import TERMINAL_STATES, AgentRole, AgentTask, LeadResult, TaskState


class AgentError(ValueError):
    pass


class TaskNotFoundError(AgentError):
    pass


class TaskLimitError(AgentError):
    pass


class AgentOrchestrator:
    """Runs a bounded in-memory lead/worker swarm through the provider contract."""

    def __init__(
        self,
        provider: Provider,
        *,
        model: str,
        max_workers: int = 3,
        max_tasks: int = 12,
        turn_timeout_seconds: float = 1_800,
        on_event: Callable[[AgentTask, str, str | None], Awaitable[None]] | None = None,
    ) -> None:
        if not 1 <= max_workers <= 3:
            raise ValueError("max_workers must be between 1 and 3")
        if max_tasks < 1:
            raise ValueError("max_tasks must be positive")
        if turn_timeout_seconds <= 0:
            raise ValueError("turn timeout seconds must be positive")
        if not model.strip():
            raise ValueError("model must not be empty")
        self._provider = provider
        self._model = model
        self._max_tasks = max_tasks
        self._turn_timeout_seconds = turn_timeout_seconds
        self._on_event = on_event
        self._tasks: dict[str, AgentTask] = {}
        self._running: dict[str, asyncio.Task[None]] = {}
        self._worker_slots = asyncio.Semaphore(max_workers)

    @property
    def tasks(self) -> tuple[AgentTask, ...]:
        return tuple(self._tasks.values())

    def create_task(self, prompt: str, *, role: AgentRole = AgentRole.WORKER) -> AgentTask:
        if len(self._tasks) >= self._max_tasks:
            raise TaskLimitError(f"task limit of {self._max_tasks} reached")
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        task = AgentTask(id=uuid4().hex, prompt=prompt, role=AgentRole(role))
        self._tasks[task.id] = task
        return task

    def operator_message(self, task_id: str, message: str) -> None:
        task = self._get(task_id)
        if task.state in TERMINAL_STATES:
            raise AgentError(f"task {task_id!r} has already finished")
        if not message.strip():
            raise ValueError("message must not be empty")
        task.operator_messages.append(message)

    def stop(self, task_id: str) -> None:
        task = self._get(task_id)
        if task.state is TaskState.QUEUED:
            task.state = TaskState.CANCELLED
            task.error = "cancelled by operator"
        elif task.state is TaskState.RUNNING:
            task.state = TaskState.INTERRUPTED
            task.error = "interrupted by operator"
            self._running[task_id].cancel()

    async def run_task(self, task_id: str) -> AgentTask:
        task = self._get(task_id)
        if task.state is not TaskState.QUEUED:
            return task
        execution = asyncio.create_task(self._execute(task))
        self._running[task.id] = execution
        try:
            await execution
        except asyncio.CancelledError:
            if task.state is not TaskState.INTERRUPTED:
                raise
        finally:
            self._running.pop(task.id, None)
        return task

    async def run_all(self, task_ids: Iterable[str] | None = None) -> LeadResult:
        selected = list(self._tasks) if task_ids is None else list(dict.fromkeys(task_ids))
        tasks = [self._get(task_id) for task_id in selected]
        await asyncio.gather(*(self.run_task(task.id) for task in tasks))
        return LeadResult(tuple(tasks))

    async def _execute(self, task: AgentTask) -> None:
        slot = self._worker_slots if task.role is AgentRole.WORKER else _NullSlot()
        async with slot:
            if task.state is not TaskState.QUEUED:
                return
            task.state = TaskState.RUNNING
            await self._notify(task, "running")
            content: list[str] = []
            stream = self._provider.stream(self._request(task))
            try:
                async with asyncio.timeout(self._turn_timeout_seconds):
                    async for event in stream:
                        if isinstance(event, TextDelta):
                            content.append(event.text)
                            task.result = "".join(content)
                            await self._notify(task, "delta", event.text)
                        elif isinstance(event, ModelError):
                            task.state = TaskState.FAILED
                            task.error = event.message
                            await self._notify(task, "failed", event.message)
                            return
                        elif isinstance(event, ModelCompleted):
                            task.result = "".join(content)
                            task.state = TaskState.COMPLETED
                            await self._notify(task, "completed", task.result)
                            return
                task.state = TaskState.FAILED
                task.error = "provider stream ended without completion"
                await self._notify(task, "failed", task.error)
            except asyncio.CancelledError:
                if task.state is not TaskState.INTERRUPTED:
                    task.state = TaskState.INTERRUPTED
                    task.error = "interrupted"
                await self._notify(task, "interrupted", task.result)
                raise
            except TimeoutError:
                task.state = TaskState.FAILED
                task.error = (
                    f"provider stream timed out after {self._turn_timeout_seconds:g} seconds"
                )
                await self._notify(task, "failed", task.error)
            except Exception as error:
                task.state = TaskState.FAILED
                task.error = str(error)
                await self._notify(task, "failed", task.error)
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()

    def _request(self, task: AgentTask) -> ChatRequest:
        messages = [
            ChatMessage(
                role="system",
                content=f"You are the {task.role} agent. Complete only the assigned task.",
            ),
            ChatMessage(role="user", content=task.prompt),
        ]
        messages.extend(
            ChatMessage(role="user", content=f"Operator message: {message}")
            for message in task.operator_messages
        )
        return ChatRequest(
            model=self._model,
            messages=messages,
            workload="lead" if task.role is AgentRole.LEAD else "worker",
        )

    def _get(self, task_id: str) -> AgentTask:
        try:
            return self._tasks[task_id]
        except KeyError as error:
            raise TaskNotFoundError(f"unknown task {task_id!r}") from error

    async def _notify(self, task: AgentTask, state: str, text: str | None = None) -> None:
        if self._on_event is not None:
            await self._on_event(task, state, text)


class _NullSlot:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None
