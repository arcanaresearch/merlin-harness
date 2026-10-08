from __future__ import annotations

import hashlib
import json

import pytest
from merlin_agents.campaign import ContextDocument
from merlin_agents.durable_service import _worker_context


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _legacy_worker_context() -> dict[str, object]:
    profile = ContextDocument.create("profile:test", "historical worker profile")
    values: dict[str, object] = {
        "assignment_id": "assignment:test",
        "profile": {
            "id": profile.id,
            "content": profile.content,
            "digest": profile.digest,
        },
        "guidance": [],
        "skills": [],
        "tool_ids": ["target_request"],
        # Historical schedules predate the optional browser/session/auth fields.
        "target": {
            "id": "target:test",
            "interface": "http_api",
            "endpoint": "https://example.test/message",
        },
        "authority_ids": ["authority:test"],
    }
    return {**values, "digest": _digest(values)}


def test_worker_context_recovery_authenticates_exact_historical_target_shape() -> None:
    restored = _worker_context(_legacy_worker_context())

    assert restored.target.endpoint == "https://example.test/message"
    assert restored.target.authentication is None
    assert restored.strategy_id == "direct"


def test_worker_context_recovery_rejects_tampered_historical_target() -> None:
    persisted = _legacy_worker_context()
    target = persisted["target"]
    assert isinstance(target, dict)
    target["endpoint"] = "https://attacker.test/message"

    with pytest.raises(ValueError, match="worker context digest mismatch"):
        _worker_context(persisted)
