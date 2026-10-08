#!/usr/bin/env python3
"""Run a deterministic authenticated Streamable HTTP acceptance against an in-process server."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx2
from mcp.server import MCPServer
from merlin_mcp import CredentialHandle, McpManager, RemoteMcpServerConfig


class BearerAuthApp:
    def __init__(self, app: object, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: object, receive: object, send: object) -> None:
        if scope["type"] == "http":  # type: ignore[index]
            headers = dict(scope["headers"])  # type: ignore[index]
            if headers.get(b"authorization") != f"Bearer {self.token}".encode():
                await send({"type": "http.response.start", "status": 401, "headers": []})  # type: ignore[operator]
                await send({"type": "http.response.body", "body": b"unauthorized"})  # type: ignore[operator]
                return
        await self.app(scope, receive, send)  # type: ignore[operator]


async def main() -> None:
    token = "acceptance-token"
    server = MCPServer("remote-acceptance")

    @server.tool()
    def echo(value: str) -> dict[str, str]:
        return {"value": value}

    mcp_app = server.streamable_http_app(stateless_http=True, host="acceptance.test")
    authenticated_app = BearerAuthApp(mcp_app, token)

    @asynccontextmanager
    async def client_factory(
        config: RemoteMcpServerConfig, headers: Mapping[str, str]
    ) -> AsyncIterator[httpx2.AsyncClient]:
        del config
        async with mcp_app.router.lifespan_context(mcp_app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=authenticated_app),
                base_url="https://acceptance.test",
                headers=headers,
            ) as client:
                yield client

    manager = McpManager(
        credential_resolver=lambda handle: token,
        remote_http_client_factory=client_factory,
    )
    await manager.add_server(
        RemoteMcpServerConfig(
            "acceptance",
            "https://acceptance.test/mcp",
            CredentialHandle("acceptance-token-handle"),
            reconnect_attempts=0,
        ),
        start=True,
    )
    quarantined = manager.list_servers()[0]
    assert quarantined.quarantined and quarantined.inventory_digest
    manager.admit_remote_inventory("acceptance", quarantined.inventory_digest)
    result = await manager.invoke("acceptance", "echo", {"value": "authenticated"})
    assert result.structured_content == {"value": "authenticated"}
    print(
        json.dumps(
            {
                "authenticated": True,
                "inventory_admitted": True,
                "result": result.structured_content,
            },
            sort_keys=True,
        )
    )
    await manager.close()


if __name__ == "__main__":
    asyncio.run(main())
