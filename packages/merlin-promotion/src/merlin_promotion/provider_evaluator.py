from __future__ import annotations

import asyncio
import json
import os
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from urllib.parse import unquote, urlsplit

from merlin_contracts import (
    ChatMessage,
    ChatRequest,
    DynamicToolOutput,
    EvaluationMetrics,
    ModelCompleted,
    ModelError,
    ModelUsage,
    Provider,
    TextDelta,
    ToolCall,
)

from .service import EvaluationBinding, EvaluationCase


@dataclass(frozen=True, slots=True)
class ExpectedToolCall:
    name: str
    arguments: dict[str, object]
    output: str
    success: bool


@dataclass(frozen=True, slots=True)
class ProviderEvaluationFixture:
    messages: tuple[ChatMessage, ...]
    expected_output: str
    forbidden_outputs: tuple[str, ...]
    evidence_markers: tuple[str, ...]
    max_output_tokens: int
    require_usage: bool
    cancel_after_events: int | None = None
    recovery_messages: tuple[ChatMessage, ...] = ()
    tools: tuple[dict[str, object], ...] = ()
    expected_tool_calls: tuple[ExpectedToolCall, ...] = ()


def load_provider_evaluation_fixture(
    case: EvaluationCase, *, max_bytes: int = 1_048_576
) -> ProviderEvaluationFixture:
    parsed = urlsplit(case.target_fixture)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ValueError("provider evaluation fixture must use an absolute local file URI")
    path = Path(unquote(parsed.path))
    if not path.is_absolute():
        raise ValueError("provider evaluation fixture path must be absolute")
    payload = _read_owned_file(path, max_bytes=max_bytes)
    actual_digest = f"sha256:{sha256(payload).hexdigest()}"
    if actual_digest != case.fixture_digest:
        raise ValueError("provider evaluation fixture digest mismatch")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("provider evaluation fixture is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise TypeError("provider evaluation fixture must be an object")
    expected = {
        "schema_version",
        "messages",
        "expected_output",
        "forbidden_outputs",
        "evidence_markers",
        "max_output_tokens",
        "require_usage",
        "cancel_after_events",
        "recovery_messages",
    }
    schema_version = value.get("schema_version")
    if schema_version == "merlin.provider-evaluation.v2":
        expected |= {"tools", "expected_tool_calls"}
    elif schema_version != "merlin.provider-evaluation.v1":
        raise ValueError("provider evaluation fixture has an unexpected schema")
    if set(value) != expected:
        raise ValueError("provider evaluation fixture has an unexpected schema")
    messages = _messages(value["messages"], field="messages")
    recovery = _messages(value["recovery_messages"], field="recovery_messages", allow_empty=True)
    expected_output = value["expected_output"]
    if not isinstance(expected_output, str):
        raise TypeError("provider evaluation expected_output must be a string")
    forbidden = _string_array(value["forbidden_outputs"], "forbidden_outputs")
    markers = _string_array(value["evidence_markers"], "evidence_markers")
    maximum = value["max_output_tokens"]
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 100_000:
        raise ValueError("provider evaluation max_output_tokens must be between 1 and 100000")
    if not isinstance(value["require_usage"], bool):
        raise TypeError("provider evaluation require_usage must be boolean")
    cancel_after = value["cancel_after_events"]
    if cancel_after is not None and (
        isinstance(cancel_after, bool)
        or not isinstance(cancel_after, int)
        or not 1 <= cancel_after <= 4_096
    ):
        raise ValueError("provider evaluation cancel_after_events must be between 1 and 4096")
    if (cancel_after is None) != (not recovery):
        raise ValueError(
            "provider evaluation cancellation requires recovery_messages and vice versa"
        )
    tools = _tools(value.get("tools", []))
    expected_tool_calls = _expected_tool_calls(value.get("expected_tool_calls", []))
    if bool(tools) != bool(expected_tool_calls):
        raise ValueError(
            "provider evaluation tools and expected_tool_calls must be declared together"
        )
    if cancel_after is not None and tools:
        raise ValueError("provider evaluation cancellation fixtures cannot invoke tools")
    advertised_names: set[str] = set()
    for tool in tools:
        function = tool.get("function")
        assert isinstance(function, dict)
        name = function.get("name")
        assert isinstance(name, str)
        advertised_names.add(name)
    if any(call.name not in advertised_names for call in expected_tool_calls):
        raise ValueError("provider evaluation expected call names must be advertised tools")
    return ProviderEvaluationFixture(
        messages=messages,
        expected_output=expected_output,
        forbidden_outputs=forbidden,
        evidence_markers=markers,
        max_output_tokens=maximum,
        require_usage=value["require_usage"],
        cancel_after_events=cancel_after,
        recovery_messages=recovery,
        tools=tools,
        expected_tool_calls=expected_tool_calls,
    )


class ProviderCaseEvaluator:
    """Execute immutable bounded routing cases through the selected production provider."""

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    async def __call__(self, candidate, case: EvaluationCase, binding: EvaluationBinding):
        if candidate.kind != "routing":
            raise ValueError("provider evaluation suites accept routing candidates only")
        expected_route = {
            "provider": binding.provider,
            "model": binding.model,
            "route": binding.route,
        }
        if any(candidate.content.get(key) != value for key, value in expected_route.items()):
            raise ValueError("routing candidate does not match the evaluation binding")
        fixture = load_provider_evaluation_fixture(case)
        timeout = _budget_int(binding, "case_timeout_seconds", default=120, minimum=1, maximum=900)
        max_events = _budget_int(binding, "max_events", default=4_096, minimum=1, maximum=100_000)
        output_cap = _budget_int(
            binding,
            "max_output_tokens_per_case",
            default=fixture.max_output_tokens,
            minimum=1,
            maximum=100_000,
        )
        if fixture.max_output_tokens > output_cap:
            raise ValueError("provider evaluation fixture exceeds the suite output-token cap")
        max_chars = _budget_int(
            binding, "max_output_characters", default=1_000_000, minimum=1, maximum=10_000_000
        )
        started = perf_counter()
        async with asyncio.timeout(timeout):
            if fixture.cancel_after_events is not None:
                iterator = self._provider.stream(
                    ChatRequest(
                        model=binding.model,
                        messages=list(fixture.messages),
                        max_output_tokens=fixture.max_output_tokens,
                        temperature=0,
                        reasoning="disabled",
                        budget_id=f"evaluation:{binding.suite_id}:{case.id}:cancel",
                        workload="provider_evaluation",
                    )
                )
                closer = getattr(iterator, "aclose", None)
                if closer is None:
                    raise ValueError("provider evaluation stream does not support cancellation")
                observed = 0
                try:
                    async for event in iterator:
                        observed += 1
                        if isinstance(event, ToolCall):
                            raise ValueError(
                                "provider evaluation cancellation stream requested a tool"
                            )
                        if isinstance(event, ModelError):
                            raise ValueError(f"provider evaluation failed: {event.code}")
                        if isinstance(event, ModelCompleted):
                            raise ValueError(
                                "provider evaluation stream completed before cancellation"
                            )
                        if observed >= fixture.cancel_after_events:
                            break
                    if observed < fixture.cancel_after_events:
                        raise ValueError("provider evaluation stream ended before cancellation")
                finally:
                    await closer()
                messages = fixture.recovery_messages
            else:
                messages = fixture.messages
            output, usage, violations, completed, tool_calls_ok = await self._consume(
                ChatRequest(
                    model=binding.model,
                    messages=list(messages),
                    max_output_tokens=fixture.max_output_tokens,
                    temperature=0,
                    reasoning="disabled",
                    budget_id=f"evaluation:{binding.suite_id}:{case.id}:result",
                    workload="provider_evaluation",
                    tools=list(fixture.tools),
                ),
                max_events=max_events,
                max_chars=max_chars,
                expected_tool_calls=fixture.expected_tool_calls,
            )
        latency_ms = max(0, int((perf_counter() - started) * 1_000))
        forbidden = any(item in output for item in fixture.forbidden_outputs)
        attribution = all(item in output for item in fixture.evidence_markers)
        usage_ok = usage is not None or not fixture.require_usage
        succeeded = (
            completed
            and output == fixture.expected_output
            and not forbidden
            and usage_ok
            and violations == 0
            and tool_calls_ok
        )
        return EvaluationMetrics(
            task_success_rate=float(succeeded),
            false_positive_rate=float(forbidden),
            evidence_attribution_rate=float(attribution),
            authority_violations=violations,
            mean_cost=usage.estimated_cost_usd if usage and usage.estimated_cost_usd else 0,
            p95_latency_ms=latency_ms,
            cancellation_recovery_rate=float(completed),
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
        )

    async def _consume(
        self,
        request: ChatRequest,
        *,
        max_events: int,
        max_chars: int,
        expected_tool_calls: tuple[ExpectedToolCall, ...] = (),
    ) -> tuple[str, ModelUsage | None, int, bool, bool]:
        parts: list[str] = []
        characters = 0
        usage = None
        violations = 0
        completed = False
        events = 0
        observed_tool_calls = 0
        invalid_tool_calls = 0

        async def handle(call: ToolCall) -> DynamicToolOutput:
            nonlocal observed_tool_calls, invalid_tool_calls
            if observed_tool_calls >= len(expected_tool_calls):
                invalid_tool_calls += 1
                return DynamicToolOutput(
                    content="DENIED: unexpected evaluation tool call", success=False
                )
            expected = expected_tool_calls[observed_tool_calls]
            observed_tool_calls += 1
            if call.name != expected.name or call.arguments != expected.arguments:
                invalid_tool_calls += 1
                return DynamicToolOutput(
                    content="DENIED: evaluation tool call mismatch", success=False
                )
            return DynamicToolOutput(content=expected.output, success=expected.success)

        if request.tools:
            interactive = getattr(self._provider, "stream_with_tools", None)
            if not callable(interactive):
                raise ValueError("provider evaluation route does not support dynamic tools")
            iterator = interactive(request, handle)
        else:
            iterator = self._provider.stream(request)
        try:
            async for event in iterator:
                events += 1
                if events > max_events:
                    raise ValueError("provider evaluation stream exceeded event bound")
                if isinstance(event, TextDelta):
                    characters += len(event.text)
                    if characters > max_chars:
                        raise ValueError("provider evaluation stream exceeded output bound")
                    parts.append(event.text)
                elif isinstance(event, ModelUsage):
                    usage = event
                elif isinstance(event, ToolCall):
                    violations += 1
                elif isinstance(event, ModelError):
                    raise ValueError(f"provider evaluation failed: {event.code}")
                elif isinstance(event, ModelCompleted):
                    completed = event.disposition == "completed"
        finally:
            closer = getattr(iterator, "aclose", None)
            if closer is not None:
                await closer()
        violations += invalid_tool_calls
        tool_calls_ok = observed_tool_calls == len(expected_tool_calls) and invalid_tool_calls == 0
        return "".join(parts), usage, violations, completed, tool_calls_ok


def _tools(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or len(value) > 16:
        raise TypeError("provider evaluation tools must be an array of at most 16 functions")
    tools: list[dict[str, object]] = []
    names: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"type", "function"}:
            raise TypeError("provider evaluation tools must be exact function objects")
        function = item.get("function")
        if item.get("type") != "function" or not isinstance(function, dict):
            raise TypeError("provider evaluation tools must be exact function objects")
        if set(function) != {"name", "description", "parameters"}:
            raise TypeError("provider evaluation tool functions have an unexpected schema")
        name = function.get("name")
        description = function.get("description")
        parameters = function.get("parameters")
        if (
            not isinstance(name, str)
            or not name
            or name in names
            or not isinstance(description, str)
            or not description
            or not isinstance(parameters, dict)
        ):
            raise ValueError("provider evaluation tool definition is invalid or duplicated")
        names.add(name)
        tools.append(item)
    return tuple(tools)


def _expected_tool_calls(value: object) -> tuple[ExpectedToolCall, ...]:
    if not isinstance(value, list) or len(value) > 16:
        raise TypeError("provider evaluation expected_tool_calls must contain at most 16 calls")
    calls: list[ExpectedToolCall] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "arguments", "output", "success"}:
            raise TypeError("provider evaluation expected tool call has an unexpected schema")
        name = item.get("name")
        arguments = item.get("arguments")
        output = item.get("output")
        success = item.get("success")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(arguments, dict)
            or not isinstance(output, str)
            or not isinstance(success, bool)
        ):
            raise TypeError("provider evaluation expected tool call is invalid")
        calls.append(ExpectedToolCall(name, arguments, output, success))
    return tuple(calls)


def _read_owned_file(path: Path, *, max_bytes: int) -> bytes:
    if path.is_symlink():
        raise ValueError("provider evaluation fixture must not be a symlink")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise ValueError("provider evaluation fixture is unavailable") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("provider evaluation fixture must be a regular file")
        if details.st_uid != os.getuid():
            raise PermissionError("provider evaluation fixture must be owned by the current user")
        if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError("provider evaluation fixture must not be group/world writable")
        if details.st_size > max_bytes:
            raise ValueError("provider evaluation fixture exceeds the size bound")
        chunks = bytearray()
        while len(chunks) <= max_bytes:
            chunk = os.read(descriptor, min(65_536, max_bytes + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > max_bytes:
            raise ValueError("provider evaluation fixture exceeds the size bound")
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _messages(value: object, *, field: str, allow_empty: bool = False) -> tuple[ChatMessage, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise TypeError(f"provider evaluation {field} must be a non-empty array")
    return tuple(ChatMessage.model_validate(item) for item in value)


def _string_array(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise TypeError(f"provider evaluation {field} must be a string array")
    return tuple(value)


def _budget_int(
    binding: EvaluationBinding, key: str, *, default: int, minimum: int, maximum: int
) -> int:
    value = binding.budget.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise TypeError(f"evaluation budget {key} must be an integer")
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"evaluation budget {key} must be between {minimum} and {maximum}")
    return result
