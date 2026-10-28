"""Streaming client for the native Anthropic Messages API (``POST /v1/messages``)."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from merlin_contracts import (
    ChatMessage,
    ChatRequest,
    ModelError,
    ModelEvent,
    ReasoningDelta,
    TextDelta,
)

from .openai_compatible import (
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderError,
    _classify_payload_failure,
    _merge_streamed_scalar,
    _plain,
)

__all__ = ["AnthropicMessagesConfig", "AnthropicMessagesProvider"]

_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "refusal": "refusal",
    "pause_turn": "stop",
}


def _supports_adaptive_thinking(model: str) -> bool:
    match = re.search(r"claude-(?:opus|sonnet)[.-](\d+)[.-](\d{1,2})(?!\d)", model.lower())
    return bool(match and (int(match.group(1)), int(match.group(2))) >= (4, 6))


def _is_opus_4_7(model: str) -> bool:
    return bool(re.search(r"opus[.-]4[.-]7(?!\d)", model.lower()))


def _reasoning_payload(
    *, model: str, effort: str, max_tokens: int, fallback_budget: int | None
) -> dict[str, Any]:
    """Translate effort using Anthropic's model-specific native protocol."""
    if _supports_adaptive_thinking(model):
        effective = "low" if effort == "minimal" else effort
        if effective == "xhigh" and not _is_opus_4_7(model):
            effective = "high"
        result: dict[str, Any] = {"thinking": {"type": "adaptive", "display": "summarized"}}
        if effort != "default":
            result["output_config"] = {"effort": effective}
        return result

    # Pre-4.6 Claude exposes only an exact token budget. A categorical effort
    # cannot be translated without inventing a budget, so require an explicit
    # operator value instead.
    if effort != "default":
        raise ProviderError(
            f"model {model!r} does not support categorical reasoning effort; "
            "use provider default or configure an exact thinking_budget_tokens value"
        )
    if fallback_budget is None:
        raise ProviderError(
            f"model {model!r} requires an explicit thinking_budget_tokens value "
            "when reasoning is enabled"
        )
    if fallback_budget < 1_024 or fallback_budget >= max_tokens:
        raise ProviderError(
            "thinking_budget_tokens must be at least 1024 and lower than max_tokens"
        )
    return {
        "thinking": {
            "type": "enabled",
            "budget_tokens": fallback_budget,
        }
    }


@dataclass(frozen=True, slots=True)
class AnthropicMessagesConfig(ProviderConfig):
    """Anthropic Messages route configuration.

    ``thinking_budget_tokens`` is an optional exact operator/provider value for
    legacy models whose native protocol has no categorical effort control.
    """

    anthropic_version: str = "2023-06-01"
    thinking_budget_tokens: int | None = None

    def __post_init__(self) -> None:
        ProviderConfig.__post_init__(self)
        if self.thinking_budget_tokens is not None and self.thinking_budget_tokens < 1_024:
            raise ValueError("thinking_budget_tokens must be at least 1024")


class AnthropicMessagesProvider(OpenAICompatibleProvider):
    _transport = "anthropic_messages"

    def __init__(self, config: AnthropicMessagesConfig, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        self.config: AnthropicMessagesConfig = config
        self._thinking_signatures: dict[str, str] = {}
        self._pending_input_tokens = 0
        self._pending_request_id: str | None = None
        self._pending_cache_details: dict[str, int] = {}
        self._thinking_buffers: dict[int, str] = {}
        self._terminal_finish: str | None = None
        self.usage_details: dict[str, int] = {}

    @property
    def _endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        return base if base.endswith("/messages") else f"{base}/messages"

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
        if self.config.api_key:
            headers["x-api-key"] = self.config.api_key
        headers["anthropic-version"] = self.config.anthropic_version
        return headers

    def _is_terminal_chunk(self, chunk: Mapping[str, Any]) -> bool:
        return chunk.get("type") == "message_stop"

    def _finish_reason(self, chunk: Mapping[str, Any]) -> str | None:
        finish, self._terminal_finish = self._terminal_finish, None
        return finish

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        if any(message.image_attachments for message in request.messages):
            raise ProviderError("image attachments are not supported by this transport")
        self._pending_input_tokens = 0
        self._pending_request_id = None
        self._pending_cache_details = {}
        self._thinking_buffers = {}
        system_messages = [
            message.content for message in request.messages if message.role == "system"
        ]
        max_tokens = (
            request.max_output_tokens
            if request.max_output_tokens is not None
            else self.config.default_max_output_tokens
        )
        payload: dict[str, Any] = {
            "model": request.model or self.config.model,
            "messages": _messages_payload(request.messages, self._thinking_signatures),
            "max_tokens": max_tokens,
            "stream": True,
        }
        if system_messages:
            # Keep the immutable worker identity independently cacheable. A
            # changing deterministic compaction summary is a later system block
            # and cannot invalidate this first breakpoint.
            payload["system"] = [
                {
                    "type": "text",
                    "text": system_messages[0],
                    "cache_control": {"type": "ephemeral"},
                },
                *({"type": "text", "text": content} for content in system_messages[1:]),
            ]
        if request.tools:
            tools = [_tool_payload(tool) for tool in request.tools]
            tools[-1]["cache_control"] = {"type": "ephemeral"}
            payload["tools"] = tools
        if request.temperature is not None and request.reasoning != "enabled":
            payload["temperature"] = request.temperature
        effort = request.reasoning_effort or self.config.reasoning_effort
        if request.reasoning != "disabled" and (
            request.reasoning == "enabled" or effort != "default"
        ):
            payload.update(
                _reasoning_payload(
                    model=request.model or self.config.model,
                    effort=effort,
                    max_tokens=max_tokens,
                    fallback_budget=self.config.thinking_budget_tokens,
                )
            )
        return payload

    def _consume(
        self,
        chunk: dict[str, Any],
        events: list[ModelEvent],
        tools: dict[tuple[int, int], dict[str, Any]],
    ) -> ModelError | None:
        event_type = str(chunk.get("type") or "")
        if event_type == "error":
            raw = chunk.get("error") or {}
            msg = (
                raw.get("message", "unknown provider error")
                if isinstance(raw, Mapping)
                else str(raw)
            )
            code, failure_kind, retryable = _classify_payload_failure(raw)
            event = ModelError(
                message=self._redact(msg),
                code=code,
                retryable=retryable,
                partial=bool(events),
                failure_kind=failure_kind,
            )
            events.append(event)
            return event
        if event_type == "message_start":
            message = chunk.get("message") or {}
            if isinstance(message, Mapping) and message.get("id"):
                self._pending_request_id = str(message["id"])
            usage = message.get("usage") if isinstance(message, Mapping) else None
            if isinstance(usage, Mapping):
                self._pending_input_tokens = int(usage.get("input_tokens") or 0)
                details: dict[str, int] = {}
                if usage.get("cache_read_input_tokens") is not None:
                    details["cache_read_input_tokens"] = int(usage["cache_read_input_tokens"])
                if usage.get("cache_creation_input_tokens") is not None:
                    details["cache_creation_input_tokens"] = int(
                        usage["cache_creation_input_tokens"]
                    )
                self._pending_cache_details = details
            return None
        if event_type == "content_block_start":
            block = chunk.get("content_block") or {}
            if not isinstance(block, Mapping):
                return None
            index = int(chunk.get("index") or 0)
            block_type = block.get("type")
            if block_type == "thinking":
                self._thinking_buffers[index] = str(block.get("thinking") or "")
            elif block_type == "tool_use":
                if len(tools) >= self.config.max_tool_calls:
                    event = ModelError(
                        message="provider exceeded tool-call count limit",
                        code="tool_call_limit",
                        partial=bool(events),
                    )
                    events.append(event)
                    return event
                tools[(index, 0)] = {
                    "call_id": _merge_streamed_scalar("", str(block.get("id") or "")),
                    "name": _merge_streamed_scalar("", str(block.get("name") or "")),
                    "arguments": "",
                    "argument_snapshot": "",
                    "argument_overlap": "",
                    "argument_chunk_count": 0,
                    "argument_chunks": [],
                    "argument_assembly": "single",
                }
            return None
        if event_type == "content_block_delta":
            delta = chunk.get("delta") or {}
            if not isinstance(delta, Mapping):
                return None
            index = int(chunk.get("index") or 0)
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                events.append(TextDelta(text=str(delta.get("text") or "")))
            elif delta_type == "thinking_delta":
                thinking = str(delta.get("thinking") or "")
                self._thinking_buffers[index] = self._thinking_buffers.get(index, "") + thinking
                events.append(ReasoningDelta(text=thinking))
            elif delta_type == "signature_delta":
                signature = str(delta.get("signature") or "")
                thinking = self._thinking_buffers.get(index, "")
                if thinking and signature:
                    self._thinking_signatures[thinking] = signature
            elif delta_type == "input_json_delta":
                entry = tools.get((index, 0))
                if entry is None:
                    event = ModelError(
                        message="provider streamed arguments for an unknown tool call",
                        code="malformed_tool_call",
                        retryable=True,
                        partial=bool(events),
                    )
                    events.append(event)
                    return event
                fragment = str(delta.get("partial_json") or "")
                entry["argument_chunk_count"] += 1
                entry["arguments"] = str(entry["arguments"]) + fragment
                entry["argument_snapshot"] = fragment
                entry["argument_overlap"] = str(entry["argument_overlap"]) + fragment
                if entry["argument_chunk_count"] > 1:
                    entry["argument_assembly"] = "fragments"
                if len(str(entry["arguments"]).encode()) > self.config.max_tool_argument_bytes:
                    event = ModelError(
                        message="provider tool arguments exceed byte limit",
                        code="tool_argument_limit",
                        partial=bool(events),
                    )
                    events.append(event)
                    return event
            return None
        if event_type == "message_delta":
            delta = chunk.get("delta") or {}
            if isinstance(delta, Mapping) and delta.get("stop_reason") is not None:
                reason = str(delta["stop_reason"])
                self._terminal_finish = _STOP_REASON_MAP.get(reason, "stop")
            usage = chunk.get("usage") or {}
            output_tokens = (
                int(usage.get("output_tokens") or 0) if isinstance(usage, Mapping) else 0
            )
            self.usage_details = dict(self._pending_cache_details)
            cache_read = self._pending_cache_details.get("cache_read_input_tokens", 0)
            cache_creation = self._pending_cache_details.get("cache_creation_input_tokens", 0)
            uncached = self._pending_input_tokens
            total_input = uncached + cache_read + cache_creation
            event = self._usage(
                {
                    "input_tokens": total_input,
                    "output_tokens": output_tokens,
                    "total_tokens": total_input + output_tokens,
                },
                request_id=self._pending_request_id,
                uncached_input_tokens=uncached,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_creation,
            )
            events.append(event)
            if error := self._budget_error(event):
                events.append(error)
                return error
            return None
        return None


def _tool_payload(tool: Mapping[str, Any]) -> dict[str, Any]:
    plain = _plain(tool)
    assert isinstance(plain, dict)
    function = plain.get("function")
    source = function if isinstance(function, Mapping) else plain
    declaration: dict[str, Any] = {"name": str(source.get("name") or "")}
    if source.get("description"):
        declaration["description"] = str(source["description"])
    declaration["input_schema"] = _sanitize_input_schema(source.get("parameters"))
    return declaration


def _sanitize_input_schema(schema: Any) -> dict[str, Any]:
    """Project a JSON Schema onto the Anthropic Messages ``input_schema`` subset.

    Anthropic rejects top-level ``oneOf``/``anyOf``/``allOf`` combinators. Object
    variants are merged (properties unioned; ``required`` intersected for
    alternatives, unioned for ``allOf``); a non-object union collapses to its
    first variant so the request stays sendable rather than failing dispatch.
    """
    plain = _plain(schema)
    if not isinstance(plain, dict):
        return {"type": "object"}
    for combiner in ("anyOf", "oneOf", "allOf"):
        variants = plain.get(combiner)
        if not isinstance(variants, list) or not variants:
            continue
        merged = _merge_object_variants(
            [variant for variant in variants if isinstance(variant, Mapping)],
            union_required=combiner == "allOf",
        )
        if merged is None:
            first = _plain(variants[0])
            merged = dict(first) if isinstance(first, dict) else {"type": "object"}
        for key, value in plain.items():
            if key in {"anyOf", "oneOf", "allOf"}:
                continue
            if key == "description" or key not in merged:
                merged[key] = value
        return merged
    return dict(plain)


def _merge_object_variants(
    variants: Sequence[Mapping[str, Any]], *, union_required: bool
) -> dict[str, Any] | None:
    if not variants or not all(
        variant.get("type") == "object" or "properties" in variant for variant in variants
    ):
        return None
    properties: dict[str, Any] = {}
    required: set[str] | None = None
    for variant in variants:
        for name, prop in (variant.get("properties") or {}).items():
            properties.setdefault(name, prop)
        variant_required = set(variant.get("required") or ())
        if required is None:
            required = variant_required
        elif union_required:
            required |= variant_required
        else:
            required &= variant_required
    merged: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        merged["required"] = sorted(required)
    additional = {variant.get("additionalProperties") for variant in variants}
    if additional == {False}:
        merged["additionalProperties"] = False
    return merged


def _messages_payload(
    messages: Sequence[ChatMessage], signatures: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Convert chat messages, folding ``tool`` results into user tool_result blocks."""
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.role
        if role == "system":
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": str(message.tool_call_id or ""),
                "content": str(message.content),
            }
            if converted and converted[-1]["role"] == "user" and converted[-1].get("_tool_results"):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block], "_tool_results": True})
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            reasoning = message.reasoning_content
            if reasoning and str(reasoning) in signatures:
                blocks.append(
                    {
                        "type": "thinking",
                        "thinking": str(reasoning),
                        "signature": signatures[str(reasoning)],
                    }
                )
            if message.content:
                blocks.append({"type": "text", "text": str(message.content)})
            for call in message.tool_calls or []:
                blocks.append(_tool_use_block(call))
            converted.append({"role": "assistant", "content": blocks})
            continue
        converted.append(
            {"role": "user", "content": [{"type": "text", "text": str(message.content)}]}
        )
    for item in converted:
        item.pop("_tool_results", None)
    return converted


def _tool_use_block(call: object) -> dict[str, Any]:
    plain = _plain(call)
    assert isinstance(plain, dict)
    function = plain.get("function")
    if isinstance(function, Mapping):
        name = str(function.get("name") or "")
        arguments = function.get("arguments")
        call_id = str(plain.get("id") or plain.get("call_id") or "")
    else:
        name = str(plain.get("name") or "")
        arguments = plain.get("arguments")
        call_id = str(plain.get("call_id") or plain.get("id") or "")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    return {
        "type": "tool_use",
        "id": call_id,
        "name": name,
        "input": arguments if isinstance(arguments, dict) else {},
    }
