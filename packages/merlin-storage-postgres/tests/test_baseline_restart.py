import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from merlin_agents import (
    AssignmentBudget,
    AssignmentContext,
    AssignmentTechnique,
    CampaignManifest,
    CampaignProfile,
    ContextDocument,
    DurableAgentService,
    ReviewedTargetAdapter,
    compile_attack_campaign,
)
from merlin_agents.campaign import compile_baseline_agenda
from merlin_contracts import (
    ModelCompleted,
    TextDelta,
    ToolCall,
    ToolContext,
    ToolEffectRecord,
    ToolEffectState,
    ToolResult,
    ToolSpec,
)
from merlin_storage_postgres import (
    CampaignApiSessionRepository,
    CampaignStore,
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    PostgresToolEffectStore,
    SessionRepository,
    ToolEffectRepository,
)
from merlin_tools import ToolBroker

SESSION_ID = "reviewed-target-session"
ORACLE_DIGEST = "sha256:" + "7" * 64
VALIDATION_CONTRACT = [
    ["reviewed_adapter_digest", "sha256:" + "a" * 64],
    ["initial_state_equivalence_digest", "sha256:" + "e" * 64],
]


def _assignment(stage: str, *, predecessor: str | None) -> dict[str, object]:
    assignment_id = f"campaign/baseline/{stage}"
    return {
        "id": assignment_id,
        "target_profile_id": "target-1",
        "agenda_id": "agenda/prompt-leak",
        "execution_stage": stage,
        "pair_id": "benign-greeting",
        "predecessor_assignment_id": predecessor,
        "oracle_digest": ORACLE_DIGEST,
        "semantic_technique_id": "prompt-leak",
        "target_session_policy": "same-reviewed-session",
        "comparison_supported": True,
        "comparison_policy": "reviewed-benign-differential-v1",
        "reviewed_adapter_digest": "sha256:" + "a" * 64,
        "strategy_contract": VALIDATION_CONTRACT,
        "comparison_dimensions": [["modality", "text"], ["turn", "single"], ["language", "und"]],
        "transform_bindings": [],
    }


def _digest_json(value: object) -> str:
    return (
        "sha256:"
        + sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )


def _api_effect(
    connection,
    *,
    session_id: str,
    attempt_id: str,
    stage: str,
    matched: bool,
    target_session_id: str = SESSION_ID,
    observed_turn: str = "single",
    observed_language: str = "und",
    latency_ms: int = 25,
    response_pad: str = "",
) -> None:
    arguments_digest = sha256(
        ("benign-control" if stage == "control" else "exact-direct-prompt").encode()
    ).hexdigest()
    adapter_digest = "sha256:" + "a" * 64
    binding = {
        "adapter_digest": adapter_digest,
        "step_id": "message",
        "oracle_digest": ORACLE_DIGEST,
        "pointer": "/gameStatus",
        "match_values": ["won"],
        "modality": "text",
        "turn": observed_turn,
        "language": observed_language,
        "purpose": "objective",
    }
    observation = {
        "schema": "merlin.api-oracle-observation.v2",
        **binding,
        "binding_digest": _digest_json(binding),
        "selected_digest": _digest_json("won" if matched else "playing"),
        "matched": matched,
    }
    observation["observation_digest"] = _digest_json(observation)
    exchange = {
        "id": "message",
        "interaction": True,
        "cleanup": False,
        "request": {"input": "control" if stage == "control" else "direct"},
        "response": {
            "gameStatus": "won" if matched else "playing",
            "response": "reviewed target response" + response_pad,
        },
        "outcome": "acknowledged",
        "latency_ms": latency_ms,
        "oracle_observation": observation,
        "oracle_observations": [observation],
    }
    repository = ToolEffectRepository(connection)
    started, created = repository.begin(
        ToolEffectRecord(
            idempotency_key=sha256(f"api:{stage}:{attempt_id}".encode()).hexdigest(),
            session_id=session_id,
            call_id=f"api-{stage}",
            agent_id=f"agent-{stage}",
            attempt_id=attempt_id,
            campaign_id="campaign/baseline",
            target_id="target-1",
            tool_name="target_request",
            effect="target_network",
            arguments_digest=arguments_digest,
            state=ToolEffectState.INTENT,
        )
    )
    assert created
    repository.finish(
        started.model_copy(
            update={
                "state": ToolEffectState.ACKNOWLEDGED,
                "result_content": json.dumps(exchange["response"]),
                "result_is_error": False,
                "result_metadata": {
                    "target_session_id": target_session_id,
                    "remote_target_session_id": target_session_id,
                    "target_profile_id": "target-1",
                    "adapter_digest": adapter_digest,
                    "target_exchanges": [exchange],
                },
            }
        )
    )


def _commit_api_stage(
    data_dir: Path,
    schedule_id: str,
    stage: str,
    *,
    matched: bool,
    target_session_id: str | None = None,
    observed_turn: str = "single",
    observed_language: str = "und",
    latency_ms: int = 25,
    response_pad: str = "",
) -> None:
    physical_session_id = target_session_id or f"{SESSION_ID}-{stage}"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = DurableSchedulerStore(connection)
            lease = store.claim(
                schedule_id,
                f"worker:campaign/baseline/{stage}",
                f"agent-{stage}",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            session_id = str(
                connection.execute(
                    "SELECT session_id FROM merlin_storage.scheduler_schedules WHERE id = %s",
                    (schedule_id,),
                ).fetchone()["session_id"]
            )
            _api_effect(
                connection,
                session_id=session_id,
                attempt_id=lease.id,
                stage=stage,
                matched=matched,
                target_session_id=physical_session_id,
                observed_turn=observed_turn,
                observed_language=observed_language,
                latency_ms=latency_ms,
                response_pad=response_pad,
            )
            store.commit_reducer(
                lease.id,
                lease.generation,
                schema_id="result/v1",
                result={"stage": stage, "worker_claim": "ignored"},
                checkpoint={"stage": stage},
                target_session_id=physical_session_id,
            )


def _effect(
    connection,
    *,
    session_id: str,
    attempt_id: str,
    stage: str,
    target_session_id: str,
) -> str:
    arguments_digest = sha256(
        ("benign-control" if stage == "control" else "exact-direct-prompt").encode()
    ).hexdigest()
    effect_id = sha256(f"{stage}:{attempt_id}".encode()).hexdigest()
    repository = ToolEffectRepository(connection)
    started, created = repository.begin(
        ToolEffectRecord(
            idempotency_key=effect_id,
            session_id=session_id,
            call_id=f"call-{stage}",
            agent_id=f"agent-{stage}",
            attempt_id=attempt_id,
            campaign_id="campaign/baseline",
            target_id="target-1",
            tool_name="target_chat",
            effect="target_network",
            arguments_digest=arguments_digest,
            state=ToolEffectState.INTENT,
        )
    )
    assert created is True
    repository.finish(
        started.model_copy(
            update={
                "state": ToolEffectState.ACKNOWLEDGED,
                "result_content": f"target response for {stage}",
                "result_is_error": False,
                "result_metadata": {
                    "target_session_id": target_session_id,
                    "target_profile_id": "target-1",
                    "target_exchanges": [{"id": stage, "interaction": True, "cleanup": False}],
                },
            }
        )
    )
    return arguments_digest


def _commit_stage(data_dir: Path, schedule_id: str, stage: str) -> None:
    target_session_id = f"{SESSION_ID}-{stage}"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = DurableSchedulerStore(connection)
            node_id = f"worker:campaign/baseline/{stage}"
            lease = store.claim(
                schedule_id,
                node_id,
                f"agent-{stage}",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            _effect(
                connection,
                session_id=str(
                    connection.execute(
                        "SELECT session_id FROM merlin_storage.scheduler_schedules WHERE id = %s",
                        (schedule_id,),
                    ).fetchone()["session_id"]
                ),
                attempt_id=lease.id,
                stage=stage,
                target_session_id=target_session_id,
            )
            store.commit_reducer(
                lease.id,
                lease.generation,
                schema_id="result/v1",
                result={"stage": stage},
                checkpoint={"stage": stage},
                target_session_id=target_session_id,
            )


def _browser_effects(
    connection,
    *,
    session_id: str,
    attempt_id: str,
    stage: str,
    submit_key: str,
    target_session_id: str,
    steps: tuple[int, int, int] = (1, 2, 3),
    extra_submit: bool = False,
    repeated_observation: bool = False,
) -> None:
    repository = ToolEffectRepository(connection)
    values = [
        (
            "browser_fill",
            {"selector": "#chat", "value": "exact-direct-prompt"},
            {"context_id": "ctx", "tab_ref": "tab", "step": steps[0]},
            {"kind": "fill"},
        ),
        (
            "browser_press",
            {"selector": "#chat", "key": submit_key},
            {"context_id": "ctx", "tab_ref": "tab", "step": steps[1]},
            {"kind": "press", "value": "Enter"},
        ),
        (
            "browser_snapshot",
            {},
            {
                "context_id": "ctx",
                "tab_ref": "tab",
                "step": steps[2],
                "target_id": "target-1",
            },
            None,
        ),
    ]
    if extra_submit:
        values.insert(
            2,
            (
                "browser_press",
                {"selector": "#chat", "key": "Enter", "duplicate": True},
                {"context_id": "ctx", "tab_ref": "tab", "step": steps[1]},
                {"kind": "press", "value": "Enter"},
            ),
        )
    if repeated_observation:
        values.append(
            (
                "browser_snapshot",
                {},
                {
                    "context_id": "ctx",
                    "tab_ref": "tab",
                    "step": steps[2] + 1,
                    "target_id": "target-1",
                },
                None,
            )
        )
    for index, (tool_name, arguments, content, action) in enumerate(values):
        encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
        effect_id = sha256(f"browser:{stage}:{index}:{attempt_id}".encode()).hexdigest()
        metadata: dict[str, object] = {
            "target_session_id": target_session_id,
            "target": "target-1",
        }
        if action is not None:
            metadata["browser_action"] = action
        started, created = repository.begin(
            ToolEffectRecord(
                idempotency_key=effect_id,
                session_id=session_id,
                call_id=f"{stage}-{index}",
                agent_id=f"agent-{stage}",
                attempt_id=attempt_id,
                campaign_id="campaign/baseline",
                target_id="target-1",
                tool_name=tool_name,
                effect=(
                    "target_read"
                    if tool_name in {"browser_snapshot", "browser_wait_for_text"}
                    else "target_network"
                ),
                arguments_digest=sha256(encoded).hexdigest(),
                state=ToolEffectState.INTENT,
            )
        )
        assert created
        repository.finish(
            started.model_copy(
                update={
                    "state": ToolEffectState.ACKNOWLEDGED,
                    "result_content": json.dumps(content),
                    "result_is_error": False,
                    "result_metadata": metadata,
                }
            )
        )


def test_baseline_ledger_restores_each_phase_without_resend(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres-baseline"
    assignments = (
        _assignment("control", predecessor=None),
        _assignment("direct", predecessor="campaign/baseline/control"),
        _assignment("replay", predecessor="campaign/baseline/direct"),
    )
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("baseline restart")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )

    _commit_stage(data_dir, schedule_id, "control")
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            restored = DurableSchedulerStore(connection)
            assert restored.baseline_predecessor_completed(
                schedule_id, "campaign/baseline/control", "control"
            )

    _commit_stage(data_dir, schedule_id, "direct")
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            restored = DurableSchedulerStore(connection)
            binding = restored.baseline_replay_binding(schedule_id, "campaign/baseline/direct")
            assert binding is not None
            assert binding["target_session_id"] == f"{SESSION_ID}-direct"
            assert binding["target_sequence"][0][0] == "target_chat"

    _commit_stage(data_dir, schedule_id, "replay")
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            restored = DurableSchedulerStore(connection)
            gate = restored.baseline_gate(schedule_id)
            assert gate["complete"] == ("agenda/prompt-leak",)
            assert gate["execution_complete"] == ("agenda/prompt-leak",)
            assert [item["stage"] for item in gate["agendas"]["agenda/prompt-leak"]] == [
                "control",
                "direct",
                "replay",
            ]
            counts = connection.execute(
                """SELECT stage, count(*) AS count
                   FROM merlin_storage.baseline_technique_attempts
                   WHERE schedule_id = %s GROUP BY stage ORDER BY stage""",
                (schedule_id,),
            ).fetchall()
            assert {row["stage"]: row["count"] for row in counts} == {
                "control": 1,
                "direct": 1,
                "replay": 1,
            }


def test_exact_replay_compares_canonical_target_arguments_not_wrapper_arguments(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-canonical-replay"
    assignments = (
        _assignment("control", predecessor=None),
        _assignment("direct", predecessor="campaign/baseline/control"),
        _assignment("replay", predecessor="campaign/baseline/direct"),
    )
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("canonical replay")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )
            canonical_digest = "sha256:" + sha256(b"effective-target-payload").hexdigest()
            for stage in ("control", "direct", "replay"):
                lease = store.claim(
                    schedule_id,
                    f"worker:campaign/baseline/{stage}",
                    f"agent-{stage}",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
                _effect(
                    connection,
                    session_id=str(session.id),
                    attempt_id=lease.id,
                    stage=stage,
                    target_session_id=f"{SESSION_ID}-{stage}",
                )
                if stage in {"direct", "replay"}:
                    wrapper_digest = sha256(f"wrapper-{stage}".encode()).hexdigest()
                    connection.execute(
                        """UPDATE merlin_storage.tool_effects
                           SET arguments_digest = %s,
                               result_metadata = result_metadata || %s::jsonb
                           WHERE attempt_id = %s""",
                        (
                            wrapper_digest,
                            json.dumps({"target_arguments_digest": canonical_digest}),
                            lease.id,
                        ),
                    )
                store.commit_reducer(
                    lease.id,
                    lease.generation,
                    schema_id="result/v1",
                    result={"stage": stage},
                    checkpoint={"stage": stage},
                    target_session_id=f"{SESSION_ID}-{stage}",
                )

            replay_stage = store.baseline_gate(schedule_id)["agendas"]["agenda/prompt-leak"][-1]
            assert replay_stage["outcome"] == "completed"
            assert replay_stage["prompt_digest"] == canonical_digest


def test_browser_baseline_repeated_observation_qualifies_one_exchange(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres-browser-repeated-observation"
    assignments = (
        _assignment("control", predecessor=None),
        _assignment("direct", predecessor="campaign/baseline/control"),
    )
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("repeated browser observation")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )
            campaigns = CampaignStore(connection)
            campaigns.prepare("campaign/baseline", str(session.id), {}, "sha256:reviewed-manifest")
            campaigns.begin_execution("campaign/baseline", "sha256:reviewed-manifest", schedule_id)
            control = store.claim(
                schedule_id,
                "worker:campaign/baseline/control",
                "control",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            _browser_effects(
                connection,
                session_id=str(session.id),
                attempt_id=control.id,
                stage="control",
                submit_key="Enter",
                target_session_id=f"{SESSION_ID}-control",
                repeated_observation=True,
            )
            store.commit_reducer(
                control.id,
                control.generation,
                schema_id="result/v1",
                result={"stage": "control"},
                checkpoint={},
                target_session_id=f"{SESSION_ID}-control",
            )

            assert store.baseline_predecessor_completed(
                schedule_id, "campaign/baseline/control", "control"
            )
            stage = store.baseline_gate(schedule_id)["agendas"]["agenda/prompt-leak"][0]
            assert stage["outcome"] == "completed"
            assert len(stage["witness_effect_ids"]) == 3


@pytest.mark.parametrize(
    ("matches", "verdict", "qualified"),
    (
        ((False, True), "positive", True),
        ((False, False), "ambiguous", False),
        ((True, True), "ambiguous", False),
        ((True, False), "ambiguous", False),
    ),
)
def test_api_pair_qualification_receipt_is_immutable_across_postgres_restart(
    tmp_path: Path,
    matches: tuple[bool, bool],
    verdict: str,
    qualified: bool,
) -> None:
    data_dir = tmp_path / f"postgres-api-pair-{verdict}-{matches[0]}-{matches[1]}"
    assignments = (
        _assignment("control", predecessor=None),
        _assignment("direct", predecessor="campaign/baseline/control"),
    )
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("API pair restart")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )

    _commit_api_stage(data_dir, schedule_id, "control", matched=matches[0])
    _commit_api_stage(data_dir, schedule_id, "direct", matched=matches[1])

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = DurableSchedulerStore(connection)
            first = store.baseline_gate(schedule_id)
            second = store.baseline_gate(schedule_id)
            assert first == second
            assert first["execution_complete"] == ("agenda/prompt-leak",)
            assert first["complete"] == ("agenda/prompt-leak",)
            assert first["comparison_qualified"] == ()
            assert first["positive"] == ()
            assert first["negative"] == ()
            assert first["ambiguous"] == ("agenda/prompt-leak",)
            comparison = first["comparisons"]["agenda/prompt-leak"]
            assert comparison["verdict"] == "ambiguous"
            assert comparison["recorded_verdict"] == "ambiguous"
            assert comparison["evaluation_uncertain"] is True
            assert comparison["oracle_binding_digest"].startswith("sha256:")
            assert len(comparison["stage_receipts"]) == 2
            assert (
                connection.execute(
                    "SELECT count(*) AS count FROM merlin_storage.baseline_comparison_receipts"
                ).fetchone()["count"]
                == 1
            )
            if matches == (False, True):
                connection.execute(
                    """UPDATE merlin_storage.baseline_comparison_receipts
                       SET stage_receipts = '[]'::jsonb WHERE schedule_id = %s""",
                    (schedule_id,),
                )
                connection.commit()
                with pytest.raises(ValueError, match="immutable"):
                    store.baseline_gate(schedule_id)
                connection.rollback()


def test_api_pair_accepts_isolated_physical_sessions_with_one_reviewed_identity(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-api-pair-isolated-physical-sessions"
    assignments = (
        _assignment("control", predecessor=None),
        _assignment("direct", predecessor="campaign/baseline/control"),
        _assignment("replay", predecessor="campaign/baseline/direct"),
    )
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("isolated API pair")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )

    for stage, matched in zip(("control", "direct", "replay"), (False, True, True), strict=True):
        _commit_api_stage(
            data_dir,
            schedule_id,
            stage,
            matched=matched,
            target_session_id=f"physical-{stage}",
        )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            gate = DurableSchedulerStore(connection).baseline_gate(schedule_id)
            stages = gate["agendas"]["agenda/prompt-leak"]
            assert [item["outcome"] for item in stages] == ["completed"] * 3
            assert [item["target_session_id"] for item in stages] == [
                "physical-control",
                "physical-direct",
                "physical-replay",
            ]
            assert gate["execution_complete"] == ("agenda/prompt-leak",)
            assert gate["comparisons"]["agenda/prompt-leak"]["reason"] == (
                "deterministic observations recorded; validator review is required"
            )


def test_api_pair_marks_reused_physical_session_as_contaminated_evidence(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-api-pair-reused-physical-session"
    assignments = (
        _assignment("control", predecessor=None),
        _assignment("direct", predecessor="campaign/baseline/control"),
    )
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("contaminated API pair")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )

    for stage, matched in (("control", False), ("direct", True)):
        _commit_api_stage(
            data_dir,
            schedule_id,
            stage,
            matched=matched,
            target_session_id="one-shared-physical-session",
        )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            stages = DurableSchedulerStore(connection).baseline_gate(schedule_id)["agendas"][
                "agenda/prompt-leak"
            ]
            assert stages[0]["outcome"] == "completed"
            assert stages[1]["outcome"] == "ambiguous"
            assert stages[1]["reason"] == "PAIR stages reused one physical target session"


def test_api_pair_marks_nonadjacent_reused_physical_session_as_contaminated_evidence(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-api-pair-reused-nonadjacent-physical-session"
    assignments = (
        _assignment("control", predecessor=None),
        _assignment("direct", predecessor="campaign/baseline/control"),
        _assignment("replay", predecessor="campaign/baseline/direct"),
    )
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("ABA-contaminated API pair")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )

    for stage, physical_session in (
        ("control", "physical-a"),
        ("direct", "physical-b"),
        ("replay", "physical-a"),
    ):
        _commit_api_stage(
            data_dir,
            schedule_id,
            stage,
            matched=stage != "control",
            target_session_id=physical_session,
        )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            stages = DurableSchedulerStore(connection).baseline_gate(schedule_id)["agendas"][
                "agenda/prompt-leak"
            ]
            assert [item["outcome"] for item in stages[:2]] == ["completed", "completed"]
            assert stages[2]["outcome"] == "ambiguous"
            assert stages[2]["reason"] == "PAIR stages reused one physical target session"


def test_api_pair_rejects_reviewed_adapter_identity_drift_across_stages(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-api-pair-reviewed-adapter-drift"
    control = _assignment("control", predecessor=None)
    direct = _assignment("direct", predecessor="campaign/baseline/control")
    direct["reviewed_adapter_digest"] = "sha256:" + "b" * 64
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("drifted API pair")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": (control, direct)},
                {"completed": []},
                [f"worker:{item['id']}" for item in (control, direct)],
                ready_nodes=[f"worker:{item['id']}" for item in (control, direct)],
            )

    _commit_api_stage(
        data_dir,
        schedule_id,
        "control",
        matched=False,
        target_session_id="physical-control",
    )
    _commit_api_stage(
        data_dir,
        schedule_id,
        "direct",
        matched=True,
        target_session_id="physical-direct",
    )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            stages = DurableSchedulerStore(connection).baseline_gate(schedule_id)["agendas"][
                "agenda/prompt-leak"
            ]
            assert stages[-1]["outcome"] == "ambiguous"
            assert stages[-1]["reason"] == "reviewed target or comparison identity drifted"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("target_profile_id", "target-2"),
        ("pair_id", "different-pair"),
        ("comparison_policy", "different-comparison-policy"),
        ("semantic_technique_id", "different-technique"),
        ("predecessor_assignment_id", "campaign/baseline/other/control"),
    ),
)
def test_api_pair_rejects_other_reviewed_identity_drift_across_stages(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    data_dir = tmp_path / f"postgres-api-pair-{field}-drift"
    control = _assignment("control", predecessor=None)
    direct = _assignment("direct", predecessor="campaign/baseline/control")
    direct[field] = value
    assignments = [control, direct]
    if field == "predecessor_assignment_id":
        unrelated_control = _assignment("control", predecessor=None)
        unrelated_control["id"] = value
        unrelated_control["agenda_id"] = "agenda/unrelated"
        assignments.append(unrelated_control)
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("drifted API pair identity")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in (control, direct)],
                ready_nodes=[f"worker:{item['id']}" for item in (control, direct)],
            )

    _commit_api_stage(
        data_dir,
        schedule_id,
        "control",
        matched=False,
        target_session_id="physical-control",
    )
    _commit_api_stage(
        data_dir,
        schedule_id,
        "direct",
        matched=True,
        target_session_id="physical-direct",
    )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            stages = DurableSchedulerStore(connection).baseline_gate(schedule_id)["agendas"][
                "agenda/prompt-leak"
            ]
            assert stages[-1]["outcome"] == "ambiguous"
            assert stages[-1]["reason"] == "reviewed target or comparison identity drifted"


def test_validation_requires_fresh_state_after_a_direct_source_attempt(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres-independent-validation"
    api_campaign_id = "campaign/api-independent-validation"
    assignments = (
        _assignment("control", predecessor=None),
        _assignment("direct", predecessor="campaign/baseline/control"),
    )
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("independent validation")
            source_schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )
            CampaignStore(connection).prepare(
                api_campaign_id,
                str(session.id),
                {"manifest": {"id": api_campaign_id}, "assignment_context": {}},
                "sha256:reviewed",
            )
            api_sessions = CampaignApiSessionRepository(connection)
            source_target_session_id = api_sessions.create(
                api_campaign_id, "target-1", "sha256:" + "a" * 64
            ).target_session_id
            api_sessions.finish_cleanup(api_campaign_id)

    _commit_api_stage(data_dir, source_schedule_id, "control", matched=False)
    _commit_api_stage(data_dir, source_schedule_id, "direct", matched=True)

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = DurableSchedulerStore(connection)
            api_sessions = CampaignApiSessionRepository(connection)
            validator_target_session_id = api_sessions.create(
                api_campaign_id, "target-1", "sha256:" + "a" * 64
            ).target_session_id
            assert validator_target_session_id != source_target_session_id
            connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET target_session_id = %s WHERE schedule_id = %s""",
                (source_target_session_id, source_schedule_id),
            )
            validator_node = "worker:campaign/wave-2/independent-validator"
            validation_schedule_id = store.create_schedule(
                session.id,
                {
                    "assignments": [
                        {
                            "id": validator_node.removeprefix("worker:"),
                            "target_profile_id": "target-1",
                            "strategy_contract": VALIDATION_CONTRACT,
                        }
                    ]
                },
                {"completed": []},
                [validator_node],
                ready_nodes=[validator_node],
            )
            validation = store.claim(
                validation_schedule_id,
                validator_node,
                "independent-validator",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            store.commit_reducer(
                validation.id,
                validation.generation,
                schema_id="result/v1",
                result={"validation_results": []},
                checkpoint={},
                target_session_id=validator_target_session_id,
            )

            assert store.validation_is_independent(source_schedule_id, validation_schedule_id)
            connection.execute(
                """UPDATE merlin_storage.scheduler_schedules
                   SET graph = jsonb_set(
                       graph,
                       '{assignments,0,strategy_contract,1,1}',
                       to_jsonb(%s::text)
                   ) WHERE id = %s""",
                ("sha256:" + "f" * 64, validation_schedule_id),
            )
            assert not store.validation_is_independent(source_schedule_id, validation_schedule_id)
            connection.execute(
                """UPDATE merlin_storage.scheduler_schedules
                   SET graph = jsonb_set(
                       graph,
                       '{assignments,0,strategy_contract,1,1}',
                       to_jsonb(%s::text)
                   ) WHERE id = %s""",
                ("sha256:" + "e" * 64, validation_schedule_id),
            )
            connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET target_session_id = %s WHERE id = %s""",
                (source_target_session_id, validation.id),
            )
            assert not store.validation_is_independent(source_schedule_id, validation_schedule_id)


@pytest.mark.parametrize("strategy_id", ("goat", "direct"))
def test_validation_requires_fresh_state_for_any_source_worker(
    tmp_path: Path, strategy_id: str
) -> None:
    data_dir = tmp_path / f"postgres-{strategy_id}-source-validation"
    source_node = f"worker:campaign/{strategy_id}-source"
    validator_node = "worker:campaign/wave-2/independent-validator"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("source worker validation")
            store = DurableSchedulerStore(connection)
            source_schedule_id = store.create_schedule(
                session.id,
                {
                    "assignments": [
                        {
                            "id": source_node.removeprefix("worker:"),
                            "target_profile_id": "target-1",
                            "strategy_contract": VALIDATION_CONTRACT,
                        }
                    ]
                },
                {"completed": []},
                [source_node],
                ready_nodes=[source_node],
            )
            source = store.claim(
                source_schedule_id,
                source_node,
                f"{strategy_id}-worker",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            store.commit_reducer(
                source.id,
                source.generation,
                schema_id="result/v1",
                result={"strategy_id": strategy_id},
                checkpoint={},
                target_session_id=SESSION_ID,
            )
            validation_schedule_id = store.create_schedule(
                session.id,
                {
                    "assignments": [
                        {
                            "id": validator_node.removeprefix("worker:"),
                            "target_profile_id": "target-1",
                            "strategy_contract": VALIDATION_CONTRACT,
                        }
                    ]
                },
                {"completed": []},
                [validator_node],
                ready_nodes=[validator_node],
            )
            validation = store.claim(
                validation_schedule_id,
                validator_node,
                "independent-validator",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            store.commit_reducer(
                validation.id,
                validation.generation,
                schema_id="result/v1",
                result={"validation_results": []},
                checkpoint={},
                target_session_id="fresh-equivalent-state",
            )

            assert store.validation_is_independent(source_schedule_id, validation_schedule_id)
            connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET target_session_id = %s WHERE id = %s""",
                (SESSION_ID, validation.id),
            )
            assert not store.validation_is_independent(source_schedule_id, validation_schedule_id)


def test_comparison_receipt_lookup_verifies_the_seal(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres-api-pair-receipt-lookup"
    assignments = (
        _assignment("control", predecessor=None),
        _assignment("direct", predecessor="campaign/baseline/control"),
        _assignment("replay", predecessor="campaign/baseline/direct"),
    )
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("API pair receipt lookup")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )

    _commit_api_stage(data_dir, schedule_id, "control", matched=False)
    _commit_api_stage(data_dir, schedule_id, "direct", matched=True)
    _commit_api_stage(data_dir, schedule_id, "replay", matched=True)

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = DurableSchedulerStore(connection)
            comparison = store.baseline_gate(schedule_id)["comparisons"]["agenda/prompt-leak"]
            digest = str(comparison["receipt_digest"])

            receipt = store.comparison_receipt(digest)
            assert receipt is not None
            assert receipt["receipt_digest"] == digest
            assert receipt["pair_id"] == comparison["pair_id"]
            assert receipt["verdict"] == "ambiguous"
            assert receipt["recorded_verdict"] == "ambiguous"
            assert receipt["evaluation_uncertain"] is True
            assert receipt["schedule_id"] == schedule_id
            assert store.comparison_receipt("sha256:" + "0" * 64) is None

            connection.execute(
                """UPDATE merlin_storage.baseline_comparison_receipts
                   SET verdict = 'negative' WHERE schedule_id = %s""",
                (schedule_id,),
            )
            connection.commit()
            assert store.comparison_receipt(digest) is None
            connection.rollback()


def test_api_pair_receipt_records_measurements_and_amplification_ratio(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-api-pair-amplification"
    assignments = (
        _assignment("control", predecessor=None),
        _assignment("direct", predecessor="campaign/baseline/control"),
        _assignment("replay", predecessor="campaign/baseline/direct"),
    )
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("API pair amplification")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )

    _commit_api_stage(data_dir, schedule_id, "control", matched=False, latency_ms=100)
    _commit_api_stage(
        data_dir, schedule_id, "direct", matched=True, latency_ms=400, response_pad="x" * 400
    )
    _commit_api_stage(
        data_dir, schedule_id, "replay", matched=True, latency_ms=400, response_pad="x" * 400
    )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            store = DurableSchedulerStore(connection)
            comparison = store.baseline_gate(schedule_id)["comparisons"]["agenda/prompt-leak"]
            receipts = {str(item["stage"]): item for item in comparison["stage_receipts"]}
            assert receipts["control"]["measurements"] == {
                "request_count": 1,
                "input_tokens_estimated": 5,
                "output_tokens_estimated": 17,
                "latency_ms": 100,
                "latency_complete": True,
            }
            direct = receipts["direct"]["measurements"]
            assert direct["request_count"] == 1
            assert direct["latency_ms"] == 400
            assert direct["latency_complete"] is True
            assert direct["output_tokens_estimated"] == 116
            amplification = comparison["amplification"]
            assert amplification is not None
            assert amplification["policy"] == "attack-over-control"
            assert amplification["latency_ratio"] == 4.0
            assert amplification["request_count_ratio"] == 1.0
            assert amplification["input_token_ratio"] == 1.0
            assert amplification["output_token_ratio"] == round(116 / 17, 6)
            receipt = store.comparison_receipt(str(comparison["receipt_digest"]))
            assert receipt is not None
            assert receipt["amplification"] == amplification


def test_exchange_measurements_and_amplification_helpers() -> None:
    from merlin_storage_postgres.scheduler_store import (
        _amplification_ratio,
        _comparison_amplification,
        _exchange_measurements,
    )

    measured = _exchange_measurements(
        (
            {"request": {"input": "abcd"}, "response": "wxyz", "latency_ms": 10},
            {"request": {"input": "efgh"}, "response": "wxyz", "latency_ms": 5},
        )
    )
    assert measured["request_count"] == 2
    assert measured["latency_ms"] == 15
    assert measured["latency_complete"] is True
    incomplete = _exchange_measurements(({"request": "a", "response": "b"},))
    assert incomplete["latency_ms"] is None
    assert incomplete["latency_complete"] is False

    assert _amplification_ratio(8, 4) == 2.0
    assert _amplification_ratio(0, 4) == 0.0
    assert _amplification_ratio(4, 0) is None
    assert _amplification_ratio(None, 4) is None
    assert _amplification_ratio(True, 4) is None

    receipts = (
        {"stage": "control", "measurements": measured},
        {"stage": "direct", "measurements": measured},
    )
    amplification = _comparison_amplification(receipts)
    assert amplification is not None
    assert amplification["latency_ratio"] == 1.0
    assert amplification["output_token_ratio"] == 1.0
    assert _comparison_amplification(({"stage": "direct", "measurements": measured},)) is None
    assert (
        _comparison_amplification(
            (
                {"stage": "control", "measurements": incomplete},
                {"stage": "direct", "measurements": measured},
            )
        )["latency_ratio"]
        is None
    )


def test_api_pair_accepts_target_capabilities_broader_than_attack_dimensions(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-api-pair-compatible-dimensions"
    assignments = []
    predecessor = None
    for stage in ("control", "direct", "replay"):
        assignment = _assignment(stage, predecessor=predecessor)
        assignment["comparison_dimensions"] = [
            ["modality", "text"],
            ["turn", "single"],
            ["language", "am"],
        ]
        assignments.append(assignment)
        predecessor = str(assignment["id"])
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("compatible dimensions")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )
    for stage in ("control", "direct", "replay"):
        _commit_api_stage(
            data_dir,
            schedule_id,
            stage,
            matched=False,
            observed_turn="multi",
            observed_language="en",
        )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            comparison = DurableSchedulerStore(connection).baseline_gate(schedule_id)[
                "comparisons"
            ]["agenda/prompt-leak"]
            assert comparison["verdict"] == "ambiguous"
            assert comparison["reason"] == (
                "deterministic observations recorded; validator review is required"
            )


def test_api_pair_blocks_when_observation_dimensions_differ_from_admitted_cell(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-api-pair-dimension-drift"
    assignments = []
    predecessor = None
    for stage in ("control", "direct", "replay"):
        assignment = _assignment(stage, predecessor=predecessor)
        assignment["comparison_dimensions"] = [
            ["modality", "image"],
            ["turn", "single"],
            ["language", "en"],
        ]
        assignments.append(assignment)
        predecessor = str(assignment["id"])
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("dimension drift")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )
    for stage, matched in zip(("control", "direct", "replay"), (False, True, True), strict=True):
        _commit_api_stage(data_dir, schedule_id, stage, matched=matched)

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            gate = DurableSchedulerStore(connection).baseline_gate(schedule_id)
            comparison = gate["comparisons"]["agenda/prompt-leak"]
            assert comparison["verdict"] == "ambiguous"
            assert "semantic dimensions drifted" in comparison["reason"]
            # Runtime drift is transient evidence, not a compile-time capability gap.
            assert comparison["permanent"] is False
            assert gate["comparison_qualified"] == ()


def test_api_pair_does_not_compare_catalogue_oracle_to_adapter_extractor(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-api-pair-distinct-oracle-domains"
    assignments = []
    predecessor = None
    for stage in ("control", "direct", "replay"):
        assignment = _assignment(stage, predecessor=predecessor)
        if stage == "control":
            assignment["oracle_digest"] = "sha256:" + "6" * 64
        assignments.append(assignment)
        predecessor = str(assignment["id"])
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("distinct oracle domains")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )
    for stage in ("control", "direct", "replay"):
        _commit_api_stage(data_dir, schedule_id, stage, matched=False)

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            comparison = DurableSchedulerStore(connection).baseline_gate(schedule_id)[
                "comparisons"
            ]["agenda/prompt-leak"]
            assert comparison["verdict"] == "ambiguous"
            assert comparison["reason"] == (
                "deterministic observations recorded; validator review is required"
            )


def test_api_pair_marks_compile_time_unsupported_comparison_permanent(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-api-pair-unsupported-permanent"
    assignments = []
    predecessor = None
    for stage in ("control", "direct", "replay"):
        assignment = _assignment(stage, predecessor=predecessor)
        assignment["comparison_supported"] = False
        assignment["comparison_block_reason"] = (
            "multi-turn comparison lacks a reviewed full-sequence adapter"
        )
        assignments.append(assignment)
        predecessor = str(assignment["id"])
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("unsupported comparison")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )
    for stage in ("control", "direct", "replay"):
        _commit_api_stage(data_dir, schedule_id, stage, matched=False)

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            gate = DurableSchedulerStore(connection).baseline_gate(schedule_id)
            comparison = gate["comparisons"]["agenda/prompt-leak"]
            assert comparison["verdict"] == "ambiguous"
            assert comparison["reason"] == (
                "deterministic observations recorded; validator review is required"
            )
            # Adapter/compiler inability is infrastructure state, not proof that
            # the target surface can never exist. Keep the cell retryable.
            assert comparison["permanent"] is False
            assert gate["comparison_qualified"] == ()


def test_api_pair_seals_terminal_blocked_receipt_without_executed_stages(
    tmp_path: Path,
) -> None:
    # A capability-blocked pair quarantines at claim and never records stage
    # rows; the gate must still reconcile it to a durable retryable receipt so
    # the unresolved cell remains visible without permitting saturation.
    data_dir = tmp_path / "postgres-api-pair-blocked-no-stages"
    assignments = []
    predecessor = None
    for stage in ("control", "direct"):
        assignment = _assignment(stage, predecessor=predecessor)
        assignment["comparison_supported"] = False
        assignment["comparison_block_reason"] = (
            "required delivery surface is pending harmless capability discovery: retrieval-context"
        )
        assignment["technique_ids"] = ["merlin-injection-taxonomy/cat/rag/retrieval-encoded"]
        assignments.append(assignment)
        predecessor = str(assignment["id"])
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("claim-blocked comparison")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            gate = DurableSchedulerStore(connection).baseline_gate(schedule_id)
            assert gate["comparisons"] == {}
            assert gate["comparison_qualified"] == ()
            # No target attempt means no synthetic content-evaluation receipt.
            again = DurableSchedulerStore(connection).baseline_gate(schedule_id)
            assert again == gate


def test_api_pair_dedupes_identical_terminal_blocked_receipts_across_agendas(
    tmp_path: Path,
) -> None:
    # Two agendas covering the same capability-blocked pair evaluate to the
    # identical content-addressed receipt; sealing must dedupe on the digest
    # instead of violating the unique constraint, and a restart replay of the
    # reconcile pass must stay a no-op.
    data_dir = tmp_path / "postgres-api-pair-blocked-dedupe"
    assignments = []
    for label in ("a", "b"):
        predecessor = None
        for stage in ("control", "direct"):
            assignment = _assignment(stage, predecessor=predecessor)
            assignment["id"] = f"campaign/baseline/{label}/{stage}"
            assignment["agenda_id"] = f"agenda/prompt-leak-{label}"
            assignment["comparison_supported"] = False
            assignment["comparison_block_reason"] = (
                "required delivery surface is pending harmless capability discovery: media-carried"
            )
            assignment["technique_ids"] = ["merlin-injection-taxonomy/cat/multimodal/image-carried"]
            assignments.append(assignment)
            predecessor = str(assignment["id"])
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("deduped blocked comparison")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )
            gate = DurableSchedulerStore(connection).baseline_gate(schedule_id)
            # A repeated reconcile pass on the same pass is idempotent.
            again = DurableSchedulerStore(connection).baseline_gate(schedule_id)
            assert again == gate
            assert gate["comparisons"] == {}
            assert (
                connection.execute(
                    "SELECT count(*) AS count FROM merlin_storage.baseline_comparison_receipts"
                ).fetchone()["count"]
                == 0
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            restored = DurableSchedulerStore(connection).baseline_gate(schedule_id)
            assert restored["comparisons"] == {}
            assert (
                connection.execute(
                    "SELECT count(*) AS count FROM merlin_storage.baseline_comparison_receipts"
                ).fetchone()["count"]
                == 0
            )


def test_api_pair_without_stages_and_supported_comparison_stays_open(
    tmp_path: Path,
) -> None:
    # A supported pair that simply has not run yet must not be sealed blocked.
    data_dir = tmp_path / "postgres-api-pair-supported-no-stages"
    assignments = []
    predecessor = None
    for stage in ("control", "direct"):
        assignment = _assignment(stage, predecessor=predecessor)
        assignments.append(assignment)
        predecessor = str(assignment["id"])
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("pending comparison")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )
            gate = DurableSchedulerStore(connection).baseline_gate(schedule_id)
            assert gate["comparisons"] == {}


@pytest.mark.parametrize(
    ("submit_key", "extra_submit", "steps", "expected_outcome", "expected_reason"),
    (
        ("Return", False, (1, 2, 3), "ambiguous", "exact replay binding diverged"),
        (
            "Enter",
            True,
            (1, 2, 3),
            "ambiguous",
            "target effect is unresolved or outside exact witness",
        ),
        (
            "Enter",
            False,
            (1, 1, 1),
            "ambiguous",
            "target effect is unresolved or outside exact witness",
        ),
    ),
)
def test_baseline_store_rejects_forged_browser_sequence(
    tmp_path: Path,
    submit_key: str,
    extra_submit: bool,
    steps: tuple[int, int, int],
    expected_outcome: str,
    expected_reason: str,
) -> None:
    data_dir = tmp_path / f"postgres-forged-{submit_key}-{extra_submit}-{steps[0]}"
    assignments = (
        _assignment("control", predecessor=None),
        _assignment("direct", predecessor="campaign/baseline/control"),
        _assignment("replay", predecessor="campaign/baseline/direct"),
    )
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("mutated submit")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"assignments": assignments},
                {"completed": []},
                [f"worker:{item['id']}" for item in assignments],
                ready_nodes=[f"worker:{item['id']}" for item in assignments],
            )
            control = store.claim(
                schedule_id,
                "worker:campaign/baseline/control",
                "control",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            _effect(
                connection,
                session_id=str(session.id),
                attempt_id=control.id,
                stage="control",
                target_session_id=f"{SESSION_ID}-control",
            )
            store.commit_reducer(
                control.id,
                control.generation,
                schema_id="result/v1",
                result={"stage": "control"},
                checkpoint={},
                target_session_id=f"{SESSION_ID}-control",
            )
            direct = store.claim(
                schedule_id,
                "worker:campaign/baseline/direct",
                "direct",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            _browser_effects(
                connection,
                session_id=str(session.id),
                attempt_id=direct.id,
                stage="direct",
                submit_key="Enter",
                target_session_id=f"{SESSION_ID}-direct",
            )
            store.commit_reducer(
                direct.id,
                direct.generation,
                schema_id="result/v1",
                result={"stage": "direct"},
                checkpoint={},
                target_session_id=f"{SESSION_ID}-direct",
            )
            replay = store.claim(
                schedule_id,
                "worker:campaign/baseline/replay",
                "replay",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            _browser_effects(
                connection,
                session_id=str(session.id),
                attempt_id=replay.id,
                stage="replay",
                submit_key=submit_key,
                target_session_id=f"{SESSION_ID}-replay",
                steps=steps,
                extra_submit=extra_submit,
            )
            store.commit_reducer(
                replay.id,
                replay.generation,
                schema_id="result/v1",
                result={"stage": "replay"},
                checkpoint={},
                target_session_id=f"{SESSION_ID}-replay",
            )

            gate = store.baseline_gate(schedule_id)
            assert gate["complete"] == gate["execution_complete"]
            assert gate["execution_complete"] == ()
            replay_stage = gate["agendas"]["agenda/prompt-leak"][-1]
            assert replay_stage["outcome"] == expected_outcome
            assert replay_stage["reason"] == expected_reason


@pytest.mark.asyncio
async def test_joined_durable_baseline_executes_neutral_control_and_exact_replay(
    tmp_path: Path,
) -> None:
    class Provider:
        capabilities = SimpleNamespace(vision=False, context_window_tokens=32_000)

        def __init__(self) -> None:
            self.system_prompts: list[str] = []

        async def stream(self, request):
            system = request.messages[0].content
            if not any(message.role == "tool" for message in request.messages):
                self.system_prompts.append(system)
                prompt = "benign-control" if "paired benign control" in system else "exact-direct"
                yield ToolCall(
                    call_id=f"call-{len(self.system_prompts)}",
                    name="target_chat",
                    arguments={"prompt": prompt},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            if "paired benign control" in system:
                yield TextDelta(
                    text='{"control_outcome":"unclear","evidence_refs":["target-receipt"]}'
                )
            else:
                yield TextDelta(
                    text=(
                        '{"attempted_techniques":[{"technique_id":"prompt-leak",'
                        '"outcome":"unclear","evidence_refs":["target-receipt"]}]}'
                    )
                )
            yield ModelCompleted(finish_reason="stop")

    class TargetChat:
        effect = "target_network"

        def __init__(self) -> None:
            self.prompts: list[str] = []

        def describe(self):
            return ToolSpec(name="target_chat", description="reviewed target", input_schema={})

        async def invoke(self, context, arguments):
            prompt = str(arguments["prompt"])
            self.prompts.append(prompt)
            return ToolResult(
                content=f"response:{prompt}",
                metadata={
                    "target_session_id": SESSION_ID,
                    "target_profile_id": "target-1",
                    "target_exchanges": [{"id": prompt, "interaction": True, "cleanup": False}],
                },
            )

    async def allow(*_args) -> bool:
        return True

    data_dir = tmp_path / "postgres-joined-baseline"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("joined baseline")
            direct_doc = ContextDocument.create("case:prompt-leak", "Reviewed direct guidance")
            control_doc = ContextDocument.create(
                "case:benign-greeting", "Send only a benign greeting"
            )
            manifest = CampaignManifest.create(
                id="campaign/joined",
                session_id=str(session.id),
                objective="Assess prompt handling",
                profile=CampaignProfile.RECOMMENDED,
                target=ReviewedTargetAdapter(
                    id="target-1",
                    interface="open_ai_compatible",
                    endpoint="https://target.invalid/v1",
                    model="target-model",
                    session_constraint="one reviewed session",
                ),
                provider_id="provider-1",
                provider_model="model-1",
                provider_effort="default",
                authority_ids=("authority-1",),
                catalogue=(direct_doc, control_doc),
                skill_documents=(),
                tool_ids=("target_chat",),
                budget=AssignmentBudget(max_turns=None, max_tokens=None, max_seconds=60),
                specialist_composition="autonomous",
            )
            context = AssignmentContext(
                target_profile_id="target-1",
                techniques=(
                    AssignmentTechnique(
                        "prompt-leak",
                        "Attempt reviewed direct prompt",
                        frozenset({"prompt", "injection", "text", "single"}),
                        (direct_doc.id,),
                        execution_role="direct",
                        control_technique_id="benign-greeting",
                        oracle_digest=ORACLE_DIGEST,
                        control_objective="Send only a benign greeting",
                        control_guidance_ids=(control_doc.id,),
                        control_oracle_digest=ORACLE_DIGEST,
                        comparison_policy="reviewed-benign-differential-v1",
                    ),
                ),
                tool_ids=("target_chat",),
                skill_ids=(),
                provider_id="provider-1",
                authority_ids=("authority-1",),
                budget=manifest.budget,
                input_schema='{"type":"object"}',
                output_schema='{"type":"object"}',
                provider_model="model-1",
            )
            compiled = compile_baseline_agenda(
                manifest,
                context,
                compile_attack_campaign(manifest, context),
            )
            provider = Provider()
            target = TargetChat()
            broker = ToolBroker(
                (target,),
                confirmer=allow,
                effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
            )
            agents = DurableAgentService(
                provider,
                broker,
                DurableSchedulerStore(connection),
                model="model-1",
                workspace_root=str(tmp_path),
            )
            live = await agents.submit(
                str(session.id),
                compiled.assignments,
                max_workers=1,
                minimum_workers=1,
                worker_contexts={item.assignment_id: item for item in compiled.worker_contexts},
            )
            assert live.execution is not None
            await live.execution
            gate = await agents.baseline_gate(live.id)
            assert gate["complete"] == gate["execution_complete"]
            assert gate["execution_complete"] == ()
            assert gate["comparisons"] == {}
            assert target.prompts == ["exact-direct"]
            assert "Worker profile: adversarial-explorer" in provider.system_prompts[0]
            direct_assignment = compiled.assignments[0]
            assert "finding_drafts" in direct_assignment.output_schema
            assert "attempted_techniques" in direct_assignment.output_schema

            scheduler = DurableSchedulerStore(connection)
            crash_schedule = scheduler.create_schedule(
                session.id,
                {"nodes": ["worker:crash"]},
                {"completed": []},
                ["worker:crash"],
                ready_nodes=["worker:crash"],
            )
            original = scheduler.claim(
                crash_schedule,
                "worker:crash",
                "original",
                lease_expires_at=datetime.now(UTC) + timedelta(milliseconds=100),
            )
            original_context = ToolContext(
                session_id=str(session.id),
                workspace_root=str(tmp_path),
                call_id="original-call",
                attempt_id=original.id,
                agent_id="worker:crash",
                target_id="target-1",
                effect_attempt_lineage=scheduler.replacement_attempt_lineage(original.id),
            )
            before_crash_calls = len(target.prompts)
            first = await broker.invoke("target_chat", original_context, {"prompt": "crash-window"})
            assert not first.is_error

            original_attempt_id = original.id
            original_generation = original.generation
            session_id = str(session.id)

    # A new embedded PostgreSQL process, connection, effect store, and broker reconstruct
    # replacement ancestry before any retry.  The acknowledged target effect is adopted.
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            scheduler = DurableSchedulerStore(connection)
            connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET lease_expires_at = now() - interval '1 second' WHERE id = %s""",
                (original_attempt_id,),
            )
            connection.commit()
            scheduler.expire(original_attempt_id, original_generation)
            replacement = scheduler.claim(
                crash_schedule,
                "worker:crash",
                "replacement",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            replacement_context = ToolContext(
                session_id=session_id,
                workspace_root=str(tmp_path),
                call_id="regenerated-call",
                attempt_id=replacement.id,
                agent_id="worker:crash",
                target_id="target-1",
                effect_attempt_lineage=scheduler.replacement_attempt_lineage(replacement.id),
            )
            restored_broker = ToolBroker(
                (target,),
                confirmer=allow,
                effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
            )
            adopted = await restored_broker.invoke(
                "target_chat", replacement_context, {"prompt": "crash-window"}
            )
            assert adopted.metadata["idempotent_replacement_adoption"] is True
            assert len(target.prompts) == before_crash_calls + 1

            # Exact acknowledged-effect identity is still adopted, but the old
            # effect lineage is not authority over a fresh authorized recovery
            # or mutation action. A changed action receives a distinct durable
            # effect identity and executes once.
            fresh_context = replacement_context.model_copy(
                update={"call_id": "fresh-recovery-call"}
            )
            fresh = await restored_broker.invoke(
                "target_chat", fresh_context, {"prompt": "fresh-recovery-action"}
            )
            assert fresh.is_error is False
            assert "idempotent_replacement_adoption" not in fresh.metadata
            assert len(target.prompts) == before_crash_calls + 2

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            restored = DurableSchedulerStore(connection)
            restored_gate = restored.baseline_gate(live.id)
            assert restored_gate["complete"] == restored_gate["execution_complete"]
            assert restored_gate["execution_complete"] == ()
            assert (
                sum(
                    str(item["node_id"]).startswith("worker:")
                    for item in restored.list_attempts(live.id)
                )
                == 1
            )
