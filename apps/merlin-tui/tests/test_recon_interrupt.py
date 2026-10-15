import asyncio
from unittest.mock import AsyncMock

import pytest
from merlin_tui.app import Composer, MerlinApp
from merlin_tui.backend import DemoBackend, StreamEvent


@pytest.mark.parametrize("action", ["escape", "/interrupt"])
async def test_interrupt_reaches_daemon_without_ui_stream_during_pending_profile(action):
    backend = DemoBackend()
    backend.interrupt = AsyncMock()
    app = MerlinApp(backend)
    async with app.run_test(size=(100, 30)) as pilot:
        app._pending_choice = (("Recommended [default]", "recommended"),)
        app._pending_choice_default = "recommended"
        app._pending_choice_kind = "profile"
        assert app._stream_task is None
        composer = app.query_one("#composer", Composer)
        composer.focus()
        if action == "escape":
            composer.text = "keep this draft"
            await pilot.press("escape")
            assert composer.text == "keep this draft"
        else:
            composer.text = "/interrupt"
            await pilot.press("enter")
        await pilot.pause()
        backend.interrupt.assert_awaited_once()
        assert composer.has_focus


async def test_recon_reader_remains_open_until_backend_acknowledges_interrupt():
    started = asyncio.Event()
    requested = asyncio.Event()
    acknowledged = asyncio.Event()

    class ReconBackend(DemoBackend):
        async def stream(self, prompt):
            yield StreamEvent("tool", "knowledge_search started")
            started.set()
            await asyncio.Event().wait()

        async def interrupt(self):
            requested.set()
            await acknowledged.wait()

    app = MerlinApp(ReconBackend())
    async with app.run_test(size=(100, 30)) as pilot:
        await app._start_backend("recon", show_assistant=True)
        await started.wait()
        stream = app._stream_task
        interruption = asyncio.create_task(app.action_interrupt())
        try:
            await requested.wait()
            await pilot.pause()
            assert not stream.done()
            assert app._work_label == "Interrupting"
        finally:
            acknowledged.set()
            await interruption
        await pilot.pause()
        assert stream.done()
        assert app._stream_task is None


async def test_interrupt_connection_error_is_visible_without_crashing_ui():
    backend = DemoBackend()
    backend.interrupt = AsyncMock(side_effect=ConnectionError("daemon disconnected"))
    app = MerlinApp(backend)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press("escape")
        await pilot.pause()
        backend.interrupt.assert_awaited_once()
        lines = [line.text for line in app.query_one("#transcript").lines]
        assert any("Interrupt failed: daemon disconnected" in line for line in lines)
