from __future__ import annotations

from merlin_contracts import (
    build_state_observation,
    evaluate_objective_gate_predicate,
    evaluate_state_transition,
)


def _observed(
    name: str,
    role: str,
    phase: str,
    value: object,
    **definition: object,
) -> dict[str, object]:
    return build_state_observation(
        name=name,
        role=role,
        phase=phase,
        step_id=f"{phase}-step",
        evaluation_root="response_body",
        pointer=f"/state/{name}",
        binding_context={"compiled_adapter_digest": "sha256:" + "1" * 64},
        selected_value=value,
        **definition,
    )


def test_canonical_observation_builder_feeds_authoritative_transition() -> None:
    observations = [
        _observed("physical_state_identity", "state_identity", "baseline", "run-1"),
        _observed("physical_state_identity", "state_identity", "result", "run-1"),
        _observed(
            "score",
            "score",
            "baseline",
            10,
            value_type="integer",
            preferred_direction="increase",
            minimum_value=0,
            maximum_value=100,
            monotonicity="nondecreasing",
            trust_class="host_observed",
        ),
        _observed(
            "score",
            "score",
            "result",
            20,
            value_type="integer",
            preferred_direction="increase",
            minimum_value=0,
            maximum_value=100,
            monotonicity="nondecreasing",
            trust_class="host_observed",
        ),
    ]

    transition = evaluate_state_transition(observations)

    assert transition["schema"] == "merlin.target-state-transition.v2"
    assert transition["identity_match"] is True
    assert transition["outcome"] == "progress"
    assert transition["validation_errors"] == []


def test_missing_observation_value_is_canonical_but_non_authoritative() -> None:
    observations = [
        _observed("physical_state_identity", "state_identity", "baseline", "run-1"),
        build_state_observation(
            name="score",
            role="score",
            phase="baseline",
            step_id="baseline-step",
            evaluation_root="response_body",
            pointer="/state/score",
            binding_context={},
            selected_available=False,
            value_type="integer",
            preferred_direction="increase",
            trust_class="host_observed",
        ),
        _observed(
            "score",
            "score",
            "result",
            20,
            value_type="integer",
            preferred_direction="increase",
            trust_class="host_observed",
        ),
    ]

    transition = evaluate_state_transition(observations)

    assert transition["outcome"] == "unclear"
    assert "score:baseline:invalid_digest" in transition["validation_errors"]
    assert transition["quarantined_observations"] == ["score"]


def test_objective_gate_present_requires_a_nonempty_authoritative_result() -> None:
    transition = {
        "schema": "merlin.target-state-transition.v2",
        "authority": "target",
        "identity_match": True,
        "conflict": False,
        "validation_errors": [],
        "quarantined_observations": [],
        "observations": [
            {
                "name": "run_id",
                "before_value": "run-1",
                "after_value": "run-1",
                "validation_errors": [],
            }
        ],
    }
    gate = {
        "success_predicate": {
            "observation_name": "run_id",
            "operator": "present",
            "expected_values": [],
        }
    }

    assert evaluate_objective_gate_predicate(gate, transition) is True
    transition["observations"][0]["after_value"] = ""
    assert evaluate_objective_gate_predicate(gate, transition) is False
