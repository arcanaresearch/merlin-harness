"""Conservative argv verification for automatic read-only subprocess approval.

Never infer safety from a command's name alone. Pin installed executables,
allow explicit option grammars, and bound filesystem operands to the workspace.
Unknown commands/options keep the ordinary subprocess permission flow.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from merlin_contracts import ToolContext

_SYSTEM = {
    "ls": {"/bin/ls", "/usr/bin/ls"},
    "cat": {"/bin/cat", "/usr/bin/cat"},
    "pwd": {"/bin/pwd", "/usr/bin/pwd"},
    "head": {"/usr/bin/head", "/bin/head"},
    "tail": {"/usr/bin/tail", "/bin/tail"},
    "wc": {"/usr/bin/wc", "/bin/wc"},
    "grep": {"/usr/bin/grep", "/bin/grep"},
    "git": {"/usr/bin/git", "/bin/git"},
    "rg": {"/usr/bin/rg"},
}
_FLAGS = {
    "ls": "1aAbBcCdDFGghiklmnopqRrSstTuUx",
    "cat": "benstuvAET",
    "pwd": "LP",
    "wc": "clmwL",
    "head": "qv",
    "tail": "qv",
    "grep": "nHhivFEwxcqls",
    "rg": "nHhivFswxcqlS",
}
_RG_FLAGS = {
    "--files",
    "--hidden",
    "--no-ignore",
    "--no-ignore-vcs",
    "--no-messages",
    "--count",
    "--count-matches",
    "--files-with-matches",
    "--files-without-match",
    "--fixed-strings",
    "--line-number",
    "--ignore-case",
    "--smart-case",
}


def _trusted_executable(value: str, root: Path) -> tuple[str, str] | None:
    if "/" in value and not Path(value).is_absolute():
        return None
    found = shutil.which(value)
    if found is None:
        return None
    executable = Path(found).resolve(strict=True)
    name = executable.name
    if executable.is_relative_to(root) or name not in _SYSTEM:
        return None
    trusted = str(executable) in _SYSTEM[name]
    if name in {"git", "rg"}:
        package = "git" if name == "git" else "ripgrep"
        for prefix in ("/opt/homebrew/Cellar", "/usr/local/Cellar"):
            base = Path(prefix) / package
            if executable.is_relative_to(base):
                relative = executable.relative_to(base).parts
                trusted |= len(relative) == 3 and relative[1:] == ("bin", name)
    return (name, str(executable)) if trusted else None


def _workspace_operand(value: str, cwd: Path, root: Path, *, directory: bool) -> bool:
    try:
        path = (cwd / value).resolve(strict=True)
        return path.is_relative_to(root) and (path.is_file() or (directory and path.is_dir()))
    except (OSError, RuntimeError, ValueError):
        return False


def _read_arguments(name: str, argv: list[str], cwd: Path, root: Path) -> bool:
    operands = []
    pattern = name in {"grep", "rg"}
    options = True
    index = 0
    while index < len(argv):
        value = argv[index]
        index += 1
        if options and value == "--":
            options = False
        elif options and name == "rg" and value == "--files":
            pattern = False
        elif options and name == "rg" and value in _RG_FLAGS:
            continue
        elif options and value in {"-n", "--lines", "-c", "--bytes"} and name in {"head", "tail"}:
            if index == len(argv) or not re.fullmatch(r"[0-9]{1,7}", argv[index]):
                return False
            index += 1
        elif options and value in {"-e", "--regexp", "-g", "--glob"} and name in {"grep", "rg"}:
            if index == len(argv) or (name == "grep" and value in {"-g", "--glob"}):
                return False
            if value in {"-e", "--regexp"}:
                pattern = False
            index += 1
        elif options and value.startswith("-"):
            if len(value) < 2 or value.startswith("--") or not set(value[1:]) <= set(_FLAGS[name]):
                return False
        elif pattern:
            pattern = False
        else:
            operands.append(value)
    if name == "pwd":
        return not operands
    if pattern:
        return False
    if name not in {"ls", "rg"} and not operands:
        return False
    return all(
        _workspace_operand(value, cwd, root, directory=name in {"ls", "rg"}) for value in operands
    )


async def _git_arguments(
    executable: str, argv: list[str], cwd: Path, root: Path
) -> list[str] | None:
    if not argv or argv[0] not in {"status", "log", "diff"}:
        return None
    command, *options = argv
    allowed = {
        "status": {
            "--short",
            "-s",
            "--branch",
            "-b",
            "--porcelain",
            "--porcelain=v1",
            "--porcelain=v2",
        },
        "log": {"--oneline", "--all", "--graph", "--decorate", "--no-decorate"},
        "diff": {
            "--stat",
            "--numstat",
            "--shortstat",
            "--name-only",
            "--name-status",
            "--cached",
            "--staged",
            "--check",
        },
    }[command]
    # A deliberately small grammar; -c, aliases, output files, arbitrary formats,
    # revision/path disambiguation, and external helpers require review.
    if any(option not in allowed for option in options):
        return None
    from .workspace import _safe_environment

    prefix = [executable, "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false"]
    process = await asyncio.create_subprocess_exec(
        *prefix,
        "config",
        "--get-regexp",
        r"^filter\..*\.(clean|process)$",
        cwd=cwd,
        env=_safe_environment(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        # Config inspection itself does not invoke filters. Any configured filter
        # makes worktree reads ambiguous, so keep the regular approval prompt.
        if await asyncio.wait_for(process.wait(), timeout=2) != 1:
            return None
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    suffix = (
        ["--ignore-submodules=all"]
        if command in {"status", "diff"}
        else ["--format=medium", "--no-show-signature"]
    )
    if command == "diff":
        suffix += ["--no-ext-diff", "--no-textconv"]
    repository = await asyncio.create_subprocess_exec(
        *prefix,
        "rev-parse",
        "--show-toplevel",
        "--absolute-git-dir",
        cwd=cwd,
        env=_safe_environment(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(repository.communicate(), timeout=2)
        paths = output.decode().splitlines()
        if repository.returncode != 0 or len(paths) != 2:
            return None
        for path in paths:
            if not await asyncio.to_thread(_workspace_operand, path, cwd, root, directory=True):
                return None
    except (TimeoutError, asyncio.CancelledError):
        if repository.returncode is None:
            repository.kill()
        await repository.wait()
        raise
    return prefix + [command, *suffix, *options]


def _verified_command(
    context: ToolContext, arguments: Mapping[str, Any]
) -> tuple[str, str, Path, Path] | None:
    argv = arguments.get("argv")
    if not isinstance(argv, list) or not 1 <= len(argv) <= 128:
        return None
    if any(
        not isinstance(value, str)
        or not value
        or any(c in value for c in "\x00\n\r;|&<>`")
        or "$(" in value
        for value in argv
    ):
        return None
    try:
        from .workspace import _workspace_path

        root = Path(context.workspace_root).resolve(strict=True)
        cwd = _workspace_path(context, arguments.get("cwd", "."))
        if not cwd.is_dir():
            return None
        trusted = _trusted_executable(argv[0], root)
        if trusted is None:
            return None
        name, executable = trusted
        if name != "git" and not _read_arguments(name, argv[1:], cwd, root):
            return None
        return name, executable, cwd, root
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


async def prepare_read_only_command(
    context: ToolContext, arguments: Mapping[str, Any]
) -> dict[str, Any] | None:
    verified = await asyncio.to_thread(_verified_command, context, arguments)
    if verified is None:
        return None
    name, executable, cwd, root = verified
    argv = arguments["argv"]
    try:
        if name == "git":
            prepared = await _git_arguments(executable, argv[1:], cwd, root)
            if prepared is None:
                return None
        else:
            prepared = [executable, *(["--no-config"] if name == "rg" else []), *argv[1:]]
        return dict(arguments) | {"argv": prepared}
    except (OSError, ValueError, TimeoutError):
        return None
