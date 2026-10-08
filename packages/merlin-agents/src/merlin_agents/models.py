from dataclasses import dataclass, field
from enum import StrEnum


class AgentRole(StrEnum):
    LEAD = "lead"
    WORKER = "worker"


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


TERMINAL_STATES = {
    TaskState.COMPLETED,
    TaskState.FAILED,
    TaskState.INTERRUPTED,
    TaskState.CANCELLED,
}


@dataclass(slots=True)
class AgentTask:
    id: str
    prompt: str
    role: AgentRole = AgentRole.WORKER
    state: TaskState = TaskState.QUEUED
    operator_messages: list[str] = field(default_factory=list)
    result: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LeadResult:
    tasks: tuple[AgentTask, ...]

    @property
    def successful(self) -> bool:
        return all(task.state is TaskState.COMPLETED for task in self.tasks)

    @property
    def combined(self) -> str:
        sections = []
        for task in self.tasks:
            values = [value for value in (task.result, task.error) if value]
            value = "\n".join(values) or "No result"
            sections.append(f"[{task.id} | {task.state}]\n{value}")
        return "\n\n".join(sections)
