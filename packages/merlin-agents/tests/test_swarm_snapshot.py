from __future__ import annotations

from dataclasses import replace

import pytest
from merlin_agents.swarm_snapshot import (
    AttemptLifecycle,
    CandidateAxes,
    CandidateEvaluation,
    CandidateEvaluationState,
    CurrentAttempt,
    IdentityProvenance,
    NodeLifecycle,
    SuccessfulResultSummary,
    SwarmProjectionRecord,
    project_swarm_snapshots,
)


def _record(
    *,
    specialist_id: str = "specialist/1",
    assignment_id: str = "assignment/1",
    candidate: CandidateAxes | None = None,
) -> SwarmProjectionRecord:
    return SwarmProjectionRecord(
        session_id="session/1",
        campaign_id="campaign/1",
        scope_provenance=IdentityProvenance.DURABLE,
        specialist_instance_id=specialist_id,
        specialist_identity_provenance=IdentityProvenance.DURABLE,
        specialist_display_name="Prompt injection specialist",
        is_specialist=True,
        specialist_lifecycle=NodeLifecycle.RUNNING,
        assignment_id=assignment_id,
        schedule_id="schedule/1",
        assignment_lifecycle=NodeLifecycle.RUNNING,
        candidate=candidate
        or CandidateAxes(
            target_id="target/1",
            target_generation="generation/1",
            scenario_fingerprint="sha256:scenario-1",
            technique_id="technique/direct-override",
            controller_id="controller/pair",
            payload_family="payload/direct",
            requested_language="lzh",
            mutation_id="mutation/rot13",
        ),
        execution_stage="direct",
        provider_model="worker-model",
        current_attempt=CurrentAttempt(
            attempt_id="attempt/current",
            lifecycle=AttemptLifecycle.FINALIZING,
        ),
        candidate_evaluation=CandidateEvaluationState(
            state=CandidateEvaluation.EVALUATION_UNCERTAIN,
            evaluation_confidence=0.42,
            evaluation_reasons=("oracle and validator disagree",),
        ),
        successful_result=SuccessfulResultSummary(
            result_id="result/previous",
            summary="Previous attempt reached the target and preserved its reply.",
        ),
    )


def test_projects_batched_rows_into_real_specialists_and_assignment_children() -> None:
    first = _record()
    second = replace(first, assignment_id="assignment/2", execution_stage="replay")
    other = _record(specialist_id="specialist/2", assignment_id="assignment/3")

    (snapshot,) = project_swarm_snapshots([other, second, first])

    assert snapshot.session_id == "session/1"
    assert snapshot.campaign_id == "campaign/1"
    assert snapshot.specialist_count == 2
    assert snapshot.assignment_count == 3
    assert [item.specialist_instance_id for item in snapshot.specialists] == [
        "specialist/1",
        "specialist/2",
    ]
    assert [item.assignment_id for item in snapshot.specialists[0].assignments] == [
        "assignment/1",
        "assignment/2",
    ]


def test_structural_task_bucket_is_visible_but_not_counted_as_a_specialist() -> None:
    structural = replace(
        _record(specialist_id="structural:campaign/1"),
        specialist_display_name="Unassigned/structural tasks",
        specialist_identity_provenance=IdentityProvenance.LEGACY_INFERRED,
        is_specialist=False,
    )

    (snapshot,) = project_swarm_snapshots([structural])

    assert snapshot.specialist_count == 0
    assert snapshot.assignment_count == 1
    assert snapshot.specialists[0].display_name == "Unassigned/structural tasks"


def test_preserves_node_attempt_evaluation_and_success_as_separate_facts() -> None:
    (snapshot,) = project_swarm_snapshots([_record()])
    specialist = snapshot.specialists[0]
    assignment = specialist.assignments[0]

    assert specialist.lifecycle is NodeLifecycle.RUNNING
    assert assignment.lifecycle is NodeLifecycle.RUNNING
    assert assignment.current_attempt is not None
    assert assignment.current_attempt.lifecycle is AttemptLifecycle.FINALIZING
    assert assignment.candidate_evaluation.state is CandidateEvaluation.EVALUATION_UNCERTAIN
    assert assignment.successful_result is not None
    assert assignment.successful_result.result_id == "result/previous"
    assert assignment.provider_model == "worker-model"


def test_exact_candidate_axes_keep_target_scenario_language_and_mutation_distinct() -> None:
    base = _record()
    other_candidate = replace(
        base.candidate,
        target_generation="generation/2",
        scenario_fingerprint="sha256:scenario-2",
        requested_language="am",
        mutation_id="mutation/parseltongue",
    )
    other = _record(assignment_id="assignment/2", candidate=other_candidate)

    (snapshot,) = project_swarm_snapshots([base, other])

    candidates = [item.candidate for item in snapshot.specialists[0].assignments]
    assert candidates == [base.candidate, other_candidate]


def test_candidate_evaluation_survives_without_a_current_attempt() -> None:
    row = replace(
        _record(),
        current_attempt=None,
        candidate_evaluation=CandidateEvaluationState(
            state=CandidateEvaluation.CONFIRMED,
            evaluation_confidence=0.97,
            evaluation_reasons=("authoritative target success signal",),
        ),
    )

    (snapshot,) = project_swarm_snapshots([row])

    assignment = snapshot.specialists[0].assignments[0]
    assert assignment.current_attempt is None
    assert assignment.candidate_evaluation.state is CandidateEvaluation.CONFIRMED


def test_does_not_parse_a_role_or_name_from_specialist_identity() -> None:
    row = replace(
        _record(specialist_id="worker-campaign/x/adversarial-explorer/agenda/y/replay"),
        specialist_display_name=None,
        specialist_identity_provenance=IdentityProvenance.LEGACY_INFERRED,
    )

    (snapshot,) = project_swarm_snapshots([row])

    specialist = snapshot.specialists[0]
    assert specialist.display_name is None
    assert specialist.identity_provenance is IdentityProvenance.LEGACY_INFERRED
    assert specialist.specialist_instance_id == row.specialist_instance_id


def test_legacy_unscoped_rows_are_explicit_and_not_mixed_with_scoped_rows() -> None:
    unscoped = replace(
        _record(specialist_id="legacy-specialist"),
        session_id=None,
        campaign_id=None,
        scope_provenance=IdentityProvenance.LEGACY_UNSCOPED,
        specialist_identity_provenance=IdentityProvenance.LEGACY_INFERRED,
    )

    snapshots = project_swarm_snapshots([_record(), unscoped])

    assert len(snapshots) == 2
    legacy = next(
        item for item in snapshots if item.scope_provenance is IdentityProvenance.LEGACY_UNSCOPED
    )
    assert legacy.session_id is None
    assert legacy.campaign_id is None
    assert legacy.specialist_count == 1


def test_rejects_legacy_unscoped_records_that_claim_scope() -> None:
    with pytest.raises(ValueError, match="must not claim"):
        replace(
            _record(),
            scope_provenance=IdentityProvenance.LEGACY_UNSCOPED,
        )


def test_rejects_conflicting_duplicate_assignment_rows() -> None:
    row = _record()

    with pytest.raises(ValueError, match="conflicting rows for assignment"):
        project_swarm_snapshots([row, replace(row, assignment_lifecycle=NodeLifecycle.COMPLETED)])


def test_repeated_assignment_ids_from_different_schedules_remain_distinct() -> None:
    first = _record()
    second = replace(
        first,
        schedule_id="schedule/2",
        assignment_lifecycle=NodeLifecycle.COMPLETED,
    )

    (snapshot,) = project_swarm_snapshots([first, second])

    assignments = snapshot.specialists[0].assignments
    assert tuple(item.schedule_id for item in assignments) == ("schedule/1", "schedule/2")
    assert tuple(item.lifecycle for item in assignments) == (
        NodeLifecycle.RUNNING,
        NodeLifecycle.COMPLETED,
    )


def test_rejects_conflicting_specialist_aggregate_lifecycle() -> None:
    first = _record()
    second = replace(
        first,
        assignment_id="assignment/2",
        specialist_lifecycle=NodeLifecycle.COMPLETED,
    )

    with pytest.raises(ValueError, match="conflicting durable fields"):
        project_swarm_snapshots([first, second])


def test_empty_batch_produces_no_snapshots() -> None:
    assert project_swarm_snapshots([]) == ()
