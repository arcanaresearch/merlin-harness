"""Canonical executable target-adapter identity."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

EXECUTABLE_ADAPTER_IDENTITY_VERSION = "merlin.executable-adapter.v1"
_TARGET_DELIVERIES = frozenset({"direct-user-channel", "inter-agent-message"})
_CLEANUP_LIFECYCLES = frozenset({"required", "not_required", "unknown"})


def reviewed_adapter_target_delivery(adapter: Mapping[str, object]) -> str | None:
    """Return an explicit reviewed target channel; absence remains unknown."""
    value = adapter.get("target_delivery")
    return value if isinstance(value, str) and value in _TARGET_DELIVERIES else None


def reviewed_adapter_cleanup_lifecycle(adapter: Mapping[str, object]) -> str:
    """Return the target cleanup obligation without inferring absence as safety."""
    steps = adapter.get("api_steps", ())
    if isinstance(steps, list) and any(
        isinstance(step, Mapping) and step.get("cleanup") is True for step in steps
    ):
        return "required"
    lifecycle = adapter.get("cleanup_lifecycle")
    status = lifecycle.get("status") if isinstance(lifecycle, Mapping) else None
    normalized = {
        "observed": "unknown",
        "not_established": "unknown",
        "not_required": "not_required",
        "required": "required",
        "unknown": "unknown",
    }.get(status if isinstance(status, str) else None, "unknown")
    return normalized if normalized in _CLEANUP_LIFECYCLES else "unknown"


def _terminal_response_projection(value: object) -> object:
    """Keep executable terminal semantics while excluding evidence provenance."""
    if not isinstance(value, Mapping):
        return value
    conditions = value.get("conditions")
    if isinstance(conditions, list):
        return {
            "conditions": [_terminal_response_projection(condition) for condition in conditions]
        }
    return {
        key: value.get(key)
        for key in (
            "pointer",
            "match_values",
            "status_codes",
            "effect_applied",
            "replay_safe",
        )
        if key in value
    }


def _session_creation_predicate_projection(value: object) -> object:
    if not isinstance(value, Mapping):
        return value
    projected = {key: value.get(key) for key in ("method", "url", "pointer") if key in value}
    statuses = value.get("status_codes")
    if isinstance(statuses, list):
        projected["status_codes"] = sorted(set(statuses))
    matches = value.get("match_values")
    if isinstance(matches, list):
        projected["match_values"] = sorted(
            matches, key=lambda item: json.dumps(item, sort_keys=True)
        )
    zero_turn = value.get("zero_turn")
    if isinstance(zero_turn, Mapping):
        projected["zero_turn"] = _session_creation_predicate_projection(zero_turn)
    return projected


def executable_adapter_projection(adapter: Mapping[str, object]) -> dict[str, object]:
    """Project fields that determine the target effect, excluding evaluator hints."""
    api_steps = [
        {
            **{
                key: step.get(key)
                for key in (
                    "tool",
                    "endpoint",
                    "method",
                    "request_template",
                    "interaction",
                    "cleanup",
                    "captures",
                    "repeat",
                    "idempotency_key",
                )
            },
            **(
                {"request_bindings": step.get("request_bindings")}
                if step.get("request_bindings")
                else {}
            ),
            **(
                {"replay_after_ambiguous_result": step.get("replay_after_ambiguous_result")}
                if "replay_after_ambiguous_result" in step
                else {}
            ),
            **(
                {
                    "terminal_response_contract": _terminal_response_projection(
                        step.get("terminal_response_contract")
                    )
                }
                if "terminal_response_contract" in step
                else {}
            ),
            **(
                {
                    "cleanup_acknowledgement": _terminal_response_projection(
                        step.get("cleanup_acknowledgement")
                    )
                }
                if "cleanup_acknowledgement" in step
                else {}
            ),
            **(
                {"cleanup_action_evidence_ref": step.get("cleanup_action_evidence_ref")}
                if "cleanup_action_evidence_ref" in step
                else {}
            ),
        }
        for step in adapter.get("api_steps", ())
        if isinstance(step, Mapping)
    ]
    browser_steps = [
        {
            key: step.get(key)
            for key in ("tool", "arguments", "cleanup", "repeat", "idempotency_key")
        }
        for step in adapter.get("browser_steps", ())
        if isinstance(step, Mapping)
    ]
    interaction = adapter.get("interaction_contract")
    interaction = interaction if isinstance(interaction, Mapping) else {}
    projection = {
        "version": EXECUTABLE_ADAPTER_IDENTITY_VERSION,
        "interface": adapter.get("interface"),
        "endpoint": adapter.get("endpoint"),
        "credential_handle": adapter.get("credential_handle"),
        "api_transport": adapter.get("api_transport"),
        **(
            {"target_delivery": reviewed_adapter_target_delivery(adapter)}
            if reviewed_adapter_target_delivery(adapter) is not None
            else {}
        ),
        "api_steps": api_steps,
        "browser_steps": browser_steps,
        "browser_interaction": {
            key: interaction.get(key) for key in ("input_locator", "submit_action", "frame_context")
        },
    }
    if "cleanup_lifecycle" in adapter:
        projection["cleanup_lifecycle"] = reviewed_adapter_cleanup_lifecycle(adapter)
    cleanup_discovery = adapter.get("cleanup_discovery")
    if isinstance(cleanup_discovery, Mapping):
        projection["cleanup_discovery"] = {
            key: cleanup_discovery.get(key)
            for key in ("status", "session_ownership", "failure_code", "retryable")
            if key in cleanup_discovery
        }
    creation_transition = adapter.get("session_creation_transition")
    if isinstance(creation_transition, Mapping):
        projection["session_creation_transition"] = {
            "precondition": _session_creation_predicate_projection(
                creation_transition.get("precondition")
            ),
            "postcondition": _session_creation_predicate_projection(
                creation_transition.get("postcondition")
            ),
        }
    return projection


def executable_adapter_digest(adapter: Mapping[str, object]) -> str:
    """Hash the sole versioned target-effect identity for an executable adapter."""
    encoded = json.dumps(
        executable_adapter_projection(adapter),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
