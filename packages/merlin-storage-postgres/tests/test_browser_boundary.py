from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from merlin_browser import (
    ApiReconHandoff,
    BrowserAction,
    BrowserBoundaryError,
    BrowserBounds,
    BrowserMetadata,
    BrowserOwner,
    TabMetadata,
    browser_tools,
    launch_isolated,
    restart_isolated,
)
from merlin_contracts import ToolContext
from merlin_permissions import (
    PermissionDecision,
    PermissionEngine,
    PermissionMode,
    PermissionScope,
)
from merlin_storage_postgres import (
    BrowserRepository,
    Database,
    DatabaseConfig,
    FindingRepository,
    MigrationRunner,
    PermissionRepository,
    PostgresBrowserStore,
    PostgresPermissionStore,
    SessionRepository,
)
from merlin_tools import ToolBroker


@asynccontextmanager
async def local_target():
    async def handler(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        path = request.split(b" ", 2)[1]
        if path == b"/frame":
            body = b'<button aria-label="Frame-only control">Frame</button>'
        elif path == b"/api":
            body = b'{"status":"ok"}'
        else:
            body = (
                b'<button aria-label="Inspect API">Inspect API</button>'
                b'<a aria-label="Open API result" target="_blank" href="/result">result</a>'
                b'<iframe src="/frame"></iframe>'
            )
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


def test_real_chromium_pg0_restart_cancel_and_evidence(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session_id = SessionRepository(connection).create("browser").id
            asyncio.run(_journey(connection, session_id))


def test_handoff_evidence_verification_is_target_bound_across_restart(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-handoff-verification"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session_id = str(SessionRepository(connection).create("handoff verification").id)
            owner = BrowserOwner(
                session_id=session_id,
                worker_id="worker:recon",
                technique_id="api-first",
            )
            now = datetime.now(UTC)
            metadata = BrowserMetadata(
                context_id="context-1",
                mode="isolated",
                target_id="target-1",
                owner=owner,
                allowed_origins=frozenset({"https://target.test"}),
                tabs=(TabMetadata(tab_ref="tab-1", url="https://target.test/chat"),),
                state="active",
                created_at=now,
                updated_at=now,
            )
            recon_observation = {
                "schema": "merlin.browser.recon-observation.v1",
                "name": "score",
                "role": "score",
                "baseline": 85,
                "result": 100,
                "baseline_pointer": "/turn/score",
                "result_pointer": "/turn/score",
                "outcome": "observed",
                "success_value_matched": True,
                "result_observed_at": now.isoformat(),
                "evidence_refs": [
                    "browser-api-response:sha256:" + "b" * 64
                ],
            }
            recon_observation["digest"] = "sha256:" + hashlib.sha256(
                json.dumps(recon_observation, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            handoff = ApiReconHandoff(
                id="handoff:verified",
                context_id=metadata.context_id,
                target_id=metadata.target_id,
                owner=owner,
                method="POST",
                url="https://target.test/chat",
                request_evidence_ref="request:verified",
                response_evidence_ref="response:verified",
                adapter='{"interface":"http_api"}',
                reusable_adapter=('{"endpoint":"https://target.test/chat","interface":"http_api"}'),
                reusable_site_facts=(
                    '{"authentication_flow":"signed-in cookie",'
                    '"entry_url":"https://target.test/chat"}'
                ),
                recon_scope={
                    "surface": "support-chat",
                    "authentication_context": "signed-in-user",
                    "tenant": "tenant-a",
                    "agent_or_bot": "support-bot",
                    "session_constraint": "one conversation",
                    "oracle_contract": "reply rendered",
                },
                recon_observations=(recon_observation,),
            )
            store = PostgresBrowserStore(connection)
            asyncio.run(store.save_metadata(metadata))
            asyncio.run(
                store.record_evidence(
                    metadata.context_id,
                    handoff.request_evidence_ref,
                    "network_request",
                    {"method": "POST"},
                )
            )
            asyncio.run(
                store.record_evidence(
                    metadata.context_id,
                    recon_observation["evidence_refs"][0],
                    "network_response",
                    {"status": 200, "score": 100},
                )
            )
            asyncio.run(
                store.record_evidence(
                    metadata.context_id,
                    handoff.response_evidence_ref,
                    "network_response",
                    {"status": 200},
                )
            )
            asyncio.run(
                store.record_evidence(
                    metadata.context_id,
                    "snapshot:supplemental",
                    "snapshot",
                    {"url": "https://target.test/chat"},
                )
            )
            asyncio.run(store.record_handoff(handoff))

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            MigrationRunner(connection).apply()
            store = PostgresBrowserStore(connection)
            assert asyncio.run(
                store.verify_handoff_evidence(
                    handoff.id,
                    handoff.target_id,
                    (handoff.request_evidence_ref, handoff.response_evidence_ref),
                )
            )
            assert asyncio.run(
                store.verify_handoff_evidence(
                    handoff.id,
                    handoff.target_id,
                    (
                        handoff.request_evidence_ref,
                        handoff.response_evidence_ref,
                        "snapshot:supplemental",
                    ),
                )
            )
            assert not asyncio.run(
                store.verify_handoff_evidence(
                    handoff.id, "target-forged", (handoff.request_evidence_ref,)
                )
            )
            assert not asyncio.run(
                store.verify_handoff_evidence(
                    handoff.id,
                    handoff.target_id,
                    (handoff.request_evidence_ref,),
                )
            )
            assert not asyncio.run(
                store.verify_handoff_evidence(handoff.id, handoff.target_id, ("evidence:forged",))
            )
            recalled = asyncio.run(store.list_handoffs(handoff.target_id))
            assert [item.id for item in recalled] == [handoff.id]
            assert recalled[0].recon_scope["agent_or_bot"] == "support-bot"
            assert recalled[0].recon_observations == handoff.recon_observations
            assert recalled[0].reusable_adapter == handoff.reusable_adapter
            assert recalled[0].reusable_site_facts == handoff.reusable_site_facts

            connection.execute(
                "DELETE FROM merlin_storage.browser_evidence WHERE evidence_ref = %s",
                (handoff.response_evidence_ref,),
            )
            connection.commit()
            assert asyncio.run(store.list_handoffs(handoff.target_id)) == ()


async def _journey(connection, session_id: str) -> None:
    store = PostgresBrowserStore(connection)
    owner = BrowserOwner(
        session_id=session_id,
        worker_id="worker:recon",
        technique_id="api-first",
    )
    async with local_target() as origin:
        bounds = BrowserBounds(target_id="target", allowed_origins={origin})
        browser = await launch_isolated(bounds, owner)
        tab = await browser.new_tab(origin)
        await store.save_metadata(browser.metadata(restart=True))
        action = BrowserAction(
            context_id=browser.context_id,
            target_id="target",
            owner=owner,
            kind="snapshot",
            tab_ref=tab,
            step=1,
        )
        observation = await browser.act(action)
        inspect = next(element for element in observation.elements if element.name == "Inspect API")
        await browser.snapshot(tab)
        stale = action.model_copy(update={"kind": "click", "element_ref": inspect.ref, "step": 2})
        recovered_click = await browser.act(stale)
        assert recovered_click.kind == "snapshot"
        current = await browser.snapshot(tab)
        popup_link = next(
            element for element in current.elements if element.name == "Open API result"
        )
        await browser.act(
            action.model_copy(update={"kind": "click", "element_ref": popup_link.ref, "step": 3})
        )
        await asyncio.sleep(0.05)
        assert len(browser.metadata().tabs) == 2
        frame_ref = next(reference for reference in browser.frame_refs(tab) if reference != "main")
        frame = await browser.snapshot(tab, frame_ref)
        assert [element.name for element in frame.elements] == ["Frame-only control"]
        permissions = PermissionEngine(
            PermissionMode.PROMPT,
            PostgresPermissionStore(PermissionRepository(connection)),
        )

        async def approve(*_):
            return PermissionDecision.ALLOW_SESSION

        broker = ToolBroker(
            browser_tools(lambda _: browser, store),
            confirmer=approve,
            permissions=permissions,
            permission_scope=lambda name, context, _: PermissionScope(
                context.session_id,
                name,
                context.workspace_root,
                target="target",
            ),
        )
        broker_result = await broker.invoke(
            "browser_snapshot",
            ToolContext(session_id=session_id, workspace_root="/workspace"),
            {"tab_ref": tab, "step": 4},
        )
        assert not broker_result.is_error
        evidence_ref = "network:request:1"
        accessibility_ref = "accessibility:snapshot:1"
        observation = observation.model_copy(
            update={"evidence_refs": (evidence_ref, accessibility_ref)}
        )
        await store.append_action(action)
        await store.record_evidence(
            browser.context_id,
            evidence_ref,
            "network_request",
            {"method": "GET", "url": origin + "/api"},
        )
        await store.record_evidence(
            browser.context_id,
            accessibility_ref,
            "accessibility_snapshot",
            {"roles": [element.role for element in observation.elements]},
        )
        response_ref = "network:response:1"
        await store.record_evidence(
            browser.context_id,
            response_ref,
            "network_response",
            {"status": 200, "content_type": "application/json"},
        )
        await store.append_observation(observation)
        api_handoff = ApiReconHandoff(
            id="handoff:1",
            context_id=browser.context_id,
            target_id="target",
            owner=owner,
            method="GET",
            url=origin + "/api",
            request_evidence_ref=evidence_ref,
            response_evidence_ref=response_ref,
            adapter='{"interface":"http_api"}',
        )
        await store.record_handoff(api_handoff)
        await store.record_handoff(
            api_handoff.model_copy(update={"created_at": api_handoff.created_at})
        )
        await store.record_handoff(
            ApiReconHandoff(
                id="handoff:browser-only",
                context_id=browser.context_id,
                target_id="target",
                owner=owner,
                method="GET",
                url=origin + "/frame",
                request_evidence_ref=evidence_ref,
                response_evidence_ref=response_ref,
                dom_fallback_reason="iframe control has no equivalent API operation",
            )
        )
        await browser.close()

        metadata = await store.load_metadata(browser.context_id)
        assert metadata is not None and metadata.state == "restart_required"
        other_owner = owner.model_copy(update={"worker_id": "worker:api-specialist"})
        with pytest.raises(BrowserBoundaryError, match="current bounds"):
            await restart_isolated(metadata, bounds, other_owner)
        recovered = await restart_isolated(metadata, bounds, owner)
        assert tab in {item.tab_ref for item in recovered.metadata().tabs}

        entered = asyncio.Event()

        async def cancellable() -> None:
            async with recovered:
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(cancellable())
        await entered.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await store.save_metadata(recovered.metadata())
        cleaned = await store.load_metadata(recovered.context_id)
        assert cleaned is not None and cleaned.state == "cleaned"
        loaded = await store.list_observations(recovered.context_id)
        assert loaded[0].evidence_refs == (evidence_ref, accessibility_ref)
        actions = await store.list_actions(recovered.context_id)
        assert [(item.kind, item.step) for item in actions] == [
            ("snapshot", 1),
            ("snapshot", 4),
        ]
        handoff = await store.load_handoff("handoff:1")
        assert handoff is not None and handoff.adapter == '{"interface":"http_api"}'
        browser_only = await store.load_handoff("handoff:browser-only")
        assert browser_only is not None and browser_only.adapter is None
        assert browser_only.dom_fallback_reason == (
            "iframe control has no equivalent API operation"
        )
        # Browser observations and recon handoffs remain evidence until a worker
        # explicitly emits a FindingDraft; persistence never auto-promotes them.
        assert FindingRepository(connection).list(session_id) == []


def _fill_action(*, value: str | None, redaction_status: str = "not_needed") -> BrowserAction:
    return BrowserAction(
        context_id="browser-context",
        target_id="target",
        owner=BrowserOwner(session_id="s1", worker_id="a1", technique_id="recon"),
        kind="fill",
        tab_ref="tab:1",
        element_ref="tab:1:1:0",
        value=value,
        step=1,
        redaction_status=redaction_status,  # type: ignore[arg-type]
    )


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []
        self.commits = 0

    def execute(self, sql: str, params: object):
        self.statements.append((sql, params))
        return self

    def commit(self) -> None:
        self.commits += 1


def test_append_action_persists_non_sensitive_fill_verbatim() -> None:
    connection = _RecordingConnection()
    action_id = BrowserRepository(connection).append_action(_fill_action(value="harmless probe"))
    assert action_id
    assert connection.commits == 1
    assert len(connection.statements) == 1


def test_append_action_rejects_unredacted_value_marked_redacted() -> None:
    connection = _RecordingConnection()
    with pytest.raises(ValueError, match="must be redacted before persistence"):
        BrowserRepository(connection).append_action(
            _fill_action(value="hunter2", redaction_status="redacted")
        )
    assert connection.statements == []
    assert connection.commits == 0


def test_append_action_accepts_redacted_placeholder() -> None:
    connection = _RecordingConnection()
    BrowserRepository(connection).append_action(
        _fill_action(value="[redacted]", redaction_status="redacted")
    )
    assert connection.commits == 1
