from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from .mailbox import AttributedInput


class DirectiveError(ValueError):
    pass


class DirectiveKind(StrEnum):
    REDIRECT = "redirect"
    HANDOFF = "handoff"
    CANCEL = "cancel"
    FOLLOW_UP = "followup"


@dataclass(frozen=True, slots=True)
class LeadDirective:
    id: str
    kind: DirectiveKind
    lead_id: str
    worker_id: str
    reason: str
    objective: str | None = None
    handoff_worker_id: str | None = None
    source_input_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkerCoordinationState:
    worker_id: str
    active: bool = True
    followups_used: int = 0


class LeadCoordinator:
    """Validate continuous lead decisions without scheduler side effects."""

    def __init__(
        self,
        lead_id: str,
        workers: Iterable[WorkerCoordinationState],
        *,
        max_followups_per_worker: int = 2,
        max_objective_chars: int = 2_000,
    ) -> None:
        worker_values = tuple(workers)
        worker_ids = [worker.worker_id for worker in worker_values]
        if not lead_id.strip() or not worker_ids or any(not value.strip() for value in worker_ids):
            raise ValueError("lead and worker identities must not be empty")
        if len(worker_ids) != len(set(worker_ids)):
            raise ValueError("worker identities must be unique")
        if any(worker.followups_used < 0 for worker in worker_values):
            raise ValueError("used follow-ups must not be negative")
        if max_followups_per_worker < 0 or max_objective_chars < 1:
            raise ValueError("coordination bounds are invalid")
        self.lead_id = lead_id
        self.max_followups_per_worker = max_followups_per_worker
        self.max_objective_chars = max_objective_chars
        self._workers = {worker.worker_id: worker for worker in worker_values}
        self._inputs: dict[str, AttributedInput] = {}
        self._directives: dict[str, LeadDirective] = {}

    def consume(self, input_id: str, value: AttributedInput) -> None:
        if not input_id.strip() or input_id in self._inputs:
            raise DirectiveError("attributed input id must be unique and non-empty")
        if value.source_agent_id not in self._workers:
            raise DirectiveError("lead input source is not a member of this swarm")
        self._inputs[input_id] = value

    def issue(self, directive: LeadDirective) -> LeadDirective:
        if not directive.id.strip():
            raise DirectiveError("directive id must not be empty")
        if directive.id in self._directives:
            raise DirectiveError(f"duplicate directive {directive.id!r}")
        if directive.lead_id != self.lead_id:
            raise DirectiveError("directive was not issued by the admitted lead")
        worker = self._worker(directive.worker_id)
        if not worker.active:
            raise DirectiveError("directive target is not active")
        if not directive.reason.strip():
            raise DirectiveError("directive reason must not be empty")
        if not directive.source_input_ids:
            raise DirectiveError("directive requires attributable committed input")
        if any(input_id not in self._inputs for input_id in directive.source_input_ids):
            raise DirectiveError("directive references unknown or unattributed input")
        objective = directive.objective.strip() if directive.objective else None
        if objective and len(objective) > self.max_objective_chars:
            raise DirectiveError("directive objective exceeds its bound")
        if directive.kind in {DirectiveKind.REDIRECT, DirectiveKind.FOLLOW_UP} and not objective:
            raise DirectiveError(f"{directive.kind.value} requires a bounded objective")
        if directive.kind is DirectiveKind.HANDOFF:
            if not directive.handoff_worker_id or directive.handoff_worker_id == worker.worker_id:
                raise DirectiveError("handoff requires a distinct destination worker")
            destination = self._worker(directive.handoff_worker_id)
            if not destination.active:
                raise DirectiveError("handoff destination is not active")
        elif directive.handoff_worker_id is not None:
            raise DirectiveError("only handoff directives may name a destination")
        if directive.kind is DirectiveKind.FOLLOW_UP:
            if worker.followups_used >= self.max_followups_per_worker:
                raise DirectiveError("worker follow-up bound exhausted")
            self._workers[worker.worker_id] = WorkerCoordinationState(
                worker.worker_id, worker.active, worker.followups_used + 1
            )
        elif directive.kind in {DirectiveKind.CANCEL, DirectiveKind.HANDOFF}:
            self._workers[worker.worker_id] = WorkerCoordinationState(
                worker.worker_id, False, worker.followups_used
            )
        self._directives[directive.id] = directive
        return directive

    @property
    def inputs(self) -> Mapping[str, AttributedInput]:
        return dict(self._inputs)

    @property
    def directives(self) -> tuple[LeadDirective, ...]:
        return tuple(self._directives.values())

    def worker_state(self, worker_id: str) -> WorkerCoordinationState:
        return self._worker(worker_id)

    def _worker(self, worker_id: str) -> WorkerCoordinationState:
        try:
            return self._workers[worker_id]
        except KeyError as error:
            raise DirectiveError(f"unknown worker {worker_id!r}") from error
