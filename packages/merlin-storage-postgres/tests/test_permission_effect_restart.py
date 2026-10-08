from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from merlin_contracts import CleanupReceipt, ToolEffectRecord, ToolEffectState
from merlin_permissions import (
    PermissionDecision,
    PermissionEffect,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    PermissionRule,
    PermissionScope,
)
from merlin_storage_postgres import (
    Database,
    DatabaseConfig,
    MigrationRunner,
    PermissionRepository,
    PostgresPermissionStore,
    PostgresToolEffectStore,
    SessionRepository,
    ToolEffectRepository,
)


@pytest.mark.asyncio
async def test_permissions_effects_and_cleanup_survive_postgres_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres"
    effect_key = sha256(b"restart-effect").hexdigest()
    cleanup_key = sha256(b"restart-cleanup").hexdigest()
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session_id = SessionRepository(connection).create("restart boundary").id
            permissions = PostgresPermissionStore(PermissionRepository(connection))
            expired = PermissionRequest(
                PermissionEffect.WORKSPACE_WRITE,
                PermissionScope(session_id, "write", str(tmp_path)),
            )
            denied = PermissionRequest(
                PermissionEffect.TARGET_NETWORK,
                PermissionScope(session_id, "target", str(tmp_path), target="reviewed"),
            )
            await permissions.save_rule(
                PermissionRule(
                    session_id,
                    expired.scope.rule_key,
                    expired.effect,
                    expired.scope.fingerprint,
                    PermissionDecision.ALLOW_SESSION,
                    datetime.now(UTC) - timedelta(seconds=1),
                )
            )
            await permissions.save_rule(
                PermissionRule(
                    session_id,
                    denied.scope.rule_key,
                    denied.effect,
                    denied.scope.fingerprint,
                    PermissionDecision.DENY_SESSION,
                )
            )
            effects = PostgresToolEffectStore(ToolEffectRepository(connection))
            intent = ToolEffectRecord(
                idempotency_key=effect_key,
                session_id=session_id,
                call_id="call-restart",
                tool_name="target",
                effect="target_network",
                arguments_digest=sha256(b"reviewed arguments").hexdigest(),
                state=ToolEffectState.INTENT,
            )
            started, created = await effects.begin(intent)
            assert created
            await effects.finish(
                started.model_copy(
                    update={
                        "state": ToolEffectState.ACKNOWLEDGED,
                        "result_content": "accepted once",
                        "result_is_error": False,
                    }
                )
            )
            await effects.record_cleanup(
                CleanupReceipt(
                    idempotency_key=cleanup_key,
                    session_id=session_id,
                    resource_kind="browser_context",
                    resource_id="context-restart",
                    state="closed",
                    details={"reason": "campaign terminal"},
                )
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted_database:
        with restarted_database.connect() as connection:
            MigrationRunner(connection).apply()
            permissions = PostgresPermissionStore(PermissionRepository(connection))
            engine = PermissionEngine(PermissionMode.PROMPT, permissions)
            assert not await engine.authorize(expired)
            assert not await engine.authorize(denied)
            reasons = [row.reason for row in await permissions.list_audit(session_id)]
            assert "session rule expired" in reasons
            assert "matching session rule" in reasons

            effects = PostgresToolEffectStore(ToolEffectRepository(connection))
            replayed, created = await effects.begin(intent)
            assert not created
            assert replayed.state is ToolEffectState.ACKNOWLEDGED
            assert replayed.result_content == "accepted once"
            cleanup = connection.execute(
                """SELECT state, details FROM merlin_storage.cleanup_receipts
                   WHERE idempotency_key = %s""",
                (cleanup_key,),
            ).fetchone()
            assert cleanup["state"] == "closed"
            assert cleanup["details"] == {"reason": "campaign terminal"}
