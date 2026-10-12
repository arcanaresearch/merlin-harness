from __future__ import annotations

from merlin_agents.arm_pool import load_ranked_pool, novel_arms, rank_frontier, rank_pool
from merlin_agents.discovery import ArmIdentity, ArmObservation
from merlin_contracts import AttemptDisposition, OutcomeAuthority


def _arm(technique: str, objective: str, **measurements: float) -> dict[str, object]:
    return {
        **ArmIdentity(
            "browser:chat", technique, "goat", "baseline", "en", "authenticated:new"
        ).projection(),
        "objective": objective,
        "status": "trial",
        **measurements,
    }


def _valid(identity: ArmIdentity, outcome: str, **metadata: object) -> ArmObservation:
    return ArmObservation(
        identity,
        outcome,
        attempt_disposition=AttemptDisposition.VALID,
        outcome_authority=OutcomeAuthority.TARGET,
        **metadata,
    )


def test_novelty_is_rank_evidence_and_never_suppresses_distinct_arms() -> None:
    objective = "Test tenant isolation with a reviewed second identity."
    decisions = novel_arms((_arm("a", objective), _arm("b", objective)), ())
    assert decisions[0].admitted is True
    assert decisions[1].admitted is True
    assert decisions[1].verdict == "duplicate"
    assert decisions[1].nearest_objective == objective
    assert decisions[1].reason == (
        "objective similarity recorded as ranking evidence; admission preserved"
    )


def test_rank_pool_wires_signals_and_reversibly_downranks_zero_signal_history() -> None:
    dead = _arm("dead", "Probe dead path")
    live = _arm(
        "live",
        "Probe distinct live boundary",
        novelty=0.8,
        rank_components={
            "generalization": {"measured_value": 0.8},
            "boundary": {"measured_value": 1.0},
        },
    )
    dead_id, live_id = ArmIdentity.from_mapping(dead), ArmIdentity.from_mapping(live)
    history = tuple(_valid(dead_id, "negative_result") for _ in range(5)) + (
        ArmObservation(live_id, "provider_failure", infrastructure_failure=True),
        _valid(live_id, "near_miss", partial_signal=0.6, boundary_signal=True),
        _valid(live_id, "win", generalized_win=True),
    )
    ranked = rank_pool((dead, live), history)
    assert [item.arm["technique_id"] for item in ranked] == ["live", "dead"]
    components = {item.name: item for item in ranked[0].ranking.components}
    assert components["generalization"].contribution > 0
    assert components["boundary"].contribution > 0
    assert ranked[0].ranking.lifecycle == "active"
    assert ranked[1].ranking.lifecycle == "trial"
    assert ranked[1].ranking.lifecycle_reason.startswith("zero signal lowers priority reversibly")
    assert (
        next(
            item for item in ranked[1].ranking.components if item.name == "reversible_downrank"
        ).contribution
        < 0
    )


def test_rank_pool_restores_priority_when_language_or_mutation_changes() -> None:
    unchanged = _arm("unchanged", "Retry the same zero-signal candidate")
    changed = _arm(
        "changed",
        "Retry after a language and mutation change",
        language_changed=1.0,
        mutation_changed=1.0,
    )
    history = tuple(
        _valid(ArmIdentity.from_mapping(arm), "negative_result")
        for arm in (unchanged,) * 5 + (changed,) * 5
    )

    ranked = rank_pool((unchanged, changed), history)

    assert [item.arm["technique_id"] for item in ranked] == ["changed", "unchanged"]
    changed_penalty = next(
        item for item in ranked[0].ranking.components if item.name == "reversible_downrank"
    )
    assert changed_penalty.contribution == 0


def test_load_ranked_pool_scopes_durable_history() -> None:
    arm = _arm("new", "Probe a new state")

    class Store:
        def __init__(self) -> None:
            self.request: tuple[str, str] | None = None

        def list_arm_observations(self, target_id: str, scenario_fingerprint: str):
            self.request = (target_id, scenario_fingerprint)
            return ()

    store = Store()
    ranked = load_ranked_pool(
        store, target_id="target-1", scenario_fingerprint="sha256:x", arms=(arm,)
    )
    assert store.request == ("target-1", "sha256:x")
    assert ranked[0].arm == arm


def test_ambiguous_and_cancelled_rows_never_advance_retirement_attempts() -> None:
    arm = _arm("unclear", "Probe an unclear path")
    identity = ArmIdentity.from_mapping(arm)

    ranked = rank_pool(
        (arm,),
        (
            ArmObservation(identity, "ambiguous"),
            ArmObservation(identity, "cancelled"),
        ),
    )

    assert ranked[0].ranking.lifecycle == "trial"
    uncertainty = next(
        item for item in ranked[0].ranking.components if item.name == "portfolio_exploration_bonus"
    )
    pristine = rank_pool((arm,), ())[0]
    pristine_uncertainty = next(
        item for item in pristine.ranking.components if item.name == "portfolio_exploration_bonus"
    )
    assert uncertainty.measured_value == pristine_uncertainty.measured_value


def test_rank_frontier_includes_existing_and_new_candidates_before_slicing() -> None:
    executed = _arm("executed", "Probe the observed browser boundary", novelty=0.2)
    executed_identity = ArmIdentity.from_mapping(executed)
    low = {
        "semantic_id": "language-boundary",
        "technique_id": "multilingual-pivot",
        "controller_id": "direct",
        "mutation_id": "register/plain",
        "requested_language": "am",
        "objective": "Probe the target in Amharic.",
        "status": "trial",
        "novelty": 0.1,
    }
    high = {
        **low,
        "mutation_id": "register/parseltongue-wenyanwen",
        "requested_language": "lzh",
        "objective": "Probe the target using Literary Chinese.",
        "novelty": 0.9,
        "generalization_opportunity": 0.8,
        "boundary_opportunity": 0.7,
    }

    ranked = rank_frontier(
        (executed, low, high),
        (ArmObservation(executed_identity, "timeout", infrastructure_failure=True),),
    )

    assert len(ranked) == 3
    assert ranked[0].arm == high
    assert {item.arm["requested_language"] for item in ranked if not item.materialized} == {
        "am",
        "lzh",
    }
    executed_rank = next(item for item in ranked if item.materialized)
    pristine_rank = next(item for item in rank_frontier((executed,), ()) if item.materialized)
    assert executed_rank.score == pristine_rank.score
    assert executed_rank.lifecycle == "trial"


def test_rank_frontier_keeps_requested_language_mutation_and_controller_cells_distinct() -> None:
    base = {
        "semantic_id": "language-boundary",
        "technique_id": "multilingual-pivot",
        "controller_id": "direct",
        "mutation_id": "baseline",
        "requested_language": "am",
        "objective": "Probe an unadvertised language boundary.",
        "status": "trial",
    }
    arms = (
        base,
        {**base, "requested_language": "lzh"},
        {**base, "mutation_id": "register/parseltongue"},
        {**base, "controller_id": "crescendo"},
    )

    ranked = rank_frontier(arms, ())

    assert len(ranked) == 4
    assert len({item.key for item in ranked}) == 4
