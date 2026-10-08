from pathlib import Path
from uuid import uuid4

import pytest
from merlin_agents.campaign_service import FrontierHypothesis
from merlin_agents.language_execution import record_language_execution
from merlin_agents.parseltongue import execute_parseltongue
from merlin_contracts import (
    ArmIdentity,
    ArmOutcome,
    CampaignCounterEvent,
    CampaignCounters,
    HypothesisRecord,
    HypothesisState,
    InfrastructureFailureCategory,
    LanguageEvidenceStatus,
    TechniqueAttempt,
    TechniqueLanguageEvidence,
)
from merlin_storage_postgres import (
    CampaignObservabilityRepository,
    CampaignStore,
    Database,
    DatabaseConfig,
    HypothesisRepository,
    MigrationRunner,
    SessionRepository,
    TechniqueAttemptRepository,
    TechniqueLanguageEvidenceRepository,
    hypothesis_record_from_frontier,
    technique_language_evidence_from_execution,
)
from merlin_storage_postgres.arm_store import CampaignArmStore
from psycopg.errors import ForeignKeyViolation, RaiseException


def _arm() -> dict[str, object]:
    return {
        "id": "arm/observability",
        "target_id": "target-1",
        "scenario_fingerprint": "scenario-1",
        "surface_id": "chat",
        "semantic_id": "semantic-1",
        "technique_id": "prompt-override",
        "strategy_id": "crescendo",
        "controller_id": "crescendo",
        "mutation_id": "authority-framing",
        "actual_language": "ms",
        "state_id": "fresh-session",
        "objective": "Probe a gradual supervisor override.",
        "source": "brainstorm",
        "rank_components": {"security_value": 0.8, "uncertainty": 1.0},
        "eligibility_reasons": ["authorized", "deliverable", "healthy"],
        "decision_reason": "Untried high-value surface/language combination.",
    }


def _outcome_with_usage(usage: dict[str, object]) -> ArmOutcome:
    return ArmOutcome(
        id="outcome/identity",
        campaign_id="campaign-1",
        arm_id="arm/observability",
        technique_attempt_id="00000000-0000-0000-0000-000000000001",
        identity=ArmIdentity.model_validate(
            {key: value for key, value in _arm().items() if key in ArmIdentity.model_fields}
        ),
        requested_language="ms",
        actual_language="ms",
        language_evidence_status="observed",
        language_evidence_source="target response detector",
        outcome="clean_negative",
        counters=CampaignCounters(target_calls=1),
        evidence_refs=("exchange:1",),
        usage=usage,
        reason="Target retained its refusal boundary.",
    )


def test_arm_outcome_identity_uses_canonical_nested_json_values() -> None:
    original = _outcome_with_usage(
        {
            "evaluation_uncertainties": [
                {
                    "source": "expected_status",
                    "reason": "status mismatch",
                    "confidence": 0.4,
                    "details": {"expected": 200, "actual": 201},
                }
            ],
            "input_tokens": 10,
        }
    )
    jsonb_reordered = _outcome_with_usage(
        {
            "input_tokens": 10,
            "evaluation_uncertainties": [
                {
                    "details": {"actual": 201, "expected": 200},
                    "confidence": 0.4,
                    "reason": "status mismatch",
                    "source": "expected_status",
                }
            ],
        }
    )
    changed_value = _outcome_with_usage(
        {
            "input_tokens": 10,
            "evaluation_uncertainties": [
                {
                    "details": {"actual": 202, "expected": 200},
                    "confidence": 0.4,
                    "reason": "status mismatch",
                    "source": "expected_status",
                }
            ],
        }
    )

    identity = CampaignObservabilityRepository._outcome_identity
    assert identity(original) == identity(jsonb_reordered)
    assert identity(original) != identity(changed_value)


def test_arm_outcome_jsonb_reordering_is_idempotent(
    tmp_path: Path,
) -> None:
    original = _outcome_with_usage(
        {
            "evaluation_uncertainties": [
                {
                    "source": "expected_status",
                    "reason": "status mismatch",
                    "confidence": 0.4,
                    "details": {"expected": 200, "actual": 201},
                }
            ],
            "input_tokens": 10,
        }
    )
    reordered = _outcome_with_usage(
        {
            "input_tokens": 10,
            "evaluation_uncertainties": [
                {
                    "details": {"actual": 201, "expected": 200},
                    "confidence": 0.4,
                    "reason": "status mismatch",
                    "source": "expected_status",
                }
            ],
        }
    )

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create()
            CampaignStore(connection).prepare("campaign-1", session.id, {}, "digest-1")
            CampaignArmStore(connection).record_arm(_arm())
            TechniqueAttemptRepository(connection).begin(
                TechniqueAttempt(
                    id="00000000-0000-0000-0000-000000000001",
                    campaign_id="campaign-1",
                    wave=0,
                    task_id="worker-1",
                    technique_id="prompt-override",
                    strategy_id="crescendo",
                    language="ms",
                    control_kind="attack",
                    oracle_digest="sha256:" + "a" * 64,
                )
            )
            repository = CampaignObservabilityRepository(connection)

            repository.record_arm_outcome(original)
            stored = repository.record_arm_outcome(reordered)

            assert stored.usage == reordered.usage


def test_runtime_evidence_adapters_bind_durable_context() -> None:
    root_runtime = FrontierHypothesis(
        surface="chat",
        assertion="A gradual supervisor claim may cross the refusal boundary.",
        oracle="The target emits the restricted completion marker.",
        novelty_basis="Recon observed a near miss.",
    )
    root = hypothesis_record_from_frontier(
        root_runtime,
        campaign_id="campaign-1",
        target_id="target-1",
        scenario_fingerprint="scenario-1",
        creating_evidence_refs=("recon:1",),
    )
    child_runtime = FrontierHypothesis(
        surface="chat",
        assertion="Authority framing may strengthen the same near miss.",
        oracle="The target emits the restricted completion marker.",
        novelty_basis="The baseline produced a partial signal.",
    )
    child = hypothesis_record_from_frontier(
        child_runtime,
        campaign_id="campaign-1",
        target_id="target-1",
        scenario_fingerprint="scenario-1",
        creating_evidence_refs=("exchange:2",),
        parent=root,
    )
    provider_receipt = record_language_execution(
        "source",
        "output",
        requested_transform_id="translated",
        applied_transform_id="translated",
        requested_language="ms",
        actual_language="ms",
        executor_id="provider-language-detector",
    )
    provider_evidence = technique_language_evidence_from_execution(
        provider_receipt,
        campaign_id="campaign-1",
        technique_attempt_id="attempt-1",
        status=LanguageEvidenceStatus.OBSERVED,
    )
    parseltongue_evidence = technique_language_evidence_from_execution(
        execute_parseltongue("hello", "visual/leetspeak"),
        campaign_id="campaign-1",
        technique_attempt_id="attempt-2",
        status=LanguageEvidenceStatus.INFERRED,
    )

    assert child.parent_hypothesis_id == root.id
    assert child.lineage == (root.id, child.id)
    assert provider_evidence.language_evidence_source == "provider-language-detector"
    assert provider_evidence.evidence_refs == (provider_receipt.receipt_digest,)
    assert parseltongue_evidence.actual_language == "und"


def test_observability_references_must_belong_to_event_campaign(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create()
            campaigns = CampaignStore(connection)
            campaigns.prepare("campaign-1", session.id, {}, "digest-1")
            campaigns.prepare("campaign-2", session.id, {}, "digest-2")
            attempt_id = str(uuid4())
            TechniqueAttemptRepository(connection).begin(
                TechniqueAttempt(
                    id=attempt_id,
                    campaign_id="campaign-2",
                    wave=0,
                    task_id="worker-2",
                    technique_id="prompt-override",
                    strategy_id="crescendo",
                    language="ms",
                    control_kind="attack",
                    oracle_digest="sha256:" + "b" * 64,
                )
            )
            hypothesis = HypothesisRepository(connection).create(
                HypothesisRecord(
                    id="hypothesis/campaign-2",
                    campaign_id="campaign-2",
                    target_id="target-1",
                    scenario_fingerprint="scenario-1",
                    claim="Campaign two hypothesis.",
                    creating_evidence_refs=("recon:2",),
                    lineage=("hypothesis/campaign-2",),
                )
            )
            CampaignArmStore(connection).record_arm(_arm())
            repository = CampaignObservabilityRepository(connection)

            with pytest.raises(ValueError, match="technique attempt does not belong"):
                repository.record_counter_event(
                    CampaignCounterEvent(
                        id="counter/wrong-attempt",
                        campaign_id="campaign-1",
                        technique_attempt_id=attempt_id,
                        counters=CampaignCounters(target_calls=1),
                    )
                )
            with pytest.raises(ValueError, match="hypothesis does not belong"):
                repository.record_counter_event(
                    CampaignCounterEvent(
                        id="counter/wrong-hypothesis",
                        campaign_id="campaign-1",
                        hypothesis_id=hypothesis.id,
                        counters=CampaignCounters(model_steps=1),
                    )
                )
            with pytest.raises(ValueError, match="technique attempt does not belong"):
                repository.record_arm_outcome(
                    ArmOutcome(
                        id="outcome/wrong-attempt",
                        campaign_id="campaign-1",
                        arm_id="arm/observability",
                        technique_attempt_id=attempt_id,
                        identity=ArmIdentity.model_validate(
                            {
                                key: value
                                for key, value in _arm().items()
                                if key in ArmIdentity.model_fields
                            }
                        ),
                        requested_language="ms",
                        actual_language="ms",
                        language_evidence_status="observed",
                        language_evidence_source="target response detector",
                        outcome="clean_negative",
                        counters=CampaignCounters(target_calls=1),
                        evidence_refs=("exchange:wrong-campaign",),
                        reason="This outcome must not cross campaign ownership.",
                    )
                )
            repository.record_counter_event(
                CampaignCounterEvent(
                    id="counter/raw-outcome",
                    campaign_id="campaign-1",
                    counters=CampaignCounters(target_calls=1),
                )
            )

            connection.commit()
            with pytest.raises(ForeignKeyViolation):
                connection.execute(
                    """INSERT INTO merlin_storage.campaign_counter_events
                           (id, campaign_id, technique_attempt_id, target_calls, model_steps,
                            validation_calls, reproduction_calls, infrastructure_failures)
                       VALUES (%s, %s, %s, 1, 0, 0, 0, 0)""",
                    ("counter/raw-wrong-attempt", "campaign-1", attempt_id),
                )
            connection.rollback()
            constraints: dict[str, list[str]] = {}
            for row in connection.execute(
                """SELECT conrelid::regclass::text AS table_name,
                          pg_get_constraintdef(oid) AS definition
                   FROM pg_constraint
                   WHERE conrelid IN (
                       'merlin_storage.campaign_counter_events'::regclass,
                       'merlin_storage.campaign_arm_outcomes'::regclass
                   )"""
            ).fetchall():
                constraints.setdefault(str(row["table_name"]), []).append(str(row["definition"]))
            counter_constraints = "\n".join(constraints["merlin_storage.campaign_counter_events"])
            outcome_constraints = "\n".join(constraints["merlin_storage.campaign_arm_outcomes"])
            assert "FOREIGN KEY (campaign_id, hypothesis_id)" in counter_constraints
            assert "FOREIGN KEY (technique_attempt_id, campaign_id)" in counter_constraints
            assert "FOREIGN KEY (campaign_id, hypothesis_id)" in outcome_constraints
            assert "FOREIGN KEY (technique_attempt_id, campaign_id)" in outcome_constraints
            assert "FOREIGN KEY (counter_event_id, campaign_id)" in outcome_constraints
            with pytest.raises(ForeignKeyViolation):
                connection.execute(
                    """INSERT INTO merlin_storage.campaign_arm_outcomes
                           (id, campaign_id, arm_id, technique_attempt_id,
                            target_id, scenario_fingerprint, surface_id, technique_id,
                            controller_id, mutation_id, actual_language, state_id,
                            requested_language, language_evidence_status,
                            language_evidence_source, outcome, counter_event_id,
                            evidence_refs, latency_ms, model_cost_usd, reason)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s)""",
                    (
                        "outcome/raw-wrong-attempt",
                        "campaign-1",
                        "arm/observability",
                        attempt_id,
                        "target-1",
                        "scenario-1",
                        "chat",
                        "prompt-override",
                        "crescendo",
                        "authority-framing",
                        "ms",
                        "fresh-session",
                        "ms",
                        "observed",
                        "target response detector",
                        "clean_negative",
                        "counter/raw-outcome",
                        ["exchange:wrong-campaign"],
                        0,
                        0.0,
                        "This raw outcome must fail its campaign-attempt join.",
                    ),
                )
            connection.rollback()


def test_hypothesis_lifecycle_is_append_only_and_preserves_lineage(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create()
            CampaignStore(connection).prepare("campaign-1", session.id, {}, "digest-1")
            repository = HypothesisRepository(connection)
            root = repository.create(
                HypothesisRecord(
                    id="hypothesis/root",
                    campaign_id="campaign-1",
                    target_id="target-1",
                    scenario_fingerprint="scenario-1",
                    claim="The target may trust a gradual supervisor instruction.",
                    creating_evidence_refs=("recon:1",),
                    lineage=("hypothesis/root",),
                )
            )
            active = repository.transition(
                root.id,
                "hypothesis-event/active",
                HypothesisState.UNTESTED,
                HypothesisState.IN_PROGRESS,
            )
            blocked = repository.transition(
                root.id,
                "hypothesis-event/blocked",
                HypothesisState.IN_PROGRESS,
                HypothesisState.BLOCKED_BY_INFRASTRUCTURE,
                reason="Adapter timed out before a response existed.",
                infrastructure_failure_category=InfrastructureFailureCategory.ADAPTER_TIMEOUT,
            )
            child = repository.create(
                HypothesisRecord(
                    id="hypothesis/child",
                    campaign_id="campaign-1",
                    target_id="target-1",
                    scenario_fingerprint="scenario-1",
                    claim="The same boundary may move under authority framing.",
                    parent_hypothesis_id=root.id,
                    creating_evidence_refs=("exchange:near-miss",),
                    lineage=(root.id, "hypothesis/child"),
                )
            )

            assert active.state == HypothesisState.IN_PROGRESS
            assert blocked.state == HypothesisState.BLOCKED_BY_INFRASTRUCTURE
            assert blocked.lineage == (root.id,)
            assert child.parent_hypothesis_id == root.id
            assert (
                len(
                    connection.execute(
                        "SELECT id FROM merlin_storage.campaign_hypothesis_events"
                    ).fetchall()
                )
                == 4
            )
            with pytest.raises(RaiseException, match="immutable"):
                connection.execute(
                    "UPDATE merlin_storage.campaign_hypotheses SET claim = 'rewritten' "
                    "WHERE id = %s",
                    (root.id,),
                )
            connection.rollback()


def test_arm_outcome_counts_and_language_evidence_survive_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres"
    attempt_id = str(uuid4())
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create()
            CampaignStore(connection).prepare("campaign-1", session.id, {}, "digest-1")
            CampaignArmStore(connection).record_arm(_arm())
            TechniqueAttemptRepository(connection).begin(
                TechniqueAttempt(
                    id=attempt_id,
                    campaign_id="campaign-1",
                    wave=0,
                    task_id="worker-1",
                    technique_id="prompt-override",
                    strategy_id="crescendo",
                    language="ms",
                    control_kind="attack",
                    oracle_digest="sha256:" + "a" * 64,
                )
            )
            language_repository = TechniqueLanguageEvidenceRepository(connection)
            language_repository.record(
                TechniqueLanguageEvidence(
                    campaign_id="campaign-1",
                    technique_attempt_id=attempt_id,
                    requested_language="ms",
                    actual_language="ms",
                    language_evidence_status="observed",
                    language_evidence_source="target response detector",
                    evidence_refs=("exchange:1",),
                )
            )
            merged_language = language_repository.record(
                TechniqueLanguageEvidence(
                    campaign_id="campaign-1",
                    technique_attempt_id=attempt_id,
                    requested_language="ms",
                    actual_language="ms",
                    language_evidence_status="observed",
                    language_evidence_source="target response detector",
                    evidence_refs=("exchange:2",),
                )
            )
            assert merged_language.evidence_refs == ("exchange:1",)
            repository = CampaignObservabilityRepository(connection)
            repository.record_counter_event(
                CampaignCounterEvent(
                    id="counter/model-plan",
                    campaign_id="campaign-1",
                    counters=CampaignCounters(model_steps=2),
                )
            )
            repository.record_arm_outcome(
                ArmOutcome(
                    id="outcome/1",
                    campaign_id="campaign-1",
                    arm_id="arm/observability",
                    technique_attempt_id=attempt_id,
                    identity=ArmIdentity.model_validate(
                        {
                            key: value
                            for key, value in _arm().items()
                            if key in ArmIdentity.model_fields
                        }
                    ),
                    requested_language="ms",
                    actual_language="ms",
                    language_evidence_status="observed",
                    language_evidence_source="target response detector",
                    outcome="clean_negative",
                    counters=CampaignCounters(target_calls=1, model_steps=1),
                    evidence_refs=("exchange:1",),
                    latency_ms=42,
                    model_cost_usd=0.01,
                    reason="Target retained its refusal boundary.",
                )
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            MigrationRunner(connection).apply()
            repository = CampaignObservabilityRepository(connection)
            counters = repository.counters("campaign-1")
            outcome = repository.get_arm_outcome("outcome/1")
            language = TechniqueLanguageEvidenceRepository(connection).get(attempt_id)

            assert counters == CampaignCounters(target_calls=1, model_steps=3)
            assert outcome is not None and outcome.identity.state_id == "fresh-session"
            assert language is not None and language.actual_language == "ms"
