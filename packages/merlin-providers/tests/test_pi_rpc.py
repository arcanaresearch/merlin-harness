from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from merlin_contracts import (
    ChatMessage,
    ChatRequest,
    ModelCompleted,
    ModelError,
    ProviderFailureKind,
    ReasoningDelta,
    TextDelta,
    ToolCall,
)

from merlin_providers import PiRpcConfig, PiRpcProvider


def _write_server(path: Path, body: str) -> tuple[str, ...]:
    path.write_text(body)
    return (sys.executable, "-u", str(path))


def test_pi_rpc_streams_messages_and_reuses_session(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "pi.py",
        """
import json, sys
prompts = 0
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "prompt":
        prompts += 1
        print(json.dumps({"id":message["id"],"type":"response","command":"prompt","success":True,"data":{"disposition":"started"}}), flush=True)
        print(json.dumps({"type":"message_update","assistantMessageEvent":{"type":"thinking_delta","delta":"think "}}), flush=True)
        print(json.dumps({"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":f"answer-{prompts}"}}), flush=True)
        print(json.dumps({"type":"agent_settled","aborted":False}), flush=True)
""",
    )

    async def scenario() -> tuple[list[object], list[object]]:
        provider = PiRpcProvider(
            PiRpcConfig(command=command, model="pi-test", state_directory=tmp_path)
        )
        first = [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="pi-test",
                    budget_id="assignment-1",
                    messages=[ChatMessage(role="user", content="first")],
                )
            )
        ]
        second = [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="pi-test",
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
        return first, second

    first, second = asyncio.run(scenario())
    assert any(isinstance(event, ReasoningDelta) and event.text == "think " for event in first)
    assert any(isinstance(event, TextDelta) and event.text == "answer-1" for event in first)
    assert isinstance(first[-1], ModelCompleted)
    assert any(isinstance(event, TextDelta) and event.text == "answer-2" for event in second)


def test_pi_rpc_admits_only_reviewed_mcp_tool_calls(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "pi.py",
        """
import json, os, pathlib, subprocess, sys, time
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") != "prompt":
        continue
    print(json.dumps({"id":message["id"],"type":"response","command":"prompt","success":True,"data":{"disposition":"started"}}), flush=True)
    config = json.loads((pathlib.Path(os.environ["PI_CODING_AGENT_DIR"]) / "mcp.json").read_text())
    server = config["mcpServers"]["merlin"]
    print(json.dumps({"type":"tool_execution_start","toolCallId":"call-1","toolName":"mcp__merlin__target_request","args":{"message":"hello"}}), flush=True)
    child = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    child.stdin.write(json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"target_request","arguments":{"message":"hello"}}}) + "\\n")
    child.stdin.flush()
    json.loads(child.stdout.readline())
    child.stdin.close()
    child.wait()
    time.sleep(0.1)
    print(json.dumps({"type":"agent_settled","aborted":False}), flush=True)
""",
    )

    async def scenario() -> list[object]:
        provider = PiRpcProvider(
            PiRpcConfig(command=command, model="pi-test", state_directory=tmp_path)
        )

        async def unused_handler(_call: ToolCall) -> str:
            raise AssertionError("the transport must not execute Merlin tools")

        events = [
            event
            async for event in provider.stream_with_tools(
                ChatRequest(
                    model="pi-test",
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
        await provider.close()
        return events

    events = asyncio.run(scenario())
    calls = [event for event in events if isinstance(event, ToolCall)]
    assert len(calls) == 1
    assert calls[0].name == "target_request"
    assert calls[0].arguments == {"message": "hello"}
    assert isinstance(events[-1], ModelCompleted)


def test_pi_rpc_fails_closed_on_unreviewed_tool(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "pi.py",
        """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "prompt":
        print(json.dumps({"id":message["id"],"type":"response","command":"prompt","success":True,"data":{"disposition":"started"}}), flush=True)
        print(json.dumps({"type":"tool_execution_start","toolCallId":"call-1","toolName":"bash","args":{}}), flush=True)
""",
    )

    async def scenario() -> list[object]:
        provider = PiRpcProvider(
            PiRpcConfig(command=command, model="pi-test", state_directory=tmp_path)
        )
        events = [
            event
            async for event in provider.stream(
                ChatRequest(model="pi-test", messages=[ChatMessage(role="user", content="hello")])
            )
        ]
        await provider.close()
        return events

    events = asyncio.run(scenario())
    assert isinstance(events[-1], ModelError)
    assert events[-1].code == "pi_rpc_forbidden_tool"
    assert events[-1].failure_kind is ProviderFailureKind.PERMISSION


def test_pi_rpc_uses_isolated_agent_directory_and_no_local_tools(tmp_path: Path) -> None:
    source = tmp_path / "source-agent"
    source.mkdir()
    (source / "auth.json").write_text("{}")
    observation = tmp_path / "observation.json"
    command = _write_server(
        tmp_path / "pi.py",
        f"""
import json, os, pathlib, sys
pathlib.Path({str(observation)!r}).write_text(json.dumps({{"argv":sys.argv[1:],"agent_dir":os.environ["PI_CODING_AGENT_DIR"],"telemetry":os.environ["PI_TELEMETRY"]}}))
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "prompt":
        print(json.dumps({{"id":message["id"],"type":"response","command":"prompt","success":True,"data":{{"disposition":"started"}}}}), flush=True)
        print(json.dumps({{"type":"agent_settled","aborted":False}}), flush=True)
""",
    )

    async def scenario() -> None:
        provider = PiRpcProvider(
            PiRpcConfig(
                command=command,
                model="pi-test",
                state_directory=tmp_path,
                source_agent_directory=source,
            )
        )
        _ = [
            event
            async for event in provider.stream(
                ChatRequest(model="pi-test", messages=[ChatMessage(role="user", content="hello")])
            )
        ]
        await provider.close()

    asyncio.run(scenario())
    observed = json.loads(observation.read_text())
    assert observed["agent_dir"] != str(source)
    assert observed["telemetry"] == "0"
    assert "--no-tools" in observed["argv"]
    assert "--no-context-files" in observed["argv"]
    assert "--no-extensions" in observed["argv"]
    assert (Path(observed["agent_dir"]) / "auth.json").is_symlink()
