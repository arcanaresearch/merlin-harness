"""Reliable streaming client for OpenAI-compatible chat-completions APIs."""

from __future__ import annotations

import asyncio
import base64
import email.utils
import hashlib
import html
import json
import os
import random
import re
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from merlin_contracts import (
    ChatRequest,
    ModelCompleted,
    ModelError,
    ModelEvent,
    ModelUsage,
    ModelWarning,
    ProviderFailureKind,
    ReasoningDelta,
    TextDelta,
    ToolCall,
)

from .capabilities import RouteIdentity


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    streaming: bool = True
    tools: bool = True
    usage_in_stream: bool = True
    model: str | None = None
    vision: bool = False
    context_window_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderQuotaSnapshot:
    """Latest provider-advertised quota metadata; absent fields remain unknown."""

    limit_requests: int | None = None
    remaining_requests: int | None = None
    reset_requests_seconds: float | None = None
    limit_tokens: int | None = None
    remaining_tokens: int | None = None
    reset_tokens_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Provider connection and budget settings.

    ``max_tokens`` is a CUMULATIVE token budget for the whole provider
    instance (settled usage across every request it serves); once spent, all
    later calls fail with ``budget_exhausted``. It is NOT the per-request
    output cap — set ``default_max_output_tokens`` (or
    ``ChatRequest.max_output_tokens``) for that. The per-request payload
    field is selected by ``output_token_parameter``.
    """

    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout: float | None = 60.0
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    partial_read_timeout: float = 60.0
    total_timeout: float | None = None
    max_attempts: int = 3
    max_concurrent_requests: int = 8
    retry_base_seconds: float = 0.25
    retry_max_seconds: float = 10.0
    max_tool_calls: int = 32
    max_tool_argument_bytes: int = 262_144
    max_tool_argument_depth: int = 32
    max_tokens: int | None = None
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    cache_read_input_cost_per_million: float | None = None
    max_cost_usd: float | None = None
    vision: bool = False
    context_window_tokens: int | None = None
    default_max_output_tokens: int = 16_384
    output_token_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    reasoning_parameter: Literal["none", "thinking"] = "none"
    reasoning_effort: Literal["default", "minimal", "low", "medium", "high", "xhigh"] = "default"
    requires_reasoning_content_for_tool_calls: bool | None = None
    required_capabilities: frozenset[Literal["streaming", "tools", "usage_in_stream"]] = frozenset(
        {"streaming"}
    )

    def __post_init__(self) -> None:
        total = self.total_timeout if self.total_timeout is not None else self.timeout
        if not self.base_url.strip() or not self.model.strip():
            raise ValueError("base_url and model must not be empty")
        if (
            total is None
            or min(
                self.connect_timeout,
                self.read_timeout,
                self.partial_read_timeout,
                total,
            )
            <= 0
        ):
            raise ValueError("provider timeouts must be positive")
        if (
            min(self.max_attempts, self.max_concurrent_requests) < 1
            or self.max_concurrent_requests > 8
            or min(self.max_tool_calls, self.max_tool_argument_bytes, self.max_tool_argument_depth)
            < 1
        ):
            raise ValueError("provider bounds must be positive")
        if self.retry_base_seconds <= 0 or self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("provider retry bounds must be positive and ordered")
        if self.context_window_tokens is not None and self.context_window_tokens < 1_024:
            raise ValueError("provider context window must be at least 1024 tokens")
        for cost_name in (
            "input_cost_per_million",
            "output_cost_per_million",
            "cache_read_input_cost_per_million",
        ):
            cost_value = getattr(self, cost_name)
            if cost_value is not None and cost_value < 0:
                raise ValueError(f"provider {cost_name} must be non-negative")

    @property
    def effective_total_timeout(self) -> float:
        value = self.total_timeout if self.total_timeout is not None else self.timeout
        assert value is not None
        return value

    @property
    def effective_requires_reasoning_content_for_tool_calls(self) -> bool:
        configured = self.requires_reasoning_content_for_tool_calls
        if configured is not None:
            return configured
        return httpx.URL(self.base_url).host == "api.deepseek.com"


class ProviderError(RuntimeError):
    pass


def _merge_streamed_scalar(current: str, incoming: str) -> str:
    """Accept fragment or cumulative streaming for call identifiers and names."""

    if not incoming:
        return current
    if not current or incoming.startswith(current):
        return incoming
    if current.startswith(incoming):
        return current
    return current + incoming


def _merge_streamed_overlap(current: str, incoming: str) -> str:
    """Build a recovery candidate by removing only an exact boundary overlap."""

    if not current or incoming.startswith(current):
        return incoming
    if current.startswith(incoming):
        return current
    overlap_limit = min(len(current), len(incoming))
    for size in range(overlap_limit, 0, -1):
        if current.endswith(incoming[:size]):
            return current + incoming[size:]
    return current + incoming


def _decode_tool_argument_object(raw: str, *, max_depth: int) -> dict[str, object] | None:
    """Decode only an exact, optionally fenced or once-encoded JSON object."""

    candidate = raw.strip() or "{}"
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fenced is not None:
        candidate = fenced.group(1).strip()
    try:
        try:
            decoded: object = json.loads(candidate)
        except json.JSONDecodeError:
            # Some OpenAI-compatible providers stream literal newlines or tabs
            # inside otherwise valid JSON string values. Python's non-strict
            # mode accepts only those control characters; structure, quoting,
            # types, and trailing content remain fully validated.
            decoded = json.loads(candidate, strict=False)
        if isinstance(decoded, str):
            try:
                decoded = json.loads(decoded)
            except json.JSONDecodeError:
                decoded = json.loads(decoded, strict=False)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict) or _json_depth(decoded) > max_depth:
        return None
    return decoded


_DSML = "｜DSML｜"
_DSML_DOUBLED = "｜｜DSML｜｜"


def _decode_deepseek_dsml(
    raw: str,
    *,
    allowed_tools: frozenset[str],
    max_calls: int,
    max_depth: int,
) -> list[ToolCall] | None:
    """Decode one complete DeepSeek V4 DSML block into admitted tool calls."""

    candidate = raw.strip().replace(_DSML_DOUBLED, _DSML)
    wrapper = re.fullmatch(
        rf"<{re.escape(_DSML)}tool_calls>\s*(.*?)\s*</{re.escape(_DSML)}tool_calls>",
        candidate,
        flags=re.DOTALL,
    )
    body = wrapper.group(1) if wrapper is not None else candidate
    invoke_pattern = re.compile(
        rf'<{re.escape(_DSML)}invoke\s+name="([^"]+)">\s*(.*?)\s*'
        rf"</{re.escape(_DSML)}invoke>",
        flags=re.DOTALL,
    )
    matches = list(invoke_pattern.finditer(body))
    if not matches or len(matches) > max_calls:
        return None
    cursor = 0
    calls: list[ToolCall] = []
    for index, match in enumerate(matches):
        if body[cursor : match.start()].strip():
            return None
        cursor = match.end()
        name = html.unescape(match.group(1)).strip()
        if not name or name not in allowed_tools:
            return None
        arguments_text = match.group(2).strip()
        arguments = _decode_tool_argument_object(arguments_text, max_depth=max_depth)
        if arguments is None:
            arguments = {}
            parameter_pattern = re.compile(
                rf'<{re.escape(_DSML)}parameter\s+name="([^"]+)"\s+'
                rf'string="(true|false)">\s*(.*?)\s*'
                rf"</{re.escape(_DSML)}parameter>",
                flags=re.DOTALL,
            )
            parameter_matches = list(parameter_pattern.finditer(arguments_text))
            parameter_cursor = 0
            for parameter in parameter_matches:
                if arguments_text[parameter_cursor : parameter.start()].strip():
                    return None
                parameter_cursor = parameter.end()
                key = html.unescape(parameter.group(1)).strip()
                if not key or key in arguments:
                    return None
                value_text = html.unescape(parameter.group(3))
                if parameter.group(2) == "true":
                    value: object = value_text
                else:
                    try:
                        value = json.loads(value_text.strip())
                    except json.JSONDecodeError:
                        return None
                arguments[key] = value
            if not parameter_matches or arguments_text[parameter_cursor:].strip():
                return None
            if _json_depth(arguments) > max_depth:
                return None
        digest = hashlib.sha256(match.group(0).encode()).hexdigest()[:16]
        calls.append(ToolCall(call_id=f"dsml-{digest}-{index}", name=name, arguments=arguments))
    if body[cursor:].strip():
        return None
    return calls


class _AdaptiveRequestGate:
    """Shared AIMD admission gate with provider-directed cooldown."""

    def __init__(self, ceiling: int, retry_base_seconds: float, retry_max_seconds: float) -> None:
        self.ceiling = ceiling
        self.limit = ceiling
        self.active = 0
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._blocked_until = 0.0
        self._successful_completions = 0
        self._condition = asyncio.Condition()
        self._waiters: list[tuple[object, str]] = []
        self._last_admitted_group: str | None = None
        self.quota_snapshot: ProviderQuotaSnapshot | None = None

    async def acquire(self, group: str = "default") -> None:
        loop = asyncio.get_running_loop()
        ticket = object()
        async with self._condition:
            self._waiters.append((ticket, group))
            try:
                while True:
                    delay = self._blocked_until - loop.time()
                    group_head = next((item for item in self._waiters if item[1] == group), None)
                    selected_group = self._next_group()
                    admissible = (
                        group_head is not None
                        and group_head[0] is ticket
                        and selected_group == group
                    )
                    if delay <= 0 and admissible and self.active < self.limit:
                        self._waiters.remove((ticket, group))
                        self.active += 1
                        self._last_admitted_group = group
                        self._condition.notify_all()
                        return
                    if delay > 0:
                        try:
                            await asyncio.wait_for(self._condition.wait(), timeout=delay)
                        except TimeoutError:
                            pass
                    else:
                        await self._condition.wait()
            except BaseException:
                waiter = (ticket, group)
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                    self._condition.notify_all()
                raise

    def _next_group(self) -> str | None:
        groups = list(dict.fromkeys(group for _, group in self._waiters))
        if not groups:
            return None
        if self._last_admitted_group not in groups:
            return groups[0]
        index = groups.index(self._last_admitted_group)
        return groups[(index + 1) % len(groups)]

    async def penalize(self, retry_after_seconds: float | None) -> None:
        loop = asyncio.get_running_loop()
        async with self._condition:
            self.limit = max(1, self.limit // 2)
            self._successful_completions = 0
            delay = min(
                self._retry_max_seconds,
                max(self._retry_base_seconds, retry_after_seconds or 0.0),
            )
            self._blocked_until = max(self._blocked_until, loop.time() + delay)
            self._condition.notify_all()

    async def observe_quota(self, headers: Mapping[str, str]) -> ProviderQuotaSnapshot | None:
        """Record documented quota metadata and apply request admission hints."""
        snapshot = ProviderQuotaSnapshot(
            limit_requests=_nonnegative_int(
                _first_header(headers, "x-ratelimit-limit-requests", "ratelimit-limit")
            ),
            remaining_requests=_nonnegative_int(
                _first_header(headers, "x-ratelimit-remaining-requests", "ratelimit-remaining")
            ),
            reset_requests_seconds=_quota_reset_after(
                _first_header(headers, "x-ratelimit-reset-requests", "ratelimit-reset")
            ),
            limit_tokens=_nonnegative_int(_first_header(headers, "x-ratelimit-limit-tokens")),
            remaining_tokens=_nonnegative_int(
                _first_header(headers, "x-ratelimit-remaining-tokens")
            ),
            reset_tokens_seconds=_quota_reset_after(
                _first_header(headers, "x-ratelimit-reset-tokens")
            ),
        )
        if not any(value is not None for value in asdict(snapshot).values()):
            return None
        self.quota_snapshot = snapshot
        remaining_requests = snapshot.remaining_requests
        if remaining_requests is None:
            return snapshot
        reset_after = snapshot.reset_requests_seconds
        loop = asyncio.get_running_loop()
        async with self._condition:
            if remaining_requests <= 0 and reset_after is not None:
                delay = min(self._retry_max_seconds, reset_after)
                self._blocked_until = max(self._blocked_until, loop.time() + delay)
            elif remaining_requests > 0:
                self.limit = max(1, min(self.limit, remaining_requests, self.ceiling))
            self._condition.notify_all()
        return snapshot

    async def release(self, *, completed: bool) -> None:
        async with self._condition:
            self.active -= 1
            if completed and self.limit < self.ceiling:
                self._successful_completions += 1
                if self._successful_completions >= self.limit:
                    self.limit += 1
                    self._successful_completions = 0
            self._condition.notify_all()


class OpenAICompatibleProvider:
    _transport = "openai_compatible"

    def __init__(self, config: ProviderConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config, self._client = config, client
        self._capabilities = ProviderCapabilities(
            model=config.model,
            vision=config.vision,
            context_window_tokens=config.context_window_tokens,
        )
        self._settled_tokens, self._settled_cost = 0, 0.0
        self._request_gate = _AdaptiveRequestGate(
            config.max_concurrent_requests,
            config.retry_base_seconds,
            config.retry_max_seconds,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    @property
    def route_identity(self) -> RouteIdentity:
        """Exact transport + endpoint + model identity for capability resolution."""
        return RouteIdentity(
            transport=self._transport,
            endpoint=self.config.base_url,
            model=self.config.model,
        )

    @property
    def settled_usage(self) -> tuple[int, float]:
        return self._settled_tokens, self._settled_cost

    @property
    def quota_snapshot(self) -> ProviderQuotaSnapshot | None:
        return self._request_gate.quota_snapshot

    def negotiate(self, capabilities: ProviderCapabilities) -> ProviderCapabilities:
        missing = [
            name for name in self.config.required_capabilities if not getattr(capabilities, name)
        ]
        if capabilities.model is not None and capabilities.model != self.config.model:
            missing.append(f"model={self.config.model}")
        if missing:
            raise ProviderError("provider capability drift: missing " + ", ".join(sorted(missing)))
        self._capabilities = capabilities
        return capabilities

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        await self._request_gate.acquire(_admission_group(request.budget_id))
        completed = False
        try:
            async for event in self._stream_admitted(request):
                completed |= isinstance(event, ModelCompleted)
                yield event
        finally:
            await self._request_gate.release(completed=completed)

    async def _stream_admitted(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        if error := self._preflight_budget(request):
            yield error
            return
        owned = self._client is None
        client = self._client or httpx.AsyncClient()
        emitted = False
        usage_seen = False
        attempt = 0
        attempt_started = time.monotonic()
        try:
            async with asyncio.timeout(self.config.effective_total_timeout):
                for attempt in range(1, self.config.max_attempts + 1):
                    retry: ModelError | None = None
                    attempt_emitted = False
                    attempt_started = time.monotonic()
                    async for event in self._attempt(client, request):
                        if isinstance(event, ModelError) and event.code == "rate_limited":
                            await self._request_gate.penalize(event.retry_after_seconds)
                        if (
                            isinstance(event, ModelError)
                            and event.retryable
                            and not emitted
                            and not attempt_emitted
                            and attempt < self.config.max_attempts
                        ):
                            retry = event
                            break
                        emitted |= isinstance(event, (TextDelta, ReasoningDelta, ToolCall))
                        attempt_emitted |= isinstance(event, (TextDelta, ReasoningDelta, ToolCall))
                        if isinstance(event, ModelUsage):
                            usage_seen = True
                            event.attempt_count = attempt
                            event.latency_ms = int((time.monotonic() - attempt_started) * 1000)
                            self._settled_tokens += event.total_tokens
                            self._settled_cost += event.estimated_cost_usd or 0
                        if isinstance(event, ModelError) and event.partial and not usage_seen:
                            # Interrupted or proxy-mediated calls lack authoritative
                            # usage; record an incomplete provider-usage marker rather
                            # than silently omitting the inference.
                            usage_seen = True
                            yield ModelUsage(usage_complete=False, attempt_count=attempt)
                        yield event
                    if retry is not None:
                        await asyncio.sleep(self._retry_delay(attempt, retry.retry_after_seconds))
                        continue
                    return
        except TimeoutError:
            if emitted and not usage_seen:
                yield ModelUsage(usage_complete=False, attempt_count=max(1, attempt))
            yield ModelError(
                message="provider total deadline exceeded",
                retryable=True,
                code="total_timeout",
                partial=emitted,
                failure_kind=ProviderFailureKind.TRANSIENT,
            )
        finally:
            if owned:
                await client.aclose()

    async def _attempt(
        self, client: httpx.AsyncClient, request: ChatRequest
    ) -> AsyncIterator[ModelEvent]:
        emitted = False
        tools: dict[tuple[int, int], dict[str, Any]] = {}
        dsml_text: list[str] = []
        dsml_enabled = self.config.model.lower().startswith("deepseek-v4") and bool(request.tools)
        _, reasoning_disabled_for_replay = self._effective_reasoning(request)
        if reasoning_disabled_for_replay:
            yield ModelWarning(
                code="reasoning_replay_incomplete",
                message=(
                    "thinking was disabled for this request because the retained tool-enabled "
                    "transcript contains an assistant message without reasoning_content"
                ),
            )
        finish, done, refused = None, False, False
        timeout = httpx.Timeout(
            connect=self.config.connect_timeout,
            read=self.config.read_timeout,
            write=self.config.connect_timeout,
            pool=self.config.connect_timeout,
        )
        try:
            async with client.stream(
                "POST",
                self._endpoint,
                headers=self._headers,
                json=self._payload(request),
                timeout=timeout,
            ) as response:
                await self._request_gate.observe_quota(response.headers)
                if response.is_error:
                    body = (await response.aread()).decode(errors="replace")
                    retry = _retry_after(response.headers.get("Retry-After"))
                    code, failure_kind, retryable = _classify_http_failure(
                        response.status_code, body
                    )
                    yield ModelError(
                        message=self._redact(
                            f"provider returned HTTP {response.status_code}: {body}"
                        ),
                        retryable=retryable,
                        code=code,
                        retry_after_seconds=retry,
                        failure_kind=failure_kind,
                    )
                    return
                lines = response.aiter_lines().__aiter__()
                received_output = False
                while True:
                    try:
                        if received_output:
                            line = await asyncio.wait_for(
                                lines.__anext__(), timeout=self.config.partial_read_timeout
                            )
                        else:
                            line = await lines.__anext__()
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        yield ModelError(
                            message="provider stopped producing events after partial output",
                            retryable=True,
                            code="read_timeout",
                            partial=True,
                            failure_kind=ProviderFailureKind.TRANSIENT,
                        )
                        return
                    try:
                        chunk = self._parse_stream_line(line)
                    except ProviderError as exc:
                        yield ModelError(
                            message=str(exc),
                            code="malformed_stream",
                            partial=emitted,
                            failure_kind=ProviderFailureKind.FORMATTING,
                        )
                        return
                    if chunk is _DONE:
                        done = True
                        break
                    if chunk is None:
                        continue
                    assert isinstance(chunk, dict)
                    received_output = True
                    refused = self._observe_refusal(chunk, refused)
                    chunk_events: list[ModelEvent] = []
                    if self._consume(chunk, chunk_events, tools):
                        for event in chunk_events:
                            if dsml_enabled and isinstance(event, TextDelta):
                                dsml_text.append(event.text)
                                continue
                            emitted |= isinstance(event, (TextDelta, ReasoningDelta, ToolCall))
                            yield event
                        return
                    for event in chunk_events:
                        if dsml_enabled and isinstance(event, TextDelta):
                            dsml_text.append(event.text)
                            continue
                        emitted |= isinstance(event, (TextDelta, ReasoningDelta, ToolCall))
                        yield event
                    if (reason := self._finish_reason(chunk)) is not None:
                        finish = reason
                    if self._is_terminal_chunk(chunk):
                        done = True
                        break
            if not done and not self._stream_end_is_terminal():
                yield ModelError(
                    message="provider stream ended before [DONE]",
                    code="incomplete_stream",
                    retryable=True,
                    partial=emitted,
                    failure_kind=ProviderFailureKind.TRANSIENT,
                )
                return
            if dsml_text:
                buffered_text = "".join(dsml_text)
                normalized_text = buffered_text.replace(_DSML_DOUBLED, _DSML)
                marker_positions = [
                    position
                    for marker in (f"<{_DSML}tool_calls>", f"<{_DSML}invoke")
                    if (position := normalized_text.find(marker)) >= 0
                ]
                marker_position = min(marker_positions) if marker_positions else -1
                plain_prefix = (
                    normalized_text[:marker_position] if marker_position >= 0 else normalized_text
                )
                dsml_candidate = (
                    normalized_text[marker_position:] if marker_position >= 0 else normalized_text
                )
                dsml_calls = _decode_deepseek_dsml(
                    dsml_candidate,
                    allowed_tools=frozenset(
                        str(item.get("function", {}).get("name", "")) for item in request.tools
                    ),
                    max_calls=self.config.max_tool_calls,
                    max_depth=self.config.max_tool_argument_depth,
                )
                if dsml_calls is not None:
                    if tools:
                        yield ModelError(
                            message="provider mixed native and DSML tool calls",
                            code="ambiguous_tool_call",
                            failure_kind=ProviderFailureKind.FORMATTING,
                        )
                        return
                    if plain_prefix:
                        emitted = True
                        yield TextDelta(text=plain_prefix)
                    for event in dsml_calls:
                        emitted = True
                        yield event
                elif marker_position >= 0:
                    yield ModelError(
                        message="provider returned malformed DeepSeek DSML tool arguments",
                        code="malformed_tool_call",
                        retryable=True,
                        failure_kind=ProviderFailureKind.FORMATTING,
                    )
                    return
                else:
                    emitted = True
                    yield TextDelta(text=buffered_text)
            terminal_events: list[ModelEvent] = []
            if self._finish_tools(
                terminal_events,
                tools,
                partial=emitted,
                finish_reason=finish,
            ):
                for event in terminal_events:
                    yield event
                return
            for event in terminal_events:
                emitted |= isinstance(event, ToolCall)
                yield event
            disposition = (
                "truncated"
                if finish == "length"
                else "refused"
                if refused or finish in {"content_filter", "refusal"}
                else "completed"
            )
            if disposition == "truncated":
                yield ModelWarning(code="truncated", message="provider stopped at its output limit")
            yield ModelCompleted(finish_reason=finish, disposition=disposition)
        except httpx.TimeoutException as exc:
            timeout_codes = (
                (httpx.ConnectTimeout, "connect_timeout"),
                (httpx.ReadTimeout, "read_timeout"),
                (httpx.WriteTimeout, "write_timeout"),
                (httpx.PoolTimeout, "pool_timeout"),
            )
            code = next(
                (candidate for kind, candidate in timeout_codes if isinstance(exc, kind)),
                "timeout",
            )
            subtype = type(exc).__name__
            detail = str(exc).strip()
            cause = exc.__cause__
            diagnostic = f"provider {code.replace('_', ' ')} ({subtype})"
            if detail:
                diagnostic += f": {detail}"
            if cause is not None:
                diagnostic += f" [cause: {type(cause).__name__}]"
            yield ModelError(
                message=self._redact(diagnostic),
                retryable=True,
                code=code,
                partial=emitted,
                failure_kind=ProviderFailureKind.TRANSIENT,
            )
        except httpx.HTTPError as exc:
            yield ModelError(
                message=self._redact(f"provider request failed: {exc}"),
                retryable=True,
                code="transport_error",
                partial=emitted,
                failure_kind=ProviderFailureKind.TRANSIENT,
            )
        except (OSError, ProviderError) as exc:
            yield ModelError(
                message=self._redact(f"provider attachment rejected: {exc}"),
                retryable=False,
                code="invalid_attachment",
                partial=emitted,
                failure_kind=ProviderFailureKind.CONFIGURATION,
            )

    def _consume(
        self,
        chunk: dict[str, Any],
        events: list[ModelEvent],
        tools: dict[tuple[int, int], dict[str, Any]],
    ) -> ModelError | None:
        if "error" in chunk:
            raw = chunk["error"]
            msg = (
                raw.get("message", "unknown provider error")
                if isinstance(raw, Mapping)
                else str(raw)
            )
            code, failure_kind, retryable = _classify_payload_failure(raw)
            event = ModelError(
                message=self._redact(msg),
                retryable=retryable,
                code=code,
                partial=bool(events),
                failure_kind=failure_kind,
            )
            events.append(event)
            return event
        if usage := chunk.get("usage"):
            request_id = chunk.get("id")
            event = self._usage(
                usage, request_id=str(request_id) if request_id is not None else None
            )
            events.append(event)
            if error := self._budget_error(event):
                events.append(error)
                return error
        choices = chunk.get("choices") or []
        if not choices:
            return None
        delta = choices[0].get("delta") or {}
        if content := delta.get("content"):
            events.append(TextDelta(text=str(content)))
        if reasoning := delta.get("reasoning_content") or delta.get("reasoning"):
            events.append(ReasoningDelta(text=str(reasoning)))
        if refusal := delta.get("refusal"):
            events.append(ModelWarning(code="refusal", message=str(refusal)))
        for part in delta.get("tool_calls") or []:
            index = int(part.get("index", 0))
            function = part.get("function") or {}
            incoming_id = str(part.get("id") or "")
            incoming_name = str(function.get("name") or "")
            same_slot = sorted(key for key in tools if key[0] == index)
            key = same_slot[-1] if same_slot else (index, 0)
            for candidate in same_slot:
                if incoming_id and tools[candidate]["call_id"] == incoming_id:
                    key = candidate
                    break
            else:
                current = tools.get(key)
                starts_new_call = bool(
                    current
                    and (
                        incoming_id
                        and current["call_id"]
                        and incoming_id != current["call_id"]
                        and incoming_name
                        or incoming_name
                        and current["name"]
                        and incoming_name != current["name"]
                    )
                )
                if starts_new_call:
                    key = (index, key[1] + 1)
            if key not in tools and len(tools) >= self.config.max_tool_calls:
                event = ModelError(
                    message="provider exceeded tool-call count limit",
                    code="tool_call_limit",
                    partial=bool(events),
                    failure_kind=ProviderFailureKind.CONFIGURATION,
                )
                events.append(event)
                return event
            item = tools.setdefault(
                key,
                {
                    "call_id": "",
                    "name": "",
                    "arguments": "",
                    "argument_snapshot": "",
                    "argument_overlap": "",
                    "argument_chunk_count": 0,
                    "argument_chunks": [],
                    "argument_assembly": "single",
                },
            )
            item["call_id"] = _merge_streamed_scalar(str(item["call_id"]), incoming_id)
            item["name"] = _merge_streamed_scalar(str(item["name"]), incoming_name)
            raw_arguments = function.get("arguments")
            argument_chunk = (
                json.dumps(raw_arguments, separators=(",", ":"))
                if isinstance(raw_arguments, Mapping)
                else str(raw_arguments or "")
            )
            if argument_chunk:
                item["argument_chunk_count"] += 1
                if os.environ.get("MERLIN_DEBUG_PROVIDER_ARGUMENTS") == "1":
                    item["argument_chunks"].append(argument_chunk)
                assembled = str(item["arguments"])
                item["argument_snapshot"] = argument_chunk
                item["argument_overlap"] = _merge_streamed_overlap(
                    str(item["argument_overlap"]), argument_chunk
                )
                if assembled and argument_chunk.startswith(assembled):
                    # An OpenAI-compatible provider may stream complete cumulative snapshots.
                    item["arguments"] = argument_chunk
                    item["argument_assembly"] = "cumulative"
                else:
                    item["arguments"] = assembled + argument_chunk
                    if item["argument_chunk_count"] > 1:
                        item["argument_assembly"] = "fragments"
            if (
                max(
                    len(str(item[key]).encode())
                    for key in ("arguments", "argument_snapshot", "argument_overlap")
                )
                > self.config.max_tool_argument_bytes
            ):
                event = ModelError(
                    message="provider tool arguments exceed byte limit",
                    code="tool_argument_limit",
                    partial=bool(events),
                    failure_kind=ProviderFailureKind.CONFIGURATION,
                )
                events.append(event)
                return event
        return None

    def _finish_tools(
        self,
        events: list[ModelEvent],
        tools: dict[tuple[int, int], dict[str, Any]],
        *,
        partial: bool = False,
        finish_reason: str | None = None,
    ) -> ModelError | None:
        completed: list[tuple[str, str, dict[str, object]]] = []
        for slot, item in tools.items():
            if not item["name"].strip():
                event = ModelError(
                    message="provider returned tool call without a function name",
                    code="malformed_tool_call",
                    retryable=True,
                    partial=partial,
                    tool_name=str(item["name"]).strip() or None,
                    failure_kind=ProviderFailureKind.FORMATTING,
                )
                events.append(event)
                return event
            argument_candidates = {
                "fragments": str(item["arguments"] or "{}"),
                "snapshot": str(item.get("argument_snapshot") or "{}"),
                "overlap": str(item.get("argument_overlap") or "{}"),
            }
            chunk_count = int(item.get("argument_chunk_count", 0))
            primary = _decode_tool_argument_object(
                argument_candidates["fragments"],
                max_depth=self.config.max_tool_argument_depth,
            )
            valid: dict[str, tuple[dict[str, object], str]] = {}
            if primary is not None:
                identity = json.dumps(primary, sort_keys=True, separators=(",", ":"))
                valid[identity] = (primary, "fragments")
            else:
                recovery_candidates = {
                    key: value for key, value in argument_candidates.items() if key != "fragments"
                }
                for assembly, argument_text in recovery_candidates.items():
                    candidate = _decode_tool_argument_object(
                        argument_text,
                        max_depth=self.config.max_tool_argument_depth,
                    )
                    if candidate is not None:
                        identity = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
                        valid.setdefault(identity, (candidate, assembly))
            if len(valid) != 1:
                argument_text = argument_candidates["fragments"]
                assembly = str(item.get("argument_assembly", "single"))
                truncated = finish_reason == "length"
                event = ModelError(
                    message=(
                        (
                            "provider truncated tool arguments at its output limit "
                            if truncated
                            else "provider returned malformed tool arguments "
                        )
                        + f"(assembly={assembly}, chunks={chunk_count}, "
                        f"bytes={len(argument_text.encode())}, "
                        f"digest={hashlib.sha256(argument_text.encode()).hexdigest()[:12]})"
                        + (
                            " debug_chunks="
                            + self._redact(
                                json.dumps(item.get("argument_chunks", []), ensure_ascii=True)
                            )
                            if os.environ.get("MERLIN_DEBUG_PROVIDER_ARGUMENTS") == "1"
                            else ""
                        )
                    ),
                    code="truncated_tool_call" if truncated else "malformed_tool_call",
                    retryable=True,
                    partial=partial,
                    tool_name=str(item["name"]).strip() or None,
                    failure_kind=(
                        ProviderFailureKind.TRUNCATED
                        if truncated
                        else ProviderFailureKind.FORMATTING
                    ),
                )
                events.append(event)
                return event
            arguments, _assembly = next(iter(valid.values()))
            call_id = str(item["call_id"]).strip()
            if not call_id:
                identity = json.dumps(
                    [slot[0], slot[1], item["name"], arguments],
                    sort_keys=True,
                    separators=(",", ":"),
                )
                call_id = "call_local_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            completed.append((call_id, str(item["name"]), arguments))
        seen_ids: dict[str, tuple[str, dict[str, object]]] = {}
        for call_id, name, arguments in completed:
            prior = seen_ids.get(call_id)
            if prior is not None:
                classification = "conflicting" if prior != (name, arguments) else "duplicate"
                event = ModelError(
                    message=f"provider returned {classification} tool call id",
                    code="ambiguous_tool_call",
                    retryable=True,
                    partial=partial,
                    tool_name=name,
                    failure_kind=ProviderFailureKind.FORMATTING,
                )
                events.append(event)
                return event
            seen_ids[call_id] = (name, arguments)
        for call_id, name, arguments in completed:
            events.append(ToolCall(call_id=call_id, name=name, arguments=arguments))
        return None

    def _usage(
        self,
        value: Mapping[str, Any],
        *,
        request_id: str | None = None,
        uncached_input_tokens: int | None = None,
        cache_read_input_tokens: int | None = None,
        cache_creation_input_tokens: int = 0,
        reasoning_output_tokens: int | None = None,
    ) -> ModelUsage:
        incoming = int(value.get("prompt_tokens") or value.get("input_tokens") or 0)
        outgoing = int(value.get("completion_tokens") or value.get("output_tokens") or 0)
        total = int(value.get("total_tokens") or incoming + outgoing)
        prompt_details = value.get("prompt_tokens_details") or value.get("input_tokens_details")
        if cache_read_input_tokens is None and isinstance(prompt_details, Mapping):
            cached = prompt_details.get("cached_tokens")
            if cached is not None:
                cache_read_input_tokens = int(cached)
        completion_details = value.get("completion_tokens_details") or value.get(
            "output_tokens_details"
        )
        if reasoning_output_tokens is None and isinstance(completion_details, Mapping):
            reasoning = completion_details.get("reasoning_tokens")
            if reasoning is not None:
                reasoning_output_tokens = int(reasoning)
        cache_read = cache_read_input_tokens or 0
        uncached = (
            uncached_input_tokens
            if uncached_input_tokens is not None
            else max(0, incoming - cache_read - cache_creation_input_tokens)
        )
        cost = None
        if (
            self.config.input_cost_per_million is not None
            and self.config.output_cost_per_million is not None
        ):
            # Cache-read rate unknown: bill cache reads at the full input rate,
            # a documented upper bound rather than an under-estimate.
            cache_read_rate = self.config.cache_read_input_cost_per_million
            cost = (
                (uncached + cache_creation_input_tokens) * self.config.input_cost_per_million
                + cache_read
                * (
                    cache_read_rate
                    if cache_read_rate is not None
                    else self.config.input_cost_per_million
                )
                + outgoing * self.config.output_cost_per_million
            ) / 1_000_000
        return ModelUsage(
            input_tokens=incoming,
            output_tokens=outgoing,
            total_tokens=total,
            estimated_cost_usd=cost,
            reasoning_output_tokens=reasoning_output_tokens or 0,
            uncached_input_tokens=uncached,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_hit=cache_read > 0,
            cache_created=cache_creation_input_tokens > 0,
            cache_miss=cache_read == 0 and uncached > 0,
            request_id=request_id,
        )

    def _preflight_budget(self, request: ChatRequest) -> ModelError | None:
        if (
            self.config.max_tokens is not None
            and self._settled_tokens + (request.max_output_tokens or 0) > self.config.max_tokens
        ):
            return ModelError(
                message=(
                    "cumulative instance token budget exhausted "
                    "(ProviderConfig.max_tokens); this is not the per-request output cap — "
                    "use default_max_output_tokens for that"
                ),
                code="budget_exhausted",
                failure_kind=ProviderFailureKind.BUDGET,
            )
        return None

    def _budget_error(self, usage: ModelUsage) -> ModelError | None:
        token_over = (
            self.config.max_tokens is not None
            and self._settled_tokens + usage.total_tokens > self.config.max_tokens
        )
        cost_over = (
            self.config.max_cost_usd is not None
            and self._settled_cost + (usage.estimated_cost_usd or 0) > self.config.max_cost_usd
        )
        return (
            ModelError(
                message=(
                    "cumulative instance budget exhausted (ProviderConfig.max_tokens / "
                    "max_cost_usd); this is not the per-request output cap — use "
                    "default_max_output_tokens for that"
                ),
                code="budget_exhausted",
                partial=True,
                failure_kind=ProviderFailureKind.BUDGET,
            )
            if token_over or cost_over
            else None
        )

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self.config.retry_max_seconds)
        return random.uniform(
            0,
            min(self.config.retry_max_seconds, self.config.retry_base_seconds * 2 ** (attempt - 1)),
        )

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    @property
    def _endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    def _parse_stream_line(self, line: str) -> dict[str, Any] | object | None:
        return _parse_sse_line(line)

    def _is_terminal_chunk(self, chunk: Mapping[str, Any]) -> bool:
        """Whether this chunk ends the stream in place of a ``[DONE]`` sentinel."""
        return False

    def _stream_end_is_terminal(self) -> bool:
        """Whether a clean stream EOF without a terminal event is a complete stream."""
        return False

    def _finish_reason(self, chunk: Mapping[str, Any]) -> str | None:
        choices = chunk.get("choices") or []
        if choices and choices[0].get("finish_reason") is not None:
            return str(choices[0]["finish_reason"])
        return None

    def _observe_refusal(self, chunk: Mapping[str, Any], refused: bool) -> bool:
        choices = chunk.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            return refused or bool(delta.get("refusal"))
        return refused

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        if any(message.image_attachments for message in request.messages) and not (
            self._capabilities.vision
        ):
            raise ProviderError("provider vision capability is not enabled")
        payload = {
            "model": request.model or self.config.model,
            "messages": [_message_payload(x) for x in request.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            payload["tools"] = [_plain(x) for x in request.tools]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if self.config.reasoning_parameter == "thinking":
            reasoning, _ = self._effective_reasoning(request)
            if reasoning is not None:
                payload["thinking"] = {"type": reasoning}
        payload[self.config.output_token_parameter] = (
            request.max_output_tokens
            if request.max_output_tokens is not None
            else self.config.default_max_output_tokens
        )
        return payload

    def _effective_reasoning(self, request: ChatRequest) -> tuple[str | None, bool]:
        reasoning = request.reasoning
        if self.config.reasoning_parameter != "thinking":
            return reasoning, False
        requires_replay = self.config.effective_requires_reasoning_content_for_tool_calls
        if reasoning is None and (self.config.reasoning_effort != "default" or requires_replay):
            reasoning = "enabled"
        incomplete_replay = (
            reasoning == "enabled"
            and bool(request.tools)
            and requires_replay
            and any(
                message.role == "assistant" and message.reasoning_content is None
                for message in request.messages
            )
        )
        if incomplete_replay:
            return "disabled", True
        return reasoning, False

    def _redact(self, message: str) -> str:
        return (
            message.replace(self.config.api_key, "[REDACTED]") if self.config.api_key else message
        )


_DONE = object()


def _provider_error_identity(value: object) -> frozenset[str]:
    if not isinstance(value, Mapping):
        return frozenset()
    raw = value.get("error", value)
    if not isinstance(raw, Mapping):
        return frozenset()
    return frozenset(
        text
        for field in ("code", "status", "type")
        if (text := str(raw.get(field) or "").strip().casefold())
    )


def _classify_http_failure(status_code: int, body: str) -> tuple[str, ProviderFailureKind, bool]:
    try:
        identities = _provider_error_identity(json.loads(body))
    except (TypeError, ValueError):
        identities = frozenset()
    quota_identities = {
        "billing_hard_limit_reached",
        "insufficient_quota",
        "quota_exceeded",
        "usage_limit_reached",
    }
    if status_code == 401:
        return "provider_auth_error", ProviderFailureKind.AUTHORIZATION, False
    if status_code == 402 or identities & quota_identities:
        return "provider_quota_exhausted", ProviderFailureKind.QUOTA, False
    if status_code == 403:
        return "provider_permission_denied", ProviderFailureKind.PERMISSION, False
    if status_code in {408, 409, 425, 429} or status_code >= 500:
        code = "rate_limited" if status_code == 429 else "provider_transient_http"
        return code, ProviderFailureKind.TRANSIENT, True
    if 400 <= status_code < 500:
        return "provider_configuration_error", ProviderFailureKind.CONFIGURATION, False
    return "provider_error", ProviderFailureKind.UNKNOWN, False


def _classify_payload_failure(
    value: object,
) -> tuple[str, ProviderFailureKind, bool]:
    identities = _provider_error_identity(value)
    if identities & {
        "billing_hard_limit_reached",
        "billing_error",
        "insufficient_quota",
        "quota_exceeded",
        "resource_exhausted",
        "usage_limit_reached",
    }:
        return "provider_quota_exhausted", ProviderFailureKind.QUOTA, False
    if identities & {
        "authentication_error",
        "invalid_api_key",
        "unauthenticated",
        "unauthorized",
    }:
        return "provider_auth_error", ProviderFailureKind.AUTHORIZATION, False
    if identities & {"forbidden", "permission_denied", "permission_error"}:
        return "provider_permission_denied", ProviderFailureKind.PERMISSION, False
    if identities & {
        "deadline_exceeded",
        "internal",
        "overloaded_error",
        "rate_limit_error",
        "rate_limited",
        "server_error",
        "unavailable",
    }:
        code = (
            "rate_limited"
            if identities & {"rate_limit_error", "rate_limited"}
            else "provider_transient_error"
        )
        return code, ProviderFailureKind.TRANSIENT, True
    if identities & {
        "context_length_exceeded",
        "invalid_argument",
        "invalid_request_error",
        "model_not_found",
        "not_found",
        "-32600",
        "-32601",
        "-32602",
    }:
        return "provider_configuration_error", ProviderFailureKind.CONFIGURATION, False
    if "-32603" in identities:
        return "provider_transient_error", ProviderFailureKind.TRANSIENT, True
    return "provider_error", ProviderFailureKind.UNKNOWN, False


def _admission_group(budget_id: str | None) -> str:
    if not budget_id:
        return "default"
    if budget_id.startswith("campaign/"):
        parts = budget_id.split("/", 2)
        if len(parts) >= 2 and parts[1]:
            return f"campaign/{parts[1]}"
    return budget_id


def _plain(value: object) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    dump = getattr(value, "model_dump", None)
    return dump(exclude_none=True, exclude_defaults=True) if callable(dump) else value


def _verified_image_data(attachments: Any) -> list[tuple[str, str]]:
    """Return (media_type, base64) pairs for digest- and size-verified attachments."""
    verified: list[tuple[str, str]] = []
    for attachment in attachments:
        path = Path(str(attachment["path"]))
        data = path.read_bytes()
        if len(data) != int(attachment["byte_length"]) or hashlib.sha256(data).hexdigest() != str(
            attachment["sha256"]
        ):
            raise ProviderError("image attachment failed size or digest verification")
        verified.append((str(attachment["media_type"]), base64.b64encode(data).decode("ascii")))
    return verified


def _message_payload(message: object) -> dict[str, Any]:
    payload = _plain(message)
    assert isinstance(payload, dict)
    attachments = payload.pop("image_attachments", ())
    if not attachments:
        return payload
    content: list[dict[str, object]] = [{"type": "text", "text": str(payload["content"])}]
    for media_type, encoded in _verified_image_data(attachments):
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{encoded}"},
            }
        )
    payload["content"] = content
    return payload


def _parse_sse_line(line: str) -> dict[str, Any] | object | None:
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if line == "data: [DONE]":
        return _DONE
    if not line.startswith("data:"):
        return None
    try:
        chunk = json.loads(line.removeprefix("data:").strip())
    except json.JSONDecodeError as exc:
        raise ProviderError("provider returned invalid JSON in event stream") from exc
    if not isinstance(chunk, dict):
        raise ProviderError("provider returned a non-object event")
    return chunk


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return max(0.0, (when - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError):
            return None


def _first_header(headers: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = headers.get(name)
        if value is not None:
            return value
    return None


def _nonnegative_int(value: str | None) -> int | None:
    try:
        parsed = int(float(value)) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed is not None and parsed >= 0 else None


def _quota_reset_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    match = re.fullmatch(
        r"(?:(?P<hours>\d+(?:\.\d+)?)h)?"
        r"(?:(?P<minutes>\d+(?:\.\d+)?)m)?"
        r"(?:(?P<seconds>\d+(?:\.\d+)?)s)?"
        r"(?:(?P<milliseconds>\d+(?:\.\d+)?)ms)?",
        value.strip().casefold(),
    )
    if match is None or not any(match.groupdict().values()):
        return None
    parts = {key: float(item or 0) for key, item in match.groupdict().items()}
    return (
        parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
        + parts["milliseconds"] / 1000
    )


def _json_depth(value: object, depth: int = 0) -> int:
    children = (
        value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    )
    return max((_json_depth(item, depth + 1) for item in children), default=depth)
