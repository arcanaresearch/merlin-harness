import json
from pathlib import Path
from uuid import uuid4

import pytest
from merlin_agents import AssignmentBudget, DurableAgentService, WorkerAssignment
from merlin_contracts import ModelError, TechniqueAttempt, TechniqueAttemptState
from merlin_storage_postgres import (
    CampaignStore,
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    SessionRepository,
    TechniqueAttemptRepository,
)


def _attempt(identifier: str, *, campaign_id: str = "campaign-1") -> TechniqueAttempt:
    return TechniqueAttempt(
        id=identifier,
        campaign_id=campaign_id,
        wave=1,
        task_id="worker/injection",
        technique_id="prompt-injection/direct",
        strategy_id="direct",
        control_kind="attack",
        oracle_digest="sha256:" + "a" * 64,
    )


def test_technique_attempt_lifecycle_is_cas_and_terminally_immutable(tmp_path: Path) -> None:
    identifier = str(uuid4())
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            repository = TechniqueAttemptRepository(connection)
            planned = repository.begin(_attempt(identifier))
            active = repository.transition(
                identifier,
                TechniqueAttemptState.PLANNED,
                TechniqueAttemptState.ACTIVE,
                prompt_digest="sha256:" + "b" * 64,
                provider="provider-1",
                model="model-1",
            )
            observed = repository.transition(
                identifier,
                TechniqueAttemptState.ACTIVE,
                TechniqueAttemptState.OBSERVED,
                evidence_refs=("evidence:one",),
            )
            terminal = repository.transition(
                identifier,
                TechniqueAttemptState.OBSERVED,
                TechniqueAttemptState.REFUTED,
                terminal_reason="sealed oracle did not match",
                usage={"input_tokens": 100, "output_tokens": 10},
            )

            assert planned.activated_at is None
            assert active.activated_at is not None
            assert observed.evidence_refs == ("evidence:one",)
            assert terminal.state == TechniqueAttemptState.REFUTED
            assert terminal.terminal_at is not None
            assert (
                repository.transition(
                    identifier,
                    TechniqueAttemptState.OBSERVED,
                    TechniqueAttemptState.REFUTED,
                    terminal_reason="sealed oracle did not match",
                )
                == terminal
            )
            with pytest.raises(ValueError, match="invalid technique-attempt transition"):
                repository.transition(
                    identifier,
                    TechniqueAttemptState.REFUTED,
                    TechniqueAttemptState.ACTIVE,
                )


def test_technique_attempt_begin_is_idempotent_but_rejects_identity_conflict(
    tmp_path: Path,
) -> None:
    identifier = str(uuid4())
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            repository = TechniqueAttemptRepository(connection)
            original = repository.begin(_attempt(identifier))

            assert repository.begin(_attempt(identifier)) == original
            with pytest.raises(ValueError, match="identity conflicts"):
                repository.begin(_attempt(identifier, campaign_id="campaign-2"))


def test_technique_attempt_active_restore_and_reduction(tmp_path: Path) -> None:
    first_id, second_id = str(uuid4()), str(uuid4())
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            repository = TechniqueAttemptRepository(connection)
            repository.begin(_attempt(first_id))
            repository.begin(
                _attempt(second_id).model_copy(update={"technique_id": "prompt-injection/roleplay"})
            )
            repository.transition(
                first_id,
                TechniqueAttemptState.PLANNED,
                TechniqueAttemptState.ACTIVE,
            )
            repository.transition(
                second_id,
                TechniqueAttemptState.PLANNED,
                TechniqueAttemptState.BLOCKED,
                terminal_reason="target prerequisite absent",
            )

            active = repository.load_active("campaign-1", "worker/injection")
            counts = repository.reduce(repository.list_for_campaign("campaign-1"))

            assert [item.id for item in active] == [first_id]
            assert counts["active"] == 1
            assert counts["blocked"] == 1


@pytest.mark.asyncio
async def test_retryable_infrastructure_keeps_real_attempt_active(tmp_path: Path) -> None:
    class TransientProvider:
        async def stream(self, request):
            yield ModelError(
                message="provider timed out before target execution",
                code="read_timeout",
                retryable=True,
            )

    class NoTools:
        def describe(self):
            return ()

        def effect(self, name):
            return None

        async def invoke(self, name, context, arguments):
            raise AssertionError("the infrastructure-failure fixture has no tools")

    campaign_id = "campaign/retryable-infrastructure"
    identity = {
        "campaign_id": campaign_id,
        "target_id": "target",
        "target_generation": "generation-1",
        "scenario_fingerprint": "scenario-1",
        "technique_id": "technique/one",
        "controller_id": "direct",
        "payload_family": "baseline",
        "requested_language": "und",
        "mutation_id": "baseline",
    }
    assignment = WorkerAssignment(
        id=f"{campaign_id}/worker",
        objective=(
            "execute\n\nSealed candidate identities: "
            + json.dumps([identity], sort_keys=True, separators=(",", ":"))
        ),
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("technique/one",),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_seconds=60),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    schedule_id = str(uuid4())

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-retryable")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("retryable infrastructure")
            store = DurableSchedulerStore(connection)
            service = DurableAgentService(
                TransientProvider(),
                NoTools(),
                store,
                model="test",
                max_provider_retries=0,
            )

            live = await service.submit(
                str(session.id),
                (assignment,),
                max_workers=1,
                minimum_workers=1,
                start_immediately=False,
                schedule_id=schedule_id,
            )
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                campaign_id,
                str(session.id),
                {"objective": "retryable infrastructure lifecycle"},
                "sha256:retryable-infrastructure",
            )
            campaigns.add_wave(campaign_id, 0, "initial", live.id)
            service.start_execution(live, max_workers=1)
            assert live.execution is not None
            await live.execution

            (attempt,) = store.list_technique_attempts(live.id)
            assert attempt.state is TechniqueAttemptState.ACTIVE
            assert attempt.terminal_at is None
            assert attempt.terminal_reason is None
            assert TechniqueAttemptRepository(connection).load_active(
                campaign_id, assignment.id
            ) == (attempt,)
            scheduler_attempts = store.list_attempts(live.id)
            assert any(
                row["state"] == "failed" and row["failure_kind"] == "provider_transient"
                for row in scheduler_attempts
            )
            (task,) = await service.list_schedule_tasks(live.id)
            assert task.status == "failed"


def test_technique_attempt_observes_without_claiming_verdict(
    tmp_path: Path,
) -> None:
    identifier = str(uuid4())
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            repository = TechniqueAttemptRepository(connection)
            repository.begin(_attempt(identifier))
            repository.transition(
                identifier,
                TechniqueAttemptState.PLANNED,
                TechniqueAttemptState.ACTIVE,
            )

            observed = repository.observe(
                identifier,
                evidence_refs=("receipt:abc",),
                usage={"input_tokens": 4},
            )

            assert observed.state == TechniqueAttemptState.OBSERVED
            assert observed.evidence_refs == ("receipt:abc",)
            assert observed.terminal_reason is None


def test_authoritative_terminalization_persists_exact_identity_and_authority(
    tmp_path: Path,
) -> None:
    identifier = str(uuid4())
    candidate_identity = {
        "campaign_id": "campaign-1",
        "target_id": "target-1",
        "target_generation": "generation-1",
        "scenario_fingerprint": "scenario-1",
        "technique_id": "prompt-injection/direct",
        "controller_id": "direct",
        "payload_family": "baseline",
        "requested_language": "und",
        "mutation_id": "baseline",
    }
    strategy_state = {
        "candidate_identity": candidate_identity,
        "outcome_authority": {
            "kind": "target",
            "outcome": "confirmed",
            "evidence_refs": ["sha256:result"],
            "target_call_ids": ["target-call-1"],
        },
    }
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            repository = TechniqueAttemptRepository(connection)
            repository.begin(_attempt(identifier))
            repository.transition(
                identifier,
                TechniqueAttemptState.PLANNED,
                TechniqueAttemptState.ACTIVE,
            )

            terminal = repository.terminalize(
                identifier,
                TechniqueAttemptState.CONFIRMED,
                reason="target-owned confirmed outcome",
                evidence_refs=("sha256:result",),
                strategy_state=strategy_state,
            )

            assert terminal.state is TechniqueAttemptState.CONFIRMED
            assert terminal.evidence_refs == ("sha256:result",)
            assert terminal.strategy_state == strategy_state
