from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Literal
from uuid import uuid4

from merlin_contracts import (
    EvidenceWitness,
    Finding,
    FindingDraft,
    FindingStatus,
    FindingValidation,
)
from merlin_contracts import FindingSeverity as ContractFindingSeverity
from psycopg.types.json import Jsonb

FindingDisposition = Literal["accepted", "rejected"]
FindingRecord = Finding
FindingSeverity = ContractFindingSeverity


class FindingRepository:
    _columns = (
        "id, session_id, title, severity, summary, reproduction, remediation, creator, "
        "status, identity, target_id, technique_id, validation, validation_summary, "
        "witnesses, attack_path, causal_ambiguity, impact_level, "
        "requires_independent_reproduction, partial_signal, created_at, dispositioned_at"
    )

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def create(
        self,
        session_id: str,
        draft: FindingDraft,
        *,
        finding_id: str | None = None,
    ) -> FindingRecord:
        _validate_draft(draft)
        with self.connection.transaction():
            row = self.connection.execute(
                f"""INSERT INTO merlin_storage.findings
                        (id, session_id, title, severity, summary, reproduction,
                        remediation, creator, status, identity, target_id, technique_id,
                        validation, validation_summary, witnesses, attack_path,
                        causal_ambiguity, impact_level, requires_independent_reproduction,
                        partial_signal)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (session_id, identity) DO UPDATE
                    SET id = merlin_storage.findings.id
                    RETURNING {self._columns}""",
                (
                    finding_id or uuid4(),
                    session_id,
                    draft.title,
                    draft.severity,
                    draft.summary,
                    draft.reproduction,
                    draft.remediation,
                    draft.creator,
                    draft.stable_identity(),
                    draft.target_id,
                    draft.technique_id,
                    draft.validation,
                    draft.validation_summary,
                    Jsonb([item.model_dump(mode="json") for item in draft.witnesses]),
                    Jsonb([item.model_dump(mode="json") for item in draft.attack_path]),
                    draft.causal_ambiguity,
                    draft.impact_level,
                    draft.requires_independent_reproduction,
                    Jsonb(draft.partial_signal.model_dump(mode="json"))
                    if draft.partial_signal is not None
                    else None,
                ),
            ).fetchone()
            evidence_rows = self.connection.execute(
                """SELECT reference FROM merlin_storage.finding_evidence
                   WHERE finding_id = %s ORDER BY ordinal""",
                (row["id"],),
            ).fetchall()
            known = {item["reference"] for item in evidence_rows}
            additions = [item for item in draft.evidence_references if item not in known]
            for ordinal, reference in enumerate(additions, start=len(evidence_rows)):
                self.connection.execute(
                    """INSERT INTO merlin_storage.finding_evidence
                           (finding_id, ordinal, reference) VALUES (%s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    (row["id"], ordinal, reference),
                )
        return self._record(row)

    def get(self, finding_id: str) -> FindingRecord | None:
        row = self.connection.execute(
            f"SELECT {self._columns} FROM merlin_storage.findings WHERE id = %s",
            (finding_id,),
        ).fetchone()
        return self._record(row) if row else None

    def list(
        self,
        session_id: str,
        *,
        status: FindingStatus | None = None,
        limit: int = 100,
    ) -> list[FindingRecord]:
        if status is None:
            rows = self.connection.execute(
                f"""SELECT {self._columns} FROM merlin_storage.findings
                    WHERE session_id = %s
                    ORDER BY created_at DESC, id DESC LIMIT %s""",
                (session_id, limit),
            ).fetchall()
        else:
            rows = self.connection.execute(
                f"""SELECT {self._columns} FROM merlin_storage.findings
                    WHERE session_id = %s AND status = %s
                    ORDER BY created_at DESC, id DESC LIMIT %s""",
                (session_id, status, limit),
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_all(self, session_id: str) -> list[FindingRecord]:
        """Return the complete session finding set for completion authority.

        Interactive consumers intentionally retain the bounded ``list`` API.  Campaign
        completion cannot use that page as absence authority: an older pending finding
        must still prevent a complete report.
        """
        rows = self.connection.execute(
            f"""SELECT {self._columns},
                       ARRAY(
                           SELECT evidence.reference
                           FROM merlin_storage.finding_evidence evidence
                           WHERE evidence.finding_id = findings.id
                           ORDER BY evidence.ordinal
                       ) AS evidence_references
                FROM merlin_storage.findings findings
                WHERE session_id = %s
                ORDER BY created_at DESC, id DESC""",
            (session_id,),
        ).fetchall()
        return [self._record(row, tuple(row.get("evidence_references") or ())) for row in rows]

    def set_disposition(self, finding_id: str, disposition: FindingDisposition) -> FindingRecord:
        if disposition not in ("accepted", "rejected"):
            raise ValueError("finding disposition must be accepted or rejected")
        row = self.connection.execute(
            f"""UPDATE merlin_storage.findings
                SET status = %s, dispositioned_at = now()
                WHERE id = %s AND status = 'draft'
                RETURNING {self._columns}""",
            (disposition, finding_id),
        ).fetchone()
        if row is None:
            self.connection.rollback()
            raise ValueError(f"finding {finding_id!r} is unknown or has already been dispositioned")
        self.connection.commit()
        return self._record(row)

    def set_validation(
        self,
        finding_id: str,
        validation: FindingValidation,
        summary: str,
        witnesses: Sequence[EvidenceWitness],
        partial_signal: Any = None,
    ) -> FindingRecord:
        if validation not in ("validated", "refuted", "inconclusive", "partial"):
            raise ValueError("invalid finding validation result")
        if not summary.strip():
            raise ValueError("validation summary must not be empty")
        if validation == "partial" and partial_signal is None:
            raise ValueError("partial findings require a partial compliance signal")
        if validation != "partial" and partial_signal is not None:
            raise ValueError("a partial compliance signal is only valid for partial findings")
        with self.connection.transaction():
            row = self.connection.execute(
                f"""UPDATE merlin_storage.findings
                    SET validation = %s, validation_summary = %s, witnesses = %s,
                        partial_signal = %s
                    WHERE id = %s AND validation = 'unvalidated'
                    RETURNING {self._columns}""",
                (
                    validation,
                    summary,
                    Jsonb([item.model_dump(mode="json") for item in witnesses]),
                    Jsonb(partial_signal.model_dump(mode="json"))
                    if partial_signal is not None
                    else None,
                    finding_id,
                ),
            ).fetchone()
            if row is None:
                raise ValueError(f"finding {finding_id!r} is unknown or already validated")
            for witness in witnesses:
                exists = self.connection.execute(
                    """SELECT 1 FROM merlin_storage.finding_evidence
                       WHERE finding_id = %s AND reference = %s""",
                    (finding_id, witness.reference),
                ).fetchone()
                if exists is not None:
                    continue
                self.connection.execute(
                    """INSERT INTO merlin_storage.finding_evidence
                           (finding_id, ordinal, reference)
                       SELECT %s, COALESCE(MAX(ordinal), -1) + 1, %s
                       FROM merlin_storage.finding_evidence
                       WHERE finding_id = %s""",
                    (finding_id, witness.reference, finding_id),
                )
        return self._record(row)

    def set_remediation(self, finding_id: str, remediation: str) -> FindingRecord:
        if not remediation.strip():
            raise ValueError("remediation must not be empty")
        row = self.connection.execute(
            f"""UPDATE merlin_storage.findings SET remediation = %s
                WHERE id = %s AND validation = 'validated'
                RETURNING {self._columns}""",
            (remediation, finding_id),
        ).fetchone()
        if row is None:
            self.connection.rollback()
            raise ValueError("remediation requires a validated finding")
        self.connection.commit()
        return self._record(row)

    def _record(
        self, row: dict[str, Any], evidence_refs: Sequence[str] | None = None
    ) -> FindingRecord:
        if evidence_refs is None:
            evidence_rows = self.connection.execute(
                """SELECT reference FROM merlin_storage.finding_evidence
                   WHERE finding_id = %s ORDER BY ordinal""",
                (row["id"],),
            ).fetchall()
            evidence_refs = [evidence["reference"] for evidence in evidence_rows]
        return Finding(
            id=str(row["id"]),
            title=row["title"],
            severity=row["severity"],
            summary=row["summary"],
            reproduction=row["reproduction"],
            remediation=row["remediation"],
            creator=row["creator"],
            evidence_references=list(evidence_refs),
            status=row["status"],
            identity=row["identity"],
            target_id=row["target_id"],
            technique_id=row["technique_id"],
            validation=row["validation"],
            validation_summary=row["validation_summary"],
            witnesses=row["witnesses"],
            attack_path=row["attack_path"],
            causal_ambiguity=row["causal_ambiguity"],
            impact_level=row["impact_level"],
            requires_independent_reproduction=row["requires_independent_reproduction"],
            partial_signal=row["partial_signal"],
        )


class PostgresFindingStore:
    """Async boundary for finding drafts and explicit operator disposition."""

    def __init__(self, connection: Any) -> None:
        self._repository = FindingRepository(connection)
        self._lock = asyncio.Lock()

    async def _call(self, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(function, *args, **kwargs)

    async def create(self, session_id: str, draft: FindingDraft) -> FindingRecord:
        return await self._call(self._repository.create, session_id, draft)

    async def get(self, finding_id: str) -> FindingRecord | None:
        return await self._call(self._repository.get, finding_id)

    async def list(
        self,
        session_id: str,
        *,
        status: FindingStatus | None = None,
        limit: int = 100,
    ) -> Sequence[FindingRecord]:
        return await self._call(self._repository.list, session_id, status=status, limit=limit)

    async def list_all(self, session_id: str) -> Sequence[FindingRecord]:
        return await self._call(self._repository.list_all, session_id)

    async def set_disposition(
        self, finding_id: str, disposition: FindingDisposition
    ) -> FindingRecord:
        return await self._call(self._repository.set_disposition, finding_id, disposition)

    async def set_validation(
        self,
        finding_id: str,
        validation: FindingValidation,
        summary: str,
        witnesses: Sequence[EvidenceWitness],
        partial_signal: Any = None,
    ) -> FindingRecord:
        return await self._call(
            self._repository.set_validation,
            finding_id,
            validation,
            summary,
            witnesses,
            partial_signal,
        )

    async def set_remediation(self, finding_id: str, remediation: str) -> FindingRecord:
        return await self._call(self._repository.set_remediation, finding_id, remediation)


def _validate_draft(draft: FindingDraft) -> None:
    required = {
        "title": draft.title,
        "summary": draft.summary,
        "creator": draft.creator,
    }
    empty = [name for name, value in required.items() if not value.strip()]
    if empty:
        raise ValueError(f"finding fields must not be empty: {', '.join(empty)}")
    if any(not reference.strip() for reference in draft.evidence_references):
        raise ValueError("evidence references must not be empty")
