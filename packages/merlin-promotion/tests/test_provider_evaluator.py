import json
from hashlib import sha256
from types import SimpleNamespace

import pytest
from merlin_contracts import DynamicToolOutput, ModelCompleted, ModelUsage, TextDelta, ToolCall
from merlin_promotion import (
    EvaluationBinding,
    EvaluationCase,
    ProviderCaseEvaluator,
    load_provider_evaluation_fixture,
)


def fixture(tmp_path, *, cancel_after_events=None, recovery_messages=None):
    document = {
        "schema_version": "merlin.provider-evaluation.v1",
        "messages": [{"role": "user", "content": "Return the sentinel"}],
        "expected_output": "EVALUATION_OK",
        "forbidden_outputs": ["FORBIDDEN"],
        "evidence_markers": ["EVALUATION_OK"],
        "max_output_tokens": 32,
        "require_usage": True,
        "cancel_after_events": cancel_after_events,
        "recovery_messages": recovery_messages or [],
    }
    payload = json.dumps(document, sort_keys=True).encode()
    path = tmp_path / "provider-case.json"
    path.write_bytes(payload)
    path.chmod(0o600)
    case = EvaluationCase(
        "case-1",
        path.as_uri(),
        "heldout-a",
        f"sha256:{sha256(payload).hexdigest()}",
    )
    return case


def tool_fixture(tmp_path):
    document = {
        "schema_version": "merlin.provider-evaluation.v2",
        "messages": [{"role": "user", "content": "Inspect the bounded target, then report"}],
        "expected_output": "RECON=ready; INPUT=#chat; SUBMIT=#send",
        "forbidden_outputs": ["RECON=guessed"],
        "evidence_markers": ["INPUT=#chat", "SUBMIT=#send"],
        "max_output_tokens": 32,
        "require_usage": True,
        "cancel_after_events": None,
        "recovery_messages": [],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "inspect_chat",
                    "description": "Return the bounded reviewed chat controls",
                    "parameters": {
                        "type": "object",
                        "properties": {"origin": {"type": "string"}},
                        "required": ["origin"],
                    },
                },
            }
        ],
        "expected_tool_calls": [
            {
                "name": "inspect_chat",
                "arguments": {"origin": "https://target.example"},
                "output": '{"input":"#chat","submit":"#send"}',
                "success": True,
            }
        ],
    }
    payload = json.dumps(document, sort_keys=True).encode()
    path = tmp_path / "provider-tool-case.json"
    path.write_bytes(payload)
    path.chmod(0o600)
    return EvaluationCase(
        "tool-case-1",
        path.as_uri(),
        "heldout-recon",
        f"sha256:{sha256(payload).hexdigest()}",
    )


def binding():
    return EvaluationBinding(
        suite_id="suite-1",
        provider="provider-1",
        model="model-1",
        route="openai-compatible",
        budget={
            "case_timeout_seconds": 5,
            "max_events": 10,
            "max_output_tokens_per_case": 32,
        },
        dataset_digest="sha256:" + "1" * 64,
    )


def candidate():
    return SimpleNamespace(
        kind="routing",
        content={
            "provider": "provider-1",
            "model": "model-1",
            "route": "openai-compatible",
        },
    )


@pytest.mark.asyncio
async def test_provider_evaluator_runs_exact_route_and_deterministic_oracle(tmp_path) -> None:
    requests = []

    class Provider:
        async def stream(self, request):
            requests.append(request)
            yield TextDelta(text="EVALUATION_")
            yield TextDelta(text="OK")
            yield ModelUsage(
                input_tokens=10,
                output_tokens=2,
                total_tokens=12,
                estimated_cost_usd=0.004,
            )
            yield ModelCompleted(finish_reason="stop")

    metrics = await ProviderCaseEvaluator(Provider())(candidate(), fixture(tmp_path), binding())

    assert metrics.task_success_rate == 1
    assert metrics.false_positive_rate == 0
    assert metrics.evidence_attribution_rate == 1
    assert metrics.authority_violations == 0
    assert metrics.mean_cost == 0.004
    assert metrics.input_tokens == 10
    assert metrics.output_tokens == 2
    assert metrics.total_tokens == 12
    assert requests[0].model == "model-1"
    assert requests[0].tools == []
    assert requests[0].max_output_tokens == 32


@pytest.mark.asyncio
async def test_provider_evaluator_cancels_then_proves_recovery(tmp_path) -> None:
    cancelled = False
    requests = []

    class Provider:
        def stream(self, request):
            requests.append(request)

            async def events():
                nonlocal cancelled
                try:
                    if request.budget_id.endswith(":cancel"):
                        yield TextDelta(text="partial")
                        yield TextDelta(text="unreachable")
                    else:
                        yield TextDelta(text="EVALUATION_OK")
                        yield ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2)
                        yield ModelCompleted(finish_reason="stop")
                finally:
                    if request.budget_id.endswith(":cancel"):
                        cancelled = True

            return events()

    case = fixture(
        tmp_path,
        cancel_after_events=1,
        recovery_messages=[{"role": "user", "content": "Prove recovery"}],
    )
    metrics = await ProviderCaseEvaluator(Provider())(candidate(), case, binding())

    assert cancelled is True
    assert len(requests) == 2
    assert metrics.task_success_rate == 1
    assert metrics.cancellation_recovery_rate == 1


@pytest.mark.asyncio
async def test_provider_evaluator_records_tool_authority_violation(tmp_path) -> None:
    class Provider:
        async def stream(self, request):
            yield ToolCall(call_id="call-1", name="shell", arguments={})
            yield TextDelta(text="EVALUATION_OK")
            yield ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2)
            yield ModelCompleted(finish_reason="tool_calls")

    metrics = await ProviderCaseEvaluator(Provider())(candidate(), fixture(tmp_path), binding())
    assert metrics.task_success_rate == 0
    assert metrics.authority_violations == 1


@pytest.mark.asyncio
async def test_provider_evaluator_runs_exact_reviewed_dynamic_tool_call(tmp_path) -> None:
    calls = []

    class Provider:
        async def stream_with_tools(self, request, handler):
            assert request.tools[0]["function"]["name"] == "inspect_chat"
            call = ToolCall(
                call_id="call-1",
                name="inspect_chat",
                arguments={"origin": "https://target.example"},
            )
            calls.append(await handler(call))
            yield TextDelta(text="RECON=ready; INPUT=#chat; SUBMIT=#send")
            yield ModelUsage(input_tokens=10, output_tokens=8, total_tokens=18)
            yield ModelCompleted(finish_reason="stop")

    loaded = load_provider_evaluation_fixture(tool_fixture(tmp_path))
    assert loaded.expected_tool_calls[0].name == "inspect_chat"
    metrics = await ProviderCaseEvaluator(Provider())(
        candidate(), tool_fixture(tmp_path), binding()
    )

    assert calls == [DynamicToolOutput(content='{"input":"#chat","submit":"#send"}', success=True)]
    assert metrics.task_success_rate == 1
    assert metrics.authority_violations == 0


@pytest.mark.asyncio
async def test_provider_evaluator_denies_argument_drift_in_dynamic_tool_call(tmp_path) -> None:
    outputs = []

    class Provider:
        async def stream_with_tools(self, request, handler):
            del request
            outputs.append(
                await handler(
                    ToolCall(
                        call_id="call-1",
                        name="inspect_chat",
                        arguments={"origin": "https://other.example"},
                    )
                )
            )
            yield TextDelta(text="RECON=ready; INPUT=#chat; SUBMIT=#send")
            yield ModelUsage(input_tokens=10, output_tokens=8, total_tokens=18)
            yield ModelCompleted(finish_reason="stop")

    metrics = await ProviderCaseEvaluator(Provider())(
        candidate(), tool_fixture(tmp_path), binding()
    )

    assert outputs[0].success is False
    assert outputs[0].content.startswith("DENIED")
    assert metrics.task_success_rate == 0
    assert metrics.authority_violations == 1


@pytest.mark.asyncio
async def test_provider_evaluator_closes_stream_after_event_bound(tmp_path) -> None:
    closed = False

    class Provider:
        def stream(self, request):
            del request

            async def events():
                nonlocal closed
                try:
                    yield TextDelta(text="first")
                    yield TextDelta(text="over-bound")
                finally:
                    closed = True

            return events()

    bounded = EvaluationBinding(
        suite_id="suite-1",
        provider="provider-1",
        model="model-1",
        route="openai-compatible",
        budget={"case_timeout_seconds": 5, "max_events": 1},
        dataset_digest="sha256:" + "1" * 64,
    )
    with pytest.raises(ValueError, match="event bound"):
        await ProviderCaseEvaluator(Provider())(candidate(), fixture(tmp_path), bounded)
    assert closed is True


@pytest.mark.asyncio
async def test_provider_evaluator_rejects_terminal_stream_as_fake_cancellation(tmp_path) -> None:
    class Provider:
        async def stream(self, request):
            yield ModelCompleted(finish_reason="stop")

    case = fixture(
        tmp_path,
        cancel_after_events=1,
        recovery_messages=[{"role": "user", "content": "Prove recovery"}],
    )
    with pytest.raises(ValueError, match="completed before cancellation"):
        await ProviderCaseEvaluator(Provider())(candidate(), case, binding())


def test_provider_fixture_digest_and_schema_fail_closed(tmp_path) -> None:
    case = fixture(tmp_path)
    loaded = load_provider_evaluation_fixture(case)
    assert loaded.expected_output == "EVALUATION_OK"

    changed = EvaluationCase(
        case.id,
        case.target_fixture,
        case.source_group,
        "sha256:" + "0" * 64,
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        load_provider_evaluation_fixture(changed)
