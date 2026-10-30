"""Private MCP broker for external agent transports.

The broker deliberately does not execute Merlin tools.  It admits a structured
call into the provider event stream and gives the external agent a short receipt
that tells it to end the turn.  Merlin executes the call after the provider turn
completes, preserving its normal authorization, budget, and evidence boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from merlin_contracts import ToolCall


class DynamicMcpBroker:
    """Expose one request's reviewed tools over a private Unix socket."""

    def __init__(self, tools: Sequence[Mapping[str, object]], *, max_line_bytes: int) -> None:
        self._tools = _mcp_tools(tools)
        self._tool_names = frozenset(str(tool["name"]) for tool in self._tools)
        self._max_line_bytes = max_line_bytes
        self._events: asyncio.Queue[ToolCall] = asyncio.Queue()
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._server: asyncio.AbstractServer | None = None
        self._socket_path: Path | None = None

    @property
    def tool_names(self) -> frozenset[str]:
        return self._tool_names

    async def start(self) -> None:
        if self._server is not None:
            return
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="merlin-mcp-")
        directory = Path(self._temporary_directory.name)
        os.chmod(directory, 0o700)
        self._socket_path = directory / "broker.sock"
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self._socket_path,
            limit=self._max_line_bytes + 1,
        )
        os.chmod(self._socket_path, 0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None
        self._socket_path = None

    def stdio_server(self) -> dict[str, object]:
        if self._socket_path is None:
            raise RuntimeError("dynamic MCP broker is not started")
        return {
            "name": "merlin",
            "command": sys.executable,
            "args": [
                "-m",
                "merlin_providers.mcp_stdio",
                "--socket",
                str(self._socket_path),
            ],
            "env": {},
        }

    def claude_config(self) -> str:
        server = self.stdio_server()
        return json.dumps(
            {
                "mcpServers": {
                    "merlin": {
                        "command": server["command"],
                        "args": server["args"],
                        "env": server["env"],
                    }
                }
            },
            separators=(",", ":"),
        )

    async def next_event(self) -> ToolCall:
        return await self._events.get()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while line := await reader.readline():
                if len(line) > self._max_line_bytes:
                    break
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    break
                if not isinstance(request, Mapping):
                    break
                action = request.get("action")
                if action == "list":
                    response: dict[str, object] = {"tools": self._tools}
                elif action == "call":
                    name = request.get("name")
                    arguments = request.get("arguments")
                    if not isinstance(name, str) or name not in self._tool_names:
                        response = {"error": "tool is not admitted by Merlin"}
                    elif not isinstance(arguments, Mapping):
                        response = {"error": "tool arguments must be an object"}
                    else:
                        call = ToolCall(
                            call_id=f"external-{uuid.uuid4()}",
                            name=name,
                            arguments=dict(arguments),
                        )
                        await self._events.put(call)
                        response = {
                            "content": (
                                "Accepted by Merlin. End this turn now. Merlin will execute the "
                                "authority-checked call and supply its result in the next model "
                                "step."
                            ),
                            "success": True,
                        }
                else:
                    response = {"error": "unsupported broker action"}
                encoded = json.dumps(response, separators=(",", ":")).encode() + b"\n"
                if len(encoded) > self._max_line_bytes:
                    break
                writer.write(encoded)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


def _mcp_tools(tools: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for tool in tools:
        function = tool.get("function")
        if tool.get("type") != "function" or not isinstance(function, Mapping):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name or name in seen:
            continue
        description = function.get("description")
        parameters = function.get("parameters")
        result.append(
            {
                "name": name,
                "description": description if isinstance(description, str) else "",
                "inputSchema": dict(parameters) if isinstance(parameters, Mapping) else {},
            }
        )
        seen.add(name)
    return result
