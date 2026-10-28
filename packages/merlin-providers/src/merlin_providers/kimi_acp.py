"""Bounded no-tool transport for Kimi Code's stable ACP v1 server."""

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
    ModelCompleted,
    ModelError,
    ModelEvent,
    ProviderFailureKind,
    ReasoningDelta,
    TextDelta,
)

from .capabilities import RouteIdentity
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

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError("Kimi ACP command must not be empty")
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
            raise ValueError("Kimi ACP timeouts must be positive")
        if self.max_line_bytes < 1_024 or self.stderr_lines < 1:
            raise ValueError("Kimi ACP protocol bounds are too small")
        if self.reasoning_effort != "default":
            raise ValueError(
                "Kimi ACP does not expose a categorical reasoning-effort option; "
                "use provider default"
            )


class KimiAcpProvider:
    """Uses Kimi ACP sessions with no declared client, MCP, file, or terminal authority."""

    def __init__(self, config: KimiAcpConfig) -> None:
        self.config = config
        self._sessions: dict[str, _KimiSession] = {}
        self._session_lock = asyncio.Lock()
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=True, tools=False, usage_in_stream=False)

    @property
    def route_identity(self) -> RouteIdentity:
        """Exact transport + endpoint + model identity for capability resolution."""
        return RouteIdentity(transport="kimi_acp", endpoint="", model=self.config.model or "")

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        if request.reasoning == "disabled" or request.reasoning_effort is not None:
            yield ModelError(
                message="Kimi ACP does not support request reasoning controls",
                code="unsupported_request_control",
            )
            return
        if request.tools:
            yield ModelError(
                message="Kimi ACP tool calls are not connected to Merlin's tool-result protocol",
                code="kimi_acp_tools_unsupported",
                failure_kind=ProviderFailureKind.CONFIGURATION,
            )
            return
        key = request.budget_id or f"ephemeral:{id(request)}"
        try:
            session = await self._session(key)
            async for event in session.stream(request):
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
                    else "kimi_acp_forbidden_authority"
                    if permission_denied
                    else _error_code(error)
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
            root = self.config.state_directory / "kimi-acp"
            root.mkdir(parents=True, exist_ok=True)
            return root
        if self._temporary_directory is None:
            self._temporary_directory = tempfile.TemporaryDirectory(prefix="merlin-kimi-")
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

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        async with self._lock:
            await self._ensure_started(request)
            assert self.session_id is not None
            prompt, history = _request_prompt(request.messages, self._history)
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
            try:
                async with asyncio.timeout(self.config.turn_timeout_seconds):
                    while True:
                        message = await self._read_message(
                            self.config.partial_idle_timeout_seconds
                            if emitted
                            else self.config.idle_timeout_seconds
                        )
                        if "id" in message and "method" in message:
                            await self._deny_request(message)
                            await self._cancel()
                            yield ModelError(
                                message=(
                                    "Kimi ACP requested forbidden client authority: "
                                    f"{message['method']}"
                                ),
                                code="kimi_acp_forbidden_authority",
                                partial=emitted,
                                failure_kind=ProviderFailureKind.PERMISSION,
                            )
                            return
                        if message.get("id") == prompt_id:
                            if "error" in message:
                                error = _mapping(message.get("error"))
                                code, failure_kind, retryable = _classify_payload_failure(error)
                                yield ModelError(
                                    message=str(error.get("message") or "Kimi ACP prompt failed"),
                                    code=(
                                        "kimi_acp_prompt_error"
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
                            await self._cancel()
                            yield ModelError(
                                message="Kimi ACP attempted a forbidden tool call",
                                code="kimi_acp_forbidden_tool",
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
            record = self._load_record()
            if record is not None:
                request_id = await self._send_request(
                    "session/load",
                    {
                        "sessionId": record[0],
                        "cwd": str(self.cwd),
                        "mcpServers": [],
                    },
                )
                await self._response(request_id, self.config.startup_timeout_seconds)
                self.session_id, self._history = record
            else:
                request_id = await self._send_request(
                    "session/new", {"cwd": str(self.cwd), "mcpServers": []}
                )
                result = await self._response(request_id, self.config.startup_timeout_seconds)
                self.session_id = str(result.get("sessionId") or "") or None
                if self.session_id is None:
                    raise RuntimeError("Kimi ACP did not return a session id")
            request_id = await self._send_request(
                "session/set_mode", {"sessionId": self.session_id, "modeId": "plan"}
            )
            await self._response(request_id, self.config.startup_timeout_seconds)
            model = request.model or self.config.model
            if model:
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
                    f"Kimi ACP requested forbidden authority: {message['method']}"
                )
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = _mapping(message.get("error"))
                code, failure_kind, retryable = _classify_payload_failure(error)
                raise _ProviderResponseError(
                    str(error.get("message") or "Kimi ACP request failed"),
                    code="kimi_acp_request_error" if code == "provider_error" else code,
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
            raise RuntimeError("Kimi ACP is not running")
        encoded = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > self.config.max_line_bytes:
            raise ValueError("Kimi ACP request exceeds the protocol line bound")
        self.process.stdin.write(encoded)
        await self.process.stdin.drain()

    async def _read_message(self, timeout: float) -> Mapping[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("Kimi ACP is not running")
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
        except ValueError as error:
            raise ValueError("Kimi ACP response exceeds the protocol line bound") from error
        if not line:
            detail = "; ".join(self._stderr) or f"exit={self.process.returncode}"
            raise RuntimeError(f"Kimi ACP closed its output ({detail})")
        if len(line) > self.config.max_line_bytes:
            raise ValueError("Kimi ACP response exceeds the protocol line bound")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError("Kimi ACP emitted invalid JSON") from error
        if not isinstance(message, Mapping):
            raise TypeError("Kimi ACP emitted a non-object protocol message")
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
    messages: Sequence[ChatMessage], previous: tuple[str, ...]
) -> tuple[str, tuple[str, ...]]:
    serialized = tuple(message.model_dump_json() for message in messages)
    start = len(previous) if serialized[: len(previous)] == previous else 0
    selected = messages[start:] or messages[-1:]
    prompt = "\n\n".join(_format_message(message) for message in selected)
    return prompt or "Continue.", serialized


def _format_message(message: ChatMessage) -> str:
    label = message.role.upper()
    if message.name:
        label += f" ({message.name})"
    return f"[SYSTEM: Do not use tools, files, shell, browser, skills, plugins, or agents.]\n[{label}]\n{message.content}"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _error_code(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "kimi_acp_timeout"
    if isinstance(error, (TypeError, ValueError)):
        return "kimi_acp_protocol_error"
    if isinstance(error, OSError):
        return "kimi_acp_launch_error"
    return "kimi_acp_error"
