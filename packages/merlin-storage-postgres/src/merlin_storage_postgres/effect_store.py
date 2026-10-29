from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from hashlib import sha256
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


class TargetEffectPermitLatchedError(RuntimeError):
    """The scheduler authority that admitted an effect no longer permits dispatch."""

    target_admission_latched = True


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

    def adopt_permitted(
        self, record: ToolEffectRecord, permit_digest: str
    ) -> tuple[ToolEffectRecord, bool]:
        """Adopt one exact scheduler-created effect intent before dispatch."""

        arguments_payload = escape_nul(record.arguments_payload)
        identity_fields = (
            "idempotency_key",
            "session_id",
            "call_id",
            "agent_id",
            "attempt_id",
            "campaign_id",
            "target_id",
            "tool_name",
            "effect",
            "arguments_digest",
            "arguments_payload",
        )
        with self.connection.transaction():
            scope = self.connection.execute(
                """SELECT campaign_id, session_id::text AS session_id, target_id,
                          target_session_id, schedule_id::text AS schedule_id,
                          node_id, assignment_id, attempt_id::text AS attempt_id,
                          generation
                   FROM merlin_storage.target_effect_permits
                   WHERE permit_digest = %s""",
                (permit_digest,),
            ).fetchone()
            if scope is None:
                raise KeyError(permit_digest)
            campaign = self.connection.execute(
                """SELECT id, session_id::text AS session_id
                   FROM merlin_storage.assessment_campaigns
                   WHERE id = %s FOR UPDATE""",
                (scope["campaign_id"],),
            ).fetchone()
            schedule = self.connection.execute(
                """SELECT id::text AS id, session_id::text AS session_id, status
                   FROM merlin_storage.scheduler_schedules
                   WHERE id = %s FOR UPDATE""",
                (scope["schedule_id"],),
            ).fetchone()
            latches = self.connection.execute(
                """SELECT target_session_id, state, transition_count,
                          latched_session_count, authorized_schedule_id::text
                              AS authorized_schedule_id,
                          lane_schedule_id::text AS lane_schedule_id,
                          lane_node_id, lane_assignment_id,
                          lane_attempt_id::text AS lane_attempt_id,
                          lane_generation, lane_recovery_required
                   FROM merlin_storage.target_admission_latches
                   WHERE campaign_id = %s AND target_id = %s
                     AND target_session_id IN ('', %s)
                   ORDER BY target_session_id FOR UPDATE""",
                (
                    scope["campaign_id"],
                    scope["target_id"],
                    scope["target_session_id"],
                ),
            ).fetchall()
            attempt = self.connection.execute(
                """SELECT id::text AS id, schedule_id::text AS schedule_id,
                          node_id, generation, state,
                          lease_expires_at > now() AS lease_active
                   FROM merlin_storage.scheduler_attempts
                   WHERE id = %s FOR UPDATE""",
                (scope["attempt_id"],),
            ).fetchone()
            permit = self.connection.execute(
                """SELECT permit_digest, campaign_id,
                          session_id::text AS session_id, target_id,
                          target_session_id,
                          schedule_id::text AS schedule_id, node_id,
                          assignment_id, attempt_id::text AS attempt_id,
                          generation,
                          call_id, tool_name, effect, arguments_digest,
                          arguments_payload, idempotency_key, state
                   FROM merlin_storage.target_effect_permits
                   WHERE permit_digest = %s FOR UPDATE""",
                (permit_digest,),
            ).fetchone()
            effect_row = self.connection.execute(
                """SELECT * FROM merlin_storage.tool_effects
                   WHERE idempotency_key = %s FOR UPDATE""",
                (record.idempotency_key,),
            ).fetchone()
            latch_by_session = {str(row["target_session_id"]): row for row in latches}
            head = latch_by_session.get("")
            latch = latch_by_session.get(str(scope["target_session_id"]))
            if (
                campaign is None
                or schedule is None
                or attempt is None
                or permit is None
                or effect_row is None
                or not scope["target_session_id"]
                or campaign["session_id"] != scope["session_id"]
                or schedule["session_id"] != scope["session_id"]
                or schedule["status"] != "running"
                or attempt["id"] != scope["attempt_id"]
                or attempt["schedule_id"] != scope["schedule_id"]
                or attempt["node_id"] != scope["node_id"]
                or attempt["generation"] != scope["generation"]
                or attempt["state"] != "running"
                or attempt["lease_active"] is not True
                or (head is not None and int(head["latched_session_count"]) != 0)
                or latch is None
                or latch["state"] not in {"open", "activated"}
                or (
                    latch["state"] == "open"
                    and head is not None
                    and int(head["transition_count"]) != 0
                )
                or (
                    latch["state"] == "activated"
                    and latch["authorized_schedule_id"] != scope["schedule_id"]
                )
                or latch["lane_schedule_id"] != scope["schedule_id"]
                or latch["lane_node_id"] != scope["node_id"]
                or latch["lane_assignment_id"] != scope["assignment_id"]
                or latch["lane_attempt_id"] != scope["attempt_id"]
                or latch["lane_generation"] != scope["generation"]
                or latch["lane_recovery_required"] is True
            ):
                raise TargetEffectPermitLatchedError(
                    "target effect permit authority is no longer active"
                )
            if any(
                permit[field] != scope[field]
                for field in (
                    "campaign_id",
                    "session_id",
                    "target_id",
                    "target_session_id",
                    "schedule_id",
                    "node_id",
                    "assignment_id",
                    "attempt_id",
                    "generation",
                )
            ):
                raise ValueError("target effect permit authority changed before adoption")
            permit_body = {
                "schema": "merlin.target-effect-permit.v1",
                **{
                    field: permit[field]
                    for field in (
                        "campaign_id",
                        "session_id",
                        "target_id",
                        "target_session_id",
                        "schedule_id",
                        "node_id",
                        "assignment_id",
                        "attempt_id",
                        "generation",
                        "call_id",
                        "tool_name",
                        "effect",
                        "arguments_digest",
                        "arguments_payload",
                        "idempotency_key",
                    )
                },
            }
            expected_permit_digest = (
                "sha256:"
                + sha256(
                    json.dumps(
                        permit_body,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
            )
            if permit["permit_digest"] != expected_permit_digest:
                raise ValueError("target effect permit digest is not canonical")
            if permit["state"] == "revoked":
                raise TargetEffectPermitLatchedError(
                    "target effect permit was revoked before dispatch"
                )
            existing = self._record(effect_row)
            normalized = record.model_copy(update={"arguments_payload": arguments_payload})
            if (
                permit["idempotency_key"] != record.idempotency_key
                or permit["campaign_id"] != record.campaign_id
                or permit["session_id"] != record.session_id
                or permit["target_id"] != record.target_id
                or permit["assignment_id"] != record.agent_id
                or permit["attempt_id"] != record.attempt_id
                or permit["call_id"] != record.call_id
                or permit["tool_name"] != record.tool_name
                or permit["effect"] != record.effect
                or permit["arguments_digest"] != record.arguments_digest
                or permit["arguments_payload"] != arguments_payload
                or any(
                    getattr(existing, field) != getattr(normalized, field)
                    for field in identity_fields
                )
            ):
                raise ValueError("target effect permit does not match its durable intent")
            permit_state = str(permit["state"])
            if permit_state == "intent":
                if existing.state is not ToolEffectState.INTENT:
                    raise ValueError("target effect permit intent is already resolved")
                adopted = self.connection.execute(
                    """UPDATE merlin_storage.target_effect_permits
                       SET state = 'adopted', updated_at = now()
                       WHERE permit_digest = %s AND state = 'intent'
                       RETURNING permit_digest""",
                    (permit_digest,),
                ).fetchone()
                if adopted is None:
                    raise ValueError("target effect permit adoption lost its fence")
                return existing, True
            if permit_state == "adopted" and existing.state is ToolEffectState.INTENT:
                return existing, False
            if permit_state == "terminal" and existing.state in {
                ToolEffectState.ACKNOWLEDGED,
                ToolEffectState.RECONCILED,
            }:
                return existing, False
            raise ValueError("target effect permit state conflicts with its durable intent")

    def finish(self, record: ToolEffectRecord) -> ToolEffectRecord:
        with self.connection.transaction():
            return self._finish_locked(record)

    def _finish_locked(self, record: ToolEffectRecord) -> ToolEffectRecord:
        record = record.model_copy(
            update={
                "result_content": escape_nul(record.result_content),
                "result_metadata": escape_nul(record.result_metadata),
            }
        )
        permit_scope = self.connection.execute(
            """SELECT campaign_id, target_id, target_session_id, schedule_id,
                      attempt_id
               FROM merlin_storage.target_effect_permits
               WHERE idempotency_key = %s""",
            (record.idempotency_key,),
        ).fetchone()
        if permit_scope is not None:
            self.connection.execute(
                """SELECT id FROM merlin_storage.assessment_campaigns
                   WHERE id = %s FOR UPDATE""",
                (permit_scope["campaign_id"],),
            ).fetchone()
            self.connection.execute(
                """SELECT id FROM merlin_storage.scheduler_schedules
                   WHERE id = %s FOR UPDATE""",
                (permit_scope["schedule_id"],),
            ).fetchone()
            self.connection.execute(
                """SELECT target_session_id
                   FROM merlin_storage.target_admission_latches
                   WHERE campaign_id = %s AND target_id = %s
                     AND target_session_id IN ('', %s)
                   ORDER BY target_session_id FOR UPDATE""",
                (
                    permit_scope["campaign_id"],
                    permit_scope["target_id"],
                    permit_scope["target_session_id"],
                ),
            ).fetchall()
            self.connection.execute(
                """SELECT id FROM merlin_storage.scheduler_attempts
                   WHERE id = %s FOR UPDATE""",
                (permit_scope["attempt_id"],),
            ).fetchone()
        permit = self.connection.execute(
            """SELECT permit_digest, state, target_session_id
               FROM merlin_storage.target_effect_permits
               WHERE idempotency_key = %s FOR UPDATE""",
            (record.idempotency_key,),
        ).fetchone()
        current_row = self.connection.execute(
            "SELECT * FROM merlin_storage.tool_effects WHERE idempotency_key = %s FOR UPDATE",
            (record.idempotency_key,),
        ).fetchone()
        if current_row is None:
            raise KeyError(record.idempotency_key)
        current = self._record(current_row)
        terminal = {ToolEffectState.ACKNOWLEDGED, ToolEffectState.RECONCILED}
        revoked_before_dispatch = bool(
            permit is not None
            and permit["state"] == "revoked"
            and record.state is ToolEffectState.RECONCILED
            and record.result_is_error is True
            and record.result_metadata.get("target_effect_dispatch_status")
            == "revoked_before_dispatch"
            and record.result_metadata.get("target_session_id") == permit["target_session_id"]
            and record.result_metadata.get("target_effect_attempted") is False
            and type(record.result_metadata.get("interaction_count")) is int
            and record.result_metadata.get("interaction_count") == 0
            and type(record.result_metadata.get("limit_interaction_count")) is int
            and record.result_metadata.get("limit_interaction_count") == 0
            and type(record.result_metadata.get("target_call_budget_charge")) is int
            and record.result_metadata.get("target_call_budget_charge") == 0
        )
        if (
            permit is not None
            and record.state in terminal
            and not revoked_before_dispatch
            and record.result_metadata.get("target_session_id") != permit["target_session_id"]
        ):
            raise ValueError(
                "terminal target effect receipt does not match its permitted target session"
            )
        if current.state in terminal:
            if current.model_dump(exclude={"updated_at"}) == record.model_dump(
                exclude={"updated_at"}
            ):
                if permit is not None and permit["state"] == "adopted":
                    repaired = self.connection.execute(
                        """UPDATE merlin_storage.target_effect_permits
                           SET state = 'terminal', updated_at = now()
                           WHERE permit_digest = %s AND state = 'adopted'
                           RETURNING permit_digest""",
                        (permit["permit_digest"],),
                    ).fetchone()
                    if repaired is None:
                        raise ValueError("target effect permit repair lost its durable fence")
                elif permit is not None and permit["state"] not in {
                    "terminal",
                    "revoked",
                }:
                    raise ValueError("terminal tool effect conflicts with its target effect permit")
                return current
            raise ValueError("terminal tool effect receipt is immutable")
        allowed = {
            ToolEffectState.INTENT: {
                ToolEffectState.ACKNOWLEDGED,
                ToolEffectState.AMBIGUOUS,
            },
            ToolEffectState.AMBIGUOUS: {ToolEffectState.RECONCILED},
        }
        if record.state not in allowed.get(current.state, set()):
            raise ValueError(f"invalid tool effect transition: {current.state} -> {record.state}")
        if (
            permit is not None
            and permit["state"] not in {"adopted", "terminal"}
            and not revoked_before_dispatch
        ):
            raise ValueError("target effect permit was not adopted before completion")
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
        if permit is not None and permit["state"] == "adopted" and record.state in terminal:
            terminal = self.connection.execute(
                """UPDATE merlin_storage.target_effect_permits
                   SET state = 'terminal', updated_at = now()
                   WHERE permit_digest = %s AND state = 'adopted'
                   RETURNING permit_digest""",
                (permit["permit_digest"],),
            ).fetchone()
            if terminal is None:
                raise ValueError("target effect permit completion lost its fence")
        if row is not None and language_evidence is not None:
            language_repository.record(language_evidence)
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

    async def adopt_permitted(
        self, record: ToolEffectRecord, permit_digest: str
    ) -> tuple[ToolEffectRecord, bool]:
        return await asyncio.to_thread(self._repository.adopt_permitted, record, permit_digest)

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
        return await asyncio.to_thread(self._repository.list_for_campaign, campaign_id, limit=limit)
