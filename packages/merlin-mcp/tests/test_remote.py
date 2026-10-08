import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp.server import MCPServer
from merlin_mcp import (
    CredentialHandle,
    McpManager,
    McpServerStateError,
    RemoteMcpServerConfig,
)


class AuthenticatedApp:
    def __init__(self, app: object, credential: str) -> None:
        self.app = app
        self.credential = credential

    async def __call__(self, scope: object, receive: object, send: object) -> None:
        if scope["type"] == "http":  # type: ignore[index]
            headers = dict(scope["headers"])  # type: ignore[index]
            if headers.get(b"authorization") != f"Bearer {self.credential}".encode():
                await send({"type": "http.response.start", "status": 401, "headers": []})  # type: ignore[operator]
                await send({"type": "http.response.body", "body": b"unauthorized"})  # type: ignore[operator]
                return
        await self.app(scope, receive, send)  # type: ignore[operator]


def build_server() -> MCPServer:
    server = MCPServer("remote\x00identity")

    @server.tool()
    def echo(value: str) -> dict[str, str]:
        """Echo remotely."""
        return {"value": value}

    @server.tool()
    async def delay(seconds: float) -> str:
        """Wait so cancellation can be observed."""
        await asyncio.sleep(seconds)
        return "finished"

    return server


def config(**values: object) -> RemoteMcpServerConfig:
    arguments = {
        "name": "remote",
        "url": "https://mcp.test/mcp",
        "credential_handle": CredentialHandle("vault://mcp/token"),
        "reconnect_attempts": 0,
    }
    arguments.update(values)
    return RemoteMcpServerConfig(**arguments)  # type: ignore[arg-type]


def factory_for(server: MCPServer, token: str):
    mcp_app = server.streamable_http_app(stateless_http=True, host="mcp.test")
    app = AuthenticatedApp(mcp_app, token)

    @asynccontextmanager
    async def factory(
        remote: RemoteMcpServerConfig, headers: Mapping[str, str]
    ) -> AsyncIterator[httpx2.AsyncClient]:
        del remote
        async with mcp_app.router.lifespan_context(mcp_app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                headers=headers,
                base_url="https://mcp.test",
            ) as client:
                yield client

    return factory


async def test_authenticated_remote_is_quarantined_until_inventory_admission() -> None:
    server = build_server()
    manager = McpManager(
        credential_resolver=lambda handle: "top-secret",
        remote_http_client_factory=factory_for(server, "top-secret"),
    )
    await manager.add_server(config(), start=True)
    info = manager.list_servers()[0]
    assert info.running and info.quarantined
    assert "\x00" not in (info.server_identity or "")
    with pytest.raises(McpServerStateError, match="quarantined"):
        await manager.invoke("remote", "echo", {"value": "hello"})
    assert info.inventory_digest is not None
    manager.admit_remote_inventory("remote", info.inventory_digest)
    result = await manager.invoke("remote", "echo", {"value": "hello"})
    assert result.structured_content == {"value": "hello"}
    await manager.close()


async def test_inventory_change_forces_readmission() -> None:
    server = build_server()
    manager = McpManager(
        credential_resolver=lambda handle: "secret",
        remote_http_client_factory=factory_for(server, "secret"),
    )
    await manager.add_server(config(), start=True)
    first = manager.list_servers()[0]
    assert first.inventory_digest
    manager.admit_remote_inventory("remote", first.inventory_digest)

    @server.tool()
    def added() -> str:
        return "new"

    assert {tool.name for tool in await manager.list_tools("remote")} == {
        "echo",
        "delay",
        "added",
    }
    changed = manager.list_servers()[0]
    assert changed.quarantined and changed.inventory_digest != first.inventory_digest
    with pytest.raises(McpServerStateError, match="quarantined"):
        await manager.invoke("remote", "added")
    await manager.close()


async def test_remote_transport_retries_once_then_connects() -> None:
    server = build_server()
    healthy = factory_for(server, "secret")
    attempts = 0

    @asynccontextmanager
    async def transient_factory(remote, headers):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx2.ConnectError("transient connection failure")
        async with healthy(remote, headers) as client:
            yield client

    manager = McpManager(
        credential_resolver=lambda handle: "secret",
        remote_http_client_factory=transient_factory,
    )
    await manager.add_server(config(reconnect_attempts=1, reconnect_delay=0), start=True)
    info = manager.list_servers()[0]
    assert attempts == 2
    assert info.running and info.inventory_digest
    await manager.close()


async def test_cancelled_remote_call_closes_transport_and_reconnects_same_inventory() -> None:
    @asynccontextmanager
    async def fresh_factory(remote, headers):
        async with factory_for(build_server(), "secret")(remote, headers) as client:
            yield client

    manager = McpManager(
        credential_resolver=lambda handle: "secret",
        remote_http_client_factory=fresh_factory,
    )
    await manager.add_server(config(), start=True)
    first = manager.list_servers()[0]
    assert first.inventory_digest
    manager.admit_remote_inventory("remote", first.inventory_digest)

    invocation = asyncio.create_task(manager.invoke("remote", "delay", {"seconds": 5}))
    await asyncio.sleep(0.05)
    invocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await invocation

    result = await manager.invoke("remote", "echo", {"value": "after-cancel"})
    assert result.structured_content == {"value": "after-cancel"}
    restarted = manager.list_servers()[0]
    assert restarted.running and restarted.inventory_digest == first.inventory_digest
    await manager.close()


async def test_bad_auth_error_does_not_expose_secret_or_handle() -> None:
    server = build_server()
    manager = McpManager(
        credential_resolver=lambda handle: "wrong-secret",
        remote_http_client_factory=factory_for(server, "expected-secret"),
    )
    with pytest.raises(McpServerStateError) as captured:
        await manager.add_server(config(), start=True)
    message = str(captured.value)
    assert "wrong-secret" not in message
    assert "vault://" not in message


def test_remote_configuration_requires_https_and_bounded_reconnect() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        config(url="http://mcp.test/mcp")
    with pytest.raises(ValueError, match="reconnect"):
        config(reconnect_attempts=4)
