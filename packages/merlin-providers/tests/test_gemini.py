from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from merlin_contracts import (
    ChatMessage,
    ChatRequest,
    ModelCompleted,
    ModelError,
    ModelUsage,
    ModelWarning,
    ProviderFailureKind,
    ReasoningDelta,
    TextDelta,
    ToolCall,
)

from merlin_providers import GeminiConfig, GeminiProvider, ProviderError
from merlin_providers.gemini import _project_schema


def _sse(chunks: list[dict[str, object]]) -> str:
    return "\n".join("data: " + json.dumps(chunk) for chunk in chunks)


def _run(provider: GeminiProvider, request: ChatRequest) -> list[object]:
    async def run() -> list[object]:
        return [event async for event in provider.stream(request)]

    return asyncio.run(run())


def _provider(handler, **config_kwargs) -> GeminiProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GeminiProvider(
        GeminiConfig(
            "https://generativelanguage.test/v1beta", "secret", "gemini-2.5-pro", **config_kwargs
        ),
        client=client,
    )


def test_gemini_effort_uses_native_levels_and_rejects_invented_legacy_budgets() -> None:
    gemini3 = GeminiProvider(
        GeminiConfig(
            "https://generativelanguage.test/v1beta",
            "secret",
            "gemini-3-pro",
            reasoning_effort="medium",
        )
    )
    payload = gemini3._payload(ChatRequest(model="gemini-3-pro", messages=[]))
    assert payload["generationConfig"]["thinkingConfig"] == {
        "includeThoughts": True,
        "thinkingLevel": "MEDIUM",
    }

    legacy = GeminiProvider(
        GeminiConfig(
            "https://generativelanguage.test/v1beta",
            "secret",
            "gemini-2.5-pro",
            reasoning_effort="high",
        )
    )
    with pytest.raises(ProviderError, match="does not support categorical reasoning effort"):
        legacy._payload(ChatRequest(model="gemini-2.5-pro", messages=[]))


def _tool_call_chunk(args: dict[str, object], name: str = "read_file") -> dict[str, object]:
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"functionCall": {"name": name, "args": args}}],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 6, "totalTokenCount": 10},
    }


def test_payload_shapes_contents_system_and_generation_config() -> None:
    provider = GeminiProvider(
        GeminiConfig("https://generativelanguage.test/v1beta", "", "gemini-2.5-pro")
    )
    request = ChatRequest(
        model="gemini-2.5-pro",
        messages=[
            ChatMessage(role="system", content="Be terse"),
            ChatMessage(role="user", content="Hi"),
            ChatMessage(
                role="assistant",
                content="Working",
                reasoning_content="thoughts",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                    }
                ],
            ),
            ChatMessage(role="tool", content="body", tool_call_id="call_1", name="read_file"),
        ],
        tools=[
            {
                "type": "function",
                "function": {"name": "read_file", "parameters": {"type": "object"}},
            }
        ],
        reasoning="enabled",
        temperature=0.1,
        max_output_tokens=256,
    )

    payload = provider._payload(request)

    assert payload["systemInstruction"] == {"parts": [{"text": "Be terse"}]}
    assert payload["generationConfig"] == {
        "maxOutputTokens": 256,
        "temperature": 0.1,
        "thinkingConfig": {"includeThoughts": True},
    }
    assert payload["tools"] == [
        {"functionDeclarations": [{"name": "read_file", "parameters": {"type": "OBJECT"}}]}
    ]
    contents = payload["contents"]
    assert contents[0] == {"role": "user", "parts": [{"text": "Hi"}]}
    assert contents[1] == {
        "role": "model",
        "parts": [
            {"text": "thoughts", "thought": True},
            {"text": "Working"},
            {"functionCall": {"name": "read_file", "args": {"path": "a.txt"}}},
        ],
    }
    assert contents[2] == {
        "role": "user",
        "parts": [{"functionResponse": {"name": "read_file", "response": {"result": "body"}}}],
    }


def test_schema_projection_strips_unsupported_keywords() -> None:
    schema = {
        "type": "object",
        "$schema": "http://json-schema.org/draft-07/schema#",
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string", "description": "File path", "pattern": "^/"},
            "depth": {"type": "integer", "minimum": 1, "default": 1},
            "tags": {"type": "array", "items": {"type": "string", "maxLength": 8}},
            "mode": {"anyOf": [{"type": "string"}, {"type": "null"}], "title": "Mode"},
        },
        "required": ["path"],
    }

    assert _project_schema(schema) == {
        "type": "OBJECT",
        "properties": {
            "path": {"type": "STRING", "description": "File path"},
            "depth": {"type": "INTEGER", "minimum": 1},
            "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
            "mode": {"anyOf": [{"type": "STRING"}, {"type": "NULL"}]},
        },
        "required": ["path"],
    }


def test_streams_text_thoughts_tool_calls_and_usage() -> None:
    seen_request: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_request.update(json.loads(request.content))
        assert request.headers["x-goog-api-key"] == "secret"
        assert request.url.path == "/v1beta/models/gemini-2.5-pro:streamGenerateContent"
        assert request.url.params["alt"] == "sse"
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": "hmm", "thought": True}],
                                }
                            }
                        ]
                    },
                    {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": "Hello"}],
                                }
                            }
                        ]
                    },
                    _tool_call_chunk({"path": "a.txt"}),
                ]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))

    usage = next(event for event in events if isinstance(event, ModelUsage))
    assert usage.input_tokens == 4
    assert usage.output_tokens == 6
    assert usage.total_tokens == 10
    assert events[0] == ReasoningDelta(text="hmm")
    assert events[1] == TextDelta(text="Hello")
    tool_calls = [event for event in events if isinstance(event, ToolCall)]
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "read_file"
    assert tool_calls[0].arguments == {"path": "a.txt"}
    assert tool_calls[0].call_id.startswith("gemini_")
    assert events[-1] == ModelCompleted(finish_reason="stop", disposition="completed")


def test_distinct_function_calls_get_distinct_local_ids() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [
                                        {
                                            "functionCall": {
                                                "name": "read_file",
                                                "args": {"path": "a.txt"},
                                            }
                                        },
                                        {
                                            "functionCall": {
                                                "name": "read_file",
                                                "args": {"path": "b.txt"},
                                            }
                                        },
                                    ],
                                },
                                "finishReason": "STOP",
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 1,
                            "candidatesTokenCount": 2,
                            "totalTokenCount": 3,
                        },
                    }
                ]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))

    tool_calls = [event for event in events if isinstance(event, ToolCall)]
    assert len(tool_calls) == 2
    assert tool_calls[0].call_id != tool_calls[1].call_id


def test_identical_duplicate_function_calls_get_distinct_ids() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        chunk = _tool_call_chunk({"path": "a.txt"})
        return httpx.Response(200, text=_sse([chunk, chunk]))

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))

    # Gemini sends no call ids; identical invocations are real repeats, so the
    # transport synthesizes a distinct id per occurrence instead of collapsing
    # them into a fail-closed duplicate.
    assert not any(isinstance(event, ModelError) for event in events)
    tool_calls = [event for event in events if isinstance(event, ToolCall)]
    assert len(tool_calls) == 2
    assert tool_calls[0].call_id != tool_calls[1].call_id
    assert tool_calls[0].name == tool_calls[1].name == "read_file"
    assert tool_calls[0].arguments == tool_calls[1].arguments == {"path": "a.txt"}


def test_max_tokens_maps_to_truncated() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "candidates": [
                            {
                                "content": {"role": "model", "parts": [{"text": "partial"}]},
                                "finishReason": "MAX_TOKENS",
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 1,
                            "candidatesTokenCount": 9,
                            "totalTokenCount": 10,
                        },
                    }
                ]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))

    completed = events[-1]
    assert isinstance(completed, ModelCompleted)
    assert completed.disposition == "truncated"
    assert completed.finish_reason == "length"


def test_safety_finish_maps_to_refused() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "candidates": [
                            {
                                "content": {"role": "model", "parts": [{"text": "no"}]},
                                "finishReason": "SAFETY",
                            }
                        ]
                    }
                ]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))

    assert events[-1] == ModelCompleted(finish_reason="content_filter", disposition="refused")


def test_blocked_prompt_warns_and_refuses() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse([{"promptFeedback": {"blockReason": "SAFETY"}}]),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))

    assert events[0] == ModelWarning(code="refusal", message="SAFETY")
    assert events[-1] == ModelCompleted(finish_reason="content_filter", disposition="refused")


@pytest.mark.parametrize(
    ("status", "expected_code", "expected_kind", "retryable"),
    [
        ("UNAUTHENTICATED", "provider_auth_error", ProviderFailureKind.AUTHORIZATION, False),
        ("RESOURCE_EXHAUSTED", "provider_quota_exhausted", ProviderFailureKind.QUOTA, False),
        (
            "INVALID_ARGUMENT",
            "provider_configuration_error",
            ProviderFailureKind.CONFIGURATION,
            False,
        ),
        (
            "UNAVAILABLE",
            "provider_transient_error",
            ProviderFailureKind.TRANSIENT,
            True,
        ),
    ],
)
def test_error_chunk_maps_to_typed_model_error(
    status: str,
    expected_code: str,
    expected_kind: ProviderFailureKind,
    retryable: bool,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse([{"error": {"code": 500, "status": status, "message": "backend exploded"}}]),
        )

    provider = _provider(handler, max_attempts=1)
    events = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))

    assert events == [
        ModelError(
            message="backend exploded",
            code=expected_code,
            retryable=retryable,
            failure_kind=expected_kind,
        )
    ]


def test_http_error_mapping_matches_chat_conventions() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "2"}, text='{"error":"slow down"}')

    provider = _provider(handler, max_attempts=1)
    events = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))

    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ModelError)
    assert error.code == "rate_limited"
    assert error.retryable is True
    assert error.retry_after_seconds == 2


def test_clean_stream_end_without_terminal_event_completes() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "candidates": [
                            {
                                "content": {"role": "model", "parts": [{"text": "Done"}]},
                                "finishReason": "STOP",
                            }
                        ]
                    }
                ]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))

    assert events == [
        TextDelta(text="Done"),
        ModelCompleted(finish_reason="stop", disposition="completed"),
    ]


def test_usage_records_cache_and_thought_details() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "candidates": [
                            {
                                "content": {"role": "model", "parts": [{"text": "Done"}]},
                                "finishReason": "STOP",
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 8,
                            "candidatesTokenCount": 4,
                            "totalTokenCount": 12,
                            "cachedContentTokenCount": 5,
                            "thoughtsTokenCount": 2,
                        },
                    }
                ]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))

    usage = next(event for event in events if isinstance(event, ModelUsage))
    assert usage.input_tokens == 8
    assert usage.output_tokens == 4
    assert usage.total_tokens == 12
    assert usage.uncached_input_tokens == 3
    assert usage.cache_read_input_tokens == 5
    assert usage.reasoning_output_tokens == 2
    assert usage.cache_hit is True
    assert usage.usage_complete is True
    assert usage.usage_source == "provider"
    assert provider.usage_details == {"cached_input_tokens": 5, "reasoning_output_tokens": 2}


class _DisconnectAfterToolChunk(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[override]
        yield ("data: " + json.dumps(_tool_call_chunk({"path": "a.txt"})) + "\n").encode()
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )


def test_interrupted_stream_after_tool_chunk_yields_one_terminal_result_per_call() -> None:
    async def interrupted_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_DisconnectAfterToolChunk())

    provider = _provider(interrupted_handler, max_attempts=1)
    events = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))

    # A consumed-but-unfinished function call is never persisted from an
    # interrupted stream: exactly one terminal error, no ToolCall, no Completed.
    assert not any(isinstance(event, ToolCall) for event in events)
    assert not any(isinstance(event, ModelCompleted) for event in events)
    terminal = [event for event in events if isinstance(event, ModelError)]
    assert len(terminal) == 1
    assert terminal[0].code == "transport_error"
    assert terminal[0].retryable is True

    async def restarted_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse([_tool_call_chunk({"path": "a.txt"})]))

    restarted = _provider(restarted_handler)
    replayed = _run(restarted, ChatRequest(model="gemini-2.5-pro", messages=[]))

    # Across interruption + restart the call is persisted exactly once.
    calls = [event for event in replayed if isinstance(event, ToolCall)]
    assert len(calls) == 1
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "a.txt"}
    assert isinstance(replayed[-1], ModelCompleted)


def test_thought_signature_captured_and_echoed_on_follow_up() -> None:
    seen_requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(json.loads(request.content))
        if len(seen_requests) == 1:
            return httpx.Response(
                200,
                text=_sse(
                    [
                        {
                            "candidates": [
                                {
                                    "content": {
                                        "role": "model",
                                        "parts": [
                                            {
                                                "functionCall": {
                                                    "name": "read_file",
                                                    "args": {"path": "a.txt"},
                                                },
                                                "thoughtSignature": "sig-abc123",
                                            }
                                        ],
                                    },
                                    "finishReason": "STOP",
                                }
                            ],
                            "usageMetadata": {
                                "promptTokenCount": 4,
                                "candidatesTokenCount": 6,
                                "totalTokenCount": 10,
                            },
                        }
                    ]
                ),
            )
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "candidates": [
                            {
                                "content": {"role": "model", "parts": [{"text": "done"}]},
                                "finishReason": "STOP",
                            }
                        ]
                    }
                ]
            ),
        )

    provider = _provider(handler)
    first = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))
    call = next(event for event in first if isinstance(event, ToolCall))

    follow_up = ChatRequest(
        model="gemini-2.5-pro",
        messages=[
            ChatMessage(role="user", content="Hi"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                ],
            ),
            ChatMessage(role="tool", content="body", tool_call_id=call.call_id, name=call.name),
        ],
    )
    _run(provider, follow_up)

    model_turn = seen_requests[1]["contents"][1]
    assert model_turn["role"] == "model"
    assert model_turn["parts"] == [
        {
            "functionCall": {"name": "read_file", "args": {"path": "a.txt"}},
            "thoughtSignature": "sig-abc123",
        }
    ]


def test_usage_emitted_once_with_terminal_counts() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "candidates": [
                            {
                                "content": {"role": "model", "parts": [{"text": "Hel"}]},
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 4,
                            "candidatesTokenCount": 1,
                            "totalTokenCount": 5,
                        },
                    },
                    {
                        "candidates": [
                            {
                                "content": {"role": "model", "parts": [{"text": "lo"}]},
                                "finishReason": "STOP",
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 4,
                            "candidatesTokenCount": 6,
                            "totalTokenCount": 10,
                        },
                    },
                ]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))

    usage_events = [event for event in events if isinstance(event, ModelUsage)]
    assert len(usage_events) == 1
    assert usage_events[0].input_tokens == 4
    assert usage_events[0].output_tokens == 6
    assert usage_events[0].total_tokens == 10


def _assistant_turn_dict(call: ToolCall, signature: str | None = None) -> dict[str, object]:
    """Mirror of the durable dict WorkerRuntime._assistant_tool_message persists."""
    payload: dict[str, object] = {
        "id": call.call_id,
        "type": "function",
        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
    }
    if signature is not None:
        payload["thought_signature"] = signature
    return payload


def test_thought_signature_survives_provider_recreation_via_stored_dict() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [
                                        {
                                            "functionCall": {
                                                "name": "read_file",
                                                "args": {"path": "a.txt"},
                                            },
                                            "thoughtSignature": "sig-durable-1",
                                        }
                                    ],
                                },
                                "finishReason": "STOP",
                            }
                        ]
                    }
                ]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))
    call = next(event for event in events if isinstance(event, ToolCall))
    assert provider.tool_call_signatures == {call.call_id: "sig-durable-1"}

    stored = ChatMessage(
        role="assistant",
        content="",
        tool_calls=[_assistant_turn_dict(call, provider.tool_call_signatures[call.call_id])],
    )
    request = ChatRequest(
        model="gemini-2.5-pro",
        messages=[
            ChatMessage(role="user", content="Hi"),
            stored,
            ChatMessage(role="tool", content="body", tool_call_id=call.call_id, name=call.name),
        ],
    )

    # A brand-new instance with no stream memory must replay the signature
    # purely from the durably stored message dict.
    fresh = GeminiProvider(
        GeminiConfig("https://generativelanguage.test/v1beta", "", "gemini-2.5-pro")
    )
    payload = fresh._payload(request)
    assert payload["contents"][1] == {
        "role": "model",
        "parts": [
            {
                "functionCall": {"name": "read_file", "args": {"path": "a.txt"}},
                "thoughtSignature": "sig-durable-1",
            }
        ],
    }


def test_identical_calls_keep_distinct_signatures_on_replay() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [
                                        {
                                            "functionCall": {
                                                "name": "read_file",
                                                "args": {"path": "a.txt"},
                                            },
                                            "thoughtSignature": "sig-first",
                                        },
                                        {
                                            "functionCall": {
                                                "name": "read_file",
                                                "args": {"path": "a.txt"},
                                            },
                                            "thoughtSignature": "sig-second",
                                        },
                                    ],
                                },
                                "finishReason": "STOP",
                            }
                        ]
                    }
                ]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))
    calls = [event for event in events if isinstance(event, ToolCall)]
    assert len(calls) == 2
    assert calls[0].call_id != calls[1].call_id
    assert provider.tool_call_signatures == {
        calls[0].call_id: "sig-first",
        calls[1].call_id: "sig-second",
    }

    request = ChatRequest(
        model="gemini-2.5-pro",
        messages=[
            ChatMessage(role="user", content="Hi"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    _assistant_turn_dict(call, provider.tool_call_signatures[call.call_id])
                    for call in calls
                ],
            ),
        ],
    )

    payload = provider._payload(request)
    assert payload["contents"][1]["parts"] == [
        {
            "functionCall": {"name": "read_file", "args": {"path": "a.txt"}},
            "thoughtSignature": "sig-first",
        },
        {
            "functionCall": {"name": "read_file", "args": {"path": "a.txt"}},
            "thoughtSignature": "sig-second",
        },
    ]


def test_identical_calls_across_streams_keep_distinct_signatures() -> None:
    signatures = iter(["sig-stream-1", "sig-stream-2"])

    async def handler(request: httpx.Request) -> httpx.Response:
        chunk = _tool_call_chunk({"path": "a.txt"})
        chunk["candidates"][0]["content"]["parts"][0]["thoughtSignature"] = next(signatures)
        return httpx.Response(200, text=_sse([chunk]))

    provider = _provider(handler)
    first = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))
    second = _run(provider, ChatRequest(model="gemini-2.5-pro", messages=[]))

    call_first = next(event for event in first if isinstance(event, ToolCall))
    call_second = next(event for event in second if isinstance(event, ToolCall))
    # Identical calls on separate streams must not share a synthesized id, so
    # the newer stream's signature never overwrites the earlier one.
    assert call_first.call_id != call_second.call_id
    assert provider.tool_call_signatures == {
        call_first.call_id: "sig-stream-1",
        call_second.call_id: "sig-stream-2",
    }
