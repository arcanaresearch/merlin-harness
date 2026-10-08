"""Pure projection of durable swarm rows into identity-correct snapshots.

The projection deliberately does not infer identities from worker names or task
paths.  Callers should fetch the input fields in one storage query and pass the
resulting batch here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from itertools import groupby


class IdentityProvenance(StrEnum):
    """How a scope or specialist identity was established."""

    DURABLE = "durable"
    LEGACY_INFERRED = "legacy_inferred"
    LEGACY_UNSCOPED = "legacy_unscoped"


class NodeLifecycle(StrEnum):
    """Aggregate durable lifecycle of a specialist or assignment node."""

    PREPARED = "prepared"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptLifecycle(StrEnum):
    """Lifecycle of only the currently selected execution attempt."""

    LEASED = "leased"
    RUNNING = "running"
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class CandidateEvaluation(StrEnum):
    """Security evaluation state, independent from execution lifecycle."""

    UNASSESSED = "unassessed"
    EVALUATION_UNCERTAIN = "evaluation_uncertain"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True, order=True)
class CandidateAxes:
    """Exact identity of one executable candidate across all ranking axes."""

    target_id: str
    target_generation: str
    scenario_fingerprint: str
    technique_id: str
    controller_id: str
    payload_family: str
    requested_language: str
    mutation_id: str

    def __post_init__(self) -> None:
        _require_non_blank(
            target_id=self.target_id,
            target_generation=self.target_generation,
            scenario_fingerprint=self.scenario_fingerprint,
            technique_id=self.technique_id,
            controller_id=self.controller_id,
            payload_family=self.payload_family,
            requested_language=self.requested_language,
            mutation_id=self.mutation_id,
        )


@dataclass(frozen=True, slots=True)
class CurrentAttempt:
    """Current attempt state without changing its parent node's lifecycle."""

    attempt_id: str
    lifecycle: AttemptLifecycle
    owner: str | None = None
    lease_generation: int | None = None

    def __post_init__(self) -> None:
        _require_non_blank(attempt_id=self.attempt_id)
        if self.owner is not None:
            _require_non_blank(owner=self.owner)
        if self.lease_generation is not None and self.lease_generation < 1:
            raise ValueError("lease generation must be positive")


@dataclass(frozen=True, slots=True)
class CandidateEvaluationState:
    """Current candidate judgement independent from any execution attempt."""

    state: CandidateEvaluation = CandidateEvaluation.UNASSESSED
    evaluation_confidence: float | None = None
    evaluation_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.evaluation_confidence is not None and not (
            0.0 <= self.evaluation_confidence <= 1.0
        ):
            raise ValueError("evaluation confidence must be between 0 and 1")
        if any(not reason.strip() for reason in self.evaluation_reasons):
            raise ValueError("evaluation reasons must not be blank")


@dataclass(frozen=True, slots=True)
class SuccessfulResultSummary:
    """Durable summary of the latest successful assignment result."""

    result_id: str
    summary: str

    def __post_init__(self) -> None:
        _require_non_blank(result_id=self.result_id, summary=self.summary)


@dataclass(frozen=True, slots=True)
class SwarmProjectionRecord:
    """One flat row returned by a batched swarm snapshot storage query."""

    session_id: str | None
    campaign_id: str | None
    scope_provenance: IdentityProvenance
    specialist_instance_id: str
    specialist_identity_provenance: IdentityProvenance
    specialist_display_name: str | None
    specialist_lifecycle: NodeLifecycle
    assignment_id: str
    schedule_id: str
    assignment_lifecycle: NodeLifecycle
    candidate: CandidateAxes
    execution_stage: str
    current_attempt: CurrentAttempt | None = None
    candidate_evaluation: CandidateEvaluationState = CandidateEvaluationState()
    successful_result: SuccessfulResultSummary | None = None
    is_specialist: bool = True
    objective: str = ""
    activity: tuple[str, ...] = ()
    failure_reason: str | None = None
    output: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_scope(self.session_id, self.campaign_id, self.scope_provenance)
        _require_non_blank(
            specialist_instance_id=self.specialist_instance_id,
            assignment_id=self.assignment_id,
            schedule_id=self.schedule_id,
            execution_stage=self.execution_stage,
        )
        if self.specialist_display_name is not None:
            _require_non_blank(specialist_display_name=self.specialist_display_name)
        if self.specialist_identity_provenance is IdentityProvenance.LEGACY_UNSCOPED:
            raise ValueError(
                "a specialist identity may be durable or legacy-inferred, not unscoped"
            )


@dataclass(frozen=True, slots=True)
class AssignmentSnapshot:
    """One durable assignment with its current attempt and latest result."""

    assignment_id: str
    schedule_id: str
    lifecycle: NodeLifecycle
    candidate: CandidateAxes
    execution_stage: str
    current_attempt: CurrentAttempt | None
    candidate_evaluation: CandidateEvaluationState
    successful_result: SuccessfulResultSummary | None
    objective: str = ""
    activity: tuple[str, ...] = ()
    failure_reason: str | None = None
    output: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SpecialistSnapshot:
    """One real specialist instance and its assignment children."""

    specialist_instance_id: str
    identity_provenance: IdentityProvenance
    display_name: str | None
    lifecycle: NodeLifecycle
    assignments: tuple[AssignmentSnapshot, ...]
    is_specialist: bool = True


@dataclass(frozen=True, slots=True)
class SwarmSnapshot:
    """Canonical swarm projection for exactly one explicit or legacy scope."""

    session_id: str | None
    campaign_id: str | None
    scope_provenance: IdentityProvenance
    specialists: tuple[SpecialistSnapshot, ...]

    @property
    def specialist_count(self) -> int:
        return sum(specialist.is_specialist for specialist in self.specialists)

    @property
    def assignment_count(self) -> int:
        return sum(len(specialist.assignments) for specialist in self.specialists)


def project_swarm_snapshots(
    records: Iterable[SwarmProjectionRecord],
) -> tuple[SwarmSnapshot, ...]:
    """Project a flat storage batch without parsing or fabricating identities."""

    ordered = sorted(records, key=_record_sort_key)
    snapshots: list[SwarmSnapshot] = []
    for scope_key, scope_rows_iter in groupby(ordered, key=_scope_key):
        scope_rows = tuple(scope_rows_iter)
        specialist_snapshots: list[SpecialistSnapshot] = []
        for specialist_id, specialist_rows_iter in groupby(
            scope_rows, key=lambda row: row.specialist_instance_id
        ):
            specialist_rows = tuple(specialist_rows_iter)
            _require_consistent_specialist_fields(specialist_rows)
            assignments = _project_assignments(specialist_rows)
            first = specialist_rows[0]
            specialist_snapshots.append(
                SpecialistSnapshot(
                    specialist_instance_id=specialist_id,
                    identity_provenance=first.specialist_identity_provenance,
                    display_name=first.specialist_display_name,
                    is_specialist=first.is_specialist,
                    lifecycle=first.specialist_lifecycle,
                    assignments=assignments,
                )
            )
        session_id, campaign_id, provenance = scope_key
        snapshots.append(
            SwarmSnapshot(
                session_id=session_id,
                campaign_id=campaign_id,
                scope_provenance=provenance,
                specialists=tuple(specialist_snapshots),
            )
        )
    return tuple(snapshots)


def _project_assignments(
    records: tuple[SwarmProjectionRecord, ...],
) -> tuple[AssignmentSnapshot, ...]:
    assignments: list[AssignmentSnapshot] = []
    for assignment_key, assignment_rows_iter in groupby(
        records, key=lambda row: (row.assignment_id, row.candidate)
    ):
        assignment_rows = tuple(assignment_rows_iter)
        first = assignment_rows[0]
        if any(row != first for row in assignment_rows[1:]):
            raise ValueError(f"conflicting rows for assignment candidate {assignment_key!r}")
        assignments.append(
            AssignmentSnapshot(
                assignment_id=first.assignment_id,
                schedule_id=first.schedule_id,
                lifecycle=first.assignment_lifecycle,
                candidate=first.candidate,
                execution_stage=first.execution_stage,
                current_attempt=first.current_attempt,
                candidate_evaluation=first.candidate_evaluation,
                successful_result=first.successful_result,
                objective=first.objective,
                activity=first.activity,
                failure_reason=first.failure_reason,
                output=first.output,
            )
        )
    return tuple(assignments)


def _require_consistent_specialist_fields(
    records: tuple[SwarmProjectionRecord, ...],
) -> None:
    first = records[0]
    expected = (
        first.specialist_identity_provenance,
        first.specialist_display_name,
        first.is_specialist,
        first.specialist_lifecycle,
    )
    for record in records[1:]:
        actual = (
            record.specialist_identity_provenance,
            record.specialist_display_name,
            record.is_specialist,
            record.specialist_lifecycle,
        )
        if actual != expected:
            raise ValueError(
                f"conflicting durable fields for specialist {first.specialist_instance_id!r}"
            )


def _record_sort_key(record: SwarmProjectionRecord) -> tuple[object, ...]:
    session_id, campaign_id, provenance = _scope_key(record)
    return (
        session_id is not None,
        session_id or "",
        campaign_id is not None,
        campaign_id or "",
        provenance,
        record.specialist_instance_id,
        record.assignment_id,
        record.candidate,
    )


def _scope_key(
    record: SwarmProjectionRecord,
) -> tuple[str | None, str | None, IdentityProvenance]:
    return (record.session_id, record.campaign_id, record.scope_provenance)


def _validate_scope(
    session_id: str | None,
    campaign_id: str | None,
    provenance: IdentityProvenance,
) -> None:
    if provenance is IdentityProvenance.LEGACY_UNSCOPED:
        if session_id is not None or campaign_id is not None:
            raise ValueError("legacy-unscoped records must not claim session or campaign scope")
        return
    if provenance is IdentityProvenance.DURABLE:
        if session_id is None or campaign_id is None:
            raise ValueError("durable scopes require session and campaign IDs")
        _require_non_blank(session_id=session_id, campaign_id=campaign_id)
        return
    if session_id is None:
        raise ValueError("legacy-inferred scopes require a session ID")
    _require_non_blank(session_id=session_id)
    if campaign_id is not None:
        _require_non_blank(campaign_id=campaign_id)


def _require_non_blank(**values: str) -> None:
    for label, value in values.items():
        if not value.strip():
            raise ValueError(f"{label.replace('_', ' ')} must not be blank")
