"""Phase B: compact per-turn worker context for direct/baseline workers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace

import pytest
from merlin_agents import (
    AssignmentBudget,
    ContextDocument,
    ReviewedTargetAdapter,
    WorkerAssignment,
    WorkerContext,
    WorkerHandoff,
    WorkerProfile,
    WorkerRuntime,
)
from merlin_agents.context_projection import (
    AssignmentPrefix,
    EvidenceDelta,
    EvidenceReceipt,
    ProjectionPolicy,
    ProtocolEvent,
    ProtocolRole,
    ProtocolUnit,
    RequestedToolCall,
    StrategyDelta,
    UsagePressure,
    project_worker_context,
)
from merlin_agents.judgments import RefusalJudgment, RefusalVerdict
from merlin_agents.runtime import _BUILTIN_TARGET_EXCHANGE_TOOLS, _STALL_PIVOT_HINT
from merlin_contracts import (
    ChatMessage,
    ChatRequest,
    ModelCompleted,
    ModelEvent,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolSpec,
)


class Provider:
    def __init__(self, turns: list[list[ModelEvent]], *, context_window: int = 1_000_000) -> None:
        self.turns = turns
        self.requests: list[ChatRequest] = []
        self.capabilities = SimpleNamespace(vision=False, context_window_tokens=context_window)

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        for event in self.turns.pop(0):
            yield event


class BigTargetTools:
    """One reviewed target actor returning a large response per exchange."""

    def __init__(self, *, response: str | None = None, body_chars: int = 8_000) -> None:
        self.calls: list[str] = []
        self.response = response or ("target reply body " + "x" * body_chars)

    def describe(self):
        return (
            ToolSpec(name="target_request", description="Target", input_schema={"type": "object"}),
        )

    def effect(self, name: str) -> str | None:
        return "target_network"

    async def invoke(self, name: str, context: object, arguments: object) -> ToolResult:
        assert name == "target_request"
        self.calls.append(name)
        return ToolResult(
            content=json.dumps({"status": 200, "response": self.response}),
            metadata={
                "target_session_id": "api:session-1",
                "target_exchanges": [
                    {"id": len(self.calls), "interaction": True, "request": arguments}
                ],
            },
        )


def handoff() -> WorkerHandoff:
    profile = WorkerProfile("injection", 1, "summary", "Test injection carefully.", frozenset())
    assignment = WorkerAssignment(
        id="task-1",
        objective="Probe the reviewed target",
        profile_id="injection",
        target_profile_id="target-1",
        technique_ids=("t1",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider-1",
        authority_ids=("auth-1",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object","required":["finding"]}',
    )
    context = WorkerContext(
        assignment_id=assignment.id,
        profile=ContextDocument.create("profile:injection@1", "Test injection carefully."),
        guidance=(),
        skills=(),
        tool_ids=assignment.tool_ids,
        target=ReviewedTargetAdapter(
            "target-1",
            "http_api",
            "https://example.test/message",
            authentication="configured provider credential",
        ),
        authority_ids=assignment.authority_ids,
        digest="sha256:reviewed-context",
    )
    return WorkerHandoff(
        "session-1", "/workspace", "model-1", profile, assignment, worker_context=context
    )


def _request_chars(request: ChatRequest) -> int:
    return sum(
        len(message.content or "") + len(str(message.tool_calls or ""))
        for message in request.messages
    )


async def _run_worker(
    turns: int, *, window: int | None, tools: BigTargetTools
) -> tuple[Provider, object]:
    events: list[list[ModelEvent]] = [
        [
            ToolCall(
                call_id=f"target-{index}",
                name="target_request",
                arguments={"input": f"probe {index}"},
            ),
            ModelCompleted(finish_reason="tool_calls"),
        ]
        for index in range(turns)
    ]
    events.append(
        [
            TextDelta(text='{"finding":"no confirmed weakness"}'),
            ModelCompleted(finish_reason="stop"),
        ]
    )
    provider = Provider(events)
    runtime = WorkerRuntime(provider, tools, compact_exchange_window=window)
    return provider, await runtime.run(handoff())


@pytest.mark.asyncio
async def test_compact_turns_prompt_size_bounded_independent_of_turn_count() -> None:
    tools = BigTargetTools()
    provider, result = await _run_worker(8, window=2, tools=tools)
    assert result.status == "completed"
    sizes = [_request_chars(request) for request in provider.requests]
    # Eight large verbatim exchanges (~8KB each) would exceed 70KB unsent; the
    # windowed request stays near prefix + manifest + two exchanges.
    assert max(sizes) < 30_000, sizes
    steady = sizes[3:]
    assert max(steady) - min(steady) <= 100, sizes


@pytest.mark.asyncio
async def test_compact_turns_stable_prefix_byte_identical_across_turns() -> None:
    tools = BigTargetTools()
    provider, result = await _run_worker(6, window=2, tools=tools)
    assert result.status == "completed"
    systems = {request.messages[0].content for request in provider.requests}
    assignments = {request.messages[1].content for request in provider.requests}
    assert len(systems) == 1
    assert len(assignments) == 1
    for request in provider.requests:
        assert request.messages[0].role == "system"
        assert request.messages[1].role == "user"


@pytest.mark.asyncio
async def test_compact_turns_receipts_and_witnesses_survive_windowing() -> None:
    tools = BigTargetTools()
    provider, result = await _run_worker(5, window=2, tools=tools)
    assert result.status == "completed"
    checkpoint = result.checkpoint or {}
    evidence = checkpoint["target_evidence"]
    assert len(evidence) == 5
    for index, receipt in enumerate(evidence):
        assert receipt["reference"] == f"target-call:target-{index}"
        for key in ("receipt_sha256", "request_sha256", "content_sha256"):
            digest = receipt[key]
            assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    # Once the window trims verbatim history, the durable compaction provenance
    # binds the omitted units by digest instead of re-sending them.
    provenance = checkpoint["compaction"]
    assert provenance is not None
    assert provenance["source_digest"]
    assert provenance["source_count"] > 0


@pytest.mark.asyncio
async def test_compact_turns_disabled_window_keeps_legacy_pressure_only_behavior() -> None:
    tools = BigTargetTools()
    provider, result = await _run_worker(5, window=None, tools=tools)
    assert result.status == "completed"
    sizes = [_request_chars(request) for request in provider.requests]
    # No window and no pressure (huge context): the full transcript is re-sent.
    assert sizes[-1] > sizes[1] + 3 * 8_000
    assert (result.checkpoint or {})["compaction"] is None


@pytest.mark.asyncio
async def test_compact_turns_stall_pivot_hint_on_refusal_without_progress() -> None:
    tools = BigTargetTools(response="I'm sorry, I can't help with that request.")
    provider, result = await _run_worker(4, window=2, tools=tools)
    assert result.status == "completed"
    hinted = [
        request
        for request in provider.requests
        if any(
            message.role == "system" and message.content == _STALL_PIVOT_HINT
            for message in request.messages
        )
    ]
    assert len(hinted) >= 2
    # The directive is deduplicated: exactly one copy per request.
    for request in hinted:
        assert sum(message.content == _STALL_PIVOT_HINT for message in request.messages) == 1


@pytest.mark.asyncio
async def test_stall_pivot_hint_ignores_confirmed_progress() -> None:
    runtime = WorkerRuntime(Provider([]), BigTargetTools())
    message = ChatMessage(
        role="tool",
        name="target_request",
        tool_call_id="c1",
        content=json.dumps({"status": 200, "response": "I can't help with that."}),
    )
    receipt = {"call_id": "c1", "target_authoritative_outcome": "confirmed"}
    assert await runtime._stall_pivot_hint([message], (receipt,)) is None
    assert await runtime._stall_pivot_hint([message], ()) == _STALL_PIVOT_HINT
    assert await runtime._stall_pivot_hint([], ()) is None


class StubRefusalJudge:
    """Deterministic stub behind the RefusalJudge protocol."""

    def __init__(self, judgment: object, *, fail: bool = False) -> None:
        self.judgment = judgment
        self.fail = fail
        self.calls = 0

    async def judge_refusal(self, *, response_texts, request_text="", language=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("judge unavailable")
        return self.judgment


def _non_english_refusal_message() -> ChatMessage:
    refusal = "抱歉，出于安全原因，我无法协助完成该请求。"
    return ChatMessage(
        role="tool",
        name="target_request",
        tool_call_id="c1",
        content=json.dumps({"status": 200, "response": refusal}),
    )


def _judgment(verdict: RefusalVerdict) -> RefusalJudgment:
    return RefusalJudgment(verdict=verdict, confidence=0.9, graded_by="stub", latency_ms=1)


@pytest.mark.asyncio
async def test_stall_pivot_hint_judge_upgrades_non_english_refusal() -> None:
    judge = StubRefusalJudge(_judgment(RefusalVerdict.REFUSED))
    runtime = WorkerRuntime(Provider([]), BigTargetTools(), refusal_judge=judge)
    assert await runtime._stall_pivot_hint([_non_english_refusal_message()], ()) == (
        _STALL_PIVOT_HINT
    )
    # Second call over the same response digest is served from the cache.
    assert await runtime._stall_pivot_hint([_non_english_refusal_message()], ()) == (
        _STALL_PIVOT_HINT
    )
    assert judge.calls == 1


@pytest.mark.asyncio
async def test_stall_pivot_hint_judge_uncertain_engaged_none_or_error_no_hint() -> None:
    for judgment in (
        _judgment(RefusalVerdict.UNCERTAIN),
        _judgment(RefusalVerdict.ENGAGED),
        None,
    ):
        runtime = WorkerRuntime(
            Provider([]), BigTargetTools(), refusal_judge=StubRefusalJudge(judgment)
        )
        assert await runtime._stall_pivot_hint([_non_english_refusal_message()], ()) is None
    runtime = WorkerRuntime(
        Provider([]),
        BigTargetTools(),
        refusal_judge=StubRefusalJudge(None, fail=True),
    )
    assert await runtime._stall_pivot_hint([_non_english_refusal_message()], ()) is None


@pytest.mark.asyncio
async def test_stall_pivot_hint_regex_hit_never_consults_judge() -> None:
    judge = StubRefusalJudge(None, fail=True)
    runtime = WorkerRuntime(Provider([]), BigTargetTools(), refusal_judge=judge)
    message = ChatMessage(
        role="tool",
        name="target_request",
        tool_call_id="c1",
        content=json.dumps({"status": 200, "response": "I can't help with that."}),
    )
    assert await runtime._stall_pivot_hint([message], ()) == _STALL_PIVOT_HINT
    assert judge.calls == 0


@pytest.mark.asyncio
async def test_stall_pivot_hint_without_judge_keeps_regex_only_behavior() -> None:
    runtime = WorkerRuntime(Provider([]), BigTargetTools())
    assert await runtime._stall_pivot_hint([_non_english_refusal_message()], ()) is None


def _unit(index: int, *, target: bool) -> ProtocolUnit:
    import hashlib

    name = "target_request" if target else "browser_snapshot"
    content = f"body-{index}"
    receipt = EvidenceReceipt(
        receipt_id=f"receipt-{index}",
        tool_call_id=f"call-{index}",
        tool_name=name,
        content_digest="sha256:" + hashlib.sha256(content.encode()).hexdigest(),
        summary=f"{name} result recorded",
    )
    return ProtocolUnit(
        (
            ProtocolEvent(
                ProtocolRole.ASSISTANT,
                "",
                requested_tool_calls=(RequestedToolCall(f"call-{index}", name),),
            ),
            ProtocolEvent(
                ProtocolRole.TOOL,
                content,
                tool_call_id=f"call-{index}",
                tool_name=name,
                durable_receipt=receipt,
            ),
        )
    )


def test_projection_window_bounds_tail_to_recent_exchanges() -> None:
    units = tuple(_unit(index, target=index % 2 == 0) for index in range(12))
    projection = project_worker_context(
        assignment_prefix=AssignmentPrefix("system", "assignment"),
        strategy_delta=StrategyDelta("direct", "provider"),
        evidence_delta=EvidenceDelta(),
        protocol=units,
        usage=UsagePressure(context_window_tokens=1_000_000),
        policy=ProjectionPolicy(recent_exchange_window=3),
        exchange_tool_names=_BUILTIN_TARGET_EXCHANGE_TOOLS,
    )
    # Six exchanges total; the tail starts at the first of the last three.
    assert len(projection.recent_protocol_tail) == 6
    assert projection.compacted_history_digest is not None
    assert projection.compacted_receipts is not None
    assert projection.compacted_receipts.receipt_count == 6


def test_projection_window_does_not_trim_within_window() -> None:
    units = tuple(_unit(index, target=True) for index in range(3))
    projection = project_worker_context(
        assignment_prefix=AssignmentPrefix("system", "assignment"),
        strategy_delta=StrategyDelta("direct", "provider"),
        evidence_delta=EvidenceDelta(),
        protocol=units,
        usage=UsagePressure(context_window_tokens=1_000_000),
        policy=ProjectionPolicy(recent_exchange_window=6),
        exchange_tool_names=_BUILTIN_TARGET_EXCHANGE_TOOLS,
    )
    assert projection.recent_protocol_tail == units
    assert projection.compacted_history_digest is None


def test_projection_window_validates_positive() -> None:
    with pytest.raises(ValueError, match="recent exchange window"):
        replace(ProjectionPolicy(), recent_exchange_window=0)
    with pytest.raises(ValueError, match="compact exchange window"):
        WorkerRuntime(Provider([]), BigTargetTools(), compact_exchange_window=0)
