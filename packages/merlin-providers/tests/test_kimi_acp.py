from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from merlin_contracts import (
    ChatMessage,
    ChatRequest,
    ModelCompleted,
    ModelError,
    ProviderFailureKind,
    ReasoningDelta,
    TextDelta,
)

from merlin_providers import KimiAcpConfig, KimiAcpProvider


def test_kimi_acp_explicitly_rejects_categorical_effort() -> None:
    with pytest.raises(ValueError, match="does not expose a categorical reasoning-effort"):
        KimiAcpConfig(reasoning_effort="high")


def _write_server(path: Path, body: str) -> tuple[str, ...]:
    path.write_text(body)
    return (sys.executable, "-u", str(path))


def test_kimi_acp_streams_messages_and_reuses_session(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "kimi.py",
        """
import json, sys
prompts = 0
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":{"protocolVersion":1,"agentCapabilities":{}}}), flush=True)
    elif method == "session/new":
        print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":{"sessionId":"session-1"}}), flush=True)
    elif method in {"session/set_mode", "session/set_config_option"}:
        print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":{}}), flush=True)
    elif method == "session/prompt":
        prompts += 1
        print(json.dumps({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"session-1","update":{"sessionUpdate":"agent_thought_chunk","content":{"type":"text","text":"think "}}}}), flush=True)
        print(json.dumps({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"session-1","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":f"answer-{prompts}"}}}}), flush=True)
        print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":{"stopReason":"end_turn"}}), flush=True)
""",
    )

    async def scenario() -> tuple[list[object], list[object]]:
        provider = KimiAcpProvider(
            KimiAcpConfig(command=command, model="kimi-test", state_directory=tmp_path)
        )
        first = [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="kimi-test",
                    budget_id="assignment-1",
                    messages=[ChatMessage(role="user", content="first")],
                )
            )
        ]
        second = [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="kimi-test",
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


def test_kimi_acp_denies_reverse_authority_request(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "kimi.py",
        """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":{"protocolVersion":1,"agentCapabilities":{}}}), flush=True)
    elif message.get("method") == "session/new":
        print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":{"sessionId":"session-1"}}), flush=True)
    elif message.get("method") in {"session/set_mode", "session/set_config_option"}:
        print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":{}}), flush=True)
    elif message.get("method") == "session/prompt":
        print(json.dumps({"jsonrpc":"2.0","id":99,"method":"session/request_permission","params":{"sessionId":"session-1"}}), flush=True)
""",
    )

    async def scenario() -> list[object]:
        provider = KimiAcpProvider(
            KimiAcpConfig(command=command, model="kimi-test", state_directory=tmp_path)
        )
        events = [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="kimi-test",
                    messages=[ChatMessage(role="user", content="hello")],
                )
            )
        ]
        await provider.close()
        return events

    events = asyncio.run(scenario())
    assert isinstance(events[-1], ModelError)
    assert events[-1].code == "kimi_acp_forbidden_authority"
    assert events[-1].failure_kind is ProviderFailureKind.PERMISSION


def test_kimi_acp_rejects_tools_before_launch(tmp_path: Path) -> None:
    provider = KimiAcpProvider(
        KimiAcpConfig(command=("does-not-exist",), model="kimi-test", state_directory=tmp_path)
    )

    async def scenario() -> list[object]:
        return [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="kimi-test",
                    messages=[ChatMessage(role="user", content="hello")],
                    tools=[{"type": "function", "function": {"name": "tool"}}],
                )
            )
        ]

    events = asyncio.run(scenario())
    assert isinstance(events[-1], ModelError)
    assert events[-1].code == "kimi_acp_tools_unsupported"
    assert events[-1].failure_kind is ProviderFailureKind.CONFIGURATION


def test_kimi_acp_preserves_structured_quota_error(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "kimi.py",
        """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":{"protocolVersion":1,"agentCapabilities":{}}}), flush=True)
    elif method == "session/new":
        print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":{"sessionId":"session-1"}}), flush=True)
    elif method in {"session/set_mode", "session/set_config_option"}:
        print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":{}}), flush=True)
    elif method == "session/prompt":
        print(json.dumps({"jsonrpc":"2.0","id":message["id"],"error":{"code":"usage_limit_reached","message":"quota exhausted"}}), flush=True)
""",
    )

    async def scenario() -> list[object]:
        provider = KimiAcpProvider(
            KimiAcpConfig(command=command, model="kimi-test", state_directory=tmp_path)
        )
        events = [
            event
            async for event in provider.stream(
                ChatRequest(
                    model="kimi-test",
                    messages=[ChatMessage(role="user", content="hello")],
                )
            )
        ]
        await provider.close()
        return events

    events = asyncio.run(scenario())
    assert events == [
        ModelError(
            message="quota exhausted",
            code="provider_quota_exhausted",
            failure_kind=ProviderFailureKind.QUOTA,
        )
    ]


def test_kimi_acp_uses_shorter_idle_timeout_after_visible_progress(tmp_path: Path) -> None:
    command = _write_server(
        tmp_path / "kimi.py",
        """
import json, sys, time
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":{"protocolVersion":1,"agentCapabilities":{}}}), flush=True)
    elif method == "session/new":
        print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":{"sessionId":"session-1"}}), flush=True)
    elif method in {"session/set_mode", "session/set_config_option"}:
        print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":{}}), flush=True)
    elif method == "session/prompt":
        print(json.dumps({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"session-1","update":{"sessionUpdate":"agent_thought_chunk","content":{"text":"working"}}}}), flush=True)
        time.sleep(1)
""",
    )

    async def scenario() -> list[object]:
        provider = KimiAcpProvider(
            KimiAcpConfig(
                command=command,
                idle_timeout_seconds=1,
                partial_idle_timeout_seconds=0.05,
                turn_timeout_seconds=2,
            )
        )
        events = [
            event
            async for event in provider.stream(
                ChatRequest(model="kimi-test", messages=[ChatMessage(role="user", content="wait")])
            )
        ]
        await provider.close()
        return events

    events = asyncio.run(scenario())
    assert isinstance(events[0], ReasoningDelta)
    assert isinstance(events[-1], ModelError)
    assert events[-1].code == "kimi_acp_timeout"
    assert events[-1].retryable is True
    assert events[-1].failure_kind is ProviderFailureKind.TRANSIENT


def test_kimi_acp_restores_owned_session_after_provider_restart(tmp_path: Path) -> None:
    methods = tmp_path / "methods"
    command = _write_server(
        tmp_path / "kimi.py",
        f"""
import json, pathlib, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    pathlib.Path({str(methods)!r}).open("a").write(str(method) + "\\n")
    if method == "initialize":
        print(json.dumps({{"jsonrpc":"2.0","id":message["id"],"result":{{"protocolVersion":1,"agentCapabilities":{{"loadSession":True}}}}}}), flush=True)
    elif method == "session/new":
        print(json.dumps({{"jsonrpc":"2.0","id":message["id"],"result":{{"sessionId":"durable"}}}}), flush=True)
    elif method == "session/load":
        print(json.dumps({{"jsonrpc":"2.0","id":message["id"],"result":{{}}}}), flush=True)
    elif method in {{"session/set_mode", "session/set_config_option"}}:
        print(json.dumps({{"jsonrpc":"2.0","id":message["id"],"result":{{}}}}), flush=True)
    elif method == "session/prompt":
        print(json.dumps({{"jsonrpc":"2.0","id":message["id"],"result":{{"stopReason":"end_turn"}}}}), flush=True)
""",
    )

    async def run_once(messages: list[ChatMessage]) -> None:
        provider = KimiAcpProvider(
            KimiAcpConfig(command=command, model="kimi-test", state_directory=tmp_path)
        )
        _ = [
            event
            async for event in provider.stream(
                ChatRequest(model="kimi-test", budget_id="assignment-1", messages=messages)
            )
        ]
        await provider.close()

    asyncio.run(run_once([ChatMessage(role="user", content="first")]))
    asyncio.run(
        run_once(
            [
                ChatMessage(role="user", content="first"),
                ChatMessage(role="assistant", content="answer"),
                ChatMessage(role="user", content="second"),
            ]
        )
    )
    observed = methods.read_text().splitlines()
    assert observed.count("session/new") == 1
    assert observed.count("session/load") == 1
