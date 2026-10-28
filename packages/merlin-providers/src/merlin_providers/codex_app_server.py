"""Bounded native transport for the Codex App Server JSONL protocol."""

from __future__ import annotations

import asyncio
import json
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
    ModelUsage,
    ProviderFailureKind,
    ReasoningDelta,
    TextDelta,
    ToolCall,
)

from .capabilities import RouteIdentity
from .openai_compatible import ProviderCapabilities, _classify_payload_failure


class _TransportClosedError(RuntimeError):
    """The native transport ended before the turn emitted a terminal event."""


class _PermissionDeniedError(RuntimeError):
    """The server requested authority excluded from the reviewed route."""


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


def _turn_parameters(
    config: CodexAppServerConfig, thread_id: str, prompt: str, model: str | None, effort: str
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt}],
        "model": model or config.model,
        "approvalPolicy": "never",
        "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
    }
    if effort != "default":
        params["effort"] = effort
    return params


@dataclass(frozen=True, slots=True)
class CodexAppServerConfig:
    command: tuple[str, ...] = ("codex", "app-server", "-c", "mcp_servers={}")
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

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError("Codex App Server command must not be empty")
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
            raise ValueError("Codex App Server timeouts must be positive")
        if self.max_line_bytes < 1_024 or self.stderr_lines < 1:
            raise ValueError("Codex App Server protocol bounds are too small")


class CodexAppServerProvider:
    """Streams Codex output while keeping Merlin in control of tools and authority."""

    def __init__(self, config: CodexAppServerConfig) -> None:
        self.config = config
        self._sessions: dict[str, _CodexSession] = {}
        self._session_lock = asyncio.Lock()
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=True, tools=True, usage_in_stream=True)

    @property
    def route_identity(self) -> RouteIdentity:
        """Exact transport + endpoint + model identity for capability resolution."""
        return RouteIdentity(
            transport="codex_app_server", endpoint="", model=self.config.model or ""
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        if request.reasoning == "disabled" and request.reasoning_control == "required":
            yield ModelError(
                message="Codex App Server cannot authoritatively disable reasoning",
                code="unsupported_request_control",
            )
            return
        if request.tools:
            yield ModelError(
                message=(
                    "Codex App Server dynamic tools are not connected to Merlin's tool-result "
                    "continuation protocol"
                ),
                code="codex_dynamic_tools_unsupported",
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
                message="Codex App Server cannot authoritatively disable reasoning",
                code="unsupported_request_control",
            )
            return
        if request.tools and handler is None:
            yield ModelError(
                message="Codex App Server tools require Merlin's authority callback",
                code="codex_dynamic_tools_unsupported",
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
            message = str(error)
            if isinstance(error, TimeoutError) and not message:
                message = (
                    "Codex App Server turn exceeded the configured total timeout "
                    f"of {self.config.turn_timeout_seconds:g} seconds"
                )
            structured = error if isinstance(error, _ProviderResponseError) else None
            yield ModelError(
                message=message,
                code=structured.code if structured is not None else _error_code(error),
                # Reopen only after an abnormal transport loss or a bounded
                # timeout. Configuration, authority, request, and protocol
                # failures are stable inputs and retrying them cannot recover.
                retryable=(
                    structured.retryable if structured is not None else _retryable_error(error)
                ),
                failure_kind=(
                    structured.failure_kind
                    if structured is not None
                    else _error_failure_kind(error)
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

    async def _session(self, key: str) -> _CodexSession:
        async with self._session_lock:
            session = self._sessions.get(key)
            if session is None:
                session = _CodexSession(self.config, self._sandbox_directory())
                self._sessions[key] = session
            return session

    async def _discard(self, key: str) -> None:
        async with self._session_lock:
            session = self._sessions.pop(key, None)
        if session is not None:
            await session.close()

    def _sandbox_directory(self) -> Path:
        if self.config.state_directory is not None:
            root = self.config.state_directory / "codex-app-server"
            root.mkdir(parents=True, exist_ok=True)
            return root
        if self._temporary_directory is None:
            self._temporary_directory = tempfile.TemporaryDirectory(prefix="merlin-codex-")
        return Path(self._temporary_directory.name)


class _CodexSession:
    def __init__(self, config: CodexAppServerConfig, cwd: Path) -> None:
        self.config, self.cwd = config, cwd
        self.process: asyncio.subprocess.Process | None = None
        self.thread_id: str | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._history: tuple[str, ...] = ()
        self._stderr: deque[str] = deque(maxlen=config.stderr_lines)
        self._stderr_task: asyncio.Task[None] | None = None

    async def stream(
        self, request: ChatRequest, handler: DynamicToolHandler | None = None
    ) -> AsyncIterator[ModelEvent]:
        async with self._lock:
            await self._ensure_started(request)
            assert self.thread_id is not None
            prompt, history = _request_prompt(request.messages, self._history)
            turn_request_id = await self._send_request(
                "turn/start",
                _turn_parameters(
                    self.config,
                    self.thread_id,
                    prompt,
                    request.model,
                    request.reasoning_effort or self.config.reasoning_effort,
                ),
            )
            turn_id: str | None = None
            usage: ModelUsage | None = None
            emitted = False
            try:
                async with asyncio.timeout(self.config.turn_timeout_seconds):
                    while True:
                        message = await self._read_message(
                            self.config.partial_idle_timeout_seconds
                            if emitted
                            else self.config.idle_timeout_seconds
                        )
                        if message.get("id") == turn_request_id:
                            if "error" in message:
                                error = _mapping(message.get("error"))
                                code, failure_kind, retryable = _classify_payload_failure(error)
                                yield ModelError(
                                    message=str(
                                        error.get("message") or "Codex turn request failed"
                                    ),
                                    code=(
                                        "codex_turn_request_error"
                                        if code == "provider_error"
                                        else code
                                    ),
                                    retryable=retryable,
                                    partial=emitted,
                                    failure_kind=failure_kind,
                                )
                                return
                            result = _mapping(message.get("result"))
                            turn_id = str(_mapping(result.get("turn")).get("id") or "") or None
                            continue
                        if "id" in message and "method" in message:
                            if message.get("method") == "item/tool/call" and handler is not None:
                                call = _dynamic_tool_call(message)
                                yield call
                                await self._write(
                                    {
                                        "id": message["id"],
                                        "result": {
                                            "contentItems": [
                                                {
                                                    "type": "inputText",
                                                    "text": (
                                                        "Accepted by Merlin. The authority-checked "
                                                        "result will be supplied in the next model "
                                                        "step; end this turn now."
                                                    ),
                                                }
                                            ],
                                            "success": True,
                                        },
                                    }
                                )
                                continue
                            await self._deny_server_request(message)
                            raise _PermissionDeniedError(
                                f"Codex App Server requested forbidden authority: {message['method']}"
                            )
                        method = message.get("method")
                        params = _mapping(message.get("params"))
                        if turn_id is not None and params.get("turnId") not in {None, turn_id}:
                            continue
                        if (
                            method == "item/reasoning/summaryTextDelta"
                            or method == "item/reasoning/textDelta"
                        ):
                            text = params.get("delta")
                            if isinstance(text, str) and text:
                                emitted = True
                                yield ReasoningDelta(text=text)
                        elif method == "item/agentMessage/delta":
                            text = params.get("delta")
                            if isinstance(text, str) and text:
                                emitted = True
                                yield TextDelta(text=text)
                        elif method == "thread/tokenUsage/updated":
                            usage = _usage(params)
                        elif method == "item/started":
                            item_type = _mapping(params.get("item")).get("type")
                            # contextCompaction is a Codex-owned transcript lifecycle item.
                            # It carries no executable authority and needs no Merlin broker call.
                            allowed_item_types = {
                                "agentMessage",
                                "reasoning",
                                "userMessage",
                                "contextCompaction",
                            }
                            if handler is not None:
                                allowed_item_types.add("dynamicToolCall")
                            if item_type not in allowed_item_types:
                                await self._interrupt(turn_id)
                                yield ModelError(
                                    message=f"Codex attempted forbidden built-in tool item {item_type!r}",
                                    code="codex_forbidden_tool",
                                    partial=emitted,
                                    failure_kind=ProviderFailureKind.PERMISSION,
                                )
                                return
                        elif method == "turn/completed":
                            turn = _mapping(params.get("turn"))
                            status = str(turn.get("status") or "failed")
                            if usage is not None:
                                yield usage
                            if status == "completed":
                                self._history = history
                                yield ModelCompleted(finish_reason=status)
                            else:
                                error = _mapping(turn.get("error"))
                                code, failure_kind, retryable = _classify_payload_failure(error)
                                yield ModelError(
                                    message=str(error.get("message") or f"Codex turn {status}"),
                                    code=(
                                        f"codex_turn_{status}" if code == "provider_error" else code
                                    ),
                                    retryable=retryable,
                                    partial=emitted,
                                    failure_kind=failure_kind,
                                )
                            return
            except asyncio.CancelledError:
                await self._interrupt(turn_id)
                raise

    async def close(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
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
                    "clientInfo": {"name": "merlin", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": bool(request.tools)},
                },
            )
            await self._response(request_id, self.config.startup_timeout_seconds)
            await self._notify("initialized", {})
        if self.thread_id is None:
            thread_params: dict[str, object] = {
                "cwd": str(self.cwd),
                "model": request.model or self.config.model,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
                "baseInstructions": _base_instructions(bool(request.tools)),
                "config": {
                    "features": {
                        "apps": False,
                        "browser_use": False,
                        "computer_use": False,
                        "multi_agent": False,
                        "plugins": False,
                        "shell_tool": False,
                        "skill_search": False,
                        "unified_exec": False,
                    },
                    "tools": {"web_search": False},
                },
            }
            if request.tools:
                thread_params["dynamicTools"] = [_dynamic_tool_spec(tool) for tool in request.tools]
            request_id = await self._send_request(
                "thread/start",
                thread_params,
            )
            result = await self._response(request_id, self.config.startup_timeout_seconds)
            self.thread_id = str(_mapping(result.get("thread")).get("id") or "") or None
            if self.thread_id is None:
                raise RuntimeError("Codex App Server did not return a thread id")

    async def _response(self, request_id: int, timeout: float) -> Mapping[str, Any]:
        while True:
            message = await self._read_message(timeout)
            if message.get("id") != request_id:
                if "id" in message and "method" in message:
                    await self._deny_server_request(message)
                    raise _PermissionDeniedError(
                        f"Codex App Server requested forbidden authority: {message['method']}"
                    )
                continue
            if "error" in message:
                error = _mapping(message.get("error"))
                code, failure_kind, retryable = _classify_payload_failure(error)
                raise _ProviderResponseError(
                    str(error.get("message") or "Codex App Server request failed"),
                    code="codex_request_error" if code == "provider_error" else code,
                    failure_kind=failure_kind,
                    retryable=retryable,
                )
            return _mapping(message.get("result"))

    async def _send_request(self, method: str, params: Mapping[str, Any]) -> int:
        self._request_id += 1
        await self._write({"id": self._request_id, "method": method, "params": dict(params)})
        return self._request_id

    async def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        await self._write({"method": method, "params": dict(params)})

    async def _deny_server_request(self, message: Mapping[str, Any]) -> None:
        await self._write(
            {
                "id": message["id"],
                "error": {"code": -32601, "message": "Merlin denies external tool authority"},
            }
        )

    async def _interrupt(self, turn_id: str | None) -> None:
        if self.thread_id is None or turn_id is None or self.process is None:
            return
        try:
            request_id = await self._send_request(
                "turn/interrupt", {"threadId": self.thread_id, "turnId": turn_id}
            )
            await self._response(request_id, self.config.shutdown_timeout_seconds)
        except (OSError, RuntimeError, TimeoutError):
            pass

    async def _write(self, message: Mapping[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Codex App Server is not running")
        encoded = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > self.config.max_line_bytes:
            raise ValueError("Codex App Server request exceeds the protocol line bound")
        self.process.stdin.write(encoded)
        await self.process.stdin.drain()

    async def _read_message(self, timeout: float) -> Mapping[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("Codex App Server is not running")
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
        except TimeoutError as error:
            raise TimeoutError(
                f"Codex App Server emitted no protocol message for {timeout:g} seconds"
            ) from error
        except ValueError as error:
            raise ValueError("Codex App Server response exceeds the protocol line bound") from error
        if not line:
            detail = "; ".join(self._stderr) or f"exit={self.process.returncode}"
            raise _TransportClosedError(f"Codex App Server closed its output ({detail})")
        if len(line) > self.config.max_line_bytes:
            raise ValueError("Codex App Server response exceeds the protocol line bound")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError("Codex App Server emitted invalid JSON") from error
        if not isinstance(message, Mapping):
            raise TypeError("Codex App Server emitted a non-object protocol message")
        return message

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while line := await self.process.stderr.readline():
            self._stderr.append(line.decode(errors="replace").rstrip()[:1_024])


def _request_prompt(
    messages: Sequence[ChatMessage], previous: tuple[str, ...]
) -> tuple[str, tuple[str, ...]]:
    serialized = tuple(message.model_dump_json() for message in messages)
    start = len(previous) if serialized[: len(previous)] == previous else 0
    selected = messages[start:]
    if not selected:
        selected = messages[-1:]
    prompt = "\n\n".join(_format_message(message) for message in selected)
    return prompt or "Continue.", serialized


def _format_message(message: ChatMessage) -> str:
    label = message.role.upper()
    if message.name:
        label += f" ({message.name})"
    return f"[{label}]\n{message.content}"


def _base_instructions(dynamic_tools: bool) -> str:
    tool_rule = (
        "Invoke only the exact dynamic tools advertised by Merlin; their results are returned "
        "through Merlin's authority callback. "
        if dynamic_tools
        else "Do not call any tools. "
    )
    return (
        "You are a language-model transport inside Merlin. "
        + tool_rule
        + "Never call shell, file, built-in browser, web, MCP, skill, plugin, app, "
        "collaboration, or approval tools. Return only reasoning and assistant text."
    )


def _usage(params: Mapping[str, Any]) -> ModelUsage | None:
    last = _mapping(_mapping(params.get("tokenUsage")).get("last"))
    try:
        input_tokens = int(last.get("inputTokens") or 0)
        cache_read = int(last.get("cachedInputTokens") or 0)
        reasoning = int(last.get("reasoningOutputTokens") or 0)
        uncached = max(0, input_tokens - cache_read)
        return ModelUsage(
            input_tokens=input_tokens,
            output_tokens=int(last.get("outputTokens") or 0),
            total_tokens=int(last.get("totalTokens") or 0),
            reasoning_output_tokens=reasoning,
            uncached_input_tokens=uncached,
            cache_read_input_tokens=cache_read,
            cache_hit=cache_read > 0,
            cache_miss=cache_read == 0 and uncached > 0,
        )
    except (TypeError, ValueError):
        return None


def _dynamic_tool_spec(tool: Mapping[str, object]) -> dict[str, object]:
    function = _mapping(tool.get("function"))
    name = function.get("name")
    description = function.get("description")
    input_schema = function.get("parameters")
    if tool.get("type") != "function" or not isinstance(name, str) or not name:
        raise ValueError("Codex dynamic tool must be a named function")
    if not isinstance(description, str) or not isinstance(input_schema, Mapping):
        raise TypeError(f"Codex dynamic tool {name!r} has an invalid description or input schema")
    return {
        "type": "function",
        "name": name,
        "description": description,
        "inputSchema": dict(input_schema),
    }


def _dynamic_tool_call(message: Mapping[str, Any]) -> ToolCall:
    params = _mapping(message.get("params"))
    arguments = params.get("arguments")
    if not isinstance(arguments, Mapping):
        raise TypeError("Codex dynamic tool arguments must be an object")
    call_id = params.get("callId")
    name = params.get("tool")
    if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
        raise ValueError("Codex dynamic tool call is missing its call id or tool name")
    return ToolCall(call_id=call_id, name=name, arguments=dict(arguments))


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _error_code(error: BaseException) -> str:
    if isinstance(error, _PermissionDeniedError):
        return "codex_forbidden_authority"
    if isinstance(error, TimeoutError):
        return "codex_timeout"
    if isinstance(error, (TypeError, ValueError)):
        return "codex_protocol_error"
    if isinstance(error, OSError):
        return "codex_launch_error"
    return "codex_app_server_error"


def _retryable_error(error: BaseException) -> bool:
    return isinstance(error, (TimeoutError, _TransportClosedError))


def _error_failure_kind(error: BaseException) -> ProviderFailureKind:
    if isinstance(error, _PermissionDeniedError):
        return ProviderFailureKind.PERMISSION
    if isinstance(error, (TimeoutError, _TransportClosedError)):
        return ProviderFailureKind.TRANSIENT
    if isinstance(error, (TypeError, ValueError)):
        return ProviderFailureKind.FORMATTING
    if isinstance(error, OSError):
        return ProviderFailureKind.CONFIGURATION
    return ProviderFailureKind.UNKNOWN
