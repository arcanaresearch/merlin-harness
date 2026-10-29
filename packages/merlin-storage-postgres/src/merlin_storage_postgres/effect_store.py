from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from merlin_contracts import (
    CleanupReceipt,
    TechniqueLanguageEvidence,
    ToolEffectRecord,
    ToolEffectState,
)
from psycopg.types.json import Jsonb

from .observability_store import TechniqueLanguageEvidenceRepository
from .sanitize import escape_nul


class ToolEffectRepository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def begin(self, record: ToolEffectRecord) -> tuple[ToolEffectRecord, bool]:
        arguments_payload = escape_nul(record.arguments_payload)
        with self.connection.transaction():
            if record.attempt_id is not None:
                legacy = self.connection.execute(
                    """SELECT effect.* FROM merlin_storage.tool_effects effect
                       JOIN merlin_storage.scheduler_attempts attempt ON attempt.id = %s
                       JOIN public.plugin_migrations migration
                         ON migration.owner = 'merlin-storage-postgres'
                        AND migration.version = 32
                       WHERE effect.session_id = %s AND effect.call_id = %s
                         AND effect.tool_name = %s AND effect.arguments_digest = %s
                         AND effect.attempt_id IS NULL
                         AND attempt.started_at < migration.applied_at
                         AND effect.created_at >= attempt.started_at
                         AND effect.created_at < migration.applied_at
                       ORDER BY effect.created_at LIMIT 1""",
                    (
                        record.attempt_id,
                        record.session_id,
                        record.call_id,
                        record.tool_name,
                        record.arguments_digest,
                    ),
                ).fetchone()
                if legacy is not None:
                    return self._record(legacy), False
            row = self.connection.execute(
                """INSERT INTO merlin_storage.tool_effects
                       (idempotency_key, session_id, call_id, agent_id, attempt_id,
                        campaign_id, target_id, tool_name, effect, arguments_digest,
                        arguments_payload, state)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'intent')
                   ON CONFLICT (idempotency_key) DO NOTHING
                   RETURNING *""",
                (
                    record.idempotency_key,
                    record.session_id,
                    record.call_id,
                    record.agent_id,
                    record.attempt_id,
                    record.campaign_id,
                    record.target_id,
                    record.tool_name,
                    record.effect,
                    record.arguments_digest,
                    (Jsonb(arguments_payload) if arguments_payload is not None else None),
                ),
            ).fetchone()
            created = row is not None
            if row is None:
                row = self.connection.execute(
                    "SELECT * FROM merlin_storage.tool_effects WHERE idempotency_key = %s",
                    (record.idempotency_key,),
                ).fetchone()
            if row is None:
                raise RuntimeError("tool effect intent disappeared")
            existing = self._record(row)
            if (
                existing.session_id != record.session_id
                or existing.call_id != record.call_id
                or (
                    existing.attempt_id is not None
                    and (
                        existing.agent_id != record.agent_id
                        or existing.attempt_id != record.attempt_id
                        or existing.campaign_id != record.campaign_id
                        or existing.target_id != record.target_id
                    )
                )
                or existing.tool_name != record.tool_name
                or existing.arguments_digest != record.arguments_digest
                or existing.arguments_payload != arguments_payload
            ):
                raise ValueError("idempotency key reused for a different tool effect")
            return existing, created

    def finish(self, record: ToolEffectRecord) -> ToolEffectRecord:
        record = record.model_copy(
            update={
                "result_content": escape_nul(record.result_content),
                "result_metadata": escape_nul(record.result_metadata),
            }
        )
        current_row = self.connection.execute(
            "SELECT * FROM merlin_storage.tool_effects WHERE idempotency_key = %s FOR UPDATE",
            (record.idempotency_key,),
        ).fetchone()
        if current_row is None:
            self.connection.rollback()
            raise KeyError(record.idempotency_key)
        current = self._record(current_row)
        terminal = {ToolEffectState.ACKNOWLEDGED, ToolEffectState.RECONCILED}
        if current.state in terminal:
            if current.model_dump(exclude={"updated_at"}) == record.model_dump(
                exclude={"updated_at"}
            ):
                self.connection.rollback()
                return current
            self.connection.rollback()
            raise ValueError("terminal tool effect receipt is immutable")
        allowed = {
            ToolEffectState.INTENT: {
                ToolEffectState.ACKNOWLEDGED,
                ToolEffectState.AMBIGUOUS,
            },
            ToolEffectState.AMBIGUOUS: {ToolEffectState.RECONCILED},
        }
        if record.state not in allowed.get(current.state, set()):
            self.connection.rollback()
            raise ValueError(f"invalid tool effect transition: {current.state} -> {record.state}")
        result_metadata = dict(record.result_metadata)
        delivery = result_metadata.get("target_delivery_receipt")
        technique_attempt_id = result_metadata.get("technique_attempt_id")
        language_repository = TechniqueLanguageEvidenceRepository(self.connection)
        language_evidence: TechniqueLanguageEvidence | None = None
        if (
            record.result_is_error is False
            and isinstance(delivery, Mapping)
            and isinstance(technique_attempt_id, str)
            and technique_attempt_id
            and isinstance(record.campaign_id, str)
        ):
            actual_language = str(delivery.get("actual_language") or "und")
            language_evidence = TechniqueLanguageEvidence(
                campaign_id=record.campaign_id,
                technique_attempt_id=technique_attempt_id,
                requested_language=str(delivery.get("requested_language") or "und"),
                actual_language=actual_language,
                language_evidence_status=(
                    "observed" if actual_language != "und" else "unavailable"
                ),
                language_evidence_source="committed-target-delivery.v1",
                evidence_refs=(record.idempotency_key,),
            )
            stored_language = language_repository.get(technique_attempt_id)
            comparable_fields = {"created_at", "evidence_refs"}
            if stored_language is not None and stored_language.model_dump(
                exclude=comparable_fields
            ) != language_evidence.model_dump(exclude=comparable_fields):
                uncertainty = {
                    "kind": "evaluation_uncertain",
                    "source": "committed-target-delivery.language",
                    "reason": (
                        "Later target delivery language metadata disagreed with the first "
                        "immutable observation for this technique attempt."
                    ),
                    "confidence": 0.0,
                    "affects": ["ranking", "evaluation"],
                    "first_requested_language": stored_language.requested_language,
                    "first_actual_language": stored_language.actual_language,
                    "later_requested_language": language_evidence.requested_language,
                    "later_actual_language": language_evidence.actual_language,
                }
                raw_uncertainties = result_metadata.get("evaluation_uncertainties", ())
                uncertainties = (
                    [dict(item) for item in raw_uncertainties if isinstance(item, Mapping)]
                    if isinstance(raw_uncertainties, (list, tuple))
                    else []
                )
                if uncertainty not in uncertainties:
                    uncertainties.append(uncertainty)
                result_metadata["evaluation_uncertain"] = True
                result_metadata["evaluation_uncertainties"] = uncertainties
                language_evidence = None
        row = self.connection.execute(
            """UPDATE merlin_storage.tool_effects
               SET state = %s, result_content = %s, result_is_error = %s,
                   result_metadata = %s, reconciliation = %s, updated_at = now()
               WHERE idempotency_key = %s
               RETURNING *""",
            (
                record.state.value,
                record.result_content,
                record.result_is_error,
                Jsonb(result_metadata),
                record.reconciliation,
                record.idempotency_key,
            ),
        ).fetchone()
        if row is not None and language_evidence is not None:
            language_repository.record(language_evidence)
        self.connection.commit()
        if row is None:
            raise KeyError(record.idempotency_key)
        return self._record(row)

    def record_cleanup(self, receipt: CleanupReceipt) -> CleanupReceipt:
        row = self.connection.execute(
            """INSERT INTO merlin_storage.cleanup_receipts
                   (idempotency_key, session_id, resource_kind, resource_id, state, details)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (idempotency_key) DO UPDATE SET
                   state = excluded.state, details = excluded.details
               RETURNING *""",
            (
                receipt.idempotency_key,
                receipt.session_id,
                receipt.resource_kind,
                receipt.resource_id,
                receipt.state,
                Jsonb(receipt.details),
            ),
        ).fetchone()
        self.connection.commit()
        value = dict(row)
        value["session_id"] = str(value["session_id"])
        return CleanupReceipt.model_validate(value)

    def list_for_session(
        self, session_id: str, *, tool_name: str | None = None
    ) -> list[ToolEffectRecord]:
        if tool_name is None:
            rows = self.connection.execute(
                """SELECT * FROM merlin_storage.tool_effects
                   WHERE session_id = %s ORDER BY created_at, idempotency_key""",
                (session_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """SELECT * FROM merlin_storage.tool_effects
                   WHERE session_id = %s AND tool_name = %s
                   ORDER BY created_at, idempotency_key""",
                (session_id, tool_name),
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_for_attempt(
        self, attempt_id: str, *, limit: int | None = None
    ) -> list[ToolEffectRecord]:
        if limit is None:
            rows = self.connection.execute(
                """SELECT * FROM merlin_storage.tool_effects
                   WHERE attempt_id = %s ORDER BY created_at, idempotency_key""",
                (attempt_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """SELECT * FROM (
                       SELECT * FROM merlin_storage.tool_effects
                       WHERE attempt_id = %s
                       ORDER BY created_at DESC, idempotency_key DESC LIMIT %s
                   ) AS recent ORDER BY created_at, idempotency_key""",
                (attempt_id, max(1, limit)),
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_for_campaign(
        self, campaign_id: str, *, limit: int | None = None
    ) -> list[ToolEffectRecord]:
        if limit is None:
            rows = self.connection.execute(
                """SELECT * FROM merlin_storage.tool_effects
                   WHERE campaign_id = %s ORDER BY created_at, idempotency_key""",
                (campaign_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """SELECT * FROM (
                       SELECT * FROM merlin_storage.tool_effects
                       WHERE campaign_id = %s
                       ORDER BY created_at DESC, idempotency_key DESC LIMIT %s
                   ) AS recent ORDER BY created_at, idempotency_key""",
                (campaign_id, max(1, limit)),
            ).fetchall()
        return [self._record(row) for row in rows]

    @staticmethod
    def _record(row: Any) -> ToolEffectRecord:
        value = dict(row)
        value["session_id"] = str(value["session_id"])
        if value.get("attempt_id") is not None:
            value["attempt_id"] = str(value["attempt_id"])
        value["state"] = ToolEffectState(value["state"])
        return ToolEffectRecord.model_validate(value)


class PostgresToolEffectStore:
    def __init__(self, repository: ToolEffectRepository) -> None:
        self._repository = repository

    async def begin(self, record: ToolEffectRecord) -> tuple[ToolEffectRecord, bool]:
        return await asyncio.to_thread(self._repository.begin, record)

    async def finish(self, record: ToolEffectRecord) -> ToolEffectRecord:
        return await asyncio.to_thread(self._repository.finish, record)

    async def record_cleanup(self, receipt: CleanupReceipt) -> CleanupReceipt:
        return await asyncio.to_thread(self._repository.record_cleanup, receipt)

    async def list_for_session(
        self, session_id: str, *, tool_name: str | None = None
    ) -> list[ToolEffectRecord]:
        return await asyncio.to_thread(
            self._repository.list_for_session, session_id, tool_name=tool_name
        )

    async def list_for_attempt(
        self, attempt_id: str, *, limit: int | None = None
    ) -> list[ToolEffectRecord]:
        return await asyncio.to_thread(
            self._repository.list_for_attempt,
            attempt_id,
            limit=limit,
        )

    async def list_for_campaign(
        self, campaign_id: str, *, limit: int | None = None
    ) -> list[ToolEffectRecord]:
        return await asyncio.to_thread(
            self._repository.list_for_campaign, campaign_id, limit=limit
        )
