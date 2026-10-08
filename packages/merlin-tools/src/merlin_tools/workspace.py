from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from merlin_contracts import ToolContext, ToolResult, ToolSpec
from merlin_permissions import PermissionEngine

from .broker import (
    DurableGrantResolver,
    PermissionScopeResolver,
    ToolBroker,
    ToolConfirmer,
    ToolEffectStore,
)

_DEFAULT_MAX_BYTES = 256 * 1024
_DEFAULT_SHELL_TIMEOUT = 30.0


def _workspace_path(context: ToolContext, value: object) -> Path:
    if not isinstance(value, str):
        raise TypeError("path must be a string")
    relative = Path(value or ".")
    if relative.is_absolute():
        raise ValueError("path must be relative to the workspace")
    root = Path(context.workspace_root).resolve(strict=True)
    target = (root / relative).resolve(strict=True)
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("path escapes the workspace") from error
    return target


def _relative_workspace_path(context: ToolContext, target: Path) -> str:
    return str(target.relative_to(Path(context.workspace_root).resolve()))


class ListDirectoryTool:
    effect = "workspace_read"

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="list_directory",
            description="List the immediate contents of a workspace directory.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
                "additionalProperties": False,
            },
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        target = _workspace_path(context, arguments.get("path", "."))
        if not target.is_dir():
            raise ValueError("path is not a directory")
        entries = sorted(target.iterdir(), key=lambda path: path.name.casefold())
        lines = [f"{entry.name}/" if entry.is_dir() else entry.name for entry in entries]
        return ToolResult(
            content="\n".join(lines),
            metadata={"path": _relative_workspace_path(context, target)},
        )


class ReadFileTool:
    effect = "workspace_read"

    def __init__(self, max_bytes: int = _DEFAULT_MAX_BYTES) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._max_bytes = max_bytes

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="read_file",
            description="Read a UTF-8 text file from the workspace.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        if "path" not in arguments:
            raise ValueError("path is required")
        target = _workspace_path(context, arguments["path"])
        if not target.is_file():
            raise ValueError("path is not a file")
        with target.open("rb") as handle:
            data = handle.read(self._max_bytes + 1)
        if len(data) > self._max_bytes:
            raise ValueError(f"file exceeds {self._max_bytes} byte read limit")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("file is not valid UTF-8 text") from error
        return ToolResult(
            content=content,
            metadata={
                "path": _relative_workspace_path(context, target),
                "bytes": len(data),
            },
        )


class ShellCommandTool:
    """Run an argv command inside the reviewed workspace with finite resources."""

    effect = "subprocess"

    def __init__(
        self,
        *,
        timeout_seconds: float = _DEFAULT_SHELL_TIMEOUT,
        max_output_bytes: int = _DEFAULT_MAX_BYTES,
        termination_grace_seconds: float = 1,
    ) -> None:
        if timeout_seconds <= 0 or max_output_bytes <= 0 or termination_grace_seconds <= 0:
            raise ValueError("shell limits must be positive")
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._termination_grace_seconds = termination_grace_seconds

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="shell_command",
            description="Run one argv command within the reviewed workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "cwd": {"type": "string", "default": "."},
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        argv = arguments.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > 128
            or any(not isinstance(item, str) or not item or "\x00" in item for item in argv)
        ):
            raise ValueError("argv must contain 1 to 128 non-empty strings")
        cwd = _workspace_path(context, arguments.get("cwd", "."))
        if not cwd.is_dir():
            raise ValueError("cwd is not a directory")
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=_safe_environment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout_seconds
            )
        except TimeoutError:
            timed_out = True
            await _terminate_process_group(process, self._termination_grace_seconds)
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            await _terminate_process_group(process, self._termination_grace_seconds)
            await process.communicate()
            raise
        combined_size = len(stdout) + len(stderr)
        truncated = combined_size > self._max_output_bytes
        stdout, stderr = _bounded_outputs(stdout, stderr, self._max_output_bytes)
        return ToolResult(
            content=stdout.decode("utf-8", errors="replace"),
            metadata={
                "argv": list(argv),
                "cwd": _relative_workspace_path(context, cwd),
                "exit_code": process.returncode,
                "stderr": stderr.decode("utf-8", errors="replace"),
                "timed_out": timed_out,
                "truncated": truncated,
                "observed_output_bytes": combined_size,
            },
        )


def _safe_environment() -> dict[str, str]:
    """Pass only non-secret process settings required by ordinary macOS commands."""
    allowed = {"LANG", "LC_ALL", "PATH", "SHELL", "TERM", "TMPDIR"}
    return {key: value for key, value in os.environ.items() if key in allowed}


async def _terminate_process_group(
    process: asyncio.subprocess.Process, grace_seconds: float
) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


def _bounded_outputs(stdout: bytes, stderr: bytes, limit: int) -> tuple[bytes, bytes]:
    stdout_size = min(len(stdout), limit)
    stdout = stdout[:stdout_size]
    stderr = stderr[: max(0, limit - stdout_size)]
    return stdout, stderr


def workspace_tool_broker(
    *,
    confirmer: ToolConfirmer | None = None,
    permissions: PermissionEngine | None = None,
    permission_scope: PermissionScopeResolver | None = None,
    effect_store: ToolEffectStore | None = None,
    durable_grant: DurableGrantResolver | None = None,
    read_max_bytes: int = _DEFAULT_MAX_BYTES,
    shell_timeout_seconds: float = _DEFAULT_SHELL_TIMEOUT,
    shell_max_output_bytes: int = _DEFAULT_MAX_BYTES,
    shell_termination_grace_seconds: float = 1,
) -> ToolBroker:
    return ToolBroker(
        (
            ListDirectoryTool(),
            ReadFileTool(read_max_bytes),
            ShellCommandTool(
                timeout_seconds=shell_timeout_seconds,
                max_output_bytes=shell_max_output_bytes,
                termination_grace_seconds=shell_termination_grace_seconds,
            ),
        ),
        confirmer=confirmer,
        permissions=permissions,
        permission_scope=permission_scope,
        effect_store=effect_store,
        durable_grant=durable_grant,
    )
