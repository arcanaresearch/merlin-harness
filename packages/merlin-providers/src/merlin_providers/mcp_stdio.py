"""Minimal stdio MCP server backed by Merlin's private broker socket."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from collections.abc import Mapping
from typing import Any

_PROTOCOL_VERSION = "2025-06-18"
_MAX_LINE_BYTES = 1_048_576


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--socket", required=True)
    arguments = parser.parse_args()
    for raw in sys.stdin.buffer:
        if len(raw) > _MAX_LINE_BYTES:
            return 2
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return 2
        if not isinstance(message, Mapping):
            return 2
        identifier = message.get("id")
        method = message.get("method")
        if identifier is None:
            continue
        if method == "initialize":
            result: dict[str, object] = {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "merlin", "version": "1"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            broker = _broker_request(arguments.socket, {"action": "list"})
            result = {"tools": broker.get("tools", [])}
        elif method == "tools/call":
            params = _mapping(message.get("params"))
            broker = _broker_request(
                arguments.socket,
                {
                    "action": "call",
                    "name": params.get("name"),
                    "arguments": params.get("arguments", {}),
                },
            )
            error = broker.get("error")
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": str(error or broker.get("content") or ""),
                    }
                ],
                "isError": bool(error) or not bool(broker.get("success", False)),
            }
        else:
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "error": {"code": -32601, "message": "method not found"},
                }
            )
            continue
        _write({"jsonrpc": "2.0", "id": identifier, "result": result})
    return 0


def _broker_request(path: str, request: Mapping[str, object]) -> Mapping[str, Any]:
    encoded = json.dumps(request, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > _MAX_LINE_BYTES:
        return {"error": "broker request exceeds the protocol bound"}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(path)
        client.sendall(encoded)
        stream = client.makefile("rb")
        response = stream.readline(_MAX_LINE_BYTES + 1)
    if not response or len(response) > _MAX_LINE_BYTES:
        return {"error": "broker returned an invalid response"}
    try:
        value = json.loads(response)
    except json.JSONDecodeError:
        return {"error": "broker returned invalid JSON"}
    return _mapping(value)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _write(message: Mapping[str, object]) -> None:
    encoded = json.dumps(message, separators=(",", ":")).encode() + b"\n"
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    raise SystemExit(main())
