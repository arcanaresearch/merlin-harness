"""Serve the current TUI session without acquiring a second daemon lease."""

from __future__ import annotations

import asyncio
import webbrowser
from pathlib import Path
from typing import Any

from aiohttp import web
from merlin_tui.app import LiveTranscript
from rich.text import Text

from .server import Workspace, create_app


class TuiConsole:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.previous_confirmer = getattr(app.backend, "_confirmer", None)
        self.runner: web.AppRunner | None = None
        self.url = ""
        self.workspace = Workspace(
            app.backend,
            close_backend=False,
            external_busy=lambda: bool(app._stream_task and not app._stream_task.done()),
            stream_handler=self.stream,
        )

    async def stream(self, prompt: str) -> None:
        # Keep the existing TUI stream reader and renderer as the sole foreground driver.
        transcript = self.app.query_one("#transcript", LiveTranscript)
        transcript.write(Text("\n● " + prompt, style="cyan"))
        self.app._remember("user", prompt)
        self.app._activate_conversation()
        await self.app._start_backend(prompt, show_assistant=True)
        task = self.app._stream_task
        if task:
            await task

    async def start(self) -> None:
        static = Path(__file__).parent / "static"
        if not (static / "index.html").is_file():
            raise RuntimeError(
                "Build the web frontend first: pnpm --dir apps/merlin-web/frontend build"
            )
        for entry in self.app._transcript_entries:
            self.workspace.emit(entry.kind, entry.text)
        self.workspace.emit("status", "Connected to the current TUI session")
        if self.app._pending_campaign_review and getattr(self.app, "_web_review", None):
            self.workspace.emit("campaign_review", self.app._web_review)
        runner = web.AppRunner(create_app(self.workspace, static))
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 8765)
            try:
                await site.start()
            except OSError:
                # Another unrelated local service must never be reused or displaced.
                site = web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
            port = site._server.sockets[0].getsockname()[1]
            self.url = f"http://127.0.0.1:{port}/#token={self.workspace.token}"
            self.runner = runner
            if hasattr(self.app.backend, "_confirmer"):
                self.app.backend._confirmer = self.workspace.confirm
        except BaseException:
            await runner.cleanup()
            raise

    async def close(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
            if hasattr(self.app.backend, "_confirmer"):
                self.app.backend._confirmer = self.previous_confirmer


async def open_workspace(app: Any) -> TuiConsole:
    console = getattr(app, "_web_console", None)
    if console is None:
        console = TuiConsole(app)
        await console.start()
        app._web_console = console
    opened = await asyncio.to_thread(webbrowser.open, console.url)
    if not opened:
        # Credential URL is shown only in the operator's terminal, never daemon logs.
        app.query_one("#transcript", LiveTranscript).write(Text("Open browser: " + console.url))
    return console
