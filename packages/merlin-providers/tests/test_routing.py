from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from merlin_contracts import (
    ChatMessage,
    ChatRequest,
    ModelCompleted,
    ModelEvent,
    TextDelta,
    ToolCall,
)
from pytest import LogCaptureFixture

from merlin_providers import ModelRoutingProvider, ProviderCapabilities, RouteIdentity


class Provider:
    def __init__(self, name: str, context_window_tokens: int = 8_192) -> None:
        self.name = name
        self.closed = False
        self.capabilities = SimpleNamespace(context_window_tokens=context_window_tokens)
        self.route_identity: object | None = None

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        yield TextDelta(text=f"{self.name}:{request.model}")
        yield ModelCompleted()

    async def close(self) -> None:
        self.closed = True


class InteractiveProvider(Provider):
    def stream_with_tools(self, request: ChatRequest, handler: object) -> AsyncIterator[ModelEvent]:
        return self.stream(request)


def test_model_routing_is_exact_and_defaults_safely() -> None:
    async def scenario() -> None:
        default = Provider("default")
        lead = Provider("lead", context_window_tokens=4_096)
        provider = ModelRoutingProvider(default, {"strong": lead})
        assert provider.capabilities_for_model("strong").context_window_tokens == 4_096
        assert provider.capabilities_for_model("routine").context_window_tokens == 8_192
        strong = [
            event
            async for event in provider.stream(
                ChatRequest(model="strong", messages=[ChatMessage(role="user", content="hello")])
            )
        ]
        fallback = [
            event
            async for event in provider.stream(
                ChatRequest(model="routine", messages=[ChatMessage(role="user", content="hello")])
            )
        ]
        assert isinstance(strong[0], TextDelta) and strong[0].text == "lead:strong"
        assert isinstance(fallback[0], TextDelta) and fallback[0].text == "default:routine"
        await provider.close()
        assert default.closed and lead.closed

    asyncio.run(scenario())


def test_route_identity_for_model_returns_exact_routed_identity() -> None:
    default = Provider("default")
    default.route_identity = RouteIdentity(
        transport="openai_compatible", endpoint="https://api.example/v1", model="routine"
    )
    lead = Provider("lead")
    lead.route_identity = RouteIdentity(transport="claude_cli", endpoint="", model="strong")
    provider = ModelRoutingProvider(default, {"strong": lead})

    assert provider.route_identity_for_model("strong") == RouteIdentity(
        transport="claude_cli", endpoint="", model="strong"
    )
    assert provider.route_identity_for_model("unknown") == RouteIdentity(
        transport="openai_compatible", endpoint="https://api.example/v1", model="unknown"
    )


def test_shared_endpoint_reports_requested_model_metadata_and_logs(
    caplog: LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        shared = InteractiveProvider("shared")
        shared.capabilities = ProviderCapabilities(
            streaming=True,
            tools=True,
            usage_in_stream=True,
            model="deepseek-v4-flash",
            vision=True,
            context_window_tokens=131_072,
        )
        shared.route_identity = RouteIdentity(
            transport="openai_compatible",
            endpoint="https://api.example/v1",
            model="deepseek-v4-flash",
        )
        shared.api_key = "DO-NOT-LOG-API-KEY"
        provider = ModelRoutingProvider(shared, {})

        pro_capabilities = provider.capabilities_for_model("deepseek-v4-pro")
        assert pro_capabilities is not None
        assert pro_capabilities.model == "deepseek-v4-pro"
        assert pro_capabilities.streaming is True
        assert pro_capabilities.tools is True
        assert pro_capabilities.usage_in_stream is True
        assert pro_capabilities.vision is None
        assert pro_capabilities.context_window_tokens is None
        assert provider.route_identity_for_model("deepseek-v4-pro") == RouteIdentity(
            transport="openai_compatible",
            endpoint="https://api.example/v1",
            model="deepseek-v4-pro",
        )

        flash_capabilities = provider.capabilities_for_model("deepseek-v4-flash")
        assert flash_capabilities is shared.capabilities
        assert flash_capabilities.vision is True
        assert flash_capabilities.context_window_tokens == 131_072

        secret_prompt = "DO-NOT-LOG-PROMPT"
        for model in ("deepseek-v4-pro", "deepseek-v4-flash"):
            events = [
                event
                async for event in provider.stream(
                    ChatRequest(
                        model=model,
                        messages=[ChatMessage(role="user", content=secret_prompt)],
                    )
                )
            ]
            assert isinstance(events[0], TextDelta)
            assert events[0].text == f"shared:{model}"

        interactive_events = [
            event
            async for event in provider.stream_with_tools(
                ChatRequest(
                    model="deepseek-v4-pro",
                    messages=[ChatMessage(role="user", content=secret_prompt)],
                ),
                lambda *_args: None,
            )
        ]
        assert isinstance(interactive_events[0], TextDelta)

        explicit = Provider("explicit")
        explicit.route_identity = RouteIdentity(
            transport="openai_compatible",
            endpoint="https://api.example/v1",
            model="deepseek-v4-pro",
        )
        explicit_provider = ModelRoutingProvider(shared, {"deepseek-v4-pro": explicit})
        explicit_events = [
            event
            async for event in explicit_provider.stream(
                ChatRequest(
                    model="deepseek-v4-pro",
                    messages=[ChatMessage(role="user", content=secret_prompt)],
                )
            )
        ]
        assert isinstance(explicit_events[0], TextDelta)

        assert shared.capabilities.model == "deepseek-v4-flash"
        assert shared.route_identity.model == "deepseek-v4-flash"

    caplog.set_level("INFO", logger="merlin_providers.routing")
    asyncio.run(scenario())

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "model=deepseek-v4-pro" in message
        and "explicit_route=False" in message
        and "interactive=False" in message
        for message in messages
    )
    assert any(
        "model=deepseek-v4-flash" in message
        and "explicit_route=False" in message
        and "interactive=False" in message
        for message in messages
    )
    assert any(
        "model=deepseek-v4-pro" in message
        and "explicit_route=False" in message
        and "interactive=True" in message
        for message in messages
    )
    assert any(
        "model=deepseek-v4-pro" in message
        and "explicit_route=True" in message
        and "interactive=False" in message
        for message in messages
    )
    assert all("DO-NOT-LOG-PROMPT" not in message for message in messages)
    assert all("DO-NOT-LOG-API-KEY" not in message for message in messages)


def test_explicit_route_model_mismatch_fails_loudly() -> None:
    default = Provider("default")
    mismatched = Provider("mismatched")
    mismatched.route_identity = RouteIdentity(
        transport="openai_compatible",
        endpoint="https://api.example/v1",
        model="deepseek-v4-flash",
    )
    provider = ModelRoutingProvider(default, {"deepseek-v4-pro": mismatched})
    request = ChatRequest(
        model="deepseek-v4-pro", messages=[ChatMessage(role="user", content="hello")]
    )

    with pytest.raises(ValueError, match="explicit provider route model mismatch"):
        provider.route_identity_for_model("deepseek-v4-pro")
    with pytest.raises(ValueError, match="explicit provider route model mismatch"):
        provider.capabilities_for_model("deepseek-v4-pro")
    with pytest.raises(ValueError, match="explicit provider route model mismatch"):
        provider.stream(request)
    with pytest.raises(ValueError, match="explicit provider route model mismatch"):
        provider.stream_with_tools(request, lambda *_args: None)


def test_explicit_route_capability_only_model_mismatch_fails_loudly() -> None:
    default = Provider("default")
    mismatched = Provider("mismatched")
    mismatched.capabilities = ProviderCapabilities(model="deepseek-v4-flash")
    provider = ModelRoutingProvider(default, {"deepseek-v4-pro": mismatched})
    request = ChatRequest(
        model="deepseek-v4-pro", messages=[ChatMessage(role="user", content="hello")]
    )

    with pytest.raises(ValueError, match="capabilities declares 'deepseek-v4-flash'"):
        provider.route_identity_for_model("deepseek-v4-pro")
    with pytest.raises(ValueError, match="capabilities declares 'deepseek-v4-flash'"):
        provider.capabilities_for_model("deepseek-v4-pro")
    with pytest.raises(ValueError, match="capabilities declares 'deepseek-v4-flash'"):
        provider.stream(request)
    with pytest.raises(ValueError, match="capabilities declares 'deepseek-v4-flash'"):
        provider.stream_with_tools(request, lambda *_args: None)


def test_explicit_route_identity_capability_conflict_fails_loudly() -> None:
    default = Provider("default")
    mismatched = Provider("mismatched")
    mismatched.route_identity = RouteIdentity(
        transport="openai_compatible",
        endpoint="https://api.example/v1",
        model="deepseek-v4-pro",
    )
    mismatched.capabilities = ProviderCapabilities(model="deepseek-v4-flash")
    provider = ModelRoutingProvider(default, {"deepseek-v4-pro": mismatched})
    request = ChatRequest(
        model="deepseek-v4-pro", messages=[ChatMessage(role="user", content="hello")]
    )

    with pytest.raises(ValueError, match="capabilities declares 'deepseek-v4-flash'"):
        provider.route_identity_for_model("deepseek-v4-pro")
    with pytest.raises(ValueError, match="capabilities declares 'deepseek-v4-flash'"):
        provider.capabilities_for_model("deepseek-v4-pro")
    with pytest.raises(ValueError, match="capabilities declares 'deepseek-v4-flash'"):
        provider.stream(request)
    with pytest.raises(ValueError, match="capabilities declares 'deepseek-v4-flash'"):
        provider.stream_with_tools(request, lambda *_args: None)


def test_providers_expose_exact_route_identities() -> None:
    from merlin_providers import (
        ClaudeCliConfig,
        ClaudeCliProvider,
        GeminiConfig,
        GeminiProvider,
        OpenAICompatibleProvider,
        ProviderConfig,
        RouteIdentity,
    )

    config = ProviderConfig("https://api.example/v1", "key", "acme-1")
    assert OpenAICompatibleProvider(config).route_identity == RouteIdentity(
        transport="openai_compatible", endpoint="https://api.example/v1", model="acme-1"
    )
    assert GeminiProvider(GeminiConfig(**asdict(config))).route_identity == RouteIdentity(
        transport="gemini", endpoint="https://api.example/v1", model="acme-1"
    )
    assert ClaudeCliProvider(ClaudeCliConfig(model="acme-1")).route_identity == RouteIdentity(
        transport="claude_cli", endpoint="", model="acme-1"
    )


def test_capabilities_for_model_resolves_from_registry() -> None:
    from merlin_providers.capabilities import ModelCapabilityRegistry

    default = Provider("default")
    default.route_identity = RouteIdentity(
        transport="openai_compatible", endpoint="https://api.example/v1", model="acme-1"
    )
    registry = ModelCapabilityRegistry()
    provider = ModelRoutingProvider(default, {}, capability_registry=registry)

    capabilities = provider.capabilities_for_model("kimi-k2-0905")
    assert capabilities is not None
    assert capabilities.model == "kimi-k2-0905"
    assert capabilities.context_window_tokens == 262_144
    assert capabilities.vision is None


def test_capabilities_for_model_config_override_wins() -> None:
    from merlin_providers.capabilities import (
        ModelCapabilityRegistry,
        OperatorOverride,
        RouteIdentity,
    )

    default = Provider("default")
    default.route_identity = RouteIdentity(
        transport="openai_compatible", endpoint="https://api.example/v1", model="acme-1"
    )
    override = OperatorOverride(
        route=RouteIdentity(
            transport="openai_compatible",
            endpoint="https://api.example/v1",
            model="kimi-k2-0905",
        ),
        values={"context_window_tokens": 64_000},
    )
    registry = ModelCapabilityRegistry(overrides=[override])
    provider = ModelRoutingProvider(default, {}, capability_registry=registry)

    capabilities = provider.capabilities_for_model("kimi-k2-0905")
    assert capabilities is not None
    assert capabilities.context_window_tokens == 64_000


def test_capabilities_for_model_unknown_model_warns_and_stays_unknown(
    caplog: LogCaptureFixture,
) -> None:
    from merlin_providers.capabilities import ModelCapabilityRegistry

    default = Provider("default")
    default.route_identity = RouteIdentity(
        transport="openai_compatible", endpoint="https://api.example/v1", model="acme-1"
    )
    provider = ModelRoutingProvider(default, {}, capability_registry=ModelCapabilityRegistry())

    with caplog.at_level("WARNING", logger="merlin_providers.routing"):
        capabilities = provider.capabilities_for_model("totally-unknown-9")
    assert capabilities is not None
    assert capabilities.model == "totally-unknown-9"
    assert capabilities.context_window_tokens is None
    assert any(
        "capabilities unknown for model=totally-unknown-9" in record.getMessage()
        for record in caplog.records
    )


def test_tool_call_signatures_forward_from_routed_providers() -> None:
    default = Provider("default")
    lead = Provider("lead")
    lead.tool_call_signatures = {"gemini_abc": "sig-1"}
    provider = ModelRoutingProvider(default, {"strong": lead})

    assert provider.tool_call_signatures == {"gemini_abc": "sig-1"}


def test_tool_call_signatures_isolated_across_routes() -> None:
    import httpx

    from merlin_providers import GeminiConfig, GeminiProvider

    def _gemini(endpoint: str, model: str, signature: str) -> GeminiProvider:
        async def handler(request: httpx.Request) -> httpx.Response:
            chunk = {
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
                                    "thoughtSignature": signature,
                                }
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ]
            }
            return httpx.Response(200, text="data: " + json.dumps(chunk))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return GeminiProvider(GeminiConfig(endpoint, "secret", model), client=client)

    async def scenario() -> None:
        default = _gemini("https://one.test/v1beta", "gemini-2.5-pro", "sig-route-default")
        lead = _gemini("https://two.test/v1beta", "gemini-2.5-flash", "sig-route-lead")
        provider = ModelRoutingProvider(default, {"gemini-2.5-flash": lead})
        request = ChatRequest(
            model="gemini-2.5-flash", messages=[ChatMessage(role="user", content="hi")]
        )
        strong_calls = [
            event async for event in provider.stream(request) if isinstance(event, ToolCall)
        ]
        routine_request = ChatRequest(
            model="gemini-2.5-pro", messages=[ChatMessage(role="user", content="hi")]
        )
        routine_calls = [
            event async for event in provider.stream(routine_request) if isinstance(event, ToolCall)
        ]
        assert len(strong_calls) == 1 and len(routine_calls) == 1
        # Identical calls on different routes must synthesize different ids so
        # the merged map keeps each route's own thoughtSignature.
        assert strong_calls[0].call_id != routine_calls[0].call_id
        assert provider.tool_call_signatures == {
            strong_calls[0].call_id: "sig-route-lead",
            routine_calls[0].call_id: "sig-route-default",
        }

    asyncio.run(scenario())
