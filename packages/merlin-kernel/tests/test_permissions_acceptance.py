"""Deterministic production ToolBroker + PostgreSQL permission acceptance."""

from __future__ import annotations

import asyncio
from pathlib import Path

from merlin_contracts import ToolContext, ToolResult, ToolSpec
from merlin_permissions import (
    PermissionDecision,
    PermissionEffect,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    PermissionRule,
    PermissionScope,
)
from merlin_storage_postgres import (
    Database,
    DatabaseConfig,
    MigrationRunner,
    PermissionRepository,
    PostgresPermissionStore,
    SessionRepository,
)
from merlin_tools import ToolBroker


class EffectTool:
    def __init__(self, name: str, effect: str) -> None:
        self.name = name
        self.effect = effect
        self.invocations = 0

    def describe(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.name, input_schema={"type": "object"})

    async def invoke(self, context: ToolContext, arguments: dict[str, object]) -> ToolResult:
        self.invocations += 1
        return ToolResult(content=f"{self.name}:executed")


def test_prompt_auto_scoping_bypass_yolo_and_restart(tmp_path: Path) -> None:
    asyncio.run(_permission_journey(tmp_path))


async def _permission_journey(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("permission acceptance")
            store = PostgresPermissionStore(PermissionRepository(connection))
            context = ToolContext(session_id=session.id, workspace_root=str(tmp_path))

            prompt_requests = []

            async def allow_once(spec, _context, _arguments):
                prompt_requests.append(spec.name)
                return True

            tools = (
                EffectTool("read", "workspace_read"),
                EffectTool("write", "workspace_write"),
                EffectTool("target", "target_network"),
            )
            prompt = ToolBroker(
                tools,
                confirmer=allow_once,
                permissions=PermissionEngine(PermissionMode.PROMPT, store),
                permission_scope=_scope,
            )
            for name in ("read", "target", "write"):
                assert not (await prompt.invoke(name, context, {"target": "one"})).is_error
            assert prompt_requests == ["read", "target", "write"]

            auto_requests = []

            async def auto_allow(spec, _context, _arguments):
                auto_requests.append(spec.name)
                return True

            auto = ToolBroker(
                tools,
                confirmer=auto_allow,
                permissions=PermissionEngine(PermissionMode.AUTO, store),
                permission_scope=_scope,
            )
            for name in ("read", "target", "write"):
                assert not (await auto.invoke(name, context, {"target": "one"})).is_error
            assert auto_requests == ["target", "write"]

            scope_prompts = []

            async def remember(_spec, _context, arguments):
                scope_prompts.append(arguments["target"])
                return PermissionDecision.ALLOW_SESSION

            scoped = ToolBroker(
                (EffectTool("target-scoped", "target_network"),),
                confirmer=remember,
                permissions=PermissionEngine(PermissionMode.AUTO, store),
                permission_scope=_scope,
            )
            await scoped.invoke("target-scoped", context, {"target": "alpha"})
            await scoped.invoke("target-scoped", context, {"target": "alpha"})
            await scoped.invoke("target-scoped", context, {"target": "beta"})
            assert scope_prompts == ["alpha", "beta"]

            destructive = EffectTool("misbehaving", "destructive")

            async def deny(_spec, _context, _arguments):
                return False

            guarded = ToolBroker(
                (destructive,),
                confirmer=deny,
                permissions=PermissionEngine(PermissionMode.AUTO, store),
            )
            denied = await guarded.invoke("misbehaving", context, {})
            assert denied.is_error and not destructive.invocations

            yolo_tool = EffectTool("yolo-write", "workspace_write")
            yolo = ToolBroker(
                (yolo_tool,), permissions=PermissionEngine(PermissionMode.YOLO, store)
            )
            assert not (await yolo.invoke("yolo-write", context, {})).is_error
            assert (await store.list_audit(session.id))[0].mode is PermissionMode.YOLO

            restarted = PermissionEngine(PermissionMode.AUTO, store)
            remembered = PermissionRequest(
                PermissionEffect.WORKSPACE_WRITE,
                _scope("write", context, {"target": "one"}),
            )
            await store.save_rule(
                # Persist the exact session/scope decision, then recreate the engine.
                PermissionRule(
                    session.id,
                    remembered.scope.tool_name,
                    remembered.effect,
                    remembered.scope.fingerprint,
                    PermissionDecision.ALLOW_SESSION,
                )
            )
            assert await restarted.authorize(remembered)


def _scope(name: str, context: ToolContext, arguments: dict[str, object]) -> PermissionScope:
    target = arguments.get("target")
    return PermissionScope(
        context.session_id,
        name,
        context.workspace_root,
        target=str(target) if target is not None else None,
    )
