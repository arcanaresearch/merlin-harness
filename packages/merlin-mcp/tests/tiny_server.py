import asyncio

from mcp.server import MCPServer

server = MCPServer("merlin-test")


@server.tool()
def echo(value: str) -> dict[str, str]:
    """Echo one value."""
    return {"value": value}


@server.tool()
async def delay(seconds: float) -> str:
    """Wait for a requested duration."""
    await asyncio.sleep(seconds)
    return "done"


if __name__ == "__main__":
    server.run("stdio")
