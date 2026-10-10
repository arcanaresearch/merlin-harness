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


@pytest.mark.asyncio
async def test_postgres_exact_command_grant_survives_restart_without_broadening(
    tmp_path: Path,
) -> None:
    config = DatabaseConfig(data_dir=tmp_path / "postgres")
    with Database(config) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("command permissions")
            scope = PermissionScope(session.id, "shell_command", str(tmp_path))
            command_scope = PermissionScope(
                session.id,
                "shell_command",
                str(tmp_path),
                operation_group="command:sha256:original-command-and-directory",
            )
            request = PermissionRequest(PermissionEffect.SUBPROCESS, scope, command_scope)
            store = PostgresPermissionStore(PermissionRepository(connection))

            async def allow_command(_request):
                return PermissionDecision.ALLOW_COMMAND_SESSION

            engine = PermissionEngine(PermissionMode.PROMPT, store, resolver=allow_command)
            assert await engine.authorize(request)
            assert await store.find_rule(PermissionRequest(request.effect, scope)) is None

    with Database(config) as restarted_database:
        with restarted_database.connect() as connection:
            store = PostgresPermissionStore(PermissionRepository(connection))
            prompts = []

            async def deny_changed_request(changed_request):
                prompts.append(changed_request)
                return PermissionDecision.DENY_ONCE

            engine = PermissionEngine(PermissionMode.PROMPT, store, resolver=deny_changed_request)
            assert await engine.authorize(request)
            assert prompts == []
            changed_request = PermissionRequest(
                PermissionEffect.SUBPROCESS,
                scope,
                PermissionScope(
                    session.id,
                    "shell_command",
                    str(tmp_path),
                    operation_group="command:sha256:different-command-or-directory",
                ),
            )
            assert not await engine.authorize(changed_request)
            assert prompts == [changed_request]
            history = await store.list_audit(session.id)
            command_audits = [
                audit
                for audit in history
                if audit.decision is PermissionDecision.ALLOW_COMMAND_SESSION
            ]
            assert len(command_audits) == 2
            assert all(audit.request.scope == command_scope for audit in command_audits)
