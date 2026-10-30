"""Bounded Claude Code transport with Merlin-brokered MCP tools."""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
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
from .dynamic_mcp import DynamicMcpBroker
from .openai_compatible import ProviderCapabilities

_SESSION_NAMESPACE = uuid.UUID("7f3a1a7b-e987-4c2a-a2ac-c30fd8b7df85")


def _effort_arguments(effort: str) -> tuple[str, ...]:
    if effort == "default":
        return ()
    return ("--effort", "low" if effort == "minimal" else effort)


@dataclass(frozen=True, slots=True)
class ClaudeCliConfig:
    command: tuple[str, ...] = ("claude",)
    model: str | None = None
    state_directory: Path | None = None
    idle_timeout_seconds: float = 60.0
    partial_idle_timeout_seconds: float = 60.0
    turn_timeout_seconds: float = 600.0
    shutdown_timeout_seconds: float = 2.0
    max_line_bytes: int = 1_048_576
    stderr_lines: int = 64
    reasoning_effort: str = "default"

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError("Claude CLI command must not be empty")
        if (
            min(
                self.idle_timeout_seconds,
                self.partial_idle_timeout_seconds,
                self.turn_timeout_seconds,
                self.shutdown_timeout_seconds,
            )
            <= 0
        ):
            raise ValueError("Claude CLI timeouts must be positive")
        if self.max_line_bytes < 1_024 or self.stderr_lines < 1:
            raise ValueError("Claude CLI protocol bounds are too small")


class ClaudeCliProvider:
    """Uses Claude authentication while admitting only Merlin-brokered MCP tools."""

    def __init__(self, config: ClaudeCliConfig) -> None:
        self.config = config
        self._history: dict[str, tuple[str, ...]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=True, tools=True, usage_in_stream=True)

    @property
    def route_identity(self) -> RouteIdentity:
        """Exact transport + endpoint + model identity for capability resolution."""
        return RouteIdentity(transport="claude_cli", endpoint="", model=self.config.model or "")

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        if request.reasoning == "disabled" and request.reasoning_control == "required":
            yield ModelError(
                message="Claude CLI cannot authoritatively disable reasoning",
                code="unsupported_request_control",
            )
            return
        if request.tools:
            yield ModelError(
                message="Claude CLI tool calls are not connected to Merlin's tool-result protocol",
                code="claude_cli_tools_unsupported",
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
                message="Claude CLI cannot authoritatively disable reasoning",
                code="unsupported_request_control",
            )
            return
        if request.tools and handler is None:
            yield ModelError(
                message="Claude CLI tools require Merlin's authority callback",
                code="claude_cli_tools_unsupported",
                failure_kind=ProviderFailureKind.CONFIGURATION,
            )
            return
        key = request.budget_id or f"ephemeral:{id(request)}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            async for event in self._stream_locked(key, request, handler):
                yield event
        if request.budget_id is None:
            self._history.pop(key, None)
            self._locks.pop(key, None)

    async def close(self) -> None:
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None

    async def _stream_locked(
        self,
        key: str,
        request: ChatRequest,
        handler: DynamicToolHandler | None,
    ) -> AsyncIterator[ModelEvent]:
        previous = self._history.get(key, ())
        prompt, history = _request_prompt(request.messages, previous)
        session_id = str(uuid.uuid5(_SESSION_NAMESPACE, key))
        resume = bool(previous)
        broker = (
            DynamicMcpBroker(request.tools, max_line_bytes=self.config.max_line_bytes)
            if request.tools and handler is not None
            else None
        )
        if broker is not None:
            await broker.start()
        mcp_config = broker.claude_config() if broker is not None else '{"mcpServers":{}}'
        arguments = [
            *self.config.command,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--input-format",
            "text",
            "--safe-mode",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--mcp-config",
            mcp_config,
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--system-prompt",
            (
                "You are a language-model transport inside Merlin. Use only MCP tools from the "
                "`merlin` server when tools are available. Never use built-in tools, skills, "
                "agents, hooks, plugins, browsers, files, or shell commands. After an MCP tool "
                "returns an acceptance receipt, end the turn immediately; Merlin supplies the "
                "authority-checked result in the next step."
            ),
        ]
        if broker is not None:
            arguments.append("--allowedTools")
            arguments.extend(f"mcp__merlin__{name}" for name in sorted(broker.tool_names))
        if request.model or self.config.model:
            arguments.extend(("--model", request.model or self.config.model or ""))
        arguments.extend(
            _effort_arguments(request.reasoning_effort or self.config.reasoning_effort)
        )
        arguments.extend(("--resume" if resume else "--session-id", session_id, prompt))
        stderr: deque[str] = deque(maxlen=self.config.stderr_lines)
        process: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[None] | None = None
        emitted_chars = 0
        output_char_bound = (
            request.max_output_tokens * 4 if request.max_output_tokens is not None else None
        )
        result_seen = False
        visible_progress = False
        line_task: asyncio.Task[bytes] | None = None
        broker_task: asyncio.Task[ToolCall] | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._sandbox_directory(),
                limit=self.config.max_line_bytes + 1,
            )
            stderr_task = asyncio.create_task(_drain_stderr(process, stderr))
            assert process.stdout is not None
            async with asyncio.timeout(self.config.turn_timeout_seconds):
                while True:
                    if line_task is None:
                        line_task = asyncio.create_task(process.stdout.readline())
                    if broker is not None and broker_task is None:
                        broker_task = asyncio.create_task(broker.next_event())
                    waiting = {line_task}
                    if broker_task is not None:
                        waiting.add(broker_task)
                    done, _ = await asyncio.wait(
                        waiting,
                        timeout=(
                            self.config.partial_idle_timeout_seconds
                            if visible_progress
                            else self.config.idle_timeout_seconds
                        ),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        raise TimeoutError
                    if broker_task is not None and broker_task in done:
                        call = broker_task.result()
                        broker_task = None
                        visible_progress = True
                        yield call
                        continue
                    line = line_task.result()
                    line_task = None
                    if not line:
                        break
                    if len(line) > self.config.max_line_bytes:
                        raise ValueError("Claude CLI response exceeds the protocol line bound")
                    message = _json_object(line)
                    event = _mapping(message.get("event"))
                    if _is_tool_use(message, event):
                        tool_name = _tool_use_name(message, event)
                        if not (
                            broker is not None
                            and isinstance(tool_name, str)
                            and tool_name.startswith("mcp__merlin__")
                        ):
                            yield ModelError(
                                message="Claude CLI attempted a forbidden tool call",
                                code="claude_cli_forbidden_tool",
                                partial=emitted_chars > 0,
                                failure_kind=ProviderFailureKind.PERMISSION,
                            )
                            return
                    delta = _mapping(event.get("delta"))
                    delta_type = delta.get("type")
                    if message.get("type") == "stream_event" and delta_type == "thinking_delta":
                        text = delta.get("thinking")
                        if isinstance(text, str) and text:
                            visible_progress = True
                            yield ReasoningDelta(text=text)
                    elif message.get("type") == "stream_event" and delta_type == "text_delta":
                        text = delta.get("text")
                        if isinstance(text, str) and text:
                            if output_char_bound is not None:
                                text = text[: max(0, output_char_bound - emitted_chars)]
                            if text:
                                visible_progress = True
                                emitted_chars += len(text)
                                yield TextDelta(text=text)
                            if output_char_bound is not None and emitted_chars >= output_char_bound:
                                yield ModelCompleted(
                                    finish_reason="output_bound", disposition="truncated"
                                )
                                return
                    elif message.get("type") == "result":
                        result_seen = True
                        if not emitted_chars:
                            text = message.get("result")
                            if isinstance(text, str) and text:
                                yield TextDelta(text=text)
                        if usage := _usage(message):
                            yield usage
                        if message.get("is_error"):
                            yield ModelError(
                                message=str(message.get("result") or "Claude CLI request failed"),
                                code="claude_cli_result_error",
                                partial=emitted_chars > 0,
                                failure_kind=ProviderFailureKind.UNKNOWN,
                            )
                        else:
                            self._history[key] = history
                            yield ModelCompleted(
                                finish_reason=str(message.get("subtype") or "success")
                            )
                        return
            return_code = await process.wait()
            if not result_seen:
                detail = "; ".join(stderr) or f"exit={return_code}"
                yield ModelError(
                    message=f"Claude CLI ended without a result ({detail})",
                    code="claude_cli_incomplete",
                    retryable=True,
                    partial=emitted_chars > 0,
                    failure_kind=ProviderFailureKind.TRANSIENT,
                )
        except TimeoutError:
            yield ModelError(
                message="Claude CLI deadline exceeded",
                code="claude_cli_timeout",
                retryable=True,
                partial=emitted_chars > 0,
                failure_kind=ProviderFailureKind.TRANSIENT,
            )
        except (OSError, TypeError, ValueError) as error:
            yield ModelError(
                message=str(error),
                code="claude_cli_protocol_error"
                if not isinstance(error, OSError)
                else "claude_cli_launch_error",
                retryable=not isinstance(error, OSError),
                partial=emitted_chars > 0,
                failure_kind=(
                    ProviderFailureKind.FORMATTING
                    if not isinstance(error, OSError)
                    else ProviderFailureKind.CONFIGURATION
                ),
            )
        finally:
            for task in (line_task, broker_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (line_task, broker_task) if task is not None),
                return_exceptions=True,
            )
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), self.config.shutdown_timeout_seconds)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            if stderr_task is not None:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            if broker is not None:
                await broker.close()

    def _sandbox_directory(self) -> Path:
        if self.config.state_directory is not None:
            root = self.config.state_directory / "claude-cli"
            root.mkdir(parents=True, exist_ok=True)
            return root
        if self._temporary_directory is None:
            self._temporary_directory = tempfile.TemporaryDirectory(prefix="merlin-claude-")
        return Path(self._temporary_directory.name)


async def _drain_stderr(process: asyncio.subprocess.Process, destination: deque[str]) -> None:
    assert process.stderr is not None
    while line := await process.stderr.readline():
        destination.append(line.decode(errors="replace").rstrip()[:1_024])


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
    return f"[{label}]\n{message.content}"


def _json_object(line: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError("Claude CLI emitted invalid stream JSON") from error
    if not isinstance(value, Mapping):
        raise TypeError("Claude CLI emitted a non-object stream event")
    return value


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _is_tool_use(message: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    if message.get("type") == "tool_use":
        return True
    block = _mapping(event.get("content_block"))
    return block.get("type") in {"tool_use", "server_tool_use"}


def _tool_use_name(message: Mapping[str, Any], event: Mapping[str, Any]) -> object:
    if message.get("type") == "tool_use":
        return message.get("name")
    return _mapping(event.get("content_block")).get("name")


def _usage(message: Mapping[str, Any]) -> ModelUsage | None:
    usage = _mapping(message.get("usage"))
    if not usage:
        return None
    try:
        uncached = int(usage.get("input_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
        input_tokens = uncached + cache_read + cache_creation
        output_tokens = int(usage.get("output_tokens") or 0)
        cost = message.get("total_cost_usd")
        return ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            estimated_cost_usd=float(cost) if cost is not None else None,
            uncached_input_tokens=uncached,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
            cache_hit=cache_read > 0,
            cache_created=cache_creation > 0,
            cache_miss=cache_read == 0 and uncached > 0,
        )
    except (TypeError, ValueError):
        return None
