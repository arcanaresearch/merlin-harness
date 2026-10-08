"""Streaming client for the native Gemini generateContent API."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from merlin_contracts import (
    ChatMessage,
    ChatRequest,
    ModelError,
    ModelEvent,
    ModelUsage,
    ModelWarning,
    ReasoningDelta,
    TextDelta,
)

from .openai_compatible import (
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderError,
    _classify_payload_failure,
    _plain,
)

__all__ = ["GeminiConfig", "GeminiProvider"]

_FINISH_REASON_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "MALFORMED_FUNCTION_CALL": "stop",
    "OTHER": "stop",
}

# JSON-Schema keywords Gemini function declarations understand; everything else
# is stripped during schema projection.
_SCHEMA_KEYS = {
    "type",
    "format",
    "description",
    "nullable",
    "enum",
    "items",
    "properties",
    "required",
    "propertyOrdering",
    "anyOf",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
}
_TYPE_NAMES = {"object", "array", "string", "number", "integer", "boolean", "null"}


@dataclass(frozen=True, slots=True)
class GeminiConfig(ProviderConfig):
    """Gemini generateContent route configuration."""


class GeminiProvider(OpenAICompatibleProvider):
    _transport = "gemini"

    def __init__(self, config: GeminiConfig, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        self.config: GeminiConfig = config
        self._terminal_finish: str | None = None
        self.usage_details: dict[str, int] = {}
        self._thought_signatures: dict[str, str] = {}
        self._call_id_occurrences: dict[str, int] = {}
        self._pending_usage: ModelUsage | None = None

    @property
    def tool_call_signatures(self) -> Mapping[str, str]:
        """thoughtSignature values captured per synthesized call id this session."""
        return dict(self._thought_signatures)

    @property
    def _endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith(":streamGenerateContent"):
            return base
        return f"{base}/models/{self.config.model}:streamGenerateContent?alt=sse"

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
        if self.config.api_key:
            headers["x-goog-api-key"] = self.config.api_key
        return headers

    def _stream_end_is_terminal(self) -> bool:
        return True

    def _finish_reason(self, chunk: Mapping[str, Any]) -> str | None:
        finish, self._terminal_finish = self._terminal_finish, None
        return finish

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        if any(message.image_attachments for message in request.messages):
            raise ProviderError("image attachments are not supported by this transport")
        system = "\n".join(
            message.content for message in request.messages if message.role == "system"
        )
        generation_config: dict[str, Any] = {
            "maxOutputTokens": (
                request.max_output_tokens
                if request.max_output_tokens is not None
                else self.config.default_max_output_tokens
            )
        }
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if request.reasoning == "enabled" or self.config.reasoning_effort != "default":
            effort = self.config.reasoning_effort
            thinking: dict[str, Any] = {"includeThoughts": True}
            if "gemini-3" in (request.model or self.config.model).lower():
                thinking["thinkingLevel"] = {
                    "minimal": "MINIMAL",
                    "low": "LOW",
                    "medium": "MEDIUM",
                    "high": "HIGH",
                    "xhigh": "HIGH",
                }.get(effort, "HIGH")
            elif effort != "default":
                raise ProviderError(
                    f"model {request.model or self.config.model!r} does not support categorical "
                    "reasoning effort; use provider default or a Gemini 3 model"
                )
            generation_config["thinkingConfig"] = thinking
        payload: dict[str, Any] = {
            "contents": _contents_payload(request.messages, self._thought_signatures),
            "generationConfig": generation_config,
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if request.tools:
            payload["tools"] = [
                {"functionDeclarations": [_function_declaration(tool) for tool in request.tools]}
            ]
        return payload

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
                code=code,
                retryable=retryable,
                partial=bool(events),
                failure_kind=failure_kind,
            )
            events.append(event)
            return event
        feedback = chunk.get("promptFeedback") or {}
        if isinstance(feedback, Mapping) and feedback.get("blockReason"):
            events.append(ModelWarning(code="refusal", message=str(feedback["blockReason"])))
            self._terminal_finish = "content_filter"
        usage = chunk.get("usageMetadata") or {}
        if isinstance(usage, Mapping) and usage:
            details: dict[str, int] = {}
            cache_read = 0
            reasoning = 0
            if usage.get("cachedContentTokenCount") is not None:
                cache_read = int(usage["cachedContentTokenCount"])
                details["cached_input_tokens"] = cache_read
            if usage.get("thoughtsTokenCount") is not None:
                reasoning = int(usage["thoughtsTokenCount"])
                details["reasoning_output_tokens"] = reasoning
            self.usage_details = details
            response_id = chunk.get("responseId")
            usage_event = self._usage(
                {
                    "input_tokens": int(usage.get("promptTokenCount") or 0),
                    "output_tokens": int(usage.get("candidatesTokenCount") or 0),
                    "total_tokens": int(
                        usage.get("totalTokenCount")
                        or int(usage.get("promptTokenCount") or 0)
                        + int(usage.get("candidatesTokenCount") or 0)
                    ),
                },
                request_id=str(response_id) if response_id is not None else None,
                cache_read_input_tokens=cache_read,
                reasoning_output_tokens=reasoning,
            )
            # usageMetadata rides every chunk with cumulative counts; hold the
            # latest event and emit it once at end of stream (last wins).
            self._pending_usage = usage_event
            if error := self._budget_error(usage_event):
                events.append(usage_event)
                self._pending_usage = None
                events.append(error)
                return error
        candidates = chunk.get("candidates") or []
        if not candidates:
            return None
        candidate = candidates[0]
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            if not isinstance(part, Mapping):
                continue
            if part.get("functionCall"):
                call = part["functionCall"]
                if len(tools) >= self.config.max_tool_calls:
                    event = ModelError(
                        message="provider exceeded tool-call count limit",
                        code="tool_call_limit",
                        partial=bool(events),
                    )
                    events.append(event)
                    return event
                arguments = call.get("args") if isinstance(call, Mapping) else {}
                name = str(call.get("name") or "") if isinstance(call, Mapping) else ""
                argument_text = json.dumps(arguments or {}, separators=(",", ":"))
                digest = hashlib.sha256(
                    json.dumps(
                        [self.config.base_url, self.config.model, name, argument_text],
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()[:24]
                # Gemini sends no call ids; synthesize one per invocation so two
                # identical calls stay distinct (and each keeps its own
                # thoughtSignature). The digest is salted with this route's
                # endpoint + model so identical calls on different routes never
                # share an id, and the suffix is the call's occurrence order
                # across every stream on this provider so a later stream can
                # never overwrite an earlier call's signature.
                occurrence = self._call_id_occurrences.get(digest, 0)
                self._call_id_occurrences[digest] = occurrence + 1
                call_id = f"gemini_{digest}" if occurrence == 0 else f"gemini_{digest}_{occurrence}"
                signature = part.get("thoughtSignature")
                if isinstance(signature, str) and signature:
                    self._thought_signatures[call_id] = signature
                tools[(len(tools), 0)] = {
                    "call_id": call_id,
                    "name": name,
                    "arguments": argument_text,
                    "argument_snapshot": "",
                    "argument_overlap": "",
                    "argument_chunk_count": 1,
                    "argument_chunks": [],
                    "argument_assembly": "single",
                }
            elif part.get("text") is not None:
                text = str(part["text"])
                if part.get("thought"):
                    events.append(ReasoningDelta(text=text))
                else:
                    events.append(TextDelta(text=text))
        if candidate.get("finishReason") is not None:
            reason = str(candidate["finishReason"])
            self._terminal_finish = _FINISH_REASON_MAP.get(reason, "stop")
        return None

    def _finish_tools(
        self,
        events: list[ModelEvent],
        tools: dict[tuple[int, int], dict[str, Any]],
        *,
        partial: bool = False,
    ) -> ModelError | None:
        if self._pending_usage is not None:
            events.append(self._pending_usage)
            self._pending_usage = None
        return super()._finish_tools(events, tools, partial=partial)


def _function_declaration(tool: Mapping[str, Any]) -> dict[str, Any]:
    plain = _plain(tool)
    assert isinstance(plain, dict)
    function = plain.get("function")
    source = function if isinstance(function, Mapping) else plain
    declaration: dict[str, Any] = {"name": str(source.get("name") or "")}
    if source.get("description"):
        declaration["description"] = str(source["description"])
    parameters = source.get("parameters")
    if isinstance(parameters, Mapping):
        declaration["parameters"] = _project_schema(parameters)
    return declaration


def _project_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Project a JSON-Schema tool spec onto the Gemini declaration subset."""
    projected: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key == "type" and isinstance(value, str) and value.casefold() in _TYPE_NAMES:
            projected[key] = value.upper()
        elif key == "properties" and isinstance(value, Mapping):
            projected[key] = {
                str(name): _project_schema(item) if isinstance(item, Mapping) else item
                for name, item in value.items()
            }
        elif key in {"items", "anyOf"}:
            if isinstance(value, Mapping):
                projected[key] = _project_schema(value)
            elif isinstance(value, list):
                projected[key] = [
                    _project_schema(item) if isinstance(item, Mapping) else item for item in value
                ]
        else:
            projected[key] = value
    return projected


def _contents_payload(
    messages: Sequence[ChatMessage], signatures: Mapping[str, str]
) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = message.role
        if role == "system":
            continue
        if role == "tool":
            name = message.name or message.tool_call_id or ""
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": str(name),
                                "response": {"result": str(message.content)},
                            }
                        }
                    ],
                }
            )
            continue
        if role == "assistant":
            parts: list[dict[str, Any]] = []
            if message.reasoning_content:
                parts.append({"text": str(message.reasoning_content), "thought": True})
            if message.content:
                parts.append({"text": str(message.content)})
            for call in message.tool_calls or []:
                parts.append(_function_call_part(call, signatures))
            contents.append({"role": "model", "parts": parts})
            continue
        contents.append({"role": "user", "parts": [{"text": str(message.content)}]})
    return contents


def _function_call_part(call: object, signatures: Mapping[str, str]) -> dict[str, Any]:
    plain = _plain(call)
    assert isinstance(plain, dict)
    function = plain.get("function")
    if isinstance(function, Mapping):
        name = str(function.get("name") or "")
        arguments = function.get("arguments")
    else:
        name = str(plain.get("name") or "")
        arguments = plain.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    part: dict[str, Any] = {
        "functionCall": {
            "name": name,
            "args": arguments if isinstance(arguments, dict) else {},
        }
    }
    # Gemini 3.x thinking models reject a replayed functionCall part without
    # the thoughtSignature the stream originally attached to it.
    signature = plain.get("thoughtSignature") or plain.get("thought_signature")
    if not signature:
        call_id = str(plain.get("id") or plain.get("call_id") or "")
        signature = signatures.get(call_id)
    if signature:
        part["thoughtSignature"] = str(signature)
    return part
