from __future__ import annotations

import asyncio
import sys
from pathlib import Path

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

from merlin_providers import ClaudeCliConfig, ClaudeCliProvider


def _write_cli(path: Path, body: str) -> tuple[str, ...]:
    path.write_text(body)
    return (sys.executable, "-u", str(path))


def test_claude_cli_preserves_legacy_advisory_off_and_rejects_required_off(
    tmp_path: Path,
) -> None:
    provider = ClaudeCliProvider(
        ClaudeCliConfig(command=("does-not-exist",), model="claude", state_directory=tmp_path)
    )

    async def collect(request: ChatRequest) -> list[object]:
        return [event async for event in provider.stream(request)]

    advisory = asyncio.run(collect(ChatRequest(model="claude", messages=[], reasoning="disabled")))
    required = asyncio.run(
        collect(
            ChatRequest(
                model="claude",
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


def test_claude_cli_streams_partial_events_and_reuses_owned_session(tmp_path: Path) -> None:
    record = tmp_path / "calls.jsonl"
    command = _write_cli(
        tmp_path / "claude.py",
        f"""
import json, pathlib, sys
pathlib.Path({str(record)!r}).open("a").write(json.dumps(sys.argv[1:]) + "\\n")
print(json.dumps({{"type":"system","subtype":"init","session_id":"owned"}}), flush=True)
print(json.dumps({{"type":"stream_event","event":{{"type":"content_block_delta","delta":{{"type":"thinking_delta","thinking":"think "}}}}}}), flush=True)
print(json.dumps({{"type":"stream_event","event":{{"type":"content_block_delta","delta":{{"type":"text_delta","text":"answer"}}}}}}), flush=True)
print(json.dumps({{"type":"result","subtype":"success","is_error":False,"session_id":"owned","usage":{{"input_tokens":4,"output_tokens":2}},"total_cost_usd":0.01}}), flush=True)
""",
    )

    async def scenario() -> tuple[list[object], list[object]]:
        provider = ClaudeCliProvider(
            ClaudeCliConfig(command=command, model="claude-test", state_directory=tmp_path)
        )
        first = [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="claude-test",
                    budget_id="assignment-1",
                    messages=[ChatMessage(role="user", content="first")],
                )
            )
        ]
        second = [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="claude-test",
                    budget_id="assignment-1",
                    messages=[
                        ChatMessage(role="user", content="first"),
                        ChatMessage(role="assistant", content="answer"),
                        ChatMessage(role="user", content="second"),
                    ],
                )
            )
        ]
        return first, second

    first, second = asyncio.run(scenario())
    assert any(isinstance(event, ReasoningDelta) and event.text == "think " for event in first)
    assert any(isinstance(event, TextDelta) and event.text == "answer" for event in first)
    assert any(
        isinstance(event, ModelUsage)
        and event.total_tokens == 6
        and event.estimated_cost_usd == 0.01
        for event in first
    )
    assert isinstance(first[-1], ModelCompleted)
    assert isinstance(second[-1], ModelCompleted)
    calls = [line for line in record.read_text().splitlines() if line]
    assert "--session-id" in calls[0]
    assert "--resume" in calls[1]
    assert all('"--tools", ""' in call for call in calls)
    assert all(
        "--strict-mcp-config" in call and "--safe-mode" in call and "--verbose" in call
        for call in calls
    )


def test_claude_cli_rejects_tools_before_launch(tmp_path: Path) -> None:
    provider = ClaudeCliProvider(
        ClaudeCliConfig(command=("does-not-exist",), model="claude-test", state_directory=tmp_path)
    )

    async def scenario() -> list[object]:
        return [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="claude-test",
                    messages=[ChatMessage(role="user", content="hello")],
                    tools=[{"type": "function", "function": {"name": "tool"}}],
                )
            )
        ]

    events = asyncio.run(scenario())
    assert isinstance(events[-1], ModelError)
    assert events[-1].code == "claude_cli_tools_unsupported"
    assert events[-1].failure_kind is ProviderFailureKind.CONFIGURATION


def test_claude_cli_admits_reviewed_mcp_tool_call(tmp_path: Path) -> None:
    command = _write_cli(
        tmp_path / "claude.py",
        """
import json, subprocess, sys, time
arguments = sys.argv[1:]
config = json.loads(arguments[arguments.index("--mcp-config") + 1])
server = config["mcpServers"]["merlin"]
child = subprocess.Popen(
    [server["command"], *server["args"]],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    text=True,
)
assert child.stdin is not None and child.stdout is not None
child.stdin.write(json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"target_request","arguments":{"message":"hello"}}}) + "\\n")
child.stdin.flush()
json.loads(child.stdout.readline())
child.stdin.close()
child.wait()
time.sleep(0.1)
print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":""}), flush=True)
""",
    )

    async def scenario() -> list[object]:
        provider = ClaudeCliProvider(
            ClaudeCliConfig(command=command, model="claude-test", state_directory=tmp_path)
        )

        async def unused_handler(_call: ToolCall) -> str:
            raise AssertionError("the transport must not execute Merlin tools")

        return [
            event
            async for event in provider.stream_with_tools(
                ChatRequest(
                    model="claude-test",
                    messages=[ChatMessage(role="user", content="hello")],
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "target_request",
                                "parameters": {"type": "object"},
                            },
                        }
                    ],
                ),
                unused_handler,
            )
        ]

    events = asyncio.run(scenario())
    calls = [event for event in events if isinstance(event, ToolCall)]
    assert len(calls) == 1
    assert calls[0].name == "target_request"
    assert calls[0].arguments == {"message": "hello"}
    assert isinstance(events[-1], ModelCompleted)


def test_claude_cli_fails_closed_on_tool_use_event(tmp_path: Path) -> None:
    command = _write_cli(
        tmp_path / "claude.py",
        """
import json
print(json.dumps({"type":"stream_event","event":{"type":"content_block_start","content_block":{"type":"tool_use","name":"Bash"}}}), flush=True)
""",
    )

    async def scenario() -> list[object]:
        provider = ClaudeCliProvider(
            ClaudeCliConfig(command=command, model="claude-test", state_directory=tmp_path)
        )
        return [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="claude-test",
                    messages=[ChatMessage(role="user", content="hello")],
                )
            )
        ]

    events = asyncio.run(scenario())
    assert isinstance(events[-1], ModelError)
    assert events[-1].code == "claude_cli_forbidden_tool"
    assert events[-1].failure_kind is ProviderFailureKind.PERMISSION


def test_claude_cli_enforces_output_bound_and_cancels_process(tmp_path: Path) -> None:
    command = _write_cli(
        tmp_path / "claude.py",
        """
import json, time
print(json.dumps({"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"abcdefghijklmnopqrstuvwxyz"}}}), flush=True)
time.sleep(30)
""",
    )

    async def scenario() -> list[object]:
        provider = ClaudeCliProvider(
            ClaudeCliConfig(command=command, model="claude-test", state_directory=tmp_path)
        )
        return [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="claude-test",
                    max_output_tokens=4,
                    messages=[ChatMessage(role="user", content="hello")],
                )
            )
        ]

    events = asyncio.run(scenario())
    assert any(
        isinstance(event, TextDelta) and event.text == "abcdefghijklmnop" for event in events
    )
    assert isinstance(events[-1], ModelCompleted)
    assert events[-1].disposition == "truncated"


def test_claude_cli_uses_shorter_idle_timeout_after_visible_progress(tmp_path: Path) -> None:
    command = _write_cli(
        tmp_path / "claude.py",
        """
import json, time
print(json.dumps({"type":"stream_event","event":{"delta":{"type":"thinking_delta","thinking":"working"}}}), flush=True)
time.sleep(1)
""",
    )

    async def scenario() -> list[object]:
        provider = ClaudeCliProvider(
            ClaudeCliConfig(
                command=command,
                idle_timeout_seconds=1,
                partial_idle_timeout_seconds=0.05,
                turn_timeout_seconds=2,
            )
        )
        return [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="claude-test", messages=[ChatMessage(role="user", content="wait")]
                )
            )
        ]

    events = asyncio.run(scenario())
    assert isinstance(events[0], ReasoningDelta)
    assert isinstance(events[-1], ModelError)
    assert events[-1].code == "claude_cli_timeout"
    assert events[-1].retryable is True
    assert events[-1].failure_kind is ProviderFailureKind.TRANSIENT
