from pathlib import Path
from uuid import uuid4

from merlin_contracts import TechniqueAttempt
from merlin_storage_postgres import (
    CampaignObservabilityRepository,
    CampaignStore,
    Database,
    DatabaseConfig,
    MigrationRunner,
    SessionRepository,
    TechniqueAttemptRepository,
)
from merlin_storage_postgres.arm_store import CampaignArmStore


def _arm(**overrides: object) -> dict[str, object]:
    arm: dict[str, object] = {
        "id": "arm/one",
        "target_id": "target-1",
        "scenario_fingerprint": "sha256:scenario-one",
        "surface_id": "chat",
        "semantic_id": "semantic/one",
        "strategy_id": "direct",
        "technique_id": "technique/one",
        "controller_id": "direct",
        "mutation_id": "mutation/none",
        "actual_language": "en",
        "state_id": "fresh-session",
        "objective": "Probe the authentication boundary.",
        "source": "brainstorm",
        "novelty_digest": "sha256:novelty",
        "status": "trial",
        "rank_components": {"security_value": 0.7, "uncertainty": 1.0},
        "eligibility_reasons": ["authorized", "deliverable", "healthy"],
        "decision_reason": "Untried authorized chat arm covers a frontier gap.",
    }
    arm.update(overrides)
    return arm


def test_pg0_arm_pool_record_is_idempotent_on_natural_key_and_retires(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = CampaignArmStore(connection)
            store.record_arm(_arm(id="arm/one"))
            # Same natural key but a different id: replay must be a no-op.
            store.record_arm(_arm(id="arm/one-again"))

            arms = store.list_arms("target-1", "sha256:scenario-one")
            assert [arm["id"] for arm in arms] == ["arm/one"]
            assert arms[0]["status"] == "trial"
            assert arms[0]["surface_id"] == "chat"
            assert arms[0]["rank_components"]["uncertainty"] == 1.0

            store.mark_retired("arm/one")
            assert store.list_arms("target-1", "sha256:scenario-one")[0]["status"] == "retired"


def test_pg0_arm_pool_scopes_arms_by_target_scenario(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = CampaignArmStore(connection)
            store.record_arm(_arm(id="arm/one", scenario_fingerprint="sha256:scenario-one"))
            store.record_arm(_arm(id="arm/two", scenario_fingerprint="sha256:scenario-two"))
            store.record_arm(_arm(id="arm/three", target_id="target-2"))

            assert [arm["id"] for arm in store.list_arms("target-1", "sha256:scenario-one")] == [
                "arm/one"
            ]
            assert [arm["id"] for arm in store.list_arms("target-1", "sha256:scenario-two")] == [
                "arm/two"
            ]
            assert [arm["id"] for arm in store.list_arms("target-2", "sha256:scenario-one")] == [
                "arm/three"
            ]


def test_pg0_arm_pool_round_trips_execution_reachability(tmp_path: Path) -> None:
    receipt = {
        "schema": "merlin.parseltongue-execution.v1",
        "requested_transform_id": "encoding/base64",
        "applied_transform_id": "encoding/base64",
        "requested_language": "am",
        "actual_language": "am",
        "receipt_digest": "sha256:receipt",
    }
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = CampaignArmStore(connection)
            store.record_arm(_arm(execution_reachability=receipt))

            stored = store.list_arms("target-1", "sha256:scenario-one")[0]

            assert stored["execution_reachability"] == receipt


def test_pg0_arm_observation_round_trips_into_bandit_history(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create()
            CampaignStore(connection).prepare("campaign-1", session.id, {}, "digest")
            attempt_id = str(uuid4())
            TechniqueAttemptRepository(connection).begin(
                TechniqueAttempt(
                    id=attempt_id,
                    campaign_id="campaign-1",
                    wave=1,
                    task_id="generated-arm",
                    technique_id="technique/one",
                    strategy_id="direct",
                    language="en",
                    control_kind="attack",
                    oracle_digest="sha256:" + "b" * 64,
                )
            )
            store = CampaignArmStore(connection)
            arm = _arm(source="catalog-mutation")
            store.record_arm(arm)
            hypothesis = store.record_hypothesis(
                {
                    "id": "hypothesis:arm-one",
                    "campaign_id": "campaign-1",
                    "target_id": "target-1",
                    "scenario_fingerprint": "sha256:scenario-one",
                    "claim": "The generated arm crosses the boundary.",
                    "parent_hypothesis_id": None,
                    "evidence_refs": ("receipt:recon",),
                }
            )
            store.record_arm_observation(
                {
                    "campaign_id": "campaign-1",
                    "arm_id": arm["id"],
                    "technique_attempt_id": attempt_id,
                    "hypothesis_id": hypothesis.id,
                    "identity": {
                        key: arm[key]
                        for key in (
                            "target_id",
                            "scenario_fingerprint",
                            "surface_id",
                            "technique_id",
                            "controller_id",
                            "mutation_id",
                            "actual_language",
                            "state_id",
                        )
                    },
                    "outcome": "partial_signal",
                    "signal": 0.6,
                    "target_calls": 1,
                    "model_steps": 3,
                    "latency_ms": 217,
                    "model_cost_usd": 0.0042,
                    "usage": {"input_tokens": 120, "output_tokens": 35},
                    "evaluation_uncertainties": (
                        {
                            "source": "adapter_expected_status",
                            "reason": "received 201 while metadata expected 200",
                            "confidence": 0.8,
                        },
                    ),
                    "evidence_refs": ("receipt:one",),
                }
            )

            history = store.list_arm_observations("target-1", "sha256:scenario-one")
            assert history[0]["outcome"] == "near_miss"
            assert history[0]["partial_signal"] == 0.6
            assert history[0]["identity"]["state_id"] == "fresh-session"
            assert history[0]["evaluation_uncertainties"] == (
                {
                    "source": "adapter_expected_status",
                    "reason": "received 201 while metadata expected 200",
                    "confidence": 0.8,
                },
            )
            outcome = connection.execute(
                """SELECT hypothesis_id, latency_ms, model_cost_usd, usage
                   FROM merlin_storage.campaign_arm_outcomes"""
            ).fetchone()
            assert outcome["hypothesis_id"] == hypothesis.id
            assert outcome["latency_ms"] == 217
            assert float(outcome["model_cost_usd"]) == 0.0042
            assert {
                key: value
                for key, value in outcome["usage"].items()
                if key != "candidate_outcome_projection"
            } == {
                "input_tokens": 120,
                "output_tokens": 35,
                "evaluation_uncertainties": [
                    {
                        "source": "adapter_expected_status",
                        "reason": "received 201 while metadata expected 200",
                        "confidence": 0.8,
                    }
                ],
            }
            assert outcome["usage"]["candidate_outcome_projection"]["candidate_id"] == arm["id"]
            store.record_arm_observation(
                {
                    "campaign_id": "campaign-1",
                    "arm_id": arm["id"],
                    "technique_attempt_id": attempt_id,
                    "hypothesis_id": hypothesis.id,
                    "identity": {
                        key: arm[key]
                        for key in (
                            "target_id",
                            "scenario_fingerprint",
                            "surface_id",
                            "technique_id",
                            "controller_id",
                            "mutation_id",
                            "actual_language",
                            "state_id",
                        )
                    },
                    "outcome": "clean_negative",
                    "target_calls": 2,
                    "model_steps": 4,
                    "latency_ms": 250,
                    "model_cost_usd": 0.005,
                    "usage": {"input_tokens": 140, "output_tokens": 40},
                    "evidence_refs": ("receipt:two",),
                }
            )
            revised = store.list_arm_observations("target-1", "sha256:scenario-one")
            assert len(revised) == 1
            assert revised[0]["outcome"] == "clean_negative"
            assert (
                connection.execute(
                    "SELECT count(*) AS value FROM merlin_storage.campaign_arm_outcomes"
                ).fetchone()["value"]
                == 1
            )
            counters = CampaignObservabilityRepository(connection).counters("campaign-1")
            assert counters.target_calls == 2
            assert counters.model_steps == 4
            transitioned = store.transition_hypothesis(
                {
                    "hypothesis_id": hypothesis.id,
                    "technique_attempt_id": attempt_id,
                    "outcome": "clean_negative",
                    "evidence_refs": ("receipt:one",),
                    "reason": "the sealed oracle refuted the claim",
                }
            )
            assert transitioned.state == "falsified"
            child = store.record_hypothesis(
                {
                    "id": "hypothesis:arm-child",
                    "campaign_id": "campaign-1",
                    "target_id": "target-1",
                    "scenario_fingerprint": "sha256:scenario-one",
                    "claim": "A refined boundary condition remains.",
                    "parent_hypothesis_id": hypothesis.id,
                    "evidence_refs": ("receipt:one",),
                }
            )
            assert child.parent_hypothesis_id == hypothesis.id
            assert child.lineage == (hypothesis.id, child.id)
            blocked = store.transition_hypothesis(
                {
                    "hypothesis_id": child.id,
                    "technique_attempt_id": attempt_id,
                    "outcome": "infrastructure_failure",
                    "infrastructure_failure_category": "missing_response_pointer",
                    "evidence_refs": ("receipt:pointer-missing",),
                    "reason": "reviewed response pointer was unavailable",
                }
            )
            assert blocked.state == "blocked_by_infrastructure"
            assert blocked.infrastructure_failure_category == "missing_response_pointer"
            unchanged = store.record_hypothesis(
                {
                    "id": child.id,
                    "campaign_id": "campaign-1",
                    "target_id": "target-1",
                    "scenario_fingerprint": "sha256:scenario-one",
                    "claim": "A refined boundary condition remains.",
                    "parent_hypothesis_id": hypothesis.id,
                    "evidence_refs": ("receipt:one",),
                }
            )
            assert unchanged.state == "blocked_by_infrastructure"
            retried = store.record_hypothesis(
                {
                    "id": child.id,
                    "campaign_id": "campaign-1",
                    "target_id": "target-1",
                    "scenario_fingerprint": "sha256:scenario-one",
                    "claim": "A refined boundary condition remains.",
                    "parent_hypothesis_id": hypothesis.id,
                    "evidence_refs": ("receipt:one",),
                    "adapter_health_recovered": True,
                    "adapter_health_recovery_evidence_refs": ("receipt:adapter-healthy",),
                }
            )
            assert retried.state == "in_progress"
            superseded_parent = store.record_hypothesis(
                {
                    "id": "hypothesis:superseded-parent",
                    "campaign_id": "campaign-1",
                    "target_id": "target-1",
                    "scenario_fingerprint": "sha256:scenario-one",
                    "claim": "The original boundary is broad.",
                    "parent_hypothesis_id": None,
                    "evidence_refs": ("receipt:parent",),
                }
            )
            superseding_child = store.record_hypothesis(
                {
                    "id": "hypothesis:superseding-child",
                    "campaign_id": "campaign-1",
                    "target_id": "target-1",
                    "scenario_fingerprint": "sha256:scenario-one",
                    "claim": "The refined boundary is narrower.",
                    "parent_hypothesis_id": superseded_parent.id,
                    "evidence_refs": ("receipt:child",),
                }
            )
            parent_state = connection.execute(
                """SELECT state FROM merlin_storage.campaign_hypothesis_events
                   WHERE hypothesis_id = %s ORDER BY sequence DESC LIMIT 1""",
                (superseded_parent.id,),
            ).fetchone()
            assert parent_state["state"] == "superseded"
            assert superseding_child.lineage == (superseded_parent.id, superseding_child.id)
            store.record_arm_ranking(
                {
                    **arm,
                    "rank_components": {
                        "generalization": {"measured_value": 0.8, "contribution": 0.2},
                        "boundary": {"measured_value": 0.6, "contribution": 0.18},
                    },
                    "eligibility_reasons": ("durable history available",),
                    "decision_reason": "restart-safe rank",
                }
            )
            ranked_arm = store.list_arms("target-1", "sha256:scenario-one")[0]
            assert ranked_arm["rank_components"]["generalization"]["measured_value"] == 0.8
            assert ranked_arm["rank_components"]["boundary"]["measured_value"] == 0.6
            store.set_lifecycle("arm/one", "active")
            assert store.list_arms("target-1", "sha256:scenario-one")[0]["status"] == "active"


def test_pg0_storage_exhaustion_is_infrastructure_without_quality(tmp_path: Path) -> None:
    from merlin_contracts.storage_failures import storage_failure_category
    from psycopg.errors import DiskFull

    category = storage_failure_category(DiskFull("injected"))
    assert category is not None
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create()
            CampaignStore(connection).prepare("campaign-1", session.id, {}, "digest")
            attempt_id = str(uuid4())
            TechniqueAttemptRepository(connection).begin(
                TechniqueAttempt(
                    id=attempt_id,
                    campaign_id="campaign-1",
                    wave=0,
                    task_id="worker-1",
                    technique_id="technique/one",
                    strategy_id="direct",
                    language="en",
                    control_kind="attack",
                    oracle_digest="sha256:" + "c" * 64,
                )
            )
            store = CampaignArmStore(connection)
            arm = _arm()
            store.record_arm(arm)
            observation = {
                **_observation(arm, attempt_id),
                "outcome": "infrastructure_failure",
                "target_calls": 0,
                "infrastructure_failure_category": category.value,
            }
            store.record_arm_observation(observation)
            history = store.list_arm_observations("target-1", "sha256:scenario-one")
            assert len(history) == 1
            assert history[0]["infrastructure_failure"] is True
            assert history[0]["outcome"] == "infrastructure_failure"
            assert history[0]["attempt_disposition"] == "uncertain"
            assert history[0]["outcome_authority"] == "none"
            assert store.list_arms("target-1", "sha256:scenario-one")[0]["status"] == "trial"
            counters = CampaignObservabilityRepository(connection).counters("campaign-1")
            assert counters.infrastructure_failures == 1
            assert counters.target_calls == 0
            hypothesis = store.record_hypothesis(
                {
                    "id": "hypothesis:storage",
                    "campaign_id": "campaign-1",
                    "target_id": "target-1",
                    "scenario_fingerprint": "sha256:scenario-one",
                    "claim": "Unverified claim",
                    "evidence_refs": ("receipt:one",),
                }
            )
            blocked = store.transition_hypothesis(
                {
                    **observation,
                    "hypothesis_id": hypothesis.id,
                    "reason": "storage unavailable",
                }
            )
            assert blocked.state == "blocked_by_infrastructure"
            assert blocked.infrastructure_failure_category == category


def _observation(arm: dict[str, object], attempt_id: str) -> dict[str, object]:
    return {
        "campaign_id": "campaign-1",
        "arm_id": arm["id"],
        "technique_attempt_id": attempt_id,
        "identity": {
            key: arm[key]
            for key in (
                "target_id",
                "scenario_fingerprint",
                "surface_id",
                "technique_id",
                "controller_id",
                "mutation_id",
                "actual_language",
                "state_id",
            )
        },
        "outcome": "clean_negative",
        "target_calls": 1,
        "model_steps": 2,
        "latency_ms": 100,
        "model_cost_usd": 0.001,
        "usage": {"input_tokens": 100, "output_tokens": 25},
        "evidence_refs": ("receipt:one",),
    }


def test_pg0_arm_observation_materializes_missing_arm_from_sealed_contract(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create()
            CampaignStore(connection).prepare("campaign-1", session.id, {}, "digest")
            attempt_id = str(uuid4())
            TechniqueAttemptRepository(connection).begin(
                TechniqueAttempt(
                    id=attempt_id,
                    campaign_id="campaign-1",
                    wave=0,
                    task_id="worker-1",
                    technique_id="technique/one",
                    strategy_id="direct",
                    language="en",
                    control_kind="attack",
                    oracle_digest="sha256:" + "c" * 64,
                )
            )
            store = CampaignArmStore(connection)
            arm = _arm()
            # No record_arm call: admission-time persistence was never wired.
            store.record_arm_observation({**_observation(arm, attempt_id), "arm": dict(arm)})

            materialized = store.list_arms("target-1", "sha256:scenario-one")
            assert [row["id"] for row in materialized] == ["arm/one"]
            outcome = connection.execute(
                """SELECT model_cost_usd, usage
                   FROM merlin_storage.campaign_arm_outcomes"""
            ).fetchone()
            assert float(outcome["model_cost_usd"]) == 0.001
            assert {
                key: value
                for key, value in outcome["usage"].items()
                if key != "candidate_outcome_projection"
            } == {"input_tokens": 100, "output_tokens": 25}
            assert outcome["usage"]["candidate_outcome_projection"]["candidate_id"] == arm["id"]


def test_pg0_arm_observation_without_sealed_contract_keeps_key_error(
    tmp_path: Path,
) -> None:
    import pytest

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create()
            CampaignStore(connection).prepare("campaign-1", session.id, {}, "digest")
            attempt_id = str(uuid4())
            TechniqueAttemptRepository(connection).begin(
                TechniqueAttempt(
                    id=attempt_id,
                    campaign_id="campaign-1",
                    wave=0,
                    task_id="worker-1",
                    technique_id="technique/one",
                    strategy_id="direct",
                    language="en",
                    control_kind="attack",
                    oracle_digest="sha256:" + "d" * 64,
                )
            )
            store = CampaignArmStore(connection)
            with pytest.raises(KeyError):
                store.record_arm_observation(_observation(_arm(), attempt_id))


def test_pg0_arm_observation_preserves_and_guards_canonical_settlement(
    tmp_path: Path,
) -> None:
    import pytest
    from merlin_contracts import candidate_id_for_identity, project_candidate_outcome

    canonical_identity = {
        "campaign_id": "campaign-1",
        "target_id": "target-1",
        "target_generation": "generation-1",
        "scenario_fingerprint": "sha256:scenario-one",
        "technique_id": "technique/one",
        "controller_id": "direct",
        "payload_family": "baseline",
        "requested_language": "en",
        "mutation_id": "mutation/none",
    }
    projection = project_candidate_outcome(
        candidate_id=candidate_id_for_identity(canonical_identity),
        attempt_disposition="valid",
        outcome_authority="target",
        outcome="refuted",
        evidence_digest="a" * 64,
    )
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create()
            CampaignStore(connection).prepare("campaign-1", session.id, {}, "digest")
            attempt_id = str(uuid4())
            TechniqueAttemptRepository(connection).begin(
                TechniqueAttempt(
                    id=attempt_id,
                    campaign_id="campaign-1",
                    wave=1,
                    task_id="generated-arm",
                    technique_id="technique/one",
                    strategy_id="direct",
                    language="en",
                    control_kind="attack",
                    oracle_digest="sha256:" + "b" * 64,
                )
            )
            store = CampaignArmStore(connection)
            arm = _arm()
            store.record_arm(arm)
            observation = {
                **_observation(arm, attempt_id),
                "attempt_disposition": "valid",
                "outcome_authority": "target",
                "canonical_candidate_identity": canonical_identity,
                "candidate_outcome_projection": projection,
            }
            store.record_arm_observation(observation)
            persisted = connection.execute(
                "SELECT usage FROM merlin_storage.campaign_arm_outcomes"
            ).fetchone()
            assert persisted["usage"]["candidate_outcome_projection"] == projection

            with pytest.raises(ValueError, match="candidate id conflicts"):
                store.record_arm_observation(
                    {
                        **observation,
                        "candidate_outcome_projection": {
                            **projection,
                            "candidate_id": "candidate/drifted",
                        },
                    }
                )
