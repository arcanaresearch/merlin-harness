from __future__ import annotations

from merlin_contracts.state_isolation import (
    StateIsolationStrategy,
    resolve_state_isolation,
)
from merlin_contracts.target_capabilities import StateCapabilities


def _selector(step_id: str, pointer: str) -> dict[str, object]:
    return {
        "schema_version": "merlin.state-selector.v1",
        "step_id": step_id,
        "evaluation_root": "response_body",
        "pointer": pointer,
        "evidence_refs": [f"evidence:{step_id}:{pointer}"],
        "confidence": 1.0,
    }


def _capabilities(*kinds: str) -> StateCapabilities:
    step_ids = {
        "provision_fresh_state": "fresh-step",
        "restore_checkpoint": "restore-step",
        "clone_state": "clone-step",
    }
    primary_step = (
        "fresh-step"
        if "provision_fresh_state" in kinds
        else "restore-step"
        if "restore_checkpoint" in kinds
        else "clone-step"
    )
    document: dict[str, object] = {
        "schema_version": "merlin.state-capabilities.v1",
        "identity": {
            "identity_scope": "physical_state",
            "baseline_selector": _selector(primary_step, "/state/id"),
            "result_selector": _selector("interaction", "/state/id"),
            "evidence_refs": ["evidence:identity"],
        },
        "freshness_rules": [
            {
                "selector": _selector(primary_step, "/state/fresh"),
                "match_values": [True],
                "evidence_refs": ["evidence:freshness"],
            }
        ],
        "affordances": [
            {
                "kind": kind,
                "step_id": step_ids[kind],
                "evaluation_root": "response_body",
                "evidence_refs": [f"evidence:{kind}"],
                "confidence": 1.0,
            }
            for kind in kinds
        ],
        "lifecycle": {
            "isolation": "independent",
            "baseline": (
                "fresh"
                if "provision_fresh_state" in kinds
                else "restore"
                if "restore_checkpoint" in kinds
                else "clone"
            ),
            "cleanup": "not_required",
            "evidence_refs": ["evidence:lifecycle"],
            "confidence": 1.0,
        },
        "maximum_parallel_states": 3,
    }
    if "restore_checkpoint" in kinds:
        document["checkpoint"] = {
            "schema_version": "merlin.state-checkpoint.v1",
            "capture_selector": _selector("interaction", "/checkpoint"),
            "capture_timing": "post_success_response",
            "binding_name": "checkpoint",
            "restore_step_id": "restore-step",
            "restore_request_pointer": "/checkpoint",
            "evidence_refs": ["evidence:checkpoint"],
        }
    return StateCapabilities.model_validate(document)


def test_resolver_prefers_fresh_then_restore_then_clone() -> None:
    all_capabilities = _capabilities("provision_fresh_state", "restore_checkpoint", "clone_state")
    assert (
        resolve_state_isolation(all_capabilities, checkpoint_available=True).strategy
        is StateIsolationStrategy.FRESH
    )
    restore = _capabilities("restore_checkpoint", "clone_state")
    assert (
        resolve_state_isolation(restore, checkpoint_available=True).strategy
        is StateIsolationStrategy.RESTORE
    )
    assert (
        resolve_state_isolation(restore, checkpoint_available=False).strategy
        is StateIsolationStrategy.CLONE
    )


def test_unknown_or_incompletely_evidenced_state_serializes() -> None:
    assert resolve_state_isolation(None).strategy is StateIsolationStrategy.SERIALIZED
    legacy_unknown = StateCapabilities.model_validate({"maximum_parallel_states": 9})
    plan = resolve_state_isolation(legacy_unknown)
    assert plan.strategy is StateIsolationStrategy.SERIALIZED
    assert plan.maximum_parallel_states == 1
    assert plan.isolation_disposition == "shared_serialized"
    assert plan.fallback_reason is not None


def test_parallel_delivery_requires_identity_freshness_and_explicit_capacity() -> None:
    plan = resolve_state_isolation(_capabilities("clone_state"))
    assert plan.strategy is StateIsolationStrategy.CLONE
    assert plan.permits_parallel_delivery is True
    assert plan.maximum_parallel_states == 3
    assert plan.requires_identity_comparison is True
    assert plan.requires_freshness_proof is True


def test_isolated_plan_does_not_degrade_to_shared_when_capacity_is_one() -> None:
    document = _capabilities("provision_fresh_state").model_dump(mode="json")
    document["maximum_parallel_states"] = 1
    plan = resolve_state_isolation(StateCapabilities.model_validate(document))
    assert plan.strategy is StateIsolationStrategy.FRESH
    assert plan.isolated is True
    assert plan.permits_parallel_delivery is False
    assert plan.isolation_disposition == "fresh"
