from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from merlin_contracts import (
    ChatMessage,
    ChatRequest,
    DynamicToolOutput,
    ModelCompleted,
    ModelError,
    ModelUsage,
    ProviderFailureKind,
    ReasoningDelta,
    TextDelta,
    ToolCall,
)

from merlin_providers import CodexAppServerConfig, CodexAppServerProvider


def _write_server(path: Path, body: str) -> tuple[str, ...]:
    path.write_text(body)
    return (sys.executable, "-u", str(path))


def test_codex_preserves_legacy_advisory_off_and_rejects_required_off(tmp_path: Path) -> None:
    provider = CodexAppServerProvider(
        CodexAppServerConfig(command=("does-not-exist",), model="gpt", state_directory=tmp_path)
    )

    async def collect(request: ChatRequest) -> list[object]:
        return [event async for event in provider.stream(request)]

    advisory = asyncio.run(collect(ChatRequest(model="gpt", messages=[], reasoning="disabled")))
    required = asyncio.run(
        collect(
            ChatRequest(
                model="gpt",
                messages=[],
                workload="structured_generation",
                reasoning="disabled",
                reasoning_control="required",
            )
        )
    )
    assert isinstance(advisory[0], ModelError)
    assert advisory[0].code != "unsupported_request_control"
    assert isinstance(required[0], ModelError)
    assert required[0].code == "unsupported_request_control"


def test_app_server_streams_reasoning_text_usage_and_reuses_thread(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "server.py",
        """
import json, sys

thread_id = "thread-1"
turn = 0
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        print(json.dumps({"id": message["id"], "result": {"serverInfo": {"name": "fake"}}}), flush=True)
    elif method == "thread/start":
        if "dynamicTools" in message["params"]:
            print(json.dumps({"id": message["id"], "error": {"message": "unexpected dynamicTools"}}), flush=True)
        else:
            print(json.dumps({"id": message["id"], "result": {"thread": {"id": thread_id}}}), flush=True)
    elif method == "turn/start":
        turn += 1
        turn_id = f"turn-{turn}"
        print(json.dumps({"id": message["id"], "result": {"turn": {"id": turn_id}}}), flush=True)
        print(json.dumps({"method": "item/reasoning/summaryTextDelta", "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "r", "summaryIndex": 0, "delta": "think "}}), flush=True)
        print(json.dumps({"method": "item/agentMessage/delta", "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "a", "delta": f"answer-{turn}"}}), flush=True)
        print(json.dumps({"method": "thread/tokenUsage/updated", "params": {"threadId": thread_id, "turnId": turn_id, "tokenUsage": {"last": {"inputTokens": 4, "cachedInputTokens": 1, "outputTokens": 2, "reasoningOutputTokens": 1, "totalTokens": 6}, "total": {"inputTokens": 4, "cachedInputTokens": 1, "outputTokens": 2, "reasoningOutputTokens": 1, "totalTokens": 6}}}}), flush=True)
        print(json.dumps({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed", "items": []}}}), flush=True)
""",
    )

    async def scenario() -> None:
        provider = CodexAppServerProvider(
            CodexAppServerConfig(command=command, model="gpt-test", state_directory=tmp_path)
        )
        first = [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="gpt-test",
                    budget_id="assignment-1",
                    messages=[ChatMessage(role="user", content="first")],
                )
            )
        ]
        second = [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="gpt-test",
                    budget_id="assignment-1",
                    messages=[
                        ChatMessage(role="user", content="first"),
                        ChatMessage(role="assistant", content="answer-1"),
                        ChatMessage(role="user", content="second"),
                    ],
                )
            )
        ]
        await provider.close()

        assert any(isinstance(event, ReasoningDelta) and event.text == "think " for event in first)
        assert any(isinstance(event, TextDelta) and event.text == "answer-1" for event in first)
        assert any(isinstance(event, ModelUsage) and event.total_tokens == 6 for event in first)
        assert isinstance(first[-1], ModelCompleted)
        assert any(isinstance(event, TextDelta) and event.text == "answer-2" for event in second)

    asyncio.run(scenario())


def test_app_server_rejects_merlin_tools_before_launch(tmp_path: Path) -> None:
    provider = CodexAppServerProvider(
        CodexAppServerConfig(
            command=("does-not-exist",), model="gpt-test", state_directory=tmp_path
        )
    )

    async def scenario() -> list[object]:
        return [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="gpt-test",
                    messages=[ChatMessage(role="user", content="hello")],
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "browser_open_tab",
                                "description": "Open a tab",
                                "parameters": {"type": "object"},
                            },
                        }
                    ],
                )
            )
        ]

    events = asyncio.run(scenario())
    assert len(events) == 1
    assert isinstance(events[0], ModelError)
    assert events[0].code == "codex_dynamic_tools_unsupported"


def test_app_server_reports_the_configured_total_turn_timeout(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "server.py",
        """
import json, sys, time
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
    elif message.get("method") == "thread/start":
        print(json.dumps({"id": message["id"], "result": {"thread": {"id": "thread-1"}}}), flush=True)
    elif message.get("method") == "turn/start":
        print(json.dumps({"id": message["id"], "result": {"turn": {"id": "turn-1"}}}), flush=True)
        time.sleep(1)
""",
    )

    async def scenario() -> list[object]:
        provider = CodexAppServerProvider(
            CodexAppServerConfig(
                command=command,
                model="gpt-test",
                state_directory=tmp_path,
                idle_timeout_seconds=1,
                turn_timeout_seconds=0.05,
            )
        )
        events = [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="gpt-test",
                    budget_id="assignment-1",
                    messages=[ChatMessage(role="user", content="wait")],
                )
            )
        ]
        await provider.close()
        return events

    events = asyncio.run(scenario())
    assert len(events) == 1
    assert isinstance(events[0], ModelError)
    assert events[0].code == "codex_timeout"
    assert events[0].retryable is True
    assert events[0].failure_kind is ProviderFailureKind.TRANSIENT
    assert events[0].message == (
        "Codex App Server turn exceeded the configured total timeout of 0.05 seconds"
    )


def test_app_server_invalid_launch_configuration_is_not_retryable(tmp_path: Path) -> None:
    async def scenario() -> list[object]:
        provider = CodexAppServerProvider(
            CodexAppServerConfig(
                command=(str(tmp_path / "missing-codex-app-server"),),
                model="gpt-test",
                state_directory=tmp_path,
            )
        )
        return [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="gpt-test",
                    messages=[ChatMessage(role="user", content="hello")],
                )
            )
        ]

    events = asyncio.run(scenario())
    assert len(events) == 1
    assert isinstance(events[0], ModelError)
    assert events[0].code == "codex_launch_error"
    assert events[0].retryable is False


def test_app_server_distinguishes_idle_timeout_from_total_timeout(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "server.py",
        """
import json, sys, time
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
    elif message.get("method") == "thread/start":
        print(json.dumps({"id": message["id"], "result": {"thread": {"id": "thread-1"}}}), flush=True)
    elif message.get("method") == "turn/start":
        print(json.dumps({"id": message["id"], "result": {"turn": {"id": "turn-1"}}}), flush=True)
        time.sleep(1)
""",
    )

    async def scenario() -> list[object]:
        provider = CodexAppServerProvider(
            CodexAppServerConfig(
                command=command,
                model="gpt-test",
                state_directory=tmp_path,
                idle_timeout_seconds=0.05,
                turn_timeout_seconds=1,
            )
        )
        events = [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="gpt-test",
                    budget_id="assignment-1",
                    messages=[ChatMessage(role="user", content="wait")],
                )
            )
        ]
        await provider.close()
        return events

    events = asyncio.run(scenario())
    assert len(events) == 1
    assert isinstance(events[0], ModelError)
    assert events[0].code == "codex_timeout"
    assert events[0].retryable is True
    assert events[0].message == "Codex App Server emitted no protocol message for 0.05 seconds"


def test_app_server_uses_shorter_idle_timeout_after_visible_progress(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "server.py",
        """
import json, sys, time
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
    elif message.get("method") == "thread/start":
        print(json.dumps({"id": message["id"], "result": {"thread": {"id": "thread-1"}}}), flush=True)
    elif message.get("method") == "turn/start":
        print(json.dumps({"id": message["id"], "result": {"turn": {"id": "turn-1"}}}), flush=True)
        print(json.dumps({"method": "item/reasoning/textDelta", "params": {"turnId": "turn-1", "delta": "working"}}), flush=True)
        time.sleep(1)
""",
    )

    async def scenario() -> list[object]:
        provider = CodexAppServerProvider(
            CodexAppServerConfig(
                command=command,
                model="gpt-test",
                state_directory=tmp_path,
                idle_timeout_seconds=1,
                partial_idle_timeout_seconds=0.05,
                turn_timeout_seconds=2,
            )
        )
        events = [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="gpt-test",
                    budget_id="assignment-progress",
                    messages=[ChatMessage(role="user", content="wait")],
                )
            )
        ]
        await provider.close()
        return events

    events = asyncio.run(scenario())
    assert isinstance(events[0], ReasoningDelta)
    assert isinstance(events[-1], ModelError)
    assert events[-1].code == "codex_timeout"
    assert events[-1].message == "Codex App Server emitted no protocol message for 0.05 seconds"


def test_app_server_returns_dynamic_tool_for_brokered_next_turn(tmp_path: Path) -> None:
    transcript = tmp_path / "dynamic-tool.json"
    command = _write_server(
        tmp_path / "server.py",
        f"""
import json, pathlib, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        print(json.dumps({{"id": message["id"], "result": {{}}}}), flush=True)
    elif method == "thread/start":
        pathlib.Path({str(transcript)!r}).write_text(json.dumps(message["params"]["dynamicTools"]))
        print(json.dumps({{"id": message["id"], "result": {{"thread": {{"id": "thread-1"}}}}}}), flush=True)
    elif method == "turn/start":
        print(json.dumps({{"id": message["id"], "result": {{"turn": {{"id": "turn-1"}}}}}}), flush=True)
        print(json.dumps({{"method": "item/started", "params": {{"threadId": "thread-1", "turnId": "turn-1", "item": {{"id": "call-1", "type": "dynamicToolCall"}}}}}}), flush=True)
        print(json.dumps({{"id": 91, "method": "item/tool/call", "params": {{"threadId": "thread-1", "turnId": "turn-1", "callId": "call-1", "tool": "knowledge_search", "arguments": {{"query": "bounded"}}}}}}), flush=True)
    elif message.get("id") == 91:
        pathlib.Path({str(transcript)!r}).write_text(pathlib.Path({str(transcript)!r}).read_text() + "\\n" + json.dumps(message))
        print(json.dumps({{"method": "item/agentMessage/delta", "params": {{"threadId": "thread-1", "turnId": "turn-1", "itemId": "a", "delta": "used-result"}}}}), flush=True)
        print(json.dumps({{"method": "turn/completed", "params": {{"threadId": "thread-1", "turn": {{"id": "turn-1", "status": "completed", "items": []}}}}}}), flush=True)
""",
    )

    async def scenario() -> tuple[list[object], list[ToolCall]]:
        provider = CodexAppServerProvider(
            CodexAppServerConfig(command=command, model="gpt-test", state_directory=tmp_path)
        )
        calls: list[ToolCall] = []

        async def handle(call: ToolCall) -> DynamicToolOutput:
            calls.append(call)
            return DynamicToolOutput(content='{"matches":["bounded"]}')

        events = [
            event
            async for event in provider.stream_with_tools(
                ChatRequest(
                    model="gpt-test",
                    budget_id="assignment-1",
                    messages=[ChatMessage(role="user", content="search")],
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "knowledge_search",
                                "description": "Search bounded knowledge",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"query": {"type": "string"}},
                                    "required": ["query"],
                                },
                            },
                        }
                    ],
                ),
                handle,
            )
        ]
        await provider.close()
        return events, calls

    events, calls = asyncio.run(scenario())
    assert calls == []
    assert (
        ToolCall(call_id="call-1", name="knowledge_search", arguments={"query": "bounded"})
        in events
    )
    assert any(isinstance(event, TextDelta) and event.text == "used-result" for event in events)
    assert isinstance(events[-1], ModelCompleted)
    tools, response = transcript.read_text().splitlines()
    assert json.loads(tools) == [
        {
            "type": "function",
            "name": "knowledge_search",
            "description": "Search bounded knowledge",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    ]
    assert json.loads(response) == {
        "id": 91,
        "result": {
            "contentItems": [
                {
                    "type": "inputText",
                    "text": (
                        "Accepted by Merlin. The authority-checked result will be supplied "
                        "in the next model step; end this turn now."
                    ),
                }
            ],
            "success": True,
        },
    }


def test_app_server_fails_closed_on_builtin_tool_activity(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "server.py",
        """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
    elif message.get("method") == "thread/start":
        print(json.dumps({"id": message["id"], "result": {"thread": {"id": "thread-1"}}}), flush=True)
    elif message.get("method") == "turn/start":
        print(json.dumps({"id": message["id"], "result": {"turn": {"id": "turn-1"}}}), flush=True)
        print(json.dumps({"method": "item/started", "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {"id": "cmd", "type": "commandExecution"}}}), flush=True)
""",
    )

    async def scenario() -> list[object]:
        provider = CodexAppServerProvider(
            CodexAppServerConfig(command=command, model="gpt-test", state_directory=tmp_path)
        )
        events = [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="gpt-test",
                    messages=[ChatMessage(role="user", content="hello")],
                )
            )
        ]
        await provider.close()
        return events

    events = asyncio.run(scenario())
    assert isinstance(events[-1], ModelError)
    assert events[-1].code == "codex_forbidden_tool"


def test_app_server_forbidden_authority_request_is_not_retryable(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "server.py",
        """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
    elif message.get("method") == "thread/start":
        print(json.dumps({"id": message["id"], "result": {"thread": {"id": "thread-1"}}}), flush=True)
    elif message.get("method") == "turn/start":
        print(json.dumps({"id": message["id"], "result": {"turn": {"id": "turn-1"}}}), flush=True)
        print(json.dumps({"id": 91, "method": "item/tool/call", "params": {}}), flush=True)
""",
    )

    async def scenario() -> list[object]:
        provider = CodexAppServerProvider(
            CodexAppServerConfig(command=command, model="gpt-test", state_directory=tmp_path)
        )
        return [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="gpt-test",
                    messages=[ChatMessage(role="user", content="hello")],
                )
            )
        ]

    events = asyncio.run(scenario())
    assert len(events) == 1
    assert isinstance(events[0], ModelError)
    assert events[0].code == "codex_forbidden_authority"
    assert events[0].retryable is False
    assert events[0].failure_kind is ProviderFailureKind.PERMISSION
    assert events[0].message == "Codex App Server requested forbidden authority: item/tool/call"


def test_app_server_allows_non_executable_context_compaction_item(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "server.py",
        """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
    elif method == "thread/start":
        print(json.dumps({"id": message["id"], "result": {"thread": {"id": "thread-1"}}}), flush=True)
    elif method == "turn/start":
        print(json.dumps({"id": message["id"], "result": {"turn": {"id": "turn-1"}}}), flush=True)
        print(json.dumps({"method": "item/started", "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {"id": "compact-1", "type": "contextCompaction"}}}), flush=True)
        print(json.dumps({"method": "item/completed", "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {"id": "compact-1", "type": "contextCompaction"}}}), flush=True)
        print(json.dumps({"method": "item/agentMessage/delta", "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "a", "delta": "continued"}}), flush=True)
        print(json.dumps({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed", "items": []}}}), flush=True)
""",
    )

    async def scenario() -> list[object]:
        provider = CodexAppServerProvider(
            CodexAppServerConfig(command=command, model="gpt-test", state_directory=tmp_path)
        )
        events = [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="gpt-test",
                    budget_id="assignment-1",
                    messages=[ChatMessage(role="user", content="continue after compaction")],
                )
            )
        ]
        await provider.close()
        return events

    events = asyncio.run(scenario())
    assert any(isinstance(event, TextDelta) and event.text == "continued" for event in events)
    assert isinstance(events[-1], ModelCompleted)


def test_app_server_enforces_protocol_line_bound(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "server.py",
        """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print("{" + "x" * 4096, flush=True)
""",
    )

    async def scenario() -> list[object]:
        provider = CodexAppServerProvider(
            CodexAppServerConfig(
                command=command,
                model="gpt-test",
                state_directory=tmp_path,
                max_line_bytes=1024,
            )
        )
        return [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="gpt-test",
                    messages=[ChatMessage(role="user", content="hello")],
                )
            )
        ]

    events = asyncio.run(scenario())
    assert isinstance(events[-1], ModelError)
    assert events[-1].code == "codex_protocol_error"


def test_app_server_reports_turn_start_error_without_waiting_for_timeout(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "server.py",
        """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
    elif message.get("method") == "thread/start":
        print(json.dumps({"id": message["id"], "result": {"thread": {"id": "thread-1"}}}), flush=True)
    elif message.get("method") == "turn/start":
        print(json.dumps({"id": message["id"], "error": {"code": -32600, "message": "invalid sandbox"}}), flush=True)
""",
    )

    async def scenario() -> list[object]:
        provider = CodexAppServerProvider(
            CodexAppServerConfig(command=command, model="gpt-test", state_directory=tmp_path)
        )
        return [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="gpt-test",
                    messages=[ChatMessage(role="user", content="hello")],
                )
            )
        ]

    events = asyncio.run(scenario())
    assert isinstance(events[-1], ModelError)
    assert events[-1].code == "provider_configuration_error"
    assert events[-1].message == "invalid sandbox"
    assert events[-1].retryable is False
    assert events[-1].failure_kind is ProviderFailureKind.CONFIGURATION


def test_app_server_invalid_json_is_not_retryable(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "server.py",
        """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
    elif message.get("method") == "thread/start":
        print(json.dumps({"id": message["id"], "result": {"thread": {"id": "thread-1"}}}), flush=True)
    elif message.get("method") == "turn/start":
        print(json.dumps({"id": message["id"], "result": {"turn": {"id": "turn-1"}}}), flush=True)
        print('■ {"detail":"Bad Request"}', flush=True)
        break
""",
    )

    async def scenario() -> list[object]:
        provider = CodexAppServerProvider(
            CodexAppServerConfig(command=command, model="gpt-test", state_directory=tmp_path)
        )
        return [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="gpt-test",
                    messages=[ChatMessage(role="user", content="hello")],
                )
            )
        ]

    events = asyncio.run(scenario())
    assert len(events) == 1
    assert isinstance(events[0], ModelError)
    assert events[0].code == "codex_protocol_error"
    assert events[0].retryable is False
    assert events[0].message == "Codex App Server emitted invalid JSON"


def test_app_server_abnormal_eof_is_retryable_without_becoming_completion(
    tmp_path: Path,
) -> None:
    command = _write_server(
        tmp_path / "server.py",
        """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
    elif message.get("method") == "thread/start":
        print(json.dumps({"id": message["id"], "result": {"thread": {"id": "thread-1"}}}), flush=True)
    elif message.get("method") == "turn/start":
        print(json.dumps({"id": message["id"], "result": {"turn": {"id": "turn-1"}}}), flush=True)
        break
""",
    )

    async def scenario() -> list[object]:
        provider = CodexAppServerProvider(
            CodexAppServerConfig(command=command, model="gpt-test", state_directory=tmp_path)
        )
        return [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="gpt-test",
                    messages=[ChatMessage(role="user", content="hello")],
                )
            )
        ]

    events = asyncio.run(scenario())
    assert len(events) == 1
    assert isinstance(events[0], ModelError)
    assert events[0].code == "codex_app_server_error"
    assert events[0].retryable is True
    assert events[0].message.startswith("Codex App Server closed its output")


def test_app_server_interrupts_exact_turn_when_consumer_is_cancelled(tmp_path: Path) -> None:
    marker = tmp_path / "interrupted"
    command = _write_server(
        tmp_path / "server.py",
        f"""
import json, pathlib, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        print(json.dumps({{"id": message["id"], "result": {{}}}}), flush=True)
    elif method == "thread/start":
        print(json.dumps({{"id": message["id"], "result": {{"thread": {{"id": "thread-1"}}}}}}), flush=True)
    elif method == "turn/start":
        print(json.dumps({{"id": message["id"], "result": {{"turn": {{"id": "turn-1"}}}}}}), flush=True)
        print(json.dumps({{"method": "item/agentMessage/delta", "params": {{"threadId": "thread-1", "turnId": "turn-1", "itemId": "a", "delta": "started"}}}}), flush=True)
    elif method == "turn/interrupt":
        pathlib.Path({str(marker)!r}).write_text("turn-1")
        print(json.dumps({{"id": message["id"], "result": {{}}}}), flush=True)
""",
    )

    async def scenario() -> None:
        provider = CodexAppServerProvider(
            CodexAppServerConfig(command=command, model="gpt-test", state_directory=tmp_path)
        )
        observed = asyncio.Event()

        async def consume() -> None:
            async for event in provider.stream(
                ChatRequest(
                    model="gpt-test",
                    budget_id="assignment-1",
                    messages=[ChatMessage(role="user", content="hello")],
                )
            ):
                if isinstance(event, TextDelta):
                    observed.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(observed.wait(), 2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await provider.close()

    asyncio.run(scenario())
    assert marker.read_text() == "turn-1"
