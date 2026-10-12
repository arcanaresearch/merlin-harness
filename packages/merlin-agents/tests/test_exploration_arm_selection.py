from merlin_agents.discovery import ArmIdentity, ArmObservation
from merlin_agents.exploration import (
    select_exploration_arms,
    select_exploration_frontier,
    select_exploration_seeds,
)
from merlin_contracts import AttemptDisposition, OutcomeAuthority


def _arm(technique: str, objective: str, *, novelty: float) -> dict[str, object]:
    return {
        **ArmIdentity(
            "browser:chat", technique, "goat", "baseline", "ms", "authenticated:new"
        ).projection(),
        "objective": objective,
        "novelty": novelty,
        "generalization_opportunity": 0.0,
        "boundary_opportunity": 0.0,
        "status": "trial",
    }


def test_selector_is_ranked_not_fifo_and_exposes_audit() -> None:
    low = _arm("low", "Probe one distinct condition", novelty=0.1)
    high = _arm("high", "Investigate an unrelated state transition", novelty=1.0)
    low_history = (
        ArmObservation(
            ArmIdentity.from_mapping(low),
            "negative_result",
            attempt_disposition=AttemptDisposition.VALID,
            outcome_authority=OutcomeAuthority.TARGET,
        ),
    )
    selection = select_exploration_arms((low, high), low_history, maximum=1)
    assert selection.selected[0]["technique_id"] == "high"
    assert selection.deferred_keys == (ArmIdentity.from_mapping(low).key,)
    assert selection.rankings[0]["components"]
    assert all(item["reason"] for item in selection.novelty_decisions)


def test_selector_projects_attributable_seed_reason() -> None:
    arm = _arm("high", "Investigate a new state transition", novelty=1.0)
    seeds = select_exploration_seeds((arm,), ())
    assert seeds[0].technique_id == "high"
    assert "bandit:" in seeds[0].reason
    assert "score=" in seeds[0].reason


def test_frontier_selector_ranks_new_and_existing_arms_before_bounding() -> None:
    existing = _arm("existing", "Probe an established target state", novelty=0.1)
    proposal = {
        "semantic_id": "rare-language-boundary",
        "technique_id": "multilingual-pivot",
        "controller_id": "crescendo",
        "mutation_id": "register/parseltongue-wenyanwen",
        "requested_language": "lzh",
        "objective": "Probe an unadvertised Literary Chinese boundary.",
        "status": "trial",
        "novelty": 0.9,
        "generalization_opportunity": 0.8,
        "boundary_opportunity": 0.7,
    }

    selection = select_exploration_frontier((existing, proposal), (), maximum=1)

    assert selection.selected == (proposal,)
    assert len(selection.rankings) == 2
    assert selection.rankings[0]["materialized"] is False
    assert selection.rankings[0]["arm"]["requested_language"] == "lzh"
    assert len(selection.deferred_keys) == 1
