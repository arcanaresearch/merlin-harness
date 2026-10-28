import hashlib
import json
from copy import deepcopy

import pytest
from merlin_contracts.target_adapter import executable_adapter_digest
from merlin_contracts.target_adapter_compiler import (
    ADAPTER_COMPILATION_VERSION,
    AdapterAdvisoryKind,
    AdapterCompilation,
    AdapterCompileError,
    AdapterCompileFailureKind,
    AdapterStepRole,
    BrowserChat,
    HttpApi,
    ProviderChat,
    compile_target_adapter,
    compiled_target_adapter_digest,
    read_target_adapter,
)
from pydantic import ValidationError


def _http_adapter() -> dict[str, object]:
    return {
        "interface": "http_api",
        "endpoint": "https://TARGET.test/api",
        "credential_handle": "credential:target",
        "api_transport": "browser_context",
        "target_delivery": "inter-agent-message",
        "api_steps": [
            {
                "step_id": "setup-label",
                "endpoint": "sessions",
                "method": "post",
                "request_template": {},
                "captures": {"session_id": "/sessionId"},
            },
            {
                "step_id": "message-label",
                "endpoint": "/messages",
                "method": "POST",
                "request_template": {"message": "{{prompt}}"},
                "interaction": True,
                "captures": {},
                "response_selector": "/reply",
                "expected_status": 200,
                "oracle_predicate": {"pointer": "/won", "match_values": [True]},
            },
            {
                "step_id": "cleanup-label",
                "endpoint": "/sessions/current",
                "method": "DELETE",
                "request_template": {},
                "cleanup": True,
                "captures": {},
                "idempotency_key": "cleanup",
            },
        ],
        "observed_surfaces": ["memory"],
        "language_policy": {"planned_languages": ["lzh"]},
    }


def _browser_adapter() -> dict[str, object]:
    return {
        "interface": "browser",
        "endpoint": "https://target.test/chat",
        "target_delivery": "direct-user-channel",
        "browser_steps": [
            {
                "step_id": "fill-label",
                "tool": "browser_fill",
                "arguments": {"locator": "textbox Message", "value": "{{user_input}}"},
            },
            {
                "step_id": "submit-label",
                "tool": "browser_click",
                "arguments": {"locator": "button Send"},
            },
        ],
        "interaction_contract": {
            "input_locator": "textbox Message",
            "submit_action": "button Send",
            "frame_context": None,
            "success_indicator": "reply rendered",
        },
    }


@pytest.mark.parametrize("interface", ["api", "open_ai_compatible"])
def test_provider_aliases_compile_to_one_canonical_actor(interface: str) -> None:
    compiled = compile_target_adapter(
        {
            "interface": interface,
            "endpoint": "HTTPS://TARGET.TEST/v1/chat/completions",
            "credential_handle": "credential:target",
        }
    )

    assert isinstance(compiled.executable, ProviderChat)
    assert compiled.executable.kind == "provider_chat"
    assert compiled.executable.actor == "target_chat"
    assert compiled.executable.input_key == "input"
    assert compiled.executable.endpoint == "https://target.test/v1/chat/completions"


def test_provider_aliases_have_the_same_executable_digest() -> None:
    legacy_api = compile_target_adapter(
        {"interface": "api", "endpoint": "https://target.test/v1/chat/completions"}
    )
    open_ai = compile_target_adapter(
        {
            "interface": "open_ai_compatible",
            "endpoint": "https://target.test/v1/chat/completions",
        }
    )

    assert legacy_api.executable == open_ai.executable
    assert compiled_target_adapter_digest(legacy_api) == compiled_target_adapter_digest(open_ai)


def test_http_api_compiles_typed_steps_and_separates_evaluation_hints() -> None:
    compiled = compile_target_adapter(_http_adapter())

    assert isinstance(compiled.executable, HttpApi)
    assert compiled.executable.actor == "target_request"
    assert compiled.executable.input_key == "input"
    assert compiled.executable.target_delivery == "inter-agent-message"
    assert [step.role for step in compiled.executable.steps] == [
        AdapterStepRole.SETUP,
        AdapterStepRole.INTERACTION,
        AdapterStepRole.CLEANUP,
    ]
    assert compiled.executable.steps[0].endpoint == "https://target.test/api/sessions"
    assert compiled.executable.steps[0].method == "POST"
    assert compiled.executable.steps[1].request_template == {"message": "{{input}}"}
    assert {item.path for item in compiled.advisories} >= {
        "api_steps[1].response_selector",
        "api_steps[1].expected_status",
        "api_steps[1].oracle_predicate",
        "observed_surfaces",
        "language_policy",
    }


def test_advisory_changes_do_not_change_executable_digest() -> None:
    original = _http_adapter()
    changed = deepcopy(original)
    changed["observed_surfaces"] = ["rag", "multimodal"]
    steps = changed["api_steps"]
    assert isinstance(steps, list)
    message = steps[1]
    assert isinstance(message, dict)
    message["response_selector"] = "/other"
    message["expected_status"] = 202
    message["oracle_predicate"] = {"pointer": "/score", "match_values": [99]}

    assert compiled_target_adapter_digest(compile_target_adapter(original)) == (
        compiled_target_adapter_digest(compile_target_adapter(changed))
    )


def test_http_direct_transport_alias_is_canonical_and_unknown_transport_is_rejected() -> None:
    absent = _http_adapter()
    absent.pop("api_transport")
    explicit = deepcopy(absent)
    explicit["api_transport"] = "direct"
    unknown = deepcopy(absent)
    unknown["api_transport"] = "carrier_pigeon"

    absent_compiled = compile_target_adapter(absent)
    explicit_compiled = compile_target_adapter(explicit)
    assert isinstance(absent_compiled.executable, HttpApi)
    assert absent_compiled.executable.api_transport == "direct"
    assert compiled_target_adapter_digest(absent_compiled) == compiled_target_adapter_digest(
        explicit_compiled
    )
    with pytest.raises(AdapterCompileError) as caught:
        compile_target_adapter(unknown)
    assert caught.value.kind is AdapterCompileFailureKind.NON_EXECUTABLE_TRANSPORT


def test_executable_request_change_changes_digest() -> None:
    original = _http_adapter()
    changed = deepcopy(original)
    steps = changed["api_steps"]
    assert isinstance(steps, list)
    message = steps[1]
    assert isinstance(message, dict)
    message["request_template"] = {"message": "{{input}}", "prefix": "changed"}

    assert compiled_target_adapter_digest(compile_target_adapter(original)) != (
        compiled_target_adapter_digest(compile_target_adapter(changed))
    )


def test_generic_constraints_and_state_capabilities_compile_as_executable_semantics() -> None:
    raw = _http_adapter()
    raw["request_constraints"] = [
        {
            "kind": "string_length",
            "step_id": "message-label",
            "evaluation_root": "request_body",
            "path": "/message",
            "maximum": 500,
            "unit": "unicode_characters",
            "source": "server_validation_response",
            "evidence_refs": ["evidence:limit"],
            "evidence_bindings": [{"evidence_ref": "evidence:limit", "observed_maximum": 500}],
            "confidence": 1.0,
        }
    ]
    raw["state_capabilities"] = {
        "identity": {
            "identity_scope": "physical_state",
            "baseline_selector": {
                "pointer": "/run/id",
                "step_id": "setup-label",
                "evaluation_root": "response_body",
            },
            "result_selector": {
                "pointer": "/run/id",
                "step_id": "message-label",
                "evaluation_root": "response_body",
            },
            "evidence_refs": ["evidence:setup"],
        },
        "observations": [
            {
                "name": "progress_stage",
                "role": "progress_ordinal",
                "baseline_selector": {
                    "step_id": "setup-label",
                    "evaluation_root": "response_body",
                    "pointer": "/run/stage",
                },
                "result_selector": {
                    "step_id": "message-label",
                    "evaluation_root": "response_body",
                    "pointer": "/run/stage",
                },
                "preferred_direction": "increase",
                "evidence_refs": ["evidence:interaction"],
            }
        ],
        "affordances": [
            {
                "kind": "provision_fresh_state",
                "step_id": "setup-label",
                "evaluation_root": "response_body",
                "evidence_refs": ["evidence:setup"],
            }
        ],
        "freshness_rules": [
            {
                "selector": {
                    "step_id": "setup-label",
                    "evaluation_root": "response_body",
                    "pointer": "/resumed",
                },
                "match_values": [False],
                "evidence_refs": ["evidence:setup"],
            }
        ],
        "maximum_parallel_states": 2,
    }

    compiled = compile_target_adapter(raw)

    assert isinstance(compiled.executable, HttpApi)
    assert compiled.executable.request_constraints[0].maximum == 500
    assert compiled.executable.state_capabilities is not None
    assert compiled.executable.state_capabilities.identity is not None
    assert compiled.executable.state_capabilities.identity.baseline_selector.pointer == "/run/id"
    assert compiled.executable.state_capabilities.observations[0].role == "progress_ordinal"


def test_checkpoint_contract_binds_exact_capture_placeholder() -> None:
    raw = _http_adapter()
    steps = raw["api_steps"]
    assert isinstance(steps, list)
    assert isinstance(steps[0], dict)
    steps[0]["captures"] = {"lease_id": "/leaseId"}
    steps.insert(
        1,
        {
            "step_id": "restore-label",
            "endpoint": "/restore",
            "method": "POST",
            "request_template": {"leaseId": "{{capture:lease_id}}"},
            "captures": {},
        },
    )
    raw["state_capabilities"] = {
        "affordances": [
            {
                "kind": "restore_checkpoint",
                "step_id": "restore-label",
                "evaluation_root": "response_body",
            }
        ],
        "checkpoint": {
            "capture_selector": {
                "step_id": "setup-label",
                "evaluation_root": "response_body",
                "pointer": "/leaseId",
            },
            "capture_timing": "live_handle",
            "binding_name": "lease_id",
            "restore_step_id": "restore-label",
            "restore_request_pointer": "/leaseId",
            "evidence_refs": ["evidence:checkpoint"],
        },
    }

    compiled = compile_target_adapter(raw)
    assert isinstance(compiled.executable, HttpApi)
    assert compiled.executable.state_capabilities is not None
    assert compiled.executable.state_capabilities.checkpoint is not None

    invalid = deepcopy(raw)
    invalid["state_capabilities"]["checkpoint"]["binding_name"] = "other"  # type: ignore[index]
    with pytest.raises(AdapterCompileError, match="exact capture placeholder"):
        compile_target_adapter(invalid)

    post_success = deepcopy(raw)
    post_success["state_capabilities"]["checkpoint"].update(  # type: ignore[index]
        {
            "capture_selector": {
                "step_id": "message-label",
                "evaluation_root": "response_body",
                "pointer": "/checkpointToken",
            },
            "capture_timing": "post_success_response",
        }
    )
    assert compile_target_adapter(post_success).executable.state_capabilities is not None

    pre_success = deepcopy(post_success)
    pre_success["state_capabilities"]["checkpoint"]["capture_selector"]["step_id"] = (  # type: ignore[index]
        "setup-label"
    )
    with pytest.raises(AdapterCompileError, match="post-success checkpoint capture"):
        compile_target_adapter(pre_success)


def test_cleanup_affordance_binds_cleanup_role_independently() -> None:
    raw = _http_adapter()
    raw["state_capabilities"] = {
        "affordances": [
            {
                "kind": "clone_state",
                "step_id": "setup-label",
                "evaluation_root": "response_body",
            },
            {
                "kind": "cleanup_state",
                "step_id": "cleanup-label",
                "evaluation_root": "response_body",
            },
        ],
        "lifecycle": {
            "isolation": "independent",
            "baseline": "clone",
            "cleanup": "explicit",
            "evidence_refs": ["evidence:lifecycle"],
            "confidence": 1.0,
        },
    }
    assert compile_target_adapter(raw).executable.state_capabilities is not None

    invalid = deepcopy(raw)
    invalid["state_capabilities"]["affordances"][1]["step_id"] = "setup-label"  # type: ignore[index]
    with pytest.raises(AdapterCompileError, match="cleanup_state affordances require cleanup"):
        compile_target_adapter(invalid)


def test_capability_provenance_does_not_change_executable_digest() -> None:
    first = _http_adapter()
    first["request_constraints"] = [
        {
            "path": "/message",
            "step_id": "message-label",
            "evaluation_root": "request_body",
            "maximum": 500,
            "source": "dom_attribute",
            "evidence_refs": ["evidence:first"],
            "evidence_bindings": [{"evidence_ref": "evidence:first", "observed_maximum": 500}],
            "confidence": 0.8,
        }
    ]
    first["state_capabilities"] = {
        "identity": {
            "baseline_selector": {
                "pointer": "/run/id",
                "step_id": "setup-label",
                "evaluation_root": "response_body",
            },
            "result_selector": {
                "pointer": "/run/id",
                "step_id": "message-label",
                "evaluation_root": "response_body",
            },
            "evidence_refs": ["evidence:first"],
        },
        "affordances": [
            {
                "kind": "provision_state",
                "step_id": "setup-label",
                "evaluation_root": "response_body",
                "evidence_refs": ["evidence:first"],
            }
        ],
    }
    second = deepcopy(first)
    second["request_constraints"][0]["source"] = "server_validation_response"  # type: ignore[index]
    second["request_constraints"][0]["evidence_refs"] = ["evidence:second"]  # type: ignore[index]
    second["request_constraints"][0]["confidence"] = 1.0  # type: ignore[index]
    second["request_constraints"][0]["evidence_bindings"][0]["evidence_ref"] = (  # type: ignore[index]
        "evidence:second"
    )
    second["state_capabilities"]["identity"]["evidence_refs"] = [  # type: ignore[index]
        "evidence:second"
    ]
    second["state_capabilities"]["identity"]["baseline_selector"]["evidence_refs"] = [  # type: ignore[index]
        "evidence:selector"
    ]
    second["state_capabilities"]["identity"]["baseline_selector"]["confidence"] = 0.51  # type: ignore[index]
    second["state_capabilities"]["affordances"][0]["evidence_refs"] = [  # type: ignore[index]
        "evidence:second"
    ]

    assert compiled_target_adapter_digest(compile_target_adapter(first)) == (
        compiled_target_adapter_digest(compile_target_adapter(second))
    )


def test_capability_semantics_change_executable_digest() -> None:
    original = _http_adapter()
    original["request_constraints"] = [
        {
            "step_id": "message-label",
            "evaluation_root": "request_body",
            "path": "/message",
            "maximum": 500,
            "evidence_bindings": [{"evidence_ref": "evidence:limit", "observed_maximum": 500}],
        }
    ]
    changed = deepcopy(original)
    changed["request_constraints"][0]["maximum"] = 499  # type: ignore[index]
    changed["request_constraints"][0]["evidence_bindings"][0][  # type: ignore[index]
        "observed_maximum"
    ] = 499

    assert compiled_target_adapter_digest(compile_target_adapter(original)) != (
        compiled_target_adapter_digest(compile_target_adapter(changed))
    )


def test_capability_selector_must_reference_an_exact_compiled_step() -> None:
    raw = _http_adapter()
    raw["state_capabilities"] = {
        "identity": {
            "baseline_selector": {
                "step_id": "missing-step",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            }
        }
    }

    with pytest.raises(AdapterCompileError, match="unknown adapter step IDs") as caught:
        compile_target_adapter(raw)
    assert caught.value.kind is AdapterCompileFailureKind.CONTRACT_VALIDATION


def test_state_management_affordance_must_bind_a_setup_step() -> None:
    raw = _http_adapter()
    raw["state_capabilities"] = {
        "affordances": [
            {
                "kind": "reset_state",
                "step_id": "message-label",
                "evaluation_root": "response_body",
            }
        ]
    }

    with pytest.raises(AdapterCompileError, match="require setup steps") as caught:
        compile_target_adapter(raw)
    assert caught.value.kind is AdapterCompileFailureKind.CONTRACT_VALIDATION


def test_physical_identity_requires_baseline_and_interaction_result() -> None:
    raw = _http_adapter()
    raw["state_capabilities"] = {
        "identity": {
            "baseline_selector": {
                "step_id": "setup-label",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            }
        }
    }

    with pytest.raises(AdapterCompileError, match="requires both baseline_selector") as caught:
        compile_target_adapter(raw)
    assert caught.value.kind is AdapterCompileFailureKind.CONTRACT_VALIDATION


def test_authoritative_result_selector_must_bind_the_interaction_response() -> None:
    raw = _http_adapter()
    raw["state_capabilities"] = {
        "identity": {
            "baseline_selector": {
                "step_id": "setup-label",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            },
            "result_selector": {
                "step_id": "cleanup-label",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            },
        }
    }

    with pytest.raises(AdapterCompileError, match="must reference an interaction step") as caught:
        compile_target_adapter(raw)
    assert caught.value.kind is AdapterCompileFailureKind.CONTRACT_VALIDATION


def test_observation_baseline_must_precede_its_interaction_result() -> None:
    raw = _http_adapter()
    steps = raw["api_steps"]
    assert isinstance(steps, list)
    steps.append(
        {
            "step_id": "late-baseline",
            "endpoint": "/late-baseline",
            "method": "GET",
            "request_template": {},
            "captures": {},
        }
    )
    raw["state_capabilities"] = {
        "identity": {
            "baseline_selector": {
                "step_id": "setup-label",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            },
            "result_selector": {
                "step_id": "message-label",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            },
        },
        "observations": [
            {
                "name": "score",
                "role": "score",
                "baseline_selector": {
                    "step_id": "late-baseline",
                    "evaluation_root": "response_body",
                    "pointer": "/score",
                },
                "result_selector": {
                    "step_id": "message-label",
                    "evaluation_root": "response_body",
                    "pointer": "/score",
                },
                "preferred_direction": "increase",
            }
        ],
    }

    with pytest.raises(AdapterCompileError, match="baseline must be observed before") as caught:
        compile_target_adapter(raw)
    assert caught.value.kind is AdapterCompileFailureKind.CONTRACT_VALIDATION


def test_observations_without_physical_identity_remain_available_as_telemetry() -> None:
    raw = _http_adapter()
    raw["state_capabilities"] = {
        "observations": [
            {
                "name": "score",
                "role": "score",
                "baseline_selector": {
                    "step_id": "setup-label",
                    "evaluation_root": "response_body",
                    "pointer": "/score",
                },
                "result_selector": {
                    "step_id": "message-label",
                    "evaluation_root": "response_body",
                    "pointer": "/score",
                },
                "preferred_direction": "increase",
            }
        ]
    }

    compiled = compile_target_adapter(raw).executable

    assert compiled.state_capabilities is not None
    assert compiled.state_capabilities.identity is None
    assert tuple(item.name for item in compiled.state_capabilities.observations) == ("score",)


def test_absent_capabilities_preserve_legacy_compiled_digest_shape() -> None:
    compiled = compile_target_adapter(_http_adapter()).executable
    payload = compiled.model_dump(mode="json")

    assert compiled.request_constraints == ()
    assert compiled.state_capabilities is None
    assert "request_constraints" not in payload
    assert "state_capabilities" not in payload
    for step in payload["steps"]:
        assert "step_id" not in step
        if not step.get("request_bindings"):
            step.pop("request_bindings", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    legacy_digest = "sha256:" + hashlib.sha256(encoded).hexdigest()

    assert compiled_target_adapter_digest(compiled) == legacy_digest


def test_conflicting_interaction_cleanup_roles_are_rejected_for_state_integrity() -> None:
    raw = _http_adapter()
    steps = raw["api_steps"]
    assert isinstance(steps, list)
    interaction = steps[1]
    assert isinstance(interaction, dict)
    interaction["cleanup"] = True

    with pytest.raises(AdapterCompileError) as caught:
        compile_target_adapter(raw)
    assert caught.value.kind is AdapterCompileFailureKind.NON_EXECUTABLE_CLEANUP


def test_browser_compiles_actor_input_and_typed_actions() -> None:
    compiled = compile_target_adapter(_browser_adapter())

    assert isinstance(compiled.executable, BrowserChat)
    assert compiled.executable.actor == "browser_chat_exchange"
    assert compiled.executable.input_key == "input"
    assert compiled.executable.steps[0].role is AdapterStepRole.INTERACTION
    assert compiled.executable.steps[0].arguments["value"] == "{{input}}"
    assert compiled.executable.interaction.input_locator == "textbox Message"
    assert [item.path for item in compiled.advisories] == ["interaction_contract.success_indicator"]


@pytest.mark.parametrize("interface", ["api", "open_ai_compatible", "browser"])
def test_delivery_contradiction_is_typed_advisory_not_execution_gate(interface: str) -> None:
    raw = (
        _browser_adapter()
        if interface == "browser"
        else {"interface": interface, "endpoint": "https://target.test/chat"}
    )
    raw["target_delivery"] = "inter-agent-message"

    compiled = compile_target_adapter(raw)

    assert compiled.executable.target_delivery is None
    assert compiled.executable.delivery_resolution == "unresolved"
    assert any(
        item.kind is AdapterAdvisoryKind.UNSUPPORTED_CAPABILITY
        and item.path == "target_delivery"
        and item.value == "inter-agent-message"
        for item in compiled.advisories
    )


def test_unknown_delivery_is_advisory_for_http_api() -> None:
    raw = _http_adapter()
    raw["target_delivery"] = "telepathy"

    compiled = compile_target_adapter(raw)

    assert isinstance(compiled.executable, HttpApi)
    assert compiled.executable.target_delivery is None
    assert any(
        item.kind is AdapterAdvisoryKind.UNSUPPORTED_CAPABILITY
        and item.path == "target_delivery"
        and item.value == "telepathy"
        for item in compiled.advisories
    )


def test_http_steps_default_to_same_origin_and_require_explicit_cross_origin_authority() -> None:
    raw = _http_adapter()
    steps = raw["api_steps"]
    assert isinstance(steps, list)
    setup = steps[0]
    assert isinstance(setup, dict)
    setup["endpoint"] = "https://sessions.target.test/start"

    with pytest.raises(AdapterCompileError) as caught:
        compile_target_adapter(raw)
    assert caught.value.kind is AdapterCompileFailureKind.UNAUTHORIZED_ORIGIN

    raw["authorized_origins"] = [
        "https://target.test",
        "https://sessions.target.test",
    ]
    compiled = compile_target_adapter(raw)
    assert isinstance(compiled.executable, HttpApi)
    assert compiled.executable.authorized_origins == (
        "https://sessions.target.test",
        "https://target.test",
    )


def test_endpoint_must_belong_to_explicit_authorized_origins() -> None:
    raw = _http_adapter()
    raw["allowed_origins"] = ["https://other.test"]

    with pytest.raises(AdapterCompileError) as caught:
        compile_target_adapter(raw)
    assert caught.value.kind is AdapterCompileFailureKind.UNAUTHORIZED_ORIGIN


def test_http_interaction_requires_exact_canonical_input_binding() -> None:
    raw = _http_adapter()
    steps = raw["api_steps"]
    assert isinstance(steps, list)
    interaction = steps[1]
    assert isinstance(interaction, dict)
    interaction["request_template"] = {"message": "prefix {{input}}"}

    with pytest.raises(AdapterCompileError) as caught:
        compile_target_adapter(raw)
    assert caught.value.kind is AdapterCompileFailureKind.NON_EXECUTABLE_INPUT


def test_http_capture_dependencies_must_come_from_prior_valid_pointers() -> None:
    valid = _http_adapter()
    valid_steps = valid["api_steps"]
    assert isinstance(valid_steps, list)
    valid_interaction = valid_steps[1]
    assert isinstance(valid_interaction, dict)
    valid_interaction["request_template"] = {
        "sessionId": "{{capture:session_id}}",
        "message": "{{input}}",
    }
    compiled = compile_target_adapter(valid)
    assert isinstance(compiled.executable, HttpApi)

    missing = deepcopy(valid)
    missing_steps = missing["api_steps"]
    assert isinstance(missing_steps, list)
    missing_interaction = missing_steps[1]
    assert isinstance(missing_interaction, dict)
    missing_interaction["request_template"] = {
        "sessionId": "{{capture:future_session}}",
        "message": "{{input}}",
    }
    with pytest.raises(AdapterCompileError) as caught:
        compile_target_adapter(missing)
    assert caught.value.kind is AdapterCompileFailureKind.NON_EXECUTABLE_STEPS

    invalid_pointer = deepcopy(valid)
    invalid_steps = invalid_pointer["api_steps"]
    assert isinstance(invalid_steps, list)
    invalid_setup = invalid_steps[0]
    assert isinstance(invalid_setup, dict)
    invalid_setup["captures"] = {"session_id": "sessionId"}
    with pytest.raises(AdapterCompileError) as caught:
        compile_target_adapter(invalid_pointer)
    assert caught.value.kind is AdapterCompileFailureKind.NON_EXECUTABLE_STEPS


def test_replay_without_idempotency_is_disabled_with_advisory() -> None:
    raw = _http_adapter()
    steps = raw["api_steps"]
    assert isinstance(steps, list)
    interaction = steps[1]
    assert isinstance(interaction, dict)
    interaction["replay_after_ambiguous_result"] = True
    interaction["idempotency_key"] = None

    compiled = compile_target_adapter(raw)
    assert isinstance(compiled.executable, HttpApi)
    assert compiled.executable.steps[1].replay_after_ambiguous_result is False
    assert any(
        item.path == "api_steps[1].replay_after_ambiguous_result" and "idempotency" in item.message
        for item in compiled.advisories
    )


@pytest.mark.parametrize("missing", ["fill", "submit", "input_locator", "submit_locator"])
def test_browser_requires_executable_fill_submit_and_locators(missing: str) -> None:
    raw = _browser_adapter()
    steps = raw["browser_steps"]
    contract = raw["interaction_contract"]
    assert isinstance(steps, list)
    assert isinstance(contract, dict)
    if missing == "fill":
        del steps[0]
    elif missing == "submit":
        del steps[1]
    elif missing == "input_locator":
        contract["input_locator"] = None
        fill = steps[0]
        assert isinstance(fill, dict)
        arguments = fill["arguments"]
        assert isinstance(arguments, dict)
        arguments.pop("locator")
    else:
        contract["submit_action"] = None
        submit = steps[1]
        assert isinstance(submit, dict)
        arguments = submit["arguments"]
        assert isinstance(arguments, dict)
        arguments.pop("locator")

    with pytest.raises(AdapterCompileError) as caught:
        compile_target_adapter(raw)
    assert caught.value.kind is AdapterCompileFailureKind.NON_EXECUTABLE_STEPS


def test_browser_press_requires_nonempty_value_and_normalizes_key_alias() -> None:
    missing = _browser_adapter()
    missing_steps = missing["browser_steps"]
    assert isinstance(missing_steps, list)
    missing_steps[1] = {
        "tool": "browser_press",
        "arguments": {"locator": "textbox Message"},
    }
    with pytest.raises(AdapterCompileError) as caught:
        compile_target_adapter(missing)
    assert caught.value.kind is AdapterCompileFailureKind.NON_EXECUTABLE_STEPS

    legacy = _browser_adapter()
    legacy_steps = legacy["browser_steps"]
    assert isinstance(legacy_steps, list)
    legacy_steps[1] = {
        "tool": "browser_press",
        "arguments": {"locator": "textbox Message", "key": " Enter "},
    }
    compiled = compile_target_adapter(legacy)
    assert isinstance(compiled.executable, BrowserChat)
    assert compiled.executable.steps[1].arguments == {
        "locator": "textbox Message",
        "value": "Enter",
    }


def test_browser_arguments_drop_unknown_and_malformed_runtime_fields() -> None:
    raw = _browser_adapter()
    steps = raw["browser_steps"]
    assert isinstance(steps, list)
    fill = steps[0]
    assert isinstance(fill, dict)
    arguments = fill["arguments"]
    assert isinstance(arguments, dict)
    arguments.update(
        {
            "mystery": "not a BrowserTool argument",
            "step": -1,
            "timeout_ms": "slow",
            "tab_ref": 42,
        }
    )

    compiled = compile_target_adapter(raw)
    assert isinstance(compiled.executable, BrowserChat)
    assert compiled.executable.steps[0].arguments == {
        "locator": "textbox Message",
        "value": "{{input}}",
    }
    paths = {item.path for item in compiled.advisories}
    assert {
        "browser_steps[0].arguments",
        "browser_steps[0].arguments.step",
        "browser_steps[0].arguments.timeout_ms",
        "browser_steps[0].arguments.tab_ref",
    } <= paths


def test_malformed_interaction_locators_fall_back_to_valid_step_locators() -> None:
    raw = _browser_adapter()
    contract = raw["interaction_contract"]
    assert isinstance(contract, dict)
    contract["input_locator"] = {"truthy": "but malformed"}
    contract["submit_action"] = ["also", "malformed"]

    compiled = compile_target_adapter(raw)
    assert isinstance(compiled.executable, BrowserChat)
    assert compiled.executable.interaction.input_locator == "textbox Message"
    assert compiled.executable.interaction.submit_action == "button Send"
    assert compiled.executable.steps[0].arguments["locator"] == "textbox Message"
    assert compiled.executable.steps[1].arguments["locator"] == "button Send"


def test_cleanup_lifecycle_is_derived_and_required_cleanup_must_execute() -> None:
    compiled = compile_target_adapter(_http_adapter())
    assert isinstance(compiled.executable, HttpApi)
    assert compiled.executable.cleanup_lifecycle == "required"
    assert compiled.executable.steps[-1].role is AdapterStepRole.CLEANUP

    missing = _http_adapter()
    missing_steps = missing["api_steps"]
    assert isinstance(missing_steps, list)
    missing["api_steps"] = missing_steps[:-1]
    missing["cleanup_lifecycle"] = {"status": "required"}
    with pytest.raises(AdapterCompileError) as caught:
        compile_target_adapter(missing)
    assert caught.value.kind is AdapterCompileFailureKind.NON_EXECUTABLE_CLEANUP


@pytest.mark.parametrize("credential", ["", "   ", 42, {"secret": "inline"}])
def test_malformed_credential_handle_is_typed_error(credential: object) -> None:
    raw = _http_adapter()
    raw["credential_handle"] = credential

    with pytest.raises(AdapterCompileError) as caught:
        compile_target_adapter(raw)
    assert caught.value.kind is AdapterCompileFailureKind.MALFORMED_CREDENTIAL


def test_pydantic_reader_failures_are_wrapped_as_typed_compile_errors() -> None:
    payload = compile_target_adapter(_http_adapter()).model_dump(mode="json")
    executable = payload["executable"]
    assert isinstance(executable, dict)
    steps = executable["steps"]
    assert isinstance(steps, list)
    first = steps[0]
    assert isinstance(first, dict)
    first["repeat"] = 0

    with pytest.raises(AdapterCompileError) as caught:
        read_target_adapter(payload)
    assert caught.value.kind is AdapterCompileFailureKind.CONTRACT_VALIDATION
    assert caught.value.path == "executable.http_api.steps.0.repeat"
    assert "Input should be greater than or equal to 1" in str(caught.value)


def test_source_aliases_are_class_metadata_not_executable_digest_fields() -> None:
    provider = compile_target_adapter({"interface": "api", "endpoint": "https://target.test/chat"})
    payload = provider.executable.model_dump(mode="json")

    assert ProviderChat.accepted_interface_aliases == ("api", "open_ai_compatible")
    assert "accepted_interface_aliases" not in payload


@pytest.mark.parametrize(
    ("raw", "kind", "path"),
    [
        (
            {"interface": "smtp", "endpoint": "https://target.test"},
            AdapterCompileFailureKind.UNKNOWN_INTERFACE,
            "interface",
        ),
        (
            {"interface": "api", "endpoint": "target.test"},
            AdapterCompileFailureKind.NON_EXECUTABLE_ENDPOINT,
            "endpoint",
        ),
        (
            {"interface": "http_api", "endpoint": "https://target.test", "api_steps": []},
            AdapterCompileFailureKind.NON_EXECUTABLE_STEPS,
            "api_steps",
        ),
        (
            {
                "interface": "browser",
                "endpoint": "https://target.test",
                "browser_steps": [{"tool": "browser_snapshot", "arguments": {}}],
            },
            AdapterCompileFailureKind.NON_EXECUTABLE_STEPS,
            "browser_steps",
        ),
        (
            {
                "interface": "browser",
                "endpoint": "https://target.test",
                "browser_steps": [
                    {"tool": "browser_click", "arguments": {"locator": "button Send"}}
                ],
            },
            AdapterCompileFailureKind.NON_EXECUTABLE_STEPS,
            "browser_steps",
        ),
    ],
)
def test_only_non_executable_transport_contracts_hard_fail(
    raw: dict[str, object], kind: AdapterCompileFailureKind, path: str
) -> None:
    with pytest.raises(AdapterCompileError) as caught:
        compile_target_adapter(raw)

    assert caught.value.kind is kind
    assert caught.value.path == path


def test_reader_accepts_raw_v1_json_compiled_variant_and_envelope() -> None:
    raw = _http_adapter()
    from_mapping = read_target_adapter(raw)
    from_json = read_target_adapter(__import__("json").dumps(raw))
    from_variant = read_target_adapter(from_mapping.executable.model_dump(mode="json"))
    from_envelope = read_target_adapter(from_mapping.model_dump(mode="json"))

    assert from_mapping == from_json
    assert from_variant.executable == from_mapping.executable
    assert from_envelope == from_mapping
    assert from_envelope.schema_version == ADAPTER_COMPILATION_VERSION


def test_unusable_extra_steps_are_advisory_when_an_interaction_remains() -> None:
    http_raw = _http_adapter()
    http_steps = http_raw["api_steps"]
    assert isinstance(http_steps, list)
    http_steps.insert(0, {"method": "", "request_template": {}})
    browser_raw = _browser_adapter()
    browser_steps = browser_raw["browser_steps"]
    assert isinstance(browser_steps, list)
    browser_steps.insert(0, {"tool": "browser_snapshot", "arguments": {}})

    http = compile_target_adapter(http_raw)
    browser = compile_target_adapter(browser_raw)

    assert isinstance(http.executable, HttpApi)
    assert len(http.executable.steps) == 3
    assert isinstance(browser.executable, BrowserChat)
    assert len(browser.executable.steps) == 2
    assert any(
        item.kind is AdapterAdvisoryKind.UNSUPPORTED_CAPABILITY
        and item.path == "api_steps[0].method"
        for item in http.advisories
    )
    assert any(
        item.kind is AdapterAdvisoryKind.UNSUPPORTED_CAPABILITY and item.path == "browser_steps[0]"
        for item in browser.advisories
    )


def test_compiled_models_are_closed_discriminated_contracts() -> None:
    compiled = compile_target_adapter(_http_adapter())
    payload = compiled.model_dump(mode="json")
    executable = payload["executable"]
    assert isinstance(executable, dict)
    executable["kind"] = "browser_chat"

    with pytest.raises(ValidationError):
        AdapterCompilation.model_validate(payload)


def test_existing_v1_digest_reader_remains_frozen() -> None:
    adapter = {
        "interface": "http_api",
        "endpoint": "https://target.test",
        "credential_handle": "credential:target",
        "api_transport": "direct",
        "api_steps": [
            {
                "tool": "target_request",
                "endpoint": "/messages",
                "method": "POST",
                "request_template": {"message": "{{prompt}}"},
                "interaction": True,
                "cleanup": False,
                "captures": {},
                "repeat": 1,
                "idempotency_key": None,
            }
        ],
    }

    assert executable_adapter_digest(adapter) == (
        "sha256:6d52c30812ba937fabdae547f217060b107ff0fdc4530516e6503022b83201eb"
    )


def _injection_adapter() -> dict[str, object]:
    raw = _http_adapter()
    steps = raw["api_steps"]
    assert isinstance(steps, list)
    steps.insert(
        2,
        {
            "step_id": "plant-comment",
            "endpoint": "/comments",
            "method": "POST",
            "request_template": {"body": "{{input}}", "channel": "issue-1"},
            "interaction": True,
            "captures": {},
            "repeat": 1,
            "idempotency_key": None,
        },
    )
    raw["content_injection"] = {"step_id": "plant-comment", "content_pointer": "/body"}
    return raw


def test_content_injection_compiles_into_executable_identity() -> None:
    compiled = compile_target_adapter(_injection_adapter())

    assert isinstance(compiled.executable, HttpApi)
    injection = compiled.executable.content_injection
    assert injection is not None
    assert injection.step_id == "plant-comment"
    assert injection.content_pointer == "/body"
    plant = next(step for step in compiled.executable.steps if step.step_id == "plant-comment")
    assert plant.request_template == {"body": "{{input}}", "channel": "issue-1"}

    plain = compile_target_adapter(_http_adapter())
    assert plain.executable.content_injection is None
    assert compiled_target_adapter_digest(compiled) != compiled_target_adapter_digest(plain)


def test_content_injection_survives_compiled_envelope_roundtrip() -> None:
    compiled = compile_target_adapter(_injection_adapter())
    reread = read_target_adapter(compiled.model_dump(mode="json"))

    assert isinstance(reread.executable, HttpApi)
    assert reread.executable.content_injection == compiled.executable.content_injection
    assert compiled_target_adapter_digest(reread) == compiled_target_adapter_digest(compiled)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"step_id": "missing-step"}, "references an unknown adapter step"),
        ({"content_pointer": "/missing"}, "destination does not exist"),
        ({"content_pointer": "/channel"}, "exact canonical input placeholder"),
        ({"extra_key": "reviewed"}, "Extra inputs are not permitted"),
        ("not-an-object", "must be an object when provided"),
    ],
)
def test_content_injection_rejects_contract_violations(mutation: object, expected: str) -> None:
    raw = _injection_adapter()
    if mutation == "not-an-object":
        raw["content_injection"] = "reviewed-write"
    else:
        assert isinstance(mutation, dict)
        raw["content_injection"] = {**raw["content_injection"], **mutation}  # type: ignore[dict-item]

    with pytest.raises(AdapterCompileError) as caught:
        compile_target_adapter(raw)

    assert caught.value.kind is AdapterCompileFailureKind.CONTRACT_VALIDATION
    assert expected in str(caught.value)


def test_content_injection_requires_an_interaction_step() -> None:
    raw = _http_adapter()
    steps = raw["api_steps"]
    assert isinstance(steps, list)
    steps.insert(
        1,
        {
            "step_id": "plant-label",
            "endpoint": "/labels",
            "method": "POST",
            "request_template": {"body": "{{input}}"},
            "interaction": False,
            "captures": {},
        },
    )
    raw["content_injection"] = {"step_id": "plant-label", "content_pointer": "/body"}

    with pytest.raises(AdapterCompileError, match="must reference an interaction step"):
        compile_target_adapter(raw)


@pytest.mark.parametrize("pointer", ["body", "//body", "/items/00", "/body~2value"])
def test_content_injection_rejects_noncanonical_json_pointers(pointer: str) -> None:
    raw = _injection_adapter()
    raw["content_injection"] = {"step_id": "plant-comment", "content_pointer": pointer}

    with pytest.raises(AdapterCompileError):
        compile_target_adapter(raw)


def test_content_injection_rejects_multiple_input_placeholders() -> None:
    raw = _injection_adapter()
    steps = raw["api_steps"]
    assert isinstance(steps, list)
    plant = next(step for step in steps if step["step_id"] == "plant-comment")
    plant["request_template"]["duplicate"] = "{{input}}"

    with pytest.raises(AdapterCompileError, match="exactly one canonical input placeholder"):
        compile_target_adapter(raw)


def test_compiled_envelope_cannot_bypass_content_injection_invariant() -> None:
    compiled = compile_target_adapter(_injection_adapter()).model_dump(mode="json")
    compiled["executable"]["steps"][2]["request_template"]["body"] = "fixed"

    with pytest.raises(AdapterCompileError, match="exact canonical input placeholder"):
        read_target_adapter(compiled)


@pytest.mark.parametrize("interface", ["api", "open_ai_compatible", "browser"])
def test_content_injection_rejected_for_non_http_interfaces(interface: str) -> None:
    raw = _http_adapter()
    raw["interface"] = interface
    raw["content_injection"] = {"step_id": "message", "content_pointer": "/prompt"}

    with pytest.raises(AdapterCompileError, match="only for http_api"):
        compile_target_adapter(raw)
