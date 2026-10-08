from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from merlin_contracts import (
    ArmIdentity,
    ArmOutcome,
    CampaignCounterEvent,
    CampaignCounters,
    HypothesisRecord,
    HypothesisState,
    InfrastructureFailureCategory,
    LanguageEvidenceStatus,
    TechniqueLanguageEvidence,
)
from psycopg.types.json import Jsonb


class _FrontierHypothesis(Protocol):
    assertion: str

    @property
    def id(self) -> str: ...


class _LanguageExecutionReceipt(Protocol):
    requested_language: str
    actual_language: str
    executor_id: str
    receipt_digest: str


_HYPOTHESIS_TRANSITIONS: dict[HypothesisState, frozenset[HypothesisState]] = {
    HypothesisState.UNTESTED: frozenset(
        {
            HypothesisState.IN_PROGRESS,
            HypothesisState.BLOCKED_BY_INFRASTRUCTURE,
            HypothesisState.SUPERSEDED,
        }
    ),
    HypothesisState.IN_PROGRESS: frozenset(
        {
            HypothesisState.SUPPORTED,
            HypothesisState.FALSIFIED,
            HypothesisState.BLOCKED_BY_INFRASTRUCTURE,
            HypothesisState.SUPERSEDED,
        }
    ),
    HypothesisState.BLOCKED_BY_INFRASTRUCTURE: frozenset(
        {HypothesisState.IN_PROGRESS, HypothesisState.SUPERSEDED}
    ),
    HypothesisState.SUPPORTED: frozenset({HypothesisState.SUPERSEDED}),
}


def hypothesis_record_from_frontier(
    hypothesis: _FrontierHypothesis,
    *,
    campaign_id: str,
    target_id: str,
    scenario_fingerprint: str,
    creating_evidence_refs: tuple[str, ...],
    parent: HypothesisRecord | None = None,
) -> HypothesisRecord:
    """Bind an agent frontier hypothesis to its durable campaign lineage."""
    scope = (campaign_id, target_id, scenario_fingerprint)
    if parent is not None and scope != (
        parent.campaign_id,
        parent.target_id,
        parent.scenario_fingerprint,
    ):
        raise ValueError("durable hypothesis parent belongs to a different campaign scope")
    return HypothesisRecord(
        id=hypothesis.id,
        campaign_id=campaign_id,
        target_id=target_id,
        scenario_fingerprint=scenario_fingerprint,
        claim=hypothesis.assertion,
        parent_hypothesis_id=None if parent is None else parent.id,
        creating_evidence_refs=creating_evidence_refs,
        lineage=(hypothesis.id,) if parent is None else (*parent.lineage, hypothesis.id),
    )


def technique_language_evidence_from_execution(
    execution: _LanguageExecutionReceipt,
    *,
    campaign_id: str,
    technique_attempt_id: str,
    status: LanguageEvidenceStatus,
) -> TechniqueLanguageEvidence:
    """Bind a sealed runtime language receipt to one durable technique attempt."""
    return TechniqueLanguageEvidence(
        campaign_id=campaign_id,
        technique_attempt_id=technique_attempt_id,
        requested_language=execution.requested_language,
        actual_language=execution.actual_language,
        language_evidence_status=status,
        language_evidence_source=execution.executor_id,
        evidence_refs=(execution.receipt_digest,),
    )


class HypothesisRepository:
    """Immutable hypothesis identities with compare-and-set lifecycle events."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def create(self, hypothesis: HypothesisRecord) -> HypothesisRecord:
        if hypothesis.state != HypothesisState.UNTESTED:
            raise ValueError("a new hypothesis must begin untested")
        if hypothesis.parent_hypothesis_id is not None:
            parent = self.get(hypothesis.parent_hypothesis_id)
            if parent is None:
                raise ValueError("parent hypothesis does not exist")
            if (
                parent.campaign_id,
                parent.target_id,
                parent.scenario_fingerprint,
            ) != (
                hypothesis.campaign_id,
                hypothesis.target_id,
                hypothesis.scenario_fingerprint,
            ):
                raise ValueError("child hypothesis scope must match its parent")
            if hypothesis.lineage != (*parent.lineage, hypothesis.id):
                raise ValueError("child hypothesis lineage must extend its durable parent")
        elif hypothesis.lineage != (hypothesis.id,):
            raise ValueError("root hypothesis lineage must contain only itself")

        with self.connection.transaction():
            inserted = self.connection.execute(
                """INSERT INTO merlin_storage.campaign_hypotheses
                       (id, schema_version, campaign_id, target_id, scenario_fingerprint,
                        claim, parent_hypothesis_id, creating_evidence_refs, lineage)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO NOTHING RETURNING id""",
                (
                    hypothesis.id,
                    hypothesis.schema_version,
                    hypothesis.campaign_id,
                    hypothesis.target_id,
                    hypothesis.scenario_fingerprint,
                    hypothesis.claim,
                    hypothesis.parent_hypothesis_id,
                    list(hypothesis.creating_evidence_refs),
                    list(hypothesis.lineage),
                ),
            ).fetchone()
            if inserted is not None:
                self.connection.execute(
                    """INSERT INTO merlin_storage.campaign_hypothesis_events
                           (id, hypothesis_id, state, evidence_refs, reason)
                       VALUES (%s, %s, 'untested', %s, %s)""",
                    (
                        f"hypothesis-created:{hypothesis.id}",
                        hypothesis.id,
                        list(hypothesis.state_evidence_refs),
                        hypothesis.state_reason,
                    ),
                )
            stored = self.get(hypothesis.id)
            if stored is None:
                raise RuntimeError("hypothesis insert did not persist")
            if self._identity(stored) != self._identity(hypothesis):
                raise ValueError("hypothesis identity conflicts with an existing record")
            return stored

    def transition(
        self,
        hypothesis_id: str,
        event_id: str,
        expected_state: HypothesisState,
        state: HypothesisState,
        *,
        evidence_refs: tuple[str, ...] = (),
        reason: str | None = None,
        infrastructure_failure_category: InfrastructureFailureCategory | None = None,
    ) -> HypothesisRecord:
        if state not in _HYPOTHESIS_TRANSITIONS.get(expected_state, frozenset()):
            raise ValueError(f"invalid hypothesis transition: {expected_state} -> {state}")
        if len(set(evidence_refs)) != len(evidence_refs):
            raise ValueError("hypothesis state evidence references must be unique")
        blocked = state == HypothesisState.BLOCKED_BY_INFRASTRUCTURE
        if blocked != (infrastructure_failure_category is not None):
            raise ValueError("infrastructure blocks require exactly one failure category")
        if state in {
            HypothesisState.SUPPORTED,
            HypothesisState.FALSIFIED,
            HypothesisState.SUPERSEDED,
        } and (not evidence_refs or not (reason or "").strip()):
            raise ValueError("terminal hypothesis transitions require evidence and a reason")
        if blocked and not (reason or "").strip():
            raise ValueError("infrastructure blocks require a reason")

        with self.connection.transaction():
            existing = self.connection.execute(
                """SELECT state, evidence_refs, reason, infrastructure_failure_category
                   FROM merlin_storage.campaign_hypothesis_events WHERE id = %s""",
                (event_id,),
            ).fetchone()
            if existing is not None:
                expected_event = (
                    state.value,
                    evidence_refs,
                    reason,
                    None
                    if infrastructure_failure_category is None
                    else infrastructure_failure_category.value,
                )
                stored_event = (
                    str(existing["state"]),
                    tuple(existing["evidence_refs"]),
                    existing["reason"],
                    existing["infrastructure_failure_category"],
                )
                if stored_event != expected_event:
                    raise ValueError("hypothesis lifecycle event identity conflicts")
                current = self.get(hypothesis_id)
                if current is None:
                    raise KeyError(hypothesis_id)
                return current

            locked = self.connection.execute(
                "SELECT id FROM merlin_storage.campaign_hypotheses WHERE id = %s FOR UPDATE",
                (hypothesis_id,),
            ).fetchone()
            if locked is None:
                raise KeyError(hypothesis_id)
            current = self.get(hypothesis_id)
            if current is None:
                raise KeyError(hypothesis_id)
            if current.state != expected_state:
                raise ValueError(
                    f"hypothesis compare-and-set failed: expected {expected_state}, "
                    f"found {current.state}"
                )
            self.connection.execute(
                """INSERT INTO merlin_storage.campaign_hypothesis_events
                       (id, hypothesis_id, state, evidence_refs, reason,
                        infrastructure_failure_category)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (
                    event_id,
                    hypothesis_id,
                    state.value,
                    list(evidence_refs),
                    reason,
                    None
                    if infrastructure_failure_category is None
                    else infrastructure_failure_category.value,
                ),
            )
            stored = self.get(hypothesis_id)
            if stored is None:
                raise RuntimeError("hypothesis transition disappeared")
            return stored

    def get(self, hypothesis_id: str) -> HypothesisRecord | None:
        row = self.connection.execute(
            """SELECT hypothesis.*, event.state, event.evidence_refs AS state_evidence_refs,
                      event.reason AS state_reason, event.infrastructure_failure_category,
                      event.created_at AS updated_at
               FROM merlin_storage.campaign_hypotheses AS hypothesis
               JOIN LATERAL (
                   SELECT state, evidence_refs, reason, infrastructure_failure_category,
                          created_at
                   FROM merlin_storage.campaign_hypothesis_events
                   WHERE hypothesis_id = hypothesis.id
                   ORDER BY sequence DESC LIMIT 1
               ) AS event ON true
               WHERE hypothesis.id = %s""",
            (hypothesis_id,),
        ).fetchone()
        return None if row is None else self._from_row(row)

    def list_for_campaign(self, campaign_id: str) -> tuple[HypothesisRecord, ...]:
        rows = self.connection.execute(
            """SELECT hypothesis.id
               FROM merlin_storage.campaign_hypotheses AS hypothesis
               WHERE campaign_id = %s ORDER BY created_at, id""",
            (campaign_id,),
        ).fetchall()
        return tuple(
            hypothesis for row in rows if (hypothesis := self.get(str(row["id"]))) is not None
        )

    @staticmethod
    def _identity(hypothesis: HypothesisRecord) -> tuple[object, ...]:
        return (
            hypothesis.schema_version,
            hypothesis.id,
            hypothesis.campaign_id,
            hypothesis.target_id,
            hypothesis.scenario_fingerprint,
            hypothesis.claim,
            hypothesis.parent_hypothesis_id,
            hypothesis.creating_evidence_refs,
            hypothesis.lineage,
        )

    @staticmethod
    def _from_row(row: Mapping[str, object]) -> HypothesisRecord:
        return HypothesisRecord(
            schema_version=int(row["schema_version"]),
            id=str(row["id"]),
            campaign_id=str(row["campaign_id"]),
            target_id=str(row["target_id"]),
            scenario_fingerprint=str(row["scenario_fingerprint"]),
            claim=str(row["claim"]),
            state=str(row["state"]),
            parent_hypothesis_id=(
                None if row["parent_hypothesis_id"] is None else str(row["parent_hypothesis_id"])
            ),
            creating_evidence_refs=tuple(row["creating_evidence_refs"]),
            lineage=tuple(row["lineage"]),
            state_evidence_refs=tuple(row["state_evidence_refs"]),
            state_reason=None if row["state_reason"] is None else str(row["state_reason"]),
            infrastructure_failure_category=row["infrastructure_failure_category"],
            created_at=row["created_at"] if isinstance(row["created_at"], datetime) else None,
            updated_at=row["updated_at"] if isinstance(row["updated_at"], datetime) else None,
        )


class CampaignObservabilityRepository:
    """Append-only counter and full-identity ARM outcome authority."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def record_counter_event(self, event: CampaignCounterEvent) -> CampaignCounterEvent:
        with self.connection.transaction():
            self._validate_campaign_references(event)
            self.connection.execute(
                """INSERT INTO merlin_storage.campaign_counter_events
                       (id, campaign_id, arm_id, hypothesis_id, technique_attempt_id,
                        target_calls, model_steps, validation_calls, reproduction_calls,
                        infrastructure_failures, infrastructure_failure_category)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO NOTHING""",
                self._counter_values(event),
            )
            stored = self.get_counter_event(event.id)
            if stored is None:
                raise RuntimeError("campaign counter event did not persist")
            if self._counter_identity(stored) != self._counter_identity(event):
                raise ValueError("campaign counter event identity conflicts")
            return stored

    def _validate_campaign_references(self, event: CampaignCounterEvent) -> None:
        references = (
            (
                "hypothesis",
                "campaign_hypotheses",
                "id",
                event.hypothesis_id,
            ),
            (
                "technique attempt",
                "campaign_technique_attempts",
                "id",
                event.technique_attempt_id,
            ),
        )
        for label, table, column, reference_id in references:
            if reference_id is None:
                continue
            matched = self.connection.execute(
                f"SELECT 1 FROM merlin_storage.{table} WHERE {column} = %s AND campaign_id = %s",
                (reference_id, event.campaign_id),
            ).fetchone()
            if matched is None:
                raise ValueError(f"{label} does not belong to campaign {event.campaign_id!r}")

    def get_counter_event(self, event_id: str) -> CampaignCounterEvent | None:
        row = self.connection.execute(
            "SELECT * FROM merlin_storage.campaign_counter_events WHERE id = %s",
            (event_id,),
        ).fetchone()
        return None if row is None else self._counter_from_row(row)

    def counters(self, campaign_id: str) -> CampaignCounters:
        row = self.connection.execute(
            """SELECT coalesce(sum(target_calls), 0) AS target_calls,
                      coalesce(sum(model_steps), 0) AS model_steps,
                      coalesce(sum(validation_calls), 0) AS validation_calls,
                      coalesce(sum(reproduction_calls), 0) AS reproduction_calls,
                      coalesce(sum(infrastructure_failures), 0) AS infrastructure_failures
               FROM merlin_storage.campaign_counter_events WHERE campaign_id = %s""",
            (campaign_id,),
        ).fetchone()
        return CampaignCounters.model_validate(row)

    def record_arm_outcome(self, outcome: ArmOutcome) -> ArmOutcome:
        event = CampaignCounterEvent(
            id=outcome.id,
            campaign_id=outcome.campaign_id,
            arm_id=outcome.arm_id,
            hypothesis_id=outcome.hypothesis_id,
            technique_attempt_id=outcome.technique_attempt_id,
            counters=outcome.counters,
            infrastructure_failure_category=outcome.infrastructure_failure_category,
        )
        with self.connection.transaction():
            arm = self.connection.execute(
                """SELECT target_id, scenario_fingerprint, surface_id, technique_id,
                          controller_id, mutation_id, actual_language, state_id
                   FROM merlin_storage.campaign_arms WHERE id = %s""",
                (outcome.arm_id,),
            ).fetchone()
            if arm is None:
                raise KeyError(outcome.arm_id)
            if ArmIdentity.model_validate(arm) != outcome.identity:
                raise ValueError("arm outcome identity does not match the durable arm")
            existing = self.connection.execute(
                """SELECT id, counter_event_id
                   FROM merlin_storage.campaign_arm_outcomes
                   WHERE campaign_id = %s AND arm_id = %s AND technique_attempt_id = %s""",
                (outcome.campaign_id, outcome.arm_id, outcome.technique_attempt_id),
            ).fetchone()
            if existing is not None and str(existing["id"]) != outcome.id:
                raise ValueError("ARM execution already has a different durable outcome identity")
            if existing is None:
                self.record_counter_event(event)
            else:
                self._validate_campaign_references(event)
                self.connection.execute(
                    """UPDATE merlin_storage.campaign_counter_events
                       SET target_calls = %s, model_steps = %s, validation_calls = %s,
                           reproduction_calls = %s, infrastructure_failures = %s,
                           infrastructure_failure_category = %s
                       WHERE id = %s AND campaign_id = %s""",
                    (
                        outcome.counters.target_calls,
                        outcome.counters.model_steps,
                        outcome.counters.validation_calls,
                        outcome.counters.reproduction_calls,
                        outcome.counters.infrastructure_failures,
                        None
                        if outcome.infrastructure_failure_category is None
                        else outcome.infrastructure_failure_category.value,
                        str(existing["counter_event_id"]),
                        outcome.campaign_id,
                    ),
                )
            self.connection.execute(
                """INSERT INTO merlin_storage.campaign_arm_outcomes
                       (id, schema_version, campaign_id, arm_id, technique_attempt_id,
                        hypothesis_id, target_id, scenario_fingerprint, surface_id,
                        technique_id, controller_id, mutation_id, actual_language, state_id,
                        requested_language, language_evidence_status,
                        language_evidence_source,
                        outcome, counter_event_id, evidence_refs, signal, distance_to_success,
                        latency_ms, model_cost_usd, usage, reason)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (campaign_id, arm_id, technique_attempt_id) DO UPDATE SET
                       requested_language = EXCLUDED.requested_language,
                       language_evidence_status = EXCLUDED.language_evidence_status,
                       language_evidence_source = EXCLUDED.language_evidence_source,
                       outcome = EXCLUDED.outcome,
                       evidence_refs = EXCLUDED.evidence_refs,
                       signal = EXCLUDED.signal,
                       distance_to_success = EXCLUDED.distance_to_success,
                       latency_ms = EXCLUDED.latency_ms,
                       model_cost_usd = EXCLUDED.model_cost_usd,
                       usage = EXCLUDED.usage,
                       reason = EXCLUDED.reason
                   WHERE campaign_arm_outcomes.id = EXCLUDED.id""",
                (
                    outcome.id,
                    outcome.schema_version,
                    outcome.campaign_id,
                    outcome.arm_id,
                    outcome.technique_attempt_id,
                    outcome.hypothesis_id,
                    outcome.identity.target_id,
                    outcome.identity.scenario_fingerprint,
                    outcome.identity.surface_id,
                    outcome.identity.technique_id,
                    outcome.identity.controller_id,
                    outcome.identity.mutation_id,
                    outcome.identity.actual_language,
                    outcome.identity.state_id,
                    outcome.requested_language,
                    outcome.language_evidence_status.value,
                    outcome.language_evidence_source,
                    outcome.outcome.value,
                    event.id,
                    list(outcome.evidence_refs),
                    outcome.signal,
                    outcome.distance_to_success,
                    outcome.latency_ms,
                    outcome.model_cost_usd,
                    Jsonb(dict(outcome.usage)),
                    outcome.reason,
                ),
            )
            stored = self.get_arm_outcome(outcome.id)
            if stored is None:
                raise RuntimeError("arm outcome did not persist")
            if self._outcome_identity(stored) != self._outcome_identity(outcome):
                raise ValueError("arm outcome identity conflicts")
            return stored

    def get_arm_outcome(self, outcome_id: str) -> ArmOutcome | None:
        row = self.connection.execute(
            """SELECT outcome.*, counters.target_calls, counters.model_steps,
                      counters.validation_calls, counters.reproduction_calls,
                      counters.infrastructure_failures,
                      counters.infrastructure_failure_category
               FROM merlin_storage.campaign_arm_outcomes AS outcome
               JOIN merlin_storage.campaign_counter_events AS counters
                 ON counters.id = outcome.counter_event_id
               WHERE outcome.id = %s""",
            (outcome_id,),
        ).fetchone()
        return None if row is None else self._outcome_from_row(row)

    def list_arm_outcomes(self, campaign_id: str) -> tuple[ArmOutcome, ...]:
        rows = self.connection.execute(
            """SELECT id FROM merlin_storage.campaign_arm_outcomes
               WHERE campaign_id = %s ORDER BY created_at, id""",
            (campaign_id,),
        ).fetchall()
        return tuple(
            outcome for row in rows if (outcome := self.get_arm_outcome(str(row["id"]))) is not None
        )

    @staticmethod
    def _counter_values(event: CampaignCounterEvent) -> tuple[object, ...]:
        return (
            event.id,
            event.campaign_id,
            event.arm_id,
            event.hypothesis_id,
            event.technique_attempt_id,
            event.counters.target_calls,
            event.counters.model_steps,
            event.counters.validation_calls,
            event.counters.reproduction_calls,
            event.counters.infrastructure_failures,
            None
            if event.infrastructure_failure_category is None
            else event.infrastructure_failure_category.value,
        )

    @staticmethod
    def _counter_identity(event: CampaignCounterEvent) -> tuple[object, ...]:
        return (
            event.id,
            event.campaign_id,
            event.arm_id,
            event.hypothesis_id,
            event.technique_attempt_id,
            event.counters,
            event.infrastructure_failure_category,
        )

    @staticmethod
    def _counter_from_row(row: Mapping[str, object]) -> CampaignCounterEvent:
        return CampaignCounterEvent(
            id=str(row["id"]),
            campaign_id=str(row["campaign_id"]),
            arm_id=None if row["arm_id"] is None else str(row["arm_id"]),
            hypothesis_id=(None if row["hypothesis_id"] is None else str(row["hypothesis_id"])),
            technique_attempt_id=(
                None if row["technique_attempt_id"] is None else str(row["technique_attempt_id"])
            ),
            counters=CampaignCounters(
                target_calls=int(row["target_calls"]),
                model_steps=int(row["model_steps"]),
                validation_calls=int(row["validation_calls"]),
                reproduction_calls=int(row["reproduction_calls"]),
                infrastructure_failures=int(row["infrastructure_failures"]),
            ),
            infrastructure_failure_category=row["infrastructure_failure_category"],
            created_at=row["created_at"] if isinstance(row["created_at"], datetime) else None,
        )

    @staticmethod
    def _outcome_identity(outcome: ArmOutcome) -> str:
        return json.dumps(
            outcome.model_dump(mode="json", exclude={"created_at"}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @staticmethod
    def _outcome_from_row(row: Mapping[str, object]) -> ArmOutcome:
        return ArmOutcome(
            schema_version=int(row["schema_version"]),
            id=str(row["id"]),
            campaign_id=str(row["campaign_id"]),
            arm_id=str(row["arm_id"]),
            technique_attempt_id=str(row["technique_attempt_id"]),
            hypothesis_id=(None if row["hypothesis_id"] is None else str(row["hypothesis_id"])),
            identity=ArmIdentity(
                target_id=str(row["target_id"]),
                scenario_fingerprint=str(row["scenario_fingerprint"]),
                surface_id=str(row["surface_id"]),
                technique_id=str(row["technique_id"]),
                controller_id=str(row["controller_id"]),
                mutation_id=str(row["mutation_id"]),
                actual_language=str(row["actual_language"]),
                state_id=str(row["state_id"]),
            ),
            requested_language=str(row["requested_language"]),
            actual_language=str(row["actual_language"]),
            language_evidence_status=str(row["language_evidence_status"]),
            language_evidence_source=str(row["language_evidence_source"]),
            outcome=str(row["outcome"]),
            counters=CampaignCounters(
                target_calls=int(row["target_calls"]),
                model_steps=int(row["model_steps"]),
                validation_calls=int(row["validation_calls"]),
                reproduction_calls=int(row["reproduction_calls"]),
                infrastructure_failures=int(row["infrastructure_failures"]),
            ),
            evidence_refs=tuple(row["evidence_refs"]),
            signal=None if row["signal"] is None else float(row["signal"]),
            distance_to_success=(
                None if row["distance_to_success"] is None else float(row["distance_to_success"])
            ),
            latency_ms=int(row["latency_ms"]),
            model_cost_usd=float(row["model_cost_usd"]),
            usage=dict(row["usage"] or {}),
            infrastructure_failure_category=row["infrastructure_failure_category"],
            reason=str(row["reason"]),
            created_at=row["created_at"] if isinstance(row["created_at"], datetime) else None,
        )


class TechniqueLanguageEvidenceRepository:
    """Write-once requested-versus-actual language evidence for target attempts."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def record(self, evidence: TechniqueLanguageEvidence) -> TechniqueLanguageEvidence:
        with self.connection.transaction():
            self.connection.execute(
                """INSERT INTO merlin_storage.technique_language_evidence
                       (technique_attempt_id, campaign_id, requested_language, actual_language,
                        language_evidence_status, language_evidence_source, evidence_refs)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (technique_attempt_id) DO NOTHING""",
                (
                    evidence.technique_attempt_id,
                    evidence.campaign_id,
                    evidence.requested_language,
                    evidence.actual_language,
                    evidence.language_evidence_status.value,
                    evidence.language_evidence_source,
                    list(evidence.evidence_refs),
                ),
            )
            stored = self.get(evidence.technique_attempt_id)
            if stored is None:
                raise RuntimeError("technique language evidence did not persist")
            comparable_fields = {"created_at", "evidence_refs"}
            if stored.model_dump(exclude=comparable_fields) != evidence.model_dump(
                exclude=comparable_fields
            ):
                raise ValueError("technique language evidence conflicts with existing record")
            # The table is deliberately append-only and keyed by technique
            # attempt.  A multi-turn attempt can produce several compatible
            # delivery receipts; the first row remains its immutable language
            # observation while later matching receipts are idempotent.
            return stored

    def get(self, technique_attempt_id: str) -> TechniqueLanguageEvidence | None:
        row = self.connection.execute(
            """SELECT * FROM merlin_storage.technique_language_evidence
               WHERE technique_attempt_id = %s""",
            (technique_attempt_id,),
        ).fetchone()
        return None if row is None else self._from_row(row)

    def list_for_campaign(self, campaign_id: str) -> tuple[TechniqueLanguageEvidence, ...]:
        rows = self.connection.execute(
            """SELECT * FROM merlin_storage.technique_language_evidence
               WHERE campaign_id = %s ORDER BY created_at, technique_attempt_id""",
            (campaign_id,),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    @staticmethod
    def _from_row(row: Mapping[str, object]) -> TechniqueLanguageEvidence:
        return TechniqueLanguageEvidence(
            technique_attempt_id=str(row["technique_attempt_id"]),
            campaign_id=str(row["campaign_id"]),
            requested_language=str(row["requested_language"]),
            actual_language=str(row["actual_language"]),
            language_evidence_status=str(row["language_evidence_status"]),
            language_evidence_source=str(row["language_evidence_source"]),
            evidence_refs=tuple(row["evidence_refs"]),
            created_at=row["created_at"] if isinstance(row["created_at"], datetime) else None,
        )
