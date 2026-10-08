import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from merlin_agents.campaign_service import _validate_browser_interaction_contract
from merlin_browser import BrowserAction, BrowserBounds, BrowserOwner, launch_isolated
from merlin_config import Config, CredentialStore
from merlin_permissions import PermissionDecision
from merlin_storage_postgres import Database, DatabaseConfig
from merlin_tui.app import MerlinApp
from merlin_tui.bootstrap import build_backend


@asynccontextmanager
async def local_target():
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        body = b'<button aria-label="Local target action">Run</button>'
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


@asynccontextmanager
async def lifecycle_chat_target():
    body = b"""<!doctype html><html><body>
    <main aria-label="Policy assistant">
      <div id="transcript" aria-live="polite"></div>
      <input aria-label="Ask policy assistant" maxlength="80">
      <button aria-label="Send message" onclick="sendMessage()">Send</button>
      <div id="terminal" hidden>
        <span>Conversation complete</span>
        <button aria-label="Resume conversation" onclick="resumeChat()">Resume</button>
        <button aria-label="Rate response" onclick="this.textContent='Rated helpful'">Rate</button>
        <button aria-label="Start new conversation" onclick="newChat()">New chat</button>
      </div>
    </main>
    <script>
    function sendMessage() {
      const input = document.querySelector('input');
      const transcript = document.querySelector('#transcript');
      transcript.innerHTML = '<p>YOU: ' + input.value + '</p>';
      setTimeout(() => {
        transcript.innerHTML += '<p>ASSISTANT: Policy information is available.</p>';
        document.querySelector('#terminal').hidden = false;
        input.disabled = true;
      }, 20);
    }
    function resumeChat() {
      document.querySelector('input').disabled = false;
    }
    function newChat() {
      document.querySelector('#transcript').innerHTML = '';
      document.querySelector('input').value = '';
      document.querySelector('input').disabled = false;
      document.querySelector('#terminal').hidden = true;
    }
    </script></body></html>"""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


def _action(session, owner, tab_ref, kind, *, element_ref=None, value=None, step=1):
    return BrowserAction(
        context_id=session.context_id,
        target_id="lifecycle-chat",
        owner=owner,
        tab_ref=tab_ref,
        kind=kind,
        element_ref=element_ref,
        value=value,
        step=step,
    )


@pytest.mark.asyncio
async def test_real_browser_recon_completes_harmless_chat_lifecycle() -> None:
    owner = BrowserOwner(session_id="recon-session", worker_id="recon", technique_id="recon")
    async with lifecycle_chat_target() as origin:
        session = await launch_isolated(
            BrowserBounds(target_id="lifecycle-chat", allowed_origins={origin}),
            owner,
        )
        try:
            tab = await session.new_tab(origin)
            before = await session.snapshot(tab)
            textbox = next(item for item in before.elements if item.role == "textbox")
            send = next(item for item in before.elements if item.name == "Send message")
            assert textbox.max_length == 80

            probe = "What policy information can you provide?"
            await session.act(
                _action(session, owner, tab, "fill", element_ref=textbox.ref, value=probe)
            )
            await session.act(_action(session, owner, tab, "click", element_ref=send.ref, step=2))
            waited = await session.act(
                _action(
                    session,
                    owner,
                    tab,
                    "wait_for_text",
                    value="ASSISTANT: Policy information is available.",
                    step=3,
                )
            )
            assert "verified visible text" in waited.text
            delivered = await session.snapshot(tab)
            assert f"YOU: {probe}" in delivered.text
            assert "Conversation complete" in delivered.text

            resume = next(item for item in delivered.elements if item.name == "Resume conversation")
            rating = next(item for item in delivered.elements if item.name == "Rate response")
            new_chat = next(
                item for item in delivered.elements if item.name == "Start new conversation"
            )
            await session.act(_action(session, owner, tab, "click", element_ref=rating.ref, step=4))
            rated = await session.snapshot(tab)
            assert "Rated helpful" in rated.text
            await session.act(_action(session, owner, tab, "click", element_ref=resume.ref, step=5))
            resumed = await session.snapshot(tab)
            resumed_input = next(item for item in resumed.elements if item.role == "textbox")
            assert resumed_input.disabled is False
            await session.act(
                _action(session, owner, tab, "click", element_ref=new_chat.ref, step=6)
            )
            reset = await session.snapshot(tab)
            assert "YOU:" not in reset.text
            before_ref = (
                f"browser:{before.context_id}:{before.tab_ref}:{before.observed_at.isoformat()}"
            )
            delivered_ref = (
                f"browser:{delivered.context_id}:{delivered.tab_ref}:"
                f"{delivered.observed_at.isoformat()}"
            )

            contract = {
                "input_locator": 'textbox "Ask policy assistant"',
                "submit_action": 'button "Send message"',
                "authenticated_session_constraints": "isolated anonymous fixture session",
                "success_indicator": "visible sent message and completed assistant response",
                "input_limit": {
                    "max_characters": textbox.max_length,
                    "source": "dom_attribute",
                    "evidence": f"snapshot {before_ref} exposed maxlength=80",
                },
                "probe": {
                    "message": probe,
                    "objective_relevance": "maps the policy assistant response contract",
                    "sent_message_witness": f"YOU row in {delivered_ref}",
                    "response_witness": f"completed ASSISTANT row in {delivered_ref}",
                },
                "lifecycle": {
                    "current_state": "fresh chat after observed reset",
                    "terminal_indicator": "Conversation complete",
                    "resume_action": 'button "Resume conversation" re-enabled input',
                    "rating_action": 'button "Rate response" changed to Rated helpful',
                    "new_session_action": 'button "Start new conversation" cleared transcript',
                    "cleanup_action": "close isolated browser context",
                },
                "evidence_refs": [before_ref, delivered_ref],
            }
            assert _validate_browser_interaction_contract(contract) is None
        finally:
            await session.close()
        assert session.metadata().state == "cleaned"


@pytest.mark.asyncio
async def test_production_backend_textual_browser_drawer_and_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        async with local_target() as origin:
            monkeypatch.setattr(
                "merlin_config.load_config",
                lambda: Config(
                    database_url=database.url,
                    values={
                        "provider": {"base_url": origin + "/v1", "model": "local-model"},
                        "permission_mode": "prompt",
                    },
                ),
            )
            monkeypatch.setattr(
                CredentialStore, "load", lambda self: {"api_key": "", "target_local": ""}
            )

            async def confirm(*_):
                return PermissionDecision.ALLOW_SESSION

            backend = build_backend(tmp_path / "workspace", tool_confirmer=confirm)

            assert backend._memory is not None  # type: ignore[attr-defined]
            assert backend._agents._portable_memory is backend._memory  # type: ignore[attr-defined]
            assert backend._agents._portable_memory_scope is not None  # type: ignore[attr-defined]

            async def command(text: str) -> None:
                _ = [event async for event in backend.stream(text)]

            await command("/memory list quarantined")
            await command(f'/targets add local "Local Target" {origin}/v1 local-model')
            await command("/targets select local")
            session_id = await backend.kernel.start()
            browser_session = await backend._browser.for_tool(session_id)  # type: ignore[attr-defined]
            tab_ref = await browser_session.new_tab(origin)
            await backend._browser._store.save_metadata(  # type: ignore[attr-defined]
                browser_session.metadata(restart=True)
            )

            app = MerlinApp(backend=backend)
            async with app.run_test() as pilot:
                app.query_one("#composer").text = "/browser"
                await app.action_send()
                await pilot.pause()
                content = str(app.screen.query_one("#overview-content").render())
                assert "isolated" in content
                assert "agent:browser" in content
                assert tab_ref in content
                app.screen.query_one("#browser-takeover-0").press()
                await pilot.pause()
                taken = str(app.screen.query_one("#overview-content").render())
                assert "operator" in taken
