import asyncio
import os
import shutil
import subprocess

import pytest
from merlin_contracts import ToolContext, ToolSpec
from merlin_permissions import (
    PermissionDecision,
    PermissionEffect,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    PermissionRule,
    PermissionScope,
)
from merlin_tools import ToolBroker, workspace_tool_broker
from merlin_tools.read_only import _read_arguments


async def git_run(*argv):
    await asyncio.to_thread(subprocess.run, list(argv), check=True)


class Store:
    def __init__(self):
        self.audits = []
        self.rules = []

    async def find_rule(self, request):
        return next(
            (
                rule
                for rule in self.rules
                if (
                    rule.tool_name == request.scope.rule_key
                    and rule.scope_fingerprint == request.scope.fingerprint
                    and rule.effect == request.effect
                )
            ),
            None,
        )

    async def save_rule(self, rule):
        self.rules.append(rule)

    async def record_audit(self, audit):
        self.audits.append(audit)


@pytest.fixture
def harness(tmp_path):
    (tmp_path / "sample.txt").write_text("hello\nworld\n")
    store = Store()
    prompts = []

    async def deny(spec, context, arguments):
        prompts.append(arguments)
        return PermissionDecision.DENY_ONCE

    engine = PermissionEngine(PermissionMode.AUTO, store)
    broker = workspace_tool_broker(permissions=engine, confirmer=deny)
    return broker, ToolContext(session_id="test", workspace_root=str(tmp_path)), store, prompts


@pytest.mark.parametrize(
    "argv",
    [
        ["ls", "-la"],
        ["pwd"],
        ["cat", "sample.txt"],
        ["head", "-n", "1", "sample.txt"],
        ["tail", "-n", "1", "sample.txt"],
        ["wc", "-l", "sample.txt"],
        ["grep", "-n", "hello", "sample.txt"],
    ],
)
async def test_auto_executes_verified_reads_and_audits_subprocess(harness, argv):
    broker, context, store, prompts = harness
    result = await broker.invoke("shell_command", context, {"argv": argv})
    assert not result.is_error
    assert result.metadata["exit_code"] == 0
    assert not prompts
    assert store.audits[-1].request.effect is PermissionEffect.SUBPROCESS
    assert store.audits[-1].reason == "auto mode verified read-only command"
    assert os.path.isabs(result.metadata["argv"][0])


@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "sample.txt"],
        ["touch", "new.txt"],
        ["mkdir", "new"],
        ["sh", "-c", "cat sample.txt"],
        ["python3", "-c", "print('hello')"],
        ["curl", "http://localhost/"],
        ["find", ".", "-exec", "rm", "{}", ";"],
        ["sed", "-i", "s/hello/bye/", "sample.txt"],
        ["git", "clean", "-fd"],
        ["git", "-c", "alias.read=!touch new.txt", "read"],
        ["git", "diff", "--output=new.txt"],
        ["git", "log", "--format=%G?"],
        ["git", "status", "--untracked-files=all"],
        ["ls", ";", "rm", "sample.txt"],
        ["cat", "sample.txt", ">", "new.txt"],
        ["cat", "$(touch new.txt)"],
        ["cat", "/etc/passwd"],
        ["cat", "../outside"],
        ["tail", "-f", "sample.txt"],
        ["grep", "-f", "/etc/passwd", "sample.txt"],
    ],
)
async def test_mutation_wrappers_unknown_options_and_outside_reads_prompt(harness, argv):
    broker, context, store, prompts = harness
    result = await broker.invoke("shell_command", context, {"argv": argv})
    assert result.is_error
    assert result.metadata["permission_denied"]
    assert len(prompts) == 1
    assert not store.audits[-1].request.read_only_command


async def test_symlink_outside_workspace_is_not_auto_approved(harness, tmp_path):
    broker, context, _, prompts = harness
    (tmp_path / "escape").symlink_to("/etc/passwd")
    result = await broker.invoke("shell_command", context, {"argv": ["cat", "escape"]})
    assert result.is_error
    assert len(prompts) == 1


async def test_path_shadowed_executable_is_not_auto_approved(harness, tmp_path, monkeypatch):
    broker, context, _, prompts = harness
    binary = tmp_path / "ls"
    binary.write_text("#!/bin/sh\ntouch marker\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    assert (await broker.invoke("shell_command", context, {"argv": ["ls"]})).is_error
    assert len(prompts) == 1
    assert not (tmp_path / "marker").exists()


@pytest.mark.parametrize("exact", [False, True])
async def test_prompt_mode_and_existing_session_denial_still_apply(harness, exact):
    broker, context, store, prompts = harness
    broker._permissions.mode = PermissionMode.PROMPT
    await broker.invoke("shell_command", context, {"argv": ["ls"]})
    assert len(prompts) == 1
    request = store.audits[-1].request
    scope = request.command_scope if exact else request.scope
    store.rules.append(
        PermissionRule(
            scope.session_id,
            scope.rule_key,
            PermissionEffect.SUBPROCESS,
            scope.fingerprint,
            PermissionDecision.DENY_SESSION,
        )
    )
    broker._permissions.mode = PermissionMode.AUTO
    assert (await broker.invoke("shell_command", context, {"argv": ["ls"]})).is_error
    assert len(prompts) == 1


async def test_custom_tool_cannot_claim_read_only_subprocess(harness):
    _, context, store, prompts = harness

    class Spoof:
        effect = "subprocess"

        def describe(self):
            return ToolSpec(
                name="shell_command", description="spoof", input_schema={"type": "object"}
            )

        async def invoke(self, context, arguments):
            pytest.fail("custom tool bypassed permission")

    async def deny(*args):
        prompts.append(args)
        return False

    broker = ToolBroker(
        [Spoof()], permissions=PermissionEngine(PermissionMode.AUTO, store), confirmer=deny
    )
    result = await broker.invoke(
        "shell_command", context, {"argv": ["ls"], "read_only_command": True}
    )
    assert result.is_error and prompts


@pytest.mark.parametrize("command", ["status", "diff", "log"])
async def test_git_reads_are_hardened_and_do_not_write_index(harness, tmp_path, command):
    broker, context, store, prompts = harness
    git = shutil.which("git")
    await git_run(git, "init", "-q", str(tmp_path))
    await git_run(git, "-C", str(tmp_path), "add", "sample.txt")
    await git_run(
        git,
        "-C",
        str(tmp_path),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-qm",
        "fixture",
    )
    hook = tmp_path / "monitor"
    hook.write_text("#!/bin/sh\ntouch marker\n")
    hook.chmod(0o755)
    await git_run(git, "-C", str(tmp_path), "config", "core.fsmonitor", str(hook))
    await git_run(git, "-C", str(tmp_path), "config", "diff.external", str(hook))
    await git_run(git, "-C", str(tmp_path), "config", "gpg.program", str(hook))
    await git_run(git, "-C", str(tmp_path), "config", "log.showSignature", "true")
    before = (tmp_path / ".git/index").read_bytes()
    (tmp_path / "sample.txt").write_text("changed\n")
    result = await broker.invoke("shell_command", context, {"argv": [git, command]})
    assert not result.is_error and not prompts
    assert result.metadata["exit_code"] == 0
    assert not (tmp_path / "marker").exists()
    assert (tmp_path / ".git/index").read_bytes() == before
    assert "--no-optional-locks" in result.metadata["argv"]


async def test_git_clean_filter_keeps_prompt_without_running_filter(harness, tmp_path):
    broker, context, _, prompts = harness
    git = shutil.which("git")
    await git_run(git, "init", "-q", str(tmp_path))
    await git_run(git, "-C", str(tmp_path), "config", "filter.evil.clean", "touch marker")
    (tmp_path / ".gitattributes").write_text("*.txt filter=evil\n")
    result = await broker.invoke("shell_command", context, {"argv": [git, "status"]})
    assert result.is_error and len(prompts) == 1
    assert not (tmp_path / "marker").exists()


async def test_read_flag_without_exact_command_scope_does_not_bypass(harness):
    broker, _, store, _ = harness
    scope = PermissionScope("test", "shell_command", "/tmp")
    assert not await broker._permissions.authorize(
        PermissionRequest(
            PermissionEffect.SUBPROCESS,
            scope,
            read_only_command=True,
        )
    )


@pytest.mark.parametrize(
    ("argv", "allowed"),
    [
        (["--files", "."], True),
        (["-n", "hello", "."], True),
        (["--hidden", "-g", "*.txt", "-e", "hello", "."], True),
        (["--pre", "sh", "hello", "."], False),
        (["--hostname-bin", "evil", "hello", "."], False),
        (["--follow", "hello", "."], False),
        (["-L", "hello", "."], False),
        (["hello", "/etc"], False),
    ],
)
def test_ripgrep_option_grammar_and_workspace_bounds(tmp_path, argv, allowed):
    assert _read_arguments("rg", argv, tmp_path, tmp_path) is allowed


async def test_git_repository_outside_workspace_keeps_prompt(harness, tmp_path):
    broker, context, _, prompts = harness
    git = shutil.which("git")
    outside = tmp_path.parent / "outside-repository"
    await git_run(git, "init", "-q", str(outside))
    (tmp_path / ".git").symlink_to(outside / ".git", target_is_directory=True)
    result = await broker.invoke("shell_command", context, {"argv": [git, "status"]})
    assert result.is_error and len(prompts) == 1


async def test_binary_is_pinned_if_path_changes_during_audit(harness, tmp_path, monkeypatch):
    broker, context, store, prompts = harness
    shadow = tmp_path / "bin"
    shadow.mkdir()
    binary = shadow / "ls"
    binary.write_text("#!/bin/sh\ntouch marker\n")
    binary.chmod(0o755)
    record = store.record_audit

    async def change_path(audit):
        monkeypatch.setenv("PATH", str(shadow) + os.pathsep + os.environ["PATH"])
        await record(audit)

    store.record_audit = change_path
    result = await broker.invoke("shell_command", context, {"argv": ["ls"]})
    assert not result.is_error and not prompts
    assert not (tmp_path / "marker").exists()


async def test_automatic_read_never_executes_if_audit_write_fails(harness, monkeypatch):
    from merlin_tools import ShellCommandTool

    broker, context, store, _ = harness

    async def fail_audit(audit):
        raise OSError("audit unavailable")

    async def unexpected_execution(*args):
        pytest.fail("read executed without its audit")

    store.record_audit = fail_audit
    monkeypatch.setattr(ShellCommandTool, "invoke", unexpected_execution)
    with pytest.raises(OSError, match="audit unavailable"):
        await broker.invoke("shell_command", context, {"argv": ["ls"]})
