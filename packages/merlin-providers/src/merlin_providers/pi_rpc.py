"""Bounded Pi RPC transport with Merlin-owned tool authority."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from merlin_contracts import (
    ChatRequest,
    DynamicToolHandler,
    ModelCompleted,
    ModelError,
    ModelEvent,
    ProviderFailureKind,
    ReasoningDelta,
    TextDelta,
    ToolCall,
)

from .capabilities import RouteIdentity
from .dynamic_mcp import DynamicMcpBroker
from .kimi_acp import _request_prompt
from .openai_compatible import ProviderCapabilities


@dataclass(frozen=True, slots=True)
class PiRpcConfig:
    command: tuple[str, ...] = ("pi",)
    model: str | None = None
    state_directory: Path | None = None
    source_agent_directory: Path | None = None
    startup_timeout_seconds: float = 15.0
    idle_timeout_seconds: float = 60.0
    partial_idle_timeout_seconds: float = 60.0
    turn_timeout_seconds: float = 600.0
    shutdown_timeout_seconds: float = 2.0
    max_line_bytes: int = 1_048_576
    stderr_lines: int = 64
    reasoning_effort: str = "default"

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError("Pi RPC command must not be empty")
        if (
            min(
                self.startup_timeout_seconds,
                self.idle_timeout_seconds,
                self.partial_idle_timeout_seconds,
                self.turn_timeout_seconds,
                self.shutdown_timeout_seconds,
            )
            <= 0
        ):
            raise ValueError("Pi RPC timeouts must be positive")
        if self.max_line_bytes < 1_024 or self.stderr_lines < 1:
            raise ValueError("Pi RPC protocol bounds are too small")


class PiRpcProvider:
    """Run Pi headlessly while exposing only request-scoped Merlin MCP tools."""

    def __init__(self, config: PiRpcConfig) -> None:
        self.config = config
        self._sessions: dict[str, _PiSession] = {}
        self._session_lock = asyncio.Lock()
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=True, tools=True, usage_in_stream=False)

    @property
    def route_identity(self) -> RouteIdentity:
        return RouteIdentity(transport="pi_rpc", endpoint="", model=self.config.model or "")

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        if request.tools:
            yield ModelError(
                message="Pi RPC tools require Merlin's authority callback",
                code="pi_rpc_tools_unsupported",
                failure_kind=ProviderFailureKind.CONFIGURATION,
            )
            return
        async for event in self._stream(request, None):
            yield event

    async def stream_with_tools(
        self, request: ChatRequest, handler: DynamicToolHandler
    ) -> AsyncIterator[ModelEvent]:
        async for event in self._stream(request, handler):
            yield event

    async def _stream(
        self, request: ChatRequest, handler: DynamicToolHandler | None
    ) -> AsyncIterator[ModelEvent]:
        if request.reasoning == "disabled" and request.reasoning_control == "required":
            yield ModelError(
                message="Pi RPC does not support disabling reasoning for one request",
                code="unsupported_request_control",
            )
            return
        if request.reasoning_effort is not None:
            yield ModelError(
                message="Pi RPC reasoning effort is fixed when its process starts",
                code="unsupported_request_control",
            )
            return
        if request.tools and handler is None:
            yield ModelError(
                message="Pi RPC tools require Merlin's authority callback",
                code="pi_rpc_tools_unsupported",
                failure_kind=ProviderFailureKind.CONFIGURATION,
            )
            return
        key = request.budget_id or f"ephemeral:{id(request)}"
        try:
            session = await self._session(key)
            async for event in session.stream(request, handler):
                yield event
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as error:
            await self._discard(key)
            yield ModelError(
                message=str(error),
                code=_error_code(error),
                retryable=isinstance(error, (RuntimeError, TimeoutError, TypeError, ValueError)),
                failure_kind=(
                    ProviderFailureKind.TRANSIENT
                    if isinstance(error, (RuntimeError, TimeoutError))
                    else ProviderFailureKind.FORMATTING
                    if isinstance(error, (TypeError, ValueError))
                    else ProviderFailureKind.CONFIGURATION
                ),
            )
        finally:
            if request.budget_id is None:
                await self._discard(key)

    async def close(self) -> None:
        async with self._session_lock:
            sessions, self._sessions = tuple(self._sessions.values()), {}
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None

    async def _session(self, key: str) -> _PiSession:
        async with self._session_lock:
            session = self._sessions.get(key)
            if session is None:
                session = _PiSession(self.config, self._state_directory(), key)
                self._sessions[key] = session
            return session

    async def _discard(self, key: str) -> None:
        async with self._session_lock:
            session = self._sessions.pop(key, None)
        if session is not None:
            await session.close()

    def _state_directory(self) -> Path:
        if self.config.state_directory is not None:
            root = self.config.state_directory / "pi-rpc"
            root.mkdir(parents=True, exist_ok=True)
            return root
        if self._temporary_directory is None:
            self._temporary_directory = tempfile.TemporaryDirectory(prefix="merlin-pi-rpc-")
        return Path(self._temporary_directory.name)


class _PiSession:
    def __init__(self, config: PiRpcConfig, root: Path, key: str) -> None:
        self.config, self.root, self.key = config, root, key
        self.cwd = root / "workspace"
        self.agent_directory = root / "agent"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.agent_directory.mkdir(parents=True, exist_ok=True)
        self.process: asyncio.subprocess.Process | None = None
        self._history: tuple[str, ...] = ()
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._stderr: deque[str] = deque(maxlen=config.stderr_lines)
        self._stderr_task: asyncio.Task[None] | None = None
        self._broker: DynamicMcpBroker | None = None
        self._tool_signature: str | None = None

    async def stream(
        self, request: ChatRequest, handler: DynamicToolHandler | None = None
    ) -> AsyncIterator[ModelEvent]:
        async with self._lock:
            await self._configure_tools(request.tools if handler is not None else ())
            await self._ensure_started()
            prompt, history = _request_prompt(
                request.messages,
                self._history,
                tools=bool(request.tools and handler is not None),
            )
            prompt_id = await self._send({"type": "prompt", "message": prompt})
            emitted = False
            emitted_chars = 0
            output_char_bound = (
                request.max_output_tokens * 4 if request.max_output_tokens is not None else None
            )
            prompt_accepted = False
            message_task: asyncio.Task[Mapping[str, Any]] | None = None
            broker_task: asyncio.Task[ToolCall] | None = None
            try:
                async with asyncio.timeout(self.config.turn_timeout_seconds):
                    while True:
                        if message_task is None:
                            message_task = asyncio.create_task(
                                self._read_message(
                                    self.config.partial_idle_timeout_seconds
                                    if emitted
                                    else self.config.idle_timeout_seconds
                                )
                            )
                        if self._broker is not None and broker_task is None:
                            broker_task = asyncio.create_task(self._broker.next_event())
                        waiting = {message_task}
                        if broker_task is not None:
                            waiting.add(broker_task)
                        done, _ = await asyncio.wait(
                            waiting,
                            timeout=(
                                self.config.partial_idle_timeout_seconds
                                if emitted
                                else self.config.idle_timeout_seconds
                            ),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if not done:
                            raise TimeoutError("Pi RPC response timed out")
                        if broker_task is not None and broker_task in done:
                            call = broker_task.result()
                            broker_task = None
                            emitted = True
                            yield call
                            continue
                        message = message_task.result()
                        message_task = None
                        if message.get("type") == "response" and message.get("id") == prompt_id:
                            if not message.get("success"):
                                raise RuntimeError(
                                    str(message.get("error") or "Pi RPC rejected the prompt")
                                )
                            prompt_accepted = True
                            continue
                        event_type = message.get("type")
                        if event_type == "tool_execution_start":
                            tool_name = message.get("toolName")
                            if not (
                                self._broker is not None
                                and isinstance(tool_name, str)
                                and tool_name.startswith("mcp__merlin__")
                            ):
                                await self._abort()
                                yield ModelError(
                                    message="Pi RPC attempted a tool outside Merlin's broker",
                                    code="pi_rpc_forbidden_tool",
                                    partial=emitted,
                                    failure_kind=ProviderFailureKind.PERMISSION,
                                )
                                return
                            continue
                        if event_type == "message_update":
                            update = _mapping(message.get("assistantMessageEvent"))
                            delta = update.get("delta")
                            if update.get("type") == "thinking_delta" and isinstance(delta, str):
                                emitted = True
                                yield ReasoningDelta(text=delta)
                            elif update.get("type") == "text_delta" and isinstance(delta, str):
                                if output_char_bound is not None:
                                    delta = delta[: max(0, output_char_bound - emitted_chars)]
                                if delta:
                                    emitted = True
                                    emitted_chars += len(delta)
                                    yield TextDelta(text=delta)
                                if (
                                    output_char_bound is not None
                                    and emitted_chars >= output_char_bound
                                ):
                                    await self._abort()
                                    yield ModelCompleted(
                                        finish_reason="output_bound", disposition="truncated"
                                    )
                                    return
                            elif update.get("type") == "error":
                                error = update.get("error")
                                raise RuntimeError(
                                    str(error if isinstance(error, str) else "Pi provider failed")
                                )
                            continue
                        if event_type == "agent_settled":
                            if not prompt_accepted:
                                raise RuntimeError("Pi RPC settled before accepting the prompt")
                            if message.get("aborted"):
                                raise RuntimeError("Pi RPC aborted the provider turn")
                            self._history = history
                            yield ModelCompleted(finish_reason="end_turn")
                            return
            except asyncio.CancelledError:
                await self._abort()
                raise
            finally:
                for task in (message_task, broker_task):
                    if task is not None:
                        task.cancel()
                await asyncio.gather(
                    *(task for task in (message_task, broker_task) if task is not None),
                    return_exceptions=True,
                )

    async def close(self) -> None:
        await self._stop_process()
        if self._broker is not None:
            await self._broker.close()
            self._broker = None

    async def _configure_tools(self, tools: Sequence[Mapping[str, object]]) -> None:
        signature = json.dumps(list(tools), sort_keys=True, separators=(",", ":")) if tools else ""
        if signature == self._tool_signature:
            return
        await self._stop_process()
        if self._broker is not None:
            await self._broker.close()
            self._broker = None
        self._tool_signature = signature
        self._history = ()
        mcp_path = self.agent_directory / "mcp.json"
        if not tools:
            mcp_path.unlink(missing_ok=True)
            return
        self._broker = DynamicMcpBroker(tools, max_line_bytes=self.config.max_line_bytes)
        await self._broker.start()
        server = self._broker.stdio_server()
        mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "merlin": {
                            "command": server["command"],
                            "args": server["args"],
                            "env": server["env"],
                            "exposure": "direct",
                        }
                    }
                },
                separators=(",", ":"),
            )
        )
        os.chmod(mcp_path, 0o600)

    async def _ensure_started(self) -> None:
        if self.process is not None:
            return
        self._link_provider_state()
        command = [
            *self.config.command,
            "--mode",
            "rpc",
            "--no-session",
            "--no-builtin-tools",
            "--no-context-files",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-extensions",
        ]
        if self._broker is not None:
            command.extend(("--extension", "builtin:mcp", "--tools", "mcp__merlin__*"))
        else:
            command.append("--no-tools")
        model = self.config.model
        if model:
            command.extend(("--model", model))
        if self.config.reasoning_effort != "default":
            command.extend(("--thinking", self.config.reasoning_effort))
        environment = dict(os.environ)
        environment.update(
            {
                "PI_CODING_AGENT_DIR": str(self.agent_directory),
                "PI_SKIP_VERSION_CHECK": "1",
                "PI_TELEMETRY": "0",
            }
        )
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=environment,
            limit=self.config.max_line_bytes + 1,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    def _link_provider_state(self) -> None:
        source = self.config.source_agent_directory
        if source is None:
            configured = os.environ.get("PI_CODING_AGENT_DIR")
            source = Path(configured).expanduser() if configured else Path.home() / ".pi" / "agent"
        for name in ("auth.json", "models.json"):
            destination = self.agent_directory / name
            source_file = source / name
            if destination.exists() or destination.is_symlink() or not source_file.is_file():
                continue
            destination.symlink_to(source_file)

    async def _stop_process(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
            await asyncio.gather(process.stdin.wait_closed(), return_exceptions=True)
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), self.config.shutdown_timeout_seconds)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None

    async def _send(self, payload: Mapping[str, Any]) -> str:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Pi RPC is not running")
        self._request_id += 1
        request_id = f"merlin-{self._request_id}"
        encoded = json.dumps({"id": request_id, **payload}, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > self.config.max_line_bytes:
            raise ValueError("Pi RPC request exceeds the protocol line bound")
        self.process.stdin.write(encoded)
        await self.process.stdin.drain()
        return request_id

    async def _abort(self) -> None:
        try:
            await self._send({"type": "abort"})
        except (OSError, RuntimeError, ValueError):
            pass

    async def _read_message(self, timeout: float) -> Mapping[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("Pi RPC is not running")
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
        except ValueError as error:
            raise ValueError("Pi RPC response exceeds the protocol line bound") from error
        if not line:
            raise RuntimeError(
                "Pi RPC closed its output "
                f"(exit={self.process.returncode}, diagnostic_lines={len(self._stderr)})"
            )
        if len(line) > self.config.max_line_bytes:
            raise ValueError("Pi RPC response exceeds the protocol line bound")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError("Pi RPC emitted invalid JSON") from error
        if not isinstance(message, Mapping):
            raise TypeError("Pi RPC emitted a non-object protocol message")
        return message

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while line := await self.process.stderr.readline():
            self._stderr.append(line.decode(errors="replace").rstrip()[:1_024])


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _error_code(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "pi_rpc_timeout"
    if isinstance(error, (TypeError, ValueError)):
        return "pi_rpc_protocol_error"
    if isinstance(error, OSError):
        return "pi_rpc_launch_error"
    return "pi_rpc_error"
