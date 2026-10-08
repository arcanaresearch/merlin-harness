from copy import deepcopy

from merlin_contracts import (
    EXECUTABLE_ADAPTER_IDENTITY_VERSION,
    executable_adapter_digest,
    executable_adapter_projection,
    reviewed_adapter_cleanup_lifecycle,
    reviewed_adapter_target_delivery,
)

_LEGACY_V1_PROJECTION = {
    "version": "merlin.executable-adapter.v1",
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
    "browser_steps": [],
    "browser_interaction": {
        "input_locator": None,
        "submit_action": None,
        "frame_context": None,
    },
}
_LEGACY_V1_DIGEST = "sha256:6d52c30812ba937fabdae547f217060b107ff0fdc4530516e6503022b83201eb"


def _api_adapter() -> dict[str, object]:
    return {
        "interface": "http_api",
        "endpoint": "https://target.test",
        "credential_handle": "credential:target",
        "api_transport": "direct",
        "api_steps": [
            {
                "step_id": "send-message",
                "tool": "target_request",
                "endpoint": "/messages",
                "method": "POST",
                "request_template": {"message": "{{prompt}}"},
                "response_selector": "/reply",
                "success_witness": "reply exists",
                "expected_status": 200,
                "oracle_predicate": {"pointer": "/won", "match_values": [True]},
                "interaction": True,
                "cleanup": False,
                "captures": {},
                "repeat": 1,
                "idempotency_key": None,
            }
        ],
        "observed_surfaces": ["conversational"],
        "language_policy": {"supported_languages": ["en"]},
    }


def test_adapter_digest_always_uses_one_versioned_transport_identity() -> None:
    unversioned = _api_adapter()
    versioned = deepcopy(unversioned)
    versioned["adapter_identity_version"] = EXECUTABLE_ADAPTER_IDENTITY_VERSION

    assert executable_adapter_projection(unversioned)["version"] == (
        EXECUTABLE_ADAPTER_IDENTITY_VERSION
    )
    assert executable_adapter_digest(unversioned) == executable_adapter_digest(versioned)


def test_legacy_v1_adapter_projection_and_digest_are_frozen() -> None:
    adapter = _api_adapter()

    assert executable_adapter_projection(adapter) == _LEGACY_V1_PROJECTION
    assert executable_adapter_digest(adapter) == _LEGACY_V1_DIGEST


def test_adapter_digest_ignores_evaluator_and_reconnaissance_metadata() -> None:
    original = _api_adapter()
    revised = deepcopy(original)
    step = revised["api_steps"][0]
    assert isinstance(step, dict)
    step.update(
        {
            "step_id": "renamed-step",
            "response_selector": "/different/reply",
            "success_witness": "different evaluator prose",
            "expected_status": 202,
            "oracle_predicate": {"pointer": "/score", "match_values": [99]},
        }
    )
    revised["observed_surfaces"] = ["memory", "rag"]
    revised["language_policy"] = {"supported_languages": ["lzh", "tok"]}

    assert executable_adapter_digest(original) == executable_adapter_digest(revised)


def test_adapter_digest_changes_when_target_effect_changes() -> None:
    original = _api_adapter()
    revised = deepcopy(original)
    step = revised["api_steps"][0]
    assert isinstance(step, dict)
    step["request_template"] = {"message": "changed {{prompt}}"}

    assert executable_adapter_digest(original) != executable_adapter_digest(revised)


def test_cleanup_lifecycle_is_canonical_and_bound_without_provenance() -> None:
    unknown = _api_adapter()
    unknown["cleanup_lifecycle"] = {
        "status": "not_established",
        "evidence_refs": ["evidence/one"],
    }
    revised_provenance = deepcopy(unknown)
    revised_provenance["cleanup_lifecycle"] = {
        "status": "not_established",
        "evidence_refs": ["evidence/two"],
    }
    not_required = deepcopy(unknown)
    not_required["cleanup_lifecycle"] = {
        "status": "not_required",
        "evidence_refs": ["evidence/stateless-contract"],
    }

    assert reviewed_adapter_cleanup_lifecycle(unknown) == "unknown"
    assert reviewed_adapter_cleanup_lifecycle(not_required) == "not_required"
    assert executable_adapter_digest(unknown) == executable_adapter_digest(revised_provenance)
    assert executable_adapter_digest(unknown) != executable_adapter_digest(not_required)


def test_cleanup_discovery_hold_is_bound_into_adapter_identity() -> None:
    unknown = _api_adapter()
    unknown["cleanup_lifecycle"] = {"status": "not_established", "evidence_refs": []}
    required = deepcopy(unknown)
    required["cleanup_discovery"] = {
        "status": "required_before_target_execution",
        "session_ownership": "owned",
        "failure_code": "cleanup_discovery_required",
        "retryable": True,
    }
    not_owned = deepcopy(required)
    not_owned["cleanup_discovery"]["session_ownership"] = "not_owned"
    not_owned["cleanup_discovery"]["failure_code"] = "session_not_owned"

    assert executable_adapter_digest(unknown) != executable_adapter_digest(required)
    assert executable_adapter_digest(required) != executable_adapter_digest(not_owned)


def test_cleanup_step_is_required_even_when_metadata_disagrees() -> None:
    adapter = _api_adapter()
    adapter["cleanup_lifecycle"] = {"status": "not_required", "evidence_refs": []}
    steps = adapter["api_steps"]
    assert isinstance(steps, list)
    steps.append(
        {
            "step_id": "cleanup",
            "endpoint": "/cleanup",
            "method": "POST",
            "request_template": {},
            "interaction": False,
            "cleanup": True,
            "captures": {},
            "repeat": 1,
            "idempotency_key": None,
        }
    )

    assert reviewed_adapter_cleanup_lifecycle(adapter) == "required"


def test_adapter_digest_binds_ambiguous_result_replay_semantics() -> None:
    replay_disabled = _api_adapter()
    disabled_step = replay_disabled["api_steps"][0]
    assert isinstance(disabled_step, dict)
    disabled_step["replay_after_ambiguous_result"] = False

    replay_enabled = deepcopy(replay_disabled)
    enabled_step = replay_enabled["api_steps"][0]
    assert isinstance(enabled_step, dict)
    enabled_step["replay_after_ambiguous_result"] = True

    assert executable_adapter_digest(replay_disabled) != executable_adapter_digest(replay_enabled)


def test_adapter_delivery_is_explicit_and_part_of_effect_identity() -> None:
    unknown = _api_adapter()
    peer = deepcopy(unknown)
    peer["target_delivery"] = "inter-agent-message"

    assert reviewed_adapter_target_delivery(unknown) is None
    assert reviewed_adapter_target_delivery(peer) == "inter-agent-message"
    assert executable_adapter_digest(unknown) != executable_adapter_digest(peer)


def test_absent_terminal_response_contract_preserves_legacy_projection() -> None:
    legacy = _api_adapter()
    declared = deepcopy(legacy)
    step = declared["api_steps"][0]
    assert isinstance(step, dict)
    step["terminal_response_contract"] = {"pointer": "/error", "match_values": [True]}

    legacy_step = executable_adapter_projection(legacy)["api_steps"][0]
    assert "terminal_response_contract" not in legacy_step
    assert executable_adapter_digest(legacy) != executable_adapter_digest(declared)


def test_terminal_lifecycle_conditions_are_bound_into_adapter_identity() -> None:
    original = _api_adapter()
    declared = deepcopy(original)
    step = declared["api_steps"][0]
    assert isinstance(step, dict)
    contract = {
        "conditions": [
            {
                "pointer": "/gameStatus",
                "match_values": ["lost"],
                "status_codes": [200],
                "effect_applied": True,
                "replay_safe": False,
            },
            {
                "pointer": "/error",
                "match_values": ["Game already ended"],
                "status_codes": [400],
                "effect_applied": False,
                "replay_safe": True,
            },
        ]
    }
    step["terminal_response_contract"] = contract

    projected_step = executable_adapter_projection(declared)["api_steps"][0]
    assert projected_step["terminal_response_contract"] == contract
    assert executable_adapter_digest(original) != executable_adapter_digest(declared)

    changed = deepcopy(declared)
    changed_step = changed["api_steps"][0]
    assert isinstance(changed_step, dict)
    changed_contract = changed_step["terminal_response_contract"]
    assert isinstance(changed_contract, dict)
    conditions = changed_contract["conditions"]
    assert isinstance(conditions, list)
    conditions[1]["match_values"] = ["Different reviewed terminal error"]
    assert executable_adapter_digest(declared) != executable_adapter_digest(changed)


def test_terminal_provenance_does_not_change_executable_adapter_identity() -> None:
    observed = _api_adapter()
    observed_step = observed["api_steps"][0]
    assert isinstance(observed_step, dict)
    observed_step["terminal_response_contract"] = {
        "pointer": "/error",
        "match_values": ["Game already ended"],
        "status_codes": [400],
        "effect_applied": False,
        "replay_safe": True,
        "predicate_observation": {
            "source": "observed_terminal_exchange",
            "evidence_refs": ["evidence:terminal-a"],
        },
        "effect_attestation": {"source": "operator_reviewed_contract"},
    }
    reviewed = deepcopy(observed)
    reviewed_step = reviewed["api_steps"][0]
    assert isinstance(reviewed_step, dict)
    reviewed_contract = reviewed_step["terminal_response_contract"]
    assert isinstance(reviewed_contract, dict)
    reviewed_contract["predicate_observation"] = {
        "source": "reviewed_explicit_contract",
        "evidence_refs": ["evidence:terminal-b"],
    }

    assert executable_adapter_projection(observed) == executable_adapter_projection(reviewed)
    assert executable_adapter_digest(observed) == executable_adapter_digest(reviewed)


def test_session_limit_observations_do_not_change_executable_adapter_identity() -> None:
    original = _api_adapter()
    observed = deepcopy(original)
    observed["session_cardinality"] = {
        "status": "unverified",
        "maximum_concurrency": None,
        "source": "observed_session_lifecycle",
        "evidence_refs": ["evidence:setup", "evidence:message"],
    }
    observed["interaction_limit"] = {
        "status": "unverified",
        "maximum_interactions": None,
        "source": "observed_terminal_exchange",
        "evidence_refs": ["evidence:terminal"],
    }

    assert executable_adapter_projection(observed) == executable_adapter_projection(original)
    assert executable_adapter_digest(observed) == executable_adapter_digest(original)


def test_session_creation_transition_semantics_change_executable_identity() -> None:
    original = _api_adapter()
    transitioned = deepcopy(original)
    transitioned["session_creation_transition"] = {
        "precondition": {
            "method": "GET",
            "url": "https://target.test/active",
            "status_codes": [404],
            "pointer": "/error",
            "match_values": ["no_active_session"],
        },
        "postcondition": {
            "method": "GET",
            "url": "https://target.test/active",
            "status_codes": [200],
            "pointer": "/sessionId",
        },
        "evidence_refs": ["ignored-provenance"],
    }

    assert executable_adapter_digest(original) != executable_adapter_digest(transitioned)
    changed_provenance = deepcopy(transitioned)
    changed_provenance["session_creation_transition"]["evidence_refs"] = ["other"]
    changed_provenance["session_creation_transition"]["arbitrary_note"] = "ignored"
    assert executable_adapter_digest(transitioned) == executable_adapter_digest(changed_provenance)
    changed_predicate = deepcopy(transitioned)
    changed_predicate["session_creation_transition"]["precondition"]["match_values"] = [
        "active_session"
    ]
    assert executable_adapter_digest(transitioned) != executable_adapter_digest(changed_predicate)


def test_cleanup_acknowledgement_semantics_are_bound_but_provenance_is_not() -> None:
    original = _api_adapter()
    cleanup = {
        "step_id": "cleanup",
        "tool": "target_request",
        "endpoint": "/cleanup",
        "method": "POST",
        "request_template": {},
        "interaction": False,
        "cleanup": True,
        "captures": {},
        "repeat": 1,
        "idempotency_key": None,
        "cleanup_acknowledgement": {
            "pointer": "/cleaned",
            "match_values": [True],
            "status_codes": [200],
        },
        "cleanup_action_evidence_ref": "browser-action:sha256:" + "a" * 64,
        "cleanup_acknowledgement_observation": {
            "source": "derived_unique_observed_scalar",
            "evidence_refs": ["evidence:cleanup-a"],
        },
    }
    observed = deepcopy(original)
    observed["api_steps"].append(cleanup)
    different_provenance = deepcopy(observed)
    different_provenance["api_steps"][1]["cleanup_acknowledgement_observation"] = {
        "source": "reviewed_explicit_contract",
        "evidence_refs": ["evidence:cleanup-b"],
    }
    different_semantics = deepcopy(observed)
    different_semantics["api_steps"][1]["cleanup_acknowledgement"]["match_values"] = [False]
    different_action = deepcopy(observed)
    different_action["api_steps"][1]["cleanup_action_evidence_ref"] = (
        "browser-action:sha256:" + "b" * 64
    )

    assert executable_adapter_digest(observed) == executable_adapter_digest(different_provenance)
    assert executable_adapter_digest(observed) != executable_adapter_digest(different_semantics)
    assert executable_adapter_digest(observed) != executable_adapter_digest(different_action)


def test_browser_adapter_identity_ignores_step_labels_but_tracks_actions() -> None:
    original = {
        "interface": "browser",
        "endpoint": "https://target.test/chat",
        "browser_steps": [
            {
                "step_id": "submit-message",
                "tool": "browser_click",
                "arguments": {"locator": "button Send"},
            }
        ],
        "interaction_contract": {
            "input_locator": "textbox Message",
            "submit_action": "click Send",
            "frame_context": None,
            "success_indicator": "reply rendered",
        },
    }
    renamed = deepcopy(original)
    renamed["browser_steps"][0]["step_id"] = "different-label"
    changed = deepcopy(original)
    changed["browser_steps"][0]["arguments"] = {"locator": "button Delete"}

    assert executable_adapter_digest(original) == executable_adapter_digest(renamed)
    assert executable_adapter_digest(original) != executable_adapter_digest(changed)
