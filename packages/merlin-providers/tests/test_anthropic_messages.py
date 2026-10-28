from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import httpx
import pytest
from merlin_contracts import (
    ChatMessage,
    ChatRequest,
    ImageAttachment,
    ModelCompleted,
    ModelError,
    ModelUsage,
    ProviderFailureKind,
    ReasoningDelta,
    TextDelta,
    ToolCall,
)

from merlin_providers import AnthropicMessagesConfig, AnthropicMessagesProvider, ProviderError


def _sse(events: list[tuple[str, dict[str, object]]]) -> str:
    return "\n".join(f"event: {name}\ndata: {json.dumps(payload)}" for name, payload in events)


def _run(provider: AnthropicMessagesProvider, request: ChatRequest) -> list[object]:
    async def run() -> list[object]:
        return [event async for event in provider.stream(request)]

    return asyncio.run(run())


def _provider(handler, **config_kwargs) -> AnthropicMessagesProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AnthropicMessagesProvider(
        AnthropicMessagesConfig(
            "https://api.anthropic.test/v1", "secret", "claude-opus-4-7", **config_kwargs
        ),
        client=client,
    )


def test_legacy_anthropic_requires_exact_operator_budget_not_categorical_effort() -> None:
    provider = AnthropicMessagesProvider(
        AnthropicMessagesConfig(
            "https://api.anthropic.test/v1",
            "secret",
            "claude-sonnet-4-5",
            reasoning_effort="high",
        )
    )
    with pytest.raises(ProviderError, match="does not support categorical reasoning effort"):
        provider._payload(
            ChatRequest(model="claude-sonnet-4-5", messages=[], max_output_tokens=8_000)
        )

    explicit = AnthropicMessagesProvider(
        AnthropicMessagesConfig(
            "https://api.anthropic.test/v1",
            "secret",
            "claude-sonnet-4-5",
            thinking_budget_tokens=2_048,
        )
    )
    payload = explicit._payload(
        ChatRequest(
            model="claude-sonnet-4-5",
            messages=[],
            reasoning="enabled",
            max_output_tokens=8_000,
        )
    )
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 2_048}


def _tool_use_stream(call_id: str = "toolu_1", path: str = "a.txt") -> list[tuple[str, dict]]:
    return [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 4,
                        "cache_creation_input_tokens": 2,
                    },
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": call_id, "name": "read_file"},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"pa'},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": f'th":"{path}"}}'},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 6},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]


def test_payload_shapes_system_messages_tools_and_thinking() -> None:
    provider = AnthropicMessagesProvider(
        AnthropicMessagesConfig("https://api.anthropic.test/v1", "", "claude-opus-4-7")
    )
    provider._thinking_signatures["prior reasoning"] = "sig_abc"
    request = ChatRequest(
        model="claude-opus-4-7",
        messages=[
            ChatMessage(role="system", content="Be terse"),
            ChatMessage(role="user", content="Hi"),
            ChatMessage(
                role="assistant",
                content="Working",
                reasoning_content="prior reasoning",
                tool_calls=[
                    {
                        "id": "toolu_1",
                        "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                    }
                ],
            ),
            ChatMessage(role="tool", content="body a", tool_call_id="toolu_1"),
            ChatMessage(role="tool", content="body b", tool_call_id="toolu_2"),
        ],
        tools=[{"type": "function", "function": {"name": "read_file", "description": "Read"}}],
        reasoning="enabled",
        max_output_tokens=8_000,
    )

    payload = provider._payload(request)

    assert payload["system"] == [
        {"type": "text", "text": "Be terse", "cache_control": {"type": "ephemeral"}}
    ]
    assert payload["max_tokens"] == 8_000
    assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "output_config" not in payload
    assert payload["stream"] is True
    assert payload["tools"] == [
        {
            "name": "read_file",
            "description": "Read",
            "input_schema": {"type": "object"},
            "cache_control": {"type": "ephemeral"},
        }
    ]
    messages = payload["messages"]
    assert messages[0] == {"role": "user", "content": [{"type": "text", "text": "Hi"}]}
    assert messages[1] == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "prior reasoning", "signature": "sig_abc"},
            {"type": "text", "text": "Working"},
            {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.txt"}},
        ],
    }
    assert messages[2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "body a"},
            {"type": "tool_result", "tool_use_id": "toolu_2", "content": "body b"},
        ],
    }
    assert "temperature" not in payload


def test_vision_payload_embeds_only_digest_verified_user_images(tmp_path) -> None:
    image = tmp_path / "diagnostic.png"
    image.write_bytes(b"png evidence")
    attachment = ImageAttachment(
        media_type="image/png",
        path=str(image),
        sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
        byte_length=image.stat().st_size,
    )
    provider = AnthropicMessagesProvider(
        AnthropicMessagesConfig(
            "https://api.anthropic.test/v1",
            "",
            "claude-haiku-4-5",
            vision=True,
        )
    )

    payload = provider._payload(
        ChatRequest(
            model="claude-haiku-4-5",
            messages=[
                ChatMessage(
                    role="user",
                    content="Inspect this failure",
                    image_attachments=[attachment],
                )
            ],
        )
    )

    assert payload["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect this failure"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(image.read_bytes()).decode("ascii"),
                    },
                },
            ],
        }
    ]

    image.write_bytes(b"tampered png")
    with pytest.raises(ProviderError, match="digest verification"):
        provider._payload(
            ChatRequest(
                model="claude-haiku-4-5",
                messages=[
                    ChatMessage(
                        role="user",
                        content="Inspect this failure",
                        image_attachments=[attachment],
                    )
                ],
            )
        )


def test_vision_payload_rejects_wrong_declared_length(tmp_path) -> None:
    image = tmp_path / "diagnostic.webp"
    image.write_bytes(b"webp evidence")
    provider = AnthropicMessagesProvider(
        AnthropicMessagesConfig(
            "https://api.anthropic.test/v1",
            "",
            "claude-haiku-4-5",
            vision=True,
        )
    )
    message = ChatMessage(
        role="user",
        content="Inspect",
        image_attachments=[
            ImageAttachment(
                media_type="image/webp",
                path=str(image),
                sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
                byte_length=image.stat().st_size + 1,
            )
        ],
    )

    with pytest.raises(ProviderError, match="size or digest verification"):
        provider._payload(ChatRequest(model="claude-haiku-4-5", messages=[message]))


def test_non_vision_anthropic_route_rejects_image_attachments(tmp_path) -> None:
    image = tmp_path / "diagnostic.jpg"
    image.write_bytes(b"jpeg evidence")
    message = ChatMessage(
        role="user",
        content="Inspect",
        image_attachments=[
            ImageAttachment(
                media_type="image/jpeg",
                path=str(image),
                sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
                byte_length=image.stat().st_size,
            )
        ],
    )
    provider = AnthropicMessagesProvider(
        AnthropicMessagesConfig("https://api.anthropic.test/v1", "", "claude-haiku-4-5")
    )

    with pytest.raises(ProviderError, match="vision capability"):
        provider._payload(ChatRequest(model="claude-haiku-4-5", messages=[message]))


@pytest.mark.parametrize("role", ("system", "assistant", "tool"))
def test_anthropic_images_are_rejected_outside_user_messages(tmp_path, role) -> None:
    image = tmp_path / "diagnostic.jpg"
    image.write_bytes(b"jpeg evidence")
    message = ChatMessage(
        role=role,
        content="Inspect",
        image_attachments=[
            ImageAttachment(
                media_type="image/jpeg",
                path=str(image),
                sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
                byte_length=image.stat().st_size,
            )
        ],
    )
    provider = AnthropicMessagesProvider(
        AnthropicMessagesConfig(
            "https://api.anthropic.test/v1",
            "",
            "claude-haiku-4-5",
            vision=True,
        )
    )

    with pytest.raises(ProviderError, match="only supported in user messages"):
        provider._payload(ChatRequest(model="claude-haiku-4-5", messages=[message]))


def test_required_off_cannot_be_reenabled_by_anthropic_config() -> None:
    provider = AnthropicMessagesProvider(
        AnthropicMessagesConfig(
            "https://api.test/v1", "", "claude", reasoning_effort="high"
        )
    )
    payload = provider._payload(
        ChatRequest(
            model="claude",
            messages=[],
            workload="worker",
            reasoning="disabled",
            reasoning_control="required",
        )
    )
    assert "thinking" not in payload


def test_payload_keeps_dynamic_compaction_outside_stable_cache_block() -> None:
    provider = AnthropicMessagesProvider(
        AnthropicMessagesConfig("https://api.anthropic.test/v1", "", "claude-sonnet-4-6")
    )
    request = ChatRequest(
        model="claude-sonnet-4-6",
        messages=[
            ChatMessage(role="system", content="stable identity"),
            ChatMessage(role="system", content="Durable compacted worker history digest-a"),
            ChatMessage(role="user", content="continue"),
        ],
    )

    first = provider._payload(request)["system"]
    second = provider._payload(
        request.model_copy(
            update={
                "messages": [
                    request.messages[0],
                    ChatMessage(role="system", content="Durable compacted worker history digest-b"),
                    request.messages[2],
                ]
            }
        )
    )["system"]

    assert (
        first[0]
        == second[0]
        == {
            "type": "text",
            "text": "stable identity",
            "cache_control": {"type": "ephemeral"},
        }
    )
    assert first[1] != second[1]
    assert "cache_control" not in first[1]


def test_unknown_reasoning_text_omits_thinking_block() -> None:
    provider = AnthropicMessagesProvider(
        AnthropicMessagesConfig("https://api.anthropic.test/v1", "", "claude-opus-4-7")
    )
    payload = provider._payload(
        ChatRequest(
            model="claude-opus-4-7",
            messages=[
                ChatMessage(role="assistant", content="Done", reasoning_content="uncached text")
            ],
        )
    )
    assert payload["messages"][0]["content"] == [{"type": "text", "text": "Done"}]


def test_tool_input_schema_projects_top_level_combinators() -> None:
    from merlin_providers.anthropic_messages import _sanitize_input_schema

    # anyOf of object variants merges: properties unioned, required intersected.
    merged = _sanitize_input_schema(
        {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {"a": {"type": "string"}, "shared": {"type": "integer"}},
                    "required": ["a", "shared"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"b": {"type": "string"}, "shared": {"type": "integer"}},
                    "required": ["b", "shared"],
                    "additionalProperties": False,
                },
            ],
            "description": "union tool",
        }
    )
    assert merged == {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "string"},
            "shared": {"type": "integer"},
        },
        "required": ["shared"],
        "additionalProperties": False,
        "description": "union tool",
    }

    # allOf unions required instead.
    merged_all = _sanitize_input_schema(
        {
            "allOf": [
                {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
                {"type": "object", "properties": {"b": {"type": "string"}}, "required": ["b"]},
            ]
        }
    )
    assert merged_all == {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a", "b"],
    }

    # A non-object union collapses to its first variant rather than failing dispatch.
    collapsed = _sanitize_input_schema({"oneOf": [{"type": "string"}, {"type": "null"}]})
    assert collapsed == {"type": "string"}

    # Plain schemas pass through untouched.
    plain = {"type": "object", "properties": {"x": {"type": "string"}}}
    assert _sanitize_input_schema(plain) == plain
    assert _sanitize_input_schema(None) == {"type": "object"}


def test_tool_payload_sanitizes_top_level_anyof() -> None:
    provider = AnthropicMessagesProvider(
        AnthropicMessagesConfig("https://api.anthropic.test/v1", "", "claude-opus-4-7")
    )
    payload = provider._payload(
        ChatRequest(
            model="claude-opus-4-7",
            messages=[ChatMessage(role="user", content="Hi")],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "composite",
                        "parameters": {
                            "anyOf": [
                                {
                                    "type": "object",
                                    "properties": {"path": {"type": "string"}},
                                    "required": ["path"],
                                },
                                {
                                    "type": "object",
                                    "properties": {"url": {"type": "string"}},
                                    "required": ["url"],
                                },
                            ]
                        },
                    },
                }
            ],
        )
    )
    schema = payload["tools"][0]["input_schema"]
    assert "anyOf" not in schema and "oneOf" not in schema and "allOf" not in schema
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"path", "url"}
    assert "required" not in schema


def test_headers_use_anthropic_authentication() -> None:
    provider = AnthropicMessagesProvider(
        AnthropicMessagesConfig("https://api.anthropic.test/v1", "secret", "claude-opus-4-7")
    )
    headers = provider._headers
    assert headers["x-api-key"] == "secret"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in headers


def test_streams_tool_call_and_usage_with_cache_details() -> None:
    seen_request: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_request.update(json.loads(request.content))
        assert request.headers["x-api-key"] == "secret"
        assert request.url.path == "/v1/messages"
        return httpx.Response(200, text=_sse(_tool_use_stream()))

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="claude-opus-4-7", messages=[]))

    usage = next(event for event in events if isinstance(event, ModelUsage))
    assert usage.input_tokens == 16
    assert usage.output_tokens == 6
    assert usage.total_tokens == 22
    assert usage.uncached_input_tokens == 10
    assert usage.cache_read_input_tokens == 4
    assert usage.cache_creation_input_tokens == 2
    assert usage.cache_hit is True
    assert usage.cache_created is True
    assert usage.cache_miss is False
    assert usage.request_id == "msg_1"
    assert usage.usage_complete is True
    assert usage.usage_source == "provider"
    assert usage.attempt_count == 1
    assert usage.latency_ms is not None
    events.remove(usage)
    assert events == [
        ToolCall(call_id="toolu_1", name="read_file", arguments={"path": "a.txt"}),
        ModelCompleted(finish_reason="tool_calls", disposition="completed"),
    ]
    assert provider.usage_details == {
        "cache_read_input_tokens": 4,
        "cache_creation_input_tokens": 2,
    }


def test_thinking_round_trips_with_signature() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    (
                        "message_start",
                        {
                            "type": "message_start",
                            "message": {"id": "msg_1", "usage": {"input_tokens": 3}},
                        },
                    ),
                    (
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "thinking", "thinking": ""},
                        },
                    ),
                    (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "thinking_delta", "thinking": "let me "},
                        },
                    ),
                    (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "thinking_delta", "thinking": "think"},
                        },
                    ),
                    (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "signature_delta", "signature": "sig_1"},
                        },
                    ),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    (
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": 1,
                            "content_block": {"type": "text", "text": ""},
                        },
                    ),
                    (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 1,
                            "delta": {"type": "text_delta", "text": "Answer"},
                        },
                    ),
                    (
                        "message_delta",
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": "end_turn"},
                            "usage": {"output_tokens": 4},
                        },
                    ),
                    ("message_stop", {"type": "message_stop"}),
                ]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="claude-opus-4-7", messages=[]))

    assert events[:3] == [
        ReasoningDelta(text="let me "),
        ReasoningDelta(text="think"),
        TextDelta(text="Answer"),
    ]
    assert events[-1] == ModelCompleted(finish_reason="stop", disposition="completed")
    assert provider._thinking_signatures == {"let me think": "sig_1"}

    payload = provider._payload(
        ChatRequest(
            model="claude-opus-4-7",
            messages=[ChatMessage(role="assistant", content="", reasoning_content="let me think")],
        )
    )
    assert payload["messages"][0]["content"] == [
        {"type": "thinking", "thinking": "let me think", "signature": "sig_1"}
    ]


def test_conflicting_duplicate_tool_use_ids_fail_closed() -> None:
    events_payload = _tool_use_stream(call_id="toolu_1", path="a.txt")
    events_payload[1] = (
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file"},
        },
    )
    second_call = _tool_use_stream(call_id="toolu_1", path="b.txt")
    for name, payload in second_call[1:5]:
        if isinstance(payload.get("index"), int):
            payload["index"] = 1
    events_payload = events_payload[:5] + second_call[1:5] + events_payload[5:]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(events_payload))

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="claude-opus-4-7", messages=[]))

    errors = [event for event in events if isinstance(event, ModelError)]
    assert len(errors) == 1
    assert errors[0].code == "ambiguous_tool_call"
    assert "conflicting" in errors[0].message
    assert not any(isinstance(event, ToolCall) for event in events)


def test_max_tokens_stop_maps_to_truncated() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    (
                        "message_start",
                        {
                            "type": "message_start",
                            "message": {"id": "msg_1", "usage": {"input_tokens": 3}},
                        },
                    ),
                    (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": "partial"},
                        },
                    ),
                    (
                        "message_delta",
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": "max_tokens"},
                            "usage": {"output_tokens": 9},
                        },
                    ),
                    ("message_stop", {"type": "message_stop"}),
                ]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="claude-opus-4-7", messages=[]))

    completed = events[-1]
    assert isinstance(completed, ModelCompleted)
    assert completed.disposition == "truncated"
    assert completed.finish_reason == "length"


def test_error_event_maps_to_model_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    (
                        "error",
                        {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
                    )
                ]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="claude-opus-4-7", messages=[]))

    assert events == [
        ModelError(
            message="busy",
            code="provider_transient_error",
            retryable=True,
            failure_kind=ProviderFailureKind.TRANSIENT,
        )
    ]


def test_http_error_mapping_matches_chat_conventions() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "2"}, text='{"error":"slow down"}')

    provider = _provider(handler, max_attempts=1)
    events = _run(provider, ChatRequest(model="claude-opus-4-7", messages=[]))

    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ModelError)
    assert error.code == "rate_limited"
    assert error.retryable is True
    assert error.retry_after_seconds == 2


@pytest.mark.parametrize(
    ("error_type", "expected_code", "expected_kind", "retryable"),
    [
        ("authentication_error", "provider_auth_error", ProviderFailureKind.AUTHORIZATION, False),
        ("billing_error", "provider_quota_exhausted", ProviderFailureKind.QUOTA, False),
        (
            "invalid_request_error",
            "provider_configuration_error",
            ProviderFailureKind.CONFIGURATION,
            False,
        ),
        (
            "overloaded_error",
            "provider_transient_error",
            ProviderFailureKind.TRANSIENT,
            True,
        ),
    ],
)
def test_structured_error_event_has_typed_failure_semantics(
    error_type: str,
    expected_code: str,
    expected_kind: ProviderFailureKind,
    retryable: bool,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    (
                        "error",
                        {
                            "type": "error",
                            "error": {"type": error_type, "message": "native failure"},
                        },
                    )
                ]
            ),
        )

    events = _run(
        _provider(handler, max_attempts=1),
        ChatRequest(model="claude-opus-4-7", messages=[]),
    )

    assert events == [
        ModelError(
            message="native failure",
            code=expected_code,
            retryable=retryable,
            failure_kind=expected_kind,
        )
    ]


def test_stream_ending_before_message_stop_is_an_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": "partial"},
                        },
                    )
                ]
            ),
        )

    provider = _provider(handler, max_attempts=1)
    events = _run(provider, ChatRequest(model="claude-opus-4-7", messages=[]))

    assert events[-1] == ModelError(
        message="provider stream ended before [DONE]",
        code="incomplete_stream",
        retryable=True,
        partial=True,
        failure_kind=ProviderFailureKind.TRANSIENT,
    )


def test_identical_parallel_tool_calls_with_distinct_ids_are_preserved() -> None:
    first = _tool_use_stream(call_id="toolu_1", path="same.txt")
    second = _tool_use_stream(call_id="toolu_2", path="same.txt")
    for name, payload in second[1:5]:
        if isinstance(payload.get("index"), int):
            payload["index"] = 1
    events_payload = first[:5] + second[1:5] + first[5:]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(events_payload))

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="claude-opus-4-7", messages=[]))

    assert [event for event in events if isinstance(event, ToolCall)] == [
        ToolCall(call_id="toolu_1", name="read_file", arguments={"path": "same.txt"}),
        ToolCall(call_id="toolu_2", name="read_file", arguments={"path": "same.txt"}),
    ]


class _DisconnectAfterToolStart(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[override]
        yield b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","usage":{"input_tokens":3}}}\n'
        yield b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"read_file"}}\n'
        yield b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"pa"}}\n'
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )


def test_interrupted_stream_mid_tool_call_yields_one_terminal_result_per_call() -> None:
    async def interrupted_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_DisconnectAfterToolStart())

    provider = _provider(interrupted_handler, max_attempts=1)
    events = _run(provider, ChatRequest(model="claude-opus-4-7", messages=[]))

    # The interrupted attempt persists no call and ends in exactly one terminal
    # error — never a partial ToolCall and never a ModelCompleted.
    assert not any(isinstance(event, ToolCall) for event in events)
    assert not any(isinstance(event, ModelCompleted) for event in events)
    assert len(events) == 1
    assert isinstance(events[0], ModelError)
    assert events[0].code == "transport_error"
    assert events[0].retryable is True

    async def restarted_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(_tool_use_stream()))

    restarted = _provider(restarted_handler)
    replayed = _run(restarted, ChatRequest(model="claude-opus-4-7", messages=[]))

    # Across interruption + restart the call is persisted exactly once.
    assert [event for event in replayed if isinstance(event, ToolCall)] == [
        ToolCall(call_id="toolu_1", name="read_file", arguments={"path": "a.txt"})
    ]
    assert isinstance(replayed[-1], ModelCompleted)
