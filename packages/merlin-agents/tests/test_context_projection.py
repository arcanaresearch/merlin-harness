from __future__ import annotations

import hashlib

from merlin_agents.context_projection import (
    AssignmentPrefix,
    EvidenceDelta,
    EvidenceObservation,
    EvidenceReceipt,
    ProgressSignal,
    ProjectionPolicy,
    ProtocolEvent,
    ProtocolRole,
    ProtocolUnit,
    RequestedToolCall,
    StallSignal,
    StrategyDelta,
    StrategyFact,
    UsagePressure,
    project_worker_context,
)


def _prefix() -> AssignmentPrefix:
    return AssignmentPrefix(
        system_instruction="stable system policy",
        assignment="test candidate exactly once",
        identity=(("candidate", "candidate/1"),),
    )


def _unit(index: int, *, size: int = 20, receipted: bool = True) -> ProtocolUnit:
    call_id = f"call-{index}"
    content = str(index) * size
    receipt = (
        EvidenceReceipt(
            receipt_id=f"receipt-{index}",
            tool_call_id=call_id,
            tool_name="target_chat",
            content_digest="sha256:" + hashlib.sha256(content.encode()).hexdigest(),
            summary=f"target exchange {index} recorded",
        )
        if receipted
        else None
    )
    return ProtocolUnit(
        (
            ProtocolEvent(
                ProtocolRole.ASSISTANT,
                f"attempt {index}",
                requested_tool_calls=(RequestedToolCall(call_id, "target_chat"),),
            ),
            ProtocolEvent(
                ProtocolRole.TOOL,
                content,
                tool_call_id=call_id,
                tool_name="target_chat",
                durable_receipt=receipt,
            ),
        )
    )


def _project(**overrides):
    arguments = {
        "assignment_prefix": _prefix(),
        "strategy_delta": StrategyDelta("pair", "attack", (StrategyFact("turn", "2"),)),
        "evidence_delta": EvidenceDelta(observations=(EvidenceObservation("signal", "near_miss"),)),
        "protocol": tuple(_unit(index) for index in range(6)),
        "usage": UsagePressure(context_window_tokens=2_000),
    }
    arguments.update(overrides)
    return project_worker_context(**arguments)


def test_assignment_prefix_is_stable_across_strategy_and_evidence_deltas() -> None:
    first = _project()
    second = _project(
        strategy_delta=StrategyDelta("crescendo", "expand", (StrategyFact("turn", "9"),)),
        evidence_delta=EvidenceDelta(observations=(EvidenceObservation("signal", "refusal"),)),
    )

    assert first.assignment_prefix == second.assignment_prefix == _prefix()
    assert first.assignment_cache_key == second.assignment_cache_key


def test_live_and_checkpoint_resume_use_the_same_pure_projection() -> None:
    durable_inputs = {
        "assignment_prefix": _prefix(),
        "strategy_delta": StrategyDelta("goat", "observe"),
        "evidence_delta": EvidenceDelta(),
        "protocol": tuple(_unit(index) for index in range(3)),
        "usage": UsagePressure(
            context_window_tokens=300,
            cumulative_input_tokens=2_000,
            cumulative_cache_read_tokens=600,
        ),
    }

    live = project_worker_context(**durable_inputs)
    restored = project_worker_context(**dict(durable_inputs))

    assert live == restored


def test_provider_tokenizer_controls_pressure_and_estimate() -> None:
    calls: list[str] = []

    def tokenizer(payload: str) -> int:
        calls.append(payload)
        return len(payload) // 10

    projected = _project(
        tokenizer=tokenizer,
        usage=UsagePressure(context_window_tokens=10_000),
    )

    assert calls
    assert projected.used_provider_tokenizer
    assert projected.estimated_input_tokens == len(calls[-1]) // 10
    assert not projected.pressure_reasons


def test_unknown_tokenizer_uses_conservative_utf8_upper_bound() -> None:
    projected = _project(protocol=(ProtocolUnit((ProtocolEvent(ProtocolRole.USER, "文言文"),)),))

    assert not projected.used_provider_tokenizer
    assert projected.estimated_input_tokens > len("文言文")


def test_pressure_uses_context_churn_and_cost_without_count_caps() -> None:
    many_small_units = tuple(
        ProtocolUnit((ProtocolEvent(ProtocolRole.USER, f"message {index}"),)) for index in range(73)
    )
    projected = _project(
        protocol=many_small_units,
        usage=UsagePressure(
            context_window_tokens=100_000,
            cumulative_input_tokens=500_000,
            cumulative_cache_read_tokens=10_000,
            cumulative_cost_usd=8.5,
            cost_budget_usd=10,
        ),
    )

    assert "cumulative_input_cache_pressure" in projected.pressure_reasons
    assert "cumulative_cost_pressure" in projected.pressure_reasons
    assert len(projected.recent_protocol_tail) < len(many_small_units)
    assert len(projected.recent_protocol_tail) not in {48, 60}


def test_compaction_keeps_complete_recent_units_and_attributable_receipts() -> None:
    protocol = tuple(_unit(index, size=400) for index in range(8))
    projected = _project(
        protocol=protocol,
        usage=UsagePressure(context_window_tokens=2_500),
        policy=ProjectionPolicy(context_trigger_ratio=0.5, context_target_ratio=0.3),
    )

    assert projected.recent_protocol_tail
    assert projected.recent_protocol_tail[-1] == protocol[-1]
    assert all(len(unit.events) == 2 for unit in projected.recent_protocol_tail)
    assert projected.compacted_history_digest is not None
    assert projected.compacted_receipts is not None
    assert projected.fits_context
    assert projected.estimated_input_tokens <= 2_500
    assert {
        receipt.tool_call_id for receipt in projected.compacted_receipts.recent_semantic_receipts
    }.isdisjoint(
        {
            event.tool_call_id
            for unit in projected.recent_protocol_tail
            for event in unit.events
            if event.tool_call_id
        }
    )


def test_compaction_never_discards_tool_evidence_without_a_receipt() -> None:
    protocol = (
        _unit(0, size=2_000, receipted=False),
        *(_unit(index, size=400) for index in range(1, 5)),
    )
    projected = _project(
        protocol=protocol,
        usage=UsagePressure(context_window_tokens=1_000),
        policy=ProjectionPolicy(context_trigger_ratio=0.5, context_target_ratio=0.3),
    )

    assert projected.recent_protocol_tail[0] == protocol[0]
    assert projected.compacted_receipts is None


def test_progress_and_stall_are_separate_typed_signals() -> None:
    projected = _project(
        progress=ProgressSignal(("new_target_receipt",)),
        stall=StallSignal(("same_semantic_state",)),
    )

    assert projected.progress.markers == ("new_target_receipt",)
    assert projected.stall.reasons == ("same_semantic_state",)


def test_protocol_unit_rejects_incomplete_tool_pairing() -> None:
    try:
        ProtocolUnit(
            (
                ProtocolEvent(
                    ProtocolRole.ASSISTANT,
                    "call target",
                    requested_tool_calls=(RequestedToolCall("missing", "target_chat"),),
                ),
            )
        )
    except ValueError as error:
        assert "every requested tool result" in str(error)
    else:
        raise AssertionError("incomplete protocol unit was accepted")


def test_protocol_unit_rejects_out_of_order_and_duplicate_tool_results() -> None:
    receipt = _unit(1).events[-1]
    for events in (
        (
            receipt,
            ProtocolEvent(
                ProtocolRole.ASSISTANT,
                "late request",
                requested_tool_calls=(RequestedToolCall("call-1", "target_chat"),),
            ),
        ),
        (
            ProtocolEvent(
                ProtocolRole.ASSISTANT,
                "duplicate request",
                requested_tool_calls=(
                    RequestedToolCall("call-1", "target_chat"),
                    RequestedToolCall("call-1", "target_chat"),
                ),
            ),
            receipt,
        ),
        (
            ProtocolEvent(
                ProtocolRole.ASSISTANT,
                "one request",
                requested_tool_calls=(RequestedToolCall("call-1", "target_chat"),),
            ),
            receipt,
            receipt,
        ),
    ):
        try:
            ProtocolUnit(events)
        except ValueError:
            pass
        else:
            raise AssertionError("malformed protocol ordering was accepted")


def test_receipt_must_match_tool_name_and_content_digest() -> None:
    good = _unit(1).events[-1]
    bad_receipt = EvidenceReceipt(
        receipt_id="receipt-bad",
        tool_call_id="call-1",
        tool_name="different_tool",
        content_digest=good.durable_receipt.content_digest,  # type: ignore[union-attr]
        summary="wrong attribution",
    )
    try:
        ProtocolEvent(
            ProtocolRole.TOOL,
            good.content,
            tool_call_id="call-1",
            tool_name="target_chat",
            durable_receipt=bad_receipt,
        )
    except ValueError as error:
        assert "same tool result" in str(error)
    else:
        raise AssertionError("misattributed receipt was accepted")


def test_long_receipt_history_stays_bounded_and_keeps_semantic_evidence() -> None:
    protocol = tuple(_unit(index, size=2_000) for index in range(100))
    projected = _project(
        protocol=protocol,
        usage=UsagePressure(context_window_tokens=6_000),
        policy=ProjectionPolicy(context_trigger_ratio=0.5, context_target_ratio=0.4),
    )

    assert projected.fits_context
    assert projected.estimated_input_tokens <= 6_000
    assert projected.compacted_receipts is not None
    assert projected.compacted_receipts.receipt_count == 99
    assert projected.compacted_receipts.recent_semantic_receipts
    assert projected.compacted_receipts.recent_semantic_receipts[-1].summary


def test_tool_result_name_must_match_the_assistant_request() -> None:
    result = _unit(1).events[-1]
    try:
        ProtocolUnit(
            (
                ProtocolEvent(
                    ProtocolRole.ASSISTANT,
                    "request another tool",
                    requested_tool_calls=(RequestedToolCall("call-1", "browser_snapshot"),),
                ),
                result,
            )
        )
    except ValueError as error:
        assert "name must match" in str(error)
    else:
        raise AssertionError("mismatched requested and returned tool names were accepted")


def test_non_tool_event_cannot_carry_tool_result_identity() -> None:
    try:
        ProtocolEvent(ProtocolRole.USER, "not a result", tool_call_id="call-1")
    except ValueError as error:
        assert "only tool events" in str(error)
    else:
        raise AssertionError("non-tool event carried a tool result identity")
