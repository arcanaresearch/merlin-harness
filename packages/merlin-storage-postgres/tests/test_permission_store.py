from pathlib import Path

import pytest
from merlin_permissions import (
    PermissionDecision,
    PermissionEffect,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    PermissionScope,
)
from merlin_storage_postgres import (
    Database,
    DatabaseConfig,
    MigrationRunner,
    PermissionRepository,
    PostgresPermissionStore,
    SessionRepository,
)


@pytest.mark.asyncio
async def test_postgres_persists_scoped_rule_and_readable_audit(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("permissions")
            store = PostgresPermissionStore(PermissionRepository(connection))

            async def allow_session(_request):
                return PermissionDecision.ALLOW_SESSION

            engine = PermissionEngine(PermissionMode.PROMPT, store, resolver=allow_session)
            request = PermissionRequest(
                PermissionEffect.WORKSPACE_WRITE,
                PermissionScope(
                    session.id,
                    "write",
                    str(tmp_path),
                    operation_group="workspace-mutations",
                ),
            )

            assert await engine.authorize(request)
            assert await engine.authorize(request)
            history = await store.list_audit(session.id)

    assert len(history) == 2
    assert history[0].reason == "matching session rule"
    assert history[1].decision is PermissionDecision.ALLOW_SESSION
    assert history[0].request.scope.operation_group == "workspace-mutations"
