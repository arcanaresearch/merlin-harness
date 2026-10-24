from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from merlin_agents.automation import (
    ArtifactAdmission,
    ArtifactEvent,
    ArtifactRun,
    AutomationArtifact,
    AutomationAudit,
    AutomationError,
    AutomationOutcome,
    AutomationStep,
    RunState,
)
from psycopg.types.json import Jsonb


class PostgresAutomationJournal:
    """Restart-safe automation journal implementing the agent pipeline protocol."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def load_outcome(
        self, run_id: str, request_id: str, owning_attempt_id: str
    ) -> AutomationOutcome | None:
        """Recover a deterministic run without re-authoring or repeating target effects."""
        row = self.connection.execute(
            """SELECT r.*, a.request_id FROM merlin_storage.automation_artifact_runs r
               JOIN merlin_storage.automation_artifacts a ON a.digest = r.artifact_digest
               WHERE r.run_id = %s""",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        if row["request_id"] != request_id or str(row["owning_attempt_id"]) != owning_attempt_id:
            raise AutomationError("persisted automation run belongs to another request or attempt")
        run = ArtifactRun(
            run_id=run_id,
            artifact_digest=row["artifact_digest"],
            owning_attempt_id=owning_attempt_id,
            state=RunState(row["state"]),
            events=tuple(
                ArtifactEvent(**{**event, "evidence_refs": tuple(event.get("evidence_refs", ()))})
                for event in row["events"]
            ),
            evidence_refs=tuple(row["evidence_refs"]),
            error=row["error"],
        )
        incomplete = run.state is RunState.EXECUTING
        return AutomationOutcome(
            request_id=request_id,
            owning_attempt_id=owning_attempt_id,
            stage="execution",
            state="ambiguous" if incomplete else run.state.value,
            artifact_digest=run.artifact_digest,
            run=run,
            error=(
                "interrupted automation run may have committed effects; replay refused"
                if incomplete
                else run.error
            ),
        )

    def admits(
        self,
        artifact_digest: str,
        owning_attempt_id: str,
        operation: str,
    ) -> bool:
        """Resolve the exact sealed executor capability without trusting model output."""
        row = self.connection.execute(
            """SELECT 1
                 FROM merlin_storage.automation_artifact_admissions
                WHERE artifact_digest = %s
                  AND owning_attempt_id = %s
                  AND %s = ANY(allowed_operations)""",
            (artifact_digest, owning_attempt_id, operation),
        ).fetchone()
        return row is not None

    def record_artifact(self, artifact: AutomationArtifact) -> None:
        payload = {
            "steps": [_step_payload(step) for step in artifact.steps],
            "state": artifact.state.value,
        }
        with self.connection.transaction():
            row = self.connection.execute(
                """INSERT INTO merlin_storage.automation_artifacts
                       (digest, request_id, owning_attempt_id, author_identity, author_model,
                        origin, schema_id, max_steps, payload)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (digest) DO NOTHING RETURNING digest""",
                (
                    artifact.digest,
                    artifact.request_id,
                    artifact.owning_attempt_id,
                    artifact.author_identity,
                    artifact.author_model,
                    artifact.origin,
                    artifact.schema,
                    artifact.max_steps,
                    Jsonb(payload),
                ),
            ).fetchone()
            if row is None:
                stored = self.connection.execute(
                    "SELECT * FROM merlin_storage.automation_artifacts WHERE digest = %s",
                    (artifact.digest,),
                ).fetchone()
                identity = (
                    stored["request_id"],
                    str(stored["owning_attempt_id"]),
                    stored["author_identity"],
                    stored["author_model"],
                    stored["origin"],
                    stored["schema_id"],
                    stored["max_steps"],
                    stored["payload"],
                )
                expected = (
                    artifact.request_id,
                    artifact.owning_attempt_id,
                    artifact.author_identity,
                    artifact.author_model,
                    artifact.origin,
                    artifact.schema,
                    artifact.max_steps,
                    payload,
                )
                if identity != expected:
                    raise AutomationError("artifact digest conflicts with persisted content")

    def record_audit(self, audit: AutomationAudit) -> None:
        author = self.connection.execute(
            "SELECT author_identity FROM merlin_storage.automation_artifacts WHERE digest = %s",
            (audit.artifact_digest,),
        ).fetchone()
        if author is None:
            raise AutomationError("cannot audit an unknown automation artifact")
        if author["author_identity"] == audit.auditor_identity:
            raise AutomationError("artifact author cannot audit their own artifact")
        payload_digest = _record_digest(asdict(audit))
        self._insert_digest_bound(
            """INSERT INTO merlin_storage.automation_artifact_audits
                   (artifact_digest, auditor_identity, auditor_model, decision, reasons,
                    static_check_refs, fixture_evidence_refs, payload_digest)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (artifact_digest) DO NOTHING RETURNING payload_digest""",
            (
                audit.artifact_digest,
                audit.auditor_identity,
                audit.auditor_model,
                audit.decision,
                list(audit.reasons),
                list(audit.static_check_refs),
                list(audit.fixture_evidence_refs),
                payload_digest,
            ),
            "automation_artifact_audits",
            audit.artifact_digest,
            payload_digest,
        )

    def record_admission(self, admission: ArtifactAdmission) -> None:
        payload_digest = _record_digest(
            {
                "artifact_digest": admission.artifact_digest,
                "owning_attempt_id": admission.owning_attempt_id,
                "origin": admission.origin,
                "allowed_operations": sorted(admission.allowed_operations),
                "environment_digest": admission.environment_digest,
            }
        )
        self._insert_digest_bound(
            """INSERT INTO merlin_storage.automation_artifact_admissions
                   (artifact_digest, owning_attempt_id, origin, allowed_operations,
                    environment_digest, payload_digest)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (artifact_digest) DO NOTHING RETURNING payload_digest""",
            (
                admission.artifact_digest,
                admission.owning_attempt_id,
                admission.origin,
                sorted(admission.allowed_operations),
                admission.environment_digest,
                payload_digest,
            ),
            "automation_artifact_admissions",
            admission.artifact_digest,
            payload_digest,
        )

    def record_run(self, run: ArtifactRun) -> None:
        events = [asdict(event) for event in run.events]
        terminal = run.state is not RunState.EXECUTING
        with self.connection.transaction():
            row = self.connection.execute(
                """INSERT INTO merlin_storage.automation_artifact_runs
                       (run_id, artifact_digest, owning_attempt_id, state, events,
                        evidence_refs, error, terminal_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN now() ELSE NULL END)
                   ON CONFLICT (run_id) DO UPDATE SET
                       state = EXCLUDED.state, events = EXCLUDED.events,
                       evidence_refs = EXCLUDED.evidence_refs, error = EXCLUDED.error,
                       updated_at = now(), terminal_at = EXCLUDED.terminal_at
                   WHERE automation_artifact_runs.state = 'executing'
                     AND automation_artifact_runs.artifact_digest = EXCLUDED.artifact_digest
                     AND automation_artifact_runs.owning_attempt_id = EXCLUDED.owning_attempt_id
                   RETURNING run_id""",
                (
                    run.run_id,
                    run.artifact_digest,
                    run.owning_attempt_id,
                    run.state.value,
                    Jsonb(events),
                    list(run.evidence_refs),
                    run.error,
                    terminal,
                ),
            ).fetchone()
            if row is None:
                stored = self.connection.execute(
                    """SELECT artifact_digest, owning_attempt_id, state, events,
                              evidence_refs, error
                       FROM merlin_storage.automation_artifact_runs WHERE run_id = %s""",
                    (run.run_id,),
                ).fetchone()
                expected = (
                    run.artifact_digest,
                    run.owning_attempt_id,
                    run.state.value,
                    events,
                    list(run.evidence_refs),
                    run.error,
                )
                actual = (
                    stored["artifact_digest"],
                    str(stored["owning_attempt_id"]),
                    stored["state"],
                    stored["events"],
                    stored["evidence_refs"],
                    stored["error"],
                )
                if actual != expected:
                    raise AutomationError("artifact run is terminal or bound to other content")

    def _insert_digest_bound(
        self,
        statement: str,
        values: tuple[object, ...],
        table: str,
        artifact_digest: str,
        payload_digest: str,
    ) -> None:
        with self.connection.transaction():
            row = self.connection.execute(statement, values).fetchone()
            if row is None:
                stored = self.connection.execute(
                    f"SELECT payload_digest FROM merlin_storage.{table} "  # noqa: S608
                    "WHERE artifact_digest = %s",
                    (artifact_digest,),
                ).fetchone()
                if stored is None or stored["payload_digest"] != payload_digest:
                    raise AutomationError("digest-bound automation record conflicts")


def _step_payload(step: AutomationStep) -> dict[str, object]:
    return {
        "step_id": step.step_id,
        "operation": step.operation,
        "arguments": dict(step.arguments),
        "evidence_keys": list(step.evidence_keys),
        "cleanup": step.cleanup,
    }


def _record_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
