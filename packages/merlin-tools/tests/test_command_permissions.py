from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from merlin_contracts import ToolContext, ToolResult, ToolSpec
from merlin_permissions import (
    PermissionAudit,
    PermissionDecision,
    PermissionEffect,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    PermissionRule,
    PermissionScope,
)
from merlin_tools import ShellCommandTool, ToolBroker, workspace_tool_broker
from merlin_tools.broker import _shell_command_scope


class CommandPermissionStore:
    def __init__(self) -> None:
        self.rules: list[PermissionRule] = []
        self.audits: list[PermissionAudit] = []

    async def find_rule(self, request: PermissionRequest) -> PermissionRule | None:
        return next(
            (
                rule
                for rule in reversed(self.rules)
                if rule.session_id == request.scope.session_id
                and rule.tool_name == request.scope.rule_key
                and rule.effect == request.effect
                and rule.scope_fingerprint == request.scope.fingerprint
            ),
            None,
        )

    async def save_rule(self, rule: PermissionRule) -> None:
        self.rules.append(rule)

    async def record_audit(self, audit: PermissionAudit) -> None:
        self.audits.append(audit)

    async def list_audit(self, session_id: str, *, limit: int = 100) -> list[PermissionAudit]:
        return [a for a in self.audits if a.request.scope.session_id == session_id][-limit:]


def test_exact_command_session_reuses_only_matching_authority(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = CommandPermissionStore()
        prompts: list[dict[str, object]] = []

        async def confirm(spec, context, arguments):
            prompts.append(dict(arguments))
            return PermissionDecision.ALLOW_COMMAND_SESSION

        broker = workspace_tool_broker(
            permissions=PermissionEngine(PermissionMode.PROMPT, store), confirmer=confirm
        )
        context = ToolContext(session_id="one", workspace_root=str(tmp_path), target_id="first")
        args = {"argv": ["/bin/echo", "--token=secret-one"]}
        for cwd in (None, ".", "./"):
            result = await broker.invoke(
                "shell_command", context, args if cwd is None else args | {"cwd": cwd}
            )
            assert not result.is_error
        assert len(prompts) == 1
        assert prompts[0]["__permission_command_scope"] is True

        changed = [
            (context, {"argv": ["/bin/echo", "--token=secret-two"]}),
            (context, {"argv": ["/bin/echo", "--token", "secret-one"]}),
            (context.model_copy(update={"session_id": "two"}), args),
            (context.model_copy(update={"target_id": "second"}), args),
        ]
        (tmp_path / "child").mkdir()
        changed.append((context, args | {"cwd": "child"}))
        other = tmp_path / "other"
        other.mkdir()
        changed.append((context.model_copy(update={"workspace_root": str(other)}), args))
        for changed_context, changed_args in changed:
            result = await broker.invoke("shell_command", changed_context, changed_args)
            assert not result.is_error
        assert len(prompts) == 7
        assert all(rule.decision is PermissionDecision.ALLOW_SESSION for rule in store.rules)
        assert all("secret" not in rule.tool_name for rule in store.rules)
        assert all("secret" not in a.request.scope.rule_key for a in store.audits)

    asyncio.run(exercise())


def test_broad_shell_session_approval_still_allows_changed_command(tmp_path: Path) -> None:
    async def exercise() -> None:
        prompts = []

        async def confirm(spec, context, arguments):
            prompts.append(arguments)
            return PermissionDecision.ALLOW_SESSION

        broker = workspace_tool_broker(
            permissions=PermissionEngine(PermissionMode.PROMPT, CommandPermissionStore()),
            confirmer=confirm,
        )
        context = ToolContext(session_id="one", workspace_root=str(tmp_path))
        for value in ("first", "second"):
            result = await broker.invoke("shell_command", context, {"argv": ["/bin/echo", value]})
            assert not result.is_error
        assert len(prompts) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "arguments",
    [
        {"argv": []},
        {"argv": [""]},
        {"argv": ["a\x00b"]},
        {"argv": ["echo"] * 129},
        {"argv": ["echo"], "cwd": "/"},
        {"argv": ["echo"], "cwd": ".."},
        {"argv": ["echo"], "cwd": "missing"},
        {"argv": ["echo"], "cwd": "file.txt"},
    ],
)
def test_invalid_commands_cannot_create_reusable_scope(tmp_path: Path, arguments: dict) -> None:
    (tmp_path / "file.txt").write_text("data")
    context = ToolContext(session_id="one", workspace_root=str(tmp_path))
    scope = PermissionScope("one", "shell_command", str(tmp_path))
    assert (
        _shell_command_scope(
            ShellCommandTool(), PermissionEffect.SUBPROCESS, scope, context, arguments
        )
        is None
    )


def test_cwd_symlinks_share_resolved_identity_but_cannot_escape(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    (tmp_path / "alias").symlink_to(child, target_is_directory=True)
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    context = ToolContext(session_id="one", workspace_root=str(tmp_path))
    scope = PermissionScope("one", "shell_command", str(tmp_path))

    def identify(cwd):
        return _shell_command_scope(
            ShellCommandTool(),
            PermissionEffect.SUBPROCESS,
            scope,
            context,
            {"argv": ["/bin/echo", "hello"], "cwd": cwd},
        )

    assert identify("child") == identify("alias")
    assert identify("child") is not None
    assert identify("escape") is None


def test_custom_shell_named_tool_cannot_offer_or_spoof_exact_approval(tmp_path: Path) -> None:
    class CustomShell:
        effect = "subprocess"

        def describe(self):
            return ToolSpec(name="shell_command", description="custom", input_schema={})

        async def invoke(self, context, arguments):
            return ToolResult(content="custom")

    async def exercise():
        captured = []

        async def confirm(spec, context, arguments):
            captured.append(arguments)
            return PermissionDecision.ALLOW_ONCE

        broker = ToolBroker(
            (CustomShell(),),
            permissions=PermissionEngine(PermissionMode.PROMPT, CommandPermissionStore()),
            confirmer=confirm,
        )
        result = await broker.invoke(
            "shell_command",
            ToolContext(session_id="one", workspace_root=str(tmp_path)),
            {"argv": ["/bin/echo"], "__permission_command_scope": True},
        )
        assert not result.is_error
        assert captured[0]["__permission_command_scope"] is False

    asyncio.run(exercise())


def test_cwd_change_during_approval_requires_retry_and_fresh_approval(tmp_path: Path) -> None:
    async def exercise() -> None:
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(first, target_is_directory=True)
        prompts = []

        async def confirm(spec, context, arguments):
            prompts.append(arguments)
            if len(prompts) == 1:
                alias.unlink()
                alias.symlink_to(second, target_is_directory=True)
            return PermissionDecision.ALLOW_COMMAND_SESSION

        broker = workspace_tool_broker(
            permissions=PermissionEngine(PermissionMode.PROMPT, CommandPermissionStore()),
            confirmer=confirm,
        )
        context = ToolContext(session_id="one", workspace_root=str(tmp_path))
        arguments = {"argv": ["/bin/pwd"], "cwd": "alias"}
        drifted = await broker.invoke("shell_command", context, arguments)
        assert drifted.is_error
        assert drifted.metadata["permission_scope_changed"] is True
        assert drifted.metadata["retry_required"] is True
        assert "exit_code" not in drifted.metadata
        assert "retry" in drifted.content
        retried = await broker.invoke("shell_command", context, arguments)
        assert not retried.is_error
        assert retried.content.strip() == str(second.resolve())
        assert len(prompts) == 2
        repeated = await broker.invoke("shell_command", context, arguments)
        assert not repeated.is_error
        assert len(prompts) == 2

    asyncio.run(exercise())
