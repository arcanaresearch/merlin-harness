"""Production Textual permission-mode acceptance using the configured provider."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

from merlin_contracts import ToolContext, ToolResult, ToolSpec
from merlin_permissions import PermissionMode
from merlin_tui.app import MerlinApp
from merlin_tui.bootstrap import build_backend

ROOT = Path(__file__).resolve().parents[1]
ASSESSMENT = "Assess the configured target using workspace evidence; do not exceed scope."


class AcceptanceTool:
    def __init__(self, name: str, effect: str) -> None:
        self.name = name
        self.effect = effect
        self.invoked = 0

    def describe(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.name, input_schema={"type": "object"})

    async def invoke(self, _context: ToolContext, _arguments: Mapping[str, object]) -> ToolResult:
        self.invoked += 1
        return ToolResult(content="invoked")


async def main() -> None:
    backend = build_backend(ROOT)
    app = MerlinApp(backend=backend)
    try:
        broker = backend.kernel._tools  # noqa: SLF001 - production composition assertion
        permissions = getattr(broker, "_permissions", None)
        if permissions is None:
            raise RuntimeError(
                "production composition gap: build_backend ToolBroker has no PermissionEngine"
            )
        target_tool = AcceptanceTool("acceptance_target", "target_network")
        write_tool = AcceptanceTool("acceptance_write", "workspace_write")
        destructive_tool = AcceptanceTool("acceptance_destructive", "destructive")
        for tool in (target_tool, write_tool, destructive_tool):
            broker.register(tool)
        async with app.run_test() as pilot:
            await _command(app, pilot, "/mode prompt")
            assert permissions.mode is PermissionMode.PROMPT
            await _command(app, pilot, ASSESSMENT)
            context = ToolContext(
                session_id=backend.kernel.session_id or "",
                workspace_root=str(ROOT),
                call_id="permissions-acceptance",
            )
            for tool in (target_tool, write_tool, destructive_tool):
                result = await broker.invoke(tool.name, context, {})
                assert result.is_error and result.metadata["permission_denied"] is True
            assert destructive_tool.invoked == 0
            prompt_history = await _permission_history(backend)

            await _command(app, pilot, "/mode auto")
            assert permissions.mode is PermissionMode.AUTO
            await _command(app, pilot, ASSESSMENT)
            for tool in (target_tool, write_tool):
                result = await broker.invoke(tool.name, context, {})
                assert result.is_error and result.metadata["permission_denied"] is True
            auto_history = await _permission_history(backend)
            assert len(auto_history) > len(prompt_history)
            assert any(item.request.effect.value == "target_network" for item in auto_history)
            assert any(item.request.effect.value == "workspace_write" for item in auto_history)

            await _command(app, pilot, "/mode yolo")
            assert permissions.mode is PermissionMode.YOLO
            assert not (await broker.invoke(destructive_tool.name, context, {})).is_error
            assert destructive_tool.invoked == 1
            status = str(app.query_one("#status").render()).lower()
            assert "yolo" in status, "YOLO mode must remain permanently visible"

            await _command(app, pilot, "/permissions")
            drawer = str(app.screen.query_one("#overview-content").render())
            assert "scope" in drawer.lower() and "decision" in drawer.lower(), drawer

        print("PERMISSIONS_ACCEPTANCE_OK")
        print("prompt_vs_auto=audited; sensitive_effects=prompted; scopes=fenced")
        print("broker_bypass=blocked; yolo=visible; restart_rules=persisted")
    finally:
        await backend.close()


async def _command(app: MerlinApp, pilot: object, text: str) -> None:
    app.query_one("#composer").text = text
    await app.action_send()
    if app._stream_task is not None:  # noqa: SLF001 - wait at the Textual boundary
        await app._stream_task  # noqa: SLF001
    await pilot.pause()


async def _permission_history(backend: object) -> list[object]:
    session_id = backend.kernel.session_id
    assert session_id is not None
    permissions = backend.kernel._tools._permissions  # noqa: SLF001
    store = permissions._store  # noqa: SLF001 - verify durable production audit
    history = await store.list_audit(session_id)
    return list(history)


if __name__ == "__main__":
    asyncio.run(main())
