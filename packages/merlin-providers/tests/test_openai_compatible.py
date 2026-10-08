from __future__ import annotations

import asyncio
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
    ModelWarning,
    ProviderFailureKind,
    ReasoningDelta,
    TextDelta,
    ToolCall,
)

from merlin_providers import (
    OpenAICompatibleProvider,
    ProviderCapabilities,
    ProviderConfig,
    ProviderError,
    ProviderQuotaSnapshot,
)
from merlin_providers.openai_compatible import _AdaptiveRequestGate, _admission_group

RECORDED_STREAM = "\n".join(  # noqa: FLY002 - readable recorded SSE fixture
    [
        'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"read_file","arguments":"{\\"pa"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"th\\":\\"README.md\\"}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":8,"total_tokens":12}}',
        "data: [DONE]",
    ]
)

DSML_OBSERVED_PARAMETERS = (
    '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="browser_snapshot">'
    '<｜｜DSML｜｜parameter name="context_id" string="true">ctx-1'
    '</｜｜DSML｜｜parameter><｜｜DSML｜｜parameter name="tab_ref" '
    'string="true">tab:1</｜｜DSML｜｜parameter></｜｜DSML｜｜invoke>'
    "</｜｜DSML｜｜tool_calls>"
)
DSML_DIRECT_JSON = (
    '<｜DSML｜tool_calls><｜DSML｜invoke name="browser_snapshot">'
    '{"context_id":"ctx-1","tab_ref":"tab:1"}'
    "</｜DSML｜invoke></｜DSML｜tool_calls>"
)
DSML_UNKNOWN_TOOL = (
    '<｜DSML｜tool_calls><｜DSML｜invoke name="unknown">{}</｜DSML｜invoke></｜DSML｜tool_calls>'
)
DSML_INVALID_TYPED_VALUE = (
    '<｜DSML｜tool_calls><｜DSML｜invoke name="browser_snapshot">'
    '<｜DSML｜parameter name="context_id" string="false">not-json'
    "</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>"
)


def test_provider_config_rejects_invalid_retry_bounds() -> None:
    with pytest.raises(ValueError, match="retry bounds"):
        ProviderConfig(
            "https://models.example/v1",
            "",
            "model",
            retry_base_seconds=2,
            retry_max_seconds=1,
        )


def test_vision_payload_embeds_only_digest_verified_images(tmp_path) -> None:
    image = tmp_path / "diagnostic.jpg"
    image.write_bytes(b"jpeg evidence")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    message = ChatMessage(
        role="user",
        content="Inspect this failure",
        image_attachments=[
            ImageAttachment(
                media_type="image/jpeg",
                path=str(image),
                sha256=digest,
                byte_length=image.stat().st_size,
            )
        ],
    )
    provider = OpenAICompatibleProvider(
        ProviderConfig("https://models.example/v1", "", "vision-model", vision=True)
    )

    payload = provider._payload(ChatRequest(model="vision-model", messages=[message]))

    content = payload["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Inspect this failure"}
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    image.write_bytes(b"tampered")
    with pytest.raises(ProviderError, match="digest verification"):
        provider._payload(ChatRequest(model="vision-model", messages=[message]))


def test_non_vision_provider_rejects_image_attachments(tmp_path) -> None:
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
    provider = OpenAICompatibleProvider(
        ProviderConfig("https://models.example/v1", "", "text-model")
    )
    with pytest.raises(ProviderError, match="vision capability"):
        provider._payload(ChatRequest(model="text-model", messages=[message]))


class _GatedSSEStream(httpx.AsyncByteStream):
    def __init__(self, first_chunk_sent: asyncio.Event, release_rest: asyncio.Event) -> None:
        self._first_chunk_sent = first_chunk_sent
        self._release_rest = release_rest

    async def __aiter__(self):  # type: ignore[override]
        self._first_chunk_sent.set()
        yield b'data: {"choices":[{"delta":{"reasoning_content":"The "}}]}\n\n'
        await self._release_rest.wait()
        yield b'data: {"choices":[{"delta":{"reasoning_content":"answer"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"Done"},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"


class _ReadTimeoutStream(httpx.AsyncByteStream):
    def __init__(self, *, partial: bool) -> None:
        self._partial = partial

    async def __aiter__(self):  # type: ignore[override]
        if self._partial:
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise httpx.ReadTimeout("provider stopped producing events")


class _StalledAfterPartialStream(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[override]
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        await asyncio.Event().wait()


class _DisconnectedStream(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[override]
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n'
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )


def test_yields_reasoning_before_provider_stream_finishes() -> None:
    async def run() -> None:
        first_chunk_sent = asyncio.Event()
        release_rest = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=_GatedSSEStream(first_chunk_sent, release_rest),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            stream = provider.stream(ChatRequest(model="model", messages=[]))

            first_event_task = asyncio.create_task(anext(stream))
            await asyncio.wait_for(first_chunk_sent.wait(), timeout=1)
            first_event = await asyncio.wait_for(first_event_task, timeout=1)

            assert first_event == ReasoningDelta(text="The ")
            release_rest.set()
            assert [event async for event in stream] == [
                ReasoningDelta(text="answer"),
                TextDelta(text="Done"),
                ModelCompleted(finish_reason="stop"),
            ]

    asyncio.run(run())


def test_streams_recorded_text_tool_calls_and_usage() -> None:
    seen_request: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_request.update(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(200, text=RECORDED_STREAM)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "secret", "default-model"),
                client=client,
            )
            request = ChatRequest(
                model="default-model",
                messages=[ChatMessage(role="user", content="Hi")],
                tools=[{"type": "function", "function": {"name": "read_file"}}],
            )
            return [event async for event in provider.stream(request)]

    events = asyncio.run(run())

    usage = next(event for event in events if isinstance(event, ModelUsage))
    assert usage.input_tokens == 4
    assert usage.output_tokens == 8
    assert usage.total_tokens == 12
    assert usage.uncached_input_tokens == 4
    assert usage.cache_read_input_tokens == 0
    assert usage.cache_miss is True
    assert usage.usage_complete is True
    assert usage.usage_source == "provider"
    assert usage.attempt_count == 1
    assert usage.latency_ms is not None
    events.remove(usage)
    assert events == [
        TextDelta(text="Hello"),
        ToolCall(call_id="call_1", name="read_file", arguments={"path": "README.md"}),
        ModelCompleted(finish_reason="tool_calls"),
    ]
    assert seen_request["model"] == "default-model"
    assert seen_request["stream"] is True
    assert seen_request["tools"] == [{"type": "function", "function": {"name": "read_file"}}]


def test_reused_transport_index_keeps_distinct_call_ids_separate() -> None:
    parts = (
        {
            "index": 0,
            "id": "call-a",
            "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
        },
        {
            "index": 0,
            "id": "call-b",
            "function": {"name": "read_file", "arguments": '{"path":"b.txt"}'},
        },
    )
    stream = (
        "".join(
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {"tool_calls": [part]},
                            "finish_reason": "tool_calls" if offset else None,
                        }
                    ]
                }
            )
            + "\n"
            for offset, part in enumerate(parts)
        )
        + "data: [DONE]\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    assert [event for event in events if isinstance(event, ToolCall)] == [
        ToolCall(call_id="call-a", name="read_file", arguments={"path": "a.txt"}),
        ToolCall(call_id="call-b", name="read_file", arguments={"path": "b.txt"}),
    ]


def test_missing_call_id_is_synthesized_after_unambiguous_assembly() -> None:
    stream = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"name":"read_file","arguments":"{\\"path\\":\\"a.txt\\"}"}}]},'
        '"finish_reason":"tool_calls"}]}\n'
        "data: [DONE]\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    calls = [event for event in asyncio.run(run()) if isinstance(event, ToolCall)]
    assert len(calls) == 1
    assert calls[0].call_id.startswith("call_local_")
    assert calls[0].arguments == {"path": "a.txt"}


@pytest.mark.parametrize(
    "second",
    [
        {"index": 1, "id": "same", "function": {"name": "read_file", "arguments": '{"path":"b"}'}},
    ],
)
def test_conflicting_call_ids_fail_closed(second: dict[str, object]) -> None:
    first = {
        "index": 0,
        "id": "same",
        "function": {"name": "read_file", "arguments": '{"path":"a"}'},
    }
    stream = (
        "data: "
        + json.dumps(
            {
                "choices": [
                    {
                        "delta": {"tool_calls": [first, second]},
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        + "\ndata: [DONE]\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model", max_attempts=1),
                client=client,
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    assert len(events) == 1
    assert isinstance(events[0], ModelError)
    assert events[0].code == "ambiguous_tool_call"


def test_identical_parallel_calls_with_distinct_ids_are_preserved() -> None:
    calls = [
        {
            "index": index,
            "id": call_id,
            "function": {"name": "browser_snapshot", "arguments": '{"tab_ref":"same"}'},
        }
        for index, call_id in enumerate(("snapshot-a", "snapshot-b"))
    ]
    stream = (
        "data: "
        + json.dumps({"choices": [{"delta": {"tool_calls": calls}, "finish_reason": "tool_calls"}]})
        + "\ndata: [DONE]\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model", max_attempts=1),
                client=client,
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    tool_calls = [event for event in events if isinstance(event, ToolCall)]
    assert [event.call_id for event in tool_calls] == ["snapshot-a", "snapshot-b"]


def test_uses_full_endpoint_without_duplicating_path() -> None:
    requested_url = ""

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested_url
        requested_url = str(request.url)
        return httpx.Response(200, text="data: [DONE]\n")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1/chat/completions", "", "fallback"),
                client=client,
            )
            request = ChatRequest(model="chosen", messages=[])
            _ = [event async for event in provider.stream(request)]

    asyncio.run(run())
    assert requested_url == "https://models.example/v1/chat/completions"


def test_redacts_api_key_from_http_error() -> None:
    secret = "sk-never-print-this"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"invalid token {secret}")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", secret, "model"), client=client
            )
            events = [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]
            assert events == [
                ModelError(
                    message="provider returned HTTP 401: invalid token [REDACTED]",
                    code="provider_auth_error",
                    failure_kind=ProviderFailureKind.AUTHORIZATION,
                )
            ]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "body", "code", "failure_kind", "retryable"),
    [
        (
            401,
            '{"error":{"code":"invalid_api_key"}}',
            "provider_auth_error",
            ProviderFailureKind.AUTHORIZATION,
            False,
        ),
        (
            403,
            '{"error":{"code":"permission_denied"}}',
            "provider_permission_denied",
            ProviderFailureKind.PERMISSION,
            False,
        ),
        (
            402,
            '{"error":{"code":"billing_hard_limit_reached"}}',
            "provider_quota_exhausted",
            ProviderFailureKind.QUOTA,
            False,
        ),
        (
            429,
            '{"error":{"type":"invalid_request_error","code":"insufficient_quota"}}',
            "provider_quota_exhausted",
            ProviderFailureKind.QUOTA,
            False,
        ),
        (
            500,
            "upstream unavailable",
            "provider_transient_http",
            ProviderFailureKind.TRANSIENT,
            True,
        ),
        (
            400,
            '{"error":{"code":"invalid_request_error"}}',
            "provider_configuration_error",
            ProviderFailureKind.CONFIGURATION,
            False,
        ),
    ],
)
def test_http_failures_have_typed_retry_semantics(
    status: int,
    body: str,
    code: str,
    failure_kind: ProviderFailureKind,
    retryable: bool,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    async def run() -> ModelError:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig(
                    "https://models.example/v1",
                    "secret",
                    "model",
                    max_attempts=1,
                ),
                client=client,
            )
            events = [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]
            assert len(events) == 1
            assert isinstance(events[0], ModelError)
            return events[0]

    event = asyncio.run(run())
    assert event.code == code
    assert event.failure_kind is failure_kind
    assert event.retryable is retryable


def test_stream_payload_quota_error_is_terminal_and_typed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                'data: {"error":{"type":"invalid_request_error",'
                '"code":"insufficient_quota",'
                '"message":"quota exhausted"}}\n\ndata: [DONE]\n\n'
            ),
        )

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "secret", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    assert asyncio.run(run()) == [
        ModelError(
            message="quota exhausted",
            code="provider_quota_exhausted",
            failure_kind=ProviderFailureKind.QUOTA,
        )
    ]


def test_invalid_stream_json_has_stable_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: not-json\n")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "secret", "model"), client=client
            )
            events = [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]
            assert events == [
                ModelError(
                    message="provider returned invalid JSON in event stream",
                    code="malformed_stream",
                    failure_kind=ProviderFailureKind.FORMATTING,
                )
            ]

    asyncio.run(run())


def test_retries_rate_limit_before_output_and_honors_retry_after() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="busy")
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\ndata: [DONE]\n',
        )

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    assert asyncio.run(run()) == [TextDelta(text="ok"), ModelCompleted(finish_reason="stop")]
    assert calls == 2


def test_provider_concurrency_cap_is_shared_across_requests() -> None:
    active = 0
    maximum_active = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            await asyncio.sleep(0.01)
            return httpx.Response(
                200,
                text=(
                    'data: {"choices":[{"delta":{"content":"ok"},'
                    '"finish_reason":"stop"}]}\ndata: [DONE]\n'
                ),
            )
        finally:
            active -= 1

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig(
                    "https://models.example/v1",
                    "",
                    "model",
                    max_concurrent_requests=1,
                ),
                client=client,
            )

            async def consume() -> None:
                _ = [
                    event
                    async for event in provider.stream(ChatRequest(model="model", messages=[]))
                ]

            await asyncio.gather(consume(), consume())

    asyncio.run(run())
    assert maximum_active == 1


def test_cancelling_active_stream_releases_shared_provider_admission() -> None:
    calls = 0

    async def run() -> tuple[int, list[object]]:
        nonlocal calls
        first_chunk_sent = asyncio.Event()
        release_rest = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(200, stream=_GatedSSEStream(first_chunk_sent, release_rest))
            return httpx.Response(
                200,
                text=(
                    'data: {"choices":[{"delta":{"content":"recovered"},'
                    '"finish_reason":"stop"}]}\ndata: [DONE]\n'
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model", max_concurrent_requests=1),
                client=client,
            )

            async def consume_first() -> None:
                _ = [
                    event
                    async for event in provider.stream(ChatRequest(model="model", messages=[]))
                ]

            active = asyncio.create_task(consume_first())
            await asyncio.wait_for(first_chunk_sent.wait(), timeout=1)
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
            recovered = [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]
            return provider._request_gate.active, recovered

    active, recovered = asyncio.run(run())
    assert active == 0
    assert recovered == [
        TextDelta(text="recovered"),
        ModelCompleted(finish_reason="stop"),
    ]


@pytest.mark.parametrize("partial, expected_calls", [(False, 3), (True, 1)])
def test_read_timeout_retries_only_before_any_output(partial: bool, expected_calls: int) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=_ReadTimeoutStream(partial=partial))

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig(
                    "https://models.example/v1",
                    "",
                    "model",
                    max_attempts=3,
                    retry_base_seconds=0.001,
                ),
                client=client,
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    assert calls == expected_calls
    assert events[-1] == ModelError(
        message="provider read timeout (ReadTimeout): provider stopped producing events",
        retryable=True,
        code="read_timeout",
        partial=partial,
        failure_kind=ProviderFailureKind.TRANSIENT,
    )
    assert (events[:-1] == [TextDelta(text="partial")]) is partial or events[:-1] == [
        TextDelta(text="partial"),
        ModelUsage(usage_complete=False, attempt_count=1),
    ]


@pytest.mark.parametrize(
    ("exception_type", "expected_code"),
    [
        (httpx.ConnectTimeout, "connect_timeout"),
        (httpx.ReadTimeout, "read_timeout"),
        (httpx.WriteTimeout, "write_timeout"),
        (httpx.PoolTimeout, "pool_timeout"),
    ],
)
def test_timeout_subtypes_are_reported_without_conflating_phases(
    exception_type: type[httpx.TimeoutException], expected_code: str
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise exception_type("timed out", request=request)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig(
                    "https://models.example/v1",
                    "",
                    "model",
                    max_attempts=1,
                ),
                client=client,
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())

    assert events == [
        ModelError(
            message=(
                f"provider {expected_code.replace('_', ' ')} ({exception_type.__name__}): timed out"
            ),
            retryable=True,
            code=expected_code,
            failure_kind=ProviderFailureKind.TRANSIENT,
        )
    ]


def test_partial_stream_uses_shorter_progress_deadline_without_replaying_request() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=_StalledAfterPartialStream())

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig(
                    "https://models.example/v1",
                    "",
                    "model",
                    read_timeout=5,
                    partial_read_timeout=0.01,
                    max_attempts=3,
                ),
                client=client,
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    assert calls == 1
    assert events == [
        TextDelta(text="partial"),
        ModelUsage(usage_complete=False, attempt_count=1),
        ModelError(
            message="provider stopped producing events after partial output",
            retryable=True,
            code="read_timeout",
            partial=True,
            failure_kind=ProviderFailureKind.TRANSIENT,
        ),
    ]


def test_rate_limit_reduces_shared_concurrency_and_success_recovers_gradually() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="busy")
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"ok"},'
                '"finish_reason":"stop"}]}\ndata: [DONE]\n'
            ),
        )

    async def run() -> tuple[int, int]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig(
                    "https://models.example/v1",
                    "",
                    "model",
                    max_concurrent_requests=4,
                    retry_base_seconds=0.001,
                ),
                client=client,
            )
            _ = [event async for event in provider.stream(ChatRequest(model="model", messages=[]))]
            reduced = provider._request_gate.limit
            _ = [event async for event in provider.stream(ChatRequest(model="model", messages=[]))]
            return reduced, provider._request_gate.limit

    assert asyncio.run(run()) == (2, 3)


def test_provider_quota_headers_proactively_delay_the_next_request() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        headers = (
            {
                "x-ratelimit-remaining-requests": "0",
                "x-ratelimit-reset-requests": "20ms",
            }
            if calls == 1
            else {}
        )
        return httpx.Response(200, headers=headers, text="data: [DONE]\n")

    async def run() -> float:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig(
                    "https://models.example/v1",
                    "",
                    "model",
                    retry_base_seconds=0.001,
                    retry_max_seconds=0.1,
                ),
                client=client,
            )
            _ = [event async for event in provider.stream(ChatRequest(model="model", messages=[]))]
            started = asyncio.get_running_loop().time()
            _ = [event async for event in provider.stream(ChatRequest(model="model", messages=[]))]
            return asyncio.get_running_loop().time() - started

    assert asyncio.run(run()) >= 0.015


def test_provider_quota_remaining_reduces_shared_admission_limit() -> None:
    async def run() -> int:
        gate = _AdaptiveRequestGate(8, 0.001, 0.1)
        await gate.observe_quota({"ratelimit-remaining": "2"})
        return gate.limit

    assert asyncio.run(run()) == 2


def test_provider_exposes_documented_request_and_token_quota_metadata() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "x-ratelimit-limit-requests": "1000",
                "x-ratelimit-remaining-requests": "999",
                "x-ratelimit-reset-requests": "250ms",
                "x-ratelimit-limit-tokens": "200000",
                "x-ratelimit-remaining-tokens": "198765",
                "x-ratelimit-reset-tokens": "1.5s",
            },
            text="data: [DONE]\n",
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            _ = [event async for event in provider.stream(ChatRequest(model="model", messages=[]))]
            return provider.quota_snapshot

    assert asyncio.run(run()) == ProviderQuotaSnapshot(
        limit_requests=1000,
        remaining_requests=999,
        reset_requests_seconds=0.25,
        limit_tokens=200000,
        remaining_tokens=198765,
        reset_tokens_seconds=1.5,
    )


def test_provider_ignores_malformed_quota_metadata() -> None:
    async def run():
        gate = _AdaptiveRequestGate(8, 0.001, 0.1)
        observed = await gate.observe_quota(
            {
                "x-ratelimit-limit-requests": "unknown",
                "x-ratelimit-remaining-requests": "-1",
                "x-ratelimit-reset-tokens": "eventually",
            }
        )
        return observed, gate.quota_snapshot, gate.limit

    assert asyncio.run(run()) == (None, None, 8)


def test_provider_gate_is_fifo_and_removes_cancelled_waiters() -> None:
    async def run() -> tuple[list[str], int, int]:
        gate = _AdaptiveRequestGate(1, 0.001, 0.01)
        order: list[str] = []
        release_first = asyncio.Event()

        async def enter(label: str, hold: asyncio.Event | None = None) -> None:
            await gate.acquire()
            order.append(label)
            if hold is not None:
                await hold.wait()
            await gate.release(completed=True)

        first = asyncio.create_task(enter("first", release_first))
        while gate.active == 0:
            await asyncio.sleep(0)
        cancelled = asyncio.create_task(enter("cancelled"))
        third = asyncio.create_task(enter("third"))
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        release_first.set()
        await asyncio.gather(first, third)
        return order, gate.active, len(gate._waiters)

    assert asyncio.run(run()) == (["first", "third"], 0, 0)


def test_provider_gate_round_robins_campaigns_and_keeps_campaign_fifo() -> None:
    async def run() -> list[str]:
        gate = _AdaptiveRequestGate(1, 0.001, 0.01)
        order: list[str] = []
        release_first = asyncio.Event()

        async def enter(label: str, group: str, hold: asyncio.Event | None = None) -> None:
            await gate.acquire(group)
            order.append(label)
            if hold is not None:
                await hold.wait()
            await gate.release(completed=True)

        first = asyncio.create_task(enter("a1", "campaign/a", release_first))
        while gate.active == 0:
            await asyncio.sleep(0)
        a2 = asyncio.create_task(enter("a2", "campaign/a"))
        a3 = asyncio.create_task(enter("a3", "campaign/a"))
        b1 = asyncio.create_task(enter("b1", "campaign/b"))
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(first, a2, a3, b1)
        return order

    assert asyncio.run(run()) == ["a1", "b1", "a2", "a3"]
    assert _admission_group("campaign/abc/wave-1/worker") == "campaign/abc"
    assert _admission_group("interactive-1") == "interactive-1"


def test_incomplete_stream_preserves_partial_without_retry() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, text='data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n'
        )

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    assert asyncio.run(run()) == [
        TextDelta(text="partial"),
        ModelUsage(usage_complete=False, attempt_count=1),
        ModelError(
            message="provider stream ended before [DONE]",
            retryable=True,
            code="incomplete_stream",
            partial=True,
            failure_kind=ProviderFailureKind.TRANSIENT,
        ),
    ]
    assert calls == 1


def test_partial_remote_disconnect_is_retryable_by_kernel() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_DisconnectedStream())

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    assert asyncio.run(run()) == [
        TextDelta(text="partial"),
        ModelUsage(usage_complete=False, attempt_count=1),
        ModelError(
            message=(
                "provider request failed: peer closed connection without sending complete "
                "message body"
            ),
            retryable=True,
            code="transport_error",
            partial=True,
            failure_kind=ProviderFailureKind.TRANSIENT,
        ),
    ]


def test_streamed_refusal_is_terminal_even_when_finish_reason_is_stop() -> None:
    stream = (
        'data: {"choices":[{"delta":{"refusal":"I cannot assist."},'
        '"finish_reason":"stop"}]}\n'
        "data: [DONE]\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    assert asyncio.run(run()) == [
        ModelWarning(code="refusal", message="I cannot assist."),
        ModelCompleted(finish_reason="stop", disposition="refused"),
    ]


@pytest.mark.parametrize(
    "tool_call",
    [
        '{"index":0,"id":"call-1","function":{"arguments":"{}"}}',
        '{"index":0,"id":"call-1","function":{"name":"read_file","arguments":"[1]"}}',
    ],
)
def test_malformed_streamed_tool_calls_fail_closed(tool_call: str) -> None:
    stream = (
        f'data: {{"choices":[{{"delta":{{"tool_calls":[{tool_call}]}},'
        '"finish_reason":"tool_calls"}]}\n'
        "data: [DONE]\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    assert len(events) == 1
    assert isinstance(events[0], ModelError)
    assert events[0].code == "malformed_tool_call"
    assert events[0].retryable is True
    if '"[1]"' in tool_call:
        assert "assembly=single" in events[0].message
        assert "chunks=1" in events[0].message
        assert "digest=" in events[0].message
        assert "[1]" not in events[0].message


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            DSML_OBSERVED_PARAMETERS,
            {"context_id": "ctx-1", "tab_ref": "tab:1"},
        ),
        (
            DSML_DIRECT_JSON,
            {"context_id": "ctx-1", "tab_ref": "tab:1"},
        ),
    ],
)
def test_deepseek_v4_dsml_text_becomes_typed_tool_call(body: str, expected: dict[str, str]) -> None:
    midpoint = len(body) // 2
    stream = "".join(
        [
            "data: "
            + json.dumps(
                {"choices": [{"delta": {"content": body[:midpoint]}}]},
                ensure_ascii=False,
            )
            + "\n",
            "data: "
            + json.dumps(
                {"choices": [{"delta": {"content": body[midpoint:]}, "finish_reason": "stop"}]},
                ensure_ascii=False,
            )
            + "\n",
            "data: [DONE]\n",
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig(
                    "https://models.example/v1", "", "deepseek-v4-flash", max_attempts=1
                ),
                client=client,
            )
            request = ChatRequest(
                model="deepseek-v4-flash",
                messages=[],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "browser_snapshot",
                            "description": "Snapshot",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )
            return [event async for event in provider.stream(request)]

    events = asyncio.run(run())
    calls = [event for event in events if isinstance(event, ToolCall)]
    assert len(calls) == 1
    assert calls[0].name == "browser_snapshot"
    assert calls[0].arguments == expected
    assert not any(isinstance(event, TextDelta) for event in events)
    assert isinstance(events[-1], ModelCompleted)


@pytest.mark.parametrize(
    "body",
    [
        '<｜DSML｜tool_calls><｜DSML｜invoke name="browser_snapshot">',
        DSML_UNKNOWN_TOOL,
        DSML_INVALID_TYPED_VALUE,
    ],
)
def test_deepseek_v4_malformed_or_unadmitted_dsml_fails_without_leaking(body: str) -> None:
    stream = (
        "data: "
        + json.dumps(
            {"choices": [{"delta": {"content": body}, "finish_reason": "stop"}]},
            ensure_ascii=False,
        )
        + "\ndata: [DONE]\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig(
                    "https://models.example/v1", "", "deepseek-v4-flash", max_attempts=1
                ),
                client=client,
            )
            request = ChatRequest(
                model="deepseek-v4-flash",
                messages=[],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "browser_snapshot",
                            "description": "Snapshot",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )
            return [event async for event in provider.stream(request)]

    events = asyncio.run(run())
    assert len(events) == 1
    assert isinstance(events[0], ModelError)
    assert events[0].code == "malformed_tool_call"
    assert body not in events[0].message


def test_cumulative_streamed_tool_arguments_use_latest_snapshot() -> None:
    stream = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1",'
        '"function":{"name":"shell_command","arguments":"{\\"command\\":\\"ec"}}]}}]}\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"{\\"command\\":\\"echo safe\\"}"}}]},'
        '"finish_reason":"tool_calls"}]}\n'
        "data: [DONE]\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    assert (
        ToolCall(call_id="call-1", name="shell_command", arguments={"command": "echo safe"})
        in events
    )
    assert isinstance(events[-1], ModelCompleted)


def test_mixed_snapshot_and_fragment_tool_arguments_recover_unambiguously() -> None:
    chunks = (
        ("call-1", "assessment_prepare", '{"adapter":{"interface":"http'),
        ("call-1", "assessment_prepare", '{"adapter":{"interface":"http_api","endpoint":"/api'),
        ("", "", '/message","api_steps":['),
        ("", "", '{"step_id":"send"}]}'),
        ("", "", "}"),
    )
    lines = []
    for call_id, name, arguments in chunks:
        part = {
            "index": 0,
            "id": call_id,
            "function": {"name": name, "arguments": arguments},
        }
        lines.append("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [part]}}]}) + "\n")
    lines.append('data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n')
    lines.append("data: [DONE]\n")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="".join(lines))

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    assert (
        ToolCall(
            call_id="call-1",
            name="assessment_prepare",
            arguments={
                "adapter": {
                    "interface": "http_api",
                    "endpoint": "/api/message",
                    "api_steps": [{"step_id": "send"}],
                }
            },
        )
        in events
    )
    assert isinstance(events[-1], ModelCompleted)


def test_overlapping_fragment_boundaries_recover_only_one_valid_object() -> None:
    stream = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1",'
        '"function":{"name":"read_file","arguments":"{\\"path\\":\\"safe.txt\\","}}]}}]}\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":",\\"mode\\":\\"r\\"}"}}]},'
        '"finish_reason":"tool_calls"}]}\n'
        "data: [DONE]\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    assert (
        ToolCall(
            call_id="call-1",
            name="read_file",
            arguments={"path": "safe.txt", "mode": "r"},
        )
        in events
    )
    assert isinstance(events[-1], ModelCompleted)


def test_valid_fragments_win_over_lossy_overlap_recovery_candidate() -> None:
    chunks = [
        "{",
        '"',
        "url",
        '"',
        ": ",
        '"',
        "https",
        "://",
        "pl",
        "under",
        ".me",
        '"',
        ", ",
        '"',
        "step",
        '"',
        ": ",
        "0",
        ", ",
        '"',
        "time",
        "out",
        "_ms",
        '"',
        ": ",
        "300",
        "00",
        "}",
    ]
    lines = []
    for index, arguments in enumerate(chunks):
        part = {
            "index": 0,
            "id": "call-1" if index == 0 else "",
            "function": {
                "name": "browser_open_tab" if index == 0 else "",
                "arguments": arguments,
            },
        }
        lines.append("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [part]}}]}) + "\n")
    lines.append('data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n')
    lines.append("data: [DONE]\n")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="".join(lines))

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    assert (
        ToolCall(
            call_id="call-1",
            name="browser_open_tab",
            arguments={"url": "https://plunder.me", "step": 0, "timeout_ms": 30_000},
        )
        in events
    )
    assert isinstance(events[-1], ModelCompleted)


def test_literal_newlines_inside_tool_string_are_decoded_without_relaxing_structure() -> None:
    arguments = json.dumps({"argv": ["bash", "-c", "python3 - <<'EOF'\nprint('ok')\nEOF"]})
    arguments = arguments.replace("\\n", "\n")
    stream = "".join(
        [
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "function": {
                                            "name": "shell_command",
                                            "arguments": arguments,
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            )
            + "\n",
            "data: [DONE]\n",
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    assert (
        ToolCall(
            call_id="call-1",
            name="shell_command",
            arguments={"argv": ["bash", "-c", "python3 - <<'EOF'\nprint('ok')\nEOF"]},
        )
        in events
    )
    assert isinstance(events[-1], ModelCompleted)


def test_object_tool_arguments_are_encoded_as_json_not_python_repr() -> None:
    stream = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1",'
        '"function":{"name":"assessment_prepare","arguments":{"profile":"recommended"}}}]},'
        '"finish_reason":"tool_calls"}]}\n'
        "data: [DONE]\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    assert (
        ToolCall(
            call_id="call-1",
            name="assessment_prepare",
            arguments={"profile": "recommended"},
        )
        in events
    )


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ('```json\n{"text":"FareCare"}\n```', {"text": "FareCare"}),
        ('"{\\"text\\":\\"FareCare\\"}"', {"text": "FareCare"}),
    ],
)
def test_unambiguous_provider_json_wrappers_are_normalized(
    arguments: str, expected: dict[str, object]
) -> None:
    part = {
        "index": 0,
        "id": "call-1",
        "function": {"name": "browser_wait_for_text", "arguments": arguments},
    }
    stream = (
        "data: "
        + json.dumps(
            {"choices": [{"delta": {"tool_calls": [part]}, "finish_reason": "tool_calls"}]}
        )
        + "\ndata: [DONE]\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    assert ToolCall(call_id="call-1", name="browser_wait_for_text", arguments=expected) in events
    assert isinstance(events[-1], ModelCompleted)


@pytest.mark.parametrize(
    "arguments",
    [
        'Use this call: {"text":"FareCare"}',
        '{"text":"FareCare",}',
        '{"text":"FareCare"} {"text":"other"}',
    ],
)
def test_ambiguous_or_malformed_provider_json_is_never_repaired_for_execution(
    arguments: str,
) -> None:
    part = {
        "index": 0,
        "id": "call-1",
        "function": {"name": "browser_wait_for_text", "arguments": arguments},
    }
    stream = (
        "data: "
        + json.dumps(
            {"choices": [{"delta": {"tool_calls": [part]}, "finish_reason": "tool_calls"}]}
        )
        + "\ndata: [DONE]\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    assert len(events) == 1
    assert isinstance(events[0], ModelError)
    assert events[0].code == "malformed_tool_call"


def test_valid_standard_fragments_are_not_overruled_by_overlap_guess() -> None:
    stream = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1",'
        '"function":{"name":"read_file","arguments":"{\\"path\\":\\"a"}}]}}]}\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"a\\"}"}}]},'
        '"finish_reason":"tool_calls"}]}\n'
        "data: [DONE]\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    assert ToolCall(call_id="call-1", name="read_file", arguments={"path": "aa"}) in events
    assert isinstance(events[-1], ModelCompleted)


def test_truncation_capability_drift_and_budget_are_explicit() -> None:
    stream = (
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n'
        'data: {"choices":[],"usage":{"total_tokens":7}}\n'
        "data: [DONE]"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> tuple[list[object], list[object]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig(
                    "https://models.example/v1",
                    "",
                    "model",
                    max_tokens=8,
                    context_window_tokens=131_072,
                ),
                client=client,
            )
            first = [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]
            second = [
                event
                async for event in provider.stream(
                    ChatRequest(model="model", messages=[], max_output_tokens=2)
                )
            ]
            with pytest.raises(ProviderError, match="capability drift"):
                provider.negotiate(ProviderCapabilities(streaming=False, model="model"))
            assert provider.capabilities.context_window_tokens == 131_072
            return first, second

    first, second = asyncio.run(run())
    assert ModelWarning(code="truncated", message="provider stopped at its output limit") in first
    assert ModelCompleted(finish_reason="length", disposition="truncated") in first
    assert second == [
        ModelError(
            message=(
                "cumulative instance token budget exhausted "
                "(ProviderConfig.max_tokens); this is not the per-request output cap — "
                "use default_max_output_tokens for that"
            ),
            code="budget_exhausted",
            failure_kind=ProviderFailureKind.BUDGET,
        )
    ]


def test_output_token_parameter_matches_the_configured_provider_contract() -> None:
    observed = {}
    stream = 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        return httpx.Response(200, text=stream)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig(
                    "https://models.example/v1",
                    "",
                    "model",
                    output_token_parameter="max_tokens",
                    reasoning_parameter="thinking",
                ),
                client=client,
            )
            _ = [
                event
                async for event in provider.stream(
                    ChatRequest(
                        model="model",
                        messages=[],
                        max_output_tokens=64,
                        reasoning="disabled",
                    )
                )
            ]

    asyncio.run(run())
    assert observed["max_tokens"] == 64
    assert "max_completion_tokens" not in observed
    assert observed["thinking"] == {"type": "disabled"}


def test_deepseek_effort_uses_native_thinking_toggle_on_the_streaming_path() -> None:
    observed: dict[str, object] = {}
    observed_url = ""
    stream = 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_url
        observed_url = str(request.url)
        observed.update(json.loads(request.content))
        return httpx.Response(200, text=stream)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig(
                    "https://api.deepseek.com/v1",
                    "secret",
                    "deepseek-chat",
                    output_token_parameter="max_tokens",
                    reasoning_parameter="thinking",
                    reasoning_effort="high",
                ),
                client=client,
            )
            _ = [
                event
                async for event in provider.stream(
                    ChatRequest(model="deepseek-chat", messages=[], max_output_tokens=64)
                )
            ]

    asyncio.run(run())
    assert observed_url == "https://api.deepseek.com/v1/chat/completions"
    assert observed["thinking"] == {"type": "enabled"}
    assert observed["max_tokens"] == 64
    assert "reasoning_effort" not in observed


def test_output_token_parameter_uses_configured_default_when_request_is_unbounded() -> None:
    observed = {}
    stream = 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        return httpx.Response(200, text=stream)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig(
                    "https://models.example/v1",
                    "",
                    "model",
                    default_max_output_tokens=4_096,
                    output_token_parameter="max_tokens",
                ),
                client=client,
            )
            _ = [event async for event in provider.stream(ChatRequest(model="model", messages=[]))]

    asyncio.run(run())
    assert observed["max_tokens"] == 4_096


def test_usage_records_cache_reasoning_and_request_id() -> None:
    stream = (
        'data: {"id":"chatcmpl_1","choices":[{"delta":{"content":"Hi"},'
        '"finish_reason":"stop"}]}\n'
        'data: {"id":"chatcmpl_1","choices":[],"usage":{"prompt_tokens":10,'
        '"completion_tokens":4,"total_tokens":14,'
        '"prompt_tokens_details":{"cached_tokens":6},'
        '"completion_tokens_details":{"reasoning_tokens":3}}}\n'
        "data: [DONE]\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model"), client=client
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    usage = next(event for event in events if isinstance(event, ModelUsage))
    assert usage.input_tokens == 10
    assert usage.output_tokens == 4
    assert usage.total_tokens == 14
    assert usage.uncached_input_tokens == 4
    assert usage.cache_read_input_tokens == 6
    assert usage.cache_creation_input_tokens == 0
    assert usage.reasoning_output_tokens == 3
    assert usage.cache_hit is True
    assert usage.cache_created is False
    assert usage.cache_miss is False
    assert usage.request_id == "chatcmpl_1"
    assert usage.usage_complete is True
    assert usage.usage_source == "provider"
    assert usage.attempt_count == 1
    assert usage.latency_ms is not None


def _usage_with_pricing(**cost_kwargs: float) -> ModelUsage:
    stream = (
        'data: {"id":"chatcmpl_1","choices":[{"delta":{"content":"Hi"},'
        '"finish_reason":"stop"}]}\n'
        'data: {"id":"chatcmpl_1","choices":[],"usage":{"prompt_tokens":10,'
        '"completion_tokens":4,"total_tokens":14,'
        '"prompt_tokens_details":{"cached_tokens":6}}}\n'
        "data: [DONE]\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model", **cost_kwargs),
                client=client,
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run())
    usage = next(event for event in events if isinstance(event, ModelUsage))
    assert usage.cache_read_input_tokens == 6
    assert usage.uncached_input_tokens == 4
    return usage


def test_usage_cost_applies_cache_read_rate() -> None:
    usage = _usage_with_pricing(
        input_cost_per_million=2.0,
        output_cost_per_million=8.0,
        cache_read_input_cost_per_million=0.5,
    )

    assert usage.estimated_cost_usd == pytest.approx((4 * 2.0 + 6 * 0.5 + 4 * 8.0) / 1_000_000)


def test_usage_cost_without_cache_rate_bills_cache_reads_at_input_rate() -> None:
    usage = _usage_with_pricing(input_cost_per_million=2.0, output_cost_per_million=8.0)

    assert usage.estimated_cost_usd == pytest.approx((10 * 2.0 + 4 * 8.0) / 1_000_000)


def test_provider_config_rejects_negative_cache_read_rate() -> None:
    with pytest.raises(ValueError, match="cache_read_input_cost_per_million"):
        ProviderConfig(
            "https://models.example/v1",
            "",
            "model",
            cache_read_input_cost_per_million=-0.5,
        )


def _run_stream(stream: str, *, max_attempts: int | None = None) -> list[object]:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    async def run() -> list[object]:
        config_kwargs = {} if max_attempts is None else {"max_attempts": max_attempts}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model", **config_kwargs),
                client=client,
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    return asyncio.run(run())


def test_interleaved_parallel_call_chunks_assemble_per_call() -> None:
    chunks = [
        {"index": 0, "id": "call-a", "function": {"name": "read_file", "arguments": '{"pa'}},
        {"index": 1, "id": "call-b", "function": {"name": "write_file", "arguments": '{"pa'}},
        {"index": 0, "function": {"arguments": 'th":"a.txt"}'}},
        {"index": 1, "function": {"arguments": 'th":"b.txt"}'}},
    ]
    stream = (
        "".join(
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {"tool_calls": [chunk]},
                            "finish_reason": "tool_calls" if offset == len(chunks) - 1 else None,
                        }
                    ]
                }
            )
            + "\n"
            for offset, chunk in enumerate(chunks)
        )
        + "data: [DONE]\n"
    )

    events = _run_stream(stream)

    assert [event for event in events if isinstance(event, ToolCall)] == [
        ToolCall(call_id="call-a", name="read_file", arguments={"path": "a.txt"}),
        ToolCall(call_id="call-b", name="write_file", arguments={"path": "b.txt"}),
    ]


def test_interleaved_missing_ids_synthesize_distinct_local_ids() -> None:
    chunks = [
        {"index": 0, "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}},
        {"index": 1, "function": {"name": "write_file", "arguments": '{"path":"b.txt"}'}},
    ]
    stream = (
        "data: "
        + json.dumps(
            {"choices": [{"delta": {"tool_calls": chunks}, "finish_reason": "tool_calls"}]}
        )
        + "\ndata: [DONE]\n"
    )

    events = _run_stream(stream)

    calls = [event for event in events if isinstance(event, ToolCall)]
    assert len(calls) == 2
    assert all(call.call_id.startswith("call_local_") for call in calls)
    # Synthesized ids are derived only after unambiguous per-slot assembly, so
    # two id-less parallel calls never collide.
    assert calls[0].call_id != calls[1].call_id
    assert {(call.name, json.dumps(call.arguments, sort_keys=True)) for call in calls} == {
        ("read_file", '{"path": "a.txt"}'),
        ("write_file", '{"path": "b.txt"}'),
    }


class _DisconnectAfterToolStart(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[override]
        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"read_file","arguments":"{\\"pa"}}]}}]}\n'
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )


def test_interrupted_stream_mid_tool_call_yields_one_terminal_result_per_call() -> None:
    async def interrupted_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_DisconnectAfterToolStart())

    async def run_interrupted() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(interrupted_handler)) as client:
            provider = OpenAICompatibleProvider(
                ProviderConfig("https://models.example/v1", "", "model", max_attempts=1),
                client=client,
            )
            return [
                event async for event in provider.stream(ChatRequest(model="model", messages=[]))
            ]

    events = asyncio.run(run_interrupted())

    # The interrupted attempt persists no call and ends in exactly one terminal
    # error — never a partial ToolCall and never a ModelCompleted.
    assert not any(isinstance(event, ToolCall) for event in events)
    assert not any(isinstance(event, ModelCompleted) for event in events)
    assert len(events) == 1
    assert isinstance(events[0], ModelError)
    assert events[0].code == "transport_error"
    assert events[0].retryable is True

    restarted = _run_stream(RECORDED_STREAM)

    # Across interruption + restart the call is persisted exactly once.
    assert [event for event in restarted if isinstance(event, ToolCall)] == [
        ToolCall(call_id="call_1", name="read_file", arguments={"path": "README.md"})
    ]
    assert isinstance(restarted[-1], ModelCompleted)
