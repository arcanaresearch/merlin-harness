from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from merlin_contracts import (
    TERMINAL_TECHNIQUE_ATTEMPT_STATES,
    TechniqueAttempt,
    TechniqueAttemptState,
)
from psycopg.types.json import Jsonb

_TRANSITIONS: dict[TechniqueAttemptState, frozenset[TechniqueAttemptState]] = {
    TechniqueAttemptState.PLANNED: frozenset(
        {
            TechniqueAttemptState.ACTIVE,
            TechniqueAttemptState.BLOCKED,
            TechniqueAttemptState.CANCELLED,
        }
    ),
    TechniqueAttemptState.ACTIVE: frozenset(
        {
            TechniqueAttemptState.EFFECT_ACKNOWLEDGED,
            TechniqueAttemptState.OBSERVED,
            TechniqueAttemptState.INCONCLUSIVE,
            TechniqueAttemptState.BLOCKED,
            TechniqueAttemptState.CANCELLED,
            TechniqueAttemptState.AMBIGUOUS,
        }
    ),
    TechniqueAttemptState.EFFECT_ACKNOWLEDGED: frozenset(
        {
            TechniqueAttemptState.OBSERVED,
            TechniqueAttemptState.INCONCLUSIVE,
            TechniqueAttemptState.BLOCKED,
            TechniqueAttemptState.CANCELLED,
            TechniqueAttemptState.AMBIGUOUS,
        }
    ),
    TechniqueAttemptState.OBSERVED: frozenset(TERMINAL_TECHNIQUE_ATTEMPT_STATES),
}


class TechniqueAttemptRepository:
    """Compare-and-set lifecycle store for authoritative target experiments."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def begin(self, attempt: TechniqueAttempt) -> TechniqueAttempt:
        if attempt.state != TechniqueAttemptState.PLANNED:
            raise ValueError("a new technique attempt must begin in planned state")
        values = (
            attempt.id,
            attempt.schema_version,
            attempt.campaign_id,
            attempt.wave,
            attempt.task_id,
            attempt.technique_id,
            attempt.strategy_id,
            attempt.hypothesis_id,
            attempt.parent_attempt_id,
            attempt.transform_id,
            attempt.language,
            attempt.control_kind.value,
            attempt.oracle_digest,
            attempt.prompt_digest,
            attempt.scheduler_attempt_id,
            attempt.state.value,
            Jsonb(attempt.strategy_state),
            list(attempt.evidence_refs),
            attempt.provider,
            attempt.model,
            Jsonb(attempt.usage),
        )
        with self.connection.transaction():
            inserted = self.connection.execute(
                """INSERT INTO merlin_storage.campaign_technique_attempts
                       (id, schema_version, campaign_id, wave, task_id, technique_id, strategy_id,
                        hypothesis_id, parent_attempt_id, transform_id, language, control_kind,
                        oracle_digest, prompt_digest, scheduler_attempt_id, state, strategy_state,
                        evidence_refs, provider, model, usage)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO NOTHING RETURNING id""",
                values,
            ).fetchone()
            stored = self.get(attempt.id)
            if stored is None:
                raise RuntimeError("technique attempt insert did not persist")
            if inserted is None and self._identity(stored) != self._identity(attempt):
                raise ValueError("technique attempt identity conflicts with an existing record")
            return stored

    def transition(
        self,
        attempt_id: str,
        expected_state: TechniqueAttemptState,
        state: TechniqueAttemptState,
        *,
        scheduler_attempt_id: str | None = None,
        prompt_digest: str | None = None,
        strategy_state: Mapping[str, object] | None = None,
        evidence_refs: tuple[str, ...] | None = None,
        provider: str | None = None,
        model: str | None = None,
        usage: Mapping[str, object] | None = None,
        terminal_reason: str | None = None,
    ) -> TechniqueAttempt:
        allowed = _TRANSITIONS.get(expected_state, frozenset())
        if state not in allowed:
            raise ValueError(f"invalid technique-attempt transition: {expected_state} -> {state}")
        terminal = state in TERMINAL_TECHNIQUE_ATTEMPT_STATES
        if terminal != bool((terminal_reason or "").strip()):
            raise ValueError("terminal transitions require exactly one terminal reason")
        if evidence_refs is not None and len(set(evidence_refs)) != len(evidence_refs):
            raise ValueError("technique attempt evidence references must be unique")
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_technique_attempts
                   SET state = %s,
                       scheduler_attempt_id = coalesce(%s, scheduler_attempt_id),
                       prompt_digest = coalesce(%s, prompt_digest),
                       strategy_state = coalesce(%s, strategy_state),
                       evidence_refs = coalesce(%s, evidence_refs),
                       provider = coalesce(%s, provider), model = coalesce(%s, model),
                       usage = coalesce(%s, usage), terminal_reason = %s,
                       activated_at = coalesce(activated_at, now()),
                       updated_at = now(), terminal_at = CASE WHEN %s THEN now() ELSE NULL END
                   WHERE id = %s AND state = %s RETURNING id""",
                (
                    state.value,
                    scheduler_attempt_id,
                    prompt_digest,
                    None if strategy_state is None else Jsonb(dict(strategy_state)),
                    None if evidence_refs is None else list(evidence_refs),
                    provider,
                    model,
                    None if usage is None else Jsonb(dict(usage)),
                    terminal_reason,
                    terminal,
                    attempt_id,
                    expected_state.value,
                ),
            ).fetchone()
            if row is None:
                current = self.get(attempt_id)
                if current is None:
                    raise KeyError(attempt_id)
                if current.state == state:
                    return current
                raise ValueError(
                    f"technique attempt compare-and-set failed: expected {expected_state}, "
                    f"found {current.state}"
                )
            stored = self.get(attempt_id)
            if stored is None:
                raise RuntimeError("technique attempt transition disappeared")
            return stored

    def rebind_scheduler(self, attempt_id: str, scheduler_attempt_id: str) -> TechniqueAttempt:
        """Move a non-terminal experiment to its current replacement lease."""

        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_technique_attempts
                   SET scheduler_attempt_id = %s, updated_at = now()
                   WHERE id = %s
                     AND state NOT IN (
                         'confirmed','refuted','inconclusive','blocked','cancelled','ambiguous'
                     )
                   RETURNING id""",
                (scheduler_attempt_id, attempt_id),
            ).fetchone()
            if row is None:
                current = self.get(attempt_id)
                if current is None:
                    raise KeyError(attempt_id)
                if current.state in TERMINAL_TECHNIQUE_ATTEMPT_STATES:
                    raise ValueError("terminal technique attempts cannot be rebound")
                raise RuntimeError("technique attempt scheduler rebind did not persist")
            rebound = self.get(attempt_id)
            if rebound is None:
                raise RuntimeError("technique attempt scheduler rebind disappeared")
            return rebound

    def observe(
        self,
        attempt_id: str,
        *,
        evidence_refs: tuple[str, ...] = (),
        usage: Mapping[str, object] | None = None,
    ) -> TechniqueAttempt:
        """Record that execution produced evidence without claiming its verdict."""

        current = self.get(attempt_id)
        if current is None:
            raise KeyError(attempt_id)
        if current.state == TechniqueAttemptState.OBSERVED:
            return current
        if current.state in TERMINAL_TECHNIQUE_ATTEMPT_STATES:
            return current
        if current.state == TechniqueAttemptState.PLANNED:
            current = self.transition(
                attempt_id, TechniqueAttemptState.PLANNED, TechniqueAttemptState.ACTIVE
            )
        return self.transition(
            attempt_id,
            current.state,
            TechniqueAttemptState.OBSERVED,
            evidence_refs=evidence_refs,
            usage=usage,
        )

    def terminalize(
        self,
        attempt_id: str,
        state: TechniqueAttemptState,
        *,
        reason: str,
        evidence_refs: tuple[str, ...] = (),
        usage: Mapping[str, object] | None = None,
        strategy_state: Mapping[str, object] | None = None,
    ) -> TechniqueAttempt:
        """Apply one immutable terminal disposition to a live experiment."""

        if state not in TERMINAL_TECHNIQUE_ATTEMPT_STATES:
            raise ValueError("terminalize requires a terminal technique-attempt state")
        current = self.get(attempt_id)
        if current is None:
            raise KeyError(attempt_id)
        if current.state in TERMINAL_TECHNIQUE_ATTEMPT_STATES:
            return current
        if state in {TechniqueAttemptState.CONFIRMED, TechniqueAttemptState.REFUTED}:
            current = self.observe(attempt_id, evidence_refs=evidence_refs, usage=usage)
        return self.transition(
            attempt_id,
            current.state,
            state,
            evidence_refs=evidence_refs,
            usage=usage,
            strategy_state=strategy_state,
            terminal_reason=reason,
        )

    def get(self, attempt_id: str) -> TechniqueAttempt | None:
        row = self.connection.execute(
            "SELECT * FROM merlin_storage.campaign_technique_attempts WHERE id = %s",
            (attempt_id,),
        ).fetchone()
        return None if row is None else self._from_row(row)

    def load_active(self, campaign_id: str, task_id: str) -> tuple[TechniqueAttempt, ...]:
        rows = self.connection.execute(
            """SELECT * FROM merlin_storage.campaign_technique_attempts
               WHERE campaign_id = %s AND task_id = %s
                 AND state NOT IN (
                     'confirmed','refuted','inconclusive','blocked','cancelled','ambiguous'
                 )
               ORDER BY wave, created_at, id""",
            (campaign_id, task_id),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_for_campaign(self, campaign_id: str) -> tuple[TechniqueAttempt, ...]:
        rows = self.connection.execute(
            """SELECT * FROM merlin_storage.campaign_technique_attempts
               WHERE campaign_id = %s ORDER BY wave, created_at, id""",
            (campaign_id,),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_for_schedule(self, schedule_id: str) -> tuple[TechniqueAttempt, ...]:
        rows = self.connection.execute(
            """SELECT attempt.*
               FROM merlin_storage.campaign_technique_attempts AS attempt
               JOIN merlin_storage.campaign_waves AS wave
                 ON wave.campaign_id = attempt.campaign_id AND wave.wave = attempt.wave
               WHERE wave.schedule_id = %s
               ORDER BY attempt.created_at, attempt.id""",
            (schedule_id,),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    @staticmethod
    def reduce(attempts: tuple[TechniqueAttempt, ...]) -> dict[str, int]:
        counts = {state.value: 0 for state in TechniqueAttemptState}
        for attempt in attempts:
            counts[attempt.state.value] += 1
        return counts

    @staticmethod
    def _identity(attempt: TechniqueAttempt) -> tuple[object, ...]:
        return (
            attempt.id,
            attempt.campaign_id,
            attempt.wave,
            attempt.task_id,
            attempt.technique_id,
            attempt.strategy_id,
            attempt.hypothesis_id,
            attempt.parent_attempt_id,
            attempt.transform_id,
            attempt.language,
            attempt.control_kind,
            attempt.oracle_digest,
        )

    @staticmethod
    def _from_row(row: Mapping[str, object]) -> TechniqueAttempt:
        return TechniqueAttempt(
            schema_version=int(row["schema_version"]),
            id=str(row["id"]),
            campaign_id=str(row["campaign_id"]),
            wave=int(row["wave"]),
            task_id=str(row["task_id"]),
            technique_id=str(row["technique_id"]),
            strategy_id=str(row["strategy_id"]),
            hypothesis_id=None if row["hypothesis_id"] is None else str(row["hypothesis_id"]),
            parent_attempt_id=(
                None if row["parent_attempt_id"] is None else str(row["parent_attempt_id"])
            ),
            transform_id=None if row["transform_id"] is None else str(row["transform_id"]),
            language=str(row["language"]),
            control_kind=str(row["control_kind"]),
            oracle_digest=str(row["oracle_digest"]),
            prompt_digest=None if row["prompt_digest"] is None else str(row["prompt_digest"]),
            scheduler_attempt_id=(
                None if row["scheduler_attempt_id"] is None else str(row["scheduler_attempt_id"])
            ),
            state=str(row["state"]),
            strategy_state=dict(row["strategy_state"]),
            evidence_refs=tuple(row["evidence_refs"]),
            provider=None if row["provider"] is None else str(row["provider"]),
            model=None if row["model"] is None else str(row["model"]),
            usage=dict(row["usage"]),
            terminal_reason=(
                None if row["terminal_reason"] is None else str(row["terminal_reason"])
            ),
            created_at=row["created_at"] if isinstance(row["created_at"], datetime) else None,
            activated_at=(
                row["activated_at"] if isinstance(row["activated_at"], datetime) else None
            ),
            updated_at=row["updated_at"] if isinstance(row["updated_at"], datetime) else None,
            terminal_at=row["terminal_at"] if isinstance(row["terminal_at"], datetime) else None,
        )
