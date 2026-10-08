from __future__ import annotations

import pytest
from merlin_agents.progress_policy import (
    BudgetEvidence,
    ProgressAction,
    ProgressSnapshot,
    ProviderHealth,
    assess_campaign_progress,
)


def snapshot(
    *,
    calls: int = 0,
    evidence: frozenset[str] = frozenset(),
    frontier: frozenset[str] = frozenset({"candidate-a"}),
    resolved: frozenset[str] = frozenset(),
    semantics: str | None = "semantic-a",
    frontier_exhausted: bool = False,
    health: ProviderHealth = ProviderHealth.HEALTHY,
    failures: frozenset[str] = frozenset(),
    exhausted: frozenset[str] = frozenset(),
) -> ProgressSnapshot:
    return ProgressSnapshot(
        durable_target_calls=calls,
        durable_evidence=evidence,
        unresolved_frontier=frontier,
        durably_resolved=resolved,
        semantic_fingerprint=semantics,
        frontier_exhausted=frontier_exhausted,
        provider_health=health,
        infrastructure_failures=failures,
        budget=BudgetEvidence(exhausted),
    )


def test_initial_frontier_continues_without_fixed_orchestration_limits() -> None:
    decision = assess_campaign_progress(None, snapshot())

    assert decision.action is ProgressAction.CONTINUE
    assert decision.terminal is False
    assert decision.semantic_plateau is False


def test_new_durable_evidence_is_progress() -> None:
    decision = assess_campaign_progress(
        snapshot(calls=1),
        snapshot(calls=2, evidence=frozenset({"receipt-1"})),
    )

    assert decision.action is ProgressAction.CONTINUE
    assert decision.made_progress is True
    assert "new durable evidence" in decision.reasons[0]


def test_frontier_identity_change_is_progress_even_when_count_is_unchanged() -> None:
    decision = assess_campaign_progress(
        snapshot(frontier=frozenset({"candidate-a"})),
        snapshot(
            frontier=frozenset({"candidate-b"}),
            resolved=frozenset({"candidate-a"}),
        ),
    )

    assert decision.action is ProgressAction.CONTINUE
    assert decision.made_progress is True
    assert "frontier work" in " ".join(decision.reasons)


def test_unexplained_frontier_removal_is_rejected_not_rewarded() -> None:
    with pytest.raises(ValueError, match="attributable durable resolution"):
        assess_campaign_progress(
            snapshot(frontier=frozenset({"candidate-a"})),
            snapshot(frontier=frozenset()),
        )


def test_semantically_new_target_observation_is_progress() -> None:
    decision = assess_campaign_progress(
        snapshot(calls=2, semantics="semantic-a"),
        snapshot(calls=3, semantics="semantic-b"),
    )

    assert decision.action is ProgressAction.CONTINUE
    assert decision.made_progress is True


def test_durable_semantic_change_is_progress_without_requiring_another_target_call() -> None:
    decision = assess_campaign_progress(
        snapshot(calls=2, semantics="semantic-a"),
        snapshot(calls=2, semantics="semantic-b"),
    )

    assert decision.action is ProgressAction.CONTINUE
    assert decision.made_progress is True
    assert "semantic state changed" in decision.reasons[-1]


def test_repeated_calls_with_same_semantics_replan_without_retiring_frontier() -> None:
    current = snapshot(calls=4, semantics="same", frontier=frozenset({"mutation-1"}))
    decision = assess_campaign_progress(
        snapshot(calls=3, semantics="same", frontier=frozenset({"mutation-1"})),
        current,
    )

    assert decision.action is ProgressAction.REPLAN
    assert decision.semantic_plateau is True
    assert decision.terminal is False
    assert current.unresolved_frontier == frozenset({"mutation-1"})


def test_missing_semantic_fingerprint_never_fabricates_a_plateau() -> None:
    decision = assess_campaign_progress(
        snapshot(calls=1, semantics=None),
        snapshot(calls=2, semantics=None),
    )

    assert decision.action is ProgressAction.CONTINUE
    assert decision.made_progress is False
    assert decision.semantic_plateau is False


def test_unchanged_snapshot_waits_for_a_new_durable_observation() -> None:
    unchanged = snapshot(calls=1)
    decision = assess_campaign_progress(unchanged, unchanged)

    assert decision.action is ProgressAction.CONTINUE
    assert decision.semantic_plateau is False


@pytest.mark.parametrize(
    "health",
    [ProviderHealth.DEGRADED, ProviderHealth.UNAVAILABLE, ProviderHealth.UNKNOWN],
)
def test_unhealthy_provider_keeps_frontier_unresolved(health: ProviderHealth) -> None:
    decision = assess_campaign_progress(snapshot(), snapshot(health=health))

    assert decision.action is ProgressAction.WAIT_FOR_INFRASTRUCTURE
    assert decision.terminal is False
    assert health.value in decision.reasons[0]


def test_provider_recovery_resumes_unresolved_work_without_fabricating_progress() -> None:
    decision = assess_campaign_progress(
        snapshot(health=ProviderHealth.UNAVAILABLE),
        snapshot(health=ProviderHealth.HEALTHY),
    )

    assert decision.action is ProgressAction.CONTINUE
    assert decision.made_progress is False
    assert decision.terminal is False


def test_infrastructure_failure_is_not_an_attack_outcome_or_completion() -> None:
    decision = assess_campaign_progress(
        snapshot(calls=1),
        snapshot(calls=2, failures=frozenset({"adapter timeout"})),
    )

    assert decision.action is ProgressAction.WAIT_FOR_INFRASTRUCTURE
    assert decision.terminal is False
    assert decision.reasons == ("adapter timeout",)


def test_explicit_budget_can_stop_unresolved_work_with_truthful_limitation() -> None:
    decision = assess_campaign_progress(
        snapshot(calls=4),
        snapshot(
            calls=4,
            frontier=frozenset({"candidate-a", "candidate-b"}),
            failures=frozenset({"provider quota unavailable"}),
            exhausted=frozenset({"cost", "target calls"}),
        ),
    )

    assert decision.action is ProgressAction.STOP_AT_BUDGET
    assert decision.terminal is True
    assert decision.limitation == (
        "Stopped at an explicit budget with 2 unresolved frontier item(s); "
        "frontier exhaustion was not established; "
        "1 infrastructure issue(s) remained unresolved."
    )
    assert "cost, target calls" in decision.reasons[0]


def test_empty_unsealed_frontier_does_not_complete() -> None:
    decision = assess_campaign_progress(
        snapshot(frontier=frozenset()),
        snapshot(frontier=frozenset()),
    )

    assert decision.action is ProgressAction.CONTINUE
    assert decision.terminal is False


def test_durably_exhausted_frontier_completes_scan_without_claiming_coverage() -> None:
    decision = assess_campaign_progress(
        snapshot(frontier=frozenset({"candidate-a"})),
        snapshot(
            frontier=frozenset(),
            resolved=frozenset({"candidate-a"}),
            frontier_exhausted=True,
        ),
    )

    assert decision.action is ProgressAction.COMPLETE
    assert decision.terminal is True
    assert decision.reasons == ("the durable full-frontier projection is exhausted",)
    assert "coverage" not in " ".join(decision.reasons).lower()


def test_plateau_observation_is_preserved_when_infrastructure_or_budget_takes_precedence() -> None:
    previous = snapshot(calls=2, semantics="same")

    waiting = assess_campaign_progress(
        previous,
        snapshot(calls=3, semantics="same", health=ProviderHealth.UNAVAILABLE),
    )
    stopped = assess_campaign_progress(
        previous,
        snapshot(calls=3, semantics="same", exhausted=frozenset({"cost"})),
    )

    assert waiting.action is ProgressAction.WAIT_FOR_INFRASTRUCTURE
    assert waiting.semantic_plateau is True
    assert stopped.action is ProgressAction.STOP_AT_BUDGET
    assert stopped.semantic_plateau is True


def test_budget_limitation_reports_unhealthy_provider_without_failure_string() -> None:
    decision = assess_campaign_progress(
        snapshot(calls=2),
        snapshot(
            calls=2,
            health=ProviderHealth.UNAVAILABLE,
            exhausted=frozenset({"tokens"}),
        ),
    )

    assert decision.action is ProgressAction.STOP_AT_BUDGET
    assert "provider health was unavailable" in (decision.limitation or "")
    assert decision.reasons[-1] == "provider health is unavailable"


def test_durable_counters_and_evidence_cannot_regress() -> None:
    with pytest.raises(ValueError, match="target call count cannot regress"):
        assess_campaign_progress(snapshot(calls=2), snapshot(calls=1))

    with pytest.raises(ValueError, match="evidence cannot disappear"):
        assess_campaign_progress(
            snapshot(evidence=frozenset({"receipt-1"})),
            snapshot(evidence=frozenset()),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("evidence", frozenset({""}), "evidence identifiers"),
        ("frontier", frozenset({" "}), "frontier identifiers"),
        ("resolved", frozenset({""}), "resolved frontier identifiers"),
        ("failures", frozenset({""}), "infrastructure failures"),
        ("exhausted", frozenset({" "}), "budget dimensions"),
    ],
)
def test_blank_durable_identity_is_rejected(
    field: str, value: frozenset[str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        snapshot(**{field: value})  # type: ignore[arg-type]
