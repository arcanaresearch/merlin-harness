import asyncio
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from merlin_assessments import AssessmentService
from merlin_assessments.service import _target_from_objective
from merlin_browser import BrowserAction, BrowserBounds, BrowserOwner, OriginDenied, launch_isolated
from merlin_contracts import TargetProfile
from merlin_packs import PackCatalog
from merlin_providers import ProviderConfig


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:3000/game", "http://localhost:3000/game"),
        ("http://127.0.0.1:8765/", "http://127.0.0.1:8765/"),
        ("http://[::1]:3000/game", "http://[::1]:3000/game"),
        ("http://[::1]", "http://[::1]/"),
        ("HTTP://LOCALHOST:3000/game", "http://localhost:3000/game"),
        ("http://target.test/chat", "http://target.test/chat"),
        ("https://Target.test/chat", "https://target.test/chat"),
    ],
)
def test_objective_preserves_target_scheme_host_port_and_path(url, expected):
    profile = _target_from_objective(f"Assess ({url}), using the game UI.")
    assert profile is not None
    assert profile.base_url == expected


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/game",
        "ftp://target.test/game",
        "http://user:secret@localhost/game",
        "http://localhost:bad/game",
    ],
)
def test_objective_rejects_unsupported_or_invalid_target_urls(url):
    assert _target_from_objective(f"Assess {url}") is None


class TargetStore:
    def __init__(self):
        unrelated = TargetProfile(
            id="unrelated", title="Unrelated", base_url="https://other.test/", model="web-chat"
        )
        self.targets = {unrelated.id: unrelated}

    async def list_targets(self):
        return tuple(self.targets.values())

    async def save_target(self, profile):
        self.targets[profile.id] = profile
        return profile

    async def get_target(self, target_id):
        return self.targets.get(target_id)


async def test_http_objective_prepares_campaign_and_operates_local_browser():
    requests = []

    async def serve(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        requests.append(request.split(b" ", 2)[1])
        body = (
            b'<button aria-label="Run game" onclick="this.textContent=\'Game played\'">'
            b"Run game</button>"
        )
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/game"
        store = TargetStore()
        service = AssessmentService(
            catalog=PackCatalog(),
            store=store,
            attacker=object(),
            attacker_config=ProviderConfig("https://attacker.test/v1", "", "test"),
            # Preparation must never construct a provider or perform network I/O.
            target_factory=SimpleNamespace(),
        )
        inputs = await service.campaign_inputs(f"Assess the game at {url}", profile="quickscan")
        target = inputs["target"]
        assert target["base_url"] == url
        assert target["id"] != "unrelated"
        assert requests == []
        parts = urlsplit(target["base_url"])
        origin = f"{parts.scheme}://{parts.netloc}"
        bounds = BrowserBounds(target_id=target["id"], allowed_origins={origin})
        owner = BrowserOwner(session_id="http-test", worker_id="operator", technique_id="recon")
        async with await launch_isolated(bounds, owner) as session:
            tab = await session.new_tab(target["base_url"])
            observation = await session.snapshot(tab)
            assert "Run game" in observation.text
            # Exercise the real browser UI over plaintext HTTP.
            button = next(element for element in observation.elements if element.name == "Run game")
            await session.act(
                BrowserAction(
                    context_id=session.context_id,
                    target_id=target["id"],
                    owner=owner,
                    tab_ref=tab,
                    kind="click",
                    element_ref=button.ref,
                    step=1,
                )
            )
            assert "Game played" in (await session.snapshot(tab)).text
            with pytest.raises(OriginDenied):
                await session.new_tab(f"https://127.0.0.1:{port}/game")
        assert b"/game" in requests
