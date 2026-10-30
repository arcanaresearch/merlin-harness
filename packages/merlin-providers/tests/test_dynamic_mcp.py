from __future__ import annotations

import asyncio
import json

from merlin_contracts import ToolCall

from merlin_providers.dynamic_mcp import DynamicMcpBroker


def test_dynamic_mcp_admits_call_without_executing_tool() -> None:
    async def scenario() -> ToolCall:
        broker = DynamicMcpBroker(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "target_request",
                        "description": "Send one reviewed target request",
                        "parameters": {
                            "type": "object",
                            "properties": {"message": {"type": "string"}},
                            "required": ["message"],
                        },
                    },
                }
            ],
            max_line_bytes=1_048_576,
        )
        await broker.start()
        server = broker.stdio_server()
        process = await asyncio.create_subprocess_exec(
            str(server["command"]),
            *(str(value) for value in server["args"]),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        assert process.stdin is not None and process.stdout is not None

        async def rpc(identifier: int, method: str, params: object) -> dict[str, object]:
            process.stdin.write(
                (
                    json.dumps(
                        {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
                    )
                    + "\n"
                ).encode()
            )
            await process.stdin.drain()
            return json.loads(await process.stdout.readline())

        initialized = await rpc(1, "initialize", {})
        listed = await rpc(2, "tools/list", {})
        called = await rpc(
            3,
            "tools/call",
            {"name": "target_request", "arguments": {"message": "hello"}},
        )
        call = await asyncio.wait_for(broker.next_event(), 1)
        process.stdin.close()
        await process.wait()
        await broker.close()

        assert initialized["result"]["serverInfo"]["name"] == "merlin"  # type: ignore[index]
        assert listed["result"]["tools"][0]["name"] == "target_request"  # type: ignore[index]
        assert called["result"]["isError"] is False  # type: ignore[index]
        return call

    call = asyncio.run(scenario())
    assert call.name == "target_request"
    assert call.arguments == {"message": "hello"}
