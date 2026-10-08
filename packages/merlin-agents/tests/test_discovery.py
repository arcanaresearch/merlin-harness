from __future__ import annotations

import pytest
from merlin_agents.discovery import (
    ArmIdentity,
    ArmObservation,
    AttemptStats,
    aggregate_observations,
    arm_key,
    check_novelty,
    classify_lifecycle,
    jaccard_3gram,
    rank_tasks_by_ucb,
)


def _identity(**changes: str) -> ArmIdentity:
    values = {
        "surface_id": "browser:chat",
        "technique_id": "pack/direct",
        "controller_id": "goat",
        "mutation_id": "baseline",
        "actual_language": "en",
        "state_id": "authenticated:new-session",
    }
    values.update(changes)
    return ArmIdentity(**values)


def test_novelty_is_cjk_safe_and_hard_thresholded() -> None:
    assert jaccard_3gram("绕过安全审查", "绕过安全审查") == 1.0
    result = check_novelty(
        "Completely distinct boundary experiment", ("Issue a benign instruction",)
    )
    assert result.verdict == "novel"


def test_arm_identity_uses_every_execution_dimension() -> None:
    baseline = _identity()
    assert ArmIdentity.from_mapping(baseline.projection()) == baseline
    assert arm_key(baseline) != arm_key(_identity(actual_language="ms"))
    assert arm_key(baseline) != arm_key(_identity(state_id="authenticated:after-refusal"))
    assert arm_key(baseline) != arm_key(_identity(surface_id="api:completion"))
    assert arm_key(baseline) != arm_key(_identity(actual_language="und"))
    with pytest.raises(ValueError, match="observed language"):
        _identity(actual_language="requested")


def test_arm_observation_preserves_structured_uncertainty_without_scoring_it() -> None:
    identity = _identity()
    uncertainty = {
        "kind": "evaluation_uncertain",
        "source": "language-observer",
        "reason": "requested and observed language disagree",
        "confidence": 0.7,
        "withhold_transform_credit": True,
    }

    observation = ArmObservation.from_mapping(
        {
            "identity": identity.projection(),
            "outcome": "near_miss",
            "partial_signal": 0.6,
            "evaluation_uncertainties": (uncertainty,),
        }
    )

    assert observation.evaluation_uncertainties == (uncertainty,)
    assert observation.evaluation_uncertainties[0] is not uncertainty
    assert observation.partial_signal == 0.6
    assert observation.transform_credit_eligible is False
    stats = aggregate_observations((observation,))[identity.key]
    assert stats.attempts == 0
    assert stats.infrastructure_failures == 0


def test_aggregate_excludes_infrastructure_and_keeps_partial_signal() -> None:
    identity = _identity()
    stats = aggregate_observations(
        (
            ArmObservation(identity, "timeout", infrastructure_failure=True, latency_ms=99_000),
            ArmObservation(identity, "successful_setup"),
            ArmObservation(identity, "near_miss", partial_signal=0.7, boundary_signal=True),
            ArmObservation(identity, "negative_result"),
        )
    )[identity.key]
    assert stats.attempts == 2
    assert stats.infrastructure_failures == 1
    assert stats.partial_signal == pytest.approx(0.7)
    assert stats.boundary_signals == 1
    assert stats.total_latency_ms == 0


def test_aggregate_withholds_credit_for_unverified_transform() -> None:
    identity = _identity()
    stats = aggregate_observations(
        (ArmObservation(identity, "win", transform_credit_eligible=False),)
    )[identity.key]
    assert stats.attempts == 0
    assert stats.wins == 0
    assert stats.infrastructure_failures == 0


def test_ranking_exposes_all_components_and_rewards_near_miss() -> None:
    partial = AttemptStats(_identity(technique_id="partial"), 3, 0, 0.8, 2)
    dead = AttemptStats(_identity(technique_id="dead"), 3, 0, 0.0, 3)
    measurements = {
        partial.key: {
            "novelty": 0.8,
            "generalization_opportunity": 0.5,
            "boundary_opportunity": 0.6,
        },
        dead.key: {"novelty": 0.1, "generalization_opportunity": 0.0, "boundary_opportunity": 0.0},
    }
    ranked = rank_tasks_by_ucb((dead, partial), arm_measurements=measurements)
    assert ranked[0].key == partial.key
    assert {item.name for item in ranked[0].components} == {
        "quality_signal",
        "portfolio_exploration_bonus",
        "epistemic_uncertainty",
        "novelty",
        "generalization",
        "boundary",
        "latency",
        "cost",
        "reversible_downrank",
    }
    assert sum(item.contribution for item in ranked[0].components) == pytest.approx(ranked[0].score)


def test_untried_arm_precedes_a_perfect_tried_arm_without_faking_its_score() -> None:
    untried = AttemptStats(_identity(technique_id="untried"), 0, 0, 0.0, 0)
    winner = AttemptStats(_identity(technique_id="winner"), 1, 1, 0.0, 0)

    ranked = rank_tasks_by_ucb((winner, untried))

    assert ranked[0].key == untried.key
    assert ranked[0].score < ranked[1].score


def test_ucb_and_epistemic_uncertainty_are_exposed_as_separate_components() -> None:
    identity = _identity(technique_id="uncertain")
    ranking = rank_tasks_by_ucb(
        (AttemptStats(identity, 2, 0, 0.0, 2),),
        arm_measurements={identity.key: {"evaluation_uncertainty": 0.75}},
    )[0]
    components = {item.name: item for item in ranking.components}

    assert components["epistemic_uncertainty"].measured_value == pytest.approx(0.75)
    assert components["epistemic_uncertainty"].contribution == 0
    assert "peer evidence" in components["portfolio_exploration_bonus"].reason


def test_retirement_uses_quality_attempts_and_partial_keeps_arm_live() -> None:
    assert classify_lifecycle("trial", attempts=5, signal=0).action == "downrank"
    assert classify_lifecycle("trial", attempts=5, signal=0.1).action == "promote"
    assert classify_lifecycle("active", attempts=20, signal=0).action == "downrank"
    assert classify_lifecycle("retired", attempts=20, signal=0, novelty=0.8).action == "reactivate"


def test_zero_signal_downrank_is_reversible_for_uncertain_or_changed_candidates() -> None:
    identity = _identity(technique_id="zero-signal")
    stats = AttemptStats(identity, 20, 0, 0.0, 20)
    ordinary = rank_tasks_by_ucb((stats,))[0]
    changed = rank_tasks_by_ucb(
        (stats,),
        arm_measurements={
            identity.key: {
                "novelty": 0.7,
                "evaluation_uncertainty": 0.8,
                "language_changed": 1.0,
            }
        },
    )[0]

    assert ordinary.lifecycle == "trial"
    assert ordinary.score < changed.score
    assert changed.lifecycle == "trial"
