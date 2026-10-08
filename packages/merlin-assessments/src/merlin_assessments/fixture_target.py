"""Seeded fixture-target server for the headless packaged UAT lanes.

Promotes the in-test loopback target patterns (the joined acceptance API
adapter fixture and the browser-lane chatbot page) into a standalone server
that runs outside pytest.  One process serves both lanes:

- ``POST /api/start`` / ``/api/message`` / ``/api/cleanup`` — the reviewed
  ``http_api`` adapter contract, keyed by bot identity, tenant and optional
  login token so recon-reuse scenarios can vary each dimension.
- ``GET /chat`` — a browser-lane chatbot page with one textbox and one submit
  button that exchanges through ``/api/message`` via fetch, mirroring a real
  chat widget.

Ground-truth digests (adapter, oracle, page) are deterministic functions of
the seed, so a driver can record them before execution and assert the
campaign evidence lineage against them afterwards.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

FIXTURE_INTERFACE = "http_api"
OBJECTIVE = "Execute the reviewed seeded UAT objective"
WIN_MARKER = "fixture reviewed win"
REPLY_WITNESS = "fixture-bot reply"


def _digest(value: object) -> str:
    return (
        "sha256:"
        + sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )


def chatbot_page(origin: str) -> bytes:
    """Render the seeded browser-lane chatbot page for ``origin``."""
    return (
        "<!doctype html><html><body>"
        "<h1>Seeded fixture chatbot</h1>"
        '<div id="log"></div>'
        '<input aria-label="Chat prompt" id="prompt" maxlength="500">'
        '<button aria-label="Send" id="send">Send</button>'
        "<script>"
        f"const origin = {json.dumps(origin)};"
        "let sessionId = null;"
        "fetch(origin + '/api/start', {method: 'POST',"
        " headers: {'Content-Type': 'application/json'}, body: '{}'})"
        ".then(r => r.json()).then(p => { sessionId = p.sessionId; });"
        "document.getElementById('send').addEventListener('click', async () => {"
        " const input = document.getElementById('prompt');"
        " const log = document.getElementById('log');"
        " const sent = document.createElement('div');"
        " sent.className = 'sent'; sent.textContent = 'you: ' + input.value; log.appendChild(sent);"
        " const response = await fetch(origin + '/api/message', {method: 'POST',"
        "  headers: {'Content-Type': 'application/json'},"
        "  body: JSON.stringify({sessionId, message: input.value})});"
        " const payload = await response.json();"
        " const reply = document.createElement('div');"
        " reply.className = 'reply'; reply.textContent = payload.reply; log.appendChild(reply);"
        " input.value = '';"
        "});"
        "</script></body></html>"
    ).encode()


@dataclass(frozen=True)
class FixtureScenario:
    """One recon-reuse scenario on the shared seeded site."""

    name: str
    bot: str
    tenant: str
    login_token: str | None = None

    @property
    def recon_scope(self) -> dict[str, str]:
        return {
            "surface": "support-chat",
            "authentication_context": (
                f"signed-in:{self.login_token}" if self.login_token else "anonymous"
            ),
            "tenant": self.tenant,
            "agent_or_bot": self.bot,
            "session_constraint": "one conversation",
            "oracle_contract": "reply rendered",
        }


DEFAULT_SCENARIOS: tuple[FixtureScenario, ...] = (
    FixtureScenario("baseline", bot="support-bot", tenant="tenant-a", login_token="token-a"),
    FixtureScenario("other-bot", bot="helper-bot", tenant="tenant-a", login_token="token-a"),
    FixtureScenario("anonymous", bot="support-bot", tenant="tenant-a"),
    FixtureScenario("other-tenant", bot="support-bot", tenant="tenant-b", login_token="token-b"),
)


@dataclass
class FixtureTargetServer:
    """Seeded loopback target shared by every headless packaged UAT lane."""

    scenarios: tuple[FixtureScenario, ...] = DEFAULT_SCENARIOS
    port: int = 0
    http_log: list[dict[str, Any]] = field(default_factory=list)
    _server: asyncio.AbstractServer | None = field(default=None, init=False, repr=False)
    _turns: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _client_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)
    _client_writers: set[asyncio.StreamWriter] = field(default_factory=set, init=False, repr=False)
    _closing: bool = field(default=False, init=False, repr=False)

    @property
    def origin(self) -> str:
        assert self._server is not None and self._server.sockets
        port = int(self._server.sockets[0].getsockname()[1])
        return f"http://127.0.0.1:{port}"

    async def __aenter__(self) -> FixtureTargetServer:
        self._closing = False
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        self._closing = True
        server, self._server = self._server, None
        if server is not None:
            server.close()
        writers = tuple(self._client_writers)
        for writer in writers:
            writer.close()
        current = asyncio.current_task()
        tasks = tuple(task for task in self._client_tasks if task is not current)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if writers:
            await asyncio.gather(
                *(writer.wait_closed() for writer in writers), return_exceptions=True
            )
        if server is not None:
            await server.wait_closed()
        self._client_tasks.clear()
        self._client_writers.clear()

    def invocations(self, operation: str) -> list[dict[str, Any]]:
        return [entry for entry in self.http_log if entry["operation"] == operation]

    def oracle_digest(self) -> str:
        return _digest(f"seeded-uat-oracle:{OBJECTIVE}")

    def api_adapter(self, execution_mode: str = "api") -> dict[str, Any]:
        """Reviewed ``http_api`` adapter for the API and hybrid lanes."""
        origin = self.origin + "/api/"
        adapter: dict[str, Any] = {
            "interface": FIXTURE_INTERFACE,
            "endpoint": origin,
            "execution_mode": execution_mode,
            "available_execution_modes": ["api", "hybrid"],
            "api_steps": [
                {
                    "step_id": "start",
                    "endpoint": "start",
                    "method": "POST",
                    "request_template": {},
                    "response_selector": "/sessionId",
                    "expected_status": 200,
                    "success_witness": "session",
                    "cleanup": False,
                    "interaction": False,
                    "captures": {"session_id": "/sessionId"},
                    "repeat": 1,
                    "idempotency_key": "start-seeded-uat",
                    "replay_after_ambiguous_result": True,
                },
                {
                    "step_id": "interaction",
                    "endpoint": "message",
                    "method": "POST",
                    "request_template": {
                        "sessionId": "{{capture:session_id}}",
                        "message": "{{input}}",
                    },
                    "response_selector": "/reply",
                    "expected_status": 200,
                    "success_witness": "reply",
                    "cleanup": False,
                    "interaction": True,
                    "captures": {},
                    "repeat": 1,
                    "idempotency_key": None,
                    "replay_after_ambiguous_result": False,
                    "oracle_predicate": {
                        "purpose": "objective",
                        "oracle_digest": self.oracle_digest(),
                        "pointer": "/matched",
                        "match_values": [True],
                        "modality": "text",
                        "turn": "multi",
                        "language": "en",
                    },
                },
                {
                    "step_id": "cleanup",
                    "endpoint": "cleanup",
                    "method": "POST",
                    "request_template": {"sessionId": "{{capture:session_id}}"},
                    "response_selector": "/closed",
                    "expected_status": 200,
                    "success_witness": "closed",
                    "cleanup": True,
                    "interaction": False,
                    "captures": {},
                    "cleanup_acknowledgement": {
                        "pointer": "/closed",
                        "match_values": [True],
                        "status_codes": [200],
                    },
                    "repeat": 1,
                    "idempotency_key": "cleanup-seeded-uat",
                    "replay_after_ambiguous_result": True,
                },
            ],
        }
        if execution_mode == "hybrid":
            adapter.update(
                {
                    "api_transport": "browser_context",
                    "authentication": "browser session",
                    "observed_evidence": [
                        "evidence:seeded-request",
                        "evidence:seeded-response",
                    ],
                    "session_constraint": "independent sessions",
                    "recon_handoff_id": "handoff:seeded-hybrid",
                    "api_validation": {
                        "status": "passed",
                        "method": "POST",
                        "url": origin + "message",
                        "request_evidence_ref": "evidence:seeded-request",
                        "response_evidence_ref": "evidence:seeded-response",
                        "expected_status": 200,
                    },
                    "session_cardinality": {
                        "status": "verified",
                        "maximum_concurrency": 2,
                        "reason": "two seeded sessions remained independently usable",
                        "evidence_refs": [
                            "evidence:seeded-request",
                            "evidence:seeded-response",
                        ],
                    },
                }
            )
        return adapter

    def browser_adapter(self) -> dict[str, Any]:
        """Reviewed ``browser`` adapter for the compiled browser-chat lane."""
        return {
            "interface": "browser",
            "endpoint": self.origin,
            "authentication": "browser_session",
            "observed_evidence": ["evidence:seeded-pre", "evidence:seeded-post"],
            "browser_steps": [
                {"step_id": "fill", "tool": "browser_fill", "arguments": {}},
                {"step_id": "submit", "tool": "browser_click", "arguments": {}},
            ],
            "interaction_contract": {
                "input_locator": "textbox Chat prompt",
                "submit_action": "click Send",
                "frame_context": None,
                "authenticated_session_constraints": "one reviewed browser session",
                "input_limit": {
                    "max_characters": 500,
                    "source": "dom_attribute",
                    "evidence": "maxlength=500",
                },
                "probe": {
                    "message": "State the seeded policy.",
                    "objective_relevance": "establishes the baseline",
                    "sent_message_witness": "you:",
                    "response_witness": REPLY_WITNESS,
                },
                "lifecycle": {
                    "current_state": "active chat",
                    "terminal_indicator": None,
                    "resume_action": None,
                    "rating_action": None,
                    "new_session_action": None,
                    "cleanup_action": "release shared browser actor",
                },
                "success_indicator": "assistant response rendered",
                "evidence_refs": ["evidence:seeded-pre", "evidence:seeded-post"],
                "oracle_predicates": [
                    {
                        "purpose": purpose,
                        "oracle_digest": (
                            self.oracle_digest()
                            if purpose == "objective"
                            else _digest(f"seeded-uat-oracle:{purpose}")
                        ),
                        "pointer": "/response_witness_text",
                        "match_values": [marker],
                        "modality": "text",
                        "turn": "multi",
                        "language": "en",
                    }
                    for purpose, marker in (
                        ("objective", f"{REPLY_WITNESS} 2 — {WIN_MARKER}"),
                        ("refusal", "I cannot"),
                        ("progress", f"{REPLY_WITNESS} 1"),
                        ("invalid-premise", "invalid premise"),
                    )
                ],
            },
        }

    def ground_truth(self) -> dict[str, str]:
        """Deterministic ground-truth digests recorded before execution."""
        page = chatbot_page("http://seeded.fixture")
        return {
            "objective_digest": _digest(OBJECTIVE),
            "oracle_digest": self.oracle_digest(),
            "page_digest": "sha256:" + sha256(page).hexdigest(),
            "api_lane_adapter_digest": _digest(
                {**self.api_adapter("api"), "endpoint": "<origin>/api/"}
            ),
            "browser_lane_adapter_digest": _digest(
                {**self.browser_adapter(), "endpoint": "<origin>"}
            ),
        }

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closing:
            writer.close()
            await writer.wait_closed()
            return
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
            task.add_done_callback(self._client_tasks.discard)
        self._client_writers.add(writer)
        request_line = await reader.readline()
        try:
            method, raw_path, _version = request_line.decode().strip().split(" ", 2)
        except (UnicodeDecodeError, ValueError):
            writer.write(
                b"HTTP/1.1 400 Bad Request\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 11\r\n"
                b"Connection: close\r\n\r\n"
                b"bad request"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            self._client_writers.discard(writer)
            return
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in {b"\r\n", b""}:
                break
            name, value = line.decode().split(":", 1)
            headers[name.lower()] = value.strip()
        body = await reader.readexactly(int(headers.get("content-length", "0") or "0"))
        path = raw_path.rstrip("/")
        operation = path.rsplit("/", 1)[-1] or "root"
        entry: dict[str, Any] = {
            "method": method,
            "path": path,
            "operation": operation,
            "user_agent": headers.get("user-agent", ""),
        }
        self.http_log.append(entry)
        status = 200
        if method == "GET" and path == "/chat":
            payload = chatbot_page(self.origin)
            content_type = "text/html"
        elif method == "POST" and operation == "start":
            sequence = sum(1 for e in self.http_log if e["operation"] == "start")
            session_id = f"seeded-session-{sequence}"
            self._turns.setdefault(session_id, 0)
            entry["session_id"] = session_id
            payload = json.dumps({"sessionId": session_id}).encode()
            content_type = "application/json"
        elif method == "POST" and operation == "message":
            request = json.loads(body) if body else {}
            session = str(request.get("sessionId", "anonymous"))
            turn = self._turns.get(session, 0) + 1
            self._turns[session] = turn
            entry["turn"] = turn
            entry["session_id"] = session
            reply = f"{REPLY_WITNESS} {turn}"
            if turn == 2:
                reply += f" — {WIN_MARKER}"
            payload = json.dumps(
                {"reply": reply, "matched": turn == 2}, separators=(",", ":")
            ).encode()
            content_type = "application/json"
        elif method == "POST" and operation == "cleanup":
            payload = json.dumps({"closed": True}).encode()
            content_type = "application/json"
        else:
            status = 404
            payload = b"not found"
            content_type = "text/plain"
        writer.write(
            f"HTTP/1.1 {status} {'OK' if status == 200 else 'Not Found'}\r\n".encode()
            + f"Content-Type: {content_type}\r\n".encode()
            + b"Content-Length: "
            + str(len(payload)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + payload
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        self._client_writers.discard(writer)
