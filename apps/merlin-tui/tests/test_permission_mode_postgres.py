from types import SimpleNamespace
from uuid import UUID

import pytest
from merlin_config import Config
from merlin_kernel import Kernel
from merlin_permissions import PermissionEngine, PermissionMode, PermissionService
from merlin_storage_postgres import (
    Database,
    DatabaseConfig,
    MigrationRunner,
    PermissionRepository,
    PostgresPermissionStore,
    PostgresSessionStore,
    SessionRepository,
)
from merlin_tui.kernel_backend import KernelBackend, KernelSettings
from psycopg import OperationalError
from psycopg.errors import CheckViolation


@pytest.fixture(scope="module")
def postgres(tmp_path_factory):
    with Database(DatabaseConfig(data_dir=tmp_path_factory.mktemp("mode-postgres"))) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
        yield database


@pytest.fixture
def runtime(postgres):
    with postgres.connect() as session_connection, postgres.connect() as audit_connection:
        store = PostgresPermissionStore(PermissionRepository(audit_connection))
        engine = PermissionEngine(PermissionMode.PROMPT, store)
        service = PermissionService(engine, store)
        settings = KernelSettings(Config(values={"permission_mode": "prompt"}), "test")
        kernel = Kernel(
            SimpleNamespace(),
            PostgresSessionStore(session_connection),
            SimpleNamespace(),
            settings,
            permissions=service,
        )
        backend = KernelBackend(kernel, settings=settings, permissions=service)
        yield SimpleNamespace(
            database=postgres,
            audit_connection=audit_connection,
            engine=engine,
            service=service,
            settings=settings,
            kernel=kernel,
            backend=backend,
        )


def audit_rows(runtime):
    with runtime.database.connect() as connection:
        return PermissionRepository(connection).list_audit(runtime.kernel.session_id)


@pytest.mark.parametrize("route", ["command", "backend", "reload"])
@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("fail_audit", [False, True])
async def test_mode_paths_use_real_session_and_preserve_mode_on_audit_failure(
    runtime,
    route,
    resumed,
    fail_audit,
):
    if resumed:
        with runtime.database.connect() as connection:
            session = SessionRepository(connection).create("resumed mode session")
        await runtime.kernel.start(session.id)
    if fail_audit:
        # Real PostgreSQL connection failure at the audit write, not a mock store.
        runtime.audit_connection.close()

    async def change():
        if route == "command":
            await runtime.kernel.submit("/mode yolo")
            events = []
            while not runtime.kernel._events.empty():
                events.append(await runtime.kernel.next_event())
            if fail_audit:
                assert any(
                    e.type == "error" and "/mode failed" in e.payload["text"] for e in events
                )
                assert not any(e.type == "command" for e in events)
            else:
                assert any(e.type == "command" and '"yolo"' in e.payload["text"] for e in events)
        elif route == "backend":
            assert await runtime.backend.set_permission_mode("yolo") == "yolo"
        else:
            await runtime.backend.reload_configuration(Config(values={"permission_mode": "yolo"}))

    if fail_audit and route != "command":
        with pytest.raises(OperationalError, match="connection is closed"):
            await change()
    else:
        await change()

    assert str(UUID(runtime.kernel.session_id)) == runtime.kernel.session_id
    assert await runtime.service.get_mode() == ("prompt" if fail_audit else "yolo")
    rows = audit_rows(runtime)
    assert len(rows) == (0 if fail_audit else 1)
    if rows:
        assert rows[0].request.scope.session_id == runtime.kernel.session_id
        assert rows[0].request.scope.tool_name == "permission.mode"
        assert rows[0].mode is PermissionMode.YOLO
    if route == "reload":
        assert runtime.settings.config.values["permission_mode"] == (
            "prompt" if fail_audit else "yolo"
        )


@pytest.mark.parametrize("route", ["chat", "rpc"])
@pytest.mark.parametrize("fail_audit", [False, True])
async def test_web_mode_switch_with_postgres(runtime, tmp_path, route, fail_audit):
    # Web is optional on the mainline; run with the web branch on PYTHONPATH.
    server = pytest.importorskip("merlin_web.server")
    from aiohttp.test_utils import TestClient, TestServer

    workspace = server.Workspace(runtime.backend)
    if fail_audit:
        runtime.audit_connection.close()
    async with TestClient(TestServer(server.create_app(workspace, tmp_path))) as client:
        client.session.headers["Cookie"] = f"{workspace.cookie_name}={workspace.token}"
        if route == "rpc":
            response = await client.post(
                "/api/rpc",
                json={"method": "set_permission_mode", "args": ["yolo"]},
            )
            assert response.status == (500 if fail_audit else 200)
            if not fail_audit:
                assert await response.json() == {"value": "yolo"}
        else:
            response = await client.post(
                "/api/chat",
                json={"prompt": "/mode yolo", "request_id": "mode"},
            )
            assert response.status == 200
            await workspace.turn
            assert any(e["kind"] == ("error" if fail_audit else "status") for e in workspace.events)
        assert await runtime.service.get_mode() == ("prompt" if fail_audit else "yolo")
        rows = audit_rows(runtime)
        assert len(rows) == (0 if fail_audit else 1)
        if rows:
            assert rows[0].request.scope.session_id == runtime.kernel.session_id


async def test_database_rejects_mode_audit_without_changing_mode(runtime):
    await runtime.kernel.start()
    connection = runtime.audit_connection
    connection.execute(
        "ALTER TABLE merlin_storage.permission_audit ADD CONSTRAINT reject_mode_test "
        "CHECK (tool_name <> 'permission.mode') NOT VALID"
    )
    connection.commit()
    try:
        with pytest.raises(CheckViolation):
            await runtime.service.set_mode("yolo", session_id=runtime.kernel.session_id)
        assert await runtime.service.get_mode() == "prompt"
        connection.rollback()
        assert audit_rows(runtime) == []
    finally:
        connection.rollback()
        connection.execute(
            "ALTER TABLE merlin_storage.permission_audit DROP CONSTRAINT reject_mode_test"
        )
        connection.commit()


@pytest.mark.parametrize("fail_audit", [False, True])
async def test_tui_mode_command_with_postgres(runtime, tmp_path, fail_audit):
    from merlin_tui.app import MerlinApp
    from merlin_tui.backend import DemoBackend
    from merlin_tui.client_state import ClientState

    class ModeBackend(DemoBackend):
        async def get_permission_mode(self):
            return await runtime.backend.get_permission_mode()

        async def set_permission_mode(self, mode):
            return await runtime.backend.set_permission_mode(mode)

    if fail_audit:
        runtime.audit_connection.close()
    app = MerlinApp(ModeBackend(), client_state=ClientState(tmp_path / "client.json"))
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer")
        composer.text = "/mode yolo"
        await pilot.press("enter")
        await pilot.pause()
        if app._stream_task is not None:
            await app._stream_task
        transcript = "\n".join(line.text for line in app.query_one("#transcript").lines)
        assert ("Mode failed" in transcript) is fail_audit
        assert app._permission_mode == ("prompt" if fail_audit else "yolo")
        assert await runtime.service.get_mode() == app._permission_mode
        rows = audit_rows(runtime)
        assert len(rows) == (0 if fail_audit else 1)
        if rows:
            assert rows[0].request.scope.session_id == runtime.kernel.session_id


async def test_auto_shell_reads_and_prompted_writes_are_audited_in_postgres(runtime, tmp_path):
    from merlin_contracts import ToolContext
    from merlin_permissions import PermissionEffect
    from merlin_tools import workspace_tool_broker

    session_id = await runtime.kernel.start()
    await runtime.service.set_mode("auto", session_id=session_id)
    prompts = []

    async def deny(spec, context, arguments):
        prompts.append(arguments)
        return False

    broker = workspace_tool_broker(permissions=runtime.engine, confirmer=deny)
    context = ToolContext(session_id=session_id, workspace_root=str(tmp_path))
    (tmp_path / "sample.txt").write_text("read fixture")
    listing = await broker.invoke("shell_command", context, {"argv": ["ls", "-la"]})
    content = await broker.invoke("shell_command", context, {"argv": ["cat", "sample.txt"]})
    assert not listing.is_error and not content.is_error
    assert content.content == "read fixture"
    assert prompts == []
    denied = await broker.invoke("shell_command", context, {"argv": ["rm", "sample.txt"]})
    assert denied.is_error and len(prompts) == 1
    assert (tmp_path / "sample.txt").exists()
    audits = audit_rows(runtime)
    reads = [row for row in audits if row.reason == "auto mode verified read-only command"]
    assert len(reads) == 2
    assert all(row.request.effect is PermissionEffect.SUBPROCESS for row in reads)
    assert all(row.request.scope.session_id == session_id for row in reads)
