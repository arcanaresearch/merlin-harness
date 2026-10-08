from __future__ import annotations

import json

from merlin_contracts import ChatMessage, ChatRequest

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
from merlin_providers.claude_cli import _request_prompt as _claude_request_prompt
from merlin_providers.kimi_acp import _request_prompt as _kimi_request_prompt


def _mixed_transcript() -> list[ChatMessage]:
    """One conversation whose turns were routed over different transports."""
    return [
        ChatMessage(role="system", content="Be terse"),
        ChatMessage(role="user", content="inspect the target"),
        # Turn recorded from an OpenAI chat-completions route.
        ChatMessage(
            role="assistant",
            content="Reading the entrypoint",
            reasoning_content="openai draft reasoning",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                }
            ],
        ),
        ChatMessage(role="tool", content="body a", name="read_file", tool_call_id="call_1"),
        # Turn recorded from an Anthropic messages route.
        ChatMessage(
            role="assistant",
            content="Reading the config",
            reasoning_content="anthropic draft reasoning",
            tool_calls=[
                {
                    "id": "toolu_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"b.txt"}'},
                }
            ],
        ),
        ChatMessage(role="tool", content="body b", name="read_file", tool_call_id="toolu_1"),
        # Turn recorded from a Gemini route (locally synthesized call id).
        ChatMessage(
            role="assistant",
            content="Snapshotting",
            reasoning_content="gemini draft reasoning",
            tool_calls=[
                {
                    "id": "gemini_abc123",
                    "type": "function",
                    "function": {"name": "browser_snapshot", "arguments": '{"tab_ref":"tab:1"}'},
                }
            ],
        ),
        ChatMessage(
            role="tool",
            content="snapshot",
            name="browser_snapshot",
            tool_call_id="gemini_abc123",
        ),
        ChatMessage(role="user", content="continue"),
    ]


_CALL_IDS = {"call_1", "toolu_1", "gemini_abc123"}


def _stringify(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def test_openai_chat_projection_keeps_foreign_call_ids_opaque() -> None:
    provider = OpenAICompatibleProvider(ProviderConfig("https://models.example/v1", "", "model"))
    payload = provider._payload(ChatRequest(model="model", messages=_mixed_transcript()))

    messages = payload["messages"]
    serialized = _stringify(messages)
    # Foreign call ids are opaque and round-trip verbatim in both directions.
    seen_call_ids = {call["id"] for message in messages for call in message.get("tool_calls", [])}
    seen_result_ids = {
        message.get("tool_call_id") for message in messages if "tool_call_id" in message
    }
    assert seen_call_ids == _CALL_IDS
    assert seen_result_ids == _CALL_IDS
    # No Anthropic/Gemini block envelopes leak into chat-completions messages.
    assert "tool_use" not in serialized
    assert "tool_result" not in serialized
    assert "functionCall" not in serialized
    assert "functionResponse" not in serialized
    assert "thinking" not in serialized
    # Reasoning stays plain reasoning_content text, never a foreign thought part.
    reasoning = [message.get("reasoning_content") for message in messages]
    assert reasoning.count(None) == len(messages) - 3
    assert "thought" not in serialized


def test_anthropic_projection_drops_unsigned_foreign_thinking() -> None:
    provider = AnthropicMessagesProvider(
        AnthropicMessagesConfig("https://api.anthropic.test/v1", "", "claude-opus-4-7")
    )
    payload = provider._payload(ChatRequest(model="claude-opus-4-7", messages=_mixed_transcript()))

    assert payload["system"][0]["text"] == "Be terse"
    messages = payload["messages"]
    blocks = [block for message in messages for block in message["content"]]
    # Reasoning recorded by other transports has no signature; it is dropped
    # rather than replayed as an invalid unsigned thinking block.
    assert not [block for block in blocks if block["type"] == "thinking"]
    tool_use_ids = {block["id"] for block in blocks if block["type"] == "tool_use"}
    assert tool_use_ids == _CALL_IDS
    tool_result_ids = {block["tool_use_id"] for block in blocks if block["type"] == "tool_result"}
    assert tool_result_ids == _CALL_IDS
    # No OpenAI/Responses/Gemini envelopes leak into Anthropic blocks.
    serialized = _stringify(messages)
    assert "function_call" not in serialized
    assert "functionCall" not in serialized
    assert "reasoning_content" not in serialized


def test_responses_projection_pairs_foreign_calls_and_outputs() -> None:
    provider = OpenAIResponsesProvider(
        OpenAIResponsesConfig("https://api.openai.test/v1", "", "gpt-5.4")
    )
    payload = provider._payload(ChatRequest(model="gpt-5.4", messages=_mixed_transcript()))

    items = payload["input"]
    function_calls = [item for item in items if item.get("type") == "function_call"]
    outputs = [item for item in items if item.get("type") == "function_call_output"]
    assert {item["call_id"] for item in function_calls} == _CALL_IDS
    assert {item["call_id"] for item in outputs} == _CALL_IDS
    reasoning_items = [item for item in items if item.get("type") == "reasoning"]
    assert len(reasoning_items) == 3
    # Every reasoning summary is plain text; no Gemini thought flags or
    # Anthropic signature fields leak into Responses items.
    serialized = _stringify(items)
    assert "thought" not in serialized
    assert "signature" not in serialized
    assert "tool_use" not in serialized


def test_gemini_projection_converts_without_leaking_call_ids() -> None:
    provider = GeminiProvider(
        GeminiConfig("https://generativelanguage.test/v1beta", "", "gemini-2.5-pro")
    )
    payload = provider._payload(ChatRequest(model="gemini-2.5-pro", messages=_mixed_transcript()))

    contents = payload["contents"]
    model_parts = [part for item in contents if item["role"] == "model" for part in item["parts"]]
    thoughts = [part for part in model_parts if part.get("thought") is True]
    assert [part["text"] for part in thoughts] == [
        "openai draft reasoning",
        "anthropic draft reasoning",
        "gemini draft reasoning",
    ]
    function_calls = [part["functionCall"] for part in model_parts if "functionCall" in part]
    assert [call["name"] for call in function_calls] == [
        "read_file",
        "read_file",
        "browser_snapshot",
    ]
    responses = [
        part["functionResponse"]
        for item in contents
        for part in item["parts"]
        if "functionResponse" in part
    ]
    assert [response["name"] for response in responses] == [
        "read_file",
        "read_file",
        "browser_snapshot",
    ]
    # Gemini has no call-id concept: foreign ids are dropped, never leaked into
    # args or response envelopes, and no OpenAI/Anthropic blocks appear.
    serialized = _stringify(contents)
    for call_id in _CALL_IDS:
        assert call_id not in serialized
    assert "tool_use" not in serialized
    assert "tool_call_id" not in serialized
    assert "function_call" not in serialized


def test_cli_prompts_render_text_only_without_foreign_blocks() -> None:
    transcript = _mixed_transcript()
    for request_prompt in (_claude_request_prompt, _kimi_request_prompt):
        prompt, history = request_prompt(transcript, ())

        assert history == tuple(message.model_dump_json() for message in transcript)
        # Role labels and message content render; reasoning, call ids, and
        # tool-call JSON from any transport never leak into the prompt text.
        assert "[ASSISTANT]" in prompt
        assert "Reading the entrypoint" in prompt
        assert "body a" in prompt
        for leaked in (
            "openai draft reasoning",
            "anthropic draft reasoning",
            "gemini draft reasoning",
            "call_1",
            "toolu_1",
            "gemini_abc123",
            "tool_calls",
            "functionCall",
            "tool_use",
        ):
            assert leaked not in prompt
