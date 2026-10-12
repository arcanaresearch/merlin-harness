import pytest
from merlin_contracts.target_capabilities import (
    RequestConstraint,
    StateCapabilities,
    StateObservation,
)
from pydantic import ValidationError


def test_generic_target_capabilities_validate_composable_state_facts() -> None:
    capabilities = StateCapabilities.model_validate(
        {
            "identity": {
                "identity_scope": "physical_state",
                "baseline_selector": {
                    "pointer": "/run/id",
                    "step_id": "setup-session",
                    "evaluation_root": "response_body",
                },
                "result_selector": {
                    "pointer": "/run/id",
                    "step_id": "send-message",
                    "evaluation_root": "response_body",
                },
                "evidence_refs": ["evidence:setup"],
            },
            "observations": [
                {
                    "name": "progress_stage",
                    "role": "progress_ordinal",
                    "baseline_selector": {
                        "step_id": "setup-session",
                        "evaluation_root": "response_body",
                        "pointer": "/run/stage",
                    },
                    "result_selector": {
                        "step_id": "send-message",
                        "evaluation_root": "response_body",
                        "pointer": "/run/stage",
                    },
                    "preferred_direction": "increase",
                },
                {
                    "name": "terminal_state",
                    "role": "terminal_status",
                    "baseline_selector": {
                        "step_id": "setup-session",
                        "evaluation_root": "response_body",
                        "pointer": "/run/status",
                    },
                    "result_selector": {
                        "step_id": "send-message",
                        "evaluation_root": "response_body",
                        "pointer": "/run/status",
                    },
                    "success_values": ["complete"],
                    "failure_values": ["failed"],
                },
            ],
            "affordances": [
                {
                    "kind": "provision_fresh_state",
                    "step_id": "setup-session",
                    "evaluation_root": "response_body",
                },
            ],
            "freshness_rules": [
                {
                    "selector": {
                        "step_id": "setup-session",
                        "evaluation_root": "response_body",
                        "pointer": "/fresh",
                    },
                    "match_values": [True],
                }
            ],
            "maximum_parallel_states": 2,
        }
    )

    assert capabilities.identity is not None
    assert capabilities.identity.baseline_selector.pointer == "/run/id"
    assert capabilities.observations[0].preferred_direction == "increase"
    assert capabilities.affordances[0].kind == "provision_fresh_state"


def test_request_constraint_rejects_non_positive_limit() -> None:
    with pytest.raises(ValidationError):
        RequestConstraint(
            step_id="send-message",
            path="/message",
            maximum=0,
            evidence_bindings=({"evidence_ref": "evidence:limit", "observed_maximum": 1},),
        )


def test_request_constraint_evidence_must_bind_the_reviewed_maximum() -> None:
    with pytest.raises(ValidationError, match="bind the reviewed maximum"):
        RequestConstraint.model_validate(
            {
                "step_id": "send-message",
                "evaluation_root": "request_body",
                "path": "/message",
                "maximum": 500,
                "evidence_bindings": [{"evidence_ref": "evidence:limit", "observed_maximum": 400}],
            }
        )


def test_observation_rejects_direction_for_terminal_status() -> None:
    with pytest.raises(ValidationError, match="preferred_direction"):
        StateObservation(
            name="terminal_state",
            role="terminal_status",
            baseline_selector={
                "step_id": "setup-session",
                "evaluation_root": "response_body",
                "pointer": "/status",
            },
            result_selector={
                "step_id": "send-message",
                "evaluation_root": "response_body",
                "pointer": "/status",
            },
            preferred_direction="increase",
        )


def test_observation_rejects_overlapping_terminal_values() -> None:
    with pytest.raises(ValidationError, match="must not overlap"):
        StateObservation(
            name="terminal_state",
            role="terminal_status",
            baseline_selector={
                "step_id": "setup-session",
                "evaluation_root": "response_body",
                "pointer": "/status",
            },
            result_selector={
                "step_id": "send-message",
                "evaluation_root": "response_body",
                "pointer": "/status",
            },
            success_values=("complete",),
            failure_values=("complete",),
        )


def test_state_capabilities_reject_duplicate_observation_names() -> None:
    with pytest.raises(ValidationError, match="observation names must be unique"):
        StateCapabilities.model_validate(
            {
                "observations": [
                    {
                        "name": "score",
                        "role": "score",
                        "baseline_selector": {
                            "step_id": "setup-session",
                            "evaluation_root": "response_body",
                            "pointer": "/score",
                        },
                        "result_selector": {
                            "step_id": "send-message",
                            "evaluation_root": "response_body",
                            "pointer": "/score",
                        },
                    },
                    {
                        "name": "score",
                        "role": "score",
                        "baseline_selector": {
                            "step_id": "setup-session",
                            "evaluation_root": "response_body",
                            "pointer": "/otherScore",
                        },
                        "result_selector": {
                            "step_id": "send-message",
                            "evaluation_root": "response_body",
                            "pointer": "/otherScore",
                        },
                    },
                ]
            }
        )
