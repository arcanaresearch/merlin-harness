"""Focused coverage for capability-aware structured model responses."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from merlin_contracts import (
    ChatMessage,
    GeneratorExecutionContract,
    ModelCompleted,
    ModelJsonError,
    ModelUsage,
    ModelWarning,
    ReasoningDelta,
    TextDelta,
)
from merlin_tui.bootstrap import (
    _request_structured_json,
    _structured_model_for_role,
    _structured_output_token_limit,
)


class _Capabilities:
    def __init__(self, maximum: int, *, assumed: bool = False) -> None:
        self.maximum = maximum
        self.assumed = assumed
        self.calls: list[tuple[str, str, str]] = []

    def resolve_model(self, model: str, *, transport: str, endpoint: str):
        self.calls.append((model, transport, endpoint))
        return SimpleNamespace(
            max_output_tokens=SimpleNamespace(
                value=self.maximum,
                provenance=SimpleNamespace(assumed=self.assumed),
            )
        )


class _Provider:
    def __init__(self, responses: list[list[object]]) -> None:
        self.responses = responses
        self.requests = []

    def route_identity_for_model(self, model: str):
        return SimpleNamespace(
            model=model,
            transport="openai_compatible",
            endpoint="https://models.example/v1",
        )

    async def stream(self, request):
        self.requests.append(request)
        for event in self.responses[len(self.requests) - 1]:
            yield event


def test_structured_model_prefers_exact_role_then_routine_then_fallback() -> None:
    config = SimpleNamespace(
        provider_role_models={
            "adversarial-researcher": "research-model",
            "routine": "routine-model",
        }
    )
    assert (
        _structured_model_for_role(config, "adversarial-researcher", "fallback") == "research-model"
    )
    assert _structured_model_for_role(config, "novelty-critic", "fallback") == "routine-model"
    assert _structured_model_for_role(SimpleNamespace(), "novelty-critic", "fallback") == "fallback"


def test_structured_output_limit_uses_exact_non_assumed_route_cap() -> None:
    provider = _Provider([])
    registry = _Capabilities(8_192)

    assert _structured_output_token_limit(registry, provider, "deepseek-v4-flash") == 8_192
    assert registry.calls == [
        (
            "deepseek-v4-flash",
            "openai_compatible",
            "https://models.example/v1",
        )
    ]
    assert (
        _structured_output_token_limit(_Capabilities(4_096, assumed=True), provider, "unknown")
        == 16_384
    )


@pytest.mark.asyncio
async def test_structured_json_repairs_reasoning_only_truncation_and_records_usage() -> None:
    provider = _Provider(
        [
            [
                ReasoningDelta(text="private reasoning consumed the first allowance"),
                ModelUsage(input_tokens=10, output_tokens=8, total_tokens=18),
                ModelWarning(code="truncated", message="provider stopped at its output limit"),
                ModelCompleted(finish_reason="length", disposition="truncated"),
            ],
            [
                TextDelta(text='[{"semantic_id":"tenant-boundary"}]'),
                ModelUsage(input_tokens=12, output_tokens=7, total_tokens=19),
                ModelCompleted(finish_reason="stop", disposition="completed"),
            ],
        ]
    )
    usage: list[ModelUsage] = []

    result = await _request_structured_json(
        provider=provider,
        capability_registry=_Capabilities(8_192),
        selected_model="deepseek-v4-flash",
        messages=(
            ChatMessage(role="system", content="Return JSON."),
            ChatMessage(role="user", content="Propose one arm."),
        ),
        expected_container="array",
        max_attempts=3,
        usage_sink=usage.append,
    )

    assert result == [{"semantic_id": "tenant-boundary"}]
    assert len(provider.requests) == 2
    assert all(request.model == "deepseek-v4-flash" for request in provider.requests)
    assert all(request.max_output_tokens == 8_192 for request in provider.requests)
    assert all(request.reasoning == "disabled" for request in provider.requests)
    assert "prior response was incomplete" in provider.requests[1].messages[-1].content
    assert [event.total_tokens for event in usage] == [18, 19]


@pytest.mark.asyncio
async def test_structured_json_retries_wrong_shape_then_reports_typed_exhaustion() -> None:
    provider = _Provider(
        [
            [
                TextDelta(text='{"wrong":"shape"}'),
                ModelCompleted(finish_reason="stop", disposition="completed"),
            ],
            [
                TextDelta(text="still not json"),
                ModelCompleted(finish_reason="stop", disposition="completed"),
            ],
        ]
    )

    with pytest.raises(ModelJsonError, match=r"failed after 2 attempt\(s\)"):
        await _request_structured_json(
            provider=provider,
            capability_registry=_Capabilities(65_536),
            selected_model="large-model",
            messages=(ChatMessage(role="user", content="Return an array."),),
            expected_container="array",
            max_attempts=2,
        )

    assert len(provider.requests) == 2
    assert all(request.max_output_tokens == 16_384 for request in provider.requests)


@pytest.mark.asyncio
async def test_structured_json_contract_cancels_and_closes_provider_at_deadline() -> None:
    class DeadlineProvider(_Provider):
        def __init__(self) -> None:
            super().__init__([])
            self.closed = asyncio.Event()
            self.continued = False

        async def stream(self, request):
            self.requests.append(request)
            try:
                yield TextDelta(text="[")
                await asyncio.sleep(1)
                self.continued = True
                yield TextDelta(text="]")
            finally:
                self.closed.set()

    provider = DeadlineProvider()
    contract = GeneratorExecutionContract(
        generator_id="bounded-regeneration",
        max_outputs=1,
        max_output_bytes=1024,
        max_seconds=0.01,
    )
    with pytest.raises(TimeoutError):
        await _request_structured_json(
            provider=provider,
            capability_registry=_Capabilities(8_192),
            selected_model="deepseek-v4-flash",
            messages=(ChatMessage(role="user", content="Return an array."),),
            expected_container="array",
            max_attempts=1,
            execution_contract=contract,
        )

    assert provider.closed.is_set()
    assert provider.continued is False


@pytest.mark.asyncio
async def test_structured_json_contract_stops_consuming_after_byte_cap() -> None:
    class ByteProvider(_Provider):
        def __init__(self) -> None:
            super().__init__([])
            self.emitted = 0
            self.closed = False

        async def stream(self, request):
            self.requests.append(request)
            try:
                for chunk in ("12345", "6", "unread"):
                    self.emitted += 1
                    yield TextDelta(text=chunk)
            finally:
                self.closed = True

    provider = ByteProvider()
    contract = GeneratorExecutionContract(
        generator_id="bounded-regeneration",
        max_outputs=1,
        max_output_bytes=5,
        max_seconds=1.0,
    )
    with pytest.raises(ValueError, match="max_output_bytes"):
        await _request_structured_json(
            provider=provider,
            capability_registry=_Capabilities(8_192),
            selected_model="deepseek-v4-flash",
            messages=(ChatMessage(role="user", content="Return an array."),),
            expected_container="array",
            max_attempts=1,
            execution_contract=contract,
        )

    assert provider.emitted == 2
    assert provider.closed is True
