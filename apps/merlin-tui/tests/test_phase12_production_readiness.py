import asyncio
import hashlib
import json
import os
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from merlin_agents import (
    AssessmentPrepareTool,
    AssignmentBudget,
    AssignmentContext,
    AssignmentTechnique,
    CampaignManifest,
    CampaignProfile,
    ReviewedTargetAdapter,
    compile_initial_campaign,
)
from merlin_agents.campaign import (
    enumerate_language_candidate_references,
    materialize_language_technique,
)
from merlin_agents.campaign_service import _json_value
from merlin_agents.transforms import materialize_transform_envelope
from merlin_assessments import ReviewedApiTargetTool
from merlin_browser import (
    ApiReconHandoff,
    BrowserAction,
    BrowserBounds,
    BrowserMetadata,
    BrowserObservation,
    BrowserOwner,
    ElementRef,
    TabMetadata,
    browser_tools,
    launch_isolated,
    normalize_api_adapter_input_aliases,
)
from merlin_browser.core import BrowserSession
from merlin_config import Config, ConfigPaths, CredentialCheckpointVault, CredentialStore
from merlin_contracts import (
    AgentTask,
    ChatMessage,
    EvidenceWitness,
    FindingDraft,
    ModelCompleted,
    ModelError,
    ModelUsage,
    PromotionCandidate,
    ReasoningDelta,
    TextDelta,
    ToolContext,
    ToolEffectRecord,
    ToolEffectState,
    ToolResult,
    ToolSpec,
    executable_adapter_digest,
    promotion_digest,
    reviewed_adapter_cleanup_lifecycle,
)
from merlin_contracts.target_adapter_compiler import (
    compiled_target_adapter_digest,
    read_target_adapter,
)
from merlin_findings import DurableReportService
from merlin_kernel import Kernel
from merlin_promotion import EvaluationCase, frozen_dataset_digest
from merlin_providers import ProviderCapabilities
from merlin_storage_postgres import (
    CampaignApiSessionRepository,
    CampaignStore,
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    PostgresBrowserStore,
    PostgresReportRevisionStore,
    PostgresSessionStore,
    PostgresToolEffectStore,
    SessionRepository,
    ToolEffectRepository,
)
from merlin_tools import ToolBroker
from merlin_tui.bootstrap import (
    AdapterStateIntegrityError,
    CleanupDiscoveryRequiredError,
    _adapter_transport_effect_identity,
    _compile_current_reviewed_adapter_state,
    _compile_objective_phase_gate_audit,
    _has_durable_handoff,
    _hydrate_handoff_adapter,
    _persisted_reviewed_api_adapter,
    _stateful_api_cleanup_discovery,
    build_backend,
)
from merlin_tui.kernel_backend import KernelBackend
from merlin_tui.local_control import LocalControlClient, serve, stop_daemon


class _NoTools:
    def describe(self):
        return ()


class _NoPrerequisiteAuditProvider:
    def __init__(self, config) -> None:
        self.config = config

    async def stream(self, request):
        yield TextDelta(
            text=(
                '{"requirements":[],"gate_proposals":[],'
                '"unresolved_requirements":[]}'
            )
        )
        yield ModelCompleted()


def test_objective_phase_gate_audit_compiles_semantics_into_valid_structure() -> None:
    objective = "Authenticate with Test Wallet. Learn the rules and play normally first."
    gates = _compile_objective_phase_gate_audit(
        objective,
        {
            "api_steps": [
                {"step_id": "read-state", "interaction": False},
                {"step_id": "play-turn", "interaction": True},
            ],
            "state_observations": [
                {"name": "authenticated"},
                {"name": "turns_completed"},
            ],
        },
        (),
        {
            "requirements": [
                {
                    "source_quote": "Authenticate with Test Wallet",
                    "gate_key": "authenticated-session",
                },
                {
                    "source_quote": "play normally first",
                    "gate_key": "normal-play-baseline",
                },
            ],
            "gate_proposals": [
                {
                    "key": "authenticated-session",
                    "objective": "Verify the reviewed Test Wallet session",
                    "actions": [{"step_id": "read-state"}],
                    "success_predicate": {
                        "observation_name": "authenticated",
                        "operator": "equals",
                        "expected_values": [True],
                    },
                },
                {
                    "key": "normal-play-baseline",
                    "objective": "Complete one normal-play turn",
                    "actions": [
                        {"step_id": "play-turn", "input_text": "Play a normal turn"}
                    ],
                    "success_predicate": {
                        "observation_name": "turns_completed",
                        "operator": "increases",
                        "expected_values": [],
                    },
                },
            ],
            "unresolved_requirements": [],
        },
    )

    assert [gate["id"] for gate in gates] == [
        "phase-gate/authenticated-session",
        "phase-gate/normal-play-baseline",
    ]
    assert [gate["ordinal"] for gate in gates] == [0, 1]
    assert gates[0]["actions"] == [
        {
            "id": "action-1",
            "step_id": "read-state",
            "input_text": "Verify the reviewed Test Wallet session",
        }
    ]
    assert gates[1]["budget"] == {"max_attempts": 2, "max_target_calls": 2}
    assert gates[1]["retain_success"] == "recipe"


def test_objective_phase_gate_audit_can_narrow_an_overbroad_existing_gate() -> None:
    objective = "Learn the rules and play normally first. Test Tier 1 through Tier 4."
    gates = _compile_objective_phase_gate_audit(
        objective,
        {
            "api_steps": [{"step_id": "play-turn", "interaction": True}],
            "state_observations": [{"name": "turn_number"}],
        },
        (
            {
                "id": "normal-play-first",
                "ordinal": 0,
                "objective": "Play through every tier before attacks",
                "actions": [
                    {
                        "id": "legacy-action",
                        "step_id": "play-turn",
                        "input_text": "Keep playing through every tier",
                    }
                ],
                "success_predicate": {
                    "observation_name": "turn_number",
                    "operator": "at_least",
                    "expected_values": [30],
                },
                "budget": {"max_attempts": 60, "max_target_calls": 60},
                "retry_on": ["infrastructure_failure"],
                "retain_success": "recipe",
                "operator_waivable": False,
            },
        ),
        {
            "requirements": [
                {
                    "source_quote": "play normally first",
                    "gate_key": "normal-play-first",
                }
            ],
            "gate_proposals": [
                {
                    "key": "normal-play-first",
                    "objective": "Complete one ordinary turn before attacks",
                    "actions": [
                        {"step_id": "play-turn", "input_text": "Play one ordinary turn"}
                    ],
                    "success_predicate": {
                        "observation_name": "turn_number",
                        "operator": "increases",
                        "expected_values": [],
                    },
                }
            ],
            "unresolved_requirements": [],
        },
    )

    assert gates[0]["id"] == "normal-play-first"
    assert gates[0]["objective"] == "Complete one ordinary turn before attacks"
    assert gates[0]["actions"] == [
        {"id": "action-1", "step_id": "play-turn", "input_text": "Play one ordinary turn"}
    ]
    assert gates[0]["budget"] == {"max_attempts": 2, "max_target_calls": 2}


def test_objective_phase_gate_audit_rejects_identity_role_and_blank_values() -> None:
    objective = "Authenticate with Test Wallet."
    adapter = {
        "api_steps": [{"step_id": "read-state", "interaction": False}],
        "state_observations": [
            {"name": "run_id", "role": "state_identity"},
            {"name": "run_status", "role": "terminal_status"},
        ],
    }
    audit = {
        "requirements": [
            {"source_quote": "Authenticate with Test Wallet", "gate_key": "auth"}
        ],
        "gate_proposals": [
            {
                "key": "auth",
                "objective": "Verify the authenticated run identity",
                "actions": [{"step_id": "read-state"}],
                "success_predicate": {
                    "observation_name": "run_id",
                    "operator": "present",
                    "expected_values": [],
                },
            }
        ],
        "unresolved_requirements": [],
    }

    with pytest.raises(ValueError, match="unknown authoritative observation 'run_id'"):
        _compile_objective_phase_gate_audit(objective, adapter, (), audit)

    audit["gate_proposals"][0]["success_predicate"]["observation_name"] = "run_status"
    gates = _compile_objective_phase_gate_audit(objective, adapter, (), audit)
    assert gates[0]["success_predicate"] == {
        "observation_name": "run_status",
        "operator": "present",
        "expected_values": [],
    }

    audit["gate_proposals"][0]["success_predicate"] = {
        "observation_name": "run_status",
        "operator": "equals",
        "expected_values": [""],
    }
    with pytest.raises(ValueError, match="cannot use blank expected values"):
        _compile_objective_phase_gate_audit(objective, adapter, (), audit)


def test_objective_phase_gate_audit_allows_reviewed_interaction_receipt() -> None:
    objective = "Play normally first."
    adapter = {
        "api_steps": [
            {
                "step_id": "play-turn",
                "interaction": True,
                "method": "POST",
            }
        ],
    }
    audit = {
        "requirements": [
            {"source_quote": "Play normally first", "gate_key": "normal-play"}
        ],
        "gate_proposals": [
            {
                "key": "normal-play",
                "objective": "Complete one ordinary turn before attacks",
                "actions": [{"step_id": "play-turn", "input_text": "Hello"}],
                "success_predicate": {
                    "observation_name": "target_exchange_completed",
                    "operator": "present",
                    "expected_values": [],
                },
            }
        ],
        "unresolved_requirements": [],
    }

    gates = _compile_objective_phase_gate_audit(objective, adapter, (), audit)

    assert gates[0]["success_predicate"] == {
        "observation_name": "target_exchange_completed",
        "operator": "present",
        "expected_values": [],
    }


def test_objective_phase_gate_audit_rejects_receipt_for_read_only_step() -> None:
    objective = "Verify setup."
    adapter = {
        "api_steps": [{"step_id": "read-state", "interaction": False, "method": "GET"}],
    }
    audit = {
        "requirements": [{"source_quote": "Verify setup", "gate_key": "setup"}],
        "gate_proposals": [
            {
                "key": "setup",
                "objective": "Verify setup",
                "actions": [{"step_id": "read-state"}],
                "success_predicate": {
                    "observation_name": "target_exchange_completed",
                    "operator": "present",
                    "expected_values": [],
                },
            }
        ],
        "unresolved_requirements": [],
    }

    with pytest.raises(
        ValueError,
        match="unknown authoritative observation 'target_exchange_completed'",
    ):
        _compile_objective_phase_gate_audit(objective, adapter, (), audit)


def test_objective_phase_gate_audit_can_explicitly_reject_overbroad_proposal() -> None:
    objective = "Play normally first."
    adapter = {
        "api_steps": [{"step_id": "play-turn", "interaction": True}],
    }
    proposed = (
        {
            "id": "attack-methods",
            "ordinal": 0,
            "objective": "Run every suggested attack method",
            "actions": [
                {"id": "action-1", "step_id": "play-turn", "input_text": "attack"}
            ],
            "success_predicate": {
                "observation_name": "target_exchange_completed",
                "operator": "present",
                "expected_values": [],
            },
            "budget": {"max_attempts": 2, "max_target_calls": 2},
        },
    )
    audit = {
        "requirements": [],
        "gate_proposals": [],
        "rejected_gates": [
            {
                "key": "attack-methods",
                "reason": "Suggested attack methods are campaign guidance, not prerequisites.",
            }
        ],
        "unresolved_requirements": [],
    }

    assert _compile_objective_phase_gate_audit(objective, adapter, proposed, audit) == ()


def test_objective_phase_gate_audit_rejects_identity_selector_as_observation() -> None:
    objective = "Authenticate with Test Wallet."
    adapter = {
        "api_steps": [{"step_id": "read-state", "interaction": False}],
        "state_capabilities": {
            "identity": {
                "name": "run_id",
                "result_selector": {
                    "step_id": "read-state",
                    "evaluation_root": "response_body",
                    "pointer": "/run/id",
                },
            },
            "observations": [{"name": "run_status"}],
        },
    }
    audit = {
        "requirements": [
            {"source_quote": "Authenticate with Test Wallet", "gate_key": "auth"}
        ],
        "gate_proposals": [
            {
                "key": "auth",
                "objective": "Verify the authenticated run",
                "actions": [{"step_id": "read-state"}],
                "success_predicate": {
                    "observation_name": "run_id",
                    "operator": "present",
                    "expected_values": [],
                },
            }
        ],
        "unresolved_requirements": [],
    }

    with pytest.raises(ValueError, match="unknown authoritative observation 'run_id'"):
        _compile_objective_phase_gate_audit(objective, adapter, (), audit)

    audit["gate_proposals"][0]["success_predicate"]["observation_name"] = "run_status"
    gates = _compile_objective_phase_gate_audit(objective, adapter, (), audit)
    assert gates[0]["success_predicate"]["observation_name"] == "run_status"


def test_objective_phase_gate_audit_rejects_unreviewed_adapter_bindings() -> None:
    with pytest.raises(ValueError, match="unknown adapter step"):
        _compile_objective_phase_gate_audit(
            "Play normally first.",
            {
                "api_steps": [{"step_id": "play-turn"}],
                "state_observations": [{"name": "turns_completed"}],
            },
            (),
            {
                "requirements": [
                    {"source_quote": "Play normally first", "gate_key": "baseline"}
                ],
                "gate_proposals": [
                    {
                        "key": "baseline",
                        "objective": "Complete a normal turn",
                        "actions": [{"step_id": "invented-step"}],
                        "success_predicate": {
                            "observation_name": "turns_completed",
                            "operator": "increases",
                            "expected_values": [],
                        },
                    }
                ],
                "unresolved_requirements": [],
            },
        )


def test_current_reviewed_adapter_state_compiles_genuinely_unsealed_legacy_input() -> None:
    compiled = _compile_current_reviewed_adapter_state(
        {
            "interface": "open_ai_compatible",
            "endpoint": "https://target.test/v1",
            "authentication": "opaque reviewed credential handle",
        }
    )

    assert isinstance(compiled["compiled_adapter"], str)
    assert (
        compiled_target_adapter_digest(read_target_adapter(compiled["compiled_adapter"]))
        == compiled["compiled_adapter_digest"]
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("compiled_adapter", None, "serialization is malformed"),
        ("compiled_adapter", {}, "serialization is malformed"),
        ("compiled_adapter", [], "serialization is malformed"),
        ("compiled_adapter", 17, "serialization is malformed"),
        ("compiled_adapter_digest", None, "digest is malformed"),
        ("compiled_adapter_digest", {}, "digest is malformed"),
        ("compiled_adapter_digest", [], "digest is malformed"),
        ("compiled_adapter_digest", 17, "digest is malformed"),
    ),
)
def test_current_reviewed_adapter_state_rejects_present_non_string_seal_fields(
    field: str, value: object, message: str
) -> None:
    sealed = _compile_current_reviewed_adapter_state(
        {
            "interface": "open_ai_compatible",
            "endpoint": "https://target.test/v1",
            "authentication": "opaque reviewed credential handle",
        }
    )
    sealed[field] = value

    with pytest.raises(AdapterStateIntegrityError, match=message):
        _compile_current_reviewed_adapter_state(sealed)


@pytest.mark.parametrize("missing", ("compiled_adapter", "compiled_adapter_digest"))
def test_current_reviewed_adapter_state_rejects_partial_seal(missing: str) -> None:
    sealed = _compile_current_reviewed_adapter_state(
        {
            "interface": "open_ai_compatible",
            "endpoint": "https://target.test/v1",
            "authentication": "opaque reviewed credential handle",
        }
    )
    sealed.pop(missing)

    with pytest.raises(AdapterStateIntegrityError, match="are incomplete"):
        _compile_current_reviewed_adapter_state(sealed)


def test_adapter_reuse_identity_ignores_evaluator_and_reconnaissance_metadata() -> None:
    base = {
        "interface": "http_api",
        "endpoint": "https://target.test/v1",
        "credential_handle": "target-token",
        "api_steps": [
            {
                "step_id": "message",
                "endpoint": "/message",
                "method": "POST",
                "request_template": {"message": "{{input}}"},
                "response_selector": "/reply",
                "interaction": True,
                "cleanup": False,
                "repeat": 1,
                "oracle_predicate": {"language": "en", "match_values": ["won"]},
                "success_witness": "recon prose",
            }
        ],
        "language_policy": {"supported_languages": ["en"]},
        "observed_surfaces": ["conversational"],
        "scenario_identity": {"scenario": "first label"},
    }
    revised = json.loads(json.dumps(base))
    revised["api_steps"][0]["oracle_predicate"] = {
        "language": "lzh",
        "match_values": ["different evaluator hint"],
    }
    revised["api_steps"][0]["success_witness"] = "better prose"
    revised["api_steps"][0]["step_id"] = "renamed evaluator step"
    revised["language_policy"] = {"supported_languages": ["lzh", "tok"]}
    revised["observed_surfaces"] = ["persistent-memory"]
    revised["scenario_identity"] = {"scenario": "revised label"}

    assert _adapter_transport_effect_identity(base) == _adapter_transport_effect_identity(revised)

    revised["api_steps"][0]["endpoint"] = "/different-effect"
    assert _adapter_transport_effect_identity(base) != _adapter_transport_effect_identity(revised)


def test_handoff_hydration_preserves_browser_fallback_repair_fields() -> None:
    sealed = {
        "interface": "browser",
        "authentication": "reviewed browser session",
        "observed_evidence": ["evidence:request", "evidence:response"],
        "interaction_contract": {"input_locator": "textbox"},
    }
    repaired = {
        "recon_handoff_id": "handoff:ignored-provider-copy",
        "browser_steps": [{"step_id": "send", "tool": "browser_click", "arguments": {}}],
    }

    hydrated = _hydrate_handoff_adapter(
        sealed,
        repaired,
        handoff_id="handoff:sealed",
        dom_fallback_reason="No equivalent API lifecycle was observed.",
    )

    assert hydrated["interface"] == "browser"
    assert hydrated["interaction_contract"] == {"input_locator": "textbox"}
    assert hydrated["browser_steps"] == repaired["browser_steps"]
    assert hydrated["recon_handoff_id"] == "handoff:sealed"
    assert hydrated["dom_fallback_reason"] == "No equivalent API lifecycle was observed."


def test_handoff_hydration_never_allows_repair_overlay_to_elevate_session_ownership() -> None:
    hydrated = _hydrate_handoff_adapter(
        {
            "interface": "http_api",
            "cleanup_discovery": {
                "status": "required_before_target_execution",
                "session_ownership": "unknown",
                "failure_code": "session_not_owned",
                "retryable": True,
            },
        },
        {
            "cleanup_discovery": {
                "status": "required_before_target_execution",
                "session_ownership": "owned",
                "failure_code": "cleanup_discovery_required",
                "retryable": True,
            },
            "session_creation_transition": {
                "precondition": {"method": "GET"},
                "postcondition": {"method": "GET"},
                "evidence_refs": ["forged:model-overlay"],
            },
        },
        handoff_id="handoff:sealed-unknown",
        dom_fallback_reason=None,
    )

    assert hydrated["cleanup_discovery"]["session_ownership"] == "unknown"
    assert "session_creation_transition" not in hydrated


def test_lifecycle_semantic_mutation_changes_adapter_identity() -> None:
    sealed = {
        "interface": "http_api",
        "endpoint": "https://target.test/api",
        "cleanup_discovery": {
            "status": "required_before_target_execution",
            "session_ownership": "unknown",
            "failure_code": "session_not_owned",
            "retryable": True,
        },
        "api_steps": [],
    }
    mutated = json.loads(json.dumps(sealed))
    mutated["cleanup_discovery"] = {
        "status": "required_before_target_execution",
        "session_ownership": "owned",
        "failure_code": "cleanup_discovery_required",
        "retryable": True,
    }
    mutated["session_creation_transition"] = {
        "precondition": {"method": "GET"},
        "postcondition": {"method": "GET"},
        "evidence_refs": ["forged:post-seal"],
    }

    assert executable_adapter_digest(sealed) != executable_adapter_digest(mutated)


def test_partial_adapter_with_evidence_still_resolves_its_durable_handoff() -> None:
    assert _has_durable_handoff(
        {
            "recon_handoff_id": "handoff:reviewed",
            "observed_evidence": ["browser-action:sha256:observed"],
        }
    )


def test_stateful_unknown_cleanup_holds_execution_without_filtering_attacks() -> None:
    adapter = {
        "interface": "http_api",
        "cleanup_lifecycle": {"status": "not_established", "evidence_refs": []},
        "cleanup_discovery": {
            "status": "required_before_target_execution",
            "session_ownership": "owned",
            "failure_code": "cleanup_discovery_required",
            "retryable": True,
        },
        "session_creation_transition": {
            "precondition": {"method": "GET"},
            "postcondition": {"method": "GET"},
            "evidence_refs": ["creation:before", "creation:after"],
        },
        "api_steps": [
            {
                "step_id": "setup",
                "interaction": False,
                "cleanup": False,
                "captures": {"session_id": "/sessionId"},
            },
            {"step_id": "message", "interaction": True, "cleanup": False, "captures": {}},
        ],
    }

    discovery = _stateful_api_cleanup_discovery(adapter)

    assert discovery is not None
    assert discovery["session_ownership"] == "owned"
    assert discovery["ownership_evidence_refs"] == ["creation:before", "creation:after"]
    error = CleanupDiscoveryRequiredError("handoff:stateful", discovery or {})
    assert error.failure_code == "cleanup_discovery_required"
    assert error.retryable is True
    assert error.cleanup_discovery_required is True
    assert error.automatic_recon_continuation["session_ownership"] == "owned"
    assert error.automatic_recon_continuation["ownership_evidence_refs"] == [
        "creation:before",
        "creation:after",
    ]
    assert "browser_end_owned_session" in error.automatic_recon_continuation["allowed_tools"]


@pytest.mark.parametrize("ownership", ("owned", "unknown", "unowned"))
def test_explicit_cleanup_discovery_hold_dominates_capture_heuristic(
    ownership: str,
) -> None:
    adapter = {
        "interface": "http_api",
        "cleanup_lifecycle": {"status": "not_established", "evidence_refs": []},
        "cleanup_discovery": {
            "status": "required_before_target_execution",
            "session_ownership": ownership,
            "retryable": True,
        },
        "session_creation_transition": {
            "precondition": {"method": "GET"},
            "postcondition": {"method": "POST"},
            "evidence_refs": ["creation:before", "creation:after"],
        },
        "api_steps": [
            {
                "step_id": "message",
                "interaction": True,
                "cleanup": False,
                "captures": {},
            }
        ],
    }

    discovery = _stateful_api_cleanup_discovery(adapter)

    assert discovery is not None
    assert discovery["session_ownership"] == ownership
    assert discovery["failure_code"] == (
        "cleanup_discovery_required" if ownership == "owned" else "session_not_owned"
    )


def test_explicit_cleanup_hold_is_released_by_canonical_not_required_lifecycle() -> None:
    adapter = {
        "interface": "http_api",
        "cleanup_lifecycle": {"status": "not_required", "evidence_refs": ["review:stateless"]},
        "cleanup_discovery": {
            "status": "required_before_target_execution",
            "session_ownership": "unknown",
            "retryable": True,
        },
        "api_steps": [
            {
                "step_id": "message",
                "interaction": True,
                "cleanup": False,
                "captures": {},
            }
        ],
    }

    assert _stateful_api_cleanup_discovery(adapter) is None


@pytest.mark.asyncio
async def test_prepare_records_unresolved_cleanup_advisory_for_every_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "postgres-cleanup-holds"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "cleanup-hold-model",
                    },
                },
            ),
        )
        monkeypatch.setattr(
            ConfigPaths,
            "default",
            classmethod(lambda cls: ConfigPaths(tmp_path / "config-cleanup-holds")),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
        backend = build_backend(tmp_path / "workspace-cleanup-holds")
        _admit_synthetic_live_handoff(backend)
        try:
            session_id = await backend.kernel.start()
            owner = BrowserOwner(
                session_id=session_id,
                worker_id="recon",
                technique_id="api-first",
            )
            browser_store = backend._browser._store  # type: ignore[attr-defined]

            def unresolved_adapter(ownership: str) -> dict[str, object]:
                return {
                    "interface": "http_api",
                    "endpoint": "https://target.test/api",
                    "api_transport": "browser_context",
                    "authentication": "reviewed browser session",
                    "cleanup_lifecycle": {
                        "status": "not_established",
                        "evidence_refs": [],
                    },
                    "cleanup_discovery": {
                        "status": "required_before_target_execution",
                        "session_ownership": ownership,
                        "retryable": True,
                    },
                    "session_creation_transition": {
                        "precondition": {"method": "GET"},
                        "postcondition": {"method": "POST"},
                        "evidence_refs": ["creation:before", "creation:after"],
                    },
                    "api_steps": [
                        {
                            "step_id": "message",
                            "endpoint": "/message",
                            "method": "POST",
                            "request_template": {"message": "{{input}}"},
                            "response_selector": "/reply",
                            "interaction": True,
                            "cleanup": False,
                            "captures": {},
                            "repeat": 1,
                            "idempotency_key": None,
                            "replay_after_ambiguous_result": False,
                        }
                    ],
                }

            for source in ("inline", "durable"):
                for ownership in ("owned", "unknown", "unowned"):
                    adapter = unresolved_adapter(ownership)
                    if source == "durable":
                        context_id = f"cleanup-{ownership}"
                        handoff_id = f"handoff:cleanup-{ownership}"
                        now = datetime.now(UTC)
                        await browser_store.save_metadata(
                            BrowserMetadata(
                                context_id=context_id,
                                mode="isolated",
                                target_id="cleanup-target",
                                owner=owner,
                                allowed_origins=frozenset({"https://target.test"}),
                                tabs=(
                                    TabMetadata(
                                        tab_ref=f"tab-{ownership}",
                                        url="https://target.test/",
                                    ),
                                ),
                                state="active",
                                created_at=now,
                                updated_at=now,
                            )
                        )
                        await browser_store.record_evidence(
                            context_id,
                            f"request-{ownership}",
                            "network",
                            {"path": "/api/message"},
                        )
                        await browser_store.record_evidence(
                            context_id,
                            f"response-{ownership}",
                            "network",
                            {"status": 200},
                        )
                        await browser_store.record_handoff(
                            ApiReconHandoff(
                                id=handoff_id,
                                context_id=context_id,
                                target_id="cleanup-target",
                                owner=owner,
                                method="POST",
                                url="https://target.test/api/message",
                                request_evidence_ref=f"request-{ownership}",
                                response_evidence_ref=f"response-{ownership}",
                                adapter=json.dumps(adapter, sort_keys=True, separators=(",", ":")),
                            )
                        )
                        adapter = {"recon_handoff_id": handoff_id}

                    resolved = await backend._campaigns.resolve_reviewed_adapter(  # type: ignore[attr-defined]
                        session_id,
                        adapter,
                    )

                    # Advisory only: unresolved cleanup/ownership evidence is
                    # recorded on the adapter and never holds target admission.
                    discovery = resolved["cleanup_discovery"]
                    assert discovery["status"] == "required_before_target_execution"
                    assert discovery["failure_code"] == (
                        "cleanup_discovery_required"
                        if ownership == "owned"
                        else "session_not_owned"
                    )
                    uncertainties = resolved["evaluation_uncertainties"]
                    assert any(
                        item.get("source") == "reviewed_adapter_resolver.cleanup_discovery"
                        for item in uncertainties
                    )

            compatible = await backend._campaigns.resolve_reviewed_adapter(  # type: ignore[attr-defined]
                session_id,
                {"recon_handoff_id": "handoff:cleanup-owned"},
            )
            assert compatible["recon_handoff_id"] == "handoff:cleanup-owned"
            with pytest.raises(
                ValueError,
                match="reviewed reconnaissance handoff belongs to another target",
            ):
                await backend._campaigns.resolve_reviewed_adapter(  # type: ignore[attr-defined]
                    session_id,
                    {
                        "recon_handoff_id": "handoff:cleanup-owned",
                        "expected_target_id": "different-target",
                    },
                )

            resolved = await backend._campaigns.resolve_reviewed_adapter(  # type: ignore[attr-defined]
                session_id,
                {
                    **unresolved_adapter("owned"),
                    "cleanup_discovery": {
                        "status": "satisfied",
                        "session_ownership": "owned",
                        "retryable": False,
                    },
                    "api_steps": [
                        *unresolved_adapter("owned")["api_steps"],  # type: ignore[misc]
                        {
                            "step_id": "cleanup",
                            "endpoint": "/cleanup",
                            "method": "POST",
                            "request_template": {"sessionId": "{{session_id}}"},
                            "response_selector": "/closed",
                            "interaction": False,
                            "cleanup": True,
                            "captures": {},
                            "repeat": 1,
                            "idempotency_key": None,
                            "replay_after_ambiguous_result": False,
                            "cleanup_acknowledgement": {
                                "pointer": "/closed",
                                "match_values": [True],
                                "status_codes": [200],
                            },
                        },
                    ],
                },
            )
            assert reviewed_adapter_cleanup_lifecycle(resolved) == "required"
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_two_physical_recon_sessions_cleanup_and_prepare_only_the_selected_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A superseding handoff must stay on B from discovery through preparation."""
    data_dir = tmp_path / "postgres-two-physical-sessions"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "two-physical-sessions-model",
                    },
                },
            ),
        )
        monkeypatch.setattr(
            ConfigPaths,
            "default",
            classmethod(lambda cls: ConfigPaths(tmp_path / "config-two-physical-sessions")),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
        monkeypatch.setattr(
            "merlin_providers.OpenAICompatibleProvider", _NoPrerequisiteAuditProvider
        )
        backend = build_backend(tmp_path / "workspace-two-physical-sessions")
        _admit_synthetic_live_handoff(backend)
        try:
            console_session_id = await backend.kernel.start()
            target_id = "two-physical-sessions-target"
            await backend._assessments.add_target(  # type: ignore[attr-defined]
                (target_id, "Two physical sessions", "https://target.test/", "target-model")
            )
            owner = BrowserOwner(
                session_id=console_session_id,
                worker_id="recon",
                technique_id="api-first",
            )
            context_id = "two-physical-sessions-context"
            tab_ref = "tab-bank"
            now = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
            metadata = BrowserMetadata(
                context_id=context_id,
                mode="isolated",
                target_id=target_id,
                owner=owner,
                allowed_origins=frozenset({"https://target.test"}),
                tabs=(TabMetadata(tab_ref=tab_ref, url="https://target.test/bank"),),
                state="active",
                created_at=now,
                updated_at=now,
            )
            browser_store = backend._browser._store  # type: ignore[attr-defined]
            await browser_store.save_metadata(metadata)

            setup_action = BrowserAction(
                context_id=context_id,
                target_id=target_id,
                owner=owner,
                kind="click",
                tab_ref=tab_ref,
                frame_ref="main",
                element_ref="start-bank-l1",
                step=2,
                created_at=now + timedelta(seconds=10),
            )
            setup_ref = BrowserSession._evidence(setup_action)  # noqa: SLF001
            setup_action = setup_action.model_copy(update={"evidence_refs": (setup_ref,)})
            await browser_store.append_action(setup_action)
            await browser_store.append_observation(
                BrowserObservation(
                    context_id=context_id,
                    target_id=target_id,
                    origin="https://target.test",
                    owner=owner,
                    tab_ref=tab_ref,
                    frame_ref="main",
                    kind="snapshot",
                    step=1,
                    elements=(
                        ElementRef(
                            ref="start-bank-l1",
                            tab_ref=tab_ref,
                            frame_ref="main",
                            role="button",
                            name="Start Bank L1",
                        ),
                    ),
                    observed_at=now + timedelta(seconds=9),
                )
            )
            await browser_store.append_observation(
                BrowserObservation(
                    context_id=context_id,
                    target_id=target_id,
                    origin="https://target.test",
                    owner=owner,
                    tab_ref=tab_ref,
                    frame_ref="main",
                    kind="snapshot",
                    step=2,
                    evidence_refs=(setup_ref,),
                    observed_at=now + timedelta(seconds=11),
                )
            )
            session_a = "session-a"
            session_b = "session-b"
            events: list[dict[str, object]] = [
                {
                    "method": "POST",
                    "url": "https://target.test/api/bank/start",
                    "status": 200,
                    "request_json": {"levelId": 9},
                    "response_json": {"sessionId": session_a, "resumed": False},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/bank/message",
                    "status": 200,
                    "request_json": {"sessionId": session_a, "message": "old probe"},
                    "response_json": {"reply": "old response"},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/bank/forfeit",
                    "status": 200,
                    "request_json": {"sessionId": session_a},
                    "response_json": {"closed": True},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/bank/start",
                    "status": 200,
                    "observed_at": (now + timedelta(seconds=12)).isoformat(),
                    "_observation_step": 2,
                    "_observation_tab_ref": tab_ref,
                    "trigger_action_ref": setup_ref,
                    "request_json": {"levelId": 1},
                    "response_json": {"sessionId": session_b, "resumed": False},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/bank/message",
                    "status": 200,
                    "request_json": {"sessionId": session_b, "message": "selected probe"},
                    "response_json": {"reply": "selected response"},
                },
            ]
            cleanup_calls: list[str] = []

            class Recorder:
                def __getattr__(self, name: str):
                    return getattr(browser_store, name)

                async def reviewed_native_input_receipt(
                    self, requested_context_id, _event, input_pointer
                ):
                    return {
                        "context_id": requested_context_id,
                        "target_id": target_id,
                        "input_pointer": input_pointer,
                        "evidence_ref": "native-request:sha256:" + "e" * 64,
                    }

            class Session:
                mode = "isolated"

                def __init__(self) -> None:
                    self.context_id = context_id
                    self.bounds = SimpleNamespace(target_id=target_id)
                    self.owner = owner

                def api_exchange_events(self):
                    return tuple(events)

                def metadata(self, *, restart: bool = False):
                    return metadata.model_copy(update={"updated_at": now + timedelta(seconds=20)})

                async def snapshot(self, requested_tab_ref, frame_ref=None, *, step=0):
                    assert requested_tab_ref == tab_ref
                    return BrowserObservation(
                        context_id=context_id,
                        target_id=target_id,
                        origin="https://target.test",
                        owner=owner,
                        tab_ref=tab_ref,
                        frame_ref="main",
                        kind="snapshot",
                        step=step,
                        elements=(
                            ElementRef(
                                ref="end-session",
                                tab_ref=tab_ref,
                                frame_ref="main",
                                role="button",
                                name="End session",
                            ),
                        ),
                        observed_at=now + timedelta(seconds=20),
                    )

                async def act(self, action, *, actor_id="console"):
                    assert action.kind == "click"
                    assert action.element_ref == "end-session"
                    cleanup_calls.append(session_b)
                    action_ref = BrowserSession._evidence(action)  # noqa: SLF001
                    events.append(
                        {
                            "method": "POST",
                            "url": "https://target.test/api/bank/forfeit",
                            "status": 200,
                            "observed_at": (action.created_at + timedelta(seconds=1)).isoformat(),
                            "trigger_action_ref": action_ref,
                            "request_json": {"sessionId": session_b},
                            "response_json": {"closed": True},
                        }
                    )
                    return BrowserObservation(
                        context_id=context_id,
                        target_id=target_id,
                        origin="https://target.test",
                        owner=owner,
                        tab_ref=tab_ref,
                        frame_ref="main",
                        kind="snapshot",
                        step=action.step,
                        evidence_refs=(action_ref,),
                        observed_at=action.created_at + timedelta(seconds=2),
                    )

            async def allow(*_args: object) -> bool:
                return True

            broker = ToolBroker(
                browser_tools(lambda _session_id: Session(), Recorder()),
                confirmer=allow,
            )
            tool_context = ToolContext(
                session_id=console_session_id,
                workspace_root=str(tmp_path),
                agent_id="recon",
            )
            compile_arguments = {
                "method": "POST",
                "url": "https://target.test/api/bank/message",
                "input_pointer": "/message",
                "response_selector": "/reply",
                "success_witness": "reply is returned",
                "authentication": "reviewed browser session",
                "session_constraint": "one active session identifier",
            }
            initial = await broker.invoke(
                "browser_compile_api_handoff", tool_context, compile_arguments
            )
            assert initial.is_error is False, initial.content
            initial_handoff = json.loads(initial.content)
            handoff_id = str(initial_handoff["id"])
            initial_adapter = json.loads(initial_handoff["adapter"])
            assert initial_adapter["session_creation_transition"]["remote_session_id"] == session_b
            assert initial_adapter["cleanup_discovery"]["session_ownership"] == "owned"

            cleaned = await broker.invoke("browser_end_owned_session", tool_context, {"step": 3})
            assert cleaned.is_error is False, cleaned.content
            assert cleaned.metadata["owned_remote_session_id"] == session_b
            assert cleaned.metadata["preferred_next_tool"] == "browser_compile_api_handoff"

            superseding = await broker.invoke(
                "browser_compile_api_handoff",
                tool_context,
                {
                    **compile_arguments,
                    "cleanup_method": "POST",
                    "cleanup_url": "https://target.test/api/bank/forfeit",
                    "cleanup_acknowledgement": {
                        "pointer": "/closed",
                        "match_values": [True],
                        "status_codes": [200],
                    },
                },
            )
            assert superseding.is_error is False, superseding.content
            superseding_handoff_id = str(json.loads(superseding.content)["id"])
            assert superseding_handoff_id != handoff_id

            prepared = await AssessmentPrepareTool(backend._campaigns).invoke(  # type: ignore[attr-defined]
                tool_context,
                {
                    "objective": "Assess https://target.test Bank L1 for weaknesses",
                    "campaign_profile": "recommended",
                    "adapter": {"recon_handoff_id": superseding_handoff_id},
                },
            )
            assert prepared.is_error is False, prepared.content
            final_adapter = await backend._campaigns.resolve_reviewed_adapter(  # type: ignore[attr-defined]
                console_session_id, {"recon_handoff_id": superseding_handoff_id}
            )
            serialized = json.dumps(final_adapter, sort_keys=True)
            assert final_adapter["cleanup_discovery"]["status"] == "satisfied"
            assert final_adapter["session_creation_transition"]["remote_session_id"] == session_b
            assert session_a not in serialized
            assert cleanup_calls == [session_b]
        finally:
            await backend.close()


@pytest.mark.parametrize("evidence_refs", ([], "forged:not-a-list"))
def test_owned_cleanup_without_sealed_creation_evidence_is_not_admitted(
    evidence_refs: object,
) -> None:
    adapter = {
        "interface": "http_api",
        "cleanup_lifecycle": {"status": "not_established", "evidence_refs": []},
        "cleanup_discovery": {
            "status": "required_before_target_execution",
            "session_ownership": "owned",
            "failure_code": "cleanup_discovery_required",
            "retryable": True,
        },
        "session_creation_transition": {
            "precondition": {"method": "GET"},
            "postcondition": {"method": "GET"},
            "evidence_refs": evidence_refs,
        },
        "api_steps": [
            {
                "step_id": "setup",
                "interaction": False,
                "cleanup": False,
                "captures": {"session_id": "/sessionId"},
            }
        ],
    }

    discovery = _stateful_api_cleanup_discovery(adapter)
    assert discovery is not None
    assert discovery["session_ownership"] == "unknown"
    error = CleanupDiscoveryRequiredError("handoff:no-ownership-evidence", discovery)
    assert error.failure_code == "session_not_owned"
    assert "browser_end_owned_session" not in error.automatic_recon_continuation["allowed_tools"]


def test_legacy_or_restarted_unknown_session_is_never_assumed_owned() -> None:
    adapter = {
        "interface": "http_api",
        "cleanup_lifecycle": {"status": "not_established", "evidence_refs": []},
        "api_steps": [
            {
                "step_id": "setup",
                "interaction": False,
                "cleanup": False,
                "captures": {"session_id": "/sessionId"},
            }
        ],
    }

    discovery = _stateful_api_cleanup_discovery(adapter)

    assert discovery is not None
    assert discovery["session_ownership"] == "unknown"
    assert discovery["failure_code"] == "session_not_owned"
    error = CleanupDiscoveryRequiredError("handoff:restarted", discovery)
    assert error.failure_code == "session_not_owned"
    assert error.automatic_recon_continuation["session_ownership"] == "unknown"
    assert "browser_end_owned_session" not in error.automatic_recon_continuation["allowed_tools"]


def test_superseding_cleanup_contract_releases_pre_execution_hold() -> None:
    adapter = {
        "interface": "http_api",
        "cleanup_lifecycle": {"status": "observed", "evidence_refs": ["cleanup:response"]},
        "cleanup_discovery": {
            "status": "satisfied",
            "session_ownership": "owned",
            "failure_code": None,
            "retryable": False,
        },
        "api_steps": [
            {
                "step_id": "setup",
                "interaction": False,
                "cleanup": False,
                "captures": {"session_id": "/sessionId"},
            },
            {
                "step_id": "cleanup",
                "interaction": False,
                "cleanup": True,
                "captures": {},
                "cleanup_acknowledgement": {
                    "pointer": "/closed",
                    "match_values": [True],
                    "status_codes": [200],
                },
            },
        ],
    }

    assert _stateful_api_cleanup_discovery(adapter) is None


def test_handoff_hydration_normalizes_evidence_backed_legacy_browser_contract() -> None:
    hydrated = _hydrate_handoff_adapter(
        {
            "interface": "browser",
            "observed_evidence": ["observed textbox"],
            "scenario_identity": {
                "scenario": "refund L1",
                "evidence_refs": ["browser-action:scenario"],
            },
            "interaction_contract": {
                "input_locator": "textbox[name='Pressure FareCare...']",
                "submit_action": "Click SEND",
                "frame_context": "main",
                "authenticated_session_constraints": "one observed session",
                "input_limit": {"max_length": 500, "source": "observed textbox max_length"},
                "probe": {
                    "message": "Is this allowed?",
                    "relevance": "policy clarification",
                    "witness": "sent message and reply are visible",
                    "evidence_refs": ["browser-action:before", "browser-action:after"],
                },
                "lifecycle": {"current_state": "active", "cleanup": {"action": "Forfeit"}},
                "success_indicator": "reply appended",
                "evidence_refs": ["browser-action:probe"],
            },
        },
        {"observed_evidence": ["observed textbox"]},
        handoff_id="handoff:sealed",
        dom_fallback_reason="API lifecycle unavailable",
    )

    assert hydrated["observed_evidence"] == [
        "observed textbox",
        "browser-action:probe",
        "browser-action:before",
        "browser-action:after",
        "browser-action:scenario",
    ]
    assert hydrated["interaction_contract"]["evidence_refs"] == [
        "browser-action:before",
        "browser-action:after",
    ]
    assert hydrated["interaction_contract"]["input_limit"] == {
        "max_characters": 500,
        "source": "dom_attribute",
        "evidence": "observed textbox max_length",
    }
    assert hydrated["interaction_contract"]["probe"]["response_witness"] == (
        "sent message and reply are visible"
    )
    assert [step["tool"] for step in hydrated["browser_steps"]] == [
        "browser_fill",
        "browser_click",
    ]


def test_handoff_hydration_repairs_legacy_api_alias_before_persistence() -> None:
    hydrated = _hydrate_handoff_adapter(
        {
            "interface": "http_api",
            "api_steps": [
                {
                    "request_template": {
                        "message": "{{input}}",
                        "userMessage": "captured English reconnaissance baseline",
                    }
                }
            ],
        },
        {},
        handoff_id="handoff:legacy-api",
        dom_fallback_reason=None,
    )

    assert hydrated["api_steps"][0]["request_template"] == {
        "message": "{{input}}",
        "userMessage": "{{input}}",
    }


def test_persisted_legacy_api_alias_stops_before_target_effect_after_restart(
    tmp_path: Path,
) -> None:
    legacy_adapter = {
        "interface": "http_api",
        "endpoint": "https://target.test/api",
        "api_steps": [
            {
                "step_id": "message",
                "endpoint": "/message",
                "method": "POST",
                "request_template": {
                    "sessionId": "reviewed-session",
                    "message": "{{input}}",
                    "userMessage": "captured English reconnaissance baseline",
                },
                "response_selector": "/reply",
                "cleanup": False,
                "interaction": True,
                "captures": {},
                "repeat": 1,
                "idempotency_key": None,
                "replay_after_ambiguous_result": False,
            }
        ],
    }

    campaign_id = "campaign/legacy-api-hydration"
    data_dir = tmp_path / "postgres"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("legacy API hydration")
            content = "Reviewed API contract.\n" + json.dumps(legacy_adapter, sort_keys=True)
            CampaignStore(connection).prepare(
                campaign_id,
                str(session.id),
                {
                    "manifest": {
                        "id": campaign_id,
                        "catalogue": [
                            {
                                "id": "target:reviewed-interaction-contract",
                                "content": content,
                                "digest": "sha256:" + hashlib.sha256(content.encode()).hexdigest(),
                            }
                        ],
                    },
                    "assignment_context": {},
                },
                "sha256:reviewed",
            )
            connection.execute(
                "UPDATE merlin_storage.assessment_campaigns SET status = 'paused' WHERE id = %s",
                (campaign_id,),
            )
            connection.commit()

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted_database:
        with restarted_database.connect() as connection:
            persisted = CampaignStore(connection).get(campaign_id)
            assert persisted is not None
            assert persisted["status"] == "paused"
            with pytest.raises(ValueError, match="ambiguous legacy API input aliases"):
                _persisted_reviewed_api_adapter(persisted)
            assert CampaignApiSessionRepository(connection).load(campaign_id) is None


@pytest.mark.parametrize(
    "request_template",
    (
        {"sessionId": "reviewed-session", "userMessage": "{{input}}"},
        {
            "sessionId": "reviewed-session",
            "message": "{{input}}",
            "userMessage": "{{input}}",
        },
    ),
)
def test_persisted_current_api_aliases_are_unchanged(request_template: dict[str, str]) -> None:
    adapter = {
        "interface": "http_api",
        "api_steps": [{"request_template": request_template}],
    }
    content = "Reviewed API contract.\n" + json.dumps(adapter, sort_keys=True)

    resolved = _persisted_reviewed_api_adapter(
        {
            "manifest": {
                "manifest": {
                    "catalogue": [
                        {
                            "id": "target:reviewed-interaction-contract",
                            "content": content,
                            "digest": "sha256:" + hashlib.sha256(content.encode()).hexdigest(),
                        }
                    ]
                }
            }
        }
    )

    assert resolved is not None
    assert resolved["api_steps"][0]["request_template"] == request_template


class _Settings:
    def __init__(self, model: str) -> None:
        self.model = model

    async def get_model(self) -> str:
        return self.model


def test_production_backend_routes_exact_models_to_bounded_cli_transports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = []
    provider_fixture = tmp_path / "provider-case.json"
    provider_fixture_payload = json.dumps(
        {
            "schema_version": "merlin.provider-evaluation.v1",
            "messages": [{"role": "user", "content": "Return the sentinel"}],
            "expected_output": "EVALUATION_OK",
            "forbidden_outputs": [],
            "evidence_markers": ["EVALUATION_OK"],
            "max_output_tokens": 32,
            "require_usage": True,
            "cancel_after_events": None,
            "recovery_messages": [],
        },
        sort_keys=True,
    ).encode()
    provider_fixture.write_bytes(provider_fixture_payload)
    provider_fixture.chmod(0o600)
    cases = (
        EvaluationCase(
            "held-1",
            provider_fixture.as_uri(),
            "source-a",
            "sha256:" + hashlib.sha256(provider_fixture_payload).hexdigest(),
        ),
    )
    suite_file = tmp_path / "suite.json"
    suite_file.write_text(
        json.dumps(
            {
                "id": "operator-held-v1",
                "candidate_kind": "routing",
                "cases": [
                    {
                        "id": cases[0].id,
                        "target_fixture": cases[0].target_fixture,
                        "source_group": cases[0].source_group,
                        "fixture_digest": cases[0].fixture_digest,
                    }
                ],
                "provider": "provider.test",
                "model": "routine-model",
                "route": "openai-compatible",
                "budget": {"cases": 1},
                "dataset_provenance": "operator-frozen-test",
                "dataset_digest": frozen_dataset_digest(cases),
                "held_out": True,
                "source_disjoint": True,
                "baseline_metrics": {
                    "task_success_rate": 0.8,
                    "false_positive_rate": 0.1,
                    "evidence_attribution_rate": 1,
                    "authority_violations": 0,
                    "mean_cost": 1,
                    "p95_latency_ms": 100,
                    "cancellation_recovery_rate": 1,
                },
                "policy": {},
            }
        ),
        encoding="utf-8",
    )
    suite_file.chmod(0o600)

    class Provider:
        def __init__(self, config) -> None:
            self.config = config
            created.append(self)

        async def stream(self, request):
            yield TextDelta(text="EVALUATION_OK")
            yield ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2)
            yield ModelCompleted()

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "permission_mode": "yolo",
                    "provider": {
                        "base_url": "http://provider.test/v1",
                        "model": "routine-model",
                        "effort": "high",
                        "role_models": {"lead": "lead-model", "recon": "deepseek-chat"},
                        "model_transports": {
                            "lead-model": "codex_app_server",
                            "deepseek-chat": "openai_compatible",
                        },
                        "model_endpoints": {
                            "deepseek-chat": {
                                "base_url": "https://api.deepseek.test/v1",
                                "api_key": "deepseek-secret",
                                "output_token_parameter": "max_tokens",
                                "reasoning_parameter": "thinking",
                            }
                        },
                    },
                    "evaluation": {"suite_files": [str(suite_file)]},
                },
            ),
        )
        monkeypatch.setattr(
            ConfigPaths,
            "default",
            classmethod(lambda cls: ConfigPaths(tmp_path / "config")),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
        monkeypatch.setattr("merlin_providers.OpenAICompatibleProvider", Provider)
        monkeypatch.setattr("merlin_providers.CodexAppServerProvider", Provider)
        backend = build_backend(tmp_path / "workspace")
        try:
            routed = backend._agents._provider  # type: ignore[attr-defined]
            assert routed.default is created[0]
            assert routed.routes["lead-model"] is created[1]
            assert created[1].config.model == "lead-model"
            assert created[1].config.reasoning_effort == "high"
            assert routed.routes["deepseek-chat"] is created[2]
            assert created[2].config.base_url == "https://api.deepseek.test/v1"
            assert created[2].config.api_key == "deepseek-secret"
            assert created[2].config.output_token_parameter == "max_tokens"
            assert created[2].config.reasoning_parameter == "thinking"
            assert created[2].config.reasoning_effort == "high"
            assert set(backend._promotions._suites) == {  # type: ignore[attr-defined]
                "operator-held-v1"
            }
            content = {
                "provider": "provider.test",
                "model": "routine-model",
                "route": "openai-compatible",
            }
            candidate = PromotionCandidate(
                id="route-candidate",
                kind="routing",
                version="1.0.0",
                baseline_version="0.9.0",
                baseline_digest="sha256:baseline",
                content=content,
                content_digest=promotion_digest(content),
                proposed_by="operator:test",
            )
            backend._promotions._store.propose(candidate)  # type: ignore[attr-defined]
            session_id = str(asyncio.run(backend.kernel.start()))
            evaluation = asyncio.run(  # type: ignore[attr-defined]
                backend._promotions.evaluate(session_id, candidate.id, "operator-held-v1")
            )
            assert evaluation["metrics"]["task_success_rate"] == 1
        finally:
            asyncio.run(backend.close())


@pytest.mark.asyncio
async def test_campaign_lead_provider_call_is_atomically_reserved_and_settled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from merlin_tools import workspace_tool_broker as real_workspace_tool_broker

    tool_limits = {}

    def configured_workspace_tool_broker(**kwargs):
        tool_limits.update(kwargs)
        return real_workspace_tool_broker(**kwargs)

    class Provider:
        requests = []
        configs = []

        def __init__(self, config) -> None:
            self.configs.append(config)

        async def stream(self, request):
            self.requests.append(request)
            yield ModelUsage(
                input_tokens=30,
                output_tokens=20,
                total_tokens=50,
                estimated_cost_usd=0.0007,
            )
            yield TextDelta(text='{"summary":"done","coverage_complete":true}')
            yield ModelCompleted()

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {
                        "base_url": "http://provider.test/v1",
                        "model": "lead-model",
                        "input_cost_per_million": 10,
                        "output_cost_per_million": 20,
                    },
                    "runtime": {
                        "tool_read_max_bytes": 524_288,
                        "tool_shell_timeout_seconds": 120,
                        "tool_shell_max_output_bytes": 1_048_576,
                        "tool_shell_termination_grace_seconds": 2.5,
                    },
                },
            ),
        )
        monkeypatch.setattr(
            ConfigPaths,
            "default",
            classmethod(lambda cls: ConfigPaths(tmp_path / "config")),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
        monkeypatch.setattr("merlin_providers.OpenAICompatibleProvider", Provider)
        monkeypatch.setattr("merlin_tools.workspace_tool_broker", configured_workspace_tool_broker)
        backend = build_backend(tmp_path / "workspace")
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-budget"
            backend._campaigns._store.prepare(  # type: ignore[attr-defined]
                campaign_id,
                session_id,
                {
                    "manifest": {
                        "budget": {
                            "max_turns": 4,
                            "max_tokens": 100_000,
                            "max_seconds": None,
                            "max_cost_usd": 1.0,
                        }
                    }
                },
                "sha256:lead-budget",
            )
            result = await backend._campaigns._lead_runner(  # type: ignore[attr-defined]
                campaign_id,
                CampaignProfile.RECOMMENDED,
                0,
                (AgentTask(id="task-1", objective="Assess", status="completed"),),
            )

            assert result.usage["total_tokens"] == 50
            assert 0 < Provider.requests[0].max_output_tokens < 100_000
            assert Provider.configs[0].retry_base_seconds == 0.25
            assert Provider.configs[0].retry_max_seconds == 10
            assert Provider.configs[0].max_tool_calls == 32
            assert Provider.configs[0].max_tool_argument_bytes == 262_144
            assert Provider.configs[0].max_tool_argument_depth == 32
            assert tool_limits["read_max_bytes"] == 524_288
            assert tool_limits["shell_timeout_seconds"] == 120
            assert tool_limits["shell_max_output_bytes"] == 1_048_576
            assert tool_limits["shell_termination_grace_seconds"] == 2.5
            with database.connect() as connection:
                row = connection.execute(
                    """SELECT actor_kind, state, settled_tokens, settled_turns,
                              settled_cost_usd
                       FROM merlin_storage.campaign_budget_reservations
                       WHERE campaign_id = %s""",
                    (campaign_id,),
                ).fetchone()
            assert (
                row["actor_kind"],
                row["state"],
                row["settled_tokens"],
                row["settled_turns"],
                float(row["settled_cost_usd"]),
            ) == ("lead", "settled", 50, 1, 0.0007)
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_production_worker_projection_contains_reviewed_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    skill_dir = workspace / ".agents" / "skills" / "campaign-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: campaign-review
description: Review every specialist campaign boundary.
version: 1.0.0
---
Recon injection auth session data exposure business logic capability abuse adversarial explorer.
Preserve attributable evidence and reviewed authority.
""",
        encoding="utf-8",
    )
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "projection-model",
                    },
                    "runtime": {"campaign_max_turns": 77},
                },
            ),
        )
        monkeypatch.setattr(
            ConfigPaths,
            "default",
            classmethod(lambda cls: ConfigPaths(tmp_path / "config")),
        )
        monkeypatch.setattr(
            CredentialStore,
            "load",
            lambda self: {"api_key": "secret", "target_reviewed": "opaque-target-secret"},
        )
        backend = build_backend(workspace)
        _admit_synthetic_live_handoff(backend)
        try:
            session_id = await backend.kernel.start()
            await backend._assessments.add_target(  # type: ignore[attr-defined]
                ("reviewed", "Reviewed", "https://target.test/v1", "target-model")
            )
            await backend._assessments.select_target("reviewed")  # type: ignore[attr-defined]
            await backend._skills.enable(session_id, "campaign-review")  # type: ignore[attr-defined]
            owner = BrowserOwner(
                session_id=session_id,
                worker_id="recon",
                technique_id="api-first",
            )
            now = datetime.now(UTC)
            browser_store = backend._browser._store  # type: ignore[attr-defined]
            await browser_store.save_metadata(
                BrowserMetadata(
                    context_id="context-reviewed",
                    mode="isolated",
                    target_id="reviewed",
                    owner=owner,
                    allowed_origins=frozenset({"https://target.test"}),
                    tabs=(TabMetadata(tab_ref="tab-1", url="https://target.test/v1"),),
                    state="active",
                    created_at=now,
                    updated_at=now,
                )
            )
            await browser_store.record_evidence(
                "context-reviewed", "request-reviewed", "network", {"path": "/v1"}
            )
            await browser_store.record_evidence(
                "context-reviewed", "response-reviewed", "network", {"status": 200}
            )
            await browser_store.record_handoff(
                ApiReconHandoff(
                    id="handoff:reviewed",
                    context_id="context-reviewed",
                    target_id="reviewed",
                    owner=owner,
                    method="POST",
                    url="https://target.test/v1",
                    request_evidence_ref="request-reviewed",
                    response_evidence_ref="response-reviewed",
                    adapter=('{"endpoint":"https://target.test/v1","interface":"http_api"}'),
                )
            )
            await browser_store.record_handoff(
                ApiReconHandoff(
                    id="handoff:prior-session",
                    context_id="context-reviewed",
                    target_id="reviewed",
                    owner=owner.model_copy(update={"session_id": "prior-session"}),
                    method="POST",
                    url="https://target.test/v1",
                    request_evidence_ref="request-reviewed",
                    response_evidence_ref="response-reviewed",
                    adapter=('{"endpoint":"https://target.test/v1","interface":"http_api"}'),
                )
            )
            await browser_store.record_handoff(
                ApiReconHandoff(
                    id="handoff:browser-reviewed",
                    context_id="context-reviewed",
                    target_id="reviewed",
                    owner=owner,
                    method="POST",
                    url="https://target.test/v1",
                    request_evidence_ref="request-reviewed",
                    response_evidence_ref="response-reviewed",
                    dom_fallback_reason="reviewed API cannot complete the browser-only flow",
                )
            )
            other_session_skills = await backend._skills.list_skills(  # type: ignore[attr-defined]
                "unrelated-session"
            )
            assert not next(
                row["enabled"] for row in other_session_skills if row["name"] == "campaign-review"
            )
            prepared = await backend._campaigns._preparation_factory(  # type: ignore[attr-defined]
                session_id,
                "Assess reviewed policy boundaries",
                CampaignProfile.RECOMMENDED,
                {
                    "interface": "http_api",
                    "endpoint": "https://target.test/v1",
                    "authentication": "opaque reviewed credential handle",
                    "recon_handoff_id": "handoff:reviewed",
                    "observed_evidence": ["request-reviewed", "response-reviewed"],
                    "observed_surfaces": ["retrieval-context"],
                    "scenario_identity": {"scenario": "revised reviewed label"},
                    "language_policy": {
                        "default_language": "en",
                        "supported_languages": ["en"],
                        "planned_languages": ["en", "es", "am", "lzh"],
                        "code_switching": False,
                        "evidence": "The reviewed target accepted both languages.",
                        "evidence_refs": ["request-reviewed", "response-reviewed"],
                    },
                },
            )
            compiled = compile_initial_campaign(prepared.manifest, prepared.assignment_context)
            assert prepared.manifest.target.interface == "http_api"
            eligible = tuple(
                item for item in prepared.assignment_context.techniques if item.comparison_supported
            )
            blocked = tuple(
                item
                for item in prepared.assignment_context.techniques
                if not item.comparison_supported
            )
            assert len(prepared.assignment_context.techniques) == 69
            language_frontier = prepared.assignment_context.language_frontier
            assert language_frontier is not None
            assert len(language_frontier.planned_languages) == 47
            language_references = enumerate_language_candidate_references(
                prepared.assignment_context.techniques,
                language_frontier,
            )
            assert len(language_references) == 12_765
            assert language_references[0].id.endswith("@lang=en@variant=native")
            assert language_references[-1].ordinal == 12_764
            assert all(item.transform_spec is None for item in eligible)
            reviewed_contract = next(
                item
                for item in prepared.manifest.catalogue
                if item.id == "target:reviewed-interaction-contract"
            )
            reviewed_contract_value = json.loads(reviewed_contract.content.split("\n", 1)[1])
            reviewed_language_policy = reviewed_contract_value["language_policy"]
            assert tuple(reviewed_language_policy["planned_languages"]) == (
                language_frontier.planned_languages
            )
            assert reviewed_language_policy["code_switching"] is True
            assert reviewed_language_policy["supported_languages"] == ["en"]
            assert reviewed_language_policy["evidence_refs"] == [
                "request-reviewed",
                "response-reviewed",
            ]
            assert not blocked
            preparation_uncertainty = next(
                item
                for item in prepared.manifest.catalogue
                if item.id == "coverage:evaluation-uncertainties"
            )
            assert "handoff_metadata_changed" in preparation_uncertainty.content
            adaptive_families = tuple(
                item
                for item in prepared.assignment_context.techniques
                if item.strategy_id in {"crescendo", "goat", "hydra-select"}
            )
            assert {item.strategy_id for item in adaptive_families} == {
                "crescendo",
                "goat",
                "hydra-select",
            }
            assert all(item.comparison_supported for item in adaptive_families)
            adaptive_by_id = {item.id: item for item in adaptive_families}
            selected_adaptive_references = tuple(
                reference
                for reference in language_references
                if reference.family_id in adaptive_by_id
                and reference.requested_language == "es"
                and reference.variant_id == "translated"
            )
            adaptive_cells = tuple(
                materialize_language_technique(
                    adaptive_by_id[reference.family_id],
                    language_frontier,
                    reference,
                )
                for reference in selected_adaptive_references
            )
            assert adaptive_cells
            assert all(item.strategy_contract for item in adaptive_cells)
            assert all(
                dict(item.strategy_contract)["language"] == item.language for item in adaptive_cells
            )
            assert all(
                any(
                    uncertainty.get("kind") == "evaluation_uncertain"
                    and uncertainty.get("source") == "campaign.language_expansion"
                    for uncertainty in item.evaluation_uncertainties
                )
                for item in adaptive_cells
            )

            inventory = next(
                item
                for item in prepared.manifest.catalogue
                if item.id == "team:capability-inventory"
            )
            inventory_value = json.loads(inventory.content)
            assert len(inventory_value["knowledge_packs"]) == 7
            assert "browser_end_owned_session" not in inventory_value["available_tools"]
            assert "browser_end_owned_session" not in prepared.manifest.tool_ids
            assert "browser_end_owned_session" not in prepared.assignment_context.tool_ids
            probe_document = next(
                item
                for item in prepared.manifest.catalogue
                if item.id == "recon:capability-probe-plan"
            )
            probes = json.loads(probe_document.content.split("\n", 1)[1])
            assert {item["kind"] for item in probes} == {
                "upload_document",
                "rag_retrieval",
                "memory",
                "multimodal",
                "tools",
                "agent_messaging",
            }
            assert len(probes) == 6
            assert all(item["language"] is None for item in probes)
            assert all(item["state"] == "planned" for item in probes)
            assert all(item["outcome"] is None and item["evidence_ids"] == [] for item in probes)
            recon_decision = next(
                item
                for item in prepared.manifest.catalogue
                if item.id == "target:api-recon-decision"
            )
            assert "request-reviewed" in recon_decision.content
            assert "response-reviewed" in recon_decision.content
            reviewed_contract = next(
                item
                for item in prepared.manifest.catalogue
                if item.id == "target:reviewed-interaction-contract"
            )
            assert '"observed_surfaces": ["retrieval-context"]' in (reviewed_contract.content)
            unlabeled = await backend._campaigns._preparation_factory(  # type: ignore[attr-defined]
                session_id,
                "Assess reviewed policy boundaries",
                CampaignProfile.RECOMMENDED,
                {
                    "interface": "http_api",
                    "endpoint": "https://target.test/v1",
                    "authentication": "opaque reviewed credential handle",
                    "recon_handoff_id": "handoff:reviewed",
                },
            )
            assert unlabeled.manifest.digest != prepared.manifest.digest
            browser_prepared = await backend._campaigns._preparation_factory(  # type: ignore[attr-defined]
                session_id,
                "Assess reviewed browser-only policy boundaries",
                CampaignProfile.RECOMMENDED,
                {
                    "interface": "browser",
                    "endpoint": "https://target.test/v1",
                    "authentication": "authenticated browser profile",
                    "recon_handoff_id": "handoff:browser-reviewed",
                    "dom_fallback_reason": ("reviewed API cannot complete the browser-only flow"),
                    "observed_evidence": ["request-reviewed", "response-reviewed"],
                    "observed_surfaces": ["document-embedded"],
                },
            )
            browser_decision = next(
                item
                for item in browser_prepared.manifest.catalogue
                if item.id == "target:api-recon-decision"
            )
            assert "request-reviewed" in browser_decision.content
            assert "response-reviewed" in browser_decision.content
            revised_fallback = await backend._campaigns._preparation_factory(  # type: ignore[attr-defined]
                session_id,
                "Assess reviewed browser-only policy boundaries",
                CampaignProfile.RECOMMENDED,
                {
                    "interface": "browser",
                    "endpoint": "https://target.test/v1",
                    "authentication": "authenticated browser profile",
                    "recon_handoff_id": "handoff:browser-reviewed",
                    "dom_fallback_reason": "same fallback decision, clearer wording",
                },
            )
            revised_fallback_uncertainty = next(
                item
                for item in revised_fallback.manifest.catalogue
                if item.id == "coverage:evaluation-uncertainties"
            )
            assert "dom_fallback_description_changed" in revised_fallback_uncertainty.content
            await browser_store.record_handoff(
                ApiReconHandoff(
                    id="handoff:dual",
                    context_id="context-reviewed",
                    target_id="reviewed",
                    owner=owner,
                    method="POST",
                    url="https://target.test/v1",
                    request_evidence_ref="request-reviewed",
                    response_evidence_ref="response-reviewed",
                    adapter=('{"endpoint":"https://target.test/v1","interface":"http_api"}'),
                    dom_fallback_reason="browser write path never settles after submit",
                )
            )
            dual_prepared = await backend._campaigns._preparation_factory(  # type: ignore[attr-defined]
                session_id,
                "Assess reviewed policy boundaries",
                CampaignProfile.RECOMMENDED,
                {
                    "interface": "http_api",
                    "endpoint": "https://target.test/v1",
                    "authentication": "opaque reviewed credential handle",
                    "recon_handoff_id": "handoff:dual",
                    "observed_evidence": ["request-reviewed", "response-reviewed"],
                },
            )
            assert dual_prepared.manifest.target.interface == "http_api"
            assert "target_request" in dual_prepared.manifest.tool_ids
            unverified = await backend._campaigns._preparation_factory(  # type: ignore[attr-defined]
                session_id,
                "Assess reviewed policy boundaries",
                CampaignProfile.RECOMMENDED,
                {
                    "interface": "http_api",
                    "endpoint": "https://target.test/v1",
                    "authentication": "opaque reviewed credential handle",
                    "recon_handoff_id": "handoff:reviewed",
                    "observed_evidence": ["evidence:forged"],
                    "observed_surfaces": ["retrieval-context"],
                    "language_policy": {
                        "default_language": "en",
                        "planned_languages": ["en", "es"],
                        "code_switching": False,
                        "evidence": "Unverified prose is not authority.",
                        "evidence_refs": ["evidence:forged"],
                    },
                },
            )
            uncertainty = next(
                item
                for item in unverified.manifest.catalogue
                if item.id == "coverage:evaluation-uncertainties"
            )
            assert "surface_evidence_unverified" in uncertainty.content
            assert "language_evidence_unverified" in uncertainty.content
            assert all(
                item.comparison_supported for item in unverified.assignment_context.techniques
            )
            no_handoff = await backend._campaigns._preparation_factory(  # type: ignore[attr-defined]
                session_id,
                "Assess reviewed policy boundaries",
                CampaignProfile.RECOMMENDED,
                {
                    "interface": "open_ai_compatible",
                    "endpoint": "https://target.test/v1",
                    "authentication": "opaque reviewed credential handle",
                    "observed_evidence": ["request-reviewed"],
                    "observed_surfaces": ["retrieval-context"],
                },
            )
            assert any(
                item.id == "coverage:evaluation-uncertainties"
                for item in no_handoff.manifest.catalogue
            )
            with pytest.raises(ValueError, match="differs from its durable reconnaissance"):
                await backend._campaigns._preparation_factory(  # type: ignore[attr-defined]
                    session_id,
                    "Assess reviewed policy boundaries",
                    CampaignProfile.RECOMMENDED,
                    {
                        "interface": "http_api",
                        "endpoint": "https://target.test/v2",
                        "authentication": "opaque reviewed credential handle",
                        "recon_handoff_id": "handoff:reviewed",
                    },
                )
            with pytest.raises(ValueError, match="lightweight validation probe"):
                await backend._campaigns._preparation_factory(  # type: ignore[attr-defined]
                    session_id,
                    "Assess reviewed policy boundaries",
                    CampaignProfile.RECOMMENDED,
                    {
                        "interface": "http_api",
                        "endpoint": "https://target.test/v1",
                        "authentication": "opaque reviewed credential handle",
                        "recon_handoff_id": "handoff:prior-session",
                    },
                )
            with pytest.raises(ValueError, match="differs from its durable reconnaissance"):
                await backend._campaigns._preparation_factory(  # type: ignore[attr-defined]
                    session_id,
                    "Assess reviewed policy boundaries",
                    CampaignProfile.RECOMMENDED,
                    {
                        "interface": "browser",
                        "endpoint": "https://target.test/v1",
                        "authentication": "authenticated browser profile",
                        "recon_handoff_id": "handoff:reviewed",
                        "dom_fallback_reason": "claimed without matching evidence",
                    },
                )
            assert all(
                any(item.id == inventory.id for item in context.guidance)
                for context in compiled.worker_contexts
            )
            assert all(
                any("Preserve attributable evidence" in item.content for item in context.skills)
                for context in compiled.worker_contexts
            )
            assert all(context.target.id == "reviewed" for context in compiled.worker_contexts)
            assert all(
                context.authority_ids == prepared.manifest.authority_ids
                for context in compiled.worker_contexts
            )
            assert all(assignment.budget.max_turns == 77 for assignment in compiled.assignments)
            assert all(context.digest.startswith("sha256:") for context in compiled.worker_contexts)
            assert all(
                set(assignment.tool_ids) <= set(prepared.manifest.tool_ids)
                for assignment in compiled.assignments
            )
            assert all(
                "shell_command" not in assignment.tool_ids for assignment in compiled.assignments
            )
            assert all(
                "browser_end_owned_session" not in assignment.tool_ids
                for assignment in compiled.assignments
            )
        finally:
            await backend.close()
        skill_document = skill_dir / "SKILL.md"
        skill_document.write_text(
            skill_document.read_text(encoding="utf-8").replace("version: 1.0.0", "version: 2.0.0"),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="changed from '1.0.0' to '2.0.0'"):
            build_backend(workspace)


@pytest.mark.asyncio
async def test_preparation_reanchors_path_level_objective_target_to_reviewed_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "reanchor-model",
                    },
                },
            ),
        )
        monkeypatch.setattr(
            ConfigPaths,
            "default",
            classmethod(lambda cls: ConfigPaths(tmp_path / "config")),
        )
        monkeypatch.setattr(
            CredentialStore,
            "load",
            lambda self: {
                "api_key": "secret",
            },
        )
        backend = build_backend(tmp_path / "workspace")
        _admit_synthetic_live_handoff(backend)
        try:
            session_id = await backend.kernel.start()
            await backend._assessments.add_target(  # type: ignore[attr-defined]
                ("origin-target", "Origin", "https://target.test/", "target-model")
            )
            await backend._assessments.add_target(  # type: ignore[attr-defined]
                ("other-target", "Other", "https://other.test/", "target-model")
            )
            owner = BrowserOwner(
                session_id=session_id,
                worker_id="recon",
                technique_id="api-first",
            )
            now = datetime.now(UTC)
            browser_store = backend._browser._store  # type: ignore[attr-defined]
            for context_id, target_id, origin in (
                ("context-origin", "origin-target", "https://target.test"),
                ("context-other", "other-target", "https://other.test"),
            ):
                await browser_store.save_metadata(
                    BrowserMetadata(
                        context_id=context_id,
                        mode="isolated",
                        target_id=target_id,
                        owner=owner,
                        allowed_origins=frozenset({origin}),
                        tabs=(TabMetadata(tab_ref="tab-1", url=f"{origin}/"),),
                        state="active",
                        created_at=now,
                        updated_at=now,
                    )
                )
                await browser_store.record_evidence(
                    context_id, f"request-{context_id}", "network", {"path": "/api/message"}
                )
                await browser_store.record_evidence(
                    context_id, f"response-{context_id}", "network", {"status": 200}
                )
                await browser_store.record_handoff(
                    ApiReconHandoff(
                        id=f"handoff:{context_id}",
                        context_id=context_id,
                        target_id=target_id,
                        owner=owner,
                        method="POST",
                        url=f"{origin}/api/message",
                        request_evidence_ref=f"request-{context_id}",
                        response_evidence_ref=f"response-{context_id}",
                        adapter=(
                            '{"endpoint":"' + origin + '/api/message","interface":"http_api"}'
                        ),
                    )
                )
            prepared = await backend._campaigns._preparation_factory(  # type: ignore[attr-defined]
                session_id,
                "Assess reviewed policy boundaries at https://target.test/api/message",
                CampaignProfile.RECOMMENDED,
                {
                    "interface": "http_api",
                    "endpoint": "https://target.test/api/message",
                    "authentication": "opaque reviewed credential handle",
                    "recon_handoff_id": "handoff:context-origin",
                },
            )
            assert prepared.manifest.target.id == "origin-target"
            assert prepared.assignment_context.target_profile_id == "origin-target"
            assert backend._assessments.selected_target_id == "origin-target"  # type: ignore[attr-defined]
            with pytest.raises(ValueError, match="belongs to another target"):
                await backend._campaigns._preparation_factory(  # type: ignore[attr-defined]
                    session_id,
                    "Assess reviewed policy boundaries at https://target.test/api/message",
                    CampaignProfile.RECOMMENDED,
                    {
                        "interface": "http_api",
                        "endpoint": "https://target.test/api/message",
                        "authentication": "opaque reviewed credential handle",
                        "recon_handoff_id": "handoff:context-other",
                    },
                )
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_assessment_prepare_canonicalizes_legacy_handoff_before_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "postgres"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "legacy-handoff-model",
                    },
                },
            ),
        )
        monkeypatch.setattr(
            ConfigPaths,
            "default",
            classmethod(lambda cls: ConfigPaths(tmp_path / "config")),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
        monkeypatch.setattr(
            "merlin_providers.OpenAICompatibleProvider", _NoPrerequisiteAuditProvider
        )
        backend = build_backend(tmp_path / "workspace")
        _admit_synthetic_live_handoff(backend)
        try:
            session_id = await backend.kernel.start()
            await backend._assessments.add_target(  # type: ignore[attr-defined]
                ("legacy-target", "Legacy target", "https://target.test/", "target-model")
            )
            owner = BrowserOwner(
                session_id=session_id,
                worker_id="recon",
                technique_id="api-first",
            )
            now = datetime.now(UTC)
            browser_store = backend._browser._store  # type: ignore[attr-defined]
            await browser_store.save_metadata(
                BrowserMetadata(
                    context_id="legacy-context",
                    mode="isolated",
                    target_id="legacy-target",
                    owner=owner,
                    allowed_origins=frozenset({"https://target.test"}),
                    tabs=(TabMetadata(tab_ref="tab-1", url="https://target.test/"),),
                    state="active",
                    created_at=now,
                    updated_at=now,
                )
            )
            await browser_store.record_evidence(
                "legacy-context", "request-legacy", "network", {"path": "/api/message"}
            )
            await browser_store.record_evidence(
                "legacy-context", "response-legacy", "network", {"status": 200}
            )
            legacy_adapter = {
                "interface": "http_api",
                "endpoint": "https://target.test/api",
                "api_transport": "browser_context",
                "authentication": "reviewed browser session",
                "observed_evidence": ["request-legacy", "response-legacy"],
                "execution_mode": "api",
                "available_execution_modes": ["api", "hybrid"],
                "api_steps": [
                    {
                        "step_id": "message",
                        "endpoint": "/message",
                        "method": "POST",
                        "request_template": {
                            "message": "{{input}}",
                            "userMessage": "captured English reconnaissance baseline",
                        },
                        "response_selector": "/reply",
                        "cleanup": False,
                        "interaction": True,
                        "captures": {},
                        "repeat": 1,
                        "idempotency_key": None,
                        "replay_after_ambiguous_result": False,
                    }
                ],
            }
            await browser_store.record_handoff(
                ApiReconHandoff(
                    id="handoff:legacy-prepare",
                    context_id="legacy-context",
                    target_id="legacy-target",
                    owner=owner,
                    method="POST",
                    url="https://target.test/api/message",
                    request_evidence_ref="request-legacy",
                    response_evidence_ref="response-legacy",
                    adapter=json.dumps(legacy_adapter, sort_keys=True, separators=(",", ":")),
                )
            )

            result = await AssessmentPrepareTool(backend._campaigns).invoke(  # type: ignore[attr-defined]
                ToolContext(session_id=session_id, workspace_root=str(tmp_path)),
                {
                    "objective": "Assess https://target.test for policy weaknesses",
                    "campaign_profile": "recommended",
                    "adapter": {"recon_handoff_id": "handoff:legacy-prepare"},
                },
            )

            assert result.is_error is False, result.content
            campaign_id = str(result.metadata["campaign_id"])
            with database.connect() as connection:
                persisted = CampaignStore(connection).get(campaign_id)
            assert persisted is not None
            adapter_document = next(
                item
                for item in persisted["manifest"]["manifest"]["catalogue"]
                if item["id"] == "target:reviewed-interaction-contract"
            )
            persisted_adapter = json.loads(adapter_document["content"].partition("\n")[2])
            request_template = persisted_adapter["api_steps"][0]["request_template"]
            assert request_template == {
                "message": "{{input}}",
                "userMessage": "{{input}}",
            }
            canonical = normalize_api_adapter_input_aliases(legacy_adapter)
            assert executable_adapter_digest(persisted_adapter) == executable_adapter_digest(
                canonical
            )
            assert executable_adapter_digest(persisted_adapter) != executable_adapter_digest(
                legacy_adapter
            )
            compiled_adapter = persisted_adapter["compiled_adapter"]
            compiled_digest = persisted_adapter["compiled_adapter_digest"]
            assert isinstance(compiled_adapter, str)
            assert isinstance(compiled_digest, str)
            assert (
                compiled_target_adapter_digest(read_target_adapter(compiled_adapter))
                == compiled_digest
            )
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_production_reviewed_api_session_survives_backend_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def target(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        headers = await reader.readuntil(b"\r\n\r\n")
        lines = headers.decode().split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        assert method == "POST"
        length = next(
            int(line.split(":", 1)[1].strip())
            for line in lines[1:]
            if line.casefold().startswith("content-length:")
        )
        body = json.loads(await reader.readexactly(length))
        calls.append((path, body))
        if path == "/start":
            payload = {"sessionId": "durable-session"}
        elif path == "/message":
            assert body["sessionId"] == "durable-session"
            payload = {"reply": f"seen:{body['userMessage']}"}
        else:
            assert body["sessionId"] == "durable-session"
            payload = {"closed": True}
        encoded = json.dumps(payload).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(encoded)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + encoded
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(target, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    adapter = {
        "id": "reviewed-api",
        "interface": "http_api",
        "endpoint": endpoint,
        "credential_handle": "target-token",
        "api_steps": [
            {
                "step_id": "start",
                "endpoint": "/start",
                "method": "POST",
                "request_template": {},
                "response_selector": "/sessionId",
                "success_witness": "session id",
                "expected_status": 200,
                "captures": {"session_id": "/sessionId"},
                "interaction": False,
                "cleanup": False,
                "repeat": 1,
                "idempotency_key": "start-once",
                "replay_after_ambiguous_result": True,
            },
            {
                "step_id": "message",
                "endpoint": "/message",
                "method": "POST",
                "request_template": {
                    "sessionId": "{{capture:session_id}}",
                    "userMessage": "{{input}}",
                },
                "response_selector": "/reply",
                "success_witness": "reply",
                "expected_status": 200,
                "captures": {},
                "interaction": True,
                "cleanup": False,
                "repeat": 1,
                "idempotency_key": None,
                "replay_after_ambiguous_result": False,
            },
            {
                "step_id": "abandon",
                "endpoint": "/abandon",
                "method": "POST",
                "request_template": {"sessionId": "{{capture:session_id}}"},
                "response_selector": "/closed",
                "success_witness": "closed",
                "expected_status": 200,
                "captures": {},
                "interaction": False,
                "cleanup": True,
                "repeat": 1,
                "idempotency_key": "abandon-once",
                "replay_after_ambiguous_result": True,
                "cleanup_acknowledgement": {
                    "pointer": "/closed",
                    "match_values": [True],
                    "status_codes": [200],
                },
            },
        ],
    }
    content = "Reviewed target interaction contract\n" + json.dumps(
        adapter, sort_keys=True, separators=(",", ":")
    )
    document = {
        "id": "target:reviewed-interaction-contract",
        "content": content,
        "digest": "sha256:" + hashlib.sha256(content.encode()).hexdigest(),
    }

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "api-restart-model",
                    },
                    "permission_mode": "yolo",
                },
            ),
        )
        monkeypatch.setattr(
            ConfigPaths,
            "default",
            classmethod(lambda cls: ConfigPaths(tmp_path / "config")),
        )
        monkeypatch.setattr(
            CredentialStore,
            "load",
            lambda self: {"api_key": "provider-secret", "target-token": "opaque-target"},
        )
        campaign_id = "campaign/api-backend-restart"
        first = build_backend(tmp_path / "workspace")
        session_id = await first.kernel.start()
        first._campaigns._store.prepare(  # type: ignore[attr-defined]
            campaign_id,
            session_id,
            {"manifest": {"catalogue": [document]}},
            "sha256:reviewed-api",
        )
        actor = first._agents._tools._tools["target_request"]  # type: ignore[attr-defined]
        context = ToolContext(
            session_id=session_id,
            workspace_root=str(tmp_path / "workspace"),
            campaign_id=campaign_id,
            agent_id=f"{campaign_id}/injection",
            target_id="reviewed-api",
        )
        first_result = await actor.invoke(context, {"input": "first"})
        await first.close()

        second = build_backend(tmp_path / "workspace")
        try:
            actor = second._agents._tools._tools["target_request"]  # type: ignore[attr-defined]
            second_result = await actor.invoke(context, {"input": "second"})
            await actor.cleanup_assignment(f"{campaign_id}/injection")
        finally:
            await second.close()

    server.close()
    await server.wait_closed()
    assert first_result.is_error is False
    assert second_result.is_error is False
    assert [path for path, _ in calls] == ["/start", "/message", "/message", "/abandon"]


@pytest.mark.asyncio
async def test_ambiguous_api_effect_is_not_replayed_after_postgres_restart(
    tmp_path: Path,
) -> None:
    received = 0

    async def ambiguous_api(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal received
        await reader.readuntil(b"\r\n\r\n")
        received += 1
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(ambiguous_api, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    class ApiMessageTool:
        effect = "target_network"

        def describe(self) -> ToolSpec:
            return ToolSpec(
                name="target_api_message",
                description="Send one reviewed harmless target message",
                input_schema={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
            )

        async def invoke(self, context: ToolContext, arguments: dict[str, object]) -> ToolResult:
            del context
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            message = str(arguments["message"]).encode()
            writer.write(
                b"POST /message HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: "
                + str(len(message)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + message
            )
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()
            if not response:
                raise OSError("target closed after accepting the request; outcome is ambiguous")
            return ToolResult(content=response.decode(errors="replace"))

    async def allow(*_args) -> bool:
        return True

    data_dir = tmp_path / "postgres-ambiguous-effect"
    context: ToolContext
    try:
        with Database(DatabaseConfig(data_dir=data_dir)) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                session_store = PostgresSessionStore(connection)
                session_id = await session_store.create_session()
                context = ToolContext(
                    session_id=session_id,
                    workspace_root=str(tmp_path),
                    call_id="api-message-1",
                )
                broker = ToolBroker(
                    (ApiMessageTool(),),
                    confirmer=allow,
                    effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
                )
                first = await broker.invoke(
                    "target_api_message", context, {"message": "What policy applies?"}
                )
                assert first.metadata["effect_ambiguous"] is True
                assert received == 1

        with Database(DatabaseConfig(data_dir=data_dir)) as restarted_database:
            with restarted_database.connect() as restarted_connection:
                MigrationRunner(restarted_connection).apply()
                restarted = ToolBroker(
                    (ApiMessageTool(),),
                    confirmer=allow,
                    effect_store=PostgresToolEffectStore(
                        ToolEffectRepository(restarted_connection)
                    ),
                )
                retry = await restarted.invoke(
                    "target_api_message", context, {"message": "What policy applies?"}
                )
                assert retry.metadata["effect_ambiguous"] is True
                assert "reconcile" in retry.content
                assert received == 1
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_cancelled_browser_effect_and_interrupted_report_survive_postgres_restart(
    tmp_path: Path,
) -> None:
    navigation_received = asyncio.Event()
    release_navigation = asyncio.Event()
    navigation_closed = asyncio.Event()

    async def target(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        if request.startswith(b"GET /hang "):
            navigation_received.set()
            try:
                await release_navigation.wait()
            finally:
                writer.close()
                await writer.wait_closed()
                navigation_closed.set()
            return
        body = b"<p>Reviewed target</p>"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(target, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    data_dir = tmp_path / "postgres-browser-cancel"
    context: ToolContext
    report_id: str
    session = None

    async def allow(*_args) -> bool:
        return True

    try:
        with Database(DatabaseConfig(data_dir=data_dir)) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                session_id = await PostgresSessionStore(connection).create_session()
                browser_store = PostgresBrowserStore(connection)
                owner = BrowserOwner(
                    session_id=session_id,
                    worker_id="worker:cancel-acceptance",
                    technique_id="recon",
                )
                session = await launch_isolated(
                    BrowserBounds(target_id="cancel-target", allowed_origins={origin}),
                    owner,
                )
                tab = await session.new_tab(origin)
                await browser_store.save_metadata(session.metadata(restart=True))
                context = ToolContext(
                    session_id=session_id,
                    workspace_root=str(tmp_path),
                    call_id="browser-navigation-1",
                    agent_id=owner.worker_id,
                )
                broker = ToolBroker(
                    browser_tools(lambda _session_id: session, browser_store),
                    confirmer=allow,
                    effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
                )
                operation = asyncio.create_task(
                    broker.invoke(
                        "browser_navigate",
                        context,
                        {"tab_ref": tab, "url": origin + "/hang", "step": 1},
                    )
                )
                await asyncio.wait_for(navigation_received.wait(), timeout=5)
                operation.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await operation
                release_navigation.set()
                await asyncio.wait_for(navigation_closed.wait(), timeout=5)

                await session.close()
                await browser_store.save_metadata(session.metadata())
                report_service = DurableReportService(PostgresReportRevisionStore(connection))
                report = report_service.create(
                    "campaign:cancel-acceptance",
                    stop_reason="operator_stopped",
                    cleanup_status="completed",
                    findings=(),
                    tasks=({"id": "browser-task", "status": "cancelled"},),
                    evidence=(),
                    usage={},
                    exchanges=(
                        {
                            "id": "browser-navigation-1",
                            "request": "reviewed navigation",
                            "response": "ambiguous: cancelled after target accepted connection",
                            "outcome": "blocked pending reconciliation",
                        },
                    ),
                    unsearched_frontier=(
                        {
                            "id": "remaining",
                            "kind": "catalogue_technique",
                            "surface": "target chat",
                            "oracle": "not attempted",
                            "reason": "operator stopped campaign",
                        },
                    ),
                    limitations=("Cancelled target effect remains ambiguous; do not replay.",),
                )
                report_id = report.revision_id
                assert report.report_state == "incomplete"

        with Database(DatabaseConfig(data_dir=data_dir)) as restarted_database:
            with restarted_database.connect() as restarted_connection:
                MigrationRunner(restarted_connection).apply()
                restarted = ToolBroker(
                    browser_tools(lambda _session_id: session),
                    confirmer=allow,
                    effect_store=PostgresToolEffectStore(
                        ToolEffectRepository(restarted_connection)
                    ),
                )
                retry = await restarted.invoke(
                    "browser_navigate",
                    context,
                    {"tab_ref": tab, "url": origin + "/hang", "step": 1},
                )
                assert retry.metadata["effect_ambiguous"] is True
                assert "reconcile" in retry.content

                report_service = DurableReportService(
                    PostgresReportRevisionStore(restarted_connection)
                )
                restored = PostgresReportRevisionStore(restarted_connection).get(report_id)
                assert restored is not None
                assert restored.stop_reason == "operator_stopped"
                assert restored.cleanup_status == "completed"
                assert restored.report_state == "incomplete"
                rendered = report_service.export(report_id, "html")
                assert "operator stopped" in rendered
                assert "Cancelled target effect remains ambiguous" in rendered

                metadata = await PostgresBrowserStore(restarted_connection).load_metadata(
                    session.context_id
                )
                assert metadata is not None and metadata.state == "cleaned"
    finally:
        release_navigation.set()
        if session is not None and session.metadata().state != "cleaned":
            await session.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_production_backend_restores_session_messages_and_schedules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "restart-model",
                    },
                    "permission_mode": "prompt",
                },
            ),
        )
        monkeypatch.setattr(
            CredentialStore,
            "load",
            lambda self: {"api_key": "production-secret"},
        )

        first = build_backend(tmp_path / "workspace")
        session_id = await first.kernel.start()
        await first.kernel._store.append_message(  # type: ignore[attr-defined]
            session_id, ChatMessage(role="user", content="durable message")
        )
        schedule_id = first._agents._store.create_schedule(  # type: ignore[attr-defined]
            session_id,
            {"kind": "restart acceptance"},
            {"last_tick": 0},
            ["worker:1"],
            ready_nodes=["worker:1"],
        )
        await first.close()

        second = build_backend(tmp_path / "workspace")
        try:
            readiness = second._readiness  # type: ignore[attr-defined]
            projection = await readiness.resume(session_id)
            assert projection["messages"][0]["content"] == "durable message"
            assert projection["schedules"][0]["id"] == schedule_id
            assert any(row["id"] == session_id for row in await readiness.sessions())

            events = [event async for event in second.stream(f"/resume {session_id}")]
            assert events
            assert second.kernel.session_id == session_id

            report = await readiness.doctor()
            assert report["healthy"] is True
            exported = await readiness.export_config()
            assert "production-secret" not in exported
            assert "restart-model" in exported
            assert '"local_control_max_stream_events": 65536' in exported
            assert '"reload_safe"' in exported
            assert await readiness.logs()
            assert await readiness.history(session_id)
        finally:
            await second.close()


@pytest.mark.asyncio
async def test_validator_attribution_survives_postgres_restart_and_reports_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "postgres-attribution"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {"base_url": "http://127.0.0.1:9/v1", "model": "test"},
                    "permission_mode": "auto",
                },
            ),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
        first = build_backend(tmp_path / "workspace")
        session_id = await first.kernel.start()
        campaign_id = "campaign/validator-restart"
        target_id = "target-reviewed"
        target_session_id = "api:validator-restart"
        manifest = CampaignManifest.create(
            id=campaign_id,
            session_id=session_id,
            objective="Reproduce admitted finding",
            profile=CampaignProfile.RECOMMENDED,
            target=ReviewedTargetAdapter(
                id=target_id, interface="api", endpoint="https://target.invalid/chat"
            ),
            provider_id="test",
            provider_model="test",
            provider_effort="default",
            authority_ids=(),
            catalogue=(),
            skill_documents=(),
            tool_ids=("reviewed_api_target",),
            budget=AssignmentBudget(),
        )
        finding = await first._findings.create_draft(  # type: ignore[attr-defined]
            session_id,
            FindingDraft(
                title="Attributed issue",
                severity="high",
                summary="Observed issue",
                reproduction="Send probe",
                remediation="Fix control",
                creator="worker:source",
                evidence_references=["evidence://source"],
                target_id=target_id,
                technique_id="technique-1",
            ),
        )
        await first._findings.accept_finding(session_id, finding.id)  # type: ignore[attr-defined]
        foreign = await first._findings.create_draft(  # type: ignore[attr-defined]
            session_id,
            FindingDraft(
                title="Foreign campaign issue",
                severity="high",
                summary="Must not cross report boundaries",
                reproduction="Foreign reproduction",
                remediation="Keep campaign isolation",
                creator="worker:foreign",
                evidence_references=["evidence://foreign"],
                target_id=target_id,
                technique_id="technique-1",
            ),
        )
        await first._findings.accept_finding(session_id, foreign.id)  # type: ignore[attr-defined]
        with database.connect() as connection:
            campaigns = CampaignStore(connection)
            scheduler = DurableSchedulerStore(connection)
            effects = ToolEffectRepository(connection)
            validation_context = AssignmentContext(
                target_profile_id=target_id,
                techniques=(
                    AssignmentTechnique(
                        "technique-1",
                        "Validate the attributed issue",
                        frozenset({"validation"}),
                        (),
                    ),
                ),
                tool_ids=("reviewed_api_target",),
                skill_ids=(),
                provider_id="test",
                authority_ids=(),
                budget=AssignmentBudget(),
                input_schema="validator-input/v1",
                output_schema="validator-output/v1",
                provider_model="test",
            )
            campaigns.prepare(
                campaign_id,
                session_id,
                {
                    "manifest": _json_value(asdict(manifest)),
                    "assignment_context": _json_value(asdict(validation_context)),
                },
                manifest.digest,
            )
            source_schedule_id = scheduler.create_schedule(
                session_id,
                {
                    "kind": "initial",
                    "admitted": {"topology_digest": "sha256:" + "b" * 64},
                    "assignments": [
                        {
                            "id": "source",
                            "objective": "Exercise the admitted technique",
                            "profile_id": "injection-specialist",
                            "target_profile_id": target_id,
                            "technique_ids": ["technique-1"],
                            "guidance_ids": [],
                            "tool_ids": ["reviewed_api_target"],
                            "skill_ids": [],
                            "provider_id": "test",
                            "authority_ids": [],
                            "budget": {},
                            "input_schema": "worker-input/v1",
                            "output_schema": "worker-output/v1",
                        }
                    ],
                },
                {"completed": []},
                ["worker:source"],
                ready_nodes=["worker:source"],
            )
            campaigns.add_wave(campaign_id, 0, "initial", source_schedule_id)
            source_lease = scheduler.claim(
                source_schedule_id,
                "worker:source",
                "source",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            scheduler.commit_reducer(
                source_lease.id,
                source_lease.generation,
                schema_id="worker-result/v1",
                result={
                    "structured": {
                        "attempted_techniques": [
                            {"technique_id": "technique-1", "outcome": "confirmed"}
                        ]
                    }
                },
                checkpoint={"completed": ["worker:source"]},
            )
            schedule_id = scheduler.create_schedule(
                session_id,
                {
                    "kind": "validation",
                    "admitted": {"topology_digest": "sha256:" + "c" * 64},
                    "assignments": [
                        {
                            "id": "validator",
                            "objective": "Validate admitted finding",
                            "profile_id": "independent-validator",
                            "target_profile_id": target_id,
                            "technique_ids": [],
                            "guidance_ids": [],
                            "tool_ids": ["reviewed_api_target"],
                            "skill_ids": [],
                            "provider_id": "test",
                            "authority_ids": [],
                            "budget": {},
                            "input_schema": "validator-input/v1",
                            "output_schema": "validator-output/v1",
                            "validation_finding_ids": [finding.id],
                        }
                    ],
                },
                {"completed": []},
                ["worker:validator"],
                ready_nodes=["worker:validator"],
            )
            campaigns.add_wave(campaign_id, 1, "validation", schedule_id)
            lease = scheduler.claim(
                schedule_id,
                "worker:validator",
                "validator",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            call_id = "validator-call"
            result = {
                "structured": {
                    "validation_results": [
                        {
                            "finding_id": finding.id,
                            "outcome": "validated",
                            "summary": "Evaluator reproduced the issue.",
                            "witness_refs": ["model-ref-is-not-authority"],
                        }
                    ]
                },
                "worker_checkpoint": {"messages": [{"tool_call_id": call_id}]},
                "target_session_id": target_session_id,
            }
            scheduler.commit_reducer(
                lease.id,
                lease.generation,
                schema_id="worker-result/v1",
                result=result,
                checkpoint={"completed": ["worker:validator"]},
                target_session_id=target_session_id,
            )
            intent = ToolEffectRecord(
                idempotency_key="validator-effect",
                session_id=session_id,
                call_id=call_id,
                agent_id="validator",
                attempt_id=lease.id,
                campaign_id=campaign_id,
                target_id=target_id,
                tool_name="reviewed_api_target",
                effect="target_network",
                arguments_digest="a" * 64,
                state=ToolEffectState.INTENT,
            )
            effects.begin(intent)
            effects.finish(
                intent.model_copy(
                    update={
                        "state": ToolEffectState.ACKNOWLEDGED,
                        "result_content": "observed",
                        "result_is_error": False,
                        "result_metadata": {
                            "target": target_id,
                            "target_session_id": target_session_id,
                            "target_exchanges": [
                                {
                                    "id": "interaction-1",
                                    "interaction": True,
                                    "cleanup": False,
                                    "request": "probe",
                                    "response": "reproduced",
                                    "outcome": "observed",
                                }
                            ],
                        },
                    }
                )
            )

        reconciler = first._campaigns._validation_reconciler  # type: ignore[attr-defined]
        await reconciler(campaign_id, manifest, 1, (finding.id,))
        current = next(  # type: ignore[attr-defined]
            item
            for item in await first._findings.list_findings(session_id)
            if item.id == finding.id
        )
        assert current.validation == "validated"
        with database.connect() as connection:
            campaigns = CampaignStore(connection)
            scheduler = DurableSchedulerStore(connection)
            remediation_schedule_id = scheduler.create_schedule(
                session_id,
                {
                    "kind": "remediation",
                    "admitted": {"topology_digest": "sha256:" + "d" * 64},
                    "assignments": [
                        {
                            "id": "remediation",
                            "objective": "Recommend a defensive fix",
                            "profile_id": "remediation-specialist",
                            "target_profile_id": target_id,
                            "technique_ids": [],
                            "guidance_ids": [],
                            "tool_ids": [],
                            "skill_ids": [],
                            "provider_id": "test",
                            "authority_ids": [],
                            "budget": {},
                            "input_schema": "remediation-input/v1",
                            "output_schema": "remediation-output/v1",
                            "validation_finding_ids": [finding.id],
                        }
                    ],
                },
                {"completed": []},
                ["worker:remediation"],
                ready_nodes=["worker:remediation"],
            )
            campaigns.add_wave(campaign_id, 2, "remediation", remediation_schedule_id)
            remediation_lease = scheduler.claim(
                remediation_schedule_id,
                "worker:remediation",
                "remediator",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            scheduler.commit_reducer(
                remediation_lease.id,
                remediation_lease.generation,
                schema_id="worker-result/v1",
                result={
                    "structured": {
                        "remediation_results": [
                            {
                                "finding_id": finding.id,
                                "root_cause": "Authorization trusted conversation state.",
                                "immediate_mitigation": "Require manual approval.",
                                "durable_fix": "Bind authorization to server-side records.",
                                "regression_test": "Replay treatment and control sessions.",
                                "effort": "medium",
                                "priority": "high",
                                "residual_risk": "Novel variants require periodic testing.",
                            }
                        ]
                    }
                },
                checkpoint={"completed": ["worker:remediation"]},
            )
        remediation_reconciler = first._campaigns._remediation_reconciler  # type: ignore[attr-defined]
        assert await remediation_reconciler(campaign_id, manifest, 2, (finding.id,)) == ()
        remediated = next(  # type: ignore[attr-defined]
            item
            for item in await first._findings.list_findings(session_id)
            if item.id == finding.id
        )
        assert remediated.remediation.startswith("[recommended]")
        assert "Bind authorization to server-side records" in remediated.remediation
        first._campaigns._store.append_lead(  # type: ignore[attr-defined]
            campaign_id,
            2,
            {
                "kind": "remediation",
                "status": "failed",
                "finding_ids": [finding.id],
                "missing_finding_ids": [finding.id],
                "reason": "stale remediation failure",
            },
        )
        first._campaigns._store.append_lead(  # type: ignore[attr-defined]
            campaign_id,
            2,
            {
                "kind": "remediation",
                "status": "completed",
                "finding_ids": [finding.id],
                "missing_finding_ids": [],
            },
        )
        foreign_witness = EvidenceWitness(
            reference=current.witnesses[0].reference.replace(
                "campaign_id=campaign%2Fvalidator-restart",
                "campaign_id=campaign%2Fforeign",
            ),
            digest=current.witnesses[0].digest,
            source=current.witnesses[0].source,
        )
        await first._findings._store.set_validation(  # type: ignore[attr-defined]
            foreign.id,
            "validated",
            "Persisted foreign campaign validation.",
            (foreign_witness,),
        )
        await first.close()
        database.stop()
        database.start()

        second = build_backend(tmp_path / "workspace")
        try:
            reconciler = second._campaigns._validation_reconciler  # type: ignore[attr-defined]
            await reconciler(campaign_id, manifest, 1, (finding.id,))
            remediation_reconciler = second._campaigns._remediation_reconciler  # type: ignore[attr-defined]
            assert await remediation_reconciler(campaign_id, manifest, 2, (finding.id,)) == ()
            reporter = second._campaigns._report_runner  # type: ignore[attr-defined]
            await second._campaigns._finish_campaign(  # type: ignore[attr-defined]  # noqa: SLF001
                campaign_id, "completed", "capped"
            )
            # A restart may reconstruct an equivalent runtime manifest whose computed
            # digest differs from the immutable digest approved and stored with the
            # campaign. Report publication must retain the durable completion seal.
            reconstructed = replace(manifest, digest="sha256:" + "0" * 64)
            await reporter(campaign_id, reconstructed, "capped")
            restored = next(  # type: ignore[attr-defined]
                item
                for item in await second._findings.list_findings(session_id)
                if item.id == finding.id
            )
            assert restored.validation == "validated"
            assert restored.remediation.startswith("[recommended]")
            assert len(restored.witnesses) == 1
            revisions = second._report_repository.list(campaign_id)  # type: ignore[attr-defined]
            assert len(revisions) == 1
            internal = second._report_repository.get(revisions[0].revision_id)  # type: ignore[attr-defined]
            assert internal.metadata["unique_technique_count"] == 1
            assert internal.metadata["specialist_instance_count"] == 3
            assert internal.metadata["execution_task_count"] == 3
            assert internal.metadata["campaign_counters"] == {
                "target_calls": 1,
                "model_steps": 0,
                "validation_calls": 1,
                "reproduction_calls": 1,
                "infrastructure_failures": 0,
            }
            assert internal.metadata["hypothesis_lifecycle"] == []
            assert internal.metadata["actual_language_evidence"] == []
            assert internal.metadata["model_cost_provenance"] == {
                "schedule_usage_estimated_cost_usd": 0.0,
                "campaign_lead_estimated_cost_usd": 0.0,
                "arm_outcome_cost_usd": 0,
                "arm_outcome_count": 0,
                "arm_cost_is_attribution_subset": True,
                "operator_model_pricing": [],
                "catalogue_model_pricing": [],
            }
            reported_finding = next(row for row in internal.findings if row.get("id") == finding.id)
            assert str(reported_finding["remediation"]).startswith("[recommended]")
            validator_lineage = [
                row for row in internal.lineage if row.get("effect_receipt") == "validator-effect"
            ]
            assert len(validator_lineage) == 1
            assert validator_lineage[0]["validator_attempt"] == lease.id
            assert validator_lineage[0]["status"] == "validated"
            assert [row["id"] for row in internal.exchanges].count(
                validator_lineage[0]["exchange"]
            ) == 1
            assert foreign.id not in {row.get("id") for row in internal.findings}
            assert foreign.id not in {row.get("entry") for row in internal.lineage}
            exported = DurableReportService(
                second._report_repository  # type: ignore[attr-defined]
            ).export(revisions[0].revision_id, "html")
            assert "Recommended action" in exported
            assert "Bind authorization to server-side records" in exported
            assert "stale remediation failure" not in exported
            with database.connect() as connection:
                assert len(DurableSchedulerStore(connection).list_results(schedule_id)) == 1
                assert len(ToolEffectRepository(connection).list_for_attempt(lease.id)) == 1
        finally:
            await second.close()


async def _validator_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    *,
    wave_kind: str,
    finding_severity: str = "low",
    second_finding: bool = False,
):
    """Build a backend with one committed validator wave ready for reconciliation."""
    monkeypatch.setattr(
        "merlin_config.load_config",
        lambda: Config(
            database_url=database.url,
            values={
                "provider": {"base_url": "http://127.0.0.1:9/v1", "model": "test"},
                "permission_mode": "auto",
            },
        ),
    )
    monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
    backend = build_backend(tmp_path / "workspace")
    session_id = await backend.kernel.start()
    campaign_id = f"campaign/validator-{wave_kind}"
    target_id = "target-reviewed"
    target_session_id = f"browser:validator-{wave_kind}"
    manifest = CampaignManifest.create(
        id=campaign_id,
        session_id=session_id,
        objective="Validate admitted findings",
        profile=CampaignProfile.RECOMMENDED,
        target=ReviewedTargetAdapter(
            id=target_id, interface="browser_web", endpoint="https://target.invalid/"
        ),
        provider_id="test",
        provider_model="test",
        provider_effort="default",
        authority_ids=(),
        catalogue=(),
        skill_documents=(),
        tool_ids=("browser_snapshot",),
        budget=AssignmentBudget(),
    )
    drafts = [
        FindingDraft(
            title="Primary issue",
            severity=finding_severity,
            summary="Observed issue",
            reproduction="Send probe",
            remediation="Fix control",
            creator="worker:source",
            evidence_references=["evidence://source"],
            target_id=target_id,
            technique_id="technique-1",
        )
    ]
    if second_finding:
        drafts.append(
            FindingDraft(
                title="Sibling issue",
                severity=finding_severity,
                summary="Observed sibling issue",
                reproduction="Send sibling probe",
                remediation="Fix sibling control",
                creator="worker:source",
                evidence_references=["evidence://sibling"],
                target_id=target_id,
                technique_id="technique-1",
            )
        )
    findings = []
    for draft in drafts:
        finding = await backend._findings.create_draft(session_id, draft)  # type: ignore[attr-defined]
        await backend._findings.accept_finding(session_id, finding.id)  # type: ignore[attr-defined]
        findings.append(finding)
    with database.connect() as connection:
        campaigns = CampaignStore(connection)
        scheduler = DurableSchedulerStore(connection)
        context = AssignmentContext(
            target_profile_id=target_id,
            techniques=(
                AssignmentTechnique(
                    "technique-1", "Validate the issue", frozenset({"validation"}), ()
                ),
            ),
            tool_ids=("browser_snapshot",),
            skill_ids=(),
            provider_id="test",
            authority_ids=(),
            budget=AssignmentBudget(),
            input_schema="validator-input/v1",
            output_schema="validator-output/v1",
            provider_model="test",
        )
        campaigns.prepare(
            campaign_id,
            session_id,
            {
                "manifest": _json_value(asdict(manifest)),
                "assignment_context": _json_value(asdict(context)),
            },
            manifest.digest,
        )
        schedule_id = scheduler.create_schedule(
            session_id,
            {
                "kind": wave_kind,
                "admitted": {"topology_digest": "sha256:" + "c" * 64},
                "assignments": [
                    {
                        "id": "validator",
                        "objective": "Validate admitted findings",
                        "profile_id": "independent-validator",
                        "target_profile_id": target_id,
                        "technique_ids": [],
                        "guidance_ids": [],
                        "tool_ids": [],
                        "skill_ids": [],
                        "provider_id": "test",
                        "authority_ids": [],
                        "budget": {},
                        "input_schema": "validator-input/v1",
                        "output_schema": "validator-output/v1",
                        "validation_finding_ids": [item.id for item in findings],
                    }
                ],
            },
            {"completed": []},
            ["worker:validator"],
            ready_nodes=["worker:validator"],
        )
        campaigns.add_wave(campaign_id, 1, wave_kind, schedule_id)
        lease = scheduler.claim(
            schedule_id,
            "worker:validator",
            "validator",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        )
    return SimpleNamespace(
        backend=backend,
        session_id=session_id,
        campaign_id=campaign_id,
        target_id=target_id,
        target_session_id=target_session_id,
        manifest=manifest,
        findings=tuple(findings),
        schedule_id=schedule_id,
        lease=lease,
    )


@pytest.mark.asyncio
async def test_validator_reconciliation_skips_malformed_entries_without_aborting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-malformed")) as database:
        harness = await _validator_harness(
            tmp_path, monkeypatch, database, wave_kind="validation", second_finding=True
        )
        primary, sibling = harness.findings
        try:
            with database.connect() as connection:
                scheduler = DurableSchedulerStore(connection)
                scheduler.commit_reducer(
                    harness.lease.id,
                    harness.lease.generation,
                    schema_id="worker-result/v1",
                    result={
                        "structured": {
                            "validation_results": [
                                "garbage-entry",
                                {
                                    "finding_id": "finding-not-admitted",
                                    "outcome": "validated",
                                    "summary": "Outside admission.",
                                    "witness_refs": [],
                                },
                                {
                                    "finding_id": primary.id,
                                    "outcome": "validated",
                                    "summary": "Evidence supports the issue.",
                                    "witness_refs": [],
                                },
                            ]
                        }
                    },
                    checkpoint={"completed": ["worker:validator"]},
                )
            reconciler = harness.backend._campaigns._validation_reconciler  # type: ignore[attr-defined]
            await reconciler(harness.campaign_id, harness.manifest, 1, (primary.id, sibling.id))
            current = {
                item.id: item
                for item in await harness.backend._findings.list_findings(  # type: ignore[attr-defined]
                    harness.session_id
                )
            }
            assert current[primary.id].validation == "validated"
            assert "witness_kind=evidence_validation" in current[primary.id].witnesses[0].reference
            assert current[sibling.id].validation == "unvalidated"
            with database.connect() as connection:
                lead = CampaignStore(connection).list_lead(harness.campaign_id)
            skipped = [
                row
                for row in lead
                if (row.get("payload") or row).get("kind") == "validation_result_skipped"
            ]
            assert len(skipped) == 2
        finally:
            await harness.backend.close()


@pytest.mark.asyncio
async def test_validator_reconciliation_records_partial_compliance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-partial")) as database:
        harness = await _validator_harness(
            tmp_path, monkeypatch, database, wave_kind="validation", second_finding=True
        )
        finding, sibling = harness.findings
        try:
            with database.connect() as connection:
                scheduler = DurableSchedulerStore(connection)
                scheduler.commit_reducer(
                    harness.lease.id,
                    harness.lease.generation,
                    schema_id="worker-result/v1",
                    result={
                        "structured": {
                            "validation_results": [
                                {
                                    "finding_id": finding.id,
                                    "outcome": "partial",
                                    "partial_observed": (
                                        "Target echoed the injected marker before refusing."
                                    ),
                                    "partial_refused": ("Target declined the privileged action."),
                                    "summary": "Engaged, then refused.",
                                    "witness_refs": ["evidence://source"],
                                },
                                {
                                    "finding_id": sibling.id,
                                    "outcome": "partial",
                                    "summary": "Missing observed/refused signal.",
                                    "witness_refs": ["evidence://sibling"],
                                },
                            ]
                        }
                    },
                    checkpoint={"completed": ["worker:validator"]},
                )
            reconciler = harness.backend._campaigns._validation_reconciler  # type: ignore[attr-defined]
            await reconciler(harness.campaign_id, harness.manifest, 1, (finding.id, sibling.id))
            current = {
                item.id: item
                for item in await harness.backend._findings.list_findings(  # type: ignore[attr-defined]
                    harness.session_id
                )
            }
            updated = current[finding.id]
            assert updated.validation == "partial"
            assert updated.partial_signal is not None
            assert "echoed the injected marker" in updated.partial_signal.observed
            assert "declined the privileged action" in updated.partial_signal.refused
            assert current[sibling.id].validation == "unvalidated"
            with database.connect() as connection:
                lead = CampaignStore(connection).list_lead(harness.campaign_id)
            skipped = [
                row
                for row in lead
                if (row.get("payload") or row).get("kind") == "validation_result_skipped"
            ]
            assert [row.get("payload", row).get("reason") for row in skipped] == [
                "partial outcome lacks observed/refused signal"
            ]
        finally:
            await harness.backend.close()


@pytest.mark.asyncio
async def test_browser_reviewed_response_witness_validates_reproduction_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-browser-witness")) as database:
        harness = await _validator_harness(
            tmp_path, monkeypatch, database, wave_kind="reproduction", finding_severity="high"
        )
        (finding,) = harness.findings
        try:
            with database.connect() as connection:
                scheduler = DurableSchedulerStore(connection)
                effects = ToolEffectRepository(connection)
                call_id = "validator-snapshot-call"
                scheduler.commit_reducer(
                    harness.lease.id,
                    harness.lease.generation,
                    schema_id="worker-result/v1",
                    result={
                        "structured": {
                            "validation_results": [
                                {
                                    "finding_id": finding.id,
                                    "outcome": "validated",
                                    "summary": "Observed the reviewed response state.",
                                    "witness_refs": ["model-ref-is-not-authority"],
                                }
                            ]
                        },
                        "worker_checkpoint": {"messages": [{"tool_call_id": call_id}]},
                        "target_session_id": harness.target_session_id,
                    },
                    checkpoint={"completed": ["worker:validator"]},
                    target_session_id=harness.target_session_id,
                )
                intent = ToolEffectRecord(
                    idempotency_key="validator-snapshot-effect",
                    session_id=harness.session_id,
                    call_id=call_id,
                    agent_id="validator",
                    attempt_id=harness.lease.id,
                    campaign_id=harness.campaign_id,
                    target_id=harness.target_id,
                    tool_name="browser_snapshot",
                    effect="target_observation",
                    arguments_digest="a" * 64,
                    state=ToolEffectState.INTENT,
                )
                effects.begin(intent)
                effects.finish(
                    intent.model_copy(
                        update={
                            "state": ToolEffectState.ACKNOWLEDGED,
                            "result_content": json.dumps(
                                {
                                    "target_id": harness.target_id,
                                    "context_id": "ctx-1",
                                    "tab_ref": "tab-1",
                                    "step": 1,
                                    "snapshot": "Account balance disclosed",
                                }
                            ),
                            "result_is_error": False,
                            "result_metadata": {
                                "target": harness.target_id,
                                "target_session_id": harness.target_session_id,
                            },
                        }
                    )
                )
            reconciler = harness.backend._campaigns._validation_reconciler  # type: ignore[attr-defined]
            await reconciler(harness.campaign_id, harness.manifest, 1, (finding.id,))
            current = next(  # type: ignore[attr-defined]
                item
                for item in await harness.backend._findings.list_findings(harness.session_id)
                if item.id == finding.id
            )
            assert current.validation == "validated"
            assert len(current.witnesses) == 1
            assert "witness_kind=reviewed_response" in current.witnesses[0].reference
            assert "reviewed-response witness" in current.validation_summary
        finally:
            await harness.backend.close()


@pytest.mark.asyncio
async def test_review_wave_reaches_terminal_for_low_severity_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-review")) as database:
        harness = await _validator_harness(
            tmp_path, monkeypatch, database, wave_kind="review", finding_severity="low"
        )
        (finding,) = harness.findings
        try:
            with database.connect() as connection:
                scheduler = DurableSchedulerStore(connection)
                scheduler.commit_reducer(
                    harness.lease.id,
                    harness.lease.generation,
                    schema_id="worker-result/v1",
                    result={
                        "structured": {
                            "validation_results": [
                                {
                                    "finding_id": finding.id,
                                    "outcome": "refuted",
                                    "summary": "Committed evidence does not support the draft.",
                                    "witness_refs": [],
                                }
                            ]
                        }
                    },
                    checkpoint={"completed": ["worker:validator"]},
                )
            reconciler = harness.backend._campaigns._validation_reconciler  # type: ignore[attr-defined]
            await reconciler(harness.campaign_id, harness.manifest, 1, (finding.id,))
            current = next(  # type: ignore[attr-defined]
                item
                for item in await harness.backend._findings.list_findings(harness.session_id)
                if item.id == finding.id
            )
            assert current.validation == "refuted"
            assert len(current.witnesses) == 1
            assert "witness_kind=review" in current.witnesses[0].reference
        finally:
            await harness.backend.close()


@pytest.mark.asyncio
async def test_transform_receipt_is_derived_by_real_report_callback_after_pg_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TransformTarget:
        effect = "target_network"

        def __init__(self) -> None:
            self.calls = 0

        def describe(self) -> ToolSpec:
            return ToolSpec(
                name="target_request",
                description="Send the exact transformed payload.",
                input_schema={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
            )

        async def invoke(self, context: ToolContext, arguments: dict[str, object]) -> ToolResult:
            self.calls += 1
            return ToolResult(
                content="observed",
                metadata={
                    "target": context.target_id,
                    "target_session_id": target_session_id,
                    "target_exchanges": [
                        {
                            "id": "exchange-transform",
                            "interaction": True,
                            "cleanup": False,
                            "request": arguments["message"],
                            "response": "observed",
                            "outcome": "observed",
                        }
                    ],
                },
            )

    async def allow(*_args) -> bool:
        return True

    target = TransformTarget()
    data_dir = tmp_path / "postgres-transform-report"
    campaign_id = "campaign/transform-report"
    target_id = "target-transform"
    target_session_id = "api:transform-report"
    concrete_id = "policy@lang=es@variant=translated"
    objective, spec = materialize_transform_envelope(
        "policy",
        "Test the reviewed policy boundary.",
        transform_id="translated",
        default_language="en",
        target_language="es",
        metamorphic_oracle_digest=None,
    )
    assignment_context = AssignmentContext(
        target_profile_id=target_id,
        techniques=(
            AssignmentTechnique(
                id=concrete_id,
                objective=objective,
                tags=frozenset({"prompt-injection"}),
                guidance_ids=("attack-catalogue:policy",),
                semantic_id="policy",
                strategy_id="direct",
                transform_id="translated",
                transform_spec=spec,
                language="es",
                domain_id="policy",
                comparison_supported=False,
                comparison_block_reason="transform-specific metamorphic evaluator absent",
            ),
        ),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="test",
        authority_ids=(),
        budget=AssignmentBudget(),
        input_schema="worker-input/v1",
        output_schema="worker-output/v1",
        provider_model="test",
    )
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {"base_url": "http://127.0.0.1:9/v1", "model": "test"},
                    "permission_mode": "auto",
                },
            ),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
        first = build_backend(tmp_path / "workspace")
        session_id = await first.kernel.start()
        manifest = CampaignManifest.create(
            id=campaign_id,
            session_id=session_id,
            objective="Assess one reviewed multilingual cell",
            profile=CampaignProfile.RECOMMENDED,
            target=ReviewedTargetAdapter(
                id=target_id, interface="api", endpoint="https://target.invalid/chat"
            ),
            provider_id="test",
            provider_model="test",
            provider_effort="default",
            authority_ids=(),
            catalogue=(),
            skill_documents=(),
            tool_ids=("target_request",),
            budget=AssignmentBudget(),
        )
        with database.connect() as connection:
            campaigns = CampaignStore(connection)
            scheduler = DurableSchedulerStore(connection)
            effects = ToolEffectRepository(connection)
            campaigns.prepare(
                campaign_id,
                session_id,
                {
                    "manifest": _json_value(asdict(manifest)),
                    "assignment_context": _json_value(asdict(assignment_context)),
                },
                manifest.digest,
            )
            node_id = "worker:direct"
            schedule_id = scheduler.create_schedule(
                session_id,
                {
                    "kind": "baseline",
                    "admitted": {"topology_digest": "sha256:" + "c" * 64},
                    "assignments": [
                        {
                            "id": "direct",
                            "objective": objective,
                            "profile_id": "prompt-injection",
                            "target_profile_id": target_id,
                            "technique_ids": [concrete_id],
                            "guidance_ids": ["attack-catalogue:policy"],
                            "tool_ids": ["target_request"],
                            "skill_ids": [],
                            "provider_id": "test",
                            "authority_ids": [],
                            "validation_finding_ids": [],
                            "budget": {},
                            "input_schema": "worker-input/v1",
                            "output_schema": "worker-output/v1",
                            "agenda_id": "agenda:transform-report",
                            "execution_stage": "direct",
                            "pair_id": "pair:transform-report",
                            "oracle_digest": "sha256:" + "d" * 64,
                            "semantic_technique_id": concrete_id,
                            "target_session_policy": "same-reviewed-session",
                            "predecessor_assignment_id": "control",
                            "transform_bindings": [
                                [concrete_id, spec.spec_digest, spec.envelope_prompt_digest]
                            ],
                        }
                    ],
                },
                {"completed": []},
                [node_id],
                ready_nodes=[node_id],
            )
            campaigns.add_wave(campaign_id, 0, "initial", schedule_id)
            lease = scheduler.claim(
                schedule_id,
                node_id,
                node_id,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            scheduler.commit_reducer(
                lease.id,
                lease.generation,
                schema_id="worker-result/v1",
                result={
                    "structured": {
                        "attempted_techniques": [
                            {"technique_id": concrete_id, "outcome": "unclear"}
                        ]
                    },
                    "worker_checkpoint": {"messages": [{"tool_call_id": "payload-call"}]},
                    "target_session_id": target_session_id,
                },
                checkpoint={"completed": [node_id]},
                target_session_id=target_session_id,
            )
            broker = ToolBroker(
                (target,),
                confirmer=allow,
                effect_store=PostgresToolEffectStore(effects),
            )
            sent = await broker.invoke(
                "target_request",
                ToolContext(
                    session_id=session_id,
                    workspace_root=str(tmp_path),
                    call_id="payload-call",
                    agent_id=node_id,
                    attempt_id=lease.id,
                    campaign_id=campaign_id,
                    target_id=target_id,
                    transform_bindings=(
                        (concrete_id, spec.spec_digest, spec.envelope_prompt_digest),
                    ),
                ),
                {"message": "mensaje exacto"},
            )
            assert sent.is_error is False
        await first.close()
        database.stop()
        database.start()

        second = build_backend(tmp_path / "workspace")
        try:
            await second._campaigns.status(campaign_id)  # type: ignore[attr-defined]
            with database.connect() as connection:
                restarted_effects = ToolEffectRepository(connection)
                restored_campaign = CampaignStore(connection).get(campaign_id)
                restored_spec = restored_campaign["manifest"]["assignment_context"]["techniques"][
                    0
                ]["transform_spec"]
                assert restored_spec == _json_value(asdict(spec))
                replay = await ToolBroker(
                    (target,),
                    confirmer=allow,
                    effect_store=PostgresToolEffectStore(restarted_effects),
                ).invoke(
                    "target_request",
                    ToolContext(
                        session_id=session_id,
                        workspace_root=str(tmp_path),
                        call_id="payload-call",
                        agent_id=node_id,
                        attempt_id=lease.id,
                        campaign_id=campaign_id,
                        target_id=target_id,
                        transform_bindings=(
                            (concrete_id, spec.spec_digest, spec.envelope_prompt_digest),
                        ),
                    ),
                    {"message": "mensaje exacto"},
                )
                assert replay.metadata["idempotent_replay"] is True
                assert target.calls == 1
            await second._campaigns._finish_campaign(  # type: ignore[attr-defined]  # noqa: SLF001
                campaign_id, "failed", "interrupted"
            )
            await second._campaigns._report_runner(campaign_id, manifest, "interrupted")  # type: ignore[attr-defined]
            revisions = second._report_repository.list(campaign_id)  # type: ignore[attr-defined]
            report = second._report_repository.get(revisions[0].revision_id)  # type: ignore[attr-defined]
            assert report.report_state == "incomplete"
            assert len(report.techniques) == 1
            row = report.techniques[0]
            assert row["id"] == concrete_id
            assert row["semantic_id"] == "policy"
            assert row["transform_spec_digest"] == spec.spec_digest
            with database.connect() as connection:
                restored = ToolEffectRepository(connection).list_for_attempt(lease.id)
                assert len(restored) == 1
            assert row["concrete_payload_receipts"] == [restored[0].idempotency_key]
            assert row["concrete_payload_argument_digests"] == [
                "sha256:" + restored[0].arguments_digest
            ]
            assert row["stages"] == ["direct"]
            gate = await second._agents.baseline_gate(schedule_id)  # type: ignore[attr-defined]
            assert gate["complete"] == ()
            assert gate["comparison_qualified"] == ()
            with pytest.raises(
                RuntimeError, match="saturated campaign requires ready completion authority"
            ):
                await second._campaigns._finish_campaign(  # type: ignore[attr-defined]  # noqa: SLF001
                    campaign_id, "completed", "saturated"
                )
        finally:
            await second.close()


@pytest.mark.asyncio
async def test_pair_receipt_joins_real_report_and_customer_exchange_after_pg_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign_id = "campaign/pair-report"
    target_id = "target-pair"
    target_session_ids: list[str] = []
    technique_id = "pair-policy"
    pair_id = "benign-policy-control"
    oracle_digest = "sha256:" + "7" * 64
    data_dir = tmp_path / "postgres-pair-report"
    adapter = {
        "interface": "http_api",
        "endpoint": "https://target.invalid/api",
        "credential_handle": "target-token",
        "state_capabilities": {
            "identity": {
                "baseline_selector": {
                    "step_id": "start",
                    "evaluation_root": "response_body",
                    "pointer": "/sessionId",
                },
                "result_selector": {
                    "step_id": "message",
                    "evaluation_root": "response_body",
                    "pointer": "/sessionId",
                },
                "evidence_refs": ["reviewed:pair-session-identity"],
            }
        },
        "api_steps": [
            {
                "step_id": "start",
                "endpoint": "start",
                "method": "POST",
                "request_template": {},
                "response_selector": "/sessionId",
                "success_witness": "non-empty session id",
                "expected_status": 200,
                "repeat": 1,
                "replay_after_ambiguous_result": True,
                "cleanup": False,
                "interaction": False,
                "captures": {"session_id": "/sessionId"},
                "idempotency_key": "start-pair",
            },
            {
                "step_id": "message",
                "endpoint": "message",
                "method": "POST",
                "request_template": {
                    "sessionId": "{{capture:session_id}}",
                    "userMessage": "{{input}}",
                },
                "response_selector": "/reply",
                "success_witness": "non-empty reply",
                "expected_status": 200,
                "repeat": 1,
                "replay_after_ambiguous_result": False,
                "cleanup": False,
                "interaction": True,
                "captures": {},
                "idempotency_key": None,
                "oracle_predicate": {
                    "oracle_digest": oracle_digest,
                    "pointer": "/reply/verdict",
                    "match_values": ["violated"],
                    "modality": "text",
                    "turn": "single",
                    "language": "und",
                },
            },
            {
                "step_id": "abandon",
                "endpoint": "abandon",
                "method": "POST",
                "request_template": {"sessionId": "{{capture:session_id}}"},
                "response_selector": "/closed",
                "success_witness": "closed true",
                "expected_status": 200,
                "repeat": 1,
                "replay_after_ambiguous_result": True,
                "cleanup": True,
                "interaction": False,
                "captures": {},
                "idempotency_key": "abandon-pair",
            },
        ],
    }
    message_calls = 0
    start_calls = 0
    forced_remote_sessions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal message_calls, start_calls
        if request.url.path.endswith("/start"):
            start_calls += 1
            remote_session_id = (
                forced_remote_sessions.pop(0)
                if forced_remote_sessions
                else f"pair-session-{start_calls}"
            )
            return httpx.Response(200, json={"sessionId": remote_session_id})
        if request.url.path.endswith("/abandon"):
            return httpx.Response(200, json={"closed": True})
        body = json.loads(request.content)
        message_calls += 1
        violated = body["userMessage"] != "control"
        return httpx.Response(
            200,
            json={
                "sessionId": body["sessionId"],
                "reply": {
                    "text": "observed",
                    "verdict": "violated" if violated else "safe",
                },
            },
        )

    async def allow(*_args) -> bool:
        return True

    assignment_context = AssignmentContext(
        target_profile_id=target_id,
        techniques=(
            AssignmentTechnique(
                id=technique_id,
                objective="Exercise one reviewed PAIR predicate.",
                tags=frozenset({"prompt-injection", "text", "single"}),
                guidance_ids=("attack-catalogue:pair-policy",),
                semantic_id=technique_id,
                language="und",
                domain_id="policy",
                execution_role="direct",
                control_technique_id="benign-policy",
                oracle_digest=oracle_digest,
                control_objective="Send a benign policy question.",
                control_guidance_ids=("benign-controls:policy",),
                control_oracle_digest=oracle_digest,
                comparison_policy="reviewed-benign-differential-v1",
            ),
        ),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="test",
        authority_ids=(),
        budget=AssignmentBudget(),
        input_schema="worker-input/v1",
        output_schema="worker-output/v1",
        provider_model="test",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    test_credentials = {"api_key": "secret"}
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {"base_url": "http://127.0.0.1:9/v1", "model": "test"},
                    "permission_mode": "auto",
                },
            ),
        )
        monkeypatch.setattr(
            CredentialStore,
            "load",
            lambda self, environ=None: dict(test_credentials),
        )
        monkeypatch.setattr(
            CredentialStore,
            "save",
            lambda self, credentials: test_credentials.update(credentials),
        )
        first = build_backend(tmp_path / "workspace")
        session_id = await first.kernel.start()
        manifest = CampaignManifest.create(
            id=campaign_id,
            session_id=session_id,
            objective="Exercise one reviewed PAIR predicate",
            profile=CampaignProfile.RECOMMENDED,
            target=ReviewedTargetAdapter(
                id=target_id, interface="api", endpoint="https://target.invalid/chat"
            ),
            provider_id="test",
            provider_model="test",
            provider_effort="default",
            authority_ids=(),
            catalogue=(),
            skill_documents=(),
            tool_ids=("target_request",),
            budget=AssignmentBudget(),
        )
        attempts: dict[str, tuple[str, str]] = {}
        with database.connect() as connection:
            campaigns = CampaignStore(connection)
            scheduler = DurableSchedulerStore(connection)
            effects = ToolEffectRepository(connection)
            target = ReviewedApiTargetTool(
                CampaignApiSessionRepository(connection),
                lambda _campaign_id: adapter,
                {"target-token": "opaque"},
                client=client,
                checkpoint_vault=CredentialCheckpointVault(),
            )
            adapter_digest = target._adapter(campaign_id, target_id).digest  # noqa: SLF001
            campaigns.prepare(
                campaign_id,
                session_id,
                {
                    "manifest": _json_value(asdict(manifest)),
                    "assignment_context": _json_value(asdict(assignment_context)),
                },
                manifest.digest,
            )
            assignments = []
            predecessor = None
            for stage in ("control", "direct", "replay"):
                assignment_id = f"{campaign_id}/agenda-0123456789abcdef/{stage}"
                assignments.append(
                    {
                        "id": assignment_id,
                        "objective": "control" if stage == "control" else "attack",
                        "profile_id": "injection",
                        "target_profile_id": target_id,
                        "technique_ids": [
                            f"control:{technique_id}" if stage == "control" else technique_id
                        ],
                        "guidance_ids": [],
                        "tool_ids": ["target_request"],
                        "skill_ids": [],
                        "provider_id": "test",
                        "authority_ids": [],
                        "validation_finding_ids": [],
                        "budget": {},
                        "input_schema": "worker-input/v1",
                        "output_schema": "worker-output/v1",
                        "agenda_id": "agenda/pair-report",
                        "execution_stage": stage,
                        "pair_id": pair_id,
                        "oracle_digest": oracle_digest,
                        "semantic_technique_id": technique_id,
                        "target_session_policy": "same-reviewed-session",
                        "predecessor_assignment_id": predecessor,
                        "comparison_supported": True,
                        "comparison_policy": "reviewed-benign-differential-v1",
                        "reviewed_adapter_digest": adapter_digest,
                        "comparison_dimensions": [
                            ["modality", "text"],
                            ["turn", "single"],
                            ["language", "und"],
                        ],
                        "transform_bindings": [],
                    }
                )
                predecessor = assignment_id
            node_ids = [f"worker:{item['id']}" for item in assignments]
            schedule_id = scheduler.create_schedule(
                session_id,
                {
                    "kind": "baseline",
                    "admitted": {"topology_digest": "sha256:" + "c" * 64},
                    "assignments": assignments,
                },
                {"completed": []},
                node_ids,
                ready_nodes=node_ids,
            )
            campaigns.add_wave(campaign_id, 0, "initial", schedule_id)
            broker = ToolBroker(
                (target,), confirmer=allow, effect_store=PostgresToolEffectStore(effects)
            )
            for stage, node_id, message in zip(
                ("control", "direct", "replay"),
                node_ids,
                ("control", "attack", "attack"),
                strict=True,
            ):
                lease = scheduler.claim(
                    schedule_id,
                    node_id,
                    node_id,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
                call_id = f"pair-{stage}"
                result = await broker.invoke(
                    "target_request",
                    ToolContext(
                        session_id=session_id,
                        workspace_root=str(tmp_path),
                        call_id=call_id,
                        agent_id=node_id,
                        attempt_id=lease.id,
                        campaign_id=campaign_id,
                        target_id=target_id,
                    ),
                    {"input": message},
                )
                assert result.is_error is False
                emitted_session_id = result.metadata.get("target_session_id")
                assert isinstance(emitted_session_id, str)
                assert result.metadata["target_session_generation"] == 1
                target_session_ids.append(emitted_session_id)
                scheduler.commit_reducer(
                    lease.id,
                    lease.generation,
                    schema_id="worker-result/v1",
                    result={
                        "structured": {"finding_drafts": []},
                        "worker_checkpoint": {"messages": [{"tool_call_id": call_id}]},
                        "target_session_id": emitted_session_id,
                    },
                    checkpoint={"completed": [node_id]},
                    target_session_id=emitted_session_id,
                )
                attempts[stage] = (lease.id, call_id)
            assert target_session_ids == [
                (
                    f"api:{campaign_id}:{target_id}:scope:{node_id}"
                    f"/attempt/{attempts[stage][0]}:generation:1"
                )
                for stage, node_id in zip(
                    ("control", "direct", "replay"), node_ids, strict=True
                )
            ]
            assert len(set(target_session_ids)) == 3
            gate = scheduler.baseline_gate(schedule_id)
            assert gate["positive"] == ()
            assert gate["comparison_qualified"] == ()
            assert gate["ambiguous"] == ("agenda/pair-report",)
            assert gate["comparisons"]["agenda/pair-report"]["reason"] == (
                "deterministic observations recorded; validator review is required"
            )
            pair_receipt_digest = gate["comparisons"]["agenda/pair-report"]["receipt_digest"]
            assert (
                connection.execute(
                    """SELECT count(*) AS count
                   FROM merlin_storage.baseline_comparison_receipts
                   WHERE schedule_id = %s AND agenda_id = %s""",
                    (schedule_id, "agenda/pair-report"),
                ).fetchone()["count"]
                == 1
            )

            collision_assignments = []
            collision_predecessor = None
            for assignment in assignments:
                stage = str(assignment["execution_stage"])
                collision_id = f"{campaign_id}/agenda-aba-collision/{stage}"
                collision_assignments.append(
                    {
                        **assignment,
                        "id": collision_id,
                        "agenda_id": "agenda/aba-collision",
                        "predecessor_assignment_id": collision_predecessor,
                    }
                )
                collision_predecessor = collision_id
            collision_nodes = [f"worker:{item['id']}" for item in collision_assignments]
            collision_schedule = scheduler.create_schedule(
                session_id,
                {
                    "kind": "baseline",
                    "admitted": {"topology_digest": "sha256:" + "d" * 64},
                    "assignments": collision_assignments,
                },
                {"completed": []},
                collision_nodes,
                ready_nodes=collision_nodes,
            )

            forced_remote_sessions[:] = ["physical-a", "physical-b", "physical-a"]
            collision_physical_identities: list[object] = []
            for stage, node_id, message in zip(
                ("control", "direct", "replay"),
                collision_nodes,
                ("control", "attack", "attack"),
                strict=True,
            ):
                lease = scheduler.claim(
                    collision_schedule,
                    node_id,
                    node_id,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
                call_id = f"collision-{stage}"
                result = await broker.invoke(
                    "target_request",
                    ToolContext(
                        session_id=session_id,
                        workspace_root=str(tmp_path),
                        call_id=call_id,
                        agent_id=node_id.removeprefix("worker:"),
                        attempt_id=lease.id,
                        campaign_id=campaign_id,
                        target_id=target_id,
                    ),
                    {"input": message},
                )
                assert result.is_error is False, (stage, result.content, result.metadata)
                physical_identity = result.metadata["physical_state_identity"]
                assert physical_identity["kind"] == "keyed_digest"
                collision_physical_identities.append(physical_identity)
                logical_session_id = result.metadata["target_session_id"]
                assert isinstance(logical_session_id, str)
                scheduler.commit_reducer(
                    lease.id,
                    lease.generation,
                    schema_id="worker-result/v1",
                    result={"structured": {}, "target_session_id": logical_session_id},
                    checkpoint={"completed": [node_id]},
                    target_session_id=logical_session_id,
                )
                await target.cleanup_assignment(node_id.removeprefix("worker:"))
            assert collision_physical_identities[0] != collision_physical_identities[1]
            assert collision_physical_identities[0] == collision_physical_identities[2]
            collision_stages = scheduler.baseline_gate(collision_schedule)["agendas"][
                "agenda/aba-collision"
            ]
            assert [(row["outcome"], row["reason"]) for row in collision_stages[:2]] == [
                ("completed", None),
                ("completed", None),
            ]
            assert collision_stages[2]["outcome"] == "ambiguous"
            assert collision_stages[2]["reason"] == (
                "PAIR stages reused one physical target session"
            )
        await first.close()
        database.stop()
        database.start()

        second = build_backend(tmp_path / "workspace")
        try:
            await second._campaigns.status(campaign_id)  # type: ignore[attr-defined]
            with database.connect() as connection:
                effects = ToolEffectRepository(connection)
                restarted_scheduler = DurableSchedulerStore(connection)
                assert restarted_scheduler.baseline_gate(schedule_id) == (
                    restarted_scheduler.baseline_gate(schedule_id)
                )
                assert (
                    restarted_scheduler.baseline_gate(schedule_id)["comparisons"][
                        "agenda/pair-report"
                    ]["receipt_digest"]
                    == pair_receipt_digest
                )
                assert (
                    connection.execute(
                        """SELECT count(*) AS count
                       FROM merlin_storage.baseline_comparison_receipts
                       WHERE schedule_id = %s AND agenda_id = %s""",
                        (schedule_id, "agenda/pair-report"),
                    ).fetchone()["count"]
                    == 1
                )
                replay_attempt, replay_call = attempts["replay"]
                replay = await ToolBroker(
                    (target,), confirmer=allow, effect_store=PostgresToolEffectStore(effects)
                ).invoke(
                    "target_request",
                    ToolContext(
                        session_id=session_id,
                        workspace_root=str(tmp_path),
                        call_id=replay_call,
                        agent_id=f"worker:{campaign_id}/agenda-0123456789abcdef/replay",
                        attempt_id=replay_attempt,
                        campaign_id=campaign_id,
                        target_id=target_id,
                    ),
                    {"input": "attack"},
                )
                assert replay.metadata["idempotent_replay"] is True
                assert message_calls == 6
                assert len(effects.list_for_attempt(replay_attempt)) == 1
            await second._campaigns._finish_campaign(  # type: ignore[attr-defined]  # noqa: SLF001
                campaign_id, "completed", "capped"
            )
            await second._campaigns._report_runner(campaign_id, manifest, "capped")  # type: ignore[attr-defined]
            revision = second._report_repository.list(campaign_id)[-1]  # type: ignore[attr-defined]
            internal = second._report_repository.get(revision.revision_id)  # type: ignore[attr-defined]
            assert internal.report_state == "incomplete"
            pair_lineage = [
                row for row in internal.lineage if row.get("pack") == "baseline-comparison"
            ]
            assert len(pair_lineage) == 3
            exchange_ids = [str(row["id"]) for row in internal.exchanges]
            assert all(exchange_ids.count(str(row["exchange"])) == 1 for row in pair_lineage)
            assert {row["stage"] for row in pair_lineage} == {"control", "direct", "replay"}
            assert all(
                str(row["attempt"]) == attempts[str(row["stage"])][0] for row in pair_lineage
            )
            technique = next(row for row in internal.techniques if row["id"] == technique_id)
            assert technique["pair_verdict"] == "ambiguous"
            assert technique["outcome"] == "unclear"
            assert internal.coverage["unclear"] == 1
            assert technique["independent_reproduction"].startswith("not established")

            customer_raw = DurableReportService(second._report_repository).export(  # type: ignore[attr-defined]
                revision.revision_id, "json", "customer"
            )
            customer = json.loads(customer_raw)
            assert customer["report_state"] == "incomplete"
            customer_lineage = [
                row
                for row in customer["lineage"]
                if row.get("stage") in {"control", "direct", "replay"}
            ]
            customer_exchange_ids = [str(row["id"]) for row in customer["exchanges"]]
            assert len(customer_lineage) == 3
            assert all(
                customer_exchange_ids.count(str(row["exchange"])) == 1 for row in customer_lineage
            )
            serialized = json.dumps(customer, sort_keys=True)
            assert all(attempt_id not in serialized for attempt_id, _call in attempts.values())
            assert all(call_id not in serialized for _attempt_id, call_id in attempts.values())
            with database.connect() as connection:
                raw_effect_ids = [
                    row.idempotency_key
                    for stage, (attempt_id, _call) in attempts.items()
                    for row in ToolEffectRepository(connection).list_for_attempt(attempt_id)
                ]
            assert all(effect_id not in serialized for effect_id in raw_effect_ids)
            with database.connect() as connection:
                forged_scheduler = DurableSchedulerStore(connection)
                forged_assignments = [
                    {
                        **item,
                        "id": (
                            f"campaign/foreign/agenda-fedcba9876543210/{item['execution_stage']}"
                        ),
                        "agenda_id": "agenda/forged-cross-campaign",
                        "predecessor_assignment_id": (
                            None
                            if item["execution_stage"] == "control"
                            else "campaign/foreign/agenda-fedcba9876543210/control"
                            if item["execution_stage"] == "direct"
                            else "campaign/foreign/agenda-fedcba9876543210/direct"
                        ),
                    }
                    for item in assignments
                ]
                forged_nodes = [f"worker:{item['id']}" for item in forged_assignments]
                forged_schedule = forged_scheduler.create_schedule(
                    session_id,
                    {"kind": "baseline", "assignments": forged_assignments},
                    {"completed": []},
                    forged_nodes,
                    ready_nodes=forged_nodes,
                )
                CampaignStore(connection).add_wave(campaign_id, 1, "followup", forged_schedule)
                CampaignStore(connection).prepare(
                    "campaign/foreign",
                    session_id,
                    {"manifest": {}, "assignment_context": {}},
                    "sha256:" + "f" * 64,
                )
                forged_target = ReviewedApiTargetTool(
                    CampaignApiSessionRepository(connection),
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque"},
                    client=client,
                )
                forged_broker = ToolBroker(
                    (forged_target,),
                    confirmer=allow,
                    effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
                )
                forged_session_ids: list[str] = []
                for stage, node_id, message in zip(
                    ("control", "direct", "replay"),
                    forged_nodes,
                    ("control", "attack", "attack"),
                    strict=True,
                ):
                    lease = forged_scheduler.claim(
                        forged_schedule,
                        node_id,
                        node_id,
                        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                    )
                    result = await forged_broker.invoke(
                        "target_request",
                        ToolContext(
                            session_id=session_id,
                            workspace_root=str(tmp_path),
                            call_id=f"forged-{stage}",
                            agent_id=node_id,
                            attempt_id=lease.id,
                            campaign_id="campaign/foreign",
                            target_id=target_id,
                        ),
                        {"input": message},
                    )
                    assert result.is_error is False
                    emitted_session_id = result.metadata.get("target_session_id")
                    assert isinstance(emitted_session_id, str)
                    assert result.metadata["target_session_generation"] == 1
                    forged_session_ids.append(emitted_session_id)
                    forged_scheduler.commit_reducer(
                        lease.id,
                        lease.generation,
                        schema_id="worker-result/v1",
                        result={"structured": {}, "target_session_id": emitted_session_id},
                        checkpoint={"completed": [node_id]},
                        target_session_id=emitted_session_id,
                    )
                assert all(
                    session_id.startswith(
                        f"api:campaign/foreign:{target_id}:scope:{node_id}/attempt/"
                    )
                    and session_id.endswith(":generation:1")
                    for session_id, node_id in zip(
                        forged_session_ids, forged_nodes, strict=True
                    )
                )
                assert len(set(forged_session_ids)) == 3
                forged_gate = forged_scheduler.baseline_gate(forged_schedule)
                assert forged_gate["positive"] == ()
                assert forged_gate["comparison_qualified"] == ()
                assert all(
                    row["outcome"] != "completed"
                    for row in forged_gate["agendas"]["agenda/forged-cross-campaign"]
                )
        finally:
            await second.close()
            await client.aclose()


@pytest.mark.asyncio
async def test_pg_restart_restores_compaction_and_partial_output_across_provider_route_change(
    tmp_path: Path,
) -> None:
    class PartialProvider:
        capabilities = ProviderCapabilities(context_window_tokens=1_024)

        async def stream(self, request):
            yield ReasoningDelta(text="partial reasoning")
            yield TextDelta(text="partial answer")
            yield ModelError(code="incomplete_stream", message="connection ended", partial=False)

    data_dir = tmp_path / "postgres-replacement"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            first_store = PostgresSessionStore(connection)
            first = Kernel(PartialProvider(), first_store, _NoTools(), _Settings("route-a"))
            session_id = await first.start()
            for sequence in range(60):
                await first_store.append_message(
                    session_id,
                    ChatMessage(role="user", content=f"{sequence}:" + " durable" * 20),
                )
            await first.submit("begin")
            await first.wait_for_turn()
            before_restart = await first_store.context_provenance(session_id)
            assert before_restart["compactions"]
            assert (await first_store.load_messages(session_id))[-1].content == "partial answer"

    class ReplacementProvider:
        capabilities = ProviderCapabilities(context_window_tokens=4_096)

        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta(text="replacement completed")
            yield ModelCompleted()

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted_database:
        with restarted_database.connect() as restarted_connection:
            MigrationRunner(restarted_connection).apply()
            restarted_store = PostgresSessionStore(restarted_connection)
            replacement_provider = ReplacementProvider()
            replacement = Kernel(
                replacement_provider,
                restarted_store,
                _NoTools(),
                _Settings("route-b"),
            )
            await replacement.start(session_id)
            await replacement.submit("continue")
            await replacement.wait_for_turn()

            after_restart = await restarted_store.context_provenance(session_id)
            assert after_restart["compactions"]
            assert after_restart["compactions"][0]["id"] == before_restart["compactions"][0]["id"]
            assert any(
                message.content == "partial answer"
                and message.reasoning_content == "partial reasoning"
                for message in replacement_provider.requests[0].messages
            )


@pytest.mark.asyncio
async def test_local_control_restart_restores_compaction_and_partial_provider_output(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-daemon-replacement"
    socket_path = Path(f"/tmp/merlin-context-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    await asyncio.to_thread(socket_path.with_suffix(".journal.json").unlink, missing_ok=True)
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            seed = PostgresSessionStore(connection)
            session_id = await seed.create_session()
            for sequence in range(60):
                await seed.append_message(
                    session_id,
                    ChatMessage(role="user", content=f"{sequence}:" + " durable" * 20),
                )

    class PartialProvider:
        capabilities = ProviderCapabilities(context_window_tokens=1_024)

        async def stream(self, request):
            yield ReasoningDelta(text="daemon partial reasoning")
            yield TextDelta(text="daemon partial answer")
            yield ModelError(code="incomplete_stream", message="connection ended", partial=False)

    class ReplacementProvider:
        capabilities = ProviderCapabilities(context_window_tokens=4_096)

        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta(text="replacement completed")
            yield ModelCompleted()

    class LazyBackend:
        def __init__(self, provider, model: str) -> None:
            self.database = Database(DatabaseConfig(data_dir=data_dir)).start()
            self.connection = self.database.connect()
            MigrationRunner(self.connection).apply()
            self.store = PostgresSessionStore(self.connection)
            self.kernel = Kernel(provider, self.store, _NoTools(), _Settings(model))
            self.backend = KernelBackend(
                self.kernel,
                resources=(self.database, self.connection),
            )
            self.started = False

        async def stream(self, prompt):
            if not self.started:
                await self.kernel.start(session_id)
                self.started = True
            async for event in self.backend.stream(prompt):
                yield event

        async def detach(self) -> None:
            await self.backend.close()

    built = []

    def first_factory(**_):
        backend = LazyBackend(PartialProvider(), "route-a")
        built.append(backend)
        return backend

    first_daemon = asyncio.create_task(serve(socket_path, backend_factory=first_factory))
    for _ in range(100):
        if await asyncio.to_thread(socket_path.exists):
            break
        await asyncio.sleep(0.01)
    assert [event async for event in LocalControlClient(socket_path=socket_path).stream("begin")]
    assert await stop_daemon(socket_path)
    await first_daemon

    replacement_provider = ReplacementProvider()

    def second_factory(**_):
        backend = LazyBackend(replacement_provider, "route-b")
        built.append(backend)
        return backend

    second_daemon = asyncio.create_task(serve(socket_path, backend_factory=second_factory))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        assert [
            event async for event in LocalControlClient(socket_path=socket_path).stream("continue")
        ]
        provenance = await built[-1].store.context_provenance(session_id)
        assert provenance["compactions"]
        assert any(
            message.content == "daemon partial answer"
            and message.reasoning_content == "daemon partial reasoning"
            for message in replacement_provider.requests[0].messages
        )
    finally:
        assert await stop_daemon(socket_path)
        await second_daemon
        await asyncio.to_thread(
            socket_path.with_suffix(".journal.json").unlink,
            missing_ok=True,
        )


class _ProbeBrowserSession:
    def __init__(self, responses: dict[str, tuple[int, object]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, object]] = []

    async def request_json(self, method, url, payload, headers, timeout_ms=15_000):
        self.calls.append((method, url, payload))
        if url not in self._responses:
            raise ValueError(f"unstubbed probe url {url}")
        return self._responses[url]


class _ProbeBrowser:
    def __init__(self, session: _ProbeBrowserSession) -> None:
        self._session = session

    async def for_tool(self, session_id: str) -> _ProbeBrowserSession:
        return self._session


def _admit_synthetic_live_handoff(backend) -> None:
    """Mark fixture-only handoffs live when no Playwright context is constructed."""
    backend._browser.handoff_context_is_current = lambda *_args: True  # type: ignore[attr-defined,method-assign]


def _probe_handoff(adapter: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        adapter=json.dumps(adapter, sort_keys=True, separators=(",", ":")),
        reusable_adapter=None,
        owner=SimpleNamespace(session_id="earlier-session"),
    )


def test_handoff_context_current_delegates_to_browser_incarnation() -> None:
    from merlin_tui.bootstrap import _handoff_context_is_current

    sealed_at = datetime.now(UTC)
    calls: list[tuple[str, datetime]] = []

    class Browser:
        def handoff_context_is_current(self, context_id: str, created_at: datetime) -> bool:
            calls.append((context_id, created_at))
            return False

    handoff = SimpleNamespace(context_id="context-1", created_at=sealed_at)
    assert _handoff_context_is_current(Browser(), handoff) is False
    assert calls == [("context-1", sealed_at)]


@pytest.mark.asyncio
async def test_cross_session_handoff_reuse_replays_setup_steps_only() -> None:
    from merlin_tui.bootstrap import _revalidate_handoff_cross_session

    adapter = {
        "interface": "http_api",
        "api_steps": [
            {
                "step_id": "observed-session-setup",
                "method": "POST",
                "endpoint": "https://target.test/api/start",
                "expected_status": 200,
                "interaction": False,
                "cleanup": False,
                "captures": {"session_id": "/sessionId"},
                "request_template": {"aiAssisted": True},
            },
            {
                "step_id": "observed-message-exchange",
                "method": "POST",
                "endpoint": "https://target.test/api/message",
                "expected_status": 200,
                "interaction": True,
                "request_template": {
                    "sessionId": "{{capture:session_id}}",
                    "userMessage": "{{input}}",
                },
            },
        ],
    }
    session = _ProbeBrowserSession(
        {"https://target.test/api/start": (200, {"sessionId": "fresh-1"})}
    )
    assert (
        await _revalidate_handoff_cross_session(
            _ProbeBrowser(session), "current-session", _probe_handoff(adapter)
        )
        is True
    )
    # The interaction step is never replayed by the probe.
    assert session.calls == [("POST", "https://target.test/api/start", {"aiAssisted": True})]


@pytest.mark.asyncio
async def test_cross_session_handoff_reuse_rejects_failed_probe() -> None:
    from merlin_tui.bootstrap import _revalidate_handoff_cross_session

    adapter = {
        "interface": "http_api",
        "api_steps": [
            {
                "step_id": "observed-session-setup",
                "method": "POST",
                "endpoint": "https://target.test/api/start",
                "expected_status": 200,
                "interaction": False,
                "cleanup": False,
                "captures": {"session_id": "/sessionId"},
                "request_template": {},
            }
        ],
    }
    # Wrong status.
    session = _ProbeBrowserSession({"https://target.test/api/start": (401, {})})
    assert (
        await _revalidate_handoff_cross_session(
            _ProbeBrowser(session), "current-session", _probe_handoff(adapter)
        )
        is False
    )
    # Capture missing from the response.
    session = _ProbeBrowserSession({"https://target.test/api/start": (200, {})})
    assert (
        await _revalidate_handoff_cross_session(
            _ProbeBrowser(session), "current-session", _probe_handoff(adapter)
        )
        is False
    )
    # No replayable setup steps at all.
    assert (
        await _revalidate_handoff_cross_session(
            _ProbeBrowser(session),
            "current-session",
            _probe_handoff({"interface": "http_api", "api_steps": []}),
        )
        is False
    )
