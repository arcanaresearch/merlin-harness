import asyncio
from unittest.mock import AsyncMock

from merlin_tui.app import Composer, MerlinApp
from merlin_tui.backend import DemoBackend, StreamEvent


async def test_enter_during_recon_uses_one_reader_and_keeps_output_and_followup():
    started = asyncio.Event()
    finish = asyncio.Event()
    messages = asyncio.Queue()

    class ReconBackend(DemoBackend):
        def __init__(self):
            super().__init__()
            self.prompts = []
            self.followups = []
            self.interrupt = AsyncMock()

        async def stream(self, prompt):
            self.prompts.append(prompt)
            yield StreamEvent("content", "Mapping the current target.")
            started.set()
            text = await messages.get()
            yield StreamEvent("user", text)
            yield StreamEvent("content", "I am checking authentication; recon will continue.")
            await finish.wait()

        async def steer(self, text):
            self.followups.append(text)
            await messages.put(text)
            return True

    backend = ReconBackend()
    app = MerlinApp(backend)
    async with app.run_test(size=(100, 30)) as pilot:
        app._activate_conversation()
        await app._start_backend("Recon https://example.test", show_assistant=True)
        await started.wait()
        await pilot.pause()
        reader = app._stream_task
        # A stale profile prompt cannot intercept conversational follow-ups.
        app._pending_choice = (("Recommended [default]", "recommended"),)
        app._pending_choice_kind = "profile"
        composer = app.query_one("#composer", Composer)
        composer.text = "What are you checking?"
        composer.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert backend.followups == ["What are you checking?"]
        assert backend.prompts == ["Recon https://example.test"]
        assert app._stream_task is reader and not reader.done()
        assert composer.text == ""
        backend.interrupt.assert_not_awaited()
        finish.set()
        await reader
        await pilot.pause()
        lines = "\n".join(line.text for line in app.query_one("#transcript").lines)
        assert "Mapping the current target." in lines
        assert lines.count("What are you checking?") == 1
        assert "I am checking authentication; recon will continue." in lines


async def test_failed_steering_keeps_draft_and_existing_recon_reader():
    started, finish = asyncio.Event(), asyncio.Event()

    class ReconBackend(DemoBackend):
        async def stream(self, prompt):
            started.set()
            yield StreamEvent("thinking", "Reconnaissance")
            await finish.wait()

        async def steer(self, text):
            raise ConnectionError("connection lost")

    app = MerlinApp(ReconBackend())
    async with app.run_test(size=(100, 30)) as pilot:
        app._activate_conversation()
        await app._start_backend("recon", show_assistant=True)
        await started.wait()
        reader = app._stream_task
        composer = app.query_one("#composer", Composer)
        composer.text = "Please check authentication"
        await app.action_send()
        assert composer.text == "Please check authentication"
        assert app._stream_task is reader and not reader.done()
        finish.set()
        await reader
        await pilot.pause()
