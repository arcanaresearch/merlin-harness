import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
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
from merlin_agents.target_reobservation import (
    extract_target_reobservation_obligations,
    normalize_target_ui_transition,
    project_target_reobservations,
    target_reobservation_schedule_id,
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
    load_migrations,
)
from merlin_storage_postgres.effect_store import TargetEffectPermitLatchedError
from merlin_tools import ToolBroker
from psycopg.errors import ForeignKeyViolation
from psycopg.types.json import Jsonb


def _qualified(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _surface_transition_observation(
    *,
    campaign_id: str,
    session_id: str,
    target_id: str,
    target_session_id: str,
    task_id: str,
    call_id: str,
    error: bool = False,
) -> dict[str, object]:
    transition = {
        "schema": "merlin.target-ui-transition.v1",
        "material": True,
        "adapter_stale": error,
        "before_digest": "sha256:" + "1" * 64,
        "after_digest": "sha256:" + "2" * 64,
        "changed_dimensions": ["controls"],
        "added_controls": [{"role": "button", "name": "Continue", "frame_ref": "main"}],
        "removed_controls": [],
        "controls_truncated": False,
        "evidence_refs": ["browser-snapshot:transition"],
        "reobservation_recommended": True,
    }
    body: dict[str, object] = {
        "call_id": call_id,
        "reference": f"target-call:{call_id}",
        "assignment_id": task_id,
        "campaign_id": campaign_id,
        "session_id": session_id,
        "target_id": target_id,
        "target_session_id": target_session_id,
        "tool_name": "browser_chat_exchange",
        "is_error": error,
        "request_sha256": "3" * 64,
        "content_sha256": "4" * 64,
        "metadata_sha256": "5" * 64,
        "target_ui_transition": transition,
    }
    if error:
        body.update(
            target_effect_attempted=False,
            interaction_count=0,
            limit_interaction_count=0,
            target_call_budget_charge=0,
        )
    receipt = {**body, "receipt_sha256": _qualified(body).removeprefix("sha256:")}
    projection = "target_ui_transition_receipts" if error else "target_evidence_receipts"
    obligations = extract_target_reobservation_obligations(
        campaign_id=campaign_id,
        session_id=session_id,
        target_id=target_id,
        source_schedule_id="placeholder",
        source_wave=0,
        task_ids=(task_id,),
        evidence={task_id: {projection: {call_id: receipt}}},
    )
    assert len(obligations) == 1
    return obligations[0].observed_payload(campaign_id, target_id)


def _target_admission(
    *,
    campaign_id: str,
    session_id: str,
    schedule_id: str,
    target_id: str,
    assignment_id: str,
    target_session_id: str | None = None,
    strategy_id: str = "direct",
    generation_id: str | None = None,
    event_set_digest: str | None = None,
    start_decision_id: str | None = None,
    target_exchange_tool_ids: tuple[str, ...] = ("browser_chat_exchange",),
    target_effect_tool_ids: tuple[str, ...] | None = None,
) -> dict[str, object]:
    effect_tool_ids = (
        target_exchange_tool_ids if target_effect_tool_ids is None else target_effect_tool_ids
    )
    return {
        "schema": "merlin.target-admission.v1",
        "campaign_id": campaign_id,
        "session_id": session_id,
        "schedule_id": schedule_id,
        "node_id": f"worker:{assignment_id}",
        "target_id": target_id,
        "assignment_id": assignment_id,
        "target_session_id": target_session_id,
        "strategy_id": strategy_id,
        "reviewed_adapter_digest": "sha256:" + "8" * 64,
        "target_effect_tool_ids": effect_tool_ids,
        "target_exchange_tool_ids": target_exchange_tool_ids,
        "generation_id": generation_id,
        "event_set_digest": event_set_digest,
        "target_reobservation_start_decision_id": start_decision_id,
    }


def _persisted_worker_context(
    *,
    assignment_id: str,
    target_id: str,
    tool_ids: tuple[str, ...],
    strategy_id: str,
    strategy_contract: list[list[object]],
) -> dict[str, object]:
    values: dict[str, object] = {
        "assignment_id": assignment_id,
        "profile": {},
        "guidance": [],
        "skills": [],
        "tool_ids": list(tool_ids),
        "target": {"id": target_id},
        "authority_ids": [],
        "strategy_id": strategy_id,
        "strategy_contract": strategy_contract,
    }
    return {**values, "digest": _qualified(values)}


def _target_effect_intent(
    *,
    session_id: str,
    campaign_id: str,
    target_id: str,
    assignment_id: str,
    attempt_id: str,
    call_id: str,
    tool_name: str = "browser_chat_exchange",
) -> dict[str, object]:
    arguments = {"message": "bounded target probe"}
    return ToolEffectRecord(
        idempotency_key=f"effect/{attempt_id}/{call_id}",
        session_id=session_id,
        call_id=call_id,
        agent_id=assignment_id,
        attempt_id=attempt_id,
        campaign_id=campaign_id,
        target_id=target_id,
        tool_name=tool_name,
        effect="target_network",
        arguments_digest=_qualified(arguments),
        arguments_payload=arguments,
        state=ToolEffectState.INTENT,
    ).model_dump(mode="json")


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


def test_schedule_event_count_does_not_read_graph() -> None:
    class Connection:
        def execute(self, query, *args, **kwargs):
            normalized = " ".join(str(query).split())
            assert normalized == (
                "SELECT event_count FROM merlin_storage.scheduler_schedules WHERE id = %s"
            )
            assert args == (("schedule-1",),)
            return SimpleNamespace(fetchone=lambda: {"event_count": 7})

    assert DurableSchedulerStore(Connection()).schedule_event_count("schedule-1") == 7


def test_list_result_contents_projects_text_without_decoding_payload() -> None:
    class Connection:
        def execute(self, query, *args, **kwargs):
            normalized = " ".join(str(query).split())
            assert "COALESCE(payload->>'content', payload::text) AS content" in normalized
            assert "payload, payload_digest" not in normalized
            return SimpleNamespace(
                fetchall=lambda: [
                    {
                        "attempt_id": "attempt-1",
                        "schedule_id": "schedule-1",
                        "node_id": "worker:one",
                        "content": "result",
                    }
                ]
            )

    rows = DurableSchedulerStore(Connection()).list_result_contents("schedule-1")

    assert rows[0]["content"] == "result"


def test_list_result_progress_uses_scalar_checkpoint_fields() -> None:
    class Connection:
        def execute(self, query, *args, **kwargs):
            normalized = " ".join(str(query).split())
            assert "active_phase" in normalized
            assert "turns_completed" in normalized
            assert "action_count" in normalized
            assert "payload, payload_digest" not in normalized
            return SimpleNamespace(fetchall=lambda: [])

    assert DurableSchedulerStore(Connection()).list_result_progress("schedule-1") == []


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


def test_heartbeat_and_effect_adoption_share_schedule_before_attempt_lock_order(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-attempt-lock-order")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("scheduler lock order")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"nodes": ["worker"]},
                {"completed": []},
                ["worker"],
                ready_nodes=["worker"],
            )
            lease = store.claim(
                schedule_id,
                "worker",
                "agent-1",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )

        schedule_locked = Event()
        heartbeat_progress = Event()

        class ObservedConnection:
            def __init__(self, connection):
                self._connection = connection

            def __getattr__(self, name):
                return getattr(self._connection, name)

            def execute(self, query, *args, **kwargs):
                normalized = " ".join(str(query).split())
                if (
                    "FROM merlin_storage.scheduler_schedules" in normalized
                    and "FOR UPDATE" in normalized
                ):
                    heartbeat_progress.set()
                result = self._connection.execute(query, *args, **kwargs)
                if (
                    "FROM merlin_storage.scheduler_attempts AS attempt" in normalized
                    and "FOR UPDATE" in normalized
                ):
                    heartbeat_progress.set()
                return result

        def adopt_effect() -> str:
            with database.connect() as connection, connection.transaction():
                connection.execute("SET LOCAL deadlock_timeout = '100ms'")
                connection.execute(
                    "SELECT id FROM merlin_storage.scheduler_schedules WHERE id = %s FOR UPDATE",
                    (schedule_id,),
                )
                schedule_locked.set()
                assert heartbeat_progress.wait(3)
                connection.execute(
                    "SELECT id FROM merlin_storage.scheduler_attempts WHERE id = %s FOR UPDATE",
                    (lease.id,),
                )
            return "effect"

        def heartbeat() -> str:
            assert schedule_locked.wait(3)
            with database.connect() as connection:
                connection.execute("SET deadlock_timeout = '100ms'")
                DurableSchedulerStore(ObservedConnection(connection)).heartbeat(
                    lease.id,
                    lease.generation,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
                )
            return "heartbeat"

        with ThreadPoolExecutor(max_workers=2) as executor:
            effect = executor.submit(adopt_effect)
            settlement = executor.submit(heartbeat)
            assert {effect.result(timeout=5), settlement.result(timeout=5)} == {
                "effect",
                "heartbeat",
            }


def _prepare_target_transition_campaign(connection, session_id: str):
    campaign_id = "campaign/target-latch"
    target_id = "target-1"
    target_tasks = (
        f"{campaign_id}/wave-0/first",
        f"{campaign_id}/wave-0/already-leased",
        f"{campaign_id}/wave-0/late",
    )
    offline_task = f"{campaign_id}/wave-0/offline"
    assignments = [
        {
            "id": task_id,
            "target_profile_id": target_id,
            "tool_ids": ["browser_chat_exchange"],
            "strategy_id": "direct",
            "strategy_contract": [],
            "reviewed_adapter_digest": "sha256:" + "8" * 64,
        }
        for task_id in target_tasks
    ] + [
        {
            "id": offline_task,
            "target_profile_id": target_id,
            "tool_ids": ["offline_analysis"],
            "strategy_id": "direct",
            "strategy_contract": [],
        }
    ]
    store = DurableSchedulerStore(connection)
    schedule_id = store.create_schedule(
        session_id,
        {"assignments": assignments, "worker_contexts": {}},
        {"nodes": {}},
        [*(f"worker:{task_id}" for task_id in target_tasks), f"worker:{offline_task}"],
        ready_nodes=[
            *(f"worker:{task_id}" for task_id in target_tasks),
            f"worker:{offline_task}",
        ],
    )
    campaigns = CampaignStore(connection)
    campaigns.prepare(
        campaign_id,
        session_id,
        {"manifest": {"id": campaign_id, "target": {"id": target_id}}},
        "sha256:reviewed",
    )
    campaigns.begin_execution(campaign_id, "sha256:reviewed", schedule_id)
    return store, campaigns, schedule_id, campaign_id, target_id, target_tasks, offline_task


def _persist_legacy_transition_upgrade_case(connection, session_id: str, *, error: bool):
    store, campaigns, schedule_id, campaign_id, target_id, tasks, _offline = (
        _prepare_target_transition_campaign(connection, session_id)
    )
    task_id = tasks[0]
    node_id = f"worker:{task_id}"
    lease = store.claim(
        schedule_id,
        node_id,
        "legacy-transition-worker",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    transition = {
        "schema": "merlin.target-ui-transition.v1",
        "material": True,
        "adapter_stale": error,
        "before_digest": "sha256:" + "1" * 64,
        "after_digest": "sha256:" + "2" * 64,
        "changed_dimensions": ["controls"],
        "added_controls": [{"role": "button", "name": "Continue", "frame_ref": "main"}],
        "removed_controls": [],
        "controls_truncated": False,
        "evidence_refs": ["browser-snapshot:legacy-transition"],
        "reobservation_recommended": True,
    }
    normalized = normalize_target_ui_transition(transition)
    assert normalized is not None
    canonical_transition, transition_digest = normalized
    target_session_id = "browser:legacy-context"
    call_id = "legacy-error-call" if error else "legacy-success-call"
    tool_name = "browser_chat_exchange"
    arguments = {"message": "advance the reviewed target"}
    request_body = json.dumps(
        {"name": tool_name, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    content = "surface changed" if error else "level complete"
    metadata = {
        "target_session_id": target_session_id,
        "target_ui_transition": transition,
        "target_effect_attempted": not error,
        "interaction_count": 0 if error else 1,
        "limit_interaction_count": 0 if error else 1,
        "target_call_budget_charge": 0 if error else 1,
    }
    receipt_body = {
        "call_id": call_id,
        "tool_name": tool_name,
        "reference": f"target-call:{call_id}",
        "campaign_id": campaign_id,
        "assignment_id": task_id,
        "attempt_id": lease.id,
        "target_id": target_id,
        "target_session_id": target_session_id,
        "request_sha256": hashlib.sha256(request_body).hexdigest(),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "metadata_sha256": _qualified(metadata).removeprefix("sha256:"),
        "is_error": error,
        "acknowledged": not error,
        "target_ui_transition": transition,
    }
    receipt = {
        **receipt_body,
        "receipt_sha256": _qualified(receipt_body).removeprefix("sha256:"),
    }
    effect = ToolEffectRecord(
        idempotency_key=f"legacy-effect/{lease.id}/{call_id}",
        session_id=session_id,
        call_id=call_id,
        agent_id=task_id,
        attempt_id=lease.id,
        campaign_id=campaign_id,
        target_id=target_id,
        tool_name=tool_name,
        effect="target_network",
        arguments_digest=_qualified(arguments),
        arguments_payload=arguments,
        state=ToolEffectState.INTENT,
    )
    effects = ToolEffectRepository(connection)
    effects.begin(effect)
    result = {
        "content": content,
        "target_id": target_id,
        "target_session_id": target_session_id,
        "worker_checkpoint": {"target_evidence": [receipt]},
    }
    # This fixture represents a daemon that committed its terminal effect and
    # scheduler result before target-admission migration 097 existed. Use the
    # pre-097 schema directly: current repository methods intentionally consult
    # the permit/latch tables introduced by that migration.
    result_digest = _qualified(result).removeprefix("sha256:")
    with connection.transaction():
        updated_effect = connection.execute(
            """UPDATE merlin_storage.tool_effects
               SET state = 'acknowledged', result_content = %s,
                   result_is_error = %s, result_metadata = %s,
                   updated_at = now()
               WHERE idempotency_key = %s AND state = 'intent'
               RETURNING idempotency_key""",
            (content, error, Jsonb(metadata), effect.idempotency_key),
        ).fetchone()
        assert updated_effect == {"idempotency_key": effect.idempotency_key}
        connection.execute(
            """INSERT INTO merlin_storage.scheduler_results
                   (attempt_id, schedule_id, node_id, generation, schema_id,
                    payload, payload_digest)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                lease.id,
                schedule_id,
                node_id,
                lease.generation,
                "schema/partial-result" if error else "schema/result",
                Jsonb(result),
                result_digest,
            ),
        )
        updated_attempt = connection.execute(
            """UPDATE merlin_storage.scheduler_attempts
               SET state = %s, error = %s, completed_at = now(),
                   target_session_id = %s, failure_kind = %s
               WHERE id = %s AND generation = %s AND state = 'running'""",
            (
                "failed" if error else "completed",
                "target_surface_disrupted" if error else None,
                target_session_id,
                "unknown" if error else None,
                lease.id,
                lease.generation,
            ),
        )
        assert updated_attempt.rowcount == 1
        if error:
            updated_node = connection.execute(
                """UPDATE merlin_storage.scheduler_nodes
                   SET state = 'failed', failed_attempts = failed_attempts + 1
                   WHERE schedule_id = %s AND node_id = %s
                     AND lease_generation = %s AND state = 'leased'""",
                (schedule_id, node_id, lease.generation),
            )
        else:
            updated_node = connection.execute(
                """UPDATE merlin_storage.scheduler_nodes
                   SET state = 'completed', result_digest = %s
                   WHERE schedule_id = %s AND node_id = %s
                     AND lease_generation = %s AND state = 'leased'""",
                (result_digest, schedule_id, node_id, lease.generation),
            )
        assert updated_node.rowcount == 1
    old_event_digest = _qualified(
        [campaign_id, task_id, call_id, receipt["receipt_sha256"], transition_digest]
    )
    legacy_payload = {
        "kind": "target_surface_transition_observed",
        "schema": "merlin.target-surface-transition-observed.v1",
        "campaign_id": campaign_id,
        "session_id": session_id,
        "target_id": target_id,
        "obligation_id": "target-reobservation:" + old_event_digest.removeprefix("sha256:"),
        "event_digest": old_event_digest,
        "transition_digest": transition_digest,
        "source_schedule_id": schedule_id,
        "source_wave": 0,
        "source_task_id": task_id,
        "target_call_id": call_id,
        "target_receipt_sha256": receipt["receipt_sha256"],
        "receipt_projection": (
            "target_ui_transition_receipts" if error else "target_evidence_receipts"
        ),
        "target_ui_transition": canonical_transition,
    }
    campaigns.commit_lead_decision(
        campaign_id,
        0,
        "target-surface-transition-observed/" + old_event_digest.removeprefix("sha256:"),
        legacy_payload,
        (),
    )
    return {
        "store": store,
        "campaigns": campaigns,
        "schedule_id": schedule_id,
        "campaign_id": campaign_id,
        "target_id": target_id,
        "target_session_id": target_session_id,
        "task_id": task_id,
        "call_id": call_id,
        "receipt_sha256": receipt["receipt_sha256"],
        "transition_digest": transition_digest,
    }


def _journal_reviewed_refresh(
    campaigns: CampaignStore,
    *,
    campaign_id: str,
    session_id: str,
    target_id: str,
    target_session_id: str,
) -> dict[str, str]:
    rows = campaigns.list_lead(campaign_id)
    projection = project_target_reobservations(
        rows, campaign_id=campaign_id, session_id=session_id, target_id=target_id
    )
    generation = projection.current_generation
    assert generation is not None
    event_set_digest = _qualified({"event_digests": generation.event_digests})
    reobserve_schedule = target_reobservation_schedule_id(campaign_id, generation.generation_id, 1)
    start = {
        "kind": "target_reobservation_started",
        "schema": "merlin.target-reobservation-started.v1",
        "wave_kind": "objective_prerequisite",
        "campaign_id": campaign_id,
        "session_id": session_id,
        "target_id": target_id,
        "target_session_id": target_session_id,
        "generation_id": generation.generation_id,
        "obligation_id": generation.obligation_id,
        "event_digests": list(generation.event_digests),
        "event_set_digest": event_set_digest,
        "observation_cutoff_sequence": generation.observation_cutoff_sequence,
        "source_wave": generation.source_wave,
        "attempt": 1,
        "schedule_id": reobserve_schedule,
    }
    start_decision_id = (
        "target-reobservation-started/" + generation.generation_id.removeprefix("sha256:") + "/1"
    )
    campaigns.commit_lead_decision(
        campaign_id,
        generation.source_wave,
        start_decision_id,
        start,
        (),
    )
    evidence_refs = ["browser-snapshot:current"]
    settlement_id = "target-reobservation-settled/" + generation.generation_id.removeprefix(
        "sha256:"
    )
    settlement = {
        "kind": "target_reobservation_settled",
        "schema": "merlin.target-reobservation-settlement.v1",
        "campaign_id": campaign_id,
        "session_id": session_id,
        "target_id": target_id,
        "target_session_id": target_session_id,
        "generation_id": generation.generation_id,
        "obligation_id": generation.obligation_id,
        "event_digests": list(generation.event_digests),
        "event_set_digest": event_set_digest,
        "event_count": len(generation.event_digests),
        "observation_cutoff_sequence": generation.observation_cutoff_sequence,
        "source_wave": generation.source_wave,
        "status": "refreshed",
        "attempt": 1,
        "schedule_id": reobserve_schedule,
        "settlement_id": settlement_id,
        "current_surface_digest": "sha256:" + "6" * 64,
        "surface_evidence_digest": _qualified({"evidence_refs": evidence_refs}),
        "surface_evidence_refs": evidence_refs,
        "recon_handoff_id": "handoff-current",
        "compiled_adapter_digest": "sha256:" + "7" * 64,
        "reviewed_adapter_digest": "sha256:" + "8" * 64,
        "result_reference": "task-result:reobserve",
        "result_digest": "sha256:" + "9" * 64,
    }
    campaigns.commit_lead_decision(
        campaign_id, generation.source_wave, settlement_id, settlement, ()
    )
    lead_payload = {
        "schema": "merlin.campaign-lead-decision.v1",
        "campaign_id": campaign_id,
        "wave": generation.source_wave,
        "source_message_ids": (),
        "summary": "continue from the refreshed target surface",
        "coverage_complete": False,
        "finish_current_work": False,
        "followups": [],
        "validation_finding_ids": [],
        "actions": [],
        "plan_update": None,
        "automation_requests": [],
        "usage": {},
        "target_reobservation_generations": [
            {
                "generation_id": generation.generation_id,
                "settlement_id": settlement_id,
                "target_session_id": target_session_id,
            }
        ],
    }
    identity = json.dumps(
        {
            "campaign_id": campaign_id,
            "wave": generation.source_wave,
            "source_message_ids": (),
            "payload": lead_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    lead_decision_id = "lead-decision:" + hashlib.sha256(identity.encode()).hexdigest()
    campaigns.commit_lead_decision(
        campaign_id,
        generation.source_wave,
        lead_decision_id,
        lead_payload,
        (),
    )
    return {
        "generation_id": generation.generation_id,
        "event_set_digest": event_set_digest,
        "settlement_id": settlement_id,
        "lead_decision_id": lead_decision_id,
        "reobserve_schedule_id": reobserve_schedule,
        "start_decision_id": start_decision_id,
        "current_surface_digest": settlement["current_surface_digest"],
        "compiled_adapter_digest": settlement["compiled_adapter_digest"],
        "reviewed_adapter_digest": settlement["reviewed_adapter_digest"],
    }


@pytest.mark.parametrize("error", (False, True), ids=("success", "error"))
def test_legacy_transition_upgrade_adopts_one_exact_durable_session_without_replay(
    tmp_path: Path, error: bool
) -> None:
    data_dir = tmp_path / f"postgres-legacy-{error}"
    migrations = load_migrations()
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply(migrations[:96])
            session = SessionRepository(connection).create("legacy transition adoption")
            case = _persist_legacy_transition_upgrade_case(connection, str(session.id), error=error)
            session_id = str(session.id)
            before_effects = connection.execute(
                "SELECT count(*) AS count FROM merlin_storage.tool_effects"
            ).fetchone()["count"]
            before_results = connection.execute(
                "SELECT count(*) AS count FROM merlin_storage.scheduler_results"
            ).fetchone()["count"]

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply(migrations[96:])
            store = DurableSchedulerStore(connection)
            campaigns = CampaignStore(connection)
            upgraded = store.upgrade_legacy_target_admission(
                str(case["campaign_id"]), session_id, str(case["target_id"])
            )

            assert upgraded == {
                "schema": "merlin.target-admission-legacy-upgrade.v1",
                "status": "upgraded",
                "scanned_count": 1,
                "upgraded_count": 1,
                "unresolved_count": 0,
                "overflow": False,
            }
            lead = campaigns.list_lead(str(case["campaign_id"]))
            assert [row["payload"]["schema"] for row in lead] == [
                "merlin.target-surface-transition-observed.v1",
                "merlin.target-surface-transition-observed.v2",
            ]
            v2 = lead[-1]["payload"]
            assert v2["target_session_id"] == case["target_session_id"]
            assert v2["event_digest"] == _qualified(
                [
                    case["campaign_id"],
                    case["target_session_id"],
                    case["task_id"],
                    case["call_id"],
                    case["receipt_sha256"],
                    case["transition_digest"],
                ]
            )
            assert (
                project_target_reobservations(
                    lead,
                    campaign_id=str(case["campaign_id"]),
                    session_id=session_id,
                    target_id=str(case["target_id"]),
                )
                .pending[0]
                .event_digest
                == v2["event_digest"]
            )
            current = store.current_target_admission_latches(
                str(case["campaign_id"]), session_id, str(case["target_id"])
            )
            assert current[0]["target_session_id"] == case["target_session_id"]

            replay = store.upgrade_legacy_target_admission(
                str(case["campaign_id"]), session_id, str(case["target_id"])
            )
            assert replay["status"] == "clear"
            assert replay["upgraded_count"] == replay["unresolved_count"] == 0
            assert (
                connection.execute(
                    "SELECT count(*) AS count FROM merlin_storage.tool_effects"
                ).fetchone()["count"]
                == before_effects
            )
            assert (
                connection.execute(
                    "SELECT count(*) AS count FROM merlin_storage.scheduler_results"
                ).fetchone()["count"]
                == before_results
            )
            assert len(campaigns.list_lead(str(case["campaign_id"]))) == 2


def test_legacy_transition_upgrade_ambiguous_session_stays_globally_latched(
    tmp_path: Path,
) -> None:
    migrations = load_migrations()
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-legacy-ambiguous")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply(migrations[:96])
            session = SessionRepository(connection).create("ambiguous legacy transition")
            case = _persist_legacy_transition_upgrade_case(connection, str(session.id), error=False)
            connection.execute(
                """UPDATE merlin_storage.tool_effects
                   SET result_metadata = jsonb_set(
                       result_metadata, '{target_session_id}', '"browser:other-context"'
                   )
                   WHERE call_id = %s""",
                (case["call_id"],),
            )
            legacy = case["campaigns"].list_lead(str(case["campaign_id"]))[0]["payload"]
            forged_event_digest = _qualified(
                [
                    case["campaign_id"],
                    case["target_session_id"],
                    case["task_id"],
                    case["call_id"],
                    case["receipt_sha256"],
                    case["transition_digest"],
                ]
            )
            forged_v2 = {
                **legacy,
                "schema": "merlin.target-surface-transition-observed.v2",
                "target_session_id": case["target_session_id"],
                "event_digest": forged_event_digest,
                "obligation_id": "target-reobservation:"
                + forged_event_digest.removeprefix("sha256:"),
            }
            case["campaigns"].commit_lead_decision(
                str(case["campaign_id"]),
                0,
                "target-surface-transition-observed/" + forged_event_digest.removeprefix("sha256:"),
                forged_v2,
                (),
            )
            MigrationRunner(connection).apply(migrations[96:])
            store = case["store"]
            assert isinstance(store, DurableSchedulerStore)

            blocked = store.upgrade_legacy_target_admission(
                str(case["campaign_id"]), str(session.id), str(case["target_id"])
            )

            assert blocked == {
                "schema": "merlin.target-admission-legacy-upgrade.v1",
                "status": "blocked",
                "scanned_count": 1,
                "upgraded_count": 0,
                "unresolved_count": 1,
                "overflow": False,
            }
            assert [
                row["payload"]["schema"]
                for row in case["campaigns"].list_lead(str(case["campaign_id"]))
            ] == [
                "merlin.target-surface-transition-observed.v1",
                "merlin.target-surface-transition-observed.v2",
            ]
            head = connection.execute(
                """SELECT state, transition_count, latched_session_count
                   FROM merlin_storage.target_admission_latches
                   WHERE campaign_id = %s AND target_id = %s
                     AND target_session_id = ''""",
                (case["campaign_id"], case["target_id"]),
            ).fetchone()
            assert head == {
                "state": "latched",
                "transition_count": 2,
                "latched_session_count": 2,
            }


def test_legacy_transition_upgrade_overflow_remains_bounded_and_globally_latched(
    tmp_path: Path,
) -> None:
    migrations = load_migrations()
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-legacy-overflow")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply(migrations[:96])
            session = SessionRepository(connection).create("legacy transition overflow")
            store, campaigns, _schedule, campaign_id, target_id, _tasks, _offline = (
                _prepare_target_transition_campaign(connection, str(session.id))
            )
            for index in range(257):
                event_digest = "sha256:" + f"{index:064x}"
                campaigns.commit_lead_decision(
                    campaign_id,
                    0,
                    "target-surface-transition-observed/" + event_digest.removeprefix("sha256:"),
                    {
                        "kind": "target_surface_transition_observed",
                        "schema": "merlin.target-surface-transition-observed.v1",
                        "campaign_id": campaign_id,
                        "session_id": str(session.id),
                        "target_id": target_id,
                        "event_digest": event_digest,
                    },
                    (),
                )

            MigrationRunner(connection).apply(migrations[96:])
            blocked = store.upgrade_legacy_target_admission(campaign_id, str(session.id), target_id)

            assert blocked == {
                "schema": "merlin.target-admission-legacy-upgrade.v1",
                "status": "blocked",
                "scanned_count": 256,
                "upgraded_count": 0,
                "unresolved_count": 256,
                "overflow": True,
            }
            head = connection.execute(
                """SELECT state, transition_count, latched_session_count
                   FROM merlin_storage.target_admission_latches
                   WHERE campaign_id = %s AND target_id = %s
                     AND target_session_id = ''""",
                (campaign_id, target_id),
            ).fetchone()
            assert head["state"] == "latched"
            assert head["transition_count"] >= 257
            assert head["latched_session_count"] >= 1


def test_pg0_transition_commit_latches_claim_and_already_leased_effect_atomically(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-target-latch")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("target transition admission")
            (
                store,
                campaigns,
                schedule_id,
                campaign_id,
                target_id,
                target_tasks,
                offline_task,
            ) = _prepare_target_transition_campaign(connection, str(session.id))
            admissions = {
                task_id: _target_admission(
                    campaign_id=campaign_id,
                    session_id=str(session.id),
                    schedule_id=schedule_id,
                    target_id=target_id,
                    assignment_id=task_id,
                )
                for task_id in target_tasks
            }
            first = store.claim(
                schedule_id,
                f"worker:{target_tasks[0]}",
                "worker:first",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=admissions[target_tasks[0]],
            )
            already_leased = store.claim(
                schedule_id,
                f"worker:{target_tasks[1]}",
                "worker:already-leased",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            with pytest.raises(
                scheduler_store_module.TargetAdmissionLatchedError,
                match="durable lane",
            ):
                store.claim(
                    schedule_id,
                    f"worker:{target_tasks[2]}",
                    "worker:parallel",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                    target_admission=admissions[target_tasks[2]],
                )
            transition = _surface_transition_observation(
                campaign_id=campaign_id,
                session_id=str(session.id),
                target_id=target_id,
                target_session_id="browser:context-1",
                task_id=target_tasks[0],
                call_id="call-success-transition",
            )

            store.commit_reducer(
                first.id,
                first.generation,
                schema_id="schema/result",
                result={"content": "level complete"},
                checkpoint={"nodes": {}},
                target_session_id="browser:context-1",
                target_surface_transition=transition,
            )

            journal = campaigns.list_lead(campaign_id)
            assert [row["payload"]["event_digest"] for row in journal] == [
                transition["event_digest"]
            ]
            with pytest.raises(
                scheduler_store_module.TargetAdmissionLatchedError,
                match="requires passive re-observation",
            ):
                store.claim(
                    schedule_id,
                    f"worker:{target_tasks[2]}",
                    "worker:late",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                    target_admission=admissions[target_tasks[2]],
                )
            with pytest.raises(scheduler_store_module.TargetAdmissionLatchedError):
                store.final_target_effect_admitted(
                    schedule_id,
                    f"worker:{target_tasks[1]}",
                    already_leased.id,
                    already_leased.generation,
                    tool_name="browser_chat_exchange",
                    call_id="call-legacy-final-effect",
                    target_admission=admissions[target_tasks[1]],
                    effect_intent=_target_effect_intent(
                        session_id=str(session.id),
                        campaign_id=campaign_id,
                        target_id=target_id,
                        assignment_id=target_tasks[1],
                        attempt_id=already_leased.id,
                        call_id="call-legacy-final-effect",
                    ),
                )
            offline = store.claim(
                schedule_id,
                f"worker:{offline_task}",
                "worker:offline",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            assert offline.node_id == f"worker:{offline_task}"
            assert not any(
                row["node_id"] == f"worker:{target_tasks[2]}"
                for row in store.list_attempts(schedule_id)
            )


def test_pg0_transition_error_rolls_back_result_and_latch_together(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-transition-error")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("target transition error")
            store, campaigns, schedule_id, campaign_id, target_id, tasks, _offline = (
                _prepare_target_transition_campaign(connection, str(session.id))
            )
            admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=schedule_id,
                target_id=target_id,
                assignment_id=tasks[0],
            )
            lease = store.claim(
                schedule_id,
                f"worker:{tasks[0]}",
                "worker:first",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=admission,
            )
            transition = _surface_transition_observation(
                campaign_id=campaign_id,
                session_id=str(session.id),
                target_id=target_id,
                target_session_id="browser:context-1",
                task_id=tasks[0],
                call_id="call-error-transition",
                error=True,
            )
            forged = {**transition, "event_digest": "sha256:" + "f" * 64}
            terminal_result = {
                "content": "surface changed before delivery",
                "error": "target_surface_disrupted",
                "worker_checkpoint": {"target_evidence": []},
            }
            with pytest.raises(ValueError, match="not canonical"):
                store.settle_attempt(
                    lease.id,
                    lease.generation,
                    "failed",
                    reason="target_surface_disrupted",
                    checkpoint={"nodes": {}},
                    target_session_id="browser:context-1",
                    failure_kind="unknown",
                    terminal_result=terminal_result,
                    target_surface_transition=forged,
                )
            assert campaigns.list_lead(campaign_id) == []
            assert store.list_results(schedule_id) == []
            assert store.list_attempts(schedule_id)[0]["state"] == "running"

            store.settle_attempt(
                lease.id,
                lease.generation,
                "failed",
                reason="target_surface_disrupted",
                checkpoint={"nodes": {}},
                target_session_id="browser:context-1",
                failure_kind="unknown",
                terminal_result=terminal_result,
                target_surface_transition=transition,
            )
            assert (
                campaigns.list_lead(campaign_id)[0]["payload"]["event_digest"]
                == transition["event_digest"]
            )
            assert store.list_results(schedule_id)[0]["payload"] == terminal_result
            assert store.list_attempts(schedule_id)[0]["state"] == "failed"


@pytest.mark.parametrize("lifecycle_action", ("expire", "pause"))
def test_pg0_expired_target_lane_requires_exact_effect_recovery(
    tmp_path: Path,
    lifecycle_action: str,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-target-lane-recovery")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("target lane recovery")
            store, _campaigns, schedule_id, campaign_id, target_id, tasks, _offline = (
                _prepare_target_transition_campaign(connection, str(session.id))
            )
            admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=schedule_id,
                target_id=target_id,
                assignment_id=tasks[0],
            )
            first = store.claim(
                schedule_id,
                f"worker:{tasks[0]}",
                "worker:first",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=admission,
            )
            if lifecycle_action == "expire":
                connection.execute(
                    """UPDATE merlin_storage.scheduler_attempts
                       SET lease_expires_at = now() - interval '1 second'
                       WHERE id = %s""",
                    (first.id,),
                )
                connection.commit()
                store.expire(first.id, first.generation)
            else:
                store.pause_schedule(schedule_id, reason="operator pause")
                store.resume_schedule(schedule_id)

            with pytest.raises(
                scheduler_store_module.TargetLaneRecoveryRequiredError,
                match="reconciliation",
            ):
                store.claim(
                    schedule_id,
                    f"worker:{tasks[1]}",
                    "worker:sibling",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                    target_admission=_target_admission(
                        campaign_id=campaign_id,
                        session_id=str(session.id),
                        schedule_id=schedule_id,
                        target_id=target_id,
                        assignment_id=tasks[1],
                    ),
                )

            replacement = store.claim(
                schedule_id,
                f"worker:{tasks[0]}",
                "worker:replacement",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=admission,
            )
            assert replacement.replaced_attempt_id == first.id
            assert replacement.lane_recovery_required is True
            physical_admission = {
                **admission,
                "target_session_id": "browser:context-1",
            }
            with pytest.raises(scheduler_store_module.TargetAdmissionLatchedError):
                store.final_target_effect_admitted(
                    schedule_id,
                    f"worker:{tasks[0]}",
                    replacement.id,
                    replacement.generation,
                    tool_name="browser_chat_exchange",
                    call_id="call-before-recovery",
                    target_admission=physical_admission,
                    effect_intent=_target_effect_intent(
                        session_id=str(session.id),
                        campaign_id=campaign_id,
                        target_id=target_id,
                        assignment_id=tasks[0],
                        attempt_id=replacement.id,
                        call_id="call-before-recovery",
                    ),
                )
            recovery_body = {
                "schema": "merlin.target-lane-recovery.v1",
                "prior_attempt_id": first.id,
                "effect_ids": [],
                "outcome": "no_material_transition",
            }
            store.resolve_target_lane_recovery(
                schedule_id,
                f"worker:{tasks[0]}",
                replacement.id,
                replacement.generation,
                target_admission=admission,
                recovery={
                    **recovery_body,
                    "recovery_digest": _qualified(recovery_body),
                },
            )
            store.final_target_effect_admitted(
                schedule_id,
                f"worker:{tasks[0]}",
                replacement.id,
                replacement.generation,
                tool_name="browser_chat_exchange",
                call_id="call-after-recovery",
                target_admission=physical_admission,
                effect_intent=_target_effect_intent(
                    session_id=str(session.id),
                    campaign_id=campaign_id,
                    target_id=target_id,
                    assignment_id=tasks[0],
                    attempt_id=replacement.id,
                    call_id="call-after-recovery",
                ),
            )


@pytest.mark.asyncio
async def test_pg0_target_effect_permit_adopts_finishes_and_releases_lane(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-target-permit")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("target effect permit")
            store, _campaigns, schedule_id, campaign_id, target_id, tasks, _offline = (
                _prepare_target_transition_campaign(connection, str(session.id))
            )
            admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=schedule_id,
                target_id=target_id,
                assignment_id=tasks[0],
            )
            lease = store.claim(
                schedule_id,
                f"worker:{tasks[0]}",
                "worker:permit",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=admission,
            )
            intent = ToolEffectRecord.model_validate(
                _target_effect_intent(
                    session_id=str(session.id),
                    campaign_id=campaign_id,
                    target_id=target_id,
                    assignment_id=tasks[0],
                    attempt_id=lease.id,
                    call_id="call-permitted",
                )
            )
            effect_admission = {**admission, "target_session_id": "browser:permit"}
            permit_digest = store.final_target_effect_admitted(
                schedule_id,
                f"worker:{tasks[0]}",
                lease.id,
                lease.generation,
                tool_name=intent.tool_name,
                call_id=intent.call_id,
                target_admission=effect_admission,
                effect_intent=intent.model_dump(mode="json"),
            )
            effects = PostgresToolEffectStore(ToolEffectRepository(connection))
            adopted, first_adoption = await effects.adopt_permitted(intent, permit_digest)
            assert first_adoption is True
            replayed, second_adoption = await effects.adopt_permitted(intent, permit_digest)
            assert replayed == adopted
            assert second_adoption is False
            with pytest.raises(KeyError):
                await effects.adopt_permitted(intent, "sha256:" + "f" * 64)
            await effects.finish(
                adopted.model_copy(
                    update={
                        "state": ToolEffectState.ACKNOWLEDGED,
                        "result_content": "target replied",
                        "result_is_error": False,
                        "result_metadata": {"target_session_id": "browser:permit"},
                    }
                )
            )
            permit = connection.execute(
                """SELECT state FROM merlin_storage.target_effect_permits
                   WHERE permit_digest = %s""",
                (permit_digest,),
            ).fetchone()
            assert permit == {"state": "terminal"}
            store.commit_reducer(
                lease.id,
                lease.generation,
                schema_id="schema/result",
                result={"content": "target replied"},
                checkpoint={"nodes": {}},
                target_session_id="browser:permit",
            )
            sibling = store.claim(
                schedule_id,
                f"worker:{tasks[1]}",
                "worker:sibling",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=_target_admission(
                    campaign_id=campaign_id,
                    session_id=str(session.id),
                    schedule_id=schedule_id,
                    target_id=target_id,
                    assignment_id=tasks[1],
                ),
            )
            assert sibling.node_id == f"worker:{tasks[1]}"


@pytest.mark.asyncio
async def test_pg0_completed_retry_releases_aggregate_lane_with_prior_effect(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-target-retry-lineage")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("completed target retry lineage")
            store, _campaigns, schedule_id, campaign_id, target_id, tasks, _offline = (
                _prepare_target_transition_campaign(connection, str(session.id))
            )
            node_id = f"worker:{tasks[0]}"
            admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=schedule_id,
                target_id=target_id,
                assignment_id=tasks[0],
            )
            first = store.claim(
                schedule_id,
                node_id,
                "worker:first",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=admission,
            )
            physical_session_id = "api:retry-lineage"
            intent = ToolEffectRecord.model_validate(
                _target_effect_intent(
                    session_id=str(session.id),
                    campaign_id=campaign_id,
                    target_id=target_id,
                    assignment_id=tasks[0],
                    attempt_id=first.id,
                    call_id="call-before-retry",
                )
            )
            permit_digest = store.final_target_effect_admitted(
                schedule_id,
                node_id,
                first.id,
                first.generation,
                tool_name=intent.tool_name,
                call_id=intent.call_id,
                target_admission={**admission, "target_session_id": physical_session_id},
                effect_intent=intent.model_dump(mode="json"),
            )
            effects = PostgresToolEffectStore(ToolEffectRepository(connection))
            adopted, _ = await effects.adopt_permitted(intent, permit_digest)
            await effects.finish(
                adopted.model_copy(
                    update={
                        "state": ToolEffectState.ACKNOWLEDGED,
                        "result_content": "target replied",
                        "result_is_error": False,
                        "result_metadata": {"target_session_id": physical_session_id},
                    }
                )
            )
            store.settle_attempt(
                first.id,
                first.generation,
                "failed",
                reason="worker failed after an acknowledged target effect",
                checkpoint={"nodes": {}},
            )
            current = store.load_schedule(schedule_id)
            assert current is not None
            store.retry_failed(
                schedule_id,
                node_id,
                checkpoint={"nodes": {}},
                expected_event_count=current["event_count"],
            )
            replacement = store.claim(
                schedule_id,
                node_id,
                "worker:replacement",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=admission,
            )
            store.commit_reducer(
                replacement.id,
                replacement.generation,
                schema_id="schema/result",
                result={"content": "replacement completed"},
                checkpoint={"nodes": {}},
            )
            sibling = store.claim(
                schedule_id,
                f"worker:{tasks[1]}",
                "worker:sibling",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=_target_admission(
                    campaign_id=campaign_id,
                    session_id=str(session.id),
                    schedule_id=schedule_id,
                    target_id=target_id,
                    assignment_id=tasks[1],
                ),
            )
            assert sibling.node_id == f"worker:{tasks[1]}"


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_mode", ("sibling", "replacement"))
async def test_pg0_expired_nonmaterial_target_effect_allows_exact_recovery(
    tmp_path: Path, recovery_mode: str
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-nonmaterial-recovery")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("nonmaterial target recovery")
            store, _campaigns, schedule_id, campaign_id, target_id, tasks, _offline = (
                _prepare_target_transition_campaign(connection, str(session.id))
            )
            node_id = f"worker:{tasks[0]}"
            admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=schedule_id,
                target_id=target_id,
                assignment_id=tasks[0],
            )
            first = store.claim(
                schedule_id,
                node_id,
                "worker:first",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=admission,
            )
            physical_session_id = "api:expired-nonmaterial"
            recovery_admission = {**admission, "target_session_id": physical_session_id}
            intent = ToolEffectRecord.model_validate(
                _target_effect_intent(
                    session_id=str(session.id),
                    campaign_id=campaign_id,
                    target_id=target_id,
                    assignment_id=tasks[0],
                    attempt_id=first.id,
                    call_id="call-before-expiry",
                )
            )
            permit_digest = store.final_target_effect_admitted(
                schedule_id,
                node_id,
                first.id,
                first.generation,
                tool_name=intent.tool_name,
                call_id=intent.call_id,
                target_admission=recovery_admission,
                effect_intent=intent.model_dump(mode="json"),
            )
            effects = PostgresToolEffectStore(ToolEffectRepository(connection))
            adopted, _ = await effects.adopt_permitted(intent, permit_digest)
            await effects.finish(
                adopted.model_copy(
                    update={
                        "state": ToolEffectState.ACKNOWLEDGED,
                        "result_content": "target replied before expiry",
                        "result_is_error": False,
                        "result_metadata": {"target_session_id": physical_session_id},
                    }
                )
            )
            connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET lease_expires_at = now() - interval '1 second' WHERE id = %s""",
                (first.id,),
            )
            connection.commit()
            store.expire(first.id, first.generation)
            connection.execute(
                """UPDATE merlin_storage.target_admission_latches
                   SET lane_schedule_id = %s, lane_node_id = %s,
                       lane_assignment_id = %s, lane_attempt_id = %s,
                       lane_generation = %s, lane_recovery_required = true,
                       lane_recovery_root_attempt_id = %s
                   WHERE campaign_id = %s AND target_id = %s AND target_session_id = %s""",
                (
                    schedule_id,
                    node_id,
                    tasks[0],
                    first.id,
                    first.generation,
                    first.id,
                    campaign_id,
                    target_id,
                    physical_session_id,
                ),
            )
            connection.commit()
            if recovery_mode == "replacement":
                replacement = store.claim(
                    schedule_id,
                    node_id,
                    "worker:replacement",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                    target_admission=recovery_admission,
                )
                assert replacement.replaced_attempt_id == first.id
                assert replacement.lane_recovery_required is True
                recovery_body = {
                    "schema": "merlin.target-lane-recovery.v1",
                    "prior_attempt_id": first.id,
                    "effect_ids": [intent.idempotency_key],
                    "outcome": "no_material_transition",
                }
                store.resolve_target_lane_recovery(
                    schedule_id,
                    node_id,
                    replacement.id,
                    replacement.generation,
                    target_admission=recovery_admission,
                    recovery={
                        **recovery_body,
                        "recovery_digest": _qualified(recovery_body),
                    },
                )
                store.commit_reducer(
                    replacement.id,
                    replacement.generation,
                    schema_id="schema/result",
                    result={"content": "replacement completed"},
                    checkpoint={"nodes": {}},
                    target_session_id=physical_session_id,
                )
            sibling = store.claim(
                schedule_id,
                f"worker:{tasks[1]}",
                "worker:sibling",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=_target_admission(
                    campaign_id=campaign_id,
                    session_id=str(session.id),
                    schedule_id=schedule_id,
                    target_id=target_id,
                    assignment_id=tasks[1],
                    target_session_id=physical_session_id,
                ),
            )
            assert sibling.node_id == f"worker:{tasks[1]}"


@pytest.mark.asyncio
async def test_pg0_target_effect_reuses_quiescent_physical_session_latch(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-target-session-reuse")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("reuse physical target session")
            store, _campaigns, schedule_id, campaign_id, target_id, tasks, _offline = (
                _prepare_target_transition_campaign(connection, str(session.id))
            )
            effects = PostgresToolEffectStore(ToolEffectRepository(connection))
            physical_session_id = "browser:reused-session"

            first_admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=schedule_id,
                target_id=target_id,
                assignment_id=tasks[0],
            )
            first_lease = store.claim(
                schedule_id,
                f"worker:{tasks[0]}",
                "worker:first-session-user",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=first_admission,
            )
            first_intent = ToolEffectRecord.model_validate(
                _target_effect_intent(
                    session_id=str(session.id),
                    campaign_id=campaign_id,
                    target_id=target_id,
                    assignment_id=tasks[0],
                    attempt_id=first_lease.id,
                    call_id="call-first-session-use",
                )
            )
            first_permit = store.final_target_effect_admitted(
                schedule_id,
                f"worker:{tasks[0]}",
                first_lease.id,
                first_lease.generation,
                tool_name=first_intent.tool_name,
                call_id=first_intent.call_id,
                target_admission={
                    **first_admission,
                    "target_session_id": physical_session_id,
                },
                effect_intent=first_intent.model_dump(mode="json"),
            )
            adopted, adopted_now = await effects.adopt_permitted(first_intent, first_permit)
            assert adopted_now is True
            await effects.finish(
                adopted.model_copy(
                    update={
                        "state": ToolEffectState.ACKNOWLEDGED,
                        "result_content": "first reply",
                        "result_is_error": False,
                        "result_metadata": {"target_session_id": physical_session_id},
                    }
                )
            )
            store.commit_reducer(
                first_lease.id,
                first_lease.generation,
                schema_id="schema/result",
                result={"content": "first reply"},
                checkpoint={"nodes": {}},
                target_session_id=physical_session_id,
            )

            physical_latch = connection.execute(
                """SELECT state, lane_attempt_id
                   FROM merlin_storage.target_admission_latches
                   WHERE campaign_id = %s AND target_id = %s
                     AND target_session_id = %s""",
                (campaign_id, target_id, physical_session_id),
            ).fetchone()
            assert physical_latch == {"state": "open", "lane_attempt_id": None}

            second_admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=schedule_id,
                target_id=target_id,
                assignment_id=tasks[1],
            )
            second_lease = store.claim(
                schedule_id,
                f"worker:{tasks[1]}",
                "worker:second-session-user",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=second_admission,
            )
            aggregate_lane = connection.execute(
                """SELECT lane_attempt_id
                   FROM merlin_storage.target_admission_latches
                   WHERE campaign_id = %s AND target_id = %s
                     AND target_session_id = ''""",
                (campaign_id, target_id),
            ).fetchone()
            assert str(aggregate_lane["lane_attempt_id"]) == second_lease.id

            second_intent = ToolEffectRecord.model_validate(
                _target_effect_intent(
                    session_id=str(session.id),
                    campaign_id=campaign_id,
                    target_id=target_id,
                    assignment_id=tasks[1],
                    attempt_id=second_lease.id,
                    call_id="call-second-session-use",
                )
            )
            second_permit = store.final_target_effect_admitted(
                schedule_id,
                f"worker:{tasks[1]}",
                second_lease.id,
                second_lease.generation,
                tool_name=second_intent.tool_name,
                call_id=second_intent.call_id,
                target_admission={
                    **second_admission,
                    "target_session_id": physical_session_id,
                },
                effect_intent=second_intent.model_dump(mode="json"),
            )
            rebound_latch = connection.execute(
                """SELECT lane_attempt_id
                   FROM merlin_storage.target_admission_latches
                   WHERE campaign_id = %s AND target_id = %s
                     AND target_session_id = %s""",
                (campaign_id, target_id, physical_session_id),
            ).fetchone()
            assert str(rebound_latch["lane_attempt_id"]) == second_lease.id
            _, adopted_now = await effects.adopt_permitted(second_intent, second_permit)
            assert adopted_now is True


@pytest.mark.asyncio
async def test_pg0_cancel_revokes_undispatched_target_permit_and_releases_lane(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-target-cancel")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("cancel target permit")
            store, _campaigns, schedule_id, campaign_id, target_id, tasks, _offline = (
                _prepare_target_transition_campaign(connection, str(session.id))
            )
            admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=schedule_id,
                target_id=target_id,
                assignment_id=tasks[0],
            )
            lease = store.claim(
                schedule_id,
                f"worker:{tasks[0]}",
                "worker:cancel",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=admission,
            )
            intent = _target_effect_intent(
                session_id=str(session.id),
                campaign_id=campaign_id,
                target_id=target_id,
                assignment_id=tasks[0],
                attempt_id=lease.id,
                call_id="call-cancelled-before-dispatch",
            )
            effect_admission = {**admission, "target_session_id": "browser:cancel"}
            permit_digest = store.final_target_effect_admitted(
                schedule_id,
                f"worker:{tasks[0]}",
                lease.id,
                lease.generation,
                tool_name="browser_chat_exchange",
                call_id="call-cancelled-before-dispatch",
                target_admission=effect_admission,
                effect_intent=intent,
            )
            assert store.cancel_node(
                schedule_id,
                f"worker:{tasks[0]}",
                reason="operator cancelled",
                checkpoint={"nodes": {}},
            )
            permit = connection.execute(
                """SELECT permit.state AS permit_state, effect.state AS effect_state,
                          effect.result_metadata
                   FROM merlin_storage.target_effect_permits AS permit
                   JOIN merlin_storage.tool_effects AS effect
                     ON effect.idempotency_key = permit.idempotency_key
                   WHERE permit.permit_digest = %s""",
                (permit_digest,),
            ).fetchone()
            assert permit["permit_state"] == "revoked"
            assert permit["effect_state"] == "reconciled"
            assert permit["result_metadata"]["target_effect_dispatch_status"] == (
                "revoked_before_dispatch"
            )
            effects = PostgresToolEffectStore(ToolEffectRepository(connection))
            with pytest.raises(TargetEffectPermitLatchedError):
                await effects.adopt_permitted(
                    ToolEffectRecord.model_validate(intent), permit_digest
                )
            assert (
                connection.execute(
                    """SELECT 1 FROM merlin_storage.target_admission_latches
                   WHERE lane_attempt_id = %s""",
                    (lease.id,),
                ).fetchone()
                is None
            )
            assert store.reconcile_cancelled_target_lanes(schedule_id) == {
                "released": (),
                "held": (),
                "latched": (),
            }
            sibling = store.claim(
                schedule_id,
                f"worker:{tasks[1]}",
                "worker:sibling",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=_target_admission(
                    campaign_id=campaign_id,
                    session_id=str(session.id),
                    schedule_id=schedule_id,
                    target_id=target_id,
                    assignment_id=tasks[1],
                ),
            )
            assert sibling.node_id == f"worker:{tasks[1]}"


@pytest.mark.asyncio
async def test_pg0_cancel_keeps_adopted_unfinished_target_effect_held(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-target-cancel-held")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("cancel adopted target effect")
            store, _campaigns, schedule_id, campaign_id, target_id, tasks, _offline = (
                _prepare_target_transition_campaign(connection, str(session.id))
            )
            admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=schedule_id,
                target_id=target_id,
                assignment_id=tasks[0],
            )
            lease = store.claim(
                schedule_id,
                f"worker:{tasks[0]}",
                "worker:cancel-adopted",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=admission,
            )
            intent = ToolEffectRecord.model_validate(
                _target_effect_intent(
                    session_id=str(session.id),
                    campaign_id=campaign_id,
                    target_id=target_id,
                    assignment_id=tasks[0],
                    attempt_id=lease.id,
                    call_id="call-adopted-before-cancel",
                )
            )
            effect_admission = {
                **admission,
                "target_session_id": "browser:cancel-adopted",
            }
            permit_digest = store.final_target_effect_admitted(
                schedule_id,
                f"worker:{tasks[0]}",
                lease.id,
                lease.generation,
                tool_name=intent.tool_name,
                call_id=intent.call_id,
                target_admission=effect_admission,
                effect_intent=intent.model_dump(mode="json"),
            )
            effects = PostgresToolEffectStore(ToolEffectRepository(connection))
            _adopted, created = await effects.adopt_permitted(intent, permit_digest)
            assert created is True
            assert store.cancel_node(
                schedule_id,
                f"worker:{tasks[0]}",
                reason="operator cancelled",
                checkpoint={"nodes": {}},
            )
            assert store.reconcile_cancelled_target_lanes(schedule_id) == {
                "released": (),
                "held": (lease.id,),
                "latched": (),
            }
            with pytest.raises(
                scheduler_store_module.TargetLaneRecoveryRequiredError,
                match="reconciliation",
            ):
                store.claim(
                    schedule_id,
                    f"worker:{tasks[1]}",
                    "worker:sibling",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                    target_admission=_target_admission(
                        campaign_id=campaign_id,
                        session_id=str(session.id),
                        schedule_id=schedule_id,
                        target_id=target_id,
                        assignment_id=tasks[1],
                    ),
                )


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle_action", ("pause", "expire"))
async def test_pg0_pause_or_expire_revokes_undispatched_target_permit(
    tmp_path: Path,
    lifecycle_action: str,
) -> None:
    with Database(
        DatabaseConfig(data_dir=tmp_path / f"postgres-target-{lifecycle_action}-intent")
    ) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create(
                f"{lifecycle_action} undispatched target effect"
            )
            store, _campaigns, schedule_id, campaign_id, target_id, tasks, _offline = (
                _prepare_target_transition_campaign(connection, str(session.id))
            )
            admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=schedule_id,
                target_id=target_id,
                assignment_id=tasks[0],
            )
            lease = store.claim(
                schedule_id,
                f"worker:{tasks[0]}",
                f"worker:{lifecycle_action}-intent",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=admission,
            )
            intent = ToolEffectRecord.model_validate(
                _target_effect_intent(
                    session_id=str(session.id),
                    campaign_id=campaign_id,
                    target_id=target_id,
                    assignment_id=tasks[0],
                    attempt_id=lease.id,
                    call_id=f"call-{lifecycle_action}-before-dispatch",
                )
            )
            physical_session_id = f"browser:{lifecycle_action}-before-dispatch"
            permit_digest = store.final_target_effect_admitted(
                schedule_id,
                f"worker:{tasks[0]}",
                lease.id,
                lease.generation,
                tool_name=intent.tool_name,
                call_id=intent.call_id,
                target_admission={
                    **admission,
                    "target_session_id": physical_session_id,
                },
                effect_intent=intent.model_dump(mode="json"),
            )

            if lifecycle_action == "pause":
                store.pause_schedule(schedule_id, reason="operator pause")
            else:
                connection.execute(
                    """UPDATE merlin_storage.scheduler_attempts
                       SET lease_expires_at = now() - interval '1 second'
                       WHERE id = %s""",
                    (lease.id,),
                )
                connection.commit()
                store.expire(lease.id, lease.generation)

            terminal = connection.execute(
                """SELECT permit.state AS permit_state, effect.state AS effect_state,
                          effect.result_metadata
                   FROM merlin_storage.target_effect_permits AS permit
                   JOIN merlin_storage.tool_effects AS effect
                     ON effect.idempotency_key = permit.idempotency_key
                   WHERE permit.permit_digest = %s""",
                (permit_digest,),
            ).fetchone()
            assert terminal["permit_state"] == "revoked"
            assert terminal["effect_state"] == "reconciled"
            assert terminal["result_metadata"] == {
                "target_effect_dispatch_status": "revoked_before_dispatch",
                "target_session_id": physical_session_id,
                "target_effect_attempted": False,
                "interaction_count": 0,
                "limit_interaction_count": 0,
                "target_call_budget_charge": 0,
            }
            assert (
                connection.execute(
                    """SELECT 1 FROM merlin_storage.target_admission_latches
                   WHERE lane_attempt_id = %s""",
                    (lease.id,),
                ).fetchone()
                is None
            )
            effects = PostgresToolEffectStore(ToolEffectRepository(connection))
            with pytest.raises(TargetEffectPermitLatchedError):
                await effects.adopt_permitted(intent, permit_digest)

            if lifecycle_action == "pause":
                store.resume_schedule(schedule_id)
            sibling = store.claim(
                schedule_id,
                f"worker:{tasks[1]}",
                f"worker:{lifecycle_action}-sibling",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=_target_admission(
                    campaign_id=campaign_id,
                    session_id=str(session.id),
                    schedule_id=schedule_id,
                    target_id=target_id,
                    assignment_id=tasks[1],
                ),
            )
            assert sibling.node_id == f"worker:{tasks[1]}"


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle_action", ("pause", "expire"))
async def test_pg0_pause_or_expire_keeps_adopted_target_effect_held(
    tmp_path: Path,
    lifecycle_action: str,
) -> None:
    with Database(
        DatabaseConfig(data_dir=tmp_path / f"postgres-target-{lifecycle_action}-adopted")
    ) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create(
                f"{lifecycle_action} adopted target effect"
            )
            store, _campaigns, schedule_id, campaign_id, target_id, tasks, _offline = (
                _prepare_target_transition_campaign(connection, str(session.id))
            )
            admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=schedule_id,
                target_id=target_id,
                assignment_id=tasks[0],
            )
            lease = store.claim(
                schedule_id,
                f"worker:{tasks[0]}",
                f"worker:{lifecycle_action}-adopted",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=admission,
            )
            intent = ToolEffectRecord.model_validate(
                _target_effect_intent(
                    session_id=str(session.id),
                    campaign_id=campaign_id,
                    target_id=target_id,
                    assignment_id=tasks[0],
                    attempt_id=lease.id,
                    call_id=f"call-{lifecycle_action}-after-adoption",
                )
            )
            physical_session_id = f"browser:{lifecycle_action}-after-adoption"
            permit_digest = store.final_target_effect_admitted(
                schedule_id,
                f"worker:{tasks[0]}",
                lease.id,
                lease.generation,
                tool_name=intent.tool_name,
                call_id=intent.call_id,
                target_admission={
                    **admission,
                    "target_session_id": physical_session_id,
                },
                effect_intent=intent.model_dump(mode="json"),
            )
            effects = PostgresToolEffectStore(ToolEffectRepository(connection))
            _adopted, adopted_now = await effects.adopt_permitted(intent, permit_digest)
            assert adopted_now is True

            if lifecycle_action == "pause":
                store.pause_schedule(schedule_id, reason="operator pause")
                store.resume_schedule(schedule_id)
            else:
                connection.execute(
                    """UPDATE merlin_storage.scheduler_attempts
                       SET lease_expires_at = now() - interval '1 second'
                       WHERE id = %s""",
                    (lease.id,),
                )
                connection.commit()
                store.expire(lease.id, lease.generation)

            held = connection.execute(
                """SELECT permit.state AS permit_state, effect.state AS effect_state,
                          latch.lane_attempt_id::text AS lane_attempt_id,
                          latch.lane_recovery_required
                   FROM merlin_storage.target_effect_permits AS permit
                   JOIN merlin_storage.tool_effects AS effect
                     ON effect.idempotency_key = permit.idempotency_key
                   JOIN merlin_storage.target_admission_latches AS latch
                     ON latch.lane_attempt_id = permit.attempt_id
                   WHERE permit.permit_digest = %s""",
                (permit_digest,),
            ).fetchone()
            assert held == {
                "permit_state": "adopted",
                "effect_state": "intent",
                "lane_attempt_id": lease.id,
                "lane_recovery_required": True,
            }
            with pytest.raises(
                scheduler_store_module.TargetLaneRecoveryRequiredError,
                match="reconciliation",
            ):
                store.claim(
                    schedule_id,
                    f"worker:{tasks[1]}",
                    f"worker:{lifecycle_action}-sibling",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                    target_admission=_target_admission(
                        campaign_id=campaign_id,
                        session_id=str(session.id),
                        schedule_id=schedule_id,
                        target_id=target_id,
                        assignment_id=tasks[1],
                    ),
                )


def test_pg0_cancel_and_target_permit_adoption_are_one_atomic_race(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-target-cancel-race")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("cancel target permit race")
            store, _campaigns, schedule_id, campaign_id, target_id, tasks, _offline = (
                _prepare_target_transition_campaign(connection, str(session.id))
            )
            node_id = f"worker:{tasks[0]}"
            admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=schedule_id,
                target_id=target_id,
                assignment_id=tasks[0],
            )
            lease = store.claim(
                schedule_id,
                node_id,
                "worker:cancel-race",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=admission,
            )
            target_session_id = "browser:cancel-race"
            intent = ToolEffectRecord.model_validate(
                _target_effect_intent(
                    session_id=str(session.id),
                    campaign_id=campaign_id,
                    target_id=target_id,
                    assignment_id=tasks[0],
                    attempt_id=lease.id,
                    call_id="call-cancel-adopt-race",
                )
            )
            permit_digest = store.final_target_effect_admitted(
                schedule_id,
                node_id,
                lease.id,
                lease.generation,
                tool_name=intent.tool_name,
                call_id=intent.call_id,
                target_admission={
                    **admission,
                    "target_session_id": target_session_id,
                },
                effect_intent=intent.model_dump(mode="json"),
            )

        ready = Barrier(2)

        def cancel() -> tuple[str, object]:
            with database.connect() as cancel_connection:
                ready.wait(timeout=10)
                try:
                    cancelled = DurableSchedulerStore(cancel_connection).cancel_node(
                        schedule_id,
                        node_id,
                        reason="operator cancelled during permit adoption",
                        checkpoint={"nodes": {}},
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    return "error", exc
                return "cancelled", cancelled

        def adopt() -> tuple[str, object]:
            with database.connect() as adopt_connection:
                ready.wait(timeout=10)
                effects = PostgresToolEffectStore(ToolEffectRepository(adopt_connection))
                try:
                    adopted = asyncio.run(effects.adopt_permitted(intent, permit_digest))
                except TargetEffectPermitLatchedError as exc:
                    return "latched", exc
                except Exception as exc:  # pragma: no cover - asserted below
                    return "error", exc
                return "adopted", adopted

        with ThreadPoolExecutor(max_workers=2) as executor:
            cancel_future = executor.submit(cancel)
            adopt_future = executor.submit(adopt)
            cancel_outcome = cancel_future.result(timeout=15)
            adopt_outcome = adopt_future.result(timeout=15)

        assert cancel_outcome == ("cancelled", True)
        assert adopt_outcome[0] in {"adopted", "latched"}
        assert adopt_outcome[0] != "error"

        with database.connect() as connection:
            permit = connection.execute(
                """SELECT permit.state AS permit_state, effect.state AS effect_state,
                          effect.result_metadata
                   FROM merlin_storage.target_effect_permits AS permit
                   JOIN merlin_storage.tool_effects AS effect
                     ON effect.idempotency_key = permit.idempotency_key
                   WHERE permit.permit_digest = %s""",
                (permit_digest,),
            ).fetchone()
            lane = connection.execute(
                """SELECT lane_recovery_required
                   FROM merlin_storage.target_admission_latches
                   WHERE lane_attempt_id = %s""",
                (lease.id,),
            ).fetchone()

            if adopt_outcome[0] == "latched":
                assert permit["permit_state"] == "revoked"
                assert permit["effect_state"] == "reconciled"
                assert permit["result_metadata"]["target_effect_dispatch_status"] == (
                    "revoked_before_dispatch"
                )
                assert lane is None
            else:
                assert permit["permit_state"] == "adopted"
                assert permit["effect_state"] == "intent"
                assert lane == {"lane_recovery_required": True}
                adopted_record, adopted_now = adopt_outcome[1]
                assert adopted_now is True
                effects = PostgresToolEffectStore(ToolEffectRepository(connection))
                asyncio.run(
                    effects.finish(
                        adopted_record.model_copy(
                            update={
                                "state": ToolEffectState.ACKNOWLEDGED,
                                "result_content": "",
                                "result_is_error": True,
                                "result_metadata": {
                                    "target_session_id": target_session_id,
                                    "target_effect_attempted": False,
                                    "interaction_count": 0,
                                    "limit_interaction_count": 0,
                                    "target_call_budget_charge": 0,
                                },
                            }
                        )
                    )
                )
                assert DurableSchedulerStore(connection).reconcile_cancelled_target_lanes(
                    schedule_id
                ) == {
                    "released": (lease.id,),
                    "held": (),
                    "latched": (),
                }

            final = connection.execute(
                """SELECT permit.state AS permit_state, effect.state AS effect_state
                   FROM merlin_storage.target_effect_permits AS permit
                   JOIN merlin_storage.tool_effects AS effect
                     ON effect.idempotency_key = permit.idempotency_key
                   WHERE permit.permit_digest = %s""",
                (permit_digest,),
            ).fetchone()
            assert final["permit_state"] in {"revoked", "terminal"}
            assert final["effect_state"] in {"reconciled", "acknowledged"}
            assert (
                connection.execute(
                    """SELECT 1 FROM merlin_storage.target_admission_latches
                   WHERE lane_attempt_id = %s""",
                    (lease.id,),
                ).fetchone()
                is None
            )


@pytest.mark.parametrize("lifecycle_action", ("pause", "expire"))
def test_pg0_pause_or_expire_and_target_permit_adoption_are_one_atomic_race(
    tmp_path: Path,
    lifecycle_action: str,
) -> None:
    with Database(
        DatabaseConfig(data_dir=tmp_path / f"postgres-target-{lifecycle_action}-race")
    ) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create(f"{lifecycle_action} target permit race")
            store, _campaigns, schedule_id, campaign_id, target_id, tasks, _offline = (
                _prepare_target_transition_campaign(connection, str(session.id))
            )
            node_id = f"worker:{tasks[0]}"
            admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=schedule_id,
                target_id=target_id,
                assignment_id=tasks[0],
            )
            lease = store.claim(
                schedule_id,
                node_id,
                f"worker:{lifecycle_action}-race",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=admission,
            )
            target_session_id = f"browser:{lifecycle_action}-race"
            intent = ToolEffectRecord.model_validate(
                _target_effect_intent(
                    session_id=str(session.id),
                    campaign_id=campaign_id,
                    target_id=target_id,
                    assignment_id=tasks[0],
                    attempt_id=lease.id,
                    call_id=f"call-{lifecycle_action}-adopt-race",
                )
            )
            permit_digest = store.final_target_effect_admitted(
                schedule_id,
                node_id,
                lease.id,
                lease.generation,
                tool_name=intent.tool_name,
                call_id=intent.call_id,
                target_admission={
                    **admission,
                    "target_session_id": target_session_id,
                },
                effect_intent=intent.model_dump(mode="json"),
            )
            if lifecycle_action == "expire":
                connection.execute(
                    """UPDATE merlin_storage.scheduler_attempts
                       SET lease_expires_at = now() - interval '1 second'
                       WHERE id = %s""",
                    (lease.id,),
                )
                connection.commit()

        ready = Barrier(2)

        def fence_lifecycle() -> tuple[str, object]:
            with database.connect() as lifecycle_connection:
                ready.wait(timeout=10)
                lifecycle_store = DurableSchedulerStore(lifecycle_connection)
                try:
                    if lifecycle_action == "pause":
                        lifecycle_store.pause_schedule(
                            schedule_id, reason="operator pause during permit adoption"
                        )
                    else:
                        lifecycle_store.expire(lease.id, lease.generation)
                except Exception as exc:  # pragma: no cover - asserted below
                    return "error", exc
                return lifecycle_action, True

        def adopt() -> tuple[str, object]:
            with database.connect() as adopt_connection:
                ready.wait(timeout=10)
                effects = PostgresToolEffectStore(ToolEffectRepository(adopt_connection))
                try:
                    adopted = asyncio.run(effects.adopt_permitted(intent, permit_digest))
                except TargetEffectPermitLatchedError as exc:
                    return "latched", exc
                except Exception as exc:  # pragma: no cover - asserted below
                    return "error", exc
                return "adopted", adopted

        with ThreadPoolExecutor(max_workers=2) as executor:
            lifecycle_future = executor.submit(fence_lifecycle)
            adopt_future = executor.submit(adopt)
            lifecycle_outcome = lifecycle_future.result(timeout=15)
            adopt_outcome = adopt_future.result(timeout=15)

        assert lifecycle_outcome == (lifecycle_action, True)
        assert adopt_outcome[0] in {"adopted", "latched"}
        assert adopt_outcome[0] != "error"

        with database.connect() as connection:
            store = DurableSchedulerStore(connection)
            permit = connection.execute(
                """SELECT permit.state AS permit_state, effect.state AS effect_state,
                          effect.result_metadata
                   FROM merlin_storage.target_effect_permits AS permit
                   JOIN merlin_storage.tool_effects AS effect
                     ON effect.idempotency_key = permit.idempotency_key
                   WHERE permit.permit_digest = %s""",
                (permit_digest,),
            ).fetchone()
            lane = connection.execute(
                """SELECT lane_recovery_required
                   FROM merlin_storage.target_admission_latches
                   WHERE lane_attempt_id = %s""",
                (lease.id,),
            ).fetchone()

            if adopt_outcome[0] == "latched":
                assert permit["permit_state"] == "revoked"
                assert permit["effect_state"] == "reconciled"
                assert permit["result_metadata"]["target_effect_dispatch_status"] == (
                    "revoked_before_dispatch"
                )
                assert lane is None
                if lifecycle_action == "pause":
                    store.resume_schedule(schedule_id)
            else:
                assert permit["permit_state"] == "adopted"
                assert permit["effect_state"] == "intent"
                assert lane == {"lane_recovery_required": True}
                adopted_record, adopted_now = adopt_outcome[1]
                assert adopted_now is True
                effects = PostgresToolEffectStore(ToolEffectRepository(connection))
                asyncio.run(
                    effects.finish(
                        adopted_record.model_copy(
                            update={
                                "state": ToolEffectState.ACKNOWLEDGED,
                                "result_content": "",
                                "result_is_error": True,
                                "result_metadata": {
                                    "target_session_id": target_session_id,
                                    "target_effect_attempted": False,
                                    "interaction_count": 0,
                                    "limit_interaction_count": 0,
                                    "target_call_budget_charge": 0,
                                },
                            }
                        )
                    )
                )
                if lifecycle_action == "pause":
                    store.resume_schedule(schedule_id)
                recovery_admission = {
                    **admission,
                    "target_session_id": target_session_id,
                }
                replacement = store.claim(
                    schedule_id,
                    node_id,
                    f"worker:{lifecycle_action}-recovery",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                    target_admission=recovery_admission,
                )
                assert replacement.replaced_attempt_id == lease.id
                assert replacement.lane_recovery_required is True
                recovery_body = {
                    "schema": "merlin.target-lane-recovery.v1",
                    "prior_attempt_id": lease.id,
                    "effect_ids": [intent.idempotency_key],
                    "outcome": "no_material_transition",
                }
                store.resolve_target_lane_recovery(
                    schedule_id,
                    node_id,
                    replacement.id,
                    replacement.generation,
                    target_admission=recovery_admission,
                    recovery={
                        **recovery_body,
                        "recovery_digest": _qualified(recovery_body),
                    },
                )
                assert store.cancel_node(
                    schedule_id,
                    node_id,
                    reason="race cleanup",
                    checkpoint={"nodes": {}},
                )

            final = connection.execute(
                """SELECT permit.state AS permit_state, effect.state AS effect_state
                   FROM merlin_storage.target_effect_permits AS permit
                   JOIN merlin_storage.tool_effects AS effect
                     ON effect.idempotency_key = permit.idempotency_key
                   WHERE permit.permit_digest = %s""",
                (permit_digest,),
            ).fetchone()
            assert final["permit_state"] in {"revoked", "terminal"}
            assert final["effect_state"] in {"reconciled", "acknowledged"}
            assert (
                connection.execute(
                    """SELECT 1 FROM merlin_storage.target_admission_latches
                   WHERE lane_attempt_id IS NOT NULL AND campaign_id = %s
                     AND target_id = %s""",
                    (campaign_id, target_id),
                ).fetchone()
                is None
            )


def test_pg0_reviewed_refresh_can_resolve_terminal_without_authorizing_schedule(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-target-terminal")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("target terminal resolution")
            store, campaigns, schedule_id, campaign_id, target_id, tasks, _offline = (
                _prepare_target_transition_campaign(connection, str(session.id))
            )
            admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=schedule_id,
                target_id=target_id,
                assignment_id=tasks[0],
            )
            lease = store.claim(
                schedule_id,
                f"worker:{tasks[0]}",
                "worker:first",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=admission,
            )
            transition = _surface_transition_observation(
                campaign_id=campaign_id,
                session_id=str(session.id),
                target_id=target_id,
                target_session_id="browser:context-terminal",
                task_id=tasks[0],
                call_id="call-terminal-transition",
            )
            store.commit_reducer(
                lease.id,
                lease.generation,
                schema_id="schema/result",
                result={"content": "level complete"},
                checkpoint={"nodes": {}},
                target_session_id="browser:context-terminal",
                target_surface_transition=transition,
            )
            authority = _journal_reviewed_refresh(
                campaigns,
                campaign_id=campaign_id,
                session_id=str(session.id),
                target_id=target_id,
                target_session_id="browser:context-terminal",
            )
            resolution = {
                key: value
                for key, value in {
                    "campaign_id": campaign_id,
                    "session_id": str(session.id),
                    "target_id": target_id,
                    "target_session_id": "browser:context-terminal",
                    "generation_id": authority["generation_id"],
                    "event_set_digest": authority["event_set_digest"],
                    "settlement_id": authority["settlement_id"],
                    "lead_decision_id": authority["lead_decision_id"],
                }.items()
            }
            resolution_id = store.resolve_target_admission_terminal(resolution)
            assert resolution_id == store.resolve_target_admission_terminal(resolution)
            state = connection.execute(
                """SELECT state, authorized_schedule_id
                   FROM merlin_storage.target_admission_latches
                   WHERE campaign_id = %s AND target_id = %s AND target_session_id = %s""",
                (campaign_id, target_id, "browser:context-terminal"),
            ).fetchone()
            assert state == {"state": "resolved", "authorized_schedule_id": None}
            head = connection.execute(
                """SELECT state, latched_session_count
                   FROM merlin_storage.target_admission_latches
                   WHERE campaign_id = %s AND target_id = %s AND target_session_id = ''""",
                (campaign_id, target_id),
            ).fetchone()
            assert head == {"state": "open", "latched_session_count": 0}
            with pytest.raises(scheduler_store_module.TargetAdmissionLatchedError):
                store.claim(
                    schedule_id,
                    f"worker:{tasks[1]}",
                    "worker:after-terminal",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                    target_admission=_target_admission(
                        campaign_id=campaign_id,
                        session_id=str(session.id),
                        schedule_id=schedule_id,
                        target_id=target_id,
                        assignment_id=tasks[1],
                        target_session_id="browser:context-terminal",
                    ),
                )


def test_pg0_activation_supersedes_all_reviewed_latched_target_sessions(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-transition-activation")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("target transition activation")
            store, campaigns, source_schedule, campaign_id, target_id, tasks, _offline = (
                _prepare_target_transition_campaign(connection, str(session.id))
            )
            source_admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=source_schedule,
                target_id=target_id,
                assignment_id=tasks[0],
            )
            source = store.claim(
                source_schedule,
                f"worker:{tasks[0]}",
                "worker:source",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=source_admission,
            )
            transition = _surface_transition_observation(
                campaign_id=campaign_id,
                session_id=str(session.id),
                target_id=target_id,
                target_session_id="browser:context-1",
                task_id=tasks[0],
                call_id="call-activation-transition",
            )
            store.commit_reducer(
                source.id,
                source.generation,
                schema_id="schema/result",
                result={"content": "level complete"},
                checkpoint={"nodes": {}},
                target_session_id="browser:context-1",
                target_surface_transition=transition,
            )
            authority = _journal_reviewed_refresh(
                campaigns,
                campaign_id=campaign_id,
                session_id=str(session.id),
                target_id=target_id,
                target_session_id="browser:context-1",
            )
            sibling_transition = _surface_transition_observation(
                campaign_id=campaign_id,
                session_id=str(session.id),
                target_id=target_id,
                target_session_id="browser:context-sibling",
                task_id=tasks[2],
                call_id="call-sibling-transition",
            )
            with connection.transaction():
                store._commit_target_surface_transition_locked(
                    source_schedule,
                    f"worker:{tasks[2]}",
                    sibling_transition,
                )
            sibling_authority = _journal_reviewed_refresh(
                campaigns,
                campaign_id=campaign_id,
                session_id=str(session.id),
                target_id=target_id,
                target_session_id="browser:context-sibling",
            )

            passive_task = (
                f"{campaign_id}/target-reobservation/"
                f"{authority['generation_id'].removeprefix('sha256:')}/recon"
            )
            passive_contract = [
                ["schema", "merlin.target-reobservation.v1"],
                ["generation_id", authority["generation_id"]],
                ["event_set_digest", authority["event_set_digest"]],
                ["target_session_id", "browser:context-1"],
                [
                    "target_reobservation_start_decision_id",
                    authority["start_decision_id"],
                ],
            ]
            store.create_schedule(
                str(session.id),
                {
                    "assignments": [
                        {
                            "id": passive_task,
                            "target_profile_id": target_id,
                            "tool_ids": ["browser_snapshot", "browser_record_api_handoff"],
                            "technique_ids": [],
                            "strategy_id": "target-reobservation/v1",
                            "strategy_contract": passive_contract,
                            "reviewed_adapter_digest": "sha256:" + "8" * 64,
                        }
                    ],
                    "worker_contexts": {},
                },
                {"nodes": {}},
                [f"worker:{passive_task}"],
                ready_nodes=[f"worker:{passive_task}"],
                schedule_id=authority["reobserve_schedule_id"],
            )
            passive_admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=authority["reobserve_schedule_id"],
                target_id=target_id,
                assignment_id=passive_task,
                target_session_id="browser:context-1",
                strategy_id="target-reobservation/v1",
                generation_id=authority["generation_id"],
                event_set_digest=authority["event_set_digest"],
                start_decision_id=authority["start_decision_id"],
                target_exchange_tool_ids=(),
            )
            passive = store.claim(
                authority["reobserve_schedule_id"],
                f"worker:{passive_task}",
                "worker:passive",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=passive_admission,
            )
            store.commit_reducer(
                passive.id,
                passive.generation,
                schema_id="schema/result",
                result={"content": "refreshed"},
                checkpoint={"nodes": {}},
                target_session_id="browser:context-1",
            )

            rebound_task = f"{campaign_id}/wave-1/rebound"
            rebound_next_task = f"{campaign_id}/wave-1/rebound-next"
            rebound_contract = [
                ["target_session_id", "browser:context-1"],
                [
                    "initial_state_equivalence_digest",
                    authority["current_surface_digest"],
                ],
                ["compiled_adapter_digest", authority["compiled_adapter_digest"]],
                ["reviewed_adapter_digest", authority["reviewed_adapter_digest"]],
                ["target_reobservation_generation_id", authority["generation_id"]],
                [
                    "target_reobservation_event_set_digest",
                    authority["event_set_digest"],
                ],
            ]
            rebound_schedule = store.create_schedule(
                str(session.id),
                {
                    "assignments": [
                        {
                            "id": rebound_task,
                            "target_profile_id": target_id,
                            "tool_ids": ["browser_chat_exchange"],
                            "strategy_id": "direct",
                            "strategy_contract": rebound_contract,
                            "reviewed_adapter_digest": authority["reviewed_adapter_digest"],
                        },
                        {
                            "id": rebound_next_task,
                            "target_profile_id": target_id,
                            "tool_ids": ["browser_chat_exchange"],
                            "strategy_id": "direct",
                            "strategy_contract": rebound_contract,
                            "reviewed_adapter_digest": authority["reviewed_adapter_digest"],
                        },
                    ],
                    "worker_contexts": {
                        rebound_task: _persisted_worker_context(
                            assignment_id=rebound_task,
                            target_id=target_id,
                            tool_ids=("browser_chat_exchange",),
                            strategy_id="direct",
                            strategy_contract=rebound_contract,
                        ),
                        rebound_next_task: _persisted_worker_context(
                            assignment_id=rebound_next_task,
                            target_id=target_id,
                            tool_ids=("browser_chat_exchange",),
                            strategy_id="direct",
                            strategy_contract=rebound_contract,
                        ),
                    },
                },
                {"nodes": {}},
                [f"worker:{rebound_task}", f"worker:{rebound_next_task}"],
                ready_nodes=[f"worker:{rebound_task}", f"worker:{rebound_next_task}"],
                initial_status="paused",
            )
            campaigns.add_wave(campaign_id, 1, "followup", rebound_schedule)
            transition_claim = campaigns.claim_frontier_transition(
                campaign_id, "test:target-activation", 60
            )
            assert transition_claim is not None
            current_latches = {
                str(row["target_session_id"]): row
                for row in store.current_target_admission_latches(
                    campaign_id, str(session.id), target_id
                )
            }
            latch_revision = current_latches["browser:context-1"]["revision"]
            activation = {
                "campaign_id": campaign_id,
                "session_id": str(session.id),
                "target_id": target_id,
                "target_session_id": "browser:context-1",
                "claim_id": transition_claim,
                "claimant_id": "test:target-activation",
                "latch_revision": latch_revision,
                "generation_id": authority["generation_id"],
                "event_set_digest": authority["event_set_digest"],
                "settlement_id": authority["settlement_id"],
                "lead_decision_id": authority["lead_decision_id"],
                "schedule_id": rebound_schedule,
                "current_surface_digest": authority["current_surface_digest"],
                "compiled_adapter_digest": authority["compiled_adapter_digest"],
                "reviewed_adapter_digest": authority["reviewed_adapter_digest"],
                "wave": 1,
                "kind": "followup",
                "source_schedule_id": None,
                "frontier_batch_id": None,
                "target_assignment_tools": {
                    rebound_task: ["browser_chat_exchange"],
                    rebound_next_task: ["browser_chat_exchange"],
                },
                "superseded_generations": (
                    {
                        "target_session_id": "browser:context-sibling",
                        "generation_id": sibling_authority["generation_id"],
                        "event_set_digest": sibling_authority["event_set_digest"],
                        "settlement_id": sibling_authority["settlement_id"],
                        "lead_decision_id": sibling_authority["lead_decision_id"],
                        "latch_revision": current_latches["browser:context-sibling"]["revision"],
                    },
                ),
            }
            forged = {**activation, "compiled_adapter_digest": "sha256:" + "f" * 64}
            with pytest.raises(scheduler_store_module.TargetAdmissionLatchedError):
                store.activate_target_admission(forged)
            with pytest.raises(scheduler_store_module.TargetAdmissionLatchedError):
                store.activate_target_admission({**activation, "superseded_generations": ()})
            extra_generation = {
                "target_session_id": "browser:forged-extra",
                "generation_id": "sha256:" + "a" * 64,
                "event_set_digest": "sha256:" + "b" * 64,
                "settlement_id": "target-reobservation-settled/" + "a" * 64,
                "lead_decision_id": "lead-decision:" + "c" * 64,
                "latch_revision": 1,
            }
            with pytest.raises(scheduler_store_module.TargetAdmissionLatchedError):
                store.activate_target_admission(
                    {
                        **activation,
                        "superseded_generations": (
                            *activation["superseded_generations"],
                            extra_generation,
                        ),
                    }
                )
            with pytest.raises(scheduler_store_module.TargetAdmissionLatchedError):
                store.activate_target_admission({**activation, "claimant_id": "forged:claimant"})
            activation_id = store.activate_target_admission(activation)
            assert activation_id == store.activate_target_admission(activation)
            sibling_latch = connection.execute(
                """SELECT state, authorized_schedule_id
                   FROM merlin_storage.target_admission_latches
                   WHERE campaign_id = %s AND target_id = %s
                     AND target_session_id = 'browser:context-sibling'""",
                (campaign_id, target_id),
            ).fetchone()
            assert sibling_latch == {"state": "resolved", "authorized_schedule_id": None}
            current = store.current_target_admission_latches(
                campaign_id, str(session.id), target_id
            )
            assert len(current) == 1
            assert current[0]["state"] == "activated"
            assert current[0]["authorized_schedule_id"] == rebound_schedule
            activation_rows_before = connection.execute(
                """SELECT count(*) AS count
                   FROM merlin_storage.campaign_lead_journal
                   WHERE campaign_id = %s
                     AND payload->>'kind' = 'target_admission_generation_activated'""",
                (campaign_id,),
            ).fetchone()["count"]
            compact_replay = {
                **activation,
                "latch_revision": current[0]["revision"],
                "superseded_generations": (),
            }
            assert store.activate_target_admission(compact_replay) == activation_id
            replayed = store.current_target_admission_latches(
                campaign_id, str(session.id), target_id
            )
            assert replayed == current
            assert (
                connection.execute(
                    """SELECT count(*) AS count
                       FROM merlin_storage.campaign_lead_journal
                       WHERE campaign_id = %s
                         AND payload->>'kind' =
                             'target_admission_generation_activated'""",
                    (campaign_id,),
                ).fetchone()["count"]
                == activation_rows_before
            )

            connection.execute(
                """UPDATE merlin_storage.campaign_frontier_transition_leases
                   SET expires_at = now() - interval '1 second'
                   WHERE campaign_id = %s AND claim_id = %s""",
                (campaign_id, transition_claim),
            )
            with pytest.raises(scheduler_store_module.TargetAdmissionLatchedError):
                store.activate_target_admission(compact_replay)
            takeover_claimant = "test:target-activation-takeover"
            takeover_claim = campaigns.claim_frontier_transition(campaign_id, takeover_claimant, 60)
            assert takeover_claim is not None
            compact_replay = {
                **compact_replay,
                "claim_id": takeover_claim,
                "claimant_id": takeover_claimant,
            }
            assert store.activate_target_admission(compact_replay) == activation_id

            campaigns.pause(campaign_id)
            with pytest.raises(scheduler_store_module.TargetAdmissionLatchedError):
                store.activate_target_admission(compact_replay)
            campaigns.resume(campaign_id)
            assert store.activate_target_admission(compact_replay) == activation_id
            with pytest.raises(scheduler_store_module.TargetAdmissionLatchedError):
                store.activate_target_admission({**compact_replay, "wave": 2})
            activation = {
                **activation,
                "claim_id": takeover_claim,
                "claimant_id": takeover_claimant,
            }
            next_rebound_task = f"{campaign_id}/wave-2/rebound"
            next_rebound_next_task = f"{campaign_id}/wave-2/rebound-next"
            rebound_graph = {
                "assignments": [
                    {
                        "id": next_rebound_task,
                        "target_profile_id": target_id,
                        "tool_ids": ["browser_chat_exchange"],
                        "strategy_id": "direct",
                        "strategy_contract": rebound_contract,
                        "reviewed_adapter_digest": authority["reviewed_adapter_digest"],
                    },
                    {
                        "id": next_rebound_next_task,
                        "target_profile_id": target_id,
                        "tool_ids": ["browser_chat_exchange"],
                        "strategy_id": "direct",
                        "strategy_contract": rebound_contract,
                        "reviewed_adapter_digest": authority["reviewed_adapter_digest"],
                    },
                ],
                "worker_contexts": {
                    next_rebound_task: _persisted_worker_context(
                        assignment_id=next_rebound_task,
                        target_id=target_id,
                        tool_ids=("browser_chat_exchange",),
                        strategy_id="direct",
                        strategy_contract=rebound_contract,
                    ),
                    next_rebound_next_task: _persisted_worker_context(
                        assignment_id=next_rebound_next_task,
                        target_id=target_id,
                        tool_ids=("browser_chat_exchange",),
                        strategy_id="direct",
                        strategy_contract=rebound_contract,
                    ),
                },
            }
            next_rebound_schedule = store.create_schedule(
                str(session.id),
                rebound_graph,
                {"nodes": {}},
                [f"worker:{next_rebound_task}", f"worker:{next_rebound_next_task}"],
                ready_nodes=[
                    f"worker:{next_rebound_task}",
                    f"worker:{next_rebound_next_task}",
                ],
                initial_status="paused",
            )
            campaigns.add_wave(campaign_id, 2, "followup", next_rebound_schedule)
            next_activation = {
                **activation,
                "schedule_id": next_rebound_schedule,
                "wave": 2,
                "latch_revision": current[0]["revision"],
                "superseded_generations": (),
                "target_assignment_tools": {
                    next_rebound_task: ["browser_chat_exchange"],
                    next_rebound_next_task: ["browser_chat_exchange"],
                },
            }
            with pytest.raises(
                scheduler_store_module.TargetAdmissionLatchedError,
                match="prior activated target schedule is not terminal",
            ):
                store.activate_target_admission(next_activation)
            store.reject_schedule(rebound_schedule, reason="reviewed follow-up completed")
            next_activation_id = store.activate_target_admission(next_activation)
            assert next_activation_id == store.activate_target_admission(next_activation)
            assert next_activation_id != activation_id
            rebound_schedule = next_rebound_schedule
            rebound_task = next_rebound_task

            rebound_admission = _target_admission(
                campaign_id=campaign_id,
                session_id=str(session.id),
                schedule_id=rebound_schedule,
                target_id=target_id,
                assignment_id=rebound_task,
                target_session_id="browser:context-1",
            )
            rebound = store.claim(
                rebound_schedule,
                f"worker:{rebound_task}",
                "worker:rebound",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                target_admission=rebound_admission,
            )
            store.final_target_effect_admitted(
                rebound_schedule,
                f"worker:{rebound_task}",
                rebound.id,
                rebound.generation,
                tool_name="browser_chat_exchange",
                call_id="call-rebound",
                target_admission=rebound_admission,
                effect_intent=_target_effect_intent(
                    session_id=str(session.id),
                    campaign_id=campaign_id,
                    target_id=target_id,
                    assignment_id=rebound_task,
                    attempt_id=rebound.id,
                    call_id="call-rebound",
                ),
            )
            with pytest.raises(scheduler_store_module.TargetAdmissionLatchedError):
                store.claim(
                    source_schedule,
                    f"worker:{tasks[2]}",
                    "worker:stale-source",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                    target_admission=_target_admission(
                        campaign_id=campaign_id,
                        session_id=str(session.id),
                        schedule_id=source_schedule,
                        target_id=target_id,
                        assignment_id=tasks[2],
                    ),
                )

            next_transition = _surface_transition_observation(
                campaign_id=campaign_id,
                session_id=str(session.id),
                target_id=target_id,
                target_session_id="browser:context-1",
                task_id=rebound_task,
                call_id="call-next-level-same-modal",
            )
            assert next_transition["transition_digest"] == transition["transition_digest"]
            assert next_transition["event_digest"] != transition["event_digest"]
            store.commit_reducer(
                rebound.id,
                rebound.generation,
                schema_id="schema/result",
                result={"content": "next level complete"},
                checkpoint={"nodes": {}},
                target_session_id="browser:context-1",
                target_surface_transition=next_transition,
            )
            latch = connection.execute(
                """SELECT state, transition_count, pending_event_digests
                   FROM merlin_storage.target_admission_latches
                   WHERE campaign_id = %s AND target_id = %s AND target_session_id = %s""",
                (campaign_id, target_id, "browser:context-1"),
            ).fetchone()
            assert latch == {
                "state": "latched",
                "transition_count": 2,
                "pending_event_digests": [next_transition["event_digest"]],
            }
            with pytest.raises(scheduler_store_module.TargetAdmissionLatchedError):
                store.claim(
                    rebound_schedule,
                    f"worker:{rebound_next_task}",
                    "worker:stale-rebound",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                    target_admission=_target_admission(
                        campaign_id=campaign_id,
                        session_id=str(session.id),
                        schedule_id=rebound_schedule,
                        target_id=target_id,
                        assignment_id=rebound_next_task,
                        target_session_id="browser:context-1",
                    ),
                )


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
                    "infrastructure_failure_category": "adaptive_strategy_unavailable",
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
            scheduler.settle_attempt(
                lease.id,
                lease.generation,
                "failed",
                reason="The requested adaptive controller is unavailable",
                checkpoint={},
                failure_kind="configuration",
            )

            rows = scheduler.list_swarm_snapshot_rows(
                str(first_session.id), campaign_id="campaign/first"
            )

            assert len(rows) == 2
            assert {row["session_id"] for row in rows} == {str(first_session.id)}
            assert {row["campaign_id"] for row in rows} == {"campaign/first"}
            assert {row["assignment"]["id"]: row["campaign_wave"] for row in rows} == {
                "task-first": 0,
                "task-followup": 1,
            }
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
            assert isinstance(current["schedule_updated_at"], datetime)
            assert current["current_result_content"] == "Partial capability result"
            assert current["current_attempt_failure_kind"] == "configuration"
            assert (
                current["current_infrastructure_failure_category"]
                == "adaptive_strategy_unavailable"
            )
            assert [message["content"] for message in current["recent_output"]] == [
                "Recorded output 3",
                "Recorded output 4",
                "Recorded output 5",
                "Target response",
            ]
            assert [event["type"] for event in current["recent_activity"]][-2:] == [
                "checkpointed",
                "failed",
            ]


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


def test_pg0_replacement_lineage_is_exactly_bounded_and_cycle_safe(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-lineage-bounds")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("bounded retry lineage")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"nodes": ["worker"]},
                {},
                ["worker"],
                ready_nodes=["worker"],
            )
            attempts = []
            for generation in range(33):
                attempt = store.claim(
                    schedule_id,
                    "worker",
                    f"agent-{generation}",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
                attempts.append(attempt)
                if generation == 32:
                    break
                store.settle_attempt(
                    attempt.id,
                    attempt.generation,
                    "failed",
                    reason="bounded lineage fixture",
                    checkpoint={"worker": "failed"},
                )
                event_count = store.load_schedule(schedule_id)["event_count"]
                store.retry_failed(
                    schedule_id,
                    "worker",
                    checkpoint={"worker": "ready"},
                    expected_event_count=event_count,
                )

            latest = attempts[-1]
            assert store.replacement_attempt_lineage(latest.id, limit=32) == tuple(
                attempt.id for attempt in attempts[-32:]
            )
            assert store.replacement_attempt_lineage(latest.id, limit=33) == tuple(
                attempt.id for attempt in attempts
            )

            connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET replaced_attempt_id = %s WHERE id = %s""",
                (latest.id, attempts[0].id),
            )
            with pytest.raises(ValueError, match="lineage contains a cycle"):
                store.replacement_attempt_lineage(latest.id)


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


def test_pg0_nonready_completion_report_error_is_rediscovered(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("nonready completion report")
            scheduler = DurableSchedulerStore(connection)
            schedule_id = scheduler.create_schedule(
                session.id, {"nodes": ["worker"]}, {}, ["worker"], ready_nodes=["worker"]
            )
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                "campaign/nonready-report",
                str(session.id),
                {
                    "manifest": {"objective": "nonready report"},
                    "assignment_context": {},
                },
                "sha256:reviewed",
            )
            campaigns.begin_execution("campaign/nonready-report", "sha256:reviewed", schedule_id)
            campaigns.finish("campaign/nonready-report", "completed")
            campaigns.append_lead(
                "campaign/nonready-report",
                0,
                {
                    "report_error": "saturated report requires a ready completion receipt",
                    "retryable": False,
                },
            )

            assert [row["id"] for row in campaigns.list_report_retryable()] == [
                "campaign/nonready-report"
            ]


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
