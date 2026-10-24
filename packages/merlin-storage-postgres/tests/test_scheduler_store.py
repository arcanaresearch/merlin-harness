import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import merlin_storage_postgres.scheduler_store as scheduler_store_module
import pytest
from merlin_agents import (
    AssignmentBudget,
    DurableAgentService,
    WorkerAssignment,
    WorkerControl,
    WorkerHandoff,
    WorkerProfile,
    WorkerRuntime,
)
from merlin_contracts import (
    AgentTask,
    ModelCompleted,
    ModelError,
    ModelUsage,
    TextDelta,
    ToolEffectRecord,
    ToolEffectState,
    ToolInteractionRole,
)
from merlin_storage_postgres import (
    CampaignStore,
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    PostgresToolEffectStore,
    ScheduleArtifactTooLarge,
    SessionRepository,
    StaleLeaseError,
    ToolEffectRepository,
)
from merlin_tools import ToolBroker
from psycopg.errors import ForeignKeyViolation


def test_adaptive_observation_validates_current_target_authority_binding() -> None:
    binding = {
        "authority": "target",
        "adapter_digest": "sha256:" + "a" * 64,
        "compiled_adapter_digest": "sha256:" + "b" * 64,
        "step_id": "interaction",
        "oracle_digest": "sha256:" + "c" * 64,
        "pointer": "/matched",
        "match_values": [True],
        "modality": "text",
        "turn": "multi",
        "language": "en",
        "purpose": "objective",
    }
    observation = {
        "schema": "merlin.api-oracle-observation.v2",
        **binding,
        "binding_digest": scheduler_store_module._adaptive_observation_digest(binding),
        "selected_digest": "sha256:" + "d" * 64,
        "matched": True,
    }
    observation["observation_digest"] = scheduler_store_module._adaptive_observation_digest(
        observation
    )

    assert scheduler_store_module._valid_adaptive_observation(observation) is True

    for invalid_authority in (None, "model"):
        invalid_binding = {
            key: value
            for key, value in binding.items()
            if key != "authority" or invalid_authority is not None
        }
        if invalid_authority is not None:
            invalid_binding["authority"] = invalid_authority
        invalid = {
            "schema": "merlin.api-oracle-observation.v2",
            **invalid_binding,
            "binding_digest": scheduler_store_module._adaptive_observation_digest(invalid_binding),
            "selected_digest": "sha256:" + "d" * 64,
            "matched": True,
        }
        invalid["observation_digest"] = scheduler_store_module._adaptive_observation_digest(invalid)
        assert scheduler_store_module._valid_adaptive_observation(invalid) is False


@pytest.mark.parametrize("artifact", ("graph", "checkpoint"))
def test_scheduler_rejects_oversized_artifact_before_opening_transaction(
    monkeypatch, artifact: str
) -> None:
    class Connection:
        def __init__(self) -> None:
            self.transaction_opened = False
            self.execute_called = False

        def transaction(self):
            self.transaction_opened = True
            raise AssertionError("oversized schedule reached PostgreSQL")

        def execute(self, *_args, **_kwargs):
            self.execute_called = True
            raise AssertionError("oversized schedule reached PostgreSQL")

    monkeypatch.setattr(scheduler_store_module, "_MAX_SCHEDULE_JSONB_BYTES", 128)
    connection = Connection()
    store = DurableSchedulerStore(connection)
    graph = {"payload": "x" * 256} if artifact == "graph" else {"nodes": ["worker"]}
    checkpoint = {"payload": "x" * 256} if artifact == "checkpoint" else {"nodes": {}}

    with pytest.raises(
        ScheduleArtifactTooLarge,
        match=rf"schedule {artifact}.*reduce the execution batch",
    ) as raised:
        store.create_schedule(
            "00000000-0000-0000-0000-000000000001",
            graph,
            checkpoint,
            ["worker"],
            ready_nodes=["worker"],
        )

    assert raised.value.artifact == artifact
    assert raised.value.actual_bytes > raised.value.limit_bytes == 128
    assert connection.transaction_opened is False
    assert connection.execute_called is False


def test_pg0_scheduler_fences_stale_attempt_and_commits_reducer_atomically(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("scheduler boundary")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"nodes": ["worker", "reducer"]},
                {"completed": []},
                ["worker", "reducer"],
                ready_nodes=["worker"],
            )
            lease = store.claim(
                schedule_id,
                "worker",
                "agent-1",
                lease_expires_at=datetime.now(UTC) + timedelta(milliseconds=100),
            )
            store.heartbeat(
                lease.id,
                lease.generation,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
            )
            connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET lease_expires_at = now() - interval '1 second' WHERE id = %s""",
                (lease.id,),
            )
            connection.commit()
            store.expire(lease.id, lease.generation)
            replacement = store.claim(
                schedule_id,
                "worker",
                "agent-2",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            assert replacement.generation == lease.generation + 1
            attempts = store.list_attempts(schedule_id)
            assert attempts[-1]["replaced_attempt_id"] == lease.id
            with pytest.raises(StaleLeaseError):
                store.commit_reducer(
                    lease.id,
                    lease.generation,
                    schema_id="result/v1",
                    result={"answer": "stale"},
                    checkpoint={"completed": ["worker"]},
                )

            digest = store.commit_reducer(
                replacement.id,
                replacement.generation,
                schema_id="result/v1",
                result={"answer": "done"},
                checkpoint={"completed": ["worker"]},
                messages=[("agent-2", "reducer", "handoff/v1", {"finding": "x"})],
            )
            result = connection.execute(
                "SELECT payload_digest FROM merlin_storage.scheduler_results WHERE attempt_id = %s",
                (replacement.id,),
            ).fetchone()
            completion = connection.execute(
                """SELECT result_digest FROM merlin_storage.scheduler_completions
                   WHERE attempt_id = %s""",
                (replacement.id,),
            ).fetchone()
            node = connection.execute(
                """SELECT state, result_digest FROM merlin_storage.scheduler_nodes
                   WHERE schedule_id = %s AND node_id = 'worker'""",
                (schedule_id,),
            ).fetchone()

            assert result["payload_digest"] == completion["result_digest"] == digest
            assert node == {"state": "completed", "result_digest": digest}
            evidence = SimpleNamespace(
                schedule_id=schedule_id,
                node_id="worker",
                attempt_id=replacement.id,
                result_digest=digest,
            )
            assert store.verify_committed_evidence(evidence)
            evidence.result_digest = f"sha256:{digest}"
            assert store.verify_committed_evidence(evidence)
            evidence.result_digest = "forged"
            assert not store.verify_committed_evidence(evidence)
            events = store.events(schedule_id)
            assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
            assert all(
                current["previous_digest"] == previous["digest"]
                for previous, current in zip(events, events[1:], strict=False)
            )
            store.verify(schedule_id)


def test_pg0_swarm_snapshot_rows_are_batched_and_session_campaign_scoped(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-swarm-snapshot")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = SessionRepository(connection)
            first_session = sessions.create("first snapshot session")
            second_session = sessions.create("second snapshot session")
            scheduler = DurableSchedulerStore(connection)
            campaigns = CampaignStore(connection)

            def create_scoped(
                session_id: str,
                campaign_id: str,
                schedule_id: str,
                task_id: str,
            ) -> None:
                projection = {
                    "schema": "merlin.swarm-projection-identity.v1",
                    "specialist_instance_id": f"specialist/{task_id}",
                    "specialist_identity_provenance": "durable",
                    "specialist_display_name": "Prompt injection specialist",
                    "task_id": task_id,
                    "execution_stage": "direct",
                    "candidates": [
                        {
                            "target_id": "target/one",
                            "target_generation": "generation/one",
                            "scenario_fingerprint": "sha256:scenario",
                            "technique_id": f"technique/{task_id}",
                            "controller_id": "pair",
                            "payload_family": "baseline",
                            "requested_language": "lzh",
                            "mutation_id": "none",
                        }
                    ],
                }
                graph = {
                    "assignments": [
                        {
                            "id": task_id,
                            "target_profile_id": "target/one",
                            "technique_ids": [f"technique/{task_id}"],
                            "swarm_projection": projection,
                        }
                    ]
                }
                scheduler.create_schedule(
                    session_id,
                    graph,
                    {},
                    [f"worker:{task_id}"],
                    ready_nodes=[f"worker:{task_id}"],
                    schedule_id=schedule_id,
                )
                campaigns.prepare(campaign_id, session_id, {}, "sha256:reviewed")
                campaigns.begin_execution(campaign_id, "sha256:reviewed", schedule_id)

            create_scoped(
                str(first_session.id),
                "campaign/first",
                "00000000-0000-0000-0000-000000000001",
                "task-first",
            )
            create_scoped(
                str(first_session.id),
                "campaign/other",
                "00000000-0000-0000-0000-000000000002",
                "task-other",
            )
            create_scoped(
                str(second_session.id),
                "campaign/second-session",
                "00000000-0000-0000-0000-000000000003",
                "task-second-session",
            )
            followup_projection = {
                "schema": "merlin.swarm-projection-identity.v1",
                "specialist_instance_id": "specialist/followup",
                "specialist_identity_provenance": "durable",
                "specialist_display_name": "Adaptive specialist",
                "task_id": "task-followup",
                "execution_stage": "execute",
                "candidates": [],
            }
            followup_schedule = scheduler.create_schedule(
                str(first_session.id),
                {
                    "assignments": [
                        {
                            "id": "task-followup",
                            "target_profile_id": "target/one",
                            "technique_ids": ["technique/followup"],
                            "swarm_projection": followup_projection,
                        }
                    ]
                },
                {},
                ["worker:task-followup"],
                ready_nodes=["worker:task-followup"],
                schedule_id="00000000-0000-0000-0000-000000000004",
            )
            campaigns.add_wave("campaign/first", 1, "followup", followup_schedule)

            lease = scheduler.claim(
                "00000000-0000-0000-0000-000000000001",
                "worker:task-first",
                "snapshot-worker",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
            )
            scheduler.commit_partial(
                lease.id,
                lease.generation,
                result={
                    "content": "Partial capability result",
                    "worker_checkpoint": {
                        "messages": [
                            {"role": "system", "content": "Private worker instructions"},
                            {"role": "user", "content": "Private assignment context"},
                            *(
                                {"role": "assistant", "content": f"Recorded output {index}"}
                                for index in range(6)
                            ),
                            {"role": "tool", "content": "Target response"},
                        ],
                        "action_history": [],
                    },
                },
            )

            rows = scheduler.list_swarm_snapshot_rows(
                str(first_session.id), campaign_id="campaign/first"
            )

            assert len(rows) == 2
            assert {row["session_id"] for row in rows} == {str(first_session.id)}
            assert {row["campaign_id"] for row in rows} == {"campaign/first"}
            assert {row["assignment"]["id"] for row in rows} == {
                "task-first",
                "task-followup",
            }
            assert {
                row["assignment"]["id"] for row in rows if row["current_attempt_id"] is not None
            } == {"task-first"}
            assert all(row["successful_result_id"] is None for row in rows)
            assert all(row["durable_specialist_id"] is None for row in rows)
            current = next(row for row in rows if row["assignment"]["id"] == "task-first")
            assert current["current_result_content"] == "Partial capability result"
            assert [message["content"] for message in current["recent_output"]] == [
                "Recorded output 3",
                "Recorded output 4",
                "Recorded output 5",
                "Target response",
            ]
            assert current["recent_activity"][-1]["type"] == "checkpointed"


def test_pg0_target_adapter_circuit_survives_restart_and_allows_one_half_open_probe(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-adapter-circuit"
    scope = "sha256:" + "a" * 64
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = DurableSchedulerStore(connection)
            assert store.admit_target_adapter_probe(scope)
            store.record_target_adapter_failure(scope, retry_after_seconds=60)
            assert not store.admit_target_adapter_probe(scope)
            retry_at = store.target_adapter_retry_at(scope)
            assert retry_at is not None and retry_at > datetime.now(UTC)
            # A worker admitted before the failure is not the half-open probe and
            # therefore cannot erase an open circuit with a late success.
            store.record_target_adapter_success(scope)
            assert not store.admit_target_adapter_probe(scope)

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = DurableSchedulerStore(connection)
            assert not store.admit_target_adapter_probe(scope)
            connection.execute(
                """UPDATE merlin_storage.target_adapter_circuits
                   SET retry_at = now() - interval '1 second' WHERE scope_key = %s""",
                (scope,),
            )
            first_probe = store.admit_target_adapter_probe(scope)
            assert first_probe is not None and first_probe.startswith("half_open:")
            first_token = first_probe.removeprefix("half_open:")
            assert not store.admit_target_adapter_probe(scope)

    # The admitted probe crashed without reporting success or failure. Its
    # durable half-open lease initially blocks replacement, then admits exactly
    # one new probe after expiry across another database restart.
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = DurableSchedulerStore(connection)
            assert not store.admit_target_adapter_probe(scope)
            connection.execute(
                """UPDATE merlin_storage.target_adapter_circuits
                   SET retry_at = now() - interval '1 second' WHERE scope_key = %s""",
                (scope,),
            )
            # Expiry itself fences the old owner, even before another worker
            # claims the replacement probe lease.
            assert not store.renew_target_adapter_probe(scope, first_token)
            store.record_target_adapter_success(scope, probe_token=first_token)
            store.record_target_adapter_failure(scope, probe_token=first_token)
            replacement_probe = store.admit_target_adapter_probe(scope)
            assert replacement_probe is not None and replacement_probe.startswith("half_open:")
            replacement_token = replacement_probe.removeprefix("half_open:")
            assert not store.admit_target_adapter_probe(scope)
            assert not store.renew_target_adapter_probe(scope, first_token)
            assert store.renew_target_adapter_probe(scope, replacement_token)
            store.record_target_adapter_success(scope, probe_token=first_token)
            store.record_target_adapter_failure(scope, probe_token=first_token)
            assert not store.admit_target_adapter_probe(scope)
            store.record_target_adapter_success(scope, probe_token=replacement_token)
            assert store.admit_target_adapter_probe(scope)


def test_pg0_infrastructure_quarantine_can_be_requeued_without_erasing_history(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-quarantined-retry"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("quarantined adapter retry")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id, {"nodes": ["worker"]}, {}, ["worker"], ready_nodes=["worker"]
            )
            lease = store.claim(
                schedule_id,
                "worker",
                "agent-1",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            store.settle_attempt(
                lease.id,
                lease.generation,
                "quarantined",
                reason="target adapter unhealthy",
                checkpoint={"worker": "ready"},
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = DurableSchedulerStore(connection)
            event_count = store.load_schedule(schedule_id)["event_count"]
            retry_checkpoint = {
                "nodes": {
                    "worker": {
                        "consecutive_retry_stalls": 2,
                        "last_retry_failure_kind": "adapter_timeout",
                    }
                }
            }
            store.retry_failed(
                schedule_id,
                "worker",
                checkpoint=retry_checkpoint,
                expected_event_count=event_count,
            )

            node = store.list_nodes(schedule_id)[0]
            attempts = store.list_attempts(schedule_id)
            assert node["state"] == "ready"
            assert node["failed_attempts"] == 0
            assert attempts[0]["state"] == "quarantined"
            retry_event = store.events(schedule_id)[-1]
            assert retry_event["type"] == "retry_scheduled"
            assert retry_event["payload"]["retry_state"] == retry_checkpoint["nodes"]["worker"]


@pytest.mark.parametrize("failure_kind", ["cleanup_unresolved", "provider_transient"])
def test_pg0_attempt_failure_kind_roundtrips_separately_from_reason(
    tmp_path: Path, failure_kind: str
) -> None:
    data_dir = tmp_path / "postgres-typed-attempt-failure"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("typed attempt failure")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"nodes": ["cleanup:worker"]},
                {},
                ["cleanup:worker"],
                ready_nodes=["cleanup:worker"],
            )
            lease = store.claim(
                schedule_id,
                "cleanup:worker",
                "scheduler",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            checkpoint = {
                "nodes": {
                    "cleanup:worker": {
                        "status": "failed",
                        "last_retry_failure_kind": failure_kind,
                    }
                }
            }
            store.settle_attempt(
                lease.id,
                lease.generation,
                "failed",
                reason="localized human explanation",
                checkpoint=checkpoint,
                failure_kind=failure_kind,
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = DurableSchedulerStore(connection)
            attempt = store.list_attempts(schedule_id)[0]
            event = store.events(schedule_id)[-1]

            assert attempt["error"] == "localized human explanation"
            assert attempt["failure_kind"] == failure_kind
            assert event["payload"]["reason"] == "localized human explanation"
            assert event["payload"]["failure_kind"] == failure_kind
            assert store.load_schedule(schedule_id)["checkpoint"] == checkpoint


def test_pg0_attempt_failure_kind_rejects_unknown_code(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-invalid-failure-kind")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("invalid attempt failure")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id, {"nodes": ["worker"]}, {}, ["worker"], ready_nodes=["worker"]
            )
            lease = store.claim(
                schedule_id,
                "worker",
                "scheduler",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )

            with pytest.raises(ValueError, match="failure kind is unknown"):
                store.settle_attempt(
                    lease.id,
                    lease.generation,
                    "failed",
                    reason="ordinary failure",
                    checkpoint={},
                    failure_kind="invented",
                )


def test_pg0_half_open_probe_neutral_release_is_token_and_expiry_fenced(
    tmp_path: Path,
) -> None:
    scope = "sha256:" + "b" * 64
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-probe-release")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = DurableSchedulerStore(connection)
            store.record_target_adapter_failure(scope, retry_after_seconds=0)
            admission = store.admit_target_adapter_probe(scope)
            assert admission is not None and admission.startswith("half_open:")
            token = admission.removeprefix("half_open:")
            failure_count = connection.execute(
                """SELECT failure_count FROM merlin_storage.target_adapter_circuits
                   WHERE scope_key = %s""",
                (scope,),
            ).fetchone()["failure_count"]

            assert store.release_target_adapter_probe(scope, token)
            row = connection.execute(
                """SELECT state, failure_count, probe_token
                   FROM merlin_storage.target_adapter_circuits WHERE scope_key = %s""",
                (scope,),
            ).fetchone()
            assert row == {
                "state": "open",
                "failure_count": failure_count,
                "probe_token": None,
            }

            replacement = store.admit_target_adapter_probe(scope)
            assert replacement is not None and replacement.startswith("half_open:")
            replacement_token = replacement.removeprefix("half_open:")
            connection.execute(
                """UPDATE merlin_storage.target_adapter_circuits
                   SET retry_at = now() - interval '1 second' WHERE scope_key = %s""",
                (scope,),
            )
            assert not store.release_target_adapter_probe(scope, replacement_token)
            assert not store.release_target_adapter_probe(scope, token)


def test_pg0_retry_lineage_survives_postgres_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("retry lineage restart")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id, {"nodes": ["worker"]}, {}, ["worker"], ready_nodes=["worker"]
            )
            first = store.claim(
                schedule_id,
                "worker",
                "agent-1",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            store.settle_attempt(
                first.id,
                first.generation,
                "failed",
                reason="transient provider failure",
                checkpoint={"worker": "failed"},
            )
            event_count = store.load_schedule(schedule_id)["event_count"]
            store.retry_failed(
                schedule_id,
                "worker",
                checkpoint={"worker": "ready"},
                expected_event_count=event_count,
            )
            replacement = store.claim(
                schedule_id,
                "worker",
                "agent-2",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            restored = DurableSchedulerStore(connection).restore(schedule_id)

    attempts = restored["attempts"]
    assert [attempt["generation"] for attempt in attempts] == [1, 2]
    assert attempts[0]["replaced_attempt_id"] is None
    assert attempts[1]["id"] == replacement.id
    assert attempts[1]["replaced_attempt_id"] == first.id


@pytest.mark.asyncio
async def test_pg0_replacement_result_resolves_persisted_ancestor_evidence_after_reopen(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-inherited-evidence"
    task_id = "campaign/fixture-id/recon"
    node_id = f"worker:{task_id}"
    campaign_id = "campaign/fixture-id"
    target_id = "target-1"
    target_session_id = "target-session-1"
    technique_attempt_id = "technique-attempt-1"
    call_id = "call-before-restart"
    arguments = {"input": "inspect target"}
    content = '{"response":"target observation"}'
    metadata = {
        "interaction_role": ToolInteractionRole.TARGET_EXCHANGE.value,
        "target_session_id": target_session_id,
        "technique_attempt_id": technique_attempt_id,
    }
    request_sha256 = WorkerRuntime._action_fingerprint(  # noqa: SLF001
        "target_request", arguments
    )
    receipt_body: dict[str, object]

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("inherited evidence restart")
            session_id = str(session.id)
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session_id,
                {"nodes": [node_id]},
                {},
                [node_id],
                ready_nodes=[node_id],
            )
            ancestor = store.claim(
                schedule_id,
                node_id,
                "agent-before-restart",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            effects = PostgresToolEffectStore(ToolEffectRepository(connection))
            intent, created = await effects.begin(
                ToolEffectRecord(
                    idempotency_key="effect-before-restart",
                    session_id=session_id,
                    call_id=call_id,
                    agent_id=task_id,
                    attempt_id=ancestor.id,
                    campaign_id=campaign_id,
                    target_id=target_id,
                    tool_name="target_request",
                    effect="target_network",
                    arguments_digest=request_sha256,
                    arguments_payload=arguments,
                    state=ToolEffectState.INTENT,
                )
            )
            assert created
            await effects.finish(
                intent.model_copy(
                    update={
                        "state": ToolEffectState.ACKNOWLEDGED,
                        "result_content": content,
                        "result_is_error": False,
                        "result_metadata": metadata,
                    }
                )
            )
            receipt_body = {
                "call_id": call_id,
                "tool_name": "target_request",
                "reference": f"target-call:{call_id}",
                "campaign_id": campaign_id,
                "assignment_id": task_id,
                "attempt_id": ancestor.id,
                "technique_attempt_id": technique_attempt_id,
                "target_id": target_id,
                "target_session_id": target_session_id,
                "request_sha256": request_sha256,
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "content_bytes": len(content.encode()),
                "metadata_sha256": WorkerRuntime._json_fingerprint(metadata),  # noqa: SLF001
                "is_error": False,
                "acknowledged": True,
            }
            receipt = {
                **receipt_body,
                "receipt_sha256": WorkerRuntime._json_fingerprint(receipt_body),  # noqa: SLF001
            }
            store.settle_attempt(
                ancestor.id,
                ancestor.generation,
                "failed",
                reason="reasoning finalization interrupted after target acknowledgement",
                checkpoint={"nodes": {node_id: {"state": "failed"}}},
                target_session_id=target_session_id,
            )
            event_count = store.load_schedule(schedule_id)["event_count"]
            store.retry_failed(
                schedule_id,
                node_id,
                checkpoint={"nodes": {node_id: {"state": "ready"}}},
                expected_event_count=event_count,
            )

    # The replacement and its final result are created after a real PostgreSQL reopen.
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = DurableSchedulerStore(connection)
            replacement = store.claim(
                schedule_id,
                node_id,
                "agent-after-restart",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            assert store.replacement_attempt_lineage(replacement.id) == (
                ancestor.id,
                replacement.id,
            )
            result_text = '{"applicable_techniques":[]}'
            store.commit_reducer(
                replacement.id,
                replacement.generation,
                schema_id="schema/result",
                result={
                    "content": result_text,
                    "target_id": target_id,
                    "target_session_id": target_session_id,
                    "worker_checkpoint": {
                        "messages": [],
                        "technique_attempt_id": technique_attempt_id,
                        # Recovery carries the immutable ancestor receipt unchanged.
                        "target_evidence": [receipt],
                    },
                },
                checkpoint={"nodes": {node_id: {"state": "completed"}}},
                target_session_id=target_session_id,
            )

    # A second reopen proves both scheduler lineage and tool effects are read from disk.
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            restarted_service = DurableAgentService(
                SimpleNamespace(),
                ToolBroker(effect_store=PostgresToolEffectStore(ToolEffectRepository(connection))),
                DurableSchedulerStore(connection),
            )
            evidence = await restarted_service.committed_task_result_evidence(
                schedule_id,
                AgentTask(
                    id=task_id,
                    objective="recon",
                    status="completed",
                    result=result_text,
                ),
            )

    assert evidence is not None
    assert evidence["attempt_id"] == replacement.id
    assert evidence["evidence_attempt_ids"] == (ancestor.id, replacement.id)
    assert evidence["inherited_target_call_ids"] == (call_id,)
    assert evidence["target_call_ids"] == (call_id,)
    assert evidence["target_call_documents"] == {call_id: {"response": "target observation"}}
    assert evidence["target_evidence_receipts"][call_id]["attempt_id"] == ancestor.id


def test_pg0_cancelled_worker_can_acknowledge_durable_lead_cancellation(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("lead cancellation race")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id, {"nodes": ["worker"]}, {}, ["worker"], ready_nodes=["worker"]
            )
            lease = store.claim(
                schedule_id,
                "worker",
                "agent-1",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            assert store.cancel_node(
                schedule_id,
                "worker",
                reason="lead-directive:cancel-1",
                checkpoint={"cancelled": ["worker"]},
                idempotency_key="cancel-1",
            )

            store.settle_attempt(
                lease.id,
                lease.generation,
                "cancelled",
                reason="cancelled",
                checkpoint={"cancelled": ["worker"]},
            )

            restored = store.restore(schedule_id)
            assert restored["attempts"][0]["state"] == "cancelled"
            assert {row["node_id"]: row["state"] for row in restored["nodes"]}["worker"] == (
                "cancelled"
            )
            assert [event["type"] for event in restored["events"]].count("cancelled") == 1


def test_pg0_durable_cancellation_discards_late_failed_settlement(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("lead cancellation fence")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id, {"nodes": ["worker"]}, {}, ["worker"], ready_nodes=["worker"]
            )
            lease = store.claim(
                schedule_id,
                "worker",
                "agent-1",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            assert store.cancel_node(
                schedule_id,
                "worker",
                reason="lead-directive:cancel-1",
                checkpoint={"cancelled": ["worker"]},
                idempotency_key="cancel-1",
            )

            store.settle_attempt(
                lease.id,
                lease.generation,
                "failed",
                reason="late provider failure",
                checkpoint={"failed": ["worker"]},
            )

            restored = store.restore(schedule_id)
            assert restored["attempts"][0]["state"] == "cancelled"
            assert {row["node_id"]: row["state"] for row in restored["nodes"]}[
                "worker"
            ] == "cancelled"


def test_pg0_child_ancestry_and_observed_target_session_survive_restart(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("child ancestry restart")
            store = DurableSchedulerStore(connection)
            parent_id = store.create_schedule(
                session.id, {"nodes": ["parent"]}, {}, ["parent"], ready_nodes=["parent"]
            )
            parent = store.claim(
                parent_id,
                "parent",
                "lead",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            with pytest.raises(ForeignKeyViolation):
                store.create_schedule(
                    session.id,
                    {"nodes": ["invalid"]},
                    {},
                    ["invalid"],
                    ready_nodes=["invalid"],
                    parent_schedule_id=parent_id,
                    parent_attempt_id="00000000-0000-0000-0000-000000000000",
                )
            child_id = store.create_schedule(
                session.id,
                {"nodes": ["child"]},
                {},
                ["child"],
                ready_nodes=["child"],
                parent_schedule_id=parent_id,
                parent_attempt_id=parent.id,
            )
            child = store.claim(
                child_id,
                "child",
                "worker",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            store.commit_reducer(
                child.id,
                child.generation,
                schema_id="result/v1",
                result={"answer": "done"},
                checkpoint={"completed": ["child"]},
                target_session_id="browser:context-1",
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            restored = DurableSchedulerStore(connection).restore(child_id)
            assert restored["schedule"]["parent_schedule_id"] == parent_id
            assert restored["schedule"]["parent_attempt_id"] == parent.id
            assert restored["attempts"][0]["target_session_id"] == "browser:context-1"
            assert len(restored["results"]) == 1


def test_pg0_target_session_binding_is_optional_idempotent_and_conflict_fenced(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("target session fencing")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"nodes": ["omitted", "conflict"]},
                {},
                ["omitted", "conflict"],
                ready_nodes=["omitted", "conflict"],
            )
            omitted = store.claim(
                schedule_id,
                "omitted",
                "worker-1",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET target_session_id = 'api:campaign-1' WHERE id = %s""",
                (omitted.id,),
            )
            connection.commit()
            store.commit_reducer(
                omitted.id,
                omitted.generation,
                schema_id="result/v1",
                result={"answer": "preserved"},
                checkpoint={"completed": ["omitted"]},
            )
            assert store.list_attempts(schedule_id)[0]["target_session_id"] == "api:campaign-1"

            conflict = store.claim(
                schedule_id,
                "conflict",
                "worker-2",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET target_session_id = 'browser:context-1' WHERE id = %s""",
                (conflict.id,),
            )
            connection.commit()
            with pytest.raises(StaleLeaseError, match="target session identity changed"):
                store.commit_reducer(
                    conflict.id,
                    conflict.generation,
                    schema_id="result/v1",
                    result={"answer": "wrong session"},
                    checkpoint={"completed": ["conflict"]},
                    target_session_id="browser:context-2",
                )
            assert all(row["attempt_id"] != conflict.id for row in store.list_results(schedule_id))


def test_pg0_terminalizes_pending_nodes_with_failed_prerequisites(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("failed prerequisite")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"nodes": ["recon", "injection"]},
                {"nodes": {}},
                ["recon", "injection"],
                ready_nodes=["recon"],
            )
            schedule = store.load_schedule(schedule_id)
            assert schedule is not None

            store.transition_failed(
                schedule_id,
                {"injection": "prerequisite 'recon' failed"},
                checkpoint={"nodes": {"injection": {"status": "failed"}}},
                expected_event_count=schedule["event_count"],
            )

            nodes = {row["node_id"]: row for row in store.list_nodes(schedule_id)}
            assert nodes["recon"]["state"] == "ready"
            assert nodes["injection"]["state"] == "failed"
            event = store.events(schedule_id)[-1]
            assert event["type"] == "prerequisites_failed"
            assert event["payload"]["failures"] == {"injection": "prerequisite 'recon' failed"}
            store.verify(schedule_id)


def test_pg0_transition_ready_skips_already_cancelled_node(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("idempotent ready transition")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"nodes": ["worker:task-1"]},
                {"ready": []},
                ["worker:task-1"],
                ready_nodes=["worker:task-1"],
            )
            # A concurrent Lead cancel moves the node straight to cancelled before
            # the scheduler's guarded pending -> ready transition is applied.
            assert store.cancel_node(
                schedule_id,
                "worker:task-1",
                reason="lead-directive:cancel-1",
                checkpoint={"cancelled": ["worker:task-1"]},
            )
            base = store.load_schedule(schedule_id)
            assert base is not None
            # A late ready-transition of an already-cancelled node is a no-op,
            # not a ValueError.
            store.transition_ready(
                schedule_id,
                ["worker:task-1"],
                checkpoint={"ready": ["worker:task-1"]},
                expected_event_count=base["event_count"],
            )
            nodes = {row["node_id"]: row for row in store.list_nodes(schedule_id)}
            assert nodes["worker:task-1"]["state"] == "cancelled"
            store.verify(schedule_id)
            # Genuinely unknown nodes still fail closed.
            current = store.load_schedule(schedule_id)
            with pytest.raises(ValueError, match="unknown"):
                store.transition_ready(
                    schedule_id,
                    ["worker:ghost"],
                    checkpoint={"ready": ["worker:ghost"]},
                    expected_event_count=current["event_count"],
                )


def test_pg0_transition_failed_skips_already_cancelled_node(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("idempotent failed transition")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"nodes": ["worker:task-1"]},
                {"failed": []},
                ["worker:task-1"],
                ready_nodes=["worker:task-1"],
            )
            assert store.cancel_node(
                schedule_id,
                "worker:task-1",
                reason="lead-directive:cancel-1",
                checkpoint={"cancelled": ["worker:task-1"]},
            )
            base = store.load_schedule(schedule_id)
            assert base is not None
            # A late failed-transition of an already-cancelled node is a no-op,
            # not a ValueError.
            store.transition_failed(
                schedule_id,
                {"worker:task-1": "prerequisite failed"},
                checkpoint={"failed": ["worker:task-1"]},
                expected_event_count=base["event_count"],
            )
            nodes = {row["node_id"]: row for row in store.list_nodes(schedule_id)}
            assert nodes["worker:task-1"]["state"] == "cancelled"
            store.verify(schedule_id)
            current = store.load_schedule(schedule_id)
            with pytest.raises(ValueError, match="unknown"):
                store.transition_failed(
                    schedule_id,
                    {"worker:ghost": "prerequisite failed"},
                    checkpoint={"failed": ["worker:ghost"]},
                    expected_event_count=current["event_count"],
                )


def test_pg0_cancelled_schedule_remains_recoverable_until_cleanup_commits(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("cancel cleanup")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"nodes": ["worker:task", "cleanup:task"]},
                {"nodes": {}},
                ["worker:task", "cleanup:task"],
                ready_nodes=["worker:task"],
            )

            store.cancel_schedule(
                schedule_id,
                reason="operator stop",
                cleanup_node_ids=["cleanup:task"],
                checkpoint={"cleanup": ["cleanup:task"]},
            )

            assert [row["id"] for row in store.list_active_schedules()] == [schedule_id]
            lease = store.claim(
                schedule_id,
                "cleanup:task",
                "scheduler",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            store.commit_reducer(
                lease.id,
                lease.generation,
                schema_id="schema/result",
                result={"cleaned": "task"},
                checkpoint={"cleanup": "completed"},
            )

            assert store.list_active_schedules() == []
            restored = store.restore(schedule_id)
            assert restored["schedule"]["status"] == "cancelled"
            states = {node["node_id"]: node["state"] for node in restored["nodes"]}
            assert states == {"cleanup:task": "completed", "worker:task": "cancelled"}
            store.verify(schedule_id)


def test_pg0_reject_schedule_terminalizes_stranded_cleanup_without_graph_hydration(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("reject incompatible schedule")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"assignments": [{"obsolete_field": True}]},
                {"nodes": {}},
                ["worker:task", "cleanup:task"],
                ready_nodes=["worker:task"],
            )
            store.cancel_schedule(
                schedule_id,
                reason="campaign rejected",
                cleanup_node_ids=["cleanup:task"],
                checkpoint={"cleanup": ["cleanup:task"]},
            )
            assert [row["id"] for row in store.list_active_schedules()] == [schedule_id]

            store.reject_schedule(schedule_id, reason="persisted graph is incompatible")

            assert store.list_active_schedules() == []
            restored = store.restore(schedule_id)
            assert restored["schedule"]["status"] == "cancelled"
            assert {node["node_id"]: node["state"] for node in restored["nodes"]} == {
                "cleanup:task": "cancelled",
                "worker:task": "cancelled",
            }
            assert restored["events"][-1]["type"] == "schedule_rejected"


def test_pg0_schedule_completion_requires_terminal_reducer(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("natural completion")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"nodes": ["worker", "reducer"]},
                {},
                ["worker", "reducer"],
                ready_nodes=["worker", "reducer"],
            )
            with pytest.raises(ValueError, match="work remains"):
                store.complete_schedule(schedule_id)
            for node_id in ("worker", "reducer"):
                lease = store.claim(
                    schedule_id,
                    node_id,
                    "scheduler",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
                store.commit_reducer(
                    lease.id,
                    lease.generation,
                    schema_id="result/v1",
                    result={"node": node_id},
                    checkpoint={"completed": node_id},
                )
            store.complete_schedule(schedule_id)
            assert store.load_schedule(schedule_id)["status"] == "completed"
            assert store.events(schedule_id)[-1]["type"] == "schedule_completed"
            store.complete_schedule(schedule_id)


def test_pg0_paused_schedule_fences_claims_and_resumes_from_checkpoint(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("durable pause")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"nodes": ["worker"]},
                {"cursor": 7},
                ["worker"],
                ready_nodes=["worker"],
            )
            first = store.claim(
                schedule_id,
                "worker",
                "agent-1",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
            )
            message = store.send_message(
                schedule_id,
                "lead",
                "worker",
                "schema/test",
                {"cursor": 7},
            )
            claimed = store.claim_messages(first.id, first.generation, "worker", limit=1)
            assert [item.id for item in claimed] == [message.id]

            store.pause_schedule(schedule_id, reason="operator")
            assert store.load_schedule(schedule_id)["status"] == "paused"
            paused = store.restore(schedule_id)
            assert paused["attempts"][0]["state"] == "expired"
            assert paused["attempts"][0]["error"] == "clean pause: operator"
            assert paused["nodes"][0]["state"] == "ready"
            assert paused["mailbox"][0]["claimed_by_attempt"] is None
            with pytest.raises(StaleLeaseError, match="current lease generation"):
                store.commit_reducer(
                    first.id,
                    first.generation,
                    schema_id="result/v1",
                    result={"late": True},
                    checkpoint={"cursor": 999},
                )
            with pytest.raises(StaleLeaseError, match="not running"):
                store.claim(
                    schedule_id,
                    "worker",
                    "agent",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )

            store.resume_schedule(schedule_id)
            restored = store.restore(schedule_id)
            assert restored["schedule"]["status"] == "running"
            assert restored["schedule"]["checkpoint"] == {"cursor": 7}
            replacement = store.claim(
                schedule_id,
                "worker",
                "agent-2",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            assert replacement.generation == first.generation + 1
            resumed = store.restore(schedule_id)
            assert resumed["attempts"][-1]["replaced_attempt_id"] == first.id
            assert [event["type"] for event in restored["events"]][-2:] == [
                "schedule_paused",
                "schedule_resumed",
            ]


def test_pg0_campaign_pause_and_resume_survive_repository_reopen(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("campaign pause")
            scheduler = DurableSchedulerStore(connection)
            schedule_id = scheduler.create_schedule(
                session.id, {"nodes": ["worker"]}, {}, ["worker"], ready_nodes=["worker"]
            )
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                "campaign/pause",
                str(session.id),
                {"manifest": {"objective": "pause acceptance"}},
                "sha256:reviewed",
            )
            campaigns.begin_execution("campaign/pause", "sha256:reviewed", schedule_id)

            campaigns.pause("campaign/pause")
            reopened = CampaignStore(connection)
            assert reopened.get("campaign/pause")["status"] == "paused"
            assert reopened.list_waves("campaign/pause")[0]["status"] == "paused"
            assert reopened.list_executing() == []

            reopened.resume("campaign/pause")
            assert reopened.get("campaign/pause")["status"] == "executing"
            assert reopened.list_waves("campaign/pause")[0]["status"] == "running"


def test_pg0_failed_campaign_can_retry_without_rewriting_completed_wave(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("campaign retry")
            scheduler = DurableSchedulerStore(connection)
            schedule_id = scheduler.create_schedule(
                session.id, {"nodes": ["worker"]}, {}, ["worker"], ready_nodes=["worker"]
            )
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                "campaign/retry",
                str(session.id),
                {"manifest": {"objective": "retry acceptance"}},
                "sha256:reviewed",
            )
            campaigns.begin_execution("campaign/retry", "sha256:reviewed", schedule_id)
            campaigns.finish_wave("campaign/retry", 0, "completed", {"completed": 1})
            campaigns.finish("campaign/retry", "failed")

            retried = campaigns.retry_incomplete("campaign/retry")

            assert retried["status"] == "executing"
            assert retried["stop_reason"] is None
            assert campaigns.list_waves("campaign/retry")[0]["status"] == "completed"


def test_pg0_campaign_worker_binds_authoritative_technique_attempt(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("technique attempt binding")
            scheduler = DurableSchedulerStore(connection)
            task_id = "worker/injection"
            node_id = f"worker:{task_id}"
            terminal_task_id = "worker/terminal"
            terminal_node_id = f"worker:{terminal_task_id}"
            expired_task_id = "worker/expired"
            expired_node_id = f"worker:{expired_task_id}"
            schedule_id = scheduler.create_schedule(
                session.id,
                {"nodes": [node_id, terminal_node_id, expired_node_id]},
                {},
                [node_id, terminal_node_id, expired_node_id],
                ready_nodes=[node_id, terminal_node_id, expired_node_id],
            )
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                "campaign/attempt-binding",
                str(session.id),
                {"manifest": {"objective": "binding acceptance"}},
                "sha256:reviewed",
            )
            campaigns.begin_execution("campaign/attempt-binding", "sha256:reviewed", schedule_id)
            lease = scheduler.claim(
                schedule_id,
                node_id,
                "agent-1",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            candidate_identity = {
                "campaign_id": "campaign/attempt-binding",
                "target_id": "target/reviewed",
                "target_generation": "generation/one",
                "scenario_fingerprint": "scenario/one",
                "technique_id": "prompt-injection/direct",
                "controller_id": "direct",
                "payload_family": "encoding/rot13",
                "requested_language": "fr",
                "mutation_id": "mutation/rot13-v1",
            }

            technique_attempt_id = scheduler.bind_technique_attempt(
                schedule_id,
                task_id,
                lease.id,
                technique_id="prompt-injection/direct",
                strategy_id="direct",
                oracle_digest="sha256:" + "a" * 64,
                control_kind="attack",
                language="fr",
                transform_id="encoding/rot13",
                target_id="target/reviewed",
                mutation_id="mutation/rot13-v1",
                provider="provider-1",
                model="model-1",
                strategy_state={"candidate_identity": candidate_identity},
            )
            scheduler.observe_technique_attempt(
                technique_attempt_id or "", evidence_refs=("receipt:one",)
            )
            attempts = scheduler.list_technique_attempts(schedule_id)

            assert len(attempts) == 1
            assert attempts[0].id == technique_attempt_id
            assert attempts[0].state.value == "observed"
            assert attempts[0].scheduler_attempt_id == lease.id
            assert attempts[0].evidence_refs == ("receipt:one",)
            assert attempts[0].strategy_state["candidate_identity"] == candidate_identity

            mismatches = (
                {**candidate_identity, "campaign_id": "campaign/other"},
                {**candidate_identity, "target_id": "target/other"},
                {**candidate_identity, "technique_id": "technique/other"},
                {**candidate_identity, "controller_id": "crescendo"},
                {**candidate_identity, "payload_family": "encoding/base64"},
                {**candidate_identity, "requested_language": "de"},
                {**candidate_identity, "mutation_id": "mutation/other"},
            )
            for mismatched in mismatches:
                with pytest.raises(
                    ValueError,
                    match="sealed candidate identity conflicts with the executed assignment axes",
                ):
                    scheduler.bind_technique_attempt(
                        schedule_id,
                        task_id,
                        lease.id,
                        technique_id="prompt-injection/direct",
                        strategy_id="direct",
                        oracle_digest="sha256:" + "a" * 64,
                        control_kind="attack",
                        language="fr",
                        transform_id="encoding/rot13",
                        target_id="target/reviewed",
                        mutation_id="mutation/rot13-v1",
                        strategy_state={"candidate_identity": mismatched},
                    )

            foreign_schedule = scheduler.create_schedule(
                session.id,
                {"nodes": [node_id]},
                {},
                [node_id],
                ready_nodes=[node_id],
            )
            foreign_lease = scheduler.claim(
                foreign_schedule,
                node_id,
                "agent-foreign",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            with pytest.raises(
                ValueError,
                match="scheduler attempt does not own the expected campaign task lease",
            ):
                scheduler.bind_technique_attempt(
                    schedule_id,
                    task_id,
                    foreign_lease.id,
                    technique_id="prompt-injection/direct",
                    strategy_id="direct",
                    oracle_digest="sha256:" + "a" * 64,
                    control_kind="attack",
                    language="fr",
                    transform_id="encoding/rot13",
                    target_id="target/reviewed",
                    mutation_id="mutation/rot13-v1",
                    strategy_state={"candidate_identity": candidate_identity},
                )
            physical_identity = "sha256:" + "b" * 64
            scheduler.terminalize_technique_attempt(
                technique_attempt_id,
                "confirmed",
                reason="target-owned confirmed outcome",
                evidence_refs=("receipt:one",),
                usage={"target_calls": 1},
                strategy_state={
                    "candidate_identity": candidate_identity,
                    "outcome_authority": {
                        "kind": "target",
                        "outcome": "confirmed",
                        "evidence_refs": ["receipt:one"],
                    },
                },
                disposition="valid",
                disposition_reason="target-authored outcome matched the reviewed oracle",
                physical_state_identity=physical_identity,
                state_identity_match=True,
            )
            terminal = scheduler.get_technique_attempt(technique_attempt_id)
            assert terminal is not None
            assert terminal.state.value == "confirmed"
            assert terminal.disposition is not None
            assert terminal.disposition.value == "valid"
            assert terminal.disposition_reason == (
                "target-authored outcome matched the reviewed oracle"
            )
            assert terminal.physical_state_identity == physical_identity
            assert terminal.state_identity_match is True
            assert terminal.strategy_state["outcome_authority"]["kind"] == "target"
            expired_lease = scheduler.claim(
                schedule_id,
                expired_node_id,
                "agent-expired",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            connection.execute(
                "UPDATE merlin_storage.scheduler_attempts "
                "SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                (expired_lease.id,),
            )
            with pytest.raises(
                ValueError,
                match="scheduler attempt does not own the expected campaign task lease",
            ):
                scheduler.bind_technique_attempt(
                    schedule_id,
                    expired_task_id,
                    expired_lease.id,
                    technique_id="prompt-injection/direct",
                    strategy_id="direct",
                    oracle_digest="sha256:" + "a" * 64,
                    control_kind="attack",
                    language="fr",
                    transform_id="encoding/rot13",
                    target_id="target/reviewed",
                    mutation_id="mutation/rot13-v1",
                    strategy_state={"candidate_identity": candidate_identity},
                )
            terminal_lease = scheduler.claim(
                schedule_id,
                terminal_node_id,
                "agent-terminal",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            scheduler.settle_attempt(
                terminal_lease.id,
                terminal_lease.generation,
                "failed",
                reason="worker stopped before binding",
                checkpoint={"nodes": {}},
                failure_kind="worker_interrupted",
            )
            with pytest.raises(
                ValueError,
                match="scheduler attempt does not own the expected campaign task lease",
            ):
                scheduler.bind_technique_attempt(
                    schedule_id,
                    terminal_task_id,
                    terminal_lease.id,
                    technique_id="prompt-injection/direct",
                    strategy_id="direct",
                    oracle_digest="sha256:" + "a" * 64,
                    control_kind="attack",
                    language="fr",
                    transform_id="encoding/rot13",
                    target_id="target/reviewed",
                    mutation_id="mutation/rot13-v1",
                    strategy_state={"candidate_identity": candidate_identity},
                )
            with pytest.raises(
                ValueError,
                match="scheduler attempt does not own the expected campaign task lease",
            ):
                scheduler.bind_technique_attempt(
                    schedule_id,
                    "worker/other",
                    lease.id,
                    technique_id="prompt-injection/direct",
                    strategy_id="direct",
                    oracle_digest="sha256:" + "a" * 64,
                    control_kind="attack",
                    language="fr",
                    transform_id="encoding/rot13",
                    target_id="target/reviewed",
                    mutation_id="mutation/rot13-v1",
                    strategy_state={"candidate_identity": candidate_identity},
                )


def test_pg0_terminal_campaign_without_report_is_discoverable_even_before_error_journal(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("report retry")
            scheduler = DurableSchedulerStore(connection)
            schedule_id = scheduler.create_schedule(
                session.id, {"nodes": ["worker"]}, {}, ["worker"], ready_nodes=["worker"]
            )
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                "campaign/report-retry",
                str(session.id),
                {
                    "manifest": {"objective": "report retry"},
                    "assignment_context": {},
                },
                "sha256:reviewed",
            )
            campaigns.begin_execution("campaign/report-retry", "sha256:reviewed", schedule_id)
            campaigns.finish(
                "campaign/report-retry",
                "failed",
                stop_reason="infrastructure_blocked_after_retry_policy",
            )

            assert [row["id"] for row in campaigns.list_report_retryable()] == [
                "campaign/report-retry"
            ]


def test_pg0_legacy_terminal_campaign_without_compiled_context_is_not_reportable(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("legacy terminal")
            scheduler = DurableSchedulerStore(connection)
            schedule_id = scheduler.create_schedule(
                session.id, {"nodes": ["worker"]}, {}, ["worker"], ready_nodes=["worker"]
            )
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                "campaign/legacy-terminal",
                str(session.id),
                {"manifest": {"id": "campaign/legacy-terminal"}},
                "sha256:reviewed",
            )
            campaigns.begin_execution("campaign/legacy-terminal", "sha256:reviewed", schedule_id)
            campaigns.finish(
                "campaign/legacy-terminal",
                "failed",
                stop_reason="infrastructure_blocked_after_retry_policy",
            )

            assert campaigns.list_report_retryable() == []


def test_pg0_nonretryable_terminal_report_error_is_not_rediscovered(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("malformed report")
            scheduler = DurableSchedulerStore(connection)
            schedule_id = scheduler.create_schedule(
                session.id, {"nodes": ["worker"]}, {}, ["worker"], ready_nodes=["worker"]
            )
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                "campaign/malformed-report",
                str(session.id),
                {
                    "manifest": {"objective": "malformed report"},
                    "assignment_context": {},
                },
                "sha256:reviewed",
            )
            campaigns.begin_execution("campaign/malformed-report", "sha256:reviewed", schedule_id)
            campaigns.finish(
                "campaign/malformed-report",
                "failed",
                stop_reason="infrastructure_blocked_after_retry_policy",
            )
            assert [row["id"] for row in campaigns.list_report_retryable()] == [
                "campaign/malformed-report"
            ]

            campaigns.append_lead(
                "campaign/malformed-report",
                0,
                {"report_error": "invalid historical manifest", "retryable": False},
            )

            assert campaigns.list_report_retryable() == []


def test_pg0_terminal_campaign_with_running_schedule_is_reconciled(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("terminal schedule")
            scheduler = DurableSchedulerStore(connection)
            schedule_id = scheduler.create_schedule(
                session.id, {"nodes": ["worker"]}, {}, ["worker"], ready_nodes=["worker"]
            )
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                "campaign/terminal-schedule",
                str(session.id),
                {"manifest": {"objective": "terminal schedule"}},
                "sha256:reviewed",
            )
            campaigns.begin_execution("campaign/terminal-schedule", "sha256:reviewed", schedule_id)
            campaigns.finish(
                "campaign/terminal-schedule",
                "failed",
                stop_reason="infrastructure_blocked_after_retry_policy",
            )

            assert [row["id"] for row in campaigns.list_terminal_active_schedules()] == [
                "campaign/terminal-schedule"
            ]

            scheduler.cancel_schedule(
                schedule_id,
                reason="terminal campaign recovery",
                cleanup_node_ids=(),
                checkpoint={},
            )
            assert campaigns.list_terminal_active_schedules() == []


def test_pg0_worker_coordination_survives_reopen_until_lead_acknowledges(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("worker coordination")
            scheduler = DurableSchedulerStore(connection)
            schedule_id = scheduler.create_schedule(
                session.id, {"nodes": ["worker"]}, {}, ["worker"], ready_nodes=["worker"]
            )
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                "campaign/coordination",
                str(session.id),
                {"manifest": {"objective": "coordination acceptance"}},
                "sha256:reviewed",
            )
            campaigns.begin_execution("campaign/coordination", "sha256:reviewed", schedule_id)
            message = {
                "id": "worker-message:one",
                "campaign_id": "campaign/coordination",
                "wave": 0,
                "source_task_id": "campaign/coordination/recon",
                "destination": "lead_and_peers",
                "content": "Reuse the observed submit selector.",
                "content_digest": "sha256:one",
            }
            assert campaigns.record_worker_message(message)["id"] == "worker-message:one"

            reopened = CampaignStore(connection)
            assert (
                reopened.list_pending_worker_messages("campaign/coordination")[0]["content"]
                == "Reuse the observed submit selector."
            )
            assert reopened.acknowledge_worker_messages(("worker-message:one",)) == 1
            assert reopened.list_pending_worker_messages("campaign/coordination") == []


def test_pg0_campaign_lessons_retain_distinct_comparable_techniques(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("comparable attempts")
            scheduler = DurableSchedulerStore(connection)
            schedule_id = scheduler.create_schedule(
                session.id, {"nodes": ["worker"]}, {}, ["worker"], ready_nodes=["worker"]
            )
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                "campaign/attempts",
                str(session.id),
                {"manifest": {"objective": "attempt identity acceptance"}},
                "sha256:reviewed",
            )
            campaigns.begin_execution("campaign/attempts", "sha256:reviewed", schedule_id)
            base = {
                "campaign_id": "campaign/attempts",
                "session_id": str(session.id),
                "wave": 0,
                "task_id": "campaign/attempts/injection",
                "profile_id": "injection",
                "target_id": "target-1",
                "outcome": "negative_result",
                "content": '{"outcome":"negative_result"}',
                "evidence_digest": "sha256:evidence",
                "target_fingerprint": "sha256:target",
                "adapter_fingerprint": "sha256:adapter",
                "scenario_fingerprint": "sha256:scenario-one",
                "surface": "https://target.test/chat",
                "transform_id": "baseline",
                "language": "und",
                "oracle_digest": "sha256:oracle",
                "validity_seconds": 86_400,
            }
            for suffix in ("one", "two"):
                campaigns.record_lesson(
                    {
                        **base,
                        "id": f"lesson:{suffix}",
                        "technique_id": f"technique:{suffix}",
                        "attempt_identity": f"sha256:{suffix}",
                    }
                )
            campaigns.record_lesson(
                {
                    **base,
                    "id": "lesson:other-scenario",
                    "task_id": "campaign/attempts/injection-other-scenario",
                    "technique_id": "technique:one",
                    "attempt_identity": "sha256:other-scenario",
                    "scenario_fingerprint": "sha256:scenario-two",
                }
            )

            lessons = campaigns.list_lessons(str(session.id))
            assert {lesson["technique_id"] for lesson in lessons} == {
                "technique:one",
                "technique:two",
            }
            history = campaigns.list_attempt_history("target-1", ("technique:one", "technique:two"))
            assert {lesson["technique_id"] for lesson in history} == {
                "technique:one",
                "technique:two",
            }
            assert len(history) == 3
            assert {lesson["scenario_fingerprint"] for lesson in history} == {
                "sha256:scenario-one",
                "sha256:scenario-two",
            }
            assert all(lesson["valid_until"] > lesson["created_at"] for lesson in history)


def test_pg0_mailbox_claims_are_generation_fenced_and_acknowledged(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("mailbox boundary")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id, {"nodes": ["agent"]}, {}, ["agent"], ready_nodes=["agent"]
            )
            message = store.send_message(
                schedule_id, "lead", "agent", "instruction/v1", {"text": "continue"}
            )
            lease = store.claim(
                schedule_id,
                "agent",
                "worker",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            claimed = store.claim_messages(lease.id, lease.generation, "agent")
            assert claimed == [message]
            assert store.acknowledge_messages(lease.id, lease.generation, [message.id]) == 1
            assert (
                store.acknowledge_messages(lease.id, lease.generation, [message.id, message.id])
                == 1
            )
            assert store.claim_messages(lease.id, lease.generation, "agent") == []


def test_pg0_deterministic_mailbox_delivery_is_queued_once(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("mailbox idempotency")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id, {"nodes": ["agent"]}, {}, ["agent"], ready_nodes=["agent"]
            )
            message_id = "68cf7a1f-9faa-48b7-9758-2c560baa8a24"

            first, first_created = store.send_message_once(
                message_id,
                schedule_id,
                "worker:source",
                "worker:agent",
                "merlin.worker-coordination.v1",
                {"message": "reuse the verified submit selector"},
            )
            second, second_created = store.send_message_once(
                message_id,
                schedule_id,
                "worker:source",
                "worker:agent",
                "merlin.worker-coordination.v1",
                {"message": "reuse the verified submit selector"},
            )

            assert first == second
            assert first_created is True
            assert second_created is False
            assert len(store.list_mailbox(schedule_id)) == 1


def test_pg0_unacknowledged_mail_is_redelivered_after_attempt_expiry(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("mailbox redelivery")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id, {"nodes": ["agent"]}, {}, ["agent"], ready_nodes=["agent"]
            )
            message = store.send_message(
                schedule_id, "lead", "agent", "instruction/v1", {"text": "continue"}
            )
            first = store.claim(
                schedule_id,
                "agent",
                "worker-1",
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=1),
            )
            assert store.claim_messages(first.id, first.generation, "agent") == [message]
            connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET lease_expires_at = now() - interval '1 second' WHERE id = %s""",
                (first.id,),
            )
            connection.commit()
            store.expire(first.id, first.generation)

            replacement = store.claim(
                schedule_id,
                "agent",
                "worker-2",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            assert store.claim_messages(replacement.id, replacement.generation, "agent") == [
                message
            ]
            projection = store.restore(schedule_id)
            assert projection["mailbox"][0]["claimed_by_attempt"] == replacement.id


def test_pg0_live_mail_checkpoint_survives_restart_without_redelivery(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("live mailbox checkpoint")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id, {"nodes": ["agent"]}, {}, ["agent"], ready_nodes=["agent"]
            )
            first = store.claim(
                schedule_id,
                "agent",
                "worker-1",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            message = store.send_message(
                schedule_id, "lead", "agent", "instruction/v1", {"message": "continue"}
            )
            assert store.claim_message(first.id, first.generation, "agent", message.id) == message
            checkpoint = {
                "worker_checkpoint": {
                    "version": 1,
                    "messages": [{"role": "user", "content": "Operator message: continue"}],
                    "usage": {"total_tokens": 17},
                    "effect_observed": False,
                    "compaction": {"source_count": 2, "source_digest": "a" * 64},
                },
                "partial": True,
            }
            store.commit_partial(
                first.id,
                first.generation,
                result=checkpoint,
                applied_message_ids=[message.id],
            )
            # Replaying the same durable checkpoint is idempotent for the same
            # fenced attempt. It must not be mistaken for a stale generation.
            store.commit_partial(
                first.id,
                first.generation,
                result=checkpoint,
                applied_message_ids=[message.id, message.id],
            )
            connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET lease_expires_at = now() - interval '1 second' WHERE id = %s""",
                (first.id,),
            )
            connection.commit()
            store.expire(first.id, first.generation)
            replacement = store.claim(
                schedule_id,
                "agent",
                "worker-2",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )

            assert store.claim_messages(replacement.id, replacement.generation, "agent") == []
            restored = store.restore(schedule_id)
            partial = next(
                result
                for result in restored["results"]
                if result["schema_id"] == "schema/partial-result"
            )
            assert partial["payload"] == checkpoint
            assert restored["mailbox"][0]["acknowledged_at"] is not None
            assert [event["type"] for event in restored["events"]].count("checkpointed") == 1


@pytest.mark.asyncio
async def test_pg0_worker_restart_restores_mail_context_budget_and_retry_state(
    tmp_path: Path,
) -> None:
    class NoTools:
        def describe(self):
            return ()

        async def invoke(self, name, context, arguments):
            raise AssertionError("no tools admitted")

    class InterruptedProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield ModelUsage(input_tokens=6, output_tokens=3, total_tokens=9)
                yield ModelError(message="busy", retryable=True)
                return
            await __import__("asyncio").Event().wait()
            if False:
                yield ModelCompleted()

    class ReplacementProvider:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield ModelUsage(input_tokens=0, output_tokens=1, total_tokens=1)
            yield TextDelta(text='{"finding":"done"}')
            yield ModelCompleted()

    profile = WorkerProfile("recon", 1, "summary", "Map carefully.", frozenset({"map"}))
    assignment = WorkerAssignment(
        id="task-1",
        objective="Inspect",
        profile_id="recon",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("session",),
        budget=AssignmentBudget(3, 10, 30),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object","required":["finding"]}',
    )
    handoff = WorkerHandoff("session", ".", "model", profile, assignment)

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("combined worker restart")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id, {"nodes": ["agent"]}, {}, ["agent"], ready_nodes=["agent"]
            )
            first = store.claim(
                schedule_id,
                "agent",
                "worker-1",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            message = store.send_message(
                schedule_id, "lead", "agent", "instruction/v1", {"message": "continue"}
            )
            store.claim_message(first.id, first.generation, "agent", message.id)
            control = WorkerControl()
            control.send("continue", message_id=message.id)

            async def checkpoint_writer(checkpoint, message_ids):
                store.commit_partial(
                    first.id,
                    first.generation,
                    result={"worker_checkpoint": dict(checkpoint), "partial": True},
                    applied_message_ids=message_ids,
                )

            runtime = WorkerRuntime(
                InterruptedProvider(),
                NoTools(),
                max_model_turns=3,
                max_tokens=10,
                max_seconds=30,
                checkpoint_writer=checkpoint_writer,
            )
            task = __import__("asyncio").create_task(runtime.run(handoff, control))
            for _ in range(100):
                partial = store.list_results(schedule_id)
                if (
                    partial
                    and partial[0]["payload"]
                    .get("worker_checkpoint", {})
                    .get("usage", {})
                    .get("total_tokens")
                    == 9
                ):
                    break
                await __import__("asyncio").sleep(0.01)
            task.cancel()
            await __import__("asyncio").gather(task, return_exceptions=True)
            connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET lease_expires_at = now() - interval '1 second' WHERE id = %s""",
                (first.id,),
            )
            connection.commit()
            store.expire(first.id, first.generation)
            replacement = store.claim(
                schedule_id,
                "agent",
                "worker-2",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            assert store.claim_messages(replacement.id, replacement.generation, "agent") == []
            checkpoint = store.list_results(schedule_id)[0]["payload"]["worker_checkpoint"]
            assert checkpoint["turns_completed"] == 1
            assert checkpoint["active_phase"] == "provider"
            provider = ReplacementProvider()
            result = await WorkerRuntime(
                provider, NoTools(), max_model_turns=3, max_tokens=10, max_seconds=30
            ).run(handoff, checkpoint=checkpoint)

            assert result.error == "worker exhausted the token budget"
            assert result.retry_class == "terminal"
            assert provider.requests[0].max_output_tokens == 1
            assert any(
                "Worker mail from" in item.content and "\ncontinue" in item.content
                for item in provider.requests[0].messages
            )


def test_pg0_unacknowledged_completion_is_projected_after_restart(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("completion redelivery")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id, {"nodes": ["child"]}, {}, ["child"], ready_nodes=["child"]
            )
            child = store.claim(
                schedule_id,
                "child",
                "worker",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            store.commit_reducer(
                child.id,
                child.generation,
                schema_id="result/v1",
                result={"answer": "done"},
                checkpoint={"completed": ["child"]},
            )

            pending = store.list_completions(schedule_id, unacknowledged_only=True)
            assert [item["attempt_id"] for item in pending] == [child.id]
            assert store.restore(schedule_id)["completions"] == pending
            store.acknowledge_completion(child.id)
            assert store.list_completions(schedule_id, unacknowledged_only=True) == []


def test_pg0_terminal_prompt_digest_survives_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres"
    schedule_id = attempt_id = ""
    prompt_digest = "sha256:" + "a" * 64
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("terminal prompt restart")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id, {"nodes": ["agent"]}, {}, ["agent"], ready_nodes=["agent"]
            )
            attempt = store.claim(
                schedule_id,
                "agent",
                "worker-1",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            attempt_id = attempt.id
            store.commit_partial(
                attempt.id,
                attempt.generation,
                result={
                    "partial": True,
                    "worker_checkpoint": {
                        "terminal_prompt_digest": prompt_digest,
                        "terminal_prompt_state": "captured",
                    },
                },
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            restored = DurableSchedulerStore(connection).restore(schedule_id)
            result = next(row for row in restored["results"] if row["attempt_id"] == attempt_id)
            checkpoint = result["payload"]["worker_checkpoint"]
            assert checkpoint["terminal_prompt_digest"] == prompt_digest
            assert checkpoint["terminal_prompt_state"] == "captured"


def test_pg0_restart_projection_transitions_and_cancelled_cleanup(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("restart boundary")
            store = DurableSchedulerStore(connection)
            graph = {
                "nodes": ["lead", "worker:task-1", "cleanup"],
                "assignments": {"task-1": {"provider": "openai", "model": "model"}},
            }
            schedule_id = store.create_schedule(
                session.id,
                graph,
                {"ready": ["lead"]},
                ["lead", "worker:task-1", "cleanup"],
                ready_nodes=["lead"],
            )
            lead = store.claim(
                schedule_id,
                "lead",
                "lead-agent",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            store.settle_attempt(
                lead.id,
                lead.generation,
                "failed",
                reason="provider unavailable",
                checkpoint={"failed": ["lead"]},
            )
            base = store.load_schedule(schedule_id)
            assert base is not None
            store.retry_failed(
                schedule_id,
                "lead",
                checkpoint={"retry": ["lead"]},
                expected_event_count=base["event_count"],
            )
            replacement = store.claim(
                schedule_id,
                "lead",
                "lead-replacement",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            assert replacement.generation == lead.generation + 1
            store.settle_attempt(
                replacement.id,
                replacement.generation,
                "failed",
                reason="retry exhausted",
                checkpoint={"failed": ["lead"], "attempts": 2},
            )
            base = store.load_schedule(schedule_id)
            assert base is not None
            store.transition_ready(
                schedule_id,
                ["worker:task-1"],
                checkpoint={"ready": ["worker:task-1"]},
                expected_event_count=base["event_count"],
            )
            with pytest.raises(StaleLeaseError, match="base changed"):
                store.transition_ready(
                    schedule_id,
                    ["cleanup"],
                    checkpoint={},
                    expected_event_count=base["event_count"],
                )
            matches = store.find_active_task("task-1")
            assert [(row["id"], row["node_id"]) for row in matches] == [
                (schedule_id, "worker:task-1")
            ]
            assert store.cancel_node(
                schedule_id,
                "worker:task-1",
                reason="operator-task-cancel",
                checkpoint={"cancelled": ["worker"]},
            )
            assert store.find_active_task("task-1") == []
            task_cancelled = store.restore(schedule_id)
            assert task_cancelled["schedule"]["status"] == "running"
            assert {node["node_id"]: node["state"] for node in task_cancelled["nodes"]}[
                "worker:task-1"
            ] == "cancelled"
            assert not store.cancel_node(
                schedule_id,
                "worker:task-1",
                reason="operator-task-cancel",
                checkpoint={"cancelled": ["worker"]},
            )
            store.cancel_schedule(
                schedule_id,
                reason="operator stop",
                cleanup_node_ids=["cleanup"],
                checkpoint={"cancelled": True, "cleanup": ["cleanup"]},
            )

            restored = store.restore(schedule_id)
            states = {node["node_id"]: node["state"] for node in restored["nodes"]}
            assert restored["schedule"]["graph"] == graph
            assert restored["schedule"]["status"] == "cancelled"
            assert states == {
                "cleanup": "ready",
                "lead": "failed",
                "worker:task-1": "cancelled",
            }
            assert len(store.list_schedules(session.id)) == 1
            assert store.list_active_schedules() == []


def test_pg0_deterministic_staged_campaign_wave_is_restart_idempotent(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("staged campaign wave")
            scheduler = DurableSchedulerStore(connection)
            campaigns = CampaignStore(connection)
            schedule_id = "2be4799f-df25-5d8f-9717-f8fb345899b1"
            graph = {"nodes": ["worker"]}
            checkpoint = {"worker": "ready"}

            assert (
                scheduler.create_schedule(
                    session.id,
                    graph,
                    checkpoint,
                    ["worker"],
                    ready_nodes=["worker"],
                    schedule_id=schedule_id,
                    initial_status="paused",
                )
                == schedule_id
            )
            assert [row["status"] for row in scheduler.list_active_schedules()] == ["paused"]
            assert (
                scheduler.create_schedule(
                    session.id,
                    graph,
                    checkpoint,
                    ["worker"],
                    ready_nodes=["worker"],
                    schedule_id=schedule_id,
                    initial_status="paused",
                )
                == schedule_id
            )

            campaigns.prepare(
                "campaign/staged",
                str(session.id),
                {"manifest": {"profile": "recommended"}},
                "sha256:staged",
            )
            first = campaigns.add_wave("campaign/staged", 1, "initial", schedule_id)
            second = campaigns.add_wave("campaign/staged", 1, "initial", schedule_id)
            assert first["schedule_id"] == second["schedule_id"]
            assert len(campaigns.list_waves("campaign/staged")) == 1
            assert scheduler.load_schedule(schedule_id)["status"] == "paused"


def test_pg0_exploration_critic_ledger_is_append_only_queryable_and_restart_safe(
    tmp_path: Path,
) -> None:
    decision = {
        "id": "critic:rejected-1",
        "campaign_id": "campaign/exploration-ledger",
        "hypothesis_id": "hypothesis:session-boundary",
        "verdict": "reject",
        "evidence_refs": ["evidence:recon-1"],
        "closest_known_technique_id": "catalogue:session-confusion",
        "meaningful_difference_claim": "No meaningful difference was established.",
        "alternative_explanation": "The response is explained by ordinary cache isolation.",
        "falsification_test": "Repeat in a fresh reviewed browser context.",
        "rationale": "Reject pending materially different evidence.",
        "confidence": "medium",
        "limitations": ["The fresh-context experiment has not run."],
        "causal_ambiguity": False,
        "high_risk_promotion": False,
    }
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("exploration critic ledger")
            store = CampaignStore(connection)
            store.prepare(
                decision["campaign_id"],
                str(session.id),
                {"profile": "exploration"},
                "sha256:exploration-ledger",
            )
            first = store.record_exploration_critic_decision(decision)
            replay = store.record_exploration_critic_decision(decision)
            assert first["decision_digest"] == replay["decision_digest"]
            with pytest.raises(ValueError, match="conflicts with different payload"):
                store.record_exploration_critic_decision({**decision, "verdict": "accept"})
            with pytest.raises(ValueError, match="unexpected fields"):
                store.record_exploration_critic_decision({**decision, "extra": "not allowed"})
            with pytest.raises(ValueError, match="flags must be booleans"):
                store.record_exploration_critic_decision({**decision, "causal_ambiguity": "false"})
            with pytest.raises(ValueError, match="evidence references"):
                store.record_exploration_critic_decision({**decision, "evidence_refs": []})
            with connection.transaction(), pytest.raises(Exception, match="append-only"):
                connection.execute(
                    """UPDATE merlin_storage.exploration_critic_decisions
                       SET verdict = 'accept' WHERE id = %s""",
                    (decision["id"],),
                )

            connection.execute(
                "DELETE FROM merlin_storage.assessment_campaigns WHERE id = %s",
                (decision["campaign_id"],),
            )

        with database.connect() as connection:
            restored = CampaignStore(connection).list_exploration_critic_decisions(
                decision["campaign_id"],
                hypothesis_id=decision["hypothesis_id"],
                verdict="reject",
            )
            assert len(restored) == 1
            assert restored[0]["decision"]["rationale"] == decision["rationale"]


def test_pg0_serializes_two_worker_claim_and_commit_on_one_connection(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("concurrent boundary")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id, {"nodes": ["a", "b"]}, {}, ["a", "b"], ready_nodes=["a", "b"]
            )

            def execute(node_id: str) -> str:
                lease = store.claim(
                    schedule_id,
                    node_id,
                    f"worker-{node_id}",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
                return store.commit_reducer(
                    lease.id,
                    lease.generation,
                    schema_id="result/v1",
                    result={"node": node_id},
                    checkpoint={"last": node_id},
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                digests = list(executor.map(execute, ["a", "b"]))

            assert len(set(digests)) == 2
            assert [node["state"] for node in store.list_nodes(schedule_id)] == [
                "completed",
                "completed",
            ]
            store.verify(schedule_id)


def test_pg0_expanded_worker_transcript_keeps_all_messages_and_enforces_session_scope(tmp_path):
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-worker-transcript")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = SessionRepository(connection)
            session = sessions.create("worker transcript")
            other = sessions.create("unrelated session")
            store = DurableSchedulerStore(connection)
            schedule = store.create_schedule(
                session.id,
                {},
                {},
                ["worker:assignment-1"],
                ready_nodes=["worker:assignment-1"],
            )
            lease = store.claim(
                schedule,
                "worker:assignment-1",
                "worker",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
            )
            messages = [{"role": "assistant", "content": f"message {i}"} for i in range(12)]
            store.commit_partial(
                lease.id,
                lease.generation,
                result={"worker_checkpoint": {"messages": messages, "action_history": []}},
            )
            rows = store.get_assignment_transcript_rows(session.id, "assignment-1")
            assert len(rows) == 1
            assert rows[0]["payload"]["worker_checkpoint"]["messages"] == messages
            assert store.get_assignment_transcript_rows(other.id, "assignment-1") == []
            assert store.get_assignment_transcript_rows(session.id, "missing") == []


@pytest.mark.asyncio
async def test_pg0_daemon_restart_keeps_recon_dormant_until_explicit_resume(tmp_path: Path) -> None:
    import asyncio

    class NoTools:
        def describe(self):
            return ()

        async def invoke(self, *args):
            raise AssertionError("restart must not invoke browser tools")

    class Provider:
        def __init__(self):
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta(text='{"finding":"done"}')
            yield ModelCompleted()

    assignments = tuple(
        WorkerAssignment(
            id=f"recon-{index}",
            objective="Inspect",
            profile_id="recon",
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=(),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for index in range(2)
    )
    data_dir = tmp_path / "restart-recon"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = DurableSchedulerStore(connection)
            creator = DurableAgentService(Provider(), NoTools(), store, model="fixture")
            schedule_ids = []
            for index in range(2):
                session = SessionRepository(connection).create(f"saved recon {index}")
                live = await creator.submit(str(session.id), assignments, start_immediately=False)
                schedule_ids.append(live.id)
            # A lease left by the previous daemon must also be fenced.
            ready_node = next(
                node["node_id"]
                for node in store.list_nodes(schedule_ids[0])
                if node["state"] == "ready"
            )
            store.claim(
                schedule_ids[0],
                ready_node,
                "old-daemon",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            store = DurableSchedulerStore(connection)
            provider = Provider()
            replacement = DurableAgentService(provider, NoTools(), store, model="fixture")
            assert await replacement.recover(resume_execution=False) == 2
            await asyncio.sleep(0)
            assert provider.requests == []
            assert all(store.load_schedule(key)["status"] == "paused" for key in schedule_ids)
            assert store.list_attempts(schedule_ids[0])[0]["state"] == "expired"
            assert all(live.execution is None for live in replacement._live.values())
            assert await replacement.recover() == 0
            assert provider.requests == []

            resumed = await replacement.resume_schedule(schedule_ids[0])
            assert resumed.execution is not None
            await asyncio.wait_for(resumed.execution, timeout=5)
            assert provider.requests
            assert store.load_schedule(schedule_ids[1])["status"] == "paused"
            assert store.list_attempts(schedule_ids[1]) == []
