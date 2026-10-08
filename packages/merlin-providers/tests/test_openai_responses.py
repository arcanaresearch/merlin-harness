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
    ProviderFailureKind,
    ReasoningDelta,
    TextDelta,
    ToolCall,
)

from merlin_providers import OpenAIResponsesConfig, OpenAIResponsesProvider


def _sse(events: list[dict[str, object]]) -> str:
    return "\n".join("data: " + json.dumps(event) for event in events)


def _completed_event(
    response_id: str = "resp_1",
    *,
    input_tokens: int = 5,
    output_tokens: int = 7,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> dict[str, object]:
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "status": "completed",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "input_tokens_details": {"cached_tokens": cached_tokens},
                "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
            },
        },
    }


def _run(provider: OpenAIResponsesProvider, request: ChatRequest) -> list[object]:
    async def run() -> list[object]:
        return [event async for event in provider.stream(request)]

    return asyncio.run(run())


def _provider(handler, **config_kwargs) -> OpenAIResponsesProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAIResponsesProvider(
        OpenAIResponsesConfig("https://api.openai.test/v1", "secret", "gpt-5.4", **config_kwargs),
        client=client,
    )


def test_payload_shapes_input_tools_and_reasoning() -> None:
    provider = OpenAIResponsesProvider(
        OpenAIResponsesConfig("https://api.openai.test/v1", "", "gpt-5.4")
    )
    request = ChatRequest(
        model="gpt-5.4",
        messages=[
            ChatMessage(role="system", content="Be terse"),
            ChatMessage(role="user", content="Hi"),
            ChatMessage(
                role="assistant",
                content="Working",
                reasoning_content="thinking hard",
                tool_calls=[{"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}}],
            ),
            ChatMessage(role="tool", content="file body", tool_call_id="call_1"),
        ],
        tools=[
            {
                "type": "function",
                "function": {"name": "read_file", "parameters": {"type": "object"}},
            }
        ],
        reasoning="enabled",
        temperature=0.2,
        max_output_tokens=64,
    )

    payload = provider._payload(request)

    assert payload["model"] == "gpt-5.4"
    assert payload["stream"] is True
    assert payload["temperature"] == 0.2
    assert payload["max_output_tokens"] == 64
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["tools"] == [
        {"type": "function", "name": "read_file", "parameters": {"type": "object"}}
    ]
    items = payload["input"]
    assert items[0] == {
        "role": "system",
        "content": [{"type": "input_text", "text": "Be terse"}],
    }
    assert items[1] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "Hi"}],
    }
    assert items[2] == {
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "thinking hard"}],
    }
    assert items[3] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Working"}],
    }
    assert items[4] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "read_file",
        "arguments": "{}",
    }
    assert items[5] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "file body",
    }
    assert "previous_response_id" not in payload


def test_previous_response_continuation_sends_only_new_items() -> None:
    provider = OpenAIResponsesProvider(
        OpenAIResponsesConfig(
            "https://api.openai.test/v1", "", "gpt-5.4", previous_response_continuation=True
        )
    )
    provider._last_response_id = "resp_prev"
    request = ChatRequest(
        model="gpt-5.4",
        messages=[
            ChatMessage(role="system", content="Be terse"),
            ChatMessage(role="user", content="Hi"),
            ChatMessage(role="assistant", content="Answer"),
            ChatMessage(role="tool", content="tool out", tool_call_id="call_1"),
            ChatMessage(role="user", content="follow up"),
        ],
    )

    payload = provider._payload(request)

    assert payload["previous_response_id"] == "resp_prev"
    assert [item.get("type", item.get("role")) for item in payload["input"]] == [
        "function_call_output",
        "user",
    ]


def test_streams_text_reasoning_tool_calls_and_usage() -> None:
    seen_request: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_request.update(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer secret"
        assert request.url.path == "/v1/responses"
        return httpx.Response(
            200,
            text=_sse(
                [
                    {"type": "response.created", "response": {"id": "resp_9"}},
                    {"type": "response.reasoning_summary_text.delta", "delta": "plan"},
                    {"type": "response.output_text.delta", "delta": "Hello"},
                    {
                        "type": "response.output_item.added",
                        "item": {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "name": "read_file",
                            "arguments": "",
                        },
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": "fc_1",
                        "delta": '{"pa',
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": "fc_1",
                        "delta": 'th":"README.md"}',
                    },
                    _completed_event("resp_9", cached_tokens=3, reasoning_tokens=2),
                ]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gpt-5.4", messages=[]))

    usage = next(event for event in events if isinstance(event, ModelUsage))
    assert usage.input_tokens == 5
    assert usage.output_tokens == 7
    assert usage.total_tokens == 12
    assert usage.uncached_input_tokens == 2
    assert usage.cache_read_input_tokens == 3
    assert usage.reasoning_output_tokens == 2
    assert usage.cache_hit is True
    assert usage.request_id == "resp_9"
    assert usage.usage_complete is True
    assert usage.usage_source == "provider"
    events.remove(usage)
    assert events == [
        ReasoningDelta(text="plan"),
        TextDelta(text="Hello"),
        ToolCall(call_id="call_1", name="read_file", arguments={"path": "README.md"}),
        ModelCompleted(finish_reason="tool_calls", disposition="completed"),
    ]
    assert seen_request["model"] == "gpt-5.4"
    assert provider.last_response_id == "resp_9"
    assert provider.usage_details == {"cached_input_tokens": 3, "reasoning_output_tokens": 2}


def test_arguments_done_event_is_authoritative_snapshot() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "type": "response.output_item.added",
                        "item": {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "name": "read_file",
                        },
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": "fc_1",
                        "delta": '{"path":"junk',
                    },
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": "fc_1",
                        "arguments": '{"path":"a.txt"}',
                    },
                    _completed_event(),
                ]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gpt-5.4", messages=[]))

    assert ToolCall(call_id="call_1", name="read_file", arguments={"path": "a.txt"}) in events


def test_conflicting_duplicate_call_ids_fail_closed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "type": "response.output_item.added",
                        "item": {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "name": "read_file",
                            "arguments": '{"path":"a.txt"}',
                        },
                    },
                    {
                        "type": "response.output_item.added",
                        "item": {
                            "type": "function_call",
                            "id": "fc_2",
                            "call_id": "call_1",
                            "name": "read_file",
                            "arguments": '{"path":"b.txt"}',
                        },
                    },
                    _completed_event(),
                ]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gpt-5.4", messages=[]))

    errors = [event for event in events if isinstance(event, ModelError)]
    assert len(errors) == 1
    assert errors[0].code == "ambiguous_tool_call"
    assert "conflicting" in errors[0].message
    assert not any(isinstance(event, ToolCall) for event in events)


def test_identical_duplicate_call_ids_fail_closed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "type": "response.output_item.added",
                        "item": {
                            "type": "function_call",
                            "id": f"fc_{index}",
                            "call_id": "call_1",
                            "name": "read_file",
                            "arguments": '{"path":"a.txt"}',
                        },
                    }
                    for index in range(2)
                ]
                + [_completed_event()]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gpt-5.4", messages=[]))

    errors = [event for event in events if isinstance(event, ModelError)]
    assert len(errors) == 1
    assert errors[0].code == "ambiguous_tool_call"
    assert "duplicate" in errors[0].message


def test_incomplete_max_output_maps_to_truncated() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {"type": "response.output_text.delta", "delta": "partial"},
                    {
                        "type": "response.incomplete",
                        "response": {
                            "id": "resp_x",
                            "status": "incomplete",
                            "incomplete_details": {"reason": "max_output_tokens"},
                            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
                        },
                    },
                ]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gpt-5.4", messages=[]))

    completed = events[-1]
    assert isinstance(completed, ModelCompleted)
    assert completed.disposition == "truncated"
    assert completed.finish_reason == "length"


@pytest.mark.parametrize(
    ("error_code", "expected_code", "expected_kind", "retryable"),
    [
        ("invalid_api_key", "provider_auth_error", ProviderFailureKind.AUTHORIZATION, False),
        ("insufficient_quota", "provider_quota_exhausted", ProviderFailureKind.QUOTA, False),
        (
            "model_not_found",
            "provider_configuration_error",
            ProviderFailureKind.CONFIGURATION,
            False,
        ),
        (
            "server_error",
            "provider_transient_error",
            ProviderFailureKind.TRANSIENT,
            True,
        ),
    ],
)
def test_response_failed_maps_to_typed_error(
    error_code: str,
    expected_code: str,
    expected_kind: ProviderFailureKind,
    retryable: bool,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "type": "response.failed",
                        "response": {
                            "id": "resp_bad",
                            "status": "failed",
                            "error": {"code": error_code, "message": "model exploded"},
                        },
                    }
                ]
            ),
        )

    provider = _provider(handler, max_attempts=1)
    events = _run(provider, ChatRequest(model="gpt-5.4", messages=[]))

    assert events == [
        ModelError(
            message="model exploded",
            code=expected_code,
            retryable=retryable,
            failure_kind=expected_kind,
        )
    ]


def test_http_error_mapping_matches_chat_conventions() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "2"}, text='{"error":"slow down"}')

    provider = _provider(handler, max_attempts=1)
    events = _run(provider, ChatRequest(model="gpt-5.4", messages=[]))

    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ModelError)
    assert error.code == "rate_limited"
    assert error.retryable is True
    assert error.retry_after_seconds == 2


def test_stream_ending_before_terminal_event_is_an_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=_sse([{"type": "response.output_text.delta", "delta": "partial"}])
        )

    provider = _provider(handler, max_attempts=1)
    events = _run(provider, ChatRequest(model="gpt-5.4", messages=[]))

    assert events[-1] == ModelError(
        message="provider stream ended before [DONE]",
        code="incomplete_stream",
        retryable=True,
        partial=True,
        failure_kind=ProviderFailureKind.TRANSIENT,
    )


def test_arguments_for_unknown_item_fail_closed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": "fc_ghost",
                        "delta": "{}",
                    }
                ]
            ),
        )

    provider = _provider(handler, max_attempts=1)
    events = _run(provider, ChatRequest(model="gpt-5.4", messages=[]))

    errors = [event for event in events if isinstance(event, ModelError)]
    assert errors[0].code == "malformed_tool_call"


def test_identical_parallel_calls_with_distinct_ids_are_preserved() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "type": "response.output_item.added",
                        "item": {
                            "type": "function_call",
                            "id": f"fc_{index}",
                            "call_id": call_id,
                            "name": "browser_snapshot",
                            "arguments": '{"tab_ref":"same"}',
                        },
                    }
                    for index, call_id in enumerate(("snapshot-a", "snapshot-b"))
                ]
                + [_completed_event()]
            ),
        )

    provider = _provider(handler)
    events = _run(provider, ChatRequest(model="gpt-5.4", messages=[]))

    assert [event for event in events if isinstance(event, ToolCall)] == [
        ToolCall(call_id="snapshot-a", name="browser_snapshot", arguments={"tab_ref": "same"}),
        ToolCall(call_id="snapshot-b", name="browser_snapshot", arguments={"tab_ref": "same"}),
    ]


class _DisconnectAfterToolStart(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[override]
        yield b'data: {"type":"response.created","response":{"id":"resp_1"}}\n'
        yield b'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read_file","arguments":""}}\n'
        yield b'data: {"type":"response.function_call_arguments.delta","item_id":"fc_1","delta":"{\\"pa"}\n'
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )


def test_interrupted_stream_mid_tool_call_yields_one_terminal_result_per_call() -> None:
    async def interrupted_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_DisconnectAfterToolStart())

    provider = _provider(interrupted_handler, max_attempts=1)
    events = _run(provider, ChatRequest(model="gpt-5.4", messages=[]))

    # The interrupted attempt persists no call and ends in exactly one terminal
    # error — never a partial ToolCall and never a ModelCompleted.
    assert not any(isinstance(event, ToolCall) for event in events)
    assert not any(isinstance(event, ModelCompleted) for event in events)
    assert len(events) == 1
    assert isinstance(events[0], ModelError)
    assert events[0].code == "transport_error"
    assert events[0].retryable is True

    async def restarted_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                [
                    {
                        "type": "response.output_item.added",
                        "item": {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "name": "read_file",
                            "arguments": '{"path":"a.txt"}',
                        },
                    },
                    _completed_event(),
                ]
            ),
        )

    restarted = _provider(restarted_handler)
    replayed = _run(restarted, ChatRequest(model="gpt-5.4", messages=[]))

    # Across interruption + restart the call is persisted exactly once.
    assert [event for event in replayed if isinstance(event, ToolCall)] == [
        ToolCall(call_id="call_1", name="read_file", arguments={"path": "a.txt"})
    ]
    assert isinstance(replayed[-1], ModelCompleted)
