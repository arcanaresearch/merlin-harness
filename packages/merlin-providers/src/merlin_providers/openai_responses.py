"""Streaming client for the native OpenAI Responses API (``POST /v1/responses``)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from merlin_contracts import (
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
    _verified_image_data,
)

__all__ = ["OpenAIResponsesConfig", "OpenAIResponsesProvider"]

_TERMINAL_EVENTS = {"response.completed", "response.failed", "response.incomplete"}


@dataclass(frozen=True, slots=True)
class OpenAIResponsesConfig(ProviderConfig):
    """Responses-API route configuration.

    ``previous_response_continuation`` opts into server-side conversation state:
    once a response completes, later requests send its ID as
    ``previous_response_id`` and only the input items produced after the last
    assistant turn. ``store`` is left at the provider default.
    """

    previous_response_continuation: bool = False
    reasoning_effort: Literal["default", "minimal", "low", "medium", "high", "xhigh"] = "default"


class OpenAIResponsesProvider(OpenAICompatibleProvider):
    _transport = "openai_responses"

    def __init__(self, config: OpenAIResponsesConfig, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        self.config: OpenAIResponsesConfig = config
        self._last_response_id: str | None = None
        self._terminal_finish: str | None = None
        self.usage_details: dict[str, int] = {}

    @property
    def last_response_id(self) -> str | None:
        return self._last_response_id

    @property
    def _endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        return base if base.endswith("/responses") else f"{base}/responses"

    def _is_terminal_chunk(self, chunk: Mapping[str, Any]) -> bool:
        return chunk.get("type") in _TERMINAL_EVENTS

    def _finish_reason(self, chunk: Mapping[str, Any]) -> str | None:
        finish, self._terminal_finish = self._terminal_finish, None
        return finish

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        if any(message.image_attachments for message in request.messages) and not (
            self._capabilities.vision
        ):
            raise ProviderError("provider vision capability is not enabled")
        messages = list(request.messages)
        payload: dict[str, Any] = {
            "model": request.model or self.config.model,
            "stream": True,
        }
        if self.config.previous_response_continuation and self._last_response_id:
            payload["previous_response_id"] = self._last_response_id
            last_assistant = max(
                (index for index, message in enumerate(messages) if message.role == "assistant"),
                default=None,
            )
            if last_assistant is not None:
                messages = messages[last_assistant + 1 :]
        payload["input"] = [item for message in messages for item in _input_items(message)]
        if request.tools:
            payload["tools"] = [_tool_payload(tool) for tool in request.tools]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.reasoning == "enabled" or self.config.reasoning_effort != "default":
            payload["reasoning"] = {
                "effort": (
                    "medium"
                    if self.config.reasoning_effort == "default"
                    else self.config.reasoning_effort
                )
            }
        payload["max_output_tokens"] = (
            request.max_output_tokens
            if request.max_output_tokens is not None
            else self.config.default_max_output_tokens
        )
        return payload

    def _consume(
        self,
        chunk: dict[str, Any],
        events: list[ModelEvent],
        tools: dict[tuple[int, int], dict[str, Any]],
    ) -> ModelError | None:
        event_type = str(chunk.get("type") or "")
        if event_type in {"error", "response.failed"}:
            raw = chunk.get("error") or (chunk.get("response") or {}).get("error") or {}
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
        if event_type == "response.output_text.delta":
            events.append(TextDelta(text=str(chunk.get("delta") or "")))
            return None
        if event_type == "response.reasoning_summary_text.delta":
            events.append(ReasoningDelta(text=str(chunk.get("delta") or "")))
            return None
        if event_type == "response.output_item.added":
            item = chunk.get("item") or {}
            if not isinstance(item, Mapping) or item.get("type") != "function_call":
                return None
            if len(tools) >= self.config.max_tool_calls:
                event = ModelError(
                    message="provider exceeded tool-call count limit",
                    code="tool_call_limit",
                    partial=bool(events),
                )
                events.append(event)
                return event
            entry = tools.setdefault(
                (len(tools), 0),
                {
                    "call_id": "",
                    "name": "",
                    "arguments": "",
                    "argument_snapshot": "",
                    "argument_overlap": "",
                    "argument_chunk_count": 0,
                    "argument_chunks": [],
                    "argument_assembly": "single",
                    "item_id": "",
                },
            )
            entry["item_id"] = str(item.get("id") or "")
            entry["call_id"] = _merge_streamed_scalar(
                str(entry["call_id"]), str(item.get("call_id") or "")
            )
            entry["name"] = _merge_streamed_scalar(str(entry["name"]), str(item.get("name") or ""))
            if item.get("arguments"):
                entry["arguments"] = str(item["arguments"])
            return None
        if event_type in {
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        }:
            item_id = str(chunk.get("item_id") or "")
            target: dict[str, Any] | None = None
            for candidate in tools.values():
                if candidate["item_id"] == item_id:
                    target = candidate
                    break
            if target is None:
                event = ModelError(
                    message="provider streamed arguments for an unknown tool call",
                    code="malformed_tool_call",
                    retryable=True,
                    partial=bool(events),
                )
                events.append(event)
                return event
            if event_type.endswith(".done"):
                target["arguments"] = str(chunk.get("arguments") or "")
                target["argument_snapshot"] = str(target["arguments"])
            else:
                delta = str(chunk.get("delta") or "")
                target["argument_chunk_count"] += 1
                target["arguments"] = str(target["arguments"]) + delta
                target["argument_snapshot"] = delta
                target["argument_overlap"] = str(target["argument_overlap"]) + delta
                if target["argument_chunk_count"] > 1:
                    target["argument_assembly"] = "fragments"
            if len(str(target["arguments"]).encode()) > self.config.max_tool_argument_bytes:
                event = ModelError(
                    message="provider tool arguments exceed byte limit",
                    code="tool_argument_limit",
                    partial=bool(events),
                )
                events.append(event)
                return event
            return None
        if event_type == "response.completed":
            response = chunk.get("response") or {}
            request_id = None
            if isinstance(response, Mapping) and response.get("id"):
                request_id = str(response["id"])
                self._last_response_id = request_id
            usage = response.get("usage") if isinstance(response, Mapping) else None
            self._record_usage_details(usage if isinstance(usage, Mapping) else {})
            self._terminal_finish = "tool_calls" if tools else "stop"
            if isinstance(usage, Mapping) and usage:
                event = self._usage(usage, request_id=request_id)
                events.append(event)
                if error := self._budget_error(event):
                    events.append(error)
                    return error
            return None
        if event_type == "response.incomplete":
            response = chunk.get("response") or {}
            details = response.get("incomplete_details") if isinstance(response, Mapping) else None
            reason = details.get("reason") if isinstance(details, Mapping) else None
            self._terminal_finish = "length" if reason == "max_output_tokens" else "stop"
            usage = response.get("usage") if isinstance(response, Mapping) else None
            if isinstance(usage, Mapping) and usage:
                self._record_usage_details(usage)
                request_id = response.get("id") if isinstance(response, Mapping) else None
                event = self._usage(
                    usage, request_id=str(request_id) if request_id is not None else None
                )
                event.usage_complete = False
                events.append(event)
            return None
        return None

    def _record_usage_details(self, usage: Mapping[str, Any]) -> None:
        details: dict[str, int] = {}
        input_details = usage.get("input_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or {}
        if isinstance(input_details, Mapping) and input_details.get("cached_tokens") is not None:
            details["cached_input_tokens"] = int(input_details["cached_tokens"])
        if (
            isinstance(output_details, Mapping)
            and output_details.get("reasoning_tokens") is not None
        ):
            details["reasoning_output_tokens"] = int(output_details["reasoning_tokens"])
        self.usage_details = details


def _tool_payload(tool: Mapping[str, Any]) -> dict[str, Any]:
    plain = _plain(tool)
    assert isinstance(plain, dict)
    function = plain.get("function")
    if isinstance(function, Mapping):
        declaration = {"type": "function", **{str(k): v for k, v in function.items()}}
        return declaration
    return plain


def _input_items(message: object) -> list[dict[str, Any]]:
    plain = _plain(message)
    assert isinstance(plain, dict)
    attachments = plain.pop("image_attachments", ())
    role = str(plain.get("role") or "")
    content = str(plain.get("content") or "")
    if role == "tool":
        return [
            {
                "type": "function_call_output",
                "call_id": str(plain.get("tool_call_id") or ""),
                "output": content,
            }
        ]
    if role == "assistant":
        items: list[dict[str, Any]] = []
        reasoning = plain.get("reasoning_content")
        if reasoning:
            items.append(
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": str(reasoning)}],
                }
            )
        if content:
            items.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content}],
                }
            )
        for call in plain.get("tool_calls") or []:
            items.append(_function_call_item(call))
        return items
    blocks: list[dict[str, object]] = [{"type": "input_text", "text": content}]
    if attachments:
        for media_type, encoded in _verified_image_data(attachments):
            blocks.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{media_type};base64,{encoded}",
                }
            )
    return [{"role": role, "content": blocks}]


def _function_call_item(call: object) -> dict[str, Any]:
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
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments or {}, separators=(",", ":"))
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }
