import asyncio
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from merlin_tui.backend import StreamEvent
from merlin_web.server import Workspace, create_app


class Backend:
    def __init__(self):
        self.calls = []
        self.release = asyncio.Event()

    async def stream(self, prompt):
        self.calls.append(("stream", prompt))
        if prompt == "/new":
            yield StreamEvent("status", "new session")
            return
        yield StreamEvent("status", "Reconnaissance")
        await self.release.wait()
        yield StreamEvent("content", "Evidence retained")

    async def steer(self, prompt):
        self.calls.append(("steer", prompt))
        return True

    async def close(self):
        pass

    async def interrupt(self):
        self.release.set()

    async def execute_campaign(self, *args):
        self.calls.append(("execute", *args))
        return {"status": "executing"}

    async def guide_campaign(self, *args):
        self.calls.append(("guide", *args))
        return {"delivery_state": "queued"}

    async def cancel_task(self, *args):
        self.calls.append(("cancel_task", *args))
        return "cancelled"

    async def resume_campaign(self, campaign_id):
        self.calls.append(("resume_campaign", campaign_id))
        return "resumed"

    async def cancel_campaign(self, campaign_id):
        self.calls.append(("cancel_campaign", campaign_id))
        return "cancelled"

    async def set_permission_mode(self, mode):
        assert mode in {"prompt", "auto", "yolo"}
        self.calls.append(("mode", mode))
        return mode

    async def list_reports(self):
        return self.reports

    async def export_campaign_report(self, campaign_id, source, format):
        from pathlib import Path

        path = Path(source).with_suffix({"markdown": ".md", "json": ".json", "csv": ".zip"}[format])
        path.write_bytes(b"retained fixture evidence")
        self.calls.append(("export", campaign_id, source, format))
        return {"run": campaign_id, "output": str(path), "format": format}


@pytest.fixture
async def browser_api(tmp_path):
    (tmp_path / "index.html").write_text("<h1>Merlin</h1>")
    backend = Backend()
    workspace = Workspace(backend)
    client = TestClient(TestServer(create_app(workspace, tmp_path)))
    await client.start_server()
    client.session.headers["Cookie"] = f"{workspace.cookie_name}={workspace.token}"
    yield client, workspace, backend, tmp_path
    await client.close()


async def test_auth_required_and_wrong_token_refused(browser_api):
    client, workspace, _, _ = browser_api
    client.session.headers.pop("Cookie")
    assert (await client.get("/api/events")).status == 401
    assert (await client.post("/api/connect", json={"token": "wrong"})).status == 401
    response = await client.post("/api/connect", json={"token": workspace.token})
    assert response.status == 200
    assert response.cookies[workspace.cookie_name]["httponly"]
    assert response.cookies[workspace.cookie_name]["samesite"] == "Strict"


async def test_origin_and_host_cannot_control_local_daemon(browser_api):
    client, _, backend, _ = browser_api
    assert (
        await client.post(
            "/api/rpc", json={"method": "interrupt"}, headers={"Origin": "https://evil.example"}
        )
    ).status == 403
    assert (await client.get("/", headers={"Host": "evil.example"})).status == 403
    assert backend.calls == []


async def test_unknown_rpc_cannot_invoke_python_attributes(browser_api):
    client, _, backend, _ = browser_api
    response = await client.post("/api/rpc", json={"method": "__getattribute__"})
    assert response.status == 400
    assert backend.calls == []


async def test_approval_preserves_exact_campaign_and_manifest(browser_api):
    client, _, backend, _ = browser_api
    response = await client.post(
        "/api/rpc",
        json={"method": "execute_campaign", "args": ["campaign/exact", "sha256:reviewed"]},
    )
    assert response.status == 200
    assert backend.calls == [("execute", "campaign/exact", "sha256:reviewed")]


async def test_recon_guidance_does_not_restart_or_interrupt_turn(browser_api):
    client, workspace, backend, _ = browser_api
    await client.post("/api/chat", json={"prompt": "scan authorized fixture", "request_id": "1"})
    await asyncio.sleep(0)
    response = await client.post("/api/chat", json={"prompt": "Focus on memory", "request_id": "2"})
    assert response.status == 200
    assert backend.calls == [("stream", "scan authorized fixture"), ("steer", "Focus on memory")]
    assert workspace.turn and not workspace.turn.done()
    response = await client.post("/api/chat", json={"prompt": "/new", "request_id": "3"})
    assert response.status == 400


async def test_duplicate_request_does_not_send_twice(browser_api):
    client, _, backend, _ = browser_api
    payload = {"prompt": "start", "request_id": "same"}
    await client.post("/api/chat", json=payload)
    await client.post("/api/chat", json=payload)
    await asyncio.sleep(0)
    assert backend.calls == [("stream", "start")]
    assert (await client.post("/api/chat", json={**payload, "prompt": "different"})).status == 400


async def test_permission_waits_for_explicit_operator_decision(browser_api):
    client, workspace, _, _ = browser_api
    task = asyncio.create_task(workspace.confirm(SimpleNamespace(name="target_request"), {}, {}))
    await asyncio.sleep(0)
    pending = (await (await client.get("/api/events")).json())["permissions"][0]
    assert not task.done()
    response = await client.post(
        "/api/permission", json={"id": pending["id"], "decision": "allow_once"}
    )
    assert response.status == 200
    assert await task == "allow_once"
    assert (
        await client.post("/api/permission", json={"id": pending["id"], "decision": "allow_once"})
    ).status == 400


async def test_events_replay_with_monotonic_cursor_after_ring_rollover(browser_api):
    client, workspace, _, _ = browser_api
    for i in range(20005):
        workspace.emit("status", str(i))
    result = await (await client.get("/api/events?after=20003")).json()
    assert [event["cursor"] for event in result["events"]] == [20004, 20005]
    result = await (await client.get("/api/events?after=0")).json()
    assert result["expired"] is True
    assert len(result["events"]) == 500


async def test_reports_allow_only_registered_artifacts_and_sandbox_html(browser_api):
    client, _, backend, tmp_path = browser_api
    report = tmp_path / "report.html"
    report.write_text("<script>fetch('/api/rpc')</script>")
    backend.reports = [{"output": str(report), "run": "campaign/fixture"}]
    assert (await client.get("/api/file", params={"path": str(report)})).status == 404
    await client.post("/api/rpc", json={"method": "list_reports"})
    response = await client.get("/api/file", params={"path": str(report)})
    assert response.status == 200
    assert response.headers["Content-Security-Policy"].startswith("sandbox;")
    assert (
        await client.get("/api/file", params={"path": str(tmp_path / "index.html")})
    ).status == 404


async def test_campaign_and_task_command_routes_bypass_model_interpretation(browser_api):
    _, workspace, backend, _ = browser_api
    await workspace.produce('/campaign message campaign/fixture "focus on authorization"')
    await workspace.produce("/cancel task task/one")
    assert backend.calls == [
        ("guide", "campaign/fixture", "focus on authorization"),
        ("cancel_task", "task/one"),
    ]


async def test_session_reset_discards_stale_review(browser_api):
    _, workspace, backend, _ = browser_api
    workspace.emit("campaign_review", '{"campaign_id":"old"}')
    backend.release.set()
    await workspace.produce("/new")
    assert workspace.latest_review is None
    assert any(event["kind"] == "session_reset" for event in workspace.events)


async def test_campaign_resume_does_not_reset_session_or_review(browser_api):
    _, workspace, backend, _ = browser_api
    workspace.latest_review = "retained"
    await workspace.produce("/resume campaign campaign/fixture")
    assert backend.calls == [("resume_campaign", "campaign/fixture")]
    assert workspace.latest_review == "retained"
    assert not any(e["kind"] == "session_reset" for e in workspace.events)


async def test_tui_cancellation_and_mode_aliases(browser_api):
    _, workspace, backend, _ = browser_api
    await workspace.produce("/agents kill all")
    await workspace.produce("/campaign cancel all")
    await workspace.produce("/mode prompt")
    assert backend.calls == [("cancel_campaign", "*"), ("cancel_campaign", "*"), ("mode", "prompt")]


async def test_invalid_json_shape_and_static_traversal(browser_api):
    client, _, _, _ = browser_api
    assert (await client.post("/api/chat", json=[])).status == 400
    assert (await client.get("/api/file", params={"path": "/etc/passwd"})).status == 404
    response = await client.get("/")
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize("format", ["markdown", "json", "csv"])
async def test_exports_preserve_selected_report_and_download_bytes(browser_api, format):
    client, _, backend, tmp_path = browser_api
    source = str(tmp_path / "selected-revision.html")
    result = await client.post(
        "/api/rpc",
        json={"method": "export_campaign_report", "args": ["campaign/selected", source, format]},
    )
    assert result.status == 200
    value = (await result.json())["value"]
    response = await client.get("/api/file", params={"path": value["output"]})
    assert response.status == 200
    assert await response.read() == b"retained fixture evidence"
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert backend.calls == [("export", "campaign/selected", source, format)]


async def test_executed_review_is_not_restored_by_historical_event_replay(browser_api):
    client, workspace, _, _ = browser_api
    workspace.emit("campaign_review", '{"campaign_id":"campaign/one"}')
    await client.post(
        "/api/rpc", json={"method": "execute_campaign", "args": ["campaign/one", "digest"]}
    )
    snapshot = await (await client.get("/api/events?after=0")).json()
    assert snapshot["events"][0]["kind"] == "campaign_review"
    assert snapshot["review"] is None


async def test_agent_state_uses_the_tui_lifecycle_not_an_absent_status_field(browser_api):
    from merlin_tui.backend import AgentView

    client, _, backend, _ = browser_api

    async def agents():
        return (
            AgentView(
                name="memory/execute",
                owner="worker/one",
                task="fixture",
                provider="local",
                elapsed="durable",
                latest_event="evaluation_uncertain",
            ),
        )

    backend.list_agents = agents
    result = await client.post("/api/rpc", json={"method": "list_agents"})
    agent = (await result.json())["value"][0]
    assert agent["status"] == "needs review"
    assert agent["status_group"] == "uncertain"
    assert agent["latest_event"] == "evaluation_uncertain"


@pytest.mark.parametrize(
    "method",
    [
        "list_campaigns",
        "list_targets",
        "list_findings",
        "list_reports",
        "list_resume_sessions",
        "list_packs",
        "list_skills",
        "list_schedules",
        "list_mcp_servers",
        "list_memory_records",
        "list_browser_contexts",
        "list_permission_history",
        "list_promotion_candidates",
        "run_doctor",
        "list_logs",
        "list_history",
    ],
)
async def test_populated_daemon_records_remain_objects(browser_api, method):
    from unittest.mock import AsyncMock

    client, _, backend, _ = browser_api
    setattr(
        backend,
        method,
        AsyncMock(
            return_value=(
                SimpleNamespace(
                    id="fixture",
                    title="Retained record",
                    nested=SimpleNamespace(count=2),
                    children=(SimpleNamespace(name="child"),),
                ),
            )
        ),
    )
    response = await client.post("/api/rpc", json={"method": method})
    assert response.status == 200
    assert (await response.json())["value"] == [
        {
            "id": "fixture",
            "title": "Retained record",
            "nested": {"count": 2},
            "children": [{"name": "child"}],
        }
    ]


async def test_daemon_pagination_and_runtime_stay_structured(browser_api):
    from unittest.mock import AsyncMock

    client, _, backend, _ = browser_api
    backend.list_resume_session_page = AsyncMock(
        return_value=SimpleNamespace(
            items=(SimpleNamespace(id="saved-session", title="Existing conversation"),),
            total=1,
            offset=0,
            limit=50,
        )
    )
    backend.get_runtime_status = AsyncMock(
        return_value=SimpleNamespace(model="fixture", routing=SimpleNamespace(lead="fixture-lead"))
    )
    for method, expected in [
        (
            "list_resume_session_page",
            {
                "items": [{"id": "saved-session", "title": "Existing conversation"}],
                "total": 1,
                "offset": 0,
                "limit": 50,
            },
        ),
        ("get_runtime_status", {"model": "fixture", "routing": {"lead": "fixture-lead"}}),
    ]:
        response = await client.post("/api/rpc", json={"method": method})
        assert (await response.json())["value"] == expected


async def test_failed_new_session_preserves_review_and_visible_history(browser_api):
    client, workspace, backend, _ = browser_api
    review = {"campaign_id": "existing", "manifest_digest": "sha256:existing"}
    workspace.emit("campaign_review", review)

    async def rejected_stream(prompt):
        raise RuntimeError("Fixture daemon rejected new session")
        yield

    backend.stream = rejected_stream
    await workspace.produce("/new")
    assert workspace.latest_review == review
    assert not any(e["kind"] == "session_reset" for e in workspace.events)
    assert any(e["kind"] == "error" for e in workspace.events)


async def test_concurrent_retries_deliver_guidance_only_once(browser_api):
    from unittest.mock import AsyncMock

    client, workspace, backend, _ = browser_api
    workspace.external_busy = lambda: True
    backend.steer = AsyncMock(return_value=True)
    payload = {"prompt": "Keep the authorized target", "request_id": "same-request"}
    responses = await asyncio.gather(*(client.post("/api/chat", json=payload) for _ in range(2)))
    assert all(response.status == 200 for response in responses)
    backend.steer.assert_awaited_once_with(payload["prompt"])
    assert sum(e["kind"] == "user" for e in workspace.events) == 1


async def test_provider_inventory_and_multiple_keys_are_private(browser_api, monkeypatch, tmp_path):
    from merlin_config import ConfigPaths, load_config

    paths = ConfigPaths(tmp_path / "isolated-config")
    monkeypatch.setattr(ConfigPaths, "default", classmethod(lambda cls: paths))
    client, _, _, _ = browser_api
    for model, endpoint, key in [
        ("one", "https://one.test/v1", "secret-one"),
        ("two", "https://two.test/v1", "secret-two"),
    ]:
        response = await client.post(
            "/api/rpc", json={"method": "configure_model_endpoint", "args": [model, endpoint, key]}
        )
        assert response.status == 200
    response = await client.post("/api/rpc", json={"method": "setup_status"})
    raw = await response.text()
    setup = (await response.json())["value"]
    assert len(setup["providers"]) == 8
    assert {route["model"] for route in setup["model_endpoints"]} == {"one", "two"}
    assert all(route["key_saved"] for route in setup["model_endpoints"])
    assert "secret-one" not in raw and "secret-two" not in raw
    routes = load_config(paths, environ={}).provider_model_endpoints
    assert routes["one"]["api_key"] == "secret-one"
    assert routes["two"]["api_key"] == "secret-two"


async def test_default_provider_change_does_not_reuse_old_key(browser_api, monkeypatch, tmp_path):
    from merlin_config import ConfigPaths, CredentialStore
    from merlin_tui.configuration import ProviderConfiguration

    paths = ConfigPaths(tmp_path / "isolated-config")
    monkeypatch.setattr(ConfigPaths, "default", classmethod(lambda cls: paths))
    ProviderConfiguration(paths).save("https://old.test/v1", "old-model", "old-secret")
    client, _, _, _ = browser_api
    response = await client.post(
        "/api/rpc",
        json={"method": "configure_provider", "args": ["https://new.test/v1", "new-model", None]},
    )
    assert response.status == 200
    assert not CredentialStore(paths).load(environ={}).get("api_key")


async def test_session_inventory_keeps_more_than_fifty_current_campaigns():
    from merlin_agents.campaign_service import CampaignService

    class Store:
        def list(self, session_id, *, limit):
            assert session_id == "selected-session"
            assert limit is None
            return [
                dict(
                    id=f"campaign/{i}",
                    session_id=session_id,
                    status="completed",
                    manifest_digest="digest",
                )
                for i in range(61)
            ]

        def list_all(self, **kwargs):
            raise AssertionError("Inventory must not load manifests from unrelated sessions")

    service = object.__new__(CampaignService)
    service._store = Store()
    result = await service.list("selected-session", limit=None)
    assert len(result) == 61
    assert all(row["session_id"] == "selected-session" for row in result)


async def test_two_workspace_ports_do_not_overwrite_browser_auth(tmp_path):
    from aiohttp import ClientSession, CookieJar

    (tmp_path / "index.html").write_text("Merlin")
    first = Workspace(Backend())
    second = Workspace(Backend())
    servers = [TestServer(create_app(item, tmp_path)) for item in (first, second)]
    try:
        for server in servers:
            await server.start_server()
        # Real browser cookies share 127.0.0.1 across both ports.
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as browser:
            for server, workspace in zip(servers, (first, second), strict=True):
                async with browser.post(
                    server.make_url("/api/connect"), json={"token": workspace.token}
                ) as response:
                    assert response.status == 200
            for server in servers:
                async with browser.get(server.make_url("/api/events")) as response:
                    assert response.status == 200
    finally:
        for server in servers:
            await server.close()


async def test_command_approval_shows_redacted_execution_details(browser_api):
    client, workspace, _, root = browser_api
    task = asyncio.create_task(
        workspace.confirm(
            SimpleNamespace(name="shell_command"),
            {"workspace_root": str(root)},
            {
                "argv": ["echo", "--token=private-value"],
                "cwd": ".",
                "__permission_effect": "subprocess",
                "__permission_command_scope": True,
            },
        )
    )
    await asyncio.sleep(0)
    payload = await (await client.get("/api/events")).json()
    pending = payload["permissions"][0]
    assert "Command: echo" in pending["details"]
    assert f"Working directory: {root.resolve()}" in pending["details"]
    assert "private-value" not in str(payload)
    assert "__permission_" not in pending["details"]
    assert pending["allow_command_session"] is True
    response = await client.post(
        "/api/permission",
        json={
            "id": pending["id"],
            "decision": "allow_command_session",
        },
    )
    assert response.status == 200
    assert await task == "allow_command_session"


@pytest.mark.parametrize("tool,marker", [("target_request", True), ("shell_command", False)])
async def test_command_allowance_cannot_be_submitted_for_ineligible_request(
    browser_api, tool, marker
):
    client, workspace, _, root = browser_api
    task = asyncio.create_task(
        workspace.confirm(
            SimpleNamespace(name=tool),
            {"workspace_root": str(root)},
            {"__permission_command_scope": marker},
        )
    )
    await asyncio.sleep(0)
    pending = (await (await client.get("/api/events")).json())["permissions"][0]
    response = await client.post(
        "/api/permission",
        json={
            "id": pending["id"],
            "decision": "allow_command_session",
        },
    )
    assert response.status == 400
    assert not task.done()
    assert (
        await client.post(
            "/api/permission",
            json={
                "id": pending["id"],
                "decision": "deny",
            },
        )
    ).status == 200
    assert await task == "deny"


async def test_browser_command_allowance_reuses_only_exact_command_and_directory(browser_api):
    from merlin_contracts import ToolContext
    from merlin_permissions import PermissionDecision, PermissionEngine, PermissionMode
    from merlin_tools import workspace_tool_broker

    client, workspace, _, root = browser_api

    class Store:
        def __init__(self):
            self.rules = []

        async def find_rule(self, request):
            return next(
                (
                    rule
                    for rule in self.rules
                    if rule.scope_fingerprint == request.scope.fingerprint
                    and rule.effect == request.effect
                ),
                None,
            )

        async def save_rule(self, rule):
            self.rules.append(rule)

        async def record_audit(self, audit):
            pass

    prompted = asyncio.Event()

    async def confirm(spec, context, arguments):
        prompted.set()
        # The daemon transport converts the returned wire string to this enum.
        return PermissionDecision(await workspace.confirm(spec, context, arguments))

    broker = workspace_tool_broker(
        permissions=PermissionEngine(PermissionMode.PROMPT, Store()),
        confirmer=confirm,
    )
    context = ToolContext(session_id="browser-session", workspace_root=str(root))
    (root / "child").mkdir()

    async def approve(args):
        prompted.clear()
        operation = asyncio.create_task(broker.invoke("shell_command", context, args))
        await asyncio.wait_for(prompted.wait(), 5)
        pending = (await (await client.get("/api/events")).json())["permissions"][0]
        assert (
            await client.post(
                "/api/permission",
                json={
                    "id": pending["id"],
                    "decision": "allow_command_session",
                },
            )
        ).status == 200
        assert not (await operation).is_error

    args = {"argv": ["/bin/echo", "safe-browser-fixture"], "cwd": "."}
    await approve(args)
    assert not (await asyncio.wait_for(broker.invoke("shell_command", context, args), 5)).is_error
    assert not workspace.permission_values
    await approve(args | {"argv": ["/bin/echo", "changed-argument"]})
    await approve(args | {"cwd": "child"})


async def test_question_snapshot_survives_refresh_and_clears_on_answer_or_session_reset(
    browser_api,
):
    import json

    client, workspace, _, _ = browser_api
    question = {
        "request_id": "s1:q1",
        "questions": [{"id": "auth", "question": "Choose authentication", "options": []}],
    }
    workspace.emit("user_input", json.dumps(question))
    # A reconnect with a cursor beyond the question still gets authoritative pending state.
    for cursor in (0, workspace.cursor):
        value = await (await client.get(f"/api/events?after={cursor}")).json()
        assert value["user_input"] == question
    workspace.emit("user_input_resolved", "s1:q1")
    assert (await (await client.get("/api/events")).json())["user_input"] is None
    workspace.emit("user_input", json.dumps(question))
    workspace.emit("session_reset", None)
    assert (await (await client.get("/api/events")).json())["user_input"] is None


async def test_structured_answer_cannot_bypass_validation_as_busy_turn_guidance(browser_api):
    from merlin_contracts.user_input import ANSWER_PREFIX

    client, workspace, backend, _ = browser_api
    workspace.turn = asyncio.create_task(workspace.produce("existing foreground turn"))
    await asyncio.sleep(0)
    response = await client.post(
        "/api/chat", json={"prompt": ANSWER_PREFIX + "{}", "request_id": "stale-answer"}
    )
    assert response.status == 400
    assert not any(call[0] == "steer" for call in backend.calls)
    backend.release.set()
    await workspace.turn


async def test_setup_reports_default_key_presence_without_returning_secret(
    browser_api, monkeypatch, tmp_path
):
    from merlin_config import ConfigPaths
    from merlin_tui.configuration import ProviderConfiguration

    paths = ConfigPaths(tmp_path / "default-provider")
    monkeypatch.setattr(ConfigPaths, "default", classmethod(lambda cls: paths))
    ProviderConfiguration(paths).save(
        "https://deepseek.test/v1", "deepseek-model", "private-default-key"
    )
    client, _, _, _ = browser_api
    response = await client.post("/api/rpc", json={"method": "setup_status"})
    raw = await response.text()
    result = (await response.json())["value"]
    assert result["provider"]["model"] == "deepseek-model"
    assert result["provider"]["key_saved"] is True
    assert "private-default-key" not in raw


async def test_chat_history_pages_and_resumed_sessions_use_durable_content(browser_api):
    from unittest.mock import AsyncMock

    client, workspace, backend, _ = browser_api
    backend.conversation_history = AsyncMock(
        return_value={
            "session_id": "saved",
            "messages": [{"id": "m1", "sequence": 1, "role": "user", "content": "Saved prompt"}],
            "before": 1,
            "has_more": False,
        }
    )
    workspace.emit("user", "/resume saved")
    workspace.emit("session_reset", "saved")
    workspace.external_busy = lambda: True
    response = await client.get("/api/history?initial=1")
    data = await response.json()
    assert data["messages"][0]["content"] == "Saved prompt"
    assert data["events"] == []
    assert backend.conversation_history.call_args.kwargs["exclude_current_turn"] is False
    workspace.emit("user", "Continue the assessment")
    workspace.emit("thinking", "Checking evidence")
    data = await (await client.get("/api/history?initial=1")).json()
    assert [e["kind"] for e in data["events"]] == ["user", "thinking"]
    assert backend.conversation_history.call_args.kwargs["exclude_current_turn"] is True
    await client.get("/api/history?before=42&session_id=saved")
    assert backend.conversation_history.call_args.kwargs == {
        "limit": 50,
        "before": 42,
        "session_id": "saved",
        "exclude_current_turn": False,
    }
    client.session.headers.pop("Cookie")
    assert (await client.get("/api/history?before=42")).status == 401


async def test_chat_history_retries_snapshot_when_turn_ends_during_read(browser_api):
    client, workspace, backend, _ = browser_api
    workspace.emit("user", "Continue")
    workspace.external_busy = lambda: True
    reads = []

    async def load(**kwargs):
        reads.append(kwargs)
        workspace.external_busy = lambda: False
        return {"messages": [{"id": "complete"}], "before": 1, "has_more": False}

    backend.conversation_history = load
    data = await (await client.get("/api/history?initial=1")).json()
    assert [r["exclude_current_turn"] for r in reads] == [True, False]
    assert data["events"] == []
    assert data["messages"] == [{"id": "complete"}]
