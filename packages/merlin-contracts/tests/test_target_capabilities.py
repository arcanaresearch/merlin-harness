import pytest
from merlin_contracts.target_capabilities import (
    RequestConstraint,
    StateCapabilities,
    StateObservation,
    state_capabilities_semantic_projection,
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


def test_state_observation_validates_typed_bounds_and_monotonicity() -> None:
    observation = StateObservation.model_validate(
        {
            "name": "score",
            "role": "score",
            "baseline_selector": {
                "step_id": "setup-session",
                "evaluation_root": "response_body",
                "pointer": "/score",
                "evidence_refs": ["evidence:baseline-score"],
            },
            "result_selector": {
                "step_id": "send-message",
                "evaluation_root": "response_body",
                "pointer": "/score",
                "evidence_refs": ["evidence:result-score"],
            },
            "value_type": "integer",
            "minimum_value": 0,
            "maximum_value": 100,
            "monotonicity": "nondecreasing",
            "trust_class": "host_observed",
        }
    )

    assert observation.maximum_value == 100
    assert observation.trust_class == "host_observed"

    with pytest.raises(ValidationError, match="numeric bounds"):
        StateObservation.model_validate(
            {
                **observation.model_dump(mode="json"),
                "value_type": "string",
            }
        )


def test_state_capabilities_validate_corroboration_references() -> None:
    selector = {
        "baseline_selector": {
            "step_id": "setup-session",
            "evaluation_root": "response_body",
            "pointer": "/value",
        },
        "result_selector": {
            "step_id": "send-message",
            "evaluation_root": "response_body",
            "pointer": "/value",
        },
    }
    capabilities = StateCapabilities.model_validate(
        {
            "observations": [
                {"name": "target_status", "role": "custom", **selector},
                {"name": "host_status", "role": "custom", **selector},
            ],
            "corroboration_rules": [
                {
                    "name": "terminal_status_agreement",
                    "observation_names": ["target_status", "host_status"],
                    "minimum_agreement": 2,
                }
            ],
        }
    )
    assert capabilities.corroboration_rules[0].minimum_agreement == 2

    with pytest.raises(ValidationError, match="unknown observations"):
        StateCapabilities.model_validate(
            {
                **capabilities.model_dump(mode="json"),
                "corroboration_rules": [
                    {
                        "name": "bad_rule",
                        "observation_names": ["target_status", "missing_status"],
                        "minimum_agreement": 2,
                    }
                ],
            }
        )


def test_target_reported_corroboration_remains_advisory_without_trusted_source() -> None:
    selector = {
        "baseline_selector": {
            "step_id": "setup-session",
            "evaluation_root": "response_body",
            "pointer": "/status",
        },
        "result_selector": {
            "step_id": "send-message",
            "evaluation_root": "response_body",
            "pointer": "/status",
        },
    }
    capabilities = StateCapabilities.model_validate(
        {
            "observations": [
                {
                    "name": "target_status",
                    "role": "custom",
                    "trust_class": "target_reported",
                    **selector,
                },
                {
                    "name": "target_echo",
                    "role": "custom",
                    "trust_class": "target_reported",
                    **selector,
                },
            ],
            "corroboration_rules": [
                {
                    "name": "status_agreement",
                    "observation_names": ["target_status", "target_echo"],
                    "minimum_agreement": 2,
                }
            ],
        }
    )
    assert capabilities.corroboration_rules[0].name == "status_agreement"


def test_checkpoint_requires_reviewed_capture_and_restore_binding() -> None:
    capabilities = StateCapabilities.model_validate(
        {
            "affordances": [
                {
                    "kind": "restore_checkpoint",
                    "step_id": "restore",
                    "evaluation_root": "response_body",
                }
            ],
            "checkpoint": {
                "capture_selector": {
                    "step_id": "start",
                    "evaluation_root": "response_body",
                    "pointer": "/leaseId",
                },
                "capture_timing": "live_handle",
                "binding_name": "lease_id",
                "restore_step_id": "restore",
                "restore_request_pointer": "/leaseId",
                "evidence_refs": ["evidence:checkpoint"],
            },
        }
    )
    assert capabilities.checkpoint is not None
    assert capabilities.checkpoint.binding_name == "lease_id"

    with pytest.raises(ValidationError, match="declared together"):
        StateCapabilities.model_validate(
            {
                "affordances": capabilities.model_dump(mode="json")["affordances"],
            }
        )

    with pytest.raises(ValidationError, match="tracking evidence"):
        StateCapabilities.model_validate(
            {
                **capabilities.model_dump(mode="json"),
                "checkpoint": {
                    **capabilities.checkpoint.model_dump(mode="json"),
                    "evidence_refs": [],
                },
            }
        )


def test_state_capabilities_bind_rotating_identity_and_resource_ownership() -> None:
    capabilities = StateCapabilities.model_validate(
        {
            "observations": [
                {
                    "name": "quota_remaining",
                    "role": "budget_remaining",
                    "baseline_selector": {
                        "step_id": "setup-session",
                        "evaluation_root": "response_body",
                        "pointer": "/quota",
                    },
                    "result_selector": {
                        "step_id": "send-message",
                        "evaluation_root": "response_body",
                        "pointer": "/quota",
                    },
                    "value_type": "integer",
                    "minimum_value": 0,
                    "trust_class": "host_observed",
                }
            ],
            "identity_equivalence_rules": [
                {
                    "name": "rotated_run_link",
                    "left_selector": {
                        "step_id": "setup-session",
                        "evaluation_root": "response_body",
                        "pointer": "/run/id",
                    },
                    "right_selector": {
                        "step_id": "send-message",
                        "evaluation_root": "response_body",
                        "pointer": "/run/previous_id",
                    },
                }
            ],
            "resource_ownership": [
                {
                    "name": "principal_quota",
                    "resource_kind": "quota",
                    "resource_observation_name": "quota_remaining",
                    "principal_selector": {
                        "step_id": "setup-session",
                        "evaluation_root": "response_body",
                        "pointer": "/principal/id",
                    },
                }
            ],
            "resource_controls": [
                {
                    "ownership_name": "principal_quota",
                    "step_id": "send-message",
                    "limit": 30,
                    "window_key": "target-run",
                    "structured_blocks": [
                        {
                            "status_codes": [429],
                            "retry_after_seconds_pointer": "/error/retry_after_seconds",
                            "evidence_refs": ["evidence:quota-contract"],
                        }
                    ],
                }
            ],
        }
    )

    assert capabilities.identity_equivalence_rules[0].relation == "equal"
    assert capabilities.resource_ownership[0].authority == "adapter_origin"
    assert capabilities.resource_controls[0].reservation_amount == 1
    assert capabilities.resource_controls[0].structured_blocks[0].status_codes == (429,)

    with pytest.raises(ValidationError, match="unknown observation"):
        StateCapabilities.model_validate(
            {
                **capabilities.model_dump(mode="json"),
                "resource_ownership": [
                    {
                        **capabilities.resource_ownership[0].model_dump(mode="json"),
                        "resource_observation_name": "missing_quota",
                    }
                ],
            }
        )

    with pytest.raises(ValidationError, match="unknown ownership"):
        StateCapabilities.model_validate(
            {
                **capabilities.model_dump(mode="json"),
                "resource_controls": [
                    {
                        **capabilities.resource_controls[0].model_dump(mode="json"),
                        "ownership_name": "missing_quota",
                    }
                ],
            }
        )


def test_versioned_state_capabilities_require_versioned_selectors() -> None:
    raw = {
        "schema_version": "merlin.state-capabilities.v1",
        "observations": [
            {
                "name": "score",
                "role": "score",
                "baseline_selector": {
                    "schema_version": "merlin.state-selector.v1",
                    "step_id": "setup-session",
                    "evaluation_root": "response_body",
                    "pointer": "/score",
                    "evidence_refs": ["evidence:baseline-score"],
                    "confidence": 1.0,
                },
                "result_selector": {
                    "schema_version": "merlin.state-selector.v1",
                    "step_id": "send-message",
                    "evaluation_root": "response_body",
                    "pointer": "/score",
                    "evidence_refs": ["evidence:result-score"],
                    "confidence": 1.0,
                },
            }
        ],
    }
    assert StateCapabilities.model_validate(raw).schema_version == ("merlin.state-capabilities.v1")

    del raw["observations"][0]["result_selector"]["schema_version"]
    with pytest.raises(ValidationError, match="every selector"):
        StateCapabilities.model_validate(raw)


def test_versioned_lifecycle_classifies_clone_and_cleanup_independently() -> None:
    capabilities = StateCapabilities.model_validate(
        {
            "schema_version": "merlin.state-capabilities.v1",
            "affordances": [
                {
                    "kind": "clone_state",
                    "step_id": "clone",
                    "evaluation_root": "response_body",
                    "evidence_refs": ["evidence:clone"],
                    "confidence": 0.95,
                },
                {
                    "kind": "cleanup_state",
                    "step_id": "cleanup",
                    "evaluation_root": "response_body",
                    "evidence_refs": ["evidence:cleanup"],
                    "confidence": 1.0,
                },
            ],
            "lifecycle": {
                "isolation": "independent",
                "baseline": "clone",
                "cleanup": "explicit",
                "evidence_refs": ["evidence:lifecycle"],
                "confidence": 0.95,
            },
        }
    )
    assert capabilities.lifecycle is not None
    assert capabilities.lifecycle.baseline == "clone"

    incomplete = capabilities.model_dump(mode="json")
    incomplete["affordances"][0].pop("confidence")
    with pytest.raises(ValidationError, match="affordances require evidence"):
        StateCapabilities.model_validate(incomplete)


def test_versioned_executable_selector_requires_evidence_and_confidence() -> None:
    with pytest.raises(ValidationError, match="selectors require evidence"):
        StateCapabilities.model_validate(
            {
                "schema_version": "merlin.state-capabilities.v1",
                "observations": [
                    {
                        "name": "score",
                        "role": "score",
                        "baseline_selector": {
                            "schema_version": "merlin.state-selector.v1",
                            "step_id": "setup",
                            "evaluation_root": "response_body",
                            "pointer": "/score",
                        },
                        "result_selector": {
                            "schema_version": "merlin.state-selector.v1",
                            "step_id": "interaction",
                            "evaluation_root": "response_body",
                            "pointer": "/score",
                        },
                    }
                ],
            }
        )


def test_supported_modalities_are_evidence_bound_but_project_only_execution_semantics() -> None:
    capabilities = StateCapabilities.model_validate(
        {
            "schema_version": "merlin.state-capabilities.v1",
            "supported_modalities": [
                {
                    "modality": "text",
                    "step_id": "interaction",
                    "request_pointer": "/payload/message",
                    "evidence_refs": ["evidence:request"],
                    "confidence": 1.0,
                }
            ],
        }
    )

    assert state_capabilities_semantic_projection(capabilities)["supported_modalities"] == [
        {
            "modality": "text",
            "step_id": "interaction",
            "request_pointer": "/payload/message",
        }
    ]

    raw = capabilities.model_dump(mode="json")
    raw["supported_modalities"][0]["evidence_refs"] = []
    with pytest.raises(ValidationError, match="at least 1 item"):
        StateCapabilities.model_validate(raw)
