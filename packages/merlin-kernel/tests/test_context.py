from hashlib import sha256

import pytest
from merlin_contracts import ChatMessage, ContextProjection, MessageLineage
from merlin_kernel import (
    MERLIN_IDENTITY,
    compact_projection,
    digest_messages,
    prune_superseded_tool_results,
)
from merlin_kernel.kernel import _compact_messages


def test_compaction_is_hash_bound_and_keeps_transcript_projection_lineage() -> None:
    messages = tuple(
        ChatMessage(role="user", content=f"message-{index}-" + "x" * 600) for index in range(6)
    )
    projection = ContextProjection(
        session_id="session-1",
        identity=MERLIN_IDENTITY,
        messages=messages,
        lineage=tuple(
            MessageLineage(
                sequence=index + 1,
                message_id=f"m{index}",
                source_message_id=f"m{index}",
            )
            for index in range(6)
        ),
        transcript_digest=digest_messages(messages),
        head_sequence=6,
    )

    compacted = compact_projection(
        projection,
        keep_messages=2,
        max_summary_characters=500,
        context_window_tokens=1_024,
    )

    assert projection.messages == messages
    assert compacted.messages[-2:] == messages[-2:]
    assert compacted.compaction is not None
    assert compacted.compaction.source_start == 1
    assert compacted.compaction.source_end == 4
    assert compacted.compaction.source_digest == digest_messages(messages[:4])
    assert (
        compacted.compaction.summary_digest
        == sha256(compacted.compaction.summary.encode()).hexdigest()
    )
    assert (
        compact_projection(
            projection,
            keep_messages=2,
            max_summary_characters=500,
            context_window_tokens=1_024,
        ).compaction
        == compacted.compaction
    )


def test_initial_merlin_identity_is_explicit_and_stable() -> None:
    assert MERLIN_IDENTITY.role == "system"
    assert MERLIN_IDENTITY.content.startswith("You are Merlin")
    assert "unlimited by default" in MERLIN_IDENTITY.content
    assert "Omit assessment_prepare.budget" in MERLIN_IDENTITY.content
    assert "Cleanup ownership is lifecycle metadata" in MERLIN_IDENTITY.content
    assert "then call assessment_prepare exactly once" in MERLIN_IDENTITY.content
    assert "do not restart reconnaissance" in MERLIN_IDENTITY.content
    assert "same reconnaissance context provably created" in MERLIN_IDENTITY.content
    assert "/robots.txt" in MERLIN_IDENTITY.content
    assert "Sitemap declaration" in MERLIN_IDENTITY.content
    assert "same-origin pages, links, forms, frames" in MERLIN_IDENTITY.content
    assert "observed, absent, or blocked" in MERLIN_IDENTITY.content
    assert "Never guess a cleanup endpoint" in MERLIN_IDENTITY.content
    assert "continue preparation and execution" in MERLIN_IDENTITY.content
    assert "Cleanup uncertainty must not suppress attack telemetry" in MERLIN_IDENTITY.content
    assert "do not prepare or execute" not in MERLIN_IDENTITY.content


def test_compaction_uses_admitted_context_window_soft_threshold() -> None:
    messages = tuple(
        ChatMessage(role="user", content=f"message-{index}-" + "x" * 200) for index in range(20)
    )
    projection = ContextProjection(
        session_id="session-window",
        identity=MERLIN_IDENTITY,
        messages=messages,
        lineage=tuple(
            MessageLineage(
                sequence=index + 1,
                message_id=f"m{index}",
                source_message_id=f"m{index}",
            )
            for index in range(20)
        ),
        transcript_digest=digest_messages(messages),
        head_sequence=20,
    )

    compacted = compact_projection(
        projection,
        max_characters=1_000_000,
        keep_messages=2,
        context_window_tokens=1_024,
        soft_utilization=0.8,
    )

    assert compacted.compaction is not None
    assert compacted.compaction.algorithm == "structured-tool-lifecycle-context-window-v3"
    assert compacted.messages[-2:] == messages[-2:]


def test_compaction_summary_preserves_tool_call_result_identity() -> None:
    messages = (
        ChatMessage(role="user", content="inspect"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"a"}'},
                }
            ],
        ),
        ChatMessage(role="tool", content="result", name="read", tool_call_id="call-1"),
        ChatMessage(role="assistant", content="observed" + "x" * 3_000),
        ChatMessage(role="user", content="next"),
        ChatMessage(role="assistant", content="ready"),
    )
    projection = ContextProjection(
        session_id="session-tools",
        identity=MERLIN_IDENTITY,
        messages=messages,
        lineage=tuple(
            MessageLineage(
                sequence=index + 1,
                message_id=f"m{index}",
                source_message_id=f"m{index}",
            )
            for index in range(len(messages))
        ),
        transcript_digest=digest_messages(messages),
        head_sequence=len(messages),
    )

    compacted = compact_projection(
        projection,
        keep_messages=2,
        context_window_tokens=1_024,
        soft_utilization=0.5,
    )

    assert compacted.compaction is not None
    summary = compacted.compaction.summary
    assert '"id":"call-1"' in summary
    assert '"name":"read"' in summary
    assert '"call_id":"call-1"' in summary
    assert "arguments_sha256" in summary
    assert "content_sha256" in summary


def test_superseded_tool_results_become_stable_hash_bound_receipts() -> None:
    large_result = "start " + "x" * 4_000 + " end"
    messages = (
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call-old",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"a"}'},
                }
            ],
        ),
        ChatMessage(role="tool", content=large_result, name="read", tool_call_id="call-old"),
        ChatMessage(role="assistant", content="I incorporated the result."),
        ChatMessage(role="user", content="next"),
    )

    first = prune_superseded_tool_results(messages, before=2)
    second = prune_superseded_tool_results(messages, before=2)

    assert tuple(messages)[1].content == large_result
    assert first == second
    assert first[0] == messages[0]
    assert first[2:] == list(messages[2:])
    receipt = first[1].content
    assert '"type":"tool_result_receipt"' in receipt
    assert '"call_id":"call-old"' in receipt
    assert sha256(large_result.encode()).hexdigest() in receipt
    assert '"content_bytes":4010' in receipt


def test_context_pressure_prunes_old_tool_output_before_range_compaction() -> None:
    large_result = "observed " + "x" * 5_000 + " terminal"
    messages = (
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call-old",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"a"}'},
                }
            ],
        ),
        ChatMessage(role="tool", content=large_result, name="read", tool_call_id="call-old"),
        ChatMessage(role="assistant", content="Result incorporated."),
        ChatMessage(role="user", content="continue"),
    )
    projection = _projection("session-prune-first", messages)

    projected = compact_projection(
        projection,
        keep_messages=2,
        context_window_tokens=2_048,
        soft_utilization=0.5,
    )

    assert projected.compaction is None
    assert projection.messages[1].content == large_result
    assert '"type":"tool_result_receipt"' in projected.messages[1].content
    assert projected.messages[2:] == messages[2:]


def test_final_request_projection_prunes_tool_output_before_summary() -> None:
    messages = [
        MERLIN_IDENTITY,
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call-old",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"a"}'},
                }
            ],
        ),
        ChatMessage(
            role="tool",
            content="start " + "x" * 5_000 + " end",
            name="read",
            tool_call_id="call-old",
        ),
        *[ChatMessage(role="assistant", content=f"state-{index}") for index in range(48)],
    ]

    projected = _compact_messages(messages, context_window_tokens=3_072)

    assert not any("Earlier durable conversation context compacted" in m.content for m in projected)
    assert '"type":"tool_result_receipt"' in projected[2].content
    assert projected[-1] == messages[-1]


def test_large_character_payload_does_not_override_large_model_window() -> None:
    messages = tuple(ChatMessage(role="user", content="x" * 10_000) for _ in range(12))
    projection = ContextProjection(
        session_id="session-large-window",
        identity=MERLIN_IDENTITY,
        messages=messages,
        lineage=tuple(
            MessageLineage(
                sequence=index + 1,
                message_id=f"m{index}",
                source_message_id=f"m{index}",
            )
            for index in range(len(messages))
        ),
        transcript_digest=digest_messages(messages),
        head_sequence=len(messages),
    )

    projected = compact_projection(projection, context_window_tokens=1_000_000)

    assert projected.compaction is None
    assert projected.messages == messages


def test_compaction_preserves_reasoning_verbatim_and_by_digest() -> None:
    dropped_reasoning = "step one: inspect the target  " * 20
    messages = (
        ChatMessage(role="user", content="inspect " + "x" * 2_000),
        ChatMessage(
            role="assistant",
            content="planning",
            reasoning_content=dropped_reasoning,
        ),
        ChatMessage(
            role="assistant",
            content="",
            reasoning_content="call the read tool next",
            tool_calls=[
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"a"}'},
                }
            ],
        ),
        ChatMessage(role="tool", content="result", name="read", tool_call_id="call-1"),
        ChatMessage(role="assistant", content="final", reasoning_content="kept reasoning"),
        ChatMessage(role="user", content="next"),
    )
    projection = ContextProjection(
        session_id="session-reasoning",
        identity=MERLIN_IDENTITY,
        messages=messages,
        lineage=tuple(
            MessageLineage(
                sequence=index + 1,
                message_id=f"m{index}",
                source_message_id=f"m{index}",
            )
            for index in range(len(messages))
        ),
        transcript_digest=digest_messages(messages),
        head_sequence=len(messages),
    )

    compacted = compact_projection(
        projection,
        keep_messages=2,
        context_window_tokens=1_024,
        soft_utilization=0.5,
    )

    assert compacted.compaction is not None
    summary = compacted.compaction.summary
    # Dropped reasoning is provenanced by digest, length, and bounded snippet —
    # never silently dropped, in the same style as arguments_sha256 lineage.
    assert sha256(dropped_reasoning.encode()).hexdigest() in summary
    assert sha256(b"call the read tool next").hexdigest() in summary
    assert '"reasoning_length":' in summary
    assert "reasoning_sha256" in summary
    # Retained turns keep reasoning_content verbatim.
    retained = compacted.messages[-2]
    assert retained.reasoning_content == "kept reasoning"


def test_unknown_context_window_warns_instead_of_falling_back_silently() -> None:
    messages = tuple(ChatMessage(role="user", content=f"message-{index}") for index in range(3))
    projection = ContextProjection(
        session_id="session-unknown-window",
        identity=MERLIN_IDENTITY,
        messages=messages,
        lineage=tuple(
            MessageLineage(
                sequence=index + 1,
                message_id=f"m{index}",
                source_message_id=f"m{index}",
            )
            for index in range(3)
        ),
        transcript_digest=digest_messages(messages),
        head_sequence=3,
    )

    with pytest.warns(UserWarning, match="tier-7 conservative assumption"):
        projected = compact_projection(projection)

    assert projected.messages == messages


def test_compacted_projection_replays_through_each_http_transport() -> None:
    from merlin_contracts import ChatRequest
    from merlin_providers import (
        AnthropicMessagesConfig,
        AnthropicMessagesProvider,
        GeminiConfig,
        GeminiProvider,
        OpenAICompatibleProvider,
        OpenAIResponsesConfig,
        OpenAIResponsesProvider,
        ProviderConfig,
    )

    messages = (
        ChatMessage(role="user", content="u0 " + "x" * 2_000),
        ChatMessage(
            role="assistant",
            content="planning " + "y" * 2_000,
            reasoning_content="dropped reasoning " * 40,
        ),
        ChatMessage(role="user", content="u2 " + "x" * 2_000),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_old",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"old"}'},
                }
            ],
        ),
        ChatMessage(role="tool", content="old result", name="read", tool_call_id="call_old"),
        ChatMessage(
            role="assistant",
            content="reading next",
            reasoning_content="kept reasoning",
            tool_calls=[
                {
                    "id": "call_new",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"new"}'},
                }
            ],
        ),
        ChatMessage(role="tool", content="new result", name="read", tool_call_id="call_new"),
        ChatMessage(role="user", content="next"),
    )
    projection = ContextProjection(
        session_id="session-replay",
        identity=MERLIN_IDENTITY,
        messages=messages,
        lineage=tuple(
            MessageLineage(
                sequence=index + 1,
                message_id=f"m{index}",
                source_message_id=f"m{index}",
            )
            for index in range(len(messages))
        ),
        transcript_digest=digest_messages(messages),
        head_sequence=len(messages),
    )

    compacted = compact_projection(
        projection,
        keep_messages=4,
        context_window_tokens=1_024,
        soft_utilization=0.5,
    )

    assert compacted.compaction is not None
    replay_messages = list(compacted.messages)
    # The summary leads; the retained tail is the assistant/tool pair plus the
    # trailing user turn — the compaction boundary never leaves an orphaned
    # tool result whose call was compacted away.
    assert replay_messages[0].role == "system"
    assert "call_old" in replay_messages[0].content
    assert [message.role for message in replay_messages[1:]] == ["assistant", "tool", "user"]
    assert replay_messages[1].reasoning_content == "kept reasoning"

    openai = OpenAICompatibleProvider(ProviderConfig("https://models.example/v1", "", "model"))
    openai_payload = openai._payload(ChatRequest(model="model", messages=replay_messages))
    assert openai_payload["messages"][0]["role"] == "system"
    assert openai_payload["messages"][1]["tool_calls"][0]["id"] == "call_new"
    assert openai_payload["messages"][2]["tool_call_id"] == "call_new"

    responses = OpenAIResponsesProvider(
        OpenAIResponsesConfig("https://api.openai.test/v1", "", "gpt-5.4")
    )
    responses_items = responses._payload(ChatRequest(model="gpt-5.4", messages=replay_messages))[
        "input"
    ]
    assert responses_items[0]["role"] == "system"
    assert {
        "type": "function_call",
        "call_id": "call_new",
        "name": "read",
        "arguments": '{"path":"new"}',
    } in responses_items
    assert {
        "type": "function_call_output",
        "call_id": "call_new",
        "output": "new result",
    } in responses_items

    anthropic = AnthropicMessagesProvider(
        AnthropicMessagesConfig("https://api.anthropic.test/v1", "", "claude-opus-4-7")
    )
    anthropic_payload = anthropic._payload(
        ChatRequest(model="claude-opus-4-7", messages=replay_messages)
    )
    assert "call_old" in anthropic_payload["system"][0]["text"]
    anthropic_blocks = [
        block for message in anthropic_payload["messages"] for block in message["content"]
    ]
    assert {
        "type": "tool_use",
        "id": "call_new",
        "name": "read",
        "input": {"path": "new"},
    } in anthropic_blocks
    assert {
        "type": "tool_result",
        "tool_use_id": "call_new",
        "content": "new result",
    } in anthropic_blocks

    gemini = GeminiProvider(
        GeminiConfig("https://generativelanguage.test/v1beta", "", "gemini-2.5-pro")
    )
    gemini_payload = gemini._payload(ChatRequest(model="gemini-2.5-pro", messages=replay_messages))
    assert "call_old" in gemini_payload["systemInstruction"]["parts"][0]["text"]
    gemini_parts = [part for item in gemini_payload["contents"] for part in item["parts"]]
    assert {"functionCall": {"name": "read", "args": {"path": "new"}}} in gemini_parts
    assert {
        "functionResponse": {"name": "read", "response": {"result": "new result"}}
    } in gemini_parts


def _projection(session_id: str, messages: tuple[ChatMessage, ...]) -> ContextProjection:
    return ContextProjection(
        session_id=session_id,
        identity=MERLIN_IDENTITY,
        messages=messages,
        lineage=tuple(
            MessageLineage(
                sequence=index + 1,
                message_id=f"m{index}",
                source_message_id=f"m{index}",
            )
            for index in range(len(messages))
        ),
        transcript_digest=digest_messages(messages),
        head_sequence=len(messages),
    )


def test_compaction_boundary_drops_dangling_assistant_tool_calls() -> None:
    # Simulates a prior compaction round: the kept tail would open with an
    # assistant whose tool_calls have no matching result in the tail.
    messages = (
        ChatMessage(role="user", content="u0 " + "x" * 2_000),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_dangling",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"old"}'},
                }
            ],
        ),
        ChatMessage(role="user", content="u2"),
        ChatMessage(role="assistant", content="done"),
    )
    projection = _projection("session-dangling", messages)

    compacted = compact_projection(
        projection,
        keep_messages=3,
        context_window_tokens=1_024,
        soft_utilization=0.5,
    )

    assert compacted.compaction is not None
    # The boundary moved forward past the dangling assistant; the kept tail
    # starts at the user turn and stays hash-bound to the removed prefix.
    assert compacted.messages[1:] == messages[2:]
    assert compacted.compaction.source_digest == digest_messages(messages[:2])
    # Dropped tool calls stay provenanced by digest/snippet in the summary.
    assert "call_dangling" in compacted.compaction.summary
    assert "arguments_sha256" in compacted.compaction.summary
    assert (
        compact_projection(
            projection,
            keep_messages=3,
            context_window_tokens=1_024,
            soft_utilization=0.5,
        ).compaction
        == compacted.compaction
    )


def test_compaction_boundary_skips_tool_results_then_balances() -> None:
    messages = (
        ChatMessage(role="user", content="u0 " + "x" * 2_000),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"a"}'},
                }
            ],
        ),
        ChatMessage(role="tool", content="result a", name="read", tool_call_id="call_a"),
        ChatMessage(role="user", content="u3"),
        ChatMessage(role="assistant", content="done"),
    )
    projection = _projection("session-paired", messages)

    compacted = compact_projection(
        projection,
        keep_messages=3,
        context_window_tokens=1_024,
        soft_utilization=0.5,
    )

    assert compacted.compaction is not None
    # Leading tool results are still skipped, and the assistant owning them is
    # dropped with them — the kept tail is tool-pairing balanced.
    assert compacted.messages[1:] == messages[3:]
    assert "call_a" in compacted.compaction.summary


def test_compaction_keeps_inflight_assistant_when_results_present() -> None:
    messages = (
        ChatMessage(role="user", content="u0 " + "x" * 2_000),
        ChatMessage(role="user", content="u1"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_kept",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"k"}'},
                }
            ],
        ),
        ChatMessage(role="tool", content="result k", name="read", tool_call_id="call_kept"),
        ChatMessage(role="user", content="u4"),
    )
    projection = _projection("session-balanced", messages)

    compacted = compact_projection(
        projection,
        keep_messages=3,
        context_window_tokens=1_024,
        soft_utilization=0.5,
    )

    assert compacted.compaction is not None
    # A boundary assistant whose calls all have results in the tail is kept.
    assert compacted.messages[1:] == messages[2:]


def test_dangling_boundary_projection_replays_through_anthropic_transport() -> None:
    from merlin_contracts import ChatRequest
    from merlin_providers import AnthropicMessagesConfig, AnthropicMessagesProvider

    messages = (
        ChatMessage(role="user", content="u0 " + "x" * 2_000),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_dangling",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"old"}'},
                }
            ],
        ),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_paired",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"new"}'},
                }
            ],
        ),
        ChatMessage(role="tool", content="new result", name="read", tool_call_id="call_paired"),
        ChatMessage(role="user", content="next"),
    )
    projection = _projection("session-anthropic-replay", messages)

    compacted = compact_projection(
        projection,
        keep_messages=4,
        context_window_tokens=1_024,
        soft_utilization=0.5,
    )

    assert compacted.compaction is not None
    anthropic = AnthropicMessagesProvider(
        AnthropicMessagesConfig("https://api.anthropic.test/v1", "", "claude-opus-4-7")
    )
    payload = anthropic._payload(
        ChatRequest(model="claude-opus-4-7", messages=list(compacted.messages))
    )
    blocks = [block for message in payload["messages"] for block in message["content"]]
    tool_use_ids = {block["id"] for block in blocks if block.get("type") == "tool_use"}
    tool_result_ids = {
        block["tool_use_id"] for block in blocks if block.get("type") == "tool_result"
    }
    # Anthropic rejects unpaired tool_use/tool_result blocks; the replayed
    # projection must be exactly balanced on both sides.
    assert tool_use_ids == tool_result_ids == {"call_paired"}
