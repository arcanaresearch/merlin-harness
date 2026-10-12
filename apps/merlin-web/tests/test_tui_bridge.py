import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession
from merlin_tui.app import COMMANDS, MerlinApp
from merlin_tui.backend import DemoBackend, StreamEvent
from merlin_web.tui_bridge import open_workspace


@pytest.mark.asyncio
async def test_web_reuses_current_session_without_second_lease(monkeypatch):
    opened = []
    monkeypatch.setattr(
        "merlin_web.tui_bridge.webbrowser.open", lambda url: opened.append(url) or True
    )
    backend = DemoBackend()
    backend.close = AsyncMock()
    backend.acquire_foreground_lease = AsyncMock()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        await pilot.pause()
        backend.acquire_foreground_lease.reset_mock()
        app._remember("user", "Existing target and scope")
        await app._run_command("/web")
        console = app._web_console
        assert "/web" in COMMANDS
        assert console.workspace.backend is backend
        assert any(e["value"] == "Existing target and scope" for e in console.workspace.events)
        async with ClientSession() as browser:
            origin = console.url.split("/#")[0]
            response = await browser.post(
                origin + "/api/connect", json={"token": console.workspace.token}
            )
            assert response.status == 200
            cookies = {console.workspace.cookie_name: console.workspace.token}
            response = await browser.get(origin + "/api/events", cookies=cookies)
            assert response.status == 200
            assert (await response.json())["events"]
        await app._run_command("/web")
        assert app._web_console is console
        assert opened == [console.url, console.url]
        backend.acquire_foreground_lease.assert_not_awaited()
        await console.close()
        backend.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_steers_running_tui_recon_and_receives_events(monkeypatch):
    monkeypatch.setattr("merlin_web.tui_bridge.webbrowser.open", lambda url: True)
    release = asyncio.Event()
    started = asyncio.Event()
    backend = DemoBackend()
    backend.steer = AsyncMock(return_value=True)

    async def stream(prompt):
        started.set()
        yield StreamEvent("tool", "Recon started")
        await release.wait()
        yield StreamEvent("content", "Recon evidence")

    backend.stream = stream
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        await pilot.pause()
        console = await open_workspace(app)
        await app._start_backend("inspect current target", show_assistant=True)
        await started.wait()
        async with ClientSession() as browser:
            origin = console.url.split("/#")[0]
            cookies = {console.workspace.cookie_name: console.workspace.token}
            response = await browser.post(
                origin + "/api/chat",
                cookies=cookies,
                json={"prompt": "Focus on authentication", "request_id": "steering"},
            )
            assert response.status == 200
            assert (await response.json())["accepted"]
            backend.steer.assert_awaited_once_with("Focus on authentication")
            response = await browser.get(origin + "/api/events", cookies=cookies)
            assert (await response.json())["busy"]
        release.set()
        await app._stream_task
        assert any(e["value"] == "Recon evidence" for e in console.workspace.events)


@pytest.mark.asyncio
async def test_browser_chat_uses_tui_stream_driver(monkeypatch):
    monkeypatch.setattr("merlin_web.tui_bridge.webbrowser.open", lambda url: True)
    backend = DemoBackend()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        await pilot.pause()
        console = await open_workspace(app)
        await console.workspace.produce("Hello from browser")
        assert any(
            e.kind == "user" and e.text == "Hello from browser" for e in app._transcript_entries
        )
        assert any(e["kind"] == "content" for e in console.workspace.events)


@pytest.mark.asyncio
async def test_web_preserves_prepared_review_and_permission_owner(monkeypatch):
    monkeypatch.setattr("merlin_web.tui_bridge.webbrowser.open", lambda url: True)
    backend = DemoBackend()
    original = AsyncMock()
    backend._confirmer = original
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._pending_campaign_review = ("campaign/prepared", "sha256:exact")
        app._web_review = '{"campaign_id":"campaign/prepared","manifest_digest":"sha256:exact"}'
        app.query_one("#composer").text = "/web"
        await pilot.press("enter")
        await pilot.pause()
        console = app._web_console
        assert app._pending_campaign_review == ("campaign/prepared", "sha256:exact")
        assert console.workspace.latest_review == app._web_review
        assert backend._confirmer == console.workspace.confirm
        await console.close()
        assert backend._confirmer is original
