#!/usr/bin/env python3
"""Authenticated remote MCP service acceptance, including inventory re-admission."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx2
from mcp.server import MCPServer
from merlin_config import ConfigPaths, CredentialStore
from merlin_mcp import McpManager, RemoteMcpServerConfig
from merlin_tools import ToolBroker
from merlin_tui.configuration import McpConfiguration
from merlin_tui.mcp_service import McpService


class BearerAuthApp:
    def __init__(self, app: object, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: object, receive: object, send: object) -> None:
        if scope["type"] == "http":  # type: ignore[index]
            headers = dict(scope["headers"])  # type: ignore[index]
            expected = f"Bearer {self.token}".encode()
            if headers.get(b"authorization") != expected:
                await send({"type": "http.response.start", "status": 401, "headers": []})  # type: ignore[operator]
                await send({"type": "http.response.body", "body": b"unauthorized"})  # type: ignore[operator]
                return
        await self.app(scope, receive, send)  # type: ignore[operator]


async def main() -> None:
    token = "acceptance-secret-never-rendered"
    server = MCPServer("merlin-remote-acceptance")

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
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=authenticated_app),
            base_url="https://acceptance.test",
            headers=headers,
        ) as client:
            yield client

    async with mcp_app.router.lifespan_context(mcp_app):
        await run_acceptance(server, token, client_factory)


async def run_acceptance(
    server: MCPServer,
    token: str,
    client_factory: object,
) -> None:
    with TemporaryDirectory(prefix="merlin-remote-acceptance-") as directory:
        paths = ConfigPaths(Path(directory))
        manager = McpManager(remote_http_client_factory=client_factory)  # type: ignore[arg-type]
        service = McpService(
            manager,
            ToolBroker(),
            McpConfiguration(paths),
            CredentialStore(paths),
        )
        try:
            first = await service.add_remote_with_secret(
                "acceptance",
                "https://acceptance.test/mcp",
                "acceptance-handle",
                token,
            )
            assert first["admission"] == "quarantined"
            first_digest = str(first["inventory_digest"])
            await service.admit_server("acceptance", first_digest)
            result = await service.invoke("acceptance", "echo", {"value": "authenticated"})
            assert "authenticated" in result.content

            @server.tool()
            def health() -> dict[str, bool]:
                return {"healthy": True}

            restarted = await service.restart_server("acceptance")
            assert restarted["health"] == "healthy"
            assert restarted["admission"] == "quarantined"
            assert restarted["inventory_digest"] != first_digest
            await service.admit_server("acceptance", str(restarted["inventory_digest"]))
            admitted = (await service.list_servers())[0]
            assert admitted["admission"] == "admitted"
            assert {"echo", "health"}.issubset(set(admitted["tools"]))
            print(
                json.dumps(
                    {
                        "authenticated": True,
                        "reconnected": True,
                        "inventory_readmitted": True,
                    },
                    sort_keys=True,
                )
            )
        finally:
            await service.close()


if __name__ == "__main__":
    asyncio.run(main())
