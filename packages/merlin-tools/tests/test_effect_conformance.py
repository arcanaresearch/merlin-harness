"""Registry-wide effect classification and auto-permission conformance."""

from __future__ import annotations

import asyncio
from pathlib import Path

from merlin_browser import browser_tools
from merlin_contracts import ToolResult, ToolSpec
from merlin_permissions import (
    PermissionEffect,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    PermissionScope,
)
from merlin_tools import McpTool, ToolBroker, ToolContext, workspace_tool_broker

# Pure observation may run automatically in AUTO mode. Reviewed mutation
# (harness state, workspace writes) requires operator review; authority
# expansion, subprocess, and destructive action are never automatic.
AUTO_RUNNABLE = {
    PermissionEffect.WORKSPACE_READ,
    PermissionEffect.PROVIDER_NETWORK,
    PermissionEffect.TARGET_READ,
}
NEVER_AUTO = {
    PermissionEffect.HARNESS_STATE,
    PermissionEffect.WORKSPACE_WRITE,
    PermissionEffect.TARGET_NETWORK,
    PermissionEffect.SUBPROCESS,
    PermissionEffect.DESTRUCTIVE,
    PermissionEffect.UNKNOWN,
}


class PermissionStore:
    def __init__(self) -> None:
        self.audits = []

    async def find_rule(self, request):
        return None

    async def save_rule(self, rule) -> None:
        raise AssertionError("no session rule expected")

    async def record_audit(self, audit) -> None:
        self.audits.append(audit)


class StubRecorder:
    async def save_metadata(self, metadata) -> None: ...

    async def append_action(self, action) -> None: ...

    async def append_observation(self, observation) -> None: ...

    async def record_evidence(self, context_id, evidence_ref, kind, payload) -> None: ...

    async def record_handoff(self, handoff) -> None: ...

    async def list_handoffs(self, target_id, limit=20):
        return ()


class StubMcpManager:
    def __init__(self) -> None:
        self.calls = []

    async def list_tools(self, server_name):
        return ()

    async def invoke(self, server_name, tool_name, arguments):
        self.calls.append(tool_name)
        return ToolResult(content="mcp")


def _registry(*, permissions: PermissionEngine | None = None) -> ToolBroker:
    def missing_session(_session_id):
        raise ValueError("no browser session in conformance registry")

    broker = workspace_tool_broker(permissions=permissions)
    for tool in browser_tools(missing_session, StubRecorder()):
        broker.register(tool)
    broker.register(
        McpTool(
            StubMcpManager(),
            server_name="demo",
            tool_name="lookup",
            description="declared MCP tool",
            input_schema={"type": "object"},
            effect="target_network",
        )
    )
    return broker


def _arguments_for(spec: ToolSpec) -> dict[str, object]:
    properties = spec.input_schema.get("properties", {})
    required = spec.input_schema.get("required", ())
    fillers = {
        "string": "conformance",
        "integer": 100,
        "object": {},
        "array": ["conformance"],
        "boolean": True,
    }
    return {
        name: fillers.get(definition.get("type", "string"), "conformance")
        for name, definition in properties.items()
        if name in required
    }


def test_every_registered_tool_declares_a_valid_known_effect() -> None:
    broker = _registry()
    assert broker.describe(), "conformance registry must not be empty"

    for spec in broker.describe():
        declared = broker.effect(spec.name)
        assert declared is not None, f"{spec.name} has no declared effect"
        effect = PermissionEffect(declared)
        assert effect is not PermissionEffect.UNKNOWN, f"{spec.name} declares unknown effect"


def test_auto_mode_decision_matrix_covers_every_effect() -> None:
    engine = PermissionEngine(PermissionMode.AUTO, PermissionStore())

    async def decide(effect: PermissionEffect) -> bool:
        return await engine.authorize(
            PermissionRequest(
                effect,
                PermissionScope("session", "tool", "/workspace"),
            )
        )

    outcomes = {effect: asyncio.run(decide(effect)) for effect in PermissionEffect}
    assert set(PermissionEffect) == AUTO_RUNNABLE | NEVER_AUTO
    for effect, allowed in outcomes.items():
        assert allowed is (effect in AUTO_RUNNABLE), f"unexpected AUTO outcome for {effect}"


def test_auto_mode_gates_every_registered_tool_by_declared_effect(tmp_path: Path) -> None:
    engine = PermissionEngine(PermissionMode.AUTO, PermissionStore())
    broker = _registry(permissions=engine)

    for spec in broker.describe():
        effect = PermissionEffect(broker.effect(spec.name) or "unknown")
        # The exact authorization the broker requests for this tool's effect.
        allowed = asyncio.run(
            engine.authorize(
                PermissionRequest(effect, PermissionScope("conformance", spec.name, str(tmp_path)))
            )
        )
        assert allowed is (effect in AUTO_RUNNABLE), (
            f"{spec.name} ({effect}) auto-permission outcome mismatch"
        )
        context = ToolContext(session_id="conformance", workspace_root=str(tmp_path))
        result = asyncio.run(broker.invoke(spec.name, context, _arguments_for(spec)))
        if result.metadata.get("schema_validation_failed") is True:
            continue  # synthesized arguments cannot reach the permission boundary
        denied = result.metadata.get("permission_denied") is True
        if effect in AUTO_RUNNABLE:
            assert not denied, f"{spec.name} observation must auto-run without review"
        else:
            assert denied, f"{spec.name} ({effect}) must never run automatically"
            assert result.is_error
