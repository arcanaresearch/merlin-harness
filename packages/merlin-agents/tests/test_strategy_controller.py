from __future__ import annotations

import pytest
from merlin_agents.strategy_controller import propose_strategy_action


def test_crescendo_content_signal_requests_validation_without_declaring_success() -> None:
    proposal = propose_strategy_action(
        "crescendo",
        {"steps": ({"decision": "success"},), "terminal_reason": "positive"},
        {"max_target_turns": 8},
    )

    assert proposal.action == "validate"
    assert proposal.target_execution_terminal is True
    assert "positive" in proposal.evaluation_uncertain[0].reason
    assert "success" not in proposal.dump().values()


def test_crescendo_refusal_backtracks_without_retiring_candidate() -> None:
    proposal = propose_strategy_action(
        "crescendo",
        {"steps": ({"decision": "refusal"},), "next_stage": "backtrack"},
        {"max_target_turns": 8},
    )

    assert proposal.action == "continue"
    assert proposal.next_stage == "backtrack"


def test_goat_continues_until_observation_plan_is_complete() -> None:
    proposal = propose_strategy_action(
        "goat",
        {"steps": ({"response_digest": "sha256:one"},)},
        (("target_turns", 3),),
    )

    assert proposal.action == "continue"
    assert proposal.observations == 1


def test_structured_uncertainty_preserves_source_reason_and_confidence() -> None:
    proposal = propose_strategy_action(
        "goat",
        {
            "steps": (),
            "evaluation_uncertainties": (
                {
                    "source": "target_adapter",
                    "reason": "oracle metadata differed",
                    "confidence": 0.25,
                },
            ),
        },
        {"target_turns": 3},
    )

    uncertainty = proposal.dump()["evaluation_uncertain"][0]
    assert uncertainty == {
        "source": "target_adapter",
        "reason": "oracle metadata differed",
        "confidence": 0.25,
    }


def test_hydra_completed_selection_requests_validator_judgement() -> None:
    proposal = propose_strategy_action(
        "hydra-select",
        {
            "plan": {"selected_candidate_ids": ("one", "two")},
            "steps": ({"candidate_id": "one"}, {"candidate_id": "two"}),
        },
    )

    assert proposal.action == "validate"
    assert "verdict" not in proposal.dump()


def test_hydra_objective_signal_requests_validation_outside_storage() -> None:
    proposal = propose_strategy_action(
        "hydra-select",
        {
            "plan": {"selected_candidate_ids": ("one", "two")},
            "steps": ({"candidate_id": "one", "objective_matched": True},),
        },
        {"stop_on_positive": True},
    )

    assert proposal.action == "validate"
    assert "objective match" in proposal.evaluation_uncertain[0].reason


def test_hydra_recovered_objective_signal_honours_serialized_stop_policy() -> None:
    proposal = propose_strategy_action(
        "hydra-select",
        {
            "plan": {"selected_candidate_ids": ("one", "two", "three")},
            "steps": ({"candidate_id": "one", "objective_matched": True},),
            # Recovery deliberately does not restore a content-derived terminal marker.
            "terminal_reason": None,
        },
        {"stop_on_positive": "true", "selected_count": "3"},
    )

    assert proposal.action == "validate"
    assert proposal.observations == 1


def test_crescendo_stall_requests_review_and_mutation_outside_storage() -> None:
    proposal = propose_strategy_action(
        "crescendo",
        {
            "steps": (
                {"decision": "no-progress"},
                {"decision": "no-progress"},
            ),
        },
        {"max_target_turns": 8, "plateau_turns": 2},
    )

    assert proposal.action == "validate"
    assert "mutate" in proposal.reason


@pytest.mark.parametrize("terminal_reason", ["ambiguous", "blocked"])
def test_legacy_uncertain_terminal_remains_retryable(terminal_reason: str) -> None:
    proposal = propose_strategy_action(
        "hydra-select",
        {"terminal_reason": terminal_reason, "steps": ()},
    )

    assert proposal.action == "retry"
    assert terminal_reason in proposal.evaluation_uncertain[0].reason


def test_hydra_crossed_effect_retries_without_resend() -> None:
    proposal = propose_strategy_action(
        "hydra-select",
        {
            "plan": {"selected_candidate_ids": ("one",)},
            "steps": (),
            "blocking_effect_id": "effect-1",
        },
    )

    assert proposal.action == "retry"
    assert "must not be resent" in proposal.reason


@pytest.mark.parametrize("terminal_reason", ["cancelled", "capped"])
def test_only_operational_terminal_reasons_propose_hard_stop(terminal_reason: str) -> None:
    proposal = propose_strategy_action(
        "goat",
        {"terminal_reason": terminal_reason, "steps": ()},
        {"target_turns": 3},
    )

    assert proposal.action == "stop"
