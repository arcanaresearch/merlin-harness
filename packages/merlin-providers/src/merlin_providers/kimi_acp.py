"""Bounded ACP v1 transport for external coding-agent runtimes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from merlin_contracts import (
    ChatMessage,
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
from .openai_compatible import ProviderCapabilities, _classify_payload_failure


class _ProviderResponseError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        failure_kind: ProviderFailureKind,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.failure_kind = failure_kind
        self.retryable = retryable


class _PermissionDeniedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KimiAcpConfig:
    command: tuple[str, ...] = ("kimi", "acp")
    model: str | None = None
    state_directory: Path | None = None
    startup_timeout_seconds: float = 15.0
    idle_timeout_seconds: float = 60.0
    partial_idle_timeout_seconds: float = 60.0
    turn_timeout_seconds: float = 600.0
    shutdown_timeout_seconds: float = 2.0
    max_line_bytes: int = 1_048_576
    stderr_lines: int = 64
    reasoning_effort: str = "default"
    transport: str = "kimi_acp"
    label: str = "Kimi ACP"
    state_subdirectory: str = "kimi-acp"
    mode_id: str | None = "plan"
    supports_reasoning_effort: bool = False
    configure_model_in_session: bool = True

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError(f"{self.label} command must not be empty")
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
            raise ValueError(f"{self.label} timeouts must be positive")
        if self.max_line_bytes < 1_024 or self.stderr_lines < 1:
            raise ValueError(f"{self.label} protocol bounds are too small")
        if not self.transport or not self.state_subdirectory:
            raise ValueError("ACP transport and state subdirectory must not be empty")
        if not self.supports_reasoning_effort and self.reasoning_effort != "default":
            raise ValueError(
                f"{self.label} does not expose a categorical reasoning-effort option; "
                "use provider default"
            )


class KimiAcpProvider:
    """Uses ACP sessions with only Merlin-brokered MCP tool authority."""

    def __init__(self, config: KimiAcpConfig) -> None:
        self.config = config
        self._sessions: dict[str, _KimiSession] = {}
        self._session_lock = asyncio.Lock()
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=True, tools=True, usage_in_stream=False)

    @property
    def route_identity(self) -> RouteIdentity:
        """Exact transport + endpoint + model identity for capability resolution."""
        return RouteIdentity(
            transport=self.config.transport,
            endpoint="",
            model=self.config.model or "",
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        if (
            request.reasoning == "disabled"
            and request.reasoning_control == "required"
            or request.reasoning_effort is not None
        ):
            yield ModelError(
                message=f"{self.config.label} does not support request reasoning controls",
                code="unsupported_request_control",
            )
            return
        if request.tools:
            yield ModelError(
                message=f"{self.config.label} tools require Merlin's authority callback",
                code=f"{self.config.transport}_tools_unsupported",
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
        if (
            request.reasoning == "disabled"
            and request.reasoning_control == "required"
            or request.reasoning_effort is not None
            and not self.config.supports_reasoning_effort
        ):
            yield ModelError(
                message=f"{self.config.label} does not support request reasoning controls",
                code="unsupported_request_control",
            )
            return
        if request.tools and handler is None:
            yield ModelError(
                message=f"{self.config.label} tools require Merlin's authority callback",
                code=f"{self.config.transport}_tools_unsupported",
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
            structured = error if isinstance(error, _ProviderResponseError) else None
            permission_denied = isinstance(error, _PermissionDeniedError)
            yield ModelError(
                message=str(error),
                code=(
                    structured.code
                    if structured is not None
                    else f"{self.config.transport}_forbidden_authority"
                    if permission_denied
                    else _error_code(error, self.config.transport)
                ),
                retryable=(
                    structured.retryable
                    if structured is not None
                    else not permission_denied
                    and isinstance(error, (RuntimeError, TimeoutError, TypeError, ValueError))
                ),
                failure_kind=(
                    structured.failure_kind
                    if structured is not None
                    else ProviderFailureKind.PERMISSION
                    if permission_denied
                    else ProviderFailureKind.TRANSIENT
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

    async def _session(self, key: str) -> _KimiSession:
        async with self._session_lock:
            session = self._sessions.get(key)
            if session is None:
                root = self._state_directory()
                session = _KimiSession(self.config, root, key)
                self._sessions[key] = session
            return session

    async def _discard(self, key: str) -> None:
        async with self._session_lock:
            session = self._sessions.pop(key, None)
        if session is not None:
            await session.close()

    def _state_directory(self) -> Path:
        if self.config.state_directory is not None:
            root = self.config.state_directory / self.config.state_subdirectory
            root.mkdir(parents=True, exist_ok=True)
            return root
        if self._temporary_directory is None:
            self._temporary_directory = tempfile.TemporaryDirectory(
                prefix=f"merlin-{self.config.transport}-"
            )
        return Path(self._temporary_directory.name)


class _KimiSession:
    def __init__(self, config: KimiAcpConfig, root: Path, key: str) -> None:
        self.config, self.root, self.key = config, root, key
        self.cwd = root / "workspace"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.process: asyncio.subprocess.Process | None = None
        self.session_id: str | None = None
        self._history: tuple[str, ...] = ()
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._stderr: deque[str] = deque(maxlen=config.stderr_lines)
        self._stderr_task: asyncio.Task[None] | None = None
        self._record_path = root / "sessions" / hashlib.sha256(key.encode()).hexdigest()
        self._broker: DynamicMcpBroker | None = None

    async def stream(
        self, request: ChatRequest, handler: DynamicToolHandler | None = None
    ) -> AsyncIterator[ModelEvent]:
        async with self._lock:
            if request.tools and handler is not None:
                if self._broker is not None:
                    await self._broker.close()
                self._broker = DynamicMcpBroker(
                    request.tools, max_line_bytes=self.config.max_line_bytes
                )
                await self._broker.start()
                if self.session_id is not None:
                    await self._reset_session()
            elif self._broker is not None:
                await self._broker.close()
                self._broker = None
                if self.session_id is not None:
                    await self._reset_session()
            await self._ensure_started(request)
            assert self.session_id is not None
            prompt, history = _request_prompt(
                request.messages,
                self._history,
                tools=bool(request.tools and handler is not None),
            )
            prompt_id = await self._send_request(
                "session/prompt",
                {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                },
            )
            emitted = False
            emitted_chars = 0
            output_char_bound = (
                request.max_output_tokens * 4 if request.max_output_tokens is not None else None
            )
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
                            raise TimeoutError
                        if broker_task is not None and broker_task in done:
                            call = broker_task.result()
                            broker_task = None
                            emitted = True
                            yield call
                            continue
                        message = message_task.result()
                        message_task = None
                        if "id" in message and "method" in message:
                            await self._deny_request(message)
                            await self._cancel()
                            yield ModelError(
                                message=(
                                    f"{self.config.label} requested forbidden client authority: "
                                    f"{message['method']}"
                                ),
                                code=f"{self.config.transport}_forbidden_authority",
                                partial=emitted,
                                failure_kind=ProviderFailureKind.PERMISSION,
                            )
                            return
                        if message.get("id") == prompt_id:
                            if "error" in message:
                                error = _mapping(message.get("error"))
                                code, failure_kind, retryable = _classify_payload_failure(error)
                                yield ModelError(
                                    message=str(
                                        error.get("message") or f"{self.config.label} prompt failed"
                                    ),
                                    code=(
                                        f"{self.config.transport}_prompt_error"
                                        if code == "provider_error"
                                        else code
                                    ),
                                    retryable=retryable,
                                    partial=emitted,
                                    failure_kind=failure_kind,
                                )
                            else:
                                result = _mapping(message.get("result"))
                                self._history = history
                                self._persist()
                                yield ModelCompleted(
                                    finish_reason=str(result.get("stopReason") or "end_turn")
                                )
                            return
                        if message.get("method") != "session/update":
                            continue
                        params = _mapping(message.get("params"))
                        if params.get("sessionId") != self.session_id:
                            continue
                        update = _mapping(params.get("update"))
                        update_type = update.get("sessionUpdate")
                        if update_type in {"tool_call", "tool_call_update"}:
                            if self._broker is not None and _reviewed_tool_update(
                                update, self._broker.tool_names
                            ):
                                # The private broker emits the authoritative ToolCall. The ACP
                                # update is presentation-only and must not create a duplicate.
                                continue
                            await self._cancel()
                            yield ModelError(
                                message=f"{self.config.label} attempted a forbidden tool call",
                                code=f"{self.config.transport}_forbidden_tool",
                                partial=emitted,
                                failure_kind=ProviderFailureKind.PERMISSION,
                            )
                            return
                        content = _mapping(update.get("content"))
                        text = content.get("text")
                        if not isinstance(text, str) or not text:
                            continue
                        if update_type == "agent_thought_chunk":
                            emitted = True
                            yield ReasoningDelta(text=text)
                        elif update_type == "agent_message_chunk":
                            if output_char_bound is not None:
                                text = text[: max(0, output_char_bound - emitted_chars)]
                            if not text:
                                continue
                            emitted = True
                            emitted_chars += len(text)
                            yield TextDelta(text=text)
                            if output_char_bound is not None and emitted_chars >= output_char_bound:
                                await self._cancel()
                                yield ModelCompleted(
                                    finish_reason="output_bound", disposition="truncated"
                                )
                                return
            except asyncio.CancelledError:
                await self._cancel()
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
        process, self.process = self.process, None
        if self._broker is not None:
            await self._broker.close()
            self._broker = None
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

    async def _reset_session(self) -> None:
        if self.session_id is not None:
            try:
                request_id = await self._send_request(
                    "session/close", {"sessionId": self.session_id}
                )
                await self._response(request_id, self.config.startup_timeout_seconds)
            except (OSError, RuntimeError, TimeoutError, TypeError, ValueError):
                pass
        self.session_id = None
        self._history = ()
        self._record_path.unlink(missing_ok=True)

    async def _ensure_started(self, request: ChatRequest) -> None:
        if self.process is None:
            self.process = await asyncio.create_subprocess_exec(
                *self.config.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                limit=self.config.max_line_bytes + 1,
            )
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            request_id = await self._send_request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                        "auth": {"terminal": False},
                    },
                    "clientInfo": {"name": "merlin", "version": "0.1.0"},
                },
            )
            await self._response(request_id, self.config.startup_timeout_seconds)
        if self.session_id is None:
            mcp_servers = [self._broker.stdio_server()] if self._broker is not None else []
            record = self._load_record()
            if record is not None:
                request_id = await self._send_request(
                    "session/load",
                    {
                        "sessionId": record[0],
                        "cwd": str(self.cwd),
                        "mcpServers": mcp_servers,
                    },
                )
                await self._response(request_id, self.config.startup_timeout_seconds)
                self.session_id, self._history = record
            else:
                request_id = await self._send_request(
                    "session/new", {"cwd": str(self.cwd), "mcpServers": mcp_servers}
                )
                result = await self._response(request_id, self.config.startup_timeout_seconds)
                self.session_id = str(result.get("sessionId") or "") or None
                if self.session_id is None:
                    raise RuntimeError(f"{self.config.label} did not return a session id")
            if self.config.mode_id is not None:
                request_id = await self._send_request(
                    "session/set_mode",
                    {"sessionId": self.session_id, "modeId": self.config.mode_id},
                )
                await self._response(request_id, self.config.startup_timeout_seconds)
            model = request.model or self.config.model
            if model and self.config.configure_model_in_session:
                request_id = await self._send_request(
                    "session/set_config_option",
                    {"sessionId": self.session_id, "configId": "model", "value": model},
                )
                await self._response(request_id, self.config.startup_timeout_seconds)

    async def _response(self, request_id: int, timeout: float) -> Mapping[str, Any]:
        while True:
            message = await self._read_message(timeout)
            if "id" in message and "method" in message:
                await self._deny_request(message)
                raise _PermissionDeniedError(
                    f"{self.config.label} requested forbidden authority: {message['method']}"
                )
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = _mapping(message.get("error"))
                code, failure_kind, retryable = _classify_payload_failure(error)
                raise _ProviderResponseError(
                    str(error.get("message") or f"{self.config.label} request failed"),
                    code=(
                        f"{self.config.transport}_request_error"
                        if code == "provider_error"
                        else code
                    ),
                    failure_kind=failure_kind,
                    retryable=retryable,
                )
            return _mapping(message.get("result"))

    async def _send_request(self, method: str, params: Mapping[str, Any]) -> int:
        self._request_id += 1
        await self._write(
            {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": dict(params)}
        )
        return self._request_id

    async def _deny_request(self, message: Mapping[str, Any]) -> None:
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "Merlin denies external authority"},
            }
        )

    async def _cancel(self) -> None:
        if self.session_id is not None:
            try:
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/cancel",
                        "params": {"sessionId": self.session_id},
                    }
                )
            except (OSError, RuntimeError, ValueError):
                pass

    async def _write(self, message: Mapping[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError(f"{self.config.label} is not running")
        encoded = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > self.config.max_line_bytes:
            raise ValueError(f"{self.config.label} request exceeds the protocol line bound")
        self.process.stdin.write(encoded)
        await self.process.stdin.drain()

    async def _read_message(self, timeout: float) -> Mapping[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError(f"{self.config.label} is not running")
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
        except ValueError as error:
            raise ValueError(
                f"{self.config.label} response exceeds the protocol line bound"
            ) from error
        if not line:
            detail = "; ".join(self._stderr) or f"exit={self.process.returncode}"
            raise RuntimeError(f"{self.config.label} closed its output ({detail})")
        if len(line) > self.config.max_line_bytes:
            raise ValueError(f"{self.config.label} response exceeds the protocol line bound")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{self.config.label} emitted invalid JSON") from error
        if not isinstance(message, Mapping):
            raise TypeError(f"{self.config.label} emitted a non-object protocol message")
        return message

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while line := await self.process.stderr.readline():
            self._stderr.append(line.decode(errors="replace").rstrip()[:1_024])

    def _persist(self) -> None:
        if self.session_id is None:
            return
        self._record_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(dir=self._record_path.parent, prefix=".session-")
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump({"session_id": self.session_id, "history": self._history}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._record_path)
            temporary = ""
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    def _load_record(self) -> tuple[str, tuple[str, ...]] | None:
        try:
            value = json.loads(self._record_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if not isinstance(value, Mapping) or not isinstance(value.get("session_id"), str):
            return None
        history = value.get("history")
        if not isinstance(history, list) or not all(isinstance(item, str) for item in history):
            return None
        return value["session_id"], tuple(history)


def _request_prompt(
    messages: Sequence[ChatMessage], previous: tuple[str, ...], *, tools: bool = False
) -> tuple[str, tuple[str, ...]]:
    serialized = tuple(message.model_dump_json() for message in messages)
    start = len(previous) if serialized[: len(previous)] == previous else 0
    selected = messages[start:] or messages[-1:]
    prompt = "\n\n".join(_format_message(message, tools=tools) for message in selected)
    return prompt or "Continue.", serialized


def _format_message(message: ChatMessage, *, tools: bool = False) -> str:
    label = message.role.upper()
    if message.name:
        label += f" ({message.name})"
    authority = (
        "Use only tools from the `merlin` MCP server. After a tool returns an acceptance "
        "receipt, end the turn immediately; Merlin supplies the authority-checked result in "
        "the next step. Do not use files, shell, browser, skills, plugins, or agents."
        if tools
        else "Do not use tools, files, shell, browser, skills, plugins, or agents."
    )
    return f"[SYSTEM: {authority}]\n[{label}]\n{message.content}"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _reviewed_tool_update(update: Mapping[str, Any], names: frozenset[str]) -> bool:
    raw_input = _mapping(update.get("rawInput"))
    candidates = (update.get("name"), raw_input.get("name"), update.get("title"))
    return any(
        isinstance(candidate, str)
        and any(name == candidate or name in candidate.split() for name in names)
        for candidate in candidates
    )


def _error_code(error: BaseException, transport: str = "kimi_acp") -> str:
    if isinstance(error, TimeoutError):
        return f"{transport}_timeout"
    if isinstance(error, (TypeError, ValueError)):
        return f"{transport}_protocol_error"
    if isinstance(error, OSError):
        return f"{transport}_launch_error"
    return f"{transport}_error"


AcpConfig = KimiAcpConfig
AcpProvider = KimiAcpProvider
