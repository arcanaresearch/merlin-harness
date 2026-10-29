import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from merlin_agents.goat import (
    GOAT_TOOLBOX_CATALOG_DIGEST,
    GoatContract,
    GoatState,
    trusted_goat_toolbox,
)
from merlin_contracts import ToolEffectRecord, ToolEffectState
from merlin_storage_postgres import (
    CampaignStore,
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    PostgresToolEffectStore,
    SessionRepository,
    ToolEffectRepository,
)
from merlin_storage_postgres.scheduler_store import TargetAdmissionLatchedError
from psycopg.types.json import Jsonb


def digest(value: object) -> str:
    return (
        "sha256:"
        + sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )


def _contract() -> tuple[GoatContract, dict[str, str]]:
    definitions = trusted_goat_toolbox()
    review = GoatContract(
        version="merlin.goat.v1",
        semantic_technique_id="semantic:goat",
        goal_digest=digest("goal"),
        toolbox=definitions,
        toolbox_catalog_digest=GOAT_TOOLBOX_CATALOG_DIGEST,
        terminal_oracle_digest=digest("oracle"),
        reviewed_adapter_digest=digest("adapter"),
        attacker_route_digest=digest(
            {
                "provider_id": "test",
                "model": "test",
                "role": "worker",
                "tool_transport": "buffered",
            }
        ),
        attacker_tool_transport="buffered",
        target_profile_id="target-1",
        session_policy="same-reviewed-session",
        modality="text",
        language="en",
        target_turns=2,
    )
    values = {
        "version": review.version,
        "semantic_technique_id": review.semantic_technique_id,
        "goal_digest": review.goal_digest,
        "toolbox_json": json.dumps(review.toolbox, separators=(",", ":")),
        "toolbox_catalog_digest": review.toolbox_catalog_digest,
        "terminal_oracle_digest": review.terminal_oracle_digest,
        "reviewed_adapter_digest": review.reviewed_adapter_digest,
        "attacker_route_digest": review.attacker_route_digest,
        "attacker_tool_transport": review.attacker_tool_transport,
        "target_profile_id": review.target_profile_id,
        "session_policy": review.session_policy,
        "modality": review.modality,
        "language": review.language,
        "target_turns": str(review.target_turns),
    }
    return review, values


def _observation(review: GoatContract, matched: bool) -> dict[str, object]:
    binding = {
        "adapter_digest": review.reviewed_adapter_digest,
        "step_id": "interaction",
        "oracle_digest": review.terminal_oracle_digest,
        "pointer": "/result",
        "match_values": ["matched"],
        "modality": "text",
        "turn": "multi",
        "language": "en",
        "purpose": "objective",
    }
    value = {
        "schema": "merlin.api-oracle-observation.v2",
        **binding,
        "binding_digest": digest(binding),
        "selected_digest": digest("reply"),
        "matched": matched,
    }
    value["observation_digest"] = digest(value)
    return value


def _goat_target_admission(
    *,
    campaign_id: str,
    session_id: str,
    schedule_id: str,
    assignment_id: str,
    adapter_digest: str,
) -> dict[str, object]:
    return {
        "schema": "merlin.target-admission.v1",
        "campaign_id": campaign_id,
        "session_id": session_id,
        "schedule_id": schedule_id,
        "node_id": f"worker:{assignment_id}",
        "assignment_id": assignment_id,
        "target_id": "target-1",
        "strategy_id": "goat",
        "reviewed_adapter_digest": adapter_digest,
        "target_effect_tool_ids": ("target_request",),
        "target_exchange_tool_ids": ("target_request",),
    }


def _goat_adoption_receipt(recovered: dict[str, object]) -> dict[str, object]:
    recovery = recovered["recovery"]
    assert isinstance(recovery, dict)
    body = {
        "schema": "merlin.goat-target-lane-adoption.v1",
        "prior_attempt_id": recovery["prior_attempt_id"],
        "checkpoint_attempt_id": recovery["checkpoint_attempt_id"],
        "effect_ids": list(recovery["effect_ids"]),
        "receipt_digests": list(recovery["receipt_digests"]),
        "pending_call_id": recovered["pending_call_id"],
        "pending_arguments_digest": recovered["pending_arguments_digest"],
    }
    return {**body, "recovery_digest": digest(body)}


def _expire_attempt(store: DurableSchedulerStore, connection, attempt) -> None:
    connection.execute(
        "UPDATE merlin_storage.scheduler_attempts "
        "SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
        (attempt.id,),
    )
    connection.commit()
    store.expire(attempt.id, attempt.generation)


async def _prepare_goat_lane_adoption_case(connection) -> dict[str, object]:
    review, values = _contract()
    campaign_id = "campaign/goat-lane-adoption"
    assignment_id = f"{campaign_id}/goat/one"
    assignment = {
        "id": assignment_id,
        "strategy_id": "goat",
        "strategy_contract": sorted(values.items()),
        "technique_ids": ["case:goat"],
        "target_profile_id": "target-1",
        "tool_ids": ["target_request"],
        "reviewed_adapter_digest": review.reviewed_adapter_digest,
        "comparison_supported": True,
        "provider_id": "test",
        "provider_model": "test",
    }
    session = SessionRepository(connection).create("GOAT lane adoption")
    store = DurableSchedulerStore(connection)
    node_id = f"worker:{assignment_id}"
    schedule_id = store.create_schedule(
        session.id,
        {"assignments": [assignment]},
        {"completed": []},
        [node_id],
        ready_nodes=[node_id],
    )
    CampaignStore(connection).prepare(
        campaign_id,
        str(session.id),
        {"manifest": {"id": campaign_id, "target": {"id": "target-1"}}},
        "sha256:reviewed",
    )
    admission = _goat_target_admission(
        campaign_id=campaign_id,
        session_id=str(session.id),
        schedule_id=schedule_id,
        assignment_id=assignment_id,
        adapter_digest=review.reviewed_adapter_digest,
    )
    first = store.claim(
        schedule_id,
        node_id,
        "agent-one",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        target_admission=admission,
    )
    plan = {
        "observation": "first observation",
        "thought": "first thought",
        "strategy": ["hypothetical"],
        "response": "first response",
    }
    arguments = {
        "input": plan["response"],
        "strategy_plan": plan,
        "strategy_turn_index": 0,
    }
    effect = ToolEffectRecord(
        idempotency_key=sha256(b"goat-lane-first-effect").hexdigest(),
        session_id=str(session.id),
        call_id="call-first",
        agent_id=assignment_id,
        attempt_id=first.id,
        campaign_id=campaign_id,
        target_id="target-1",
        tool_name="target_request",
        effect="target_network",
        arguments_digest=sha256(
            json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        arguments_payload=arguments,
        state=ToolEffectState.INTENT,
    )
    target_session_id = "api:goat-lane"
    physical_admission = {**admission, "target_session_id": target_session_id}
    permit_digest = store.final_target_effect_admitted(
        schedule_id,
        node_id,
        first.id,
        first.generation,
        tool_name=effect.tool_name,
        call_id=effect.call_id,
        target_admission=physical_admission,
        effect_intent=effect.model_dump(mode="json"),
    )
    effects = PostgresToolEffectStore(ToolEffectRepository(connection))
    adopted, adopted_now = await effects.adopt_permitted(effect, permit_digest)
    assert adopted_now is True
    await effects.finish(
        adopted.model_copy(
            update={
                "state": ToolEffectState.ACKNOWLEDGED,
                "result_content": "first reply",
                "result_is_error": False,
                "result_metadata": {
                    "campaign_id": campaign_id,
                    "target_profile_id": "target-1",
                    "target_session_id": target_session_id,
                    "target_exchanges": [
                        {
                            "id": "exchange-first",
                            "interaction": True,
                            "cleanup": False,
                            "response": {"reply": "first reply"},
                            "oracle_observations": [_observation(review, False)],
                        }
                    ],
                },
            }
        )
    )
    durable = store.goat_state(schedule_id, assignment_id, first.id)
    assert durable is not None
    pending_arguments = {
        "input": "pending response",
        "strategy_plan": {
            "observation": "pending observation",
            "thought": "pending thought",
            "strategy": ["hypothetical"],
            "response": "pending response",
        },
        "strategy_turn_index": 1,
    }
    pending = GoatState.restore(durable, review, first.id).with_pending(
        review, "call-pending", pending_arguments
    )
    store.commit_partial(
        first.id,
        first.generation,
        result={"worker_checkpoint": {"strategy_state": pending.dump()}},
    )
    _expire_attempt(store, connection, first)
    replacement = store.claim(
        schedule_id,
        node_id,
        "agent-two",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        target_admission=admission,
    )
    recovered = store.goat_state(
        schedule_id,
        assignment_id,
        replacement.id,
        pending.dump(),
    )
    assert recovered is not None
    return {
        "store": store,
        "schedule_id": schedule_id,
        "node_id": node_id,
        "assignment_id": assignment_id,
        "campaign_id": campaign_id,
        "session_id": str(session.id),
        "admission": admission,
        "physical_admission": physical_admission,
        "first": first,
        "replacement": replacement,
        "recovered": recovered,
    }


@pytest.mark.asyncio
async def test_goat_lane_adoption_rebinds_checkpoint_across_two_consecutive_crashes(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-goat-lane-rebind")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            case = await _prepare_goat_lane_adoption_case(connection)
            store = case["store"]
            assert isinstance(store, DurableSchedulerStore)
            current = case["replacement"]
            recovered = case["recovered"]
            assert isinstance(recovered, dict)

            generic_body = {
                "schema": "merlin.target-lane-recovery.v1",
                "prior_attempt_id": case["first"].id,
                "effect_ids": list(recovered["recovery"]["effect_ids"]),
                "outcome": "no_material_transition",
            }
            with pytest.raises(TargetAdmissionLatchedError):
                store.resolve_target_lane_recovery(
                    case["schedule_id"],
                    case["node_id"],
                    current.id,
                    current.generation,
                    target_admission=case["physical_admission"],
                    recovery={**generic_body, "recovery_digest": digest(generic_body)},
                )

            for crash_index in range(2):
                adopted_session = store.adopt_goat_target_lane(
                    case["schedule_id"],
                    case["node_id"],
                    current.id,
                    current.generation,
                    target_admission=case["physical_admission"],
                    recovery=_goat_adoption_receipt(recovered),
                )
                assert adopted_session == case["physical_admission"]["target_session_id"]
                checkpoint = connection.execute(
                    """SELECT payload #> '{worker_checkpoint,strategy_state}' AS state
                       FROM merlin_storage.scheduler_results WHERE attempt_id = %s""",
                    (current.id,),
                ).fetchone()
                assert checkpoint is not None
                rebound_state = checkpoint["state"]
                assert rebound_state["attempt_id"] == current.id
                assert rebound_state["pending_call_id"] == "call-pending"

                _expire_attempt(store, connection, current)
                current = store.claim(
                    case["schedule_id"],
                    case["node_id"],
                    f"agent-after-adoption-{crash_index}",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                    target_admission=case["admission"],
                )
                recovered = store.goat_state(
                    case["schedule_id"],
                    case["assignment_id"],
                    current.id,
                    rebound_state,
                )
                assert recovered is not None
                assert recovered["attempt_id"] == current.id
                assert recovered["pending_call_id"] == "call-pending"


@pytest.mark.asyncio
async def test_goat_lane_adoption_crosses_two_consecutive_pre_adoption_crashes(
    tmp_path: Path,
) -> None:
    with Database(
        DatabaseConfig(data_dir=tmp_path / "postgres-goat-lane-pre-adoption")
    ) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            case = await _prepare_goat_lane_adoption_case(connection)
            store = case["store"]
            assert isinstance(store, DurableSchedulerStore)
            current = case["replacement"]
            recovered = case["recovered"]
            assert isinstance(recovered, dict)
            original = connection.execute(
                """SELECT payload #> '{worker_checkpoint,strategy_state}' AS state
                   FROM merlin_storage.scheduler_results WHERE attempt_id = %s""",
                (case["first"].id,),
            ).fetchone()
            assert original is not None
            original_state = original["state"]

            for crash_index in range(2):
                assert recovered["recovery"]["checkpoint_attempt_id"] == case["first"].id
                assert recovered["recovery"]["prior_attempt_id"] == current.replaced_attempt_id
                _expire_attempt(store, connection, current)
                current = store.claim(
                    case["schedule_id"],
                    case["node_id"],
                    f"agent-before-adoption-{crash_index}",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                    target_admission=case["admission"],
                )
                recovered = store.goat_state(
                    case["schedule_id"],
                    case["assignment_id"],
                    current.id,
                    original_state,
                )
                assert recovered is not None
                assert recovered["pending_call_id"] == "call-pending"

            adopted_session = store.adopt_goat_target_lane(
                case["schedule_id"],
                case["node_id"],
                current.id,
                current.generation,
                target_admission=case["physical_admission"],
                recovery=_goat_adoption_receipt(recovered),
            )
            assert adopted_session == case["physical_admission"]["target_session_id"]
            checkpoint = connection.execute(
                """SELECT payload #> '{worker_checkpoint,strategy_state}' AS state
                   FROM merlin_storage.scheduler_results WHERE attempt_id = %s""",
                (current.id,),
            ).fetchone()
            assert checkpoint is not None
            assert checkpoint["state"]["attempt_id"] == current.id
            assert checkpoint["state"]["pending_call_id"] == "call-pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["pending_digest", "current_effect_and_permit"])
async def test_goat_lane_adoption_rejects_forged_or_extra_effect_authority(
    tmp_path: Path,
    tamper: str,
) -> None:
    with Database(
        DatabaseConfig(data_dir=tmp_path / f"postgres-goat-lane-reject-{tamper}")
    ) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            case = await _prepare_goat_lane_adoption_case(connection)
            store = case["store"]
            assert isinstance(store, DurableSchedulerStore)
            replacement = case["replacement"]
            recovery = _goat_adoption_receipt(case["recovered"])
            if tamper == "pending_digest":
                recovery["pending_arguments_digest"] = "sha256:" + "f" * 64
                recovery["recovery_digest"] = digest(
                    {key: value for key, value in recovery.items() if key != "recovery_digest"}
                )
            else:
                extra_arguments = {"input": "unreceipted current effect"}
                extra = ToolEffectRecord(
                    idempotency_key=sha256(b"goat-current-extra-effect").hexdigest(),
                    session_id=case["session_id"],
                    call_id="call-extra-current",
                    agent_id=case["assignment_id"],
                    attempt_id=replacement.id,
                    campaign_id=case["campaign_id"],
                    target_id="target-1",
                    tool_name="target_request",
                    effect="target_network",
                    arguments_digest=digest(extra_arguments),
                    arguments_payload=extra_arguments,
                    state=ToolEffectState.INTENT,
                )
                stored, created = ToolEffectRepository(connection).begin(extra)
                assert created is True
                connection.execute(
                    """INSERT INTO merlin_storage.target_effect_permits
                           (permit_digest, campaign_id, session_id, target_id,
                            target_session_id, schedule_id, node_id, assignment_id,
                            attempt_id, generation, call_id, tool_name, effect,
                            arguments_digest, arguments_payload, idempotency_key, state)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, 'intent')""",
                    (
                        digest("extra-current-permit"),
                        case["campaign_id"],
                        case["session_id"],
                        "target-1",
                        case["physical_admission"]["target_session_id"],
                        case["schedule_id"],
                        case["node_id"],
                        case["assignment_id"],
                        replacement.id,
                        replacement.generation,
                        stored.call_id,
                        stored.tool_name,
                        stored.effect,
                        stored.arguments_digest,
                        Jsonb(extra_arguments),
                        stored.idempotency_key,
                    ),
                )
                connection.commit()

            with pytest.raises(TargetAdmissionLatchedError):
                store.adopt_goat_target_lane(
                    case["schedule_id"],
                    case["node_id"],
                    replacement.id,
                    replacement.generation,
                    target_admission=case["physical_admission"],
                    recovery=recovery,
                )
            lanes = {
                str(row["target_session_id"]): row
                for row in connection.execute(
                    """SELECT target_session_id, lane_attempt_id::text,
                              lane_recovery_required
                       FROM merlin_storage.target_admission_latches
                       WHERE campaign_id = %s AND target_id = 'target-1'""",
                    (case["campaign_id"],),
                ).fetchall()
            }
            assert lanes[""]["lane_attempt_id"] == replacement.id
            physical = lanes[str(case["physical_admission"]["target_session_id"])]
            assert physical["lane_attempt_id"] == case["first"].id
            assert physical["lane_recovery_required"] is True


def test_goat_fixed_k_receipts_and_exact_history_survive_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres-goat"
    review, values = _contract()
    assignment_id = "campaign/fixture-id/goat/one"
    assignment = {
        "id": assignment_id,
        "strategy_id": "goat",
        "strategy_contract": sorted(values.items()),
        "technique_ids": ["case:goat"],
        "target_profile_id": "target-1",
        "comparison_supported": True,
        "provider_id": "test",
        "provider_model": "test",
    }
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("GOAT restart")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"assignments": [assignment]},
                {"completed": []},
                [f"worker:{assignment_id}"],
                ready_nodes=[f"worker:{assignment_id}"],
            )
            lease = store.claim(
                schedule_id,
                f"worker:{assignment_id}",
                "agent-one",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            repository = ToolEffectRepository(connection)
            for index, matched in enumerate((True, False)):
                plan = {
                    "observation": f"观察 {index}",
                    "thought": f"thought {index}",
                    "strategy": ["hypothetical"],
                    "response": f"回应 {index}",
                }
                arguments = {
                    "input": plan["response"],
                    "strategy_plan": plan,
                    "strategy_turn_index": index,
                }
                key = sha256(f"goat-effect-{index}".encode()).hexdigest()
                started, created = repository.begin(
                    ToolEffectRecord(
                        idempotency_key=key,
                        session_id=session.id,
                        call_id=f"call-{index}",
                        agent_id=assignment_id,
                        attempt_id=lease.id,
                        campaign_id="campaign/fixture-id",
                        target_id="target-1",
                        tool_name="target_request",
                        effect="target_network",
                        arguments_digest=sha256(
                            json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
                        ).hexdigest(),
                        arguments_payload=arguments,
                        state=ToolEffectState.INTENT,
                    )
                )
                assert created
                repository.finish(
                    started.model_copy(
                        update={
                            "state": ToolEffectState.ACKNOWLEDGED,
                            "result_content": f"回复 {index}",
                            "result_is_error": False,
                            "result_metadata": {
                                "campaign_id": "campaign/fixture-id",
                                "target_profile_id": "target-1",
                                "target_session_id": "api:campaign",
                                "target_exchanges": [
                                    {
                                        "id": f"exchange-{index}",
                                        "interaction": True,
                                        "cleanup": False,
                                        "response": {"reply": f"回复 {index}"},
                                        "oracle_observations": [_observation(review, matched)],
                                    }
                                ],
                            },
                        }
                    )
                )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            restored = DurableSchedulerStore(connection)
            gate = restored.goat_gate(schedule_id)
            strategy = gate["strategies"][assignment_id]
            assert strategy["evidence_verdict"] == "ambiguous"
            assert strategy["evaluation_uncertain"] is True
            assert strategy["steps"][0]["objective_matched"] is True
            assert strategy["execution_complete"] is True
            assert len(strategy["steps"]) == 2
            state = restored.goat_state(schedule_id, assignment_id, lease.id)
            assert state is not None
            restored_state = GoatState.restore(state, review, lease.id)
            assert restored_state.steps[0].plan["response"] == "回应 0"
            assert restored_state.steps[0].target_reply == {"reply": "回复 0"}
            assert restored_state.steps[0].call_id == "call-0"


def test_blocked_goat_cell_is_persisted_without_effect(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres-goat-blocked"
    assignment_id = "campaign/blocked/goat/one"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("blocked GOAT")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {
                    "assignments": [
                        {
                            "id": assignment_id,
                            "strategy_id": "goat",
                            "strategy_contract": [],
                            "technique_ids": ["case:goat"],
                            "comparison_supported": False,
                            "comparison_block_reason": "unsupported reviewed adapter",
                            "target_profile_id": "target-1",
                        }
                    ]
                },
                {"completed": []},
                [f"worker:{assignment_id}"],
                ready_nodes=[f"worker:{assignment_id}"],
            )
            gate = store.goat_gate(schedule_id)
            assert gate["blocked"] == ()
            assert assignment_id not in gate["strategies"]
            drifted = {
                **store.load_schedule(schedule_id)["graph"]["assignments"][0],
                "comparison_block_reason": "tampered reason",
            }
            store._reconcile_goat_assignment(schedule_id, drifted)
            assert (
                connection.execute(
                    "SELECT count(*) AS count FROM merlin_storage.tool_effects"
                ).fetchone()["count"]
                == 0
            )
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            gate = DurableSchedulerStore(connection).goat_gate(schedule_id)
            assert gate["blocked"] == ()
            assert assignment_id not in gate["strategies"]


def test_browser_goat_waits_for_acknowledged_effects_before_sealing_receipt(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-browser-goat"
    review, values = _contract()
    campaign_id = "campaign/browser-goat"
    assignment_id = f"{campaign_id}/goat/one"
    assignment = {
        "id": assignment_id,
        "strategy_id": "goat",
        "strategy_contract": sorted(values.items()),
        "technique_ids": ["case:goat"],
        "tool_ids": ["browser_chat_exchange"],
        "target_profile_id": "target-1",
        "comparison_supported": True,
        "provider_id": "test",
        "provider_model": "test",
    }
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("Browser GOAT lifecycle")
            store = DurableSchedulerStore(connection)
            node_id = f"worker:{assignment_id}"
            schedule_id = store.create_schedule(
                session.id,
                {"assignments": [assignment]},
                {"completed": []},
                [node_id],
                ready_nodes=[node_id],
            )
            lease = store.claim(
                schedule_id,
                node_id,
                "agent-one",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            connection.execute(
                "UPDATE merlin_storage.scheduler_nodes SET state = 'failed' "
                "WHERE schedule_id = %s AND node_id = %s",
                (schedule_id, node_id),
            )
            connection.commit()

            assert assignment_id not in store.goat_gate(schedule_id)["strategies"]

            repository = ToolEffectRepository(connection)
            for index, matched in enumerate((False, True)):
                plan = {
                    "observation": f"observation {index}",
                    "thought": f"thought {index}",
                    "strategy": ["hypothetical"],
                    "response": f"browser prompt {index}",
                }
                arguments = {
                    "prompt": plan["response"],
                    "strategy_plan": plan,
                    "strategy_turn_index": index,
                }
                effect, created = repository.begin(
                    ToolEffectRecord(
                        idempotency_key=sha256(f"browser-goat-{index}".encode()).hexdigest(),
                        session_id=session.id,
                        call_id=f"browser-call-{index}",
                        agent_id=assignment_id,
                        attempt_id=lease.id,
                        campaign_id=campaign_id,
                        target_id="target-1",
                        tool_name="browser_chat_exchange",
                        effect="browser_interaction",
                        arguments_digest=sha256(
                            json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
                        ).hexdigest(),
                        arguments_payload=arguments,
                        state=ToolEffectState.INTENT,
                    )
                )
                assert created
                if index == 0:
                    assert assignment_id not in store.goat_gate(schedule_id)["strategies"]
                observation = _observation(review, matched)
                observation["match_values"] = ["fixture reviewed win — exact"]
                binding_keys = (
                    "adapter_digest",
                    "step_id",
                    "oracle_digest",
                    "pointer",
                    "match_values",
                    "modality",
                    "turn",
                    "language",
                    "purpose",
                )
                observation["binding_digest"] = digest(
                    {key: observation[key] for key in binding_keys}
                )
                observation["observation_digest"] = digest(
                    {
                        key: value
                        for key, value in observation.items()
                        if key != "observation_digest"
                    }
                )
                repository.finish(
                    effect.model_copy(
                        update={
                            "state": ToolEffectState.ACKNOWLEDGED,
                            "result_content": f"browser reply {index}",
                            "result_is_error": False,
                            "result_metadata": {
                                "target_session_id": "browser:context-1",
                                "target_exchanges": [
                                    {
                                        "id": f"browser-exchange-{index}",
                                        "interaction": True,
                                        "cleanup": False,
                                        "response": f"browser reply {index}",
                                        "oracle_observations": [observation],
                                    }
                                ],
                            },
                        }
                    )
                )

            gate = store.goat_gate(schedule_id)
            receipt = gate["strategies"][assignment_id]
            assert gate["positive"] == ()
            assert gate["ambiguous"] == (assignment_id,)
            assert receipt["execution_complete"] is True
            assert receipt["evidence_verdict"] == "ambiguous"
            assert receipt["steps"][1]["objective_matched"] is True
            assert len(receipt["steps"]) == 2


def test_goat_replacement_adopts_later_pending_plan_and_rejects_intent_replay(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-goat-pending"
    review, values = _contract()
    assignment_id = "campaign/pending-goat/goat/one"
    campaign_id = "campaign/pending-goat"
    assignment = {
        "id": assignment_id,
        "strategy_id": "goat",
        "strategy_contract": sorted(values.items()),
        "technique_ids": ["case:goat"],
        "target_profile_id": "target-1",
        "comparison_supported": True,
        "provider_id": "test",
        "provider_model": "test",
    }
    first_plan = {
        "observation": "first observation",
        "thought": "first thought",
        "strategy": ["hypothetical"],
        "response": "first response",
    }
    first_arguments = {
        "input": first_plan["response"],
        "strategy_plan": first_plan,
        "strategy_turn_index": 0,
    }
    pending_plan = {
        "observation": "later observation",
        "thought": "later thought",
        "strategy": ["hypothetical"],
        "response": "准确的待处理回应",
    }
    pending_arguments = {
        "input": pending_plan["response"],
        "strategy_plan": pending_plan,
        "strategy_turn_index": 1,
    }

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("GOAT pending replacement")
            store = DurableSchedulerStore(connection)
            node_id = f"worker:{assignment_id}"
            schedule_id = store.create_schedule(
                session.id,
                {"assignments": [assignment]},
                {"completed": []},
                [node_id],
                ready_nodes=[node_id],
            )
            first = store.claim(
                schedule_id,
                node_id,
                "agent-one",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            repository = ToolEffectRepository(connection)
            effect, created = repository.begin(
                ToolEffectRecord(
                    idempotency_key=sha256(b"goat-first-effect").hexdigest(),
                    session_id=session.id,
                    call_id="call-first",
                    agent_id=assignment_id,
                    attempt_id=first.id,
                    campaign_id=campaign_id,
                    target_id="target-1",
                    tool_name="target_request",
                    effect="target_network",
                    arguments_digest=sha256(
                        json.dumps(first_arguments, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    arguments_payload=first_arguments,
                    state=ToolEffectState.INTENT,
                )
            )
            assert created
            repository.finish(
                effect.model_copy(
                    update={
                        "state": ToolEffectState.ACKNOWLEDGED,
                        "result_content": "first reply",
                        "result_is_error": False,
                        "result_metadata": {
                            "campaign_id": campaign_id,
                            "target_profile_id": "target-1",
                            "target_session_id": "api:campaign",
                            "target_exchanges": [
                                {
                                    "id": "exchange-first",
                                    "interaction": True,
                                    "cleanup": False,
                                    "response": {"reply": "first reply"},
                                    # The target effect is real, but this intentionally
                                    # malformed evaluator observation must remain unknown.
                                    # It cannot become a synthetic negative or prevent the
                                    # replacement from adopting the pending second turn.
                                    "oracle_observations": [
                                        {
                                            **_observation(review, False),
                                            "language": "fr",
                                        }
                                    ],
                                }
                            ],
                        },
                    }
                )
            )
            durable_prefix = store.goat_state(schedule_id, assignment_id, first.id)
            assert durable_prefix is not None
            assert durable_prefix["steps"][0]["objective_matched"] is None
            pending_state = GoatState.restore(durable_prefix, review, first.id).with_pending(
                review, "call-later", pending_arguments
            )

            connection.execute(
                "UPDATE merlin_storage.scheduler_attempts "
                "SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                (first.id,),
            )
            connection.commit()
            store.expire(first.id, first.generation)
            replacement = store.claim(
                schedule_id,
                node_id,
                "agent-two",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )

            adopted = store.goat_state(
                schedule_id,
                assignment_id,
                replacement.id,
                pending_state.dump(),
            )
            assert adopted is not None
            restored = GoatState.restore(adopted, review, replacement.id)
            assert len(restored.steps) == 1
            assert restored.pending_call_id == "call-later"
            assert restored.pending_arguments == pending_arguments
            assert restored.attempt_id == replacement.id

            pending_effect, created = repository.begin(
                ToolEffectRecord(
                    idempotency_key=sha256(b"goat-pending-effect").hexdigest(),
                    session_id=session.id,
                    call_id="call-later",
                    agent_id=assignment_id,
                    attempt_id=first.id,
                    campaign_id=campaign_id,
                    target_id="target-1",
                    tool_name="target_request",
                    effect="target_network",
                    arguments_digest=sha256(
                        json.dumps(
                            pending_arguments, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                    arguments_payload=pending_arguments,
                    state=ToolEffectState.INTENT,
                )
            )
            assert created
            assert pending_effect.state is ToolEffectState.INTENT
            connection.commit()
            ambiguous = store.goat_state(
                schedule_id,
                assignment_id,
                replacement.id,
                pending_state.dump(),
            )
            assert ambiguous is not None
            assert "pending_call_id" not in ambiguous


def _acknowledged_turn_effect(
    repository: ToolEffectRepository,
    *,
    key: bytes,
    session_id: str,
    call_id: str,
    assignment_id: str,
    attempt_id: str,
    campaign_id: str,
    turn_index: int,
    matched: bool,
    review: GoatContract,
    result_is_error: bool = False,
    extra_metadata: dict[str, object] | None = None,
) -> None:
    plan = {
        "observation": f"observation {call_id}",
        "thought": f"thought {call_id}",
        "strategy": ["hypothetical"],
        "response": f"response {call_id}",
    }
    arguments = {
        "input": plan["response"],
        "strategy_plan": plan,
        "strategy_turn_index": turn_index,
    }
    started, created = repository.begin(
        ToolEffectRecord(
            idempotency_key=sha256(key).hexdigest(),
            session_id=session_id,
            call_id=call_id,
            agent_id=assignment_id,
            attempt_id=attempt_id,
            campaign_id=campaign_id,
            target_id="target-1",
            tool_name="target_request",
            effect="target_network",
            arguments_digest=sha256(
                json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            arguments_payload=arguments,
            state=ToolEffectState.INTENT,
        )
    )
    assert created
    metadata: dict[str, object] = {
        "campaign_id": campaign_id,
        "target_profile_id": "target-1",
        "target_session_id": "api:campaign",
        "target_exchanges": [
            {
                "id": f"exchange-{call_id}",
                "interaction": True,
                "cleanup": False,
                "response": {"reply": f"reply {call_id}"},
                "oracle_observations": [_observation(review, matched)],
            }
        ],
    }
    metadata.update(extra_metadata or {})
    repository.finish(
        started.model_copy(
            update={
                "state": ToolEffectState.ACKNOWLEDGED,
                "result_content": f"reply {call_id}",
                "result_is_error": result_is_error,
                "result_metadata": metadata,
            }
        )
    )


def _goat_assignment(assignment_id: str, values: dict[str, str]) -> dict[str, object]:
    return {
        "id": assignment_id,
        "strategy_id": "goat",
        "strategy_contract": sorted(values.items()),
        "technique_ids": ["case:goat"],
        "target_profile_id": "target-1",
        "comparison_supported": True,
        "provider_id": "test",
        "provider_model": "test",
    }


def test_goat_gate_seals_blocked_terminal_for_exhausted_target_session(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-goat-exhausted"
    review, values = _contract()
    assignment_id = "campaign/exhausted-goat/goat/one"
    campaign_id = "campaign/exhausted-goat"
    assignment = _goat_assignment(assignment_id, values)
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("GOAT exhausted session")
            store = DurableSchedulerStore(connection)
            node_id = f"worker:{assignment_id}"
            schedule_id = store.create_schedule(
                session.id,
                {"assignments": [assignment]},
                {"completed": []},
                [node_id],
                ready_nodes=[node_id],
            )
            lease = store.claim(
                schedule_id,
                node_id,
                "agent-one",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            repository = ToolEffectRepository(connection)
            _acknowledged_turn_effect(
                repository,
                key=b"goat-exhausted-turn-0",
                session_id=session.id,
                call_id="call-first",
                assignment_id=assignment_id,
                attempt_id=lease.id,
                campaign_id=campaign_id,
                turn_index=0,
                matched=False,
                review=review,
            )
            # The interaction pointer miss persisted across the single session
            # rotation and the observed body was an error/rate-limit envelope.
            _acknowledged_turn_effect(
                repository,
                key=b"goat-exhausted-turn-1",
                session_id=session.id,
                call_id="call-capped",
                assignment_id=assignment_id,
                attempt_id=lease.id,
                campaign_id=campaign_id,
                turn_index=1,
                matched=False,
                review=review,
                result_is_error=True,
                extra_metadata={
                    "infrastructure_failure_category": "missing_response_pointer",
                    "response_digest": "sha256:" + "1" * 64,
                    "target_session_exhausted": True,
                },
            )
            # The worker finished without the gate adjudicating the cell.
            connection.execute(
                "UPDATE merlin_storage.scheduler_nodes SET state = 'completed' "
                "WHERE schedule_id = %s AND node_id = %s",
                (schedule_id, node_id),
            )
            connection.commit()

            gate = store.goat_gate(schedule_id)
            receipt = gate["strategies"][assignment_id]
            assert gate["blocked"] == ()
            assert gate["ambiguous"] == (assignment_id,)
            assert receipt["execution_terminal"] == "blocked"
            assert receipt["evidence_verdict"] == "ambiguous"
            assert receipt["execution_complete"] is False
            assert "rate/message cap exhausted" in receipt["block_reason"]
            assert "sha256:" + "1" * 64 in receipt["block_reason"]

            again = store.goat_gate(schedule_id)
            assert again["strategies"][assignment_id]["receipt_digest"] == receipt["receipt_digest"]


def test_goat_state_seals_terminal_for_intent_crossed_pending_effect(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-goat-intent-crossed"
    review, values = _contract()
    assignment_id = "campaign/intent-crossed/goat/one"
    campaign_id = "campaign/intent-crossed"
    assignment = _goat_assignment(assignment_id, values)
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("GOAT INTENT crossing")
            store = DurableSchedulerStore(connection)
            node_id = f"worker:{assignment_id}"
            schedule_id = store.create_schedule(
                session.id,
                {"assignments": [assignment]},
                {"completed": []},
                [node_id],
                ready_nodes=[node_id],
            )
            lease = store.claim(
                schedule_id,
                node_id,
                "agent-one",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            repository = ToolEffectRepository(connection)
            _acknowledged_turn_effect(
                repository,
                key=b"goat-intent-crossed-turn-0",
                session_id=session.id,
                call_id="call-first",
                assignment_id=assignment_id,
                attempt_id=lease.id,
                campaign_id=campaign_id,
                turn_index=0,
                matched=False,
                review=review,
            )
            # The attempt died after the target INTENT for the next turn: the
            # effect never reached an acknowledged step receipt. It belongs to
            # a rotated session, so the reconciliation effects query (scoped to
            # the schedule session) cannot see it while the pending-effect
            # lookup by attempt/call can — the run24 poisoned-retry shape.
            rotated = SessionRepository(connection).create("GOAT INTENT rotated")
            pending_plan = {
                "observation": "pending observation",
                "thought": "pending thought",
                "strategy": ["hypothetical"],
                "response": "pending response",
            }
            pending_arguments = {
                "input": pending_plan["response"],
                "strategy_plan": pending_plan,
                "strategy_turn_index": 1,
            }
            _pending, created = repository.begin(
                ToolEffectRecord(
                    idempotency_key=sha256(b"goat-intent-crossed-turn-1").hexdigest(),
                    session_id=rotated.id,
                    call_id="call-later",
                    agent_id=assignment_id,
                    attempt_id=lease.id,
                    campaign_id=campaign_id,
                    target_id="target-1",
                    tool_name="target_request",
                    effect="target_network",
                    arguments_digest=sha256(
                        json.dumps(
                            pending_arguments, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                    arguments_payload=pending_arguments,
                    state=ToolEffectState.INTENT,
                )
            )
            assert created
            connection.commit()

            pending_state = {
                "schema": "merlin.goat-state.v1",
                "attempt_id": str(lease.id),
                "pending_call_id": "call-later",
                "pending_arguments": pending_arguments,
            }
            state = store.goat_state(schedule_id, assignment_id, lease.id, pending_state)
            assert state is not None
            assert state["terminal_reason"] == "ambiguous"
            assert "pending_call_id" not in state

            gate = store.goat_gate(schedule_id)
            receipt = gate["strategies"][assignment_id]
            assert receipt["execution_terminal"] == "ambiguous"
            assert receipt["evidence_verdict"] == "ambiguous"
            assert receipt["execution_complete"] is False
            assert receipt["block_reason"] is None
            assert len(receipt["step_receipt_digests"]) == 1

            again = store.goat_state(schedule_id, assignment_id, lease.id, pending_state)
            assert again is not None
            assert again["terminal_reason"] == "ambiguous"
            assert "pending_call_id" not in again


def test_goat_gate_seals_negative_for_completed_run_over_admitted_dual_transports(
    tmp_path: Path,
) -> None:
    # The admitted toolbox lists both target transports; the worker committed
    # every turn over target_request with the objective oracle never matching.
    # Reconciliation must follow the committed effects, not the admission list.
    data_dir = tmp_path / "postgres-goat-completed-negative"
    review, values = _contract()
    assignment_id = "campaign/completed-goat/goat/one"
    campaign_id = "campaign/completed-goat"
    assignment = {
        **_goat_assignment(assignment_id, values),
        "tool_ids": ["target_request", "browser_chat_exchange"],
    }
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("GOAT completed negative")
            store = DurableSchedulerStore(connection)
            node_id = f"worker:{assignment_id}"
            schedule_id = store.create_schedule(
                session.id,
                {"assignments": [assignment]},
                {"completed": []},
                [node_id],
                ready_nodes=[node_id],
            )
            lease = store.claim(
                schedule_id,
                node_id,
                "agent-one",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            repository = ToolEffectRepository(connection)
            for turn_index in range(2):
                _acknowledged_turn_effect(
                    repository,
                    key=f"goat-completed-turn-{turn_index}".encode(),
                    session_id=session.id,
                    call_id=f"call-{turn_index}",
                    assignment_id=assignment_id,
                    attempt_id=lease.id,
                    campaign_id=campaign_id,
                    turn_index=turn_index,
                    matched=False,
                    review=review,
                )
            connection.execute(
                "UPDATE merlin_storage.scheduler_nodes SET state = 'completed' "
                "WHERE schedule_id = %s AND node_id = %s",
                (schedule_id, node_id),
            )
            connection.commit()

            gate = store.goat_gate(schedule_id)
            receipt = gate["strategies"][assignment_id]
            assert receipt["execution_terminal"] == "max-turns"
            assert receipt["execution_complete"] is True
            assert receipt["evidence_verdict"] == "ambiguous"
            assert gate["negative"] == ()
            assert gate["ambiguous"] == (assignment_id,)
            assert gate["complete"] == (assignment_id,)
            assert gate["execution_complete"] == (assignment_id,)
            assert len(receipt["step_receipt_digests"]) == 2

            again = store.goat_gate(schedule_id)
            assert again["strategies"][assignment_id]["receipt_digest"] == receipt["receipt_digest"]


def test_goat_state_does_not_seal_live_attempt_pending_effect_on_schedule_session(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-goat-live-pending"
    review, values = _contract()
    assignment_id = "campaign/live-pending/goat/one"
    campaign_id = "campaign/live-pending"
    assignment = _goat_assignment(assignment_id, values)
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("GOAT live pending")
            store = DurableSchedulerStore(connection)
            node_id = f"worker:{assignment_id}"
            schedule_id = store.create_schedule(
                session.id,
                {"assignments": [assignment]},
                {"completed": []},
                [node_id],
                ready_nodes=[node_id],
            )
            lease = store.claim(
                schedule_id,
                node_id,
                "agent-one",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            repository = ToolEffectRepository(connection)
            _acknowledged_turn_effect(
                repository,
                key=b"goat-live-pending-turn-0",
                session_id=session.id,
                call_id="call-first",
                assignment_id=assignment_id,
                attempt_id=lease.id,
                campaign_id=campaign_id,
                turn_index=0,
                matched=False,
                review=review,
            )
            # The live attempt re-enters goat_state after its turn-1 effect
            # committed on the schedule session but before the acknowledge
            # landed. The reconcile query can still see this effect, so the
            # intent-crossed seal must not fire: sealing here would journal an
            # empty ambiguous terminal receipt ahead of the turn's own
            # reconcile and poison the assignment permanently.
            pending_plan = {
                "observation": "pending observation",
                "thought": "pending thought",
                "strategy": ["hypothetical"],
                "response": "pending response",
            }
            pending_arguments = {
                "input": pending_plan["response"],
                "strategy_plan": pending_plan,
                "strategy_turn_index": 1,
            }
            _pending, created = repository.begin(
                ToolEffectRecord(
                    idempotency_key=sha256(b"goat-live-pending-turn-1").hexdigest(),
                    session_id=session.id,
                    call_id="call-later",
                    agent_id=assignment_id,
                    attempt_id=lease.id,
                    campaign_id=campaign_id,
                    target_id="target-1",
                    tool_name="target_request",
                    effect="target_network",
                    arguments_digest=sha256(
                        json.dumps(
                            pending_arguments, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                    arguments_payload=pending_arguments,
                    state=ToolEffectState.INTENT,
                )
            )
            assert created
            connection.commit()

            pending_state = {
                "schema": "merlin.goat-state.v1",
                "attempt_id": str(lease.id),
                "pending_call_id": "call-later",
                "pending_arguments": pending_arguments,
            }
            state = store.goat_state(schedule_id, assignment_id, lease.id, pending_state)
            assert state is not None
            assert state["terminal_reason"] is None
            assert "pending_call_id" not in state
            sealed = connection.execute(
                """SELECT COUNT(*) AS count FROM merlin_storage.goat_terminal_receipts
                   WHERE schedule_id = %s AND assignment_id = %s""",
                (schedule_id, assignment_id),
            ).fetchone()
            assert sealed["count"] == 0

            # Once the turn-1 effect acknowledges, the next reconcile converts
            # it into the second step receipt and the run seals max-turns
            # through the normal path instead of the intent-crossed seal.
            repository.finish(
                _pending.model_copy(
                    update={
                        "state": ToolEffectState.ACKNOWLEDGED,
                        "result_content": "reply call-later",
                        "result_is_error": False,
                        "result_metadata": {
                            "campaign_id": campaign_id,
                            "target_profile_id": "target-1",
                            "target_session_id": "api:campaign",
                            "target_exchanges": [
                                {
                                    "id": "exchange-call-later",
                                    "interaction": True,
                                    "cleanup": False,
                                    "response": {"reply": "reply call-later"},
                                    "oracle_observations": [_observation(review, False)],
                                }
                            ],
                        },
                    }
                )
            )
            connection.commit()
            again = store.goat_state(schedule_id, assignment_id, lease.id, pending_state)
            assert again is not None
            assert len(again["steps"]) == 2
            assert again["terminal_reason"] is None
            sealed = connection.execute(
                """SELECT execution_terminal, evidence_verdict
                   FROM merlin_storage.goat_terminal_receipts
                   WHERE schedule_id = %s AND assignment_id = %s""",
                (schedule_id, assignment_id),
            ).fetchone()
            assert sealed["execution_terminal"] == "max-turns"
            assert sealed["evidence_verdict"] == "ambiguous"
