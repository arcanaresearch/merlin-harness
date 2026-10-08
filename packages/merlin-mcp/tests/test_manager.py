import asyncio
import sys
from pathlib import Path

import pytest
from merlin_mcp import (
    McpManager,
    McpServerConfig,
    McpServerNotFoundError,
    McpTimeoutError,
)

SERVER = Path(__file__).with_name("tiny_server.py")


def config(**overrides: object) -> McpServerConfig:
    values = {
        "name": "tiny",
        "command": sys.executable,
        "args": (str(SERVER),),
        "startup_timeout": 5.0,
        "invocation_timeout": 2.0,
        "output_timeout": 2.0,
        "shutdown_timeout": 2.0,
    }
    values.update(overrides)
    return McpServerConfig(**values)  # type: ignore[arg-type]


async def test_real_stdio_server_lifecycle_and_invocation() -> None:
    async with McpManager() as manager:
        await manager.add_server(config(), start=True)

        tools = await manager.list_tools("tiny")
        assert [tool.name for tool in tools] == ["echo", "delay"]
        assert tools[0].server_name == "tiny"

        result = await manager.invoke("tiny", "echo", {"value": "hello"})
        assert result.is_error is False
        assert result.structured_content == {"value": "hello"}

        status = await manager.check_server("tiny")
        assert status.running is True
        await manager.restart_server("tiny")
        await manager.stop_server("tiny")
        assert manager.list_servers()[0].running is False


async def test_invocation_timeout_does_not_leak_server() -> None:
    manager = McpManager()
    await manager.add_server(config(invocation_timeout=0.05), start=True)
    with pytest.raises(McpTimeoutError):
        await manager.invoke("tiny", "delay", {"seconds": 1})
    result = await manager.invoke("tiny", "echo", {"value": "reconnected"})
    assert result.structured_content == {"value": "reconnected"}
    await manager.close()
    assert manager.list_servers()[0].running is False


async def test_cancelled_invocation_closes_transport_before_reconnect() -> None:
    manager = McpManager()
    await manager.add_server(config(), start=True)
    invocation = asyncio.create_task(manager.invoke("tiny", "delay", {"seconds": 1}))
    await asyncio.sleep(0.05)
    invocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await invocation
    result = await manager.invoke("tiny", "echo", {"value": "clean"})
    assert result.structured_content == {"value": "clean"}
    await manager.close()


async def test_unknown_and_invalid_configuration() -> None:
    manager = McpManager()
    with pytest.raises(McpServerNotFoundError):
        await manager.start("missing")
    with pytest.raises(ValueError):
        config(startup_timeout=0)


async def test_remove_stops_and_forgets_server() -> None:
    manager = McpManager()
    manager.add(config())
    await manager.start("tiny")
    await manager.remove_server("tiny")
    assert manager.list_servers() == ()
