"""Durable authority for the campaign bandit arm pool.

``CampaignArmStore`` owns the ``merlin_storage.campaign_arms`` table and mirrors
``CampaignStore``'s connection style: it is constructed around one serialized
connection and delegates all statements to ``self.connection.execute``. It never
opens its own connection, so the daemon that already owns a connection can wrap
it alongside the other campaign repositories without a second pool.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from merlin_contracts import (
    ArmIdentity,
    ArmOutcome,
    ArmOutcomeKind,
    CampaignCounters,
    HypothesisRecord,
    HypothesisState,
    InfrastructureFailureCategory,
    LanguageEvidenceStatus,
)
from psycopg.types.json import Jsonb

from .observability_store import CampaignObservabilityRepository, HypothesisRepository

_STATUS = {"trial", "active", "retired"}
_SOURCE = {
    "seed",
    "brainstorm",
    "axis-expansion",
    "gap-fill",
    "win-generalization",
    "mutation",
    "crossover",
    "failure-derived",
    "boundary",
    "contextual",
    "catalog-mutation",
}
_REQUIRED = (
    "id",
    "target_id",
    "scenario_fingerprint",
    "surface_id",
    "semantic_id",
    "strategy_id",
    "technique_id",
    "controller_id",
    "mutation_id",
    "actual_language",
    "state_id",
    "objective",
    "source",
    "rank_components",
    "eligibility_reasons",
    "decision_reason",
)


def _jsonb_canonicalize(value: object) -> object:
    """Preserve structured metadata in PostgreSQL JSONB's deterministic key order."""
    if isinstance(value, Mapping):
        return {
            str(key): _jsonb_canonicalize(item)
            for key, item in sorted(
                value.items(), key=lambda pair: (len(str(pair[0]).encode()), str(pair[0]))
            )
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_jsonb_canonicalize(item) for item in value]
    return value


class CampaignArmStore:
    """Read/write access to admitted bandit arms for one campaign target."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def record_arm(self, arm: Mapping[str, object]) -> None:
        """Insert one arm idempotently under its complete schedulable identity.

        Replaying the same natural key is a no-op (``ON CONFLICT DO NOTHING``),
        so brainstorm and seed admission can be retried safely. ``axis_coords``
        is optional JSON coordinates on the four-axis taxonomy (register,
        framing, encoding, structure) persisted as ``jsonb``.
        """
        missing = [name for name in _REQUIRED if not str(arm.get(name) or "").strip()]
        if missing:
            raise ValueError(f"campaign arm is missing required fields: {', '.join(missing)}")
        status = str(arm.get("status") or "trial")
        if status not in _STATUS:
            raise ValueError(f"campaign arm status must be one of {sorted(_STATUS)}")
        source = str(arm["source"])
        if source not in _SOURCE:
            raise ValueError(f"campaign arm source must be one of {sorted(_SOURCE)}")
        axis_coords_raw = arm.get("axis_coords")
        if axis_coords_raw is not None and not isinstance(axis_coords_raw, Mapping):
            raise ValueError("campaign arm axis_coords must be a mapping")
        axis_coords = None if axis_coords_raw is None else Jsonb(dict(axis_coords_raw))
        reachability_raw = arm.get("execution_reachability")
        if reachability_raw is not None and not isinstance(reachability_raw, Mapping):
            raise ValueError("campaign arm execution_reachability must be a mapping")
        reachability = None if reachability_raw is None else Jsonb(dict(reachability_raw))
        rank_components_raw = arm["rank_components"]
        if not isinstance(rank_components_raw, Mapping):
            raise ValueError("campaign arm rank_components must be a mapping")
        eligibility_reasons_raw = arm["eligibility_reasons"]
        if not isinstance(eligibility_reasons_raw, Sequence) or isinstance(
            eligibility_reasons_raw, (str, bytes)
        ):
            raise ValueError("campaign arm eligibility_reasons must be a sequence")
        eligibility_reasons = tuple(str(reason).strip() for reason in eligibility_reasons_raw)
        if not eligibility_reasons or any(not reason for reason in eligibility_reasons):
            raise ValueError("campaign arm eligibility_reasons cannot be empty")
        self.connection.execute(
            """INSERT INTO merlin_storage.campaign_arms
                   (id, target_id, scenario_fingerprint, surface_id, semantic_id, strategy_id,
                    technique_id, controller_id, mutation_id, actual_language, state_id,
                    objective, source,
                    novelty_digest, status, axis_coords, rank_components, eligibility_reasons,
                    decision_reason, execution_reachability)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s)
               ON CONFLICT (target_id, scenario_fingerprint, surface_id, semantic_id,
                            technique_id, controller_id, mutation_id, actual_language, state_id)
               DO NOTHING""",
            (
                str(arm["id"]),
                str(arm["target_id"]),
                str(arm["scenario_fingerprint"]),
                str(arm["surface_id"]),
                str(arm["semantic_id"]),
                str(arm["strategy_id"]),
                str(arm["technique_id"]),
                str(arm["controller_id"]),
                str(arm["mutation_id"]),
                str(arm["actual_language"]),
                str(arm["state_id"]),
                str(arm["objective"]),
                source,
                arm.get("novelty_digest"),
                status,
                axis_coords,
                Jsonb(dict(rank_components_raw)),
                list(eligibility_reasons),
                str(arm["decision_reason"]),
                reachability,
            ),
        )

    def list_arms(self, target_id: str, scenario_fingerprint: str) -> list[dict[str, Any]]:
        """Return every arm admitted for a target scenario, oldest first."""
        rows = self.connection.execute(
            """SELECT id, target_id, scenario_fingerprint, surface_id, semantic_id,
                      strategy_id, technique_id, controller_id, mutation_id,
                      actual_language, state_id,
                      objective, source, novelty_digest, status, axis_coords, rank_components,
                      eligibility_reasons, decision_reason, execution_reachability,
                      created_at, updated_at
               FROM merlin_storage.campaign_arms
               WHERE target_id = %s AND scenario_fingerprint = %s
               ORDER BY created_at, id""",
            (target_id, scenario_fingerprint),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_retired(self, arm_id: str) -> None:
        """Transition an arm to ``retired`` without deleting its history."""
        self.connection.execute(
            """UPDATE merlin_storage.campaign_arms
               SET status = 'retired', updated_at = now()
               WHERE id = %s""",
            (arm_id,),
        )

    def set_lifecycle(self, arm_id: str, status: str) -> None:
        """Persist a ranking lifecycle transition without deleting evidence."""
        if status not in _STATUS:
            raise ValueError(f"campaign arm status must be one of {sorted(_STATUS)}")
        self.connection.execute(
            "UPDATE merlin_storage.campaign_arms SET status = %s, updated_at = now() WHERE id = %s",
            (status, arm_id),
        )

    def record_arm_observation(self, observation: Mapping[str, object]) -> None:
        """Append an attributable execution result to the canonical outcome ledger."""
        identity_raw = observation.get("identity")
        if not isinstance(identity_raw, Mapping):
            raise ValueError("arm observation requires a canonical identity")
        identity = ArmIdentity.model_validate(identity_raw)
        self._ensure_arm_record(observation, identity)
        outcome = ArmOutcomeKind(str(observation["outcome"]))
        infrastructure = outcome == ArmOutcomeKind.INFRASTRUCTURE_FAILURE
        quality = outcome not in {ArmOutcomeKind.INFRASTRUCTURE_FAILURE, ArmOutcomeKind.AMBIGUOUS}
        if quality and int(observation.get("target_calls") or 0) < 1:
            raise ValueError("quality ARM outcomes require an acknowledged target call")
        category = (
            InfrastructureFailureCategory(str(observation["infrastructure_failure_category"]))
            if infrastructure
            else None
        )
        evidence_refs = tuple(str(value) for value in observation.get("evidence_refs", ()))
        supplied_attempt_id = str(observation["technique_attempt_id"])
        attempt = self.connection.execute(
            """SELECT id FROM merlin_storage.campaign_technique_attempts
               WHERE campaign_id = %s
                 AND (id::text = %s OR scheduler_attempt_id::text = %s)""",
            (str(observation["campaign_id"]), supplied_attempt_id, supplied_attempt_id),
        ).fetchone()
        if attempt is None:
            raise ValueError("ARM outcome has no attributable campaign technique attempt")
        technique_attempt_id = str(attempt["id"])
        uncertainties_raw = observation.get("evaluation_uncertainties", ())
        if not isinstance(uncertainties_raw, Sequence) or isinstance(
            uncertainties_raw, (str, bytes)
        ):
            raise ValueError("ARM evaluation uncertainties must be a sequence of mappings")
        if any(not isinstance(item, Mapping) for item in uncertainties_raw):
            raise ValueError("ARM evaluation uncertainties must contain only mappings")
        usage = dict(observation["usage"]) if isinstance(observation.get("usage"), Mapping) else {}
        if uncertainties_raw:
            usage["evaluation_uncertainties"] = _jsonb_canonicalize(uncertainties_raw)
        usage = _jsonb_canonicalize(usage)
        if not isinstance(usage, dict):
            raise TypeError("canonical ARM usage metadata must remain a mapping")
        stable = "\n".join(
            (
                str(observation["campaign_id"]),
                str(observation["arm_id"]),
                technique_attempt_id,
            )
        )
        CampaignObservabilityRepository(self.connection).record_arm_outcome(
            ArmOutcome(
                id="arm-outcome:" + hashlib.sha256(stable.encode()).hexdigest(),
                campaign_id=str(observation["campaign_id"]),
                arm_id=str(observation["arm_id"]),
                technique_attempt_id=technique_attempt_id,
                hypothesis_id=(
                    None
                    if observation.get("hypothesis_id") is None
                    else str(observation["hypothesis_id"])
                ),
                identity=identity,
                requested_language=str(
                    observation.get("requested_language") or identity.actual_language
                ),
                actual_language=identity.actual_language,
                language_evidence_status=LanguageEvidenceStatus.OBSERVED,
                language_evidence_source="committed target receipt",
                outcome=outcome,
                attempt_disposition=observation.get("attempt_disposition") or "uncertain",
                outcome_authority=observation.get("outcome_authority") or "none",
                counters=CampaignCounters(
                    target_calls=int(observation.get("target_calls") or 0),
                    model_steps=max(1, int(observation.get("model_steps") or 0)),
                    infrastructure_failures=int(infrastructure),
                ),
                evidence_refs=evidence_refs,
                signal=(
                    None if observation.get("signal") is None else float(observation["signal"])
                ),
                distance_to_success=(
                    None
                    if observation.get("distance_to_success") is None
                    else float(observation["distance_to_success"])
                ),
                latency_ms=int(observation.get("latency_ms") or 0),
                model_cost_usd=float(observation.get("model_cost_usd") or 0.0),
                usage=usage,
                infrastructure_failure_category=category,
                reason=str(observation.get("reason") or "committed ARM execution outcome"),
            )
        )

    def _ensure_arm_record(self, observation: Mapping[str, object], identity: ArmIdentity) -> None:
        """Materialize the durable arm row when terminal evidence outran admission.

        Admission paths that never persisted the ``campaign_arms`` row (the ARM
        discovery sink is optional, and legacy assignments predate it) otherwise
        make the outcome insert fail its FK check and worker settlement drops
        the costed outcome entirely. The observation carries the full sealed arm
        contract, so the row can be rebuilt idempotently; when it does not, the
        downstream FK lookup keeps raising ``KeyError`` exactly as before.
        """
        arm_id = str(observation["arm_id"])
        exists = self.connection.execute(
            "SELECT 1 FROM merlin_storage.campaign_arms WHERE id = %s",
            (arm_id,),
        ).fetchone()
        if exists is not None:
            return
        arm = observation.get("arm")
        if not isinstance(arm, Mapping):
            return
        record: dict[str, object] = {
            "id": arm_id,
            "target_id": identity.target_id,
            "scenario_fingerprint": identity.scenario_fingerprint,
            "surface_id": identity.surface_id,
            "semantic_id": str(arm.get("semantic_id") or identity.technique_id),
            "strategy_id": str(arm.get("strategy_id") or "direct"),
            "technique_id": identity.technique_id,
            "controller_id": identity.controller_id,
            "mutation_id": identity.mutation_id,
            "actual_language": identity.actual_language,
            "state_id": identity.state_id,
            "objective": str(arm.get("objective") or f"executed arm {arm_id}"),
            "source": str(arm.get("source") or "seed"),
            "rank_components": (
                dict(arm["rank_components"])
                if isinstance(arm.get("rank_components"), Mapping)
                else {}
            ),
            "eligibility_reasons": (
                tuple(str(reason) for reason in arm["eligibility_reasons"])  # type: ignore[index]
                if isinstance(arm.get("eligibility_reasons"), Sequence)
                and not isinstance(arm.get("eligibility_reasons"), (str, bytes))
                and tuple(str(reason) for reason in arm["eligibility_reasons"])  # type: ignore[index]
                else (
                    "durable arm row materialized at terminal outcome recording; "
                    "admission-time persistence was not wired for this campaign",
                )
            ),
            "decision_reason": str(
                arm.get("decision_reason") or "materialized at terminal outcome recording"
            ),
        }
        for optional in ("novelty_digest", "status", "axis_coords", "execution_reachability"):
            value = arm.get(optional)
            if value is not None:
                record[optional] = value
        try:
            self.record_arm(record)
        except (KeyError, TypeError, ValueError) as error:
            raise KeyError(arm_id) from error

    def record_hypothesis(self, value: Mapping[str, object]) -> HypothesisRecord:
        """Atomically persist lineage, admission, retry, and parent supersession."""
        with self.connection.transaction():
            return self._record_hypothesis(value)

    def _record_hypothesis(self, value: Mapping[str, object]) -> HypothesisRecord:
        """Persist an accepted frontier identity and its evidence-backed admission."""
        hypothesis_id = str(value["id"])
        parent_id = value.get("parent_hypothesis_id")
        repository = HypothesisRepository(self.connection)
        parent = None if parent_id is None else repository.get(str(parent_id))
        if parent_id is not None and parent is None:
            raise ValueError("durable hypothesis parent does not exist")
        record = HypothesisRecord(
            id=hypothesis_id,
            campaign_id=str(value["campaign_id"]),
            target_id=str(value["target_id"]),
            scenario_fingerprint=str(value["scenario_fingerprint"]),
            claim=str(value["claim"]),
            parent_hypothesis_id=None if parent is None else parent.id,
            creating_evidence_refs=tuple(str(item) for item in value["evidence_refs"]),
            lineage=(hypothesis_id,) if parent is None else (*parent.lineage, hypothesis_id),
        )
        stored = repository.create(record)
        recovery_refs = tuple(
            str(item) for item in value.get("adapter_health_recovery_evidence_refs", ())
        )
        if stored.state == HypothesisState.BLOCKED_BY_INFRASTRUCTURE:
            if value.get("adapter_health_recovered") is not True or not recovery_refs:
                return stored
            stored = repository.transition(
                hypothesis_id,
                "hypothesis-retry:"
                + hashlib.sha256("\n".join((hypothesis_id, *recovery_refs)).encode()).hexdigest(),
                HypothesisState.BLOCKED_BY_INFRASTRUCTURE,
                HypothesisState.IN_PROGRESS,
                evidence_refs=recovery_refs,
                reason="adapter health recovery was explicitly acknowledged",
            )
        if stored.state == HypothesisState.UNTESTED:
            stored = repository.transition(
                hypothesis_id,
                f"hypothesis-admitted:{hypothesis_id}",
                HypothesisState.UNTESTED,
                HypothesisState.IN_PROGRESS,
                evidence_refs=record.creating_evidence_refs,
                reason=str(
                    value.get("admission_reason")
                    or "independent critic admitted a receipt-bound experiment"
                ),
            )
        if parent is not None and parent.state in {
            HypothesisState.UNTESTED,
            HypothesisState.IN_PROGRESS,
            HypothesisState.SUPPORTED,
            HypothesisState.BLOCKED_BY_INFRASTRUCTURE,
        }:
            repository.transition(
                parent.id,
                f"hypothesis-superseded:{hypothesis_id}",
                parent.state,
                HypothesisState.SUPERSEDED,
                evidence_refs=record.creating_evidence_refs,
                reason=f"durable child {hypothesis_id} supersedes this hypothesis",
            )
        return stored

    def get_hypothesis(self, hypothesis_id: str) -> HypothesisRecord | None:
        """Return the globally content-addressed hypothesis, if already recorded."""
        return HypothesisRepository(self.connection).get(str(hypothesis_id))

    def list_hypotheses(self, campaign_id: str) -> tuple[HypothesisRecord, ...]:
        """Return the durable campaign portfolio for cross-wave capacity checks."""
        return HypothesisRepository(self.connection).list_for_campaign(campaign_id)

    def record_arm_ranking(self, arm: Mapping[str, object]) -> None:
        """Persist the latest explainable rank decision for restart-safe selection."""
        components = arm.get("rank_components")
        reasons = arm.get("eligibility_reasons")
        if not isinstance(components, Mapping) or not isinstance(reasons, Sequence):
            raise ValueError("ARM ranking requires components and eligibility reasons")
        self.connection.execute(
            """UPDATE merlin_storage.campaign_arms
               SET rank_components = %s, eligibility_reasons = %s,
                   decision_reason = %s, updated_at = now()
               WHERE id = %s""",
            (
                Jsonb(dict(components)),
                [str(item) for item in reasons],
                str(arm["decision_reason"]),
                str(arm["id"]),
            ),
        )

    def transition_hypothesis(self, value: Mapping[str, object]) -> HypothesisRecord:
        """Append a terminal hypothesis event derived from committed execution evidence."""
        repository = HypothesisRepository(self.connection)
        hypothesis_id = str(value["hypothesis_id"])
        current = repository.get(hypothesis_id)
        if current is None:
            raise KeyError(hypothesis_id)
        outcome = str(value["outcome"])
        state = {
            "authoritative_success": HypothesisState.SUPPORTED,
            "validated_finding": HypothesisState.SUPPORTED,
            "clean_negative": HypothesisState.FALSIFIED,
            "infrastructure_failure": HypothesisState.BLOCKED_BY_INFRASTRUCTURE,
        }.get(outcome)
        if state is None or current.state != HypothesisState.IN_PROGRESS:
            return current
        category = (
            InfrastructureFailureCategory(str(value["infrastructure_failure_category"]))
            if state == HypothesisState.BLOCKED_BY_INFRASTRUCTURE
            else None
        )
        return repository.transition(
            hypothesis_id,
            "hypothesis-outcome:"
            + hashlib.sha256(
                f"{hypothesis_id}\n{value['technique_attempt_id']}\n{outcome}".encode()
            ).hexdigest(),
            HypothesisState.IN_PROGRESS,
            state,
            evidence_refs=tuple(str(item) for item in value["evidence_refs"]),
            reason=str(value["reason"]),
            infrastructure_failure_category=category,
        )

    def list_arm_observations(
        self, target_id: str, scenario_fingerprint: str
    ) -> list[dict[str, object]]:
        """Project durable outcomes into the agent band's canonical history schema."""
        rows = self.connection.execute(
            """SELECT outcome.surface_id, outcome.technique_id, outcome.controller_id,
                      outcome.mutation_id, outcome.actual_language, outcome.state_id,
                      outcome.outcome, outcome.signal, outcome.distance_to_success,
                      outcome.attempt_disposition, outcome.outcome_authority,
                      outcome.latency_ms, outcome.model_cost_usd, outcome.usage,
                      counter.infrastructure_failure_category, arm.source
               FROM merlin_storage.campaign_arm_outcomes AS outcome
               JOIN merlin_storage.campaign_counter_events AS counter
                 ON counter.id = outcome.counter_event_id
               JOIN merlin_storage.campaign_arms AS arm ON arm.id = outcome.arm_id
               WHERE outcome.target_id = %s AND outcome.scenario_fingerprint = %s
               ORDER BY outcome.created_at, outcome.id""",
            (target_id, scenario_fingerprint),
        ).fetchall()
        outcome_map = {
            "authoritative_success": "win",
            "validated_finding": "validated_finding",
            "partial_signal": "near_miss",
            "clean_negative": "clean_negative",
            "infrastructure_failure": "infrastructure_failure",
            "ambiguous": "ambiguous",
        }
        return [
            {
                "identity": {
                    name: str(row[name])
                    for name in (
                        "surface_id",
                        "technique_id",
                        "controller_id",
                        "mutation_id",
                        "actual_language",
                        "state_id",
                    )
                },
                "outcome": outcome_map[str(row["outcome"])],
                "attempt_disposition": (
                    "uncertain"
                    if row["attempt_disposition"] is None
                    else str(row["attempt_disposition"])
                ),
                "outcome_authority": (
                    "none" if row["outcome_authority"] is None else str(row["outcome_authority"])
                ),
                "partial_signal": (
                    float(row["signal"])
                    if row["signal"] is not None
                    else max(0.0, 1.0 - float(row["distance_to_success"]))
                    if row["distance_to_success"] is not None
                    else 0.0
                ),
                "infrastructure_failure": row["infrastructure_failure_category"] is not None,
                "generalized_win": (
                    str(row["source"]) == "win-generalization"
                    and str(row["outcome"]) in {"authoritative_success", "validated_finding"}
                ),
                "boundary_signal": (
                    str(row["source"]) == "boundary"
                    and str(row["outcome"])
                    in {"authoritative_success", "validated_finding", "partial_signal"}
                ),
                "latency_ms": int(row["latency_ms"]),
                "cost_microusd": round(float(row["model_cost_usd"]) * 1_000_000),
                "evaluation_uncertainties": tuple(
                    dict(item)
                    for item in (
                        row["usage"].get("evaluation_uncertainties", ())
                        if isinstance(row["usage"], Mapping)
                        else ()
                    )
                    if isinstance(item, Mapping)
                ),
            }
            for row in rows
        ]


_AXIS_REQUIRED = ("axis", "slug", "display_name", "instruction_fragment", "origin")


class CampaignAxisStore:
    """Read/write access to the four-axis taxonomy vocabulary.

    Mirrors ``CampaignArmStore``'s connection style: it is constructed around one
    serialized connection and delegates all statements to
    ``self.connection.execute``, never opening its own connection.
    """

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def list_axis_values(self, axis: str) -> list[dict[str, Any]]:
        """Return every value for one axis, ordered by obscurity then slug."""
        rows = self.connection.execute(
            """SELECT axis, slug, display_name, instruction_fragment, origin, obscurity,
                      created_at
               FROM merlin_storage.campaign_axis_values
               WHERE axis = %s
               ORDER BY obscurity, slug""",
            (axis,),
        ).fetchall()
        return [dict(row) for row in rows]

    def insert_axis_value(self, value: Mapping[str, object]) -> None:
        """Insert one axis value idempotently on ``(axis, slug)``.

        Replaying an existing (axis, slug) pair is a no-op
        (``ON CONFLICT DO NOTHING``), so axis-expansion replay is safe.
        """
        missing = [name for name in _AXIS_REQUIRED if not str(value.get(name) or "").strip()]
        if missing:
            raise ValueError(f"axis value is missing required fields: {', '.join(missing)}")
        obscurity_raw = value.get("obscurity")
        obscurity = 3 if obscurity_raw is None else int(obscurity_raw)
        self.connection.execute(
            """INSERT INTO merlin_storage.campaign_axis_values
                   (axis, slug, display_name, instruction_fragment, origin, obscurity)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (axis, slug) DO NOTHING""",
            (
                str(value["axis"]),
                str(value["slug"]),
                str(value["display_name"]),
                str(value["instruction_fragment"]),
                str(value["origin"]),
                obscurity,
            ),
        )

    def list_axes(self) -> list[str]:
        """Return the distinct axes that currently have at least one value."""
        rows = self.connection.execute(
            """SELECT DISTINCT axis
               FROM merlin_storage.campaign_axis_values
               ORDER BY axis"""
        ).fetchall()
        return [str(row["axis"]) for row in rows]
