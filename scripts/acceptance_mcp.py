"""Real provider + local MCP + Textual acceptance smoke."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from merlin_tui.app import MerlinApp
from merlin_tui.bootstrap import build_app

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "packages/merlin-mcp/tests/tiny_server.py"


async def main() -> None:
    app = build_app()
    assert isinstance(app, MerlinApp)
    backend = app.backend
    server_name = "acceptance-smoke"
    try:
        add = f"/mcp add {server_name} {sys.executable} {SERVER}"
        async with app.run_test() as pilot:
            composer = app.query_one("#composer")
            composer.text = add
            await app.action_send()
            assert app._stream_task is not None
            await app._stream_task
            await pilot.pause()
            composer.text = (
                f"You must call mcp__{server_name}__echo exactly once with value "
                "MERLIN_MCP_OK, "
                "then report its returned value."
            )
            await app.action_send()
            assert app._stream_task is not None
            for _ in range(100):
                await pilot.pause(0.05)
                allow = app.screen.query("#tool-confirm-allow")
                if allow:
                    allow.first().press()
                    break
            await app._stream_task
            await pilot.pause()
            transcript = "".join(line.text for line in app.query_one("#transcript").lines)
            assert f"mcp__{server_name}__echo" in transcript, transcript
            assert "MERLIN_MCP_OK" in transcript, transcript
            before = await backend.list_mcp_servers()
            assert before and before[0].recent_calls
            restarted = await backend._mcp.restart_server(server_name)
            assert restarted["status"] == "running"
            result = await backend._mcp.invoke(server_name, "echo", {"value": "AFTER_RESTART"})
            assert "AFTER_RESTART" in result.content
            print("MCP_ACCEPTANCE_OK")
            print(f"tools={','.join(before[0].tools)}")
            print(f"recent={before[0].recent_calls[-1]}")
            print("restart=running; invocation=AFTER_RESTART")
            await backend._mcp.remove_server(server_name)
    finally:
        await backend.close()


if __name__ == "__main__":
    asyncio.run(main())
