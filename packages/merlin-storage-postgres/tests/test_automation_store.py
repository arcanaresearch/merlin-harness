from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from merlin_agents.automation import (
    ArtifactAdmission,
    ArtifactExecutor,
    ArtifactRun,
    AutomationArtifact,
    AutomationAudit,
    AutomationError,
    AutomationPipeline,
    AutomationStep,
    RunState,
)
from merlin_contracts import TechniqueAttempt, TechniqueAttemptControlKind
from merlin_storage_postgres import (
    Database,
    DatabaseConfig,
    MigrationRunner,
    PostgresAutomationJournal,
    TechniqueAttemptRepository,
)


def test_postgres_automation_lifecycle_is_restart_safe_and_attempt_bound(tmp_path: Path) -> None:
    attempt_id = str(uuid4())
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            TechniqueAttemptRepository(connection).begin(
                TechniqueAttempt(
                    id=attempt_id,
                    campaign_id="campaign-1",
                    wave=0,
                    task_id="task-1",
                    technique_id="technique-1",
                    strategy_id="direct",
                    control_kind=TechniqueAttemptControlKind.ATTACK,
                    oracle_digest="sha256:" + "a" * 64,
                )
            )
            journal = PostgresAutomationJournal(connection)
            pipeline = AutomationPipeline(journal)
            authored = pipeline.author(
                AutomationArtifact(
                    request_id="request-1",
                    owning_attempt_id=attempt_id,
                    author_identity="author-1",
                    author_model="model-author",
                    origin="https://reviewed.example",
                    max_steps=1,
                    steps=(
                        AutomationStep(
                            step_id="exchange",
                            operation="browser_chat_exchange",
                            arguments={"prompt": "fixture"},
                            evidence_keys=("response",),
                        ),
                    ),
                )
            )
            audit = AutomationAudit(
                artifact_digest=authored.digest,
                auditor_identity="auditor-1",
                auditor_model="model-auditor",
                decision="accept",
                reasons=("bounded and fixture-backed",),
                static_check_refs=("static:1",),
                fixture_evidence_refs=("fixture:1",),
            )
            audited = pipeline.audit(authored, audit)
            admission = ArtifactAdmission(
                artifact_digest=authored.digest,
                owning_attempt_id=attempt_id,
                origin=authored.origin,
                allowed_operations=frozenset({"browser_chat_exchange"}),
                environment_digest="sha256:" + "b" * 64,
            )
            admitted = pipeline.admit(audited, audit, admission)

            async def execute() -> None:
                result = await ArtifactExecutor(
                    PostgresAutomationJournal(connection),
                    lambda _step: {
                        "status": "completed",
                        "effect_state": "committed",
                        "evidence": {"response": "evidence:exchange"},
                    },
                ).execute("run-1", admitted, admission)
                assert result.state is RunState.COMPLETED

            asyncio.run(execute())

            row = connection.execute(
                """SELECT artifact_digest, owning_attempt_id, state, evidence_refs
                   FROM merlin_storage.automation_artifact_runs WHERE run_id = 'run-1'"""
            ).fetchone()
            assert row["artifact_digest"] == admitted.digest
            assert str(row["owning_attempt_id"]) == attempt_id
            assert row["state"] == "completed"
            assert row["evidence_refs"] == ["evidence:exchange"]

            journal.record_artifact(authored)
            journal.record_audit(audit)
            journal.record_admission(admission)
            with pytest.raises(AutomationError, match="terminal or bound"):
                journal.record_run(
                    ArtifactRun(
                        run_id="run-1",
                        artifact_digest=admitted.digest,
                        owning_attempt_id=attempt_id,
                        state=RunState.FAILED,
                        error="changed after terminal",
                    )
                )
