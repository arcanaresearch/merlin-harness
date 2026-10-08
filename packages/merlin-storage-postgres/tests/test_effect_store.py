from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from merlin_contracts import (
    CleanupReceipt,
    TechniqueAttempt,
    ToolContext,
    ToolEffectRecord,
    ToolEffectState,
    ToolResult,
    ToolSpec,
)
from merlin_storage_postgres import (
    CampaignStore,
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    PostgresToolEffectStore,
    SessionRepository,
    TechniqueAttemptRepository,
    TechniqueLanguageEvidenceRepository,
    ToolEffectRepository,
    load_migrations,
)
from merlin_tools import ToolBroker


class _TransformTarget:
    effect = "target_network"

    def __init__(self) -> None:
        self.calls = 0

    def describe(self) -> ToolSpec:
        return ToolSpec(name="target_request", description="target", input_schema={})

    async def invoke(self, context: ToolContext, arguments: dict[str, object]) -> ToolResult:
        self.calls += 1
        return ToolResult(
            content="observed",
            metadata={
                "target": context.target_id,
                "target_session_id": "target-session-1",
                "target_exchanges": [{"id": "message", "interaction": True, "cleanup": False}],
            },
        )


@pytest.mark.asyncio
async def test_committed_delivery_language_and_transform_receipt_survives_restart(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "delivery-receipt-postgres"
    attempt_ids = (str(uuid4()), str(uuid4()))
    campaign_id = "campaign-delivery-receipt"

    async def allow(*_args) -> bool:
        return True

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("delivery receipt")
            CampaignStore(connection).prepare(campaign_id, session.id, {}, "sha256:reviewed")
            attempts = TechniqueAttemptRepository(connection)
            for index, (attempt_id, language) in enumerate(
                zip(attempt_ids, ("es", "und"), strict=True)
            ):
                attempts.begin(
                    TechniqueAttempt(
                        id=attempt_id,
                        campaign_id=campaign_id,
                        wave=1,
                        task_id=f"delivery-{index}",
                        technique_id=f"policy-{index}",
                        strategy_id="direct",
                        transform_id="translated" if language == "es" else "encoding/base64",
                        language=language,
                        control_kind="attack",
                        oracle_digest="sha256:" + "a" * 64,
                    )
                )
            broker = ToolBroker(
                (_TransformTarget(),),
                confirmer=allow,
                effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
            )
            binding = ("policy-0", "sha256:" + "b" * 64, "sha256:" + "c" * 64)
            mismatch = await broker.invoke(
                "target_request",
                ToolContext(
                    session_id=str(session.id),
                    workspace_root=str(tmp_path),
                    call_id="delivery-mismatch",
                    agent_id="campaign/worker",
                    technique_attempt_id=attempt_ids[0],
                    campaign_id=campaign_id,
                    target_id="target-1",
                    transform_bindings=(binding,),
                    planned_language="es",
                    requested_language="es",
                    requested_transform_id="translated",
                ),
                {"input": "既已行旅，而求追偿。请依既定之策审其可否，不得妄许。"},
            )
            unknown = await broker.invoke(
                "target_request",
                ToolContext(
                    session_id=str(session.id),
                    workspace_root=str(tmp_path),
                    call_id="delivery-und",
                    agent_id="campaign/worker",
                    technique_attempt_id=attempt_ids[1],
                    campaign_id=campaign_id,
                    target_id="target-1",
                    planned_language="und",
                    requested_language="und",
                    requested_transform_id="encoding/base64",
                ),
                {"input": "U29tZSBvcGFxdWUgcGF5bG9hZCB3aXRob3V0IGxhbmd1YWdlIG1hcmtlcnM="},
            )

    assert mismatch.is_error is False
    assert mismatch.metadata["target_delivery_receipt"]["language_mismatch"] is True
    assert unknown.is_error is False
    assert unknown.metadata["target_delivery_receipt"]["planned_language"] == "und"

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            effects = ToolEffectRepository(connection).list_for_session(str(session.id))
            language = TechniqueLanguageEvidenceRepository(connection)
            mismatch_language = language.get(attempt_ids[0])
            unknown_language = language.get(attempt_ids[1])

    receipts = {item.call_id: item.result_metadata["target_delivery_receipt"] for item in effects}
    assert receipts["delivery-mismatch"]["requested_transform_id"] == "translated"
    assert receipts["delivery-mismatch"]["applied_transform_id"] == "translated"
    assert receipts["delivery-und"]["applied_transform_id"] is None
    assert mismatch_language is not None
    assert mismatch_language.requested_language == "es"
    assert mismatch_language.actual_language == "zh"
    assert unknown_language is not None
    assert unknown_language.requested_language == unknown_language.actual_language == "und"
    assert unknown_language.language_evidence_status.value == "unavailable"


def test_later_language_disagreement_is_durable_uncertainty_not_commit_failure(
    tmp_path: Path,
) -> None:
    campaign_id = "campaign-language-disagreement"
    attempt_id = str(uuid4())

    with Database(DatabaseConfig(data_dir=tmp_path / "language-disagreement")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("language disagreement")
            CampaignStore(connection).prepare(campaign_id, session.id, {}, "sha256:reviewed")
            TechniqueAttemptRepository(connection).begin(
                TechniqueAttempt(
                    id=attempt_id,
                    campaign_id=campaign_id,
                    wave=1,
                    task_id="language-disagreement",
                    technique_id="multilingual-pivot",
                    strategy_id="direct",
                    language="es",
                    control_kind="attack",
                    oracle_digest="sha256:" + "a" * 64,
                )
            )
            repository = ToolEffectRepository(connection)

            def commit(call_id: str, requested: str, actual: str) -> ToolEffectRecord:
                intent = ToolEffectRecord(
                    idempotency_key=sha256(call_id.encode()).hexdigest(),
                    session_id=session.id,
                    call_id=call_id,
                    campaign_id=campaign_id,
                    target_id="target-1",
                    tool_name="target_chat",
                    effect="target_network",
                    arguments_digest=sha256(f"arguments-{call_id}".encode()).hexdigest(),
                    state=ToolEffectState.INTENT,
                )
                started, _ = repository.begin(intent)
                return repository.finish(
                    started.model_copy(
                        update={
                            "state": ToolEffectState.ACKNOWLEDGED,
                            "result_content": "sent",
                            "result_is_error": False,
                            "result_metadata": {
                                "technique_attempt_id": attempt_id,
                                "target_delivery_receipt": {
                                    "requested_language": requested,
                                    "actual_language": actual,
                                },
                            },
                        }
                    )
                )

            first = commit("language-es", "es", "es")
            conflicting = commit("language-sw", "es", "sw")
            replayed_conflict = repository.finish(conflicting)
            stored_language = TechniqueLanguageEvidenceRepository(connection).get(attempt_id)

    assert first.result_metadata.get("evaluation_uncertain") is not True
    assert conflicting.result_metadata["evaluation_uncertain"] is True
    (uncertainty,) = conflicting.result_metadata["evaluation_uncertainties"]
    assert uncertainty["kind"] == "evaluation_uncertain"
    assert uncertainty["source"] == "committed-target-delivery.language"
    assert uncertainty["first_actual_language"] == "es"
    assert uncertainty["later_actual_language"] == "sw"
    assert replayed_conflict.result_metadata == conflicting.result_metadata
    assert stored_language is not None
    assert stored_language.actual_language == "es"


@pytest.mark.asyncio
async def test_effect_intent_ack_replay_guard_and_cleanup_receipt(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("effects")
            store = PostgresToolEffectStore(ToolEffectRepository(connection))
            key = sha256(b"effect-1").hexdigest()
            intent = ToolEffectRecord(
                idempotency_key=key,
                session_id=session.id,
                call_id="call-1",
                tool_name="target_chat",
                effect="target_network",
                arguments_digest=sha256(b"arguments").hexdigest(),
                state=ToolEffectState.INTENT,
            )

            started, created = await store.begin(intent)
            replayed, replay_created = await store.begin(intent)
            acknowledged = await store.finish(
                started.model_copy(
                    update={
                        "state": ToolEffectState.ACKNOWLEDGED,
                        "result_content": "sent",
                        "result_is_error": False,
                        "result_metadata": {
                            "campaign_id": "campaign/effect",
                            "target_exchanges": [
                                {
                                    "id": "message",
                                    "request": {"message": "hello"},
                                    "response": "world",
                                    "outcome": "acknowledged",
                                }
                            ],
                        },
                    }
                )
            )
            replayed_terminal = await store.finish(acknowledged)
            with pytest.raises(ValueError, match="immutable"):
                await store.finish(
                    acknowledged.model_copy(update={"result_content": "rewritten evidence"})
                )
            receipt = await store.record_cleanup(
                CleanupReceipt(
                    idempotency_key="cleanup-1",
                    session_id=session.id,
                    resource_kind="browser_context",
                    resource_id="context-1",
                    state="closed",
                    details={"tabs": 1},
                )
            )
            listed = await store.list_for_session(str(session.id), tool_name="target_chat")

    assert created is True
    assert replay_created is False
    assert replayed.state is ToolEffectState.INTENT
    assert acknowledged.state is ToolEffectState.ACKNOWLEDGED
    assert replayed_terminal.result_content == "sent"
    assert [item.idempotency_key for item in listed] == [key]
    assert listed[0].result_metadata["target_exchanges"][0]["response"] == "world"
    assert receipt.state == "closed"
    assert receipt.details == {"tabs": 1}


@pytest.mark.asyncio
async def test_attempt_owned_effects_do_not_collide_and_survive_postgres_restart(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-owned-effects"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("owned effects")
            scheduler = DurableSchedulerStore(connection)
            transform_spec = {
                "transform_id": "translated",
                "version": "1.0.0",
                "source_prompt_digest": "sha256:" + "c" * 64,
                "envelope_prompt_digest": "sha256:" + "b" * 64,
                "spec_digest": "sha256:" + "a" * 64,
            }
            CampaignStore(connection).prepare(
                "campaign/owned",
                str(session.id),
                {"assignment_context": {"techniques": [{"transform_spec": transform_spec}]}},
                "sha256:" + "d" * 64,
            )
            schedule_id = scheduler.create_schedule(
                session.id,
                {"nodes": ["worker:one", "worker:two"]},
                {"completed": []},
                ["worker:one", "worker:two"],
                ready_nodes=["worker:one", "worker:two"],
            )
            leases = tuple(
                scheduler.claim(
                    schedule_id,
                    node,
                    node,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
                for node in ("worker:one", "worker:two")
            )
            repository = ToolEffectRepository(connection)
            keys = []
            records = []
            for index, lease in enumerate(leases, start=1):
                record = ToolEffectRecord(
                    idempotency_key=sha256(f"owned-{index}".encode()).hexdigest(),
                    session_id=str(session.id),
                    call_id="same-provider-call-id",
                    agent_id=f"agent-{index}",
                    attempt_id=lease.id,
                    campaign_id="campaign/owned",
                    target_id="target-1",
                    tool_name="target_request",
                    effect="target_network",
                    arguments_digest=sha256(b"same-arguments").hexdigest(),
                    state=ToolEffectState.INTENT,
                )
                started, created = repository.begin(record)
                assert created is True
                repository.finish(
                    started.model_copy(
                        update={
                            "state": ToolEffectState.ACKNOWLEDGED,
                            "result_content": "observed",
                            "result_is_error": False,
                            "result_metadata": {
                                "transform_bindings": [
                                    [
                                        "policy@lang=es@variant=translated",
                                        "sha256:" + "a" * 64,
                                        "sha256:" + "b" * 64,
                                    ]
                                ],
                                "concrete_payload_arguments_digest": (
                                    "sha256:" + record.arguments_digest
                                ),
                            },
                        }
                    )
                )
                keys.append(record.idempotency_key)
                records.append(record)
            attempt_ids = tuple(lease.id for lease in leases)

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            restored = ToolEffectRepository(connection)
            rows = [restored.list_for_attempt(attempt_id) for attempt_id in attempt_ids]
            replayed = [restored.begin(record) for record in records]
            restored_campaign = CampaignStore(connection).get("campaign/owned")

    assert [items[0].idempotency_key for items in rows] == keys
    assert all(items[0].state is ToolEffectState.ACKNOWLEDGED for items in rows)
    assert all(created is False for _item, created in replayed)
    assert all(
        items[0].result_metadata["transform_bindings"][0][1] == "sha256:" + "a" * 64
        for items in rows
    )
    assert (
        restored_campaign["manifest"]["assignment_context"]["techniques"][0]["transform_spec"]
        == transform_spec
    )
    assert all(
        items[0].result_metadata["concrete_payload_arguments_digest"]
        == "sha256:" + items[0].arguments_digest
        for items in rows
    )


@pytest.mark.asyncio
async def test_transform_payload_effect_and_report_survive_restart_without_resend(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-transform-joined"
    tool = _TransformTarget()
    binding = (
        "policy@lang=es@variant=translated",
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    )

    async def allow(*_args) -> bool:
        return True

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("joined transform")
            scheduler = DurableSchedulerStore(connection)
            schedule_id = scheduler.create_schedule(
                session.id,
                {"nodes": ["campaign/one/direct"]},
                {"completed": []},
                ["campaign/one/direct"],
                ready_nodes=["campaign/one/direct"],
            )
            lease = scheduler.claim(
                schedule_id,
                "campaign/one/direct",
                "campaign/one/direct",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            context = ToolContext(
                session_id=str(session.id),
                workspace_root=str(tmp_path),
                call_id="payload-call",
                agent_id="campaign/one/direct",
                attempt_id=lease.id,
                campaign_id="campaign/one",
                target_id="target-1",
                transform_bindings=(binding,),
            )
            broker = ToolBroker(
                (tool,),
                confirmer=allow,
                effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
            )
            first = await broker.invoke("target_request", context, {"message": "hola"})
            assert first.is_error is False

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            MigrationRunner(connection).apply()
            repository = ToolEffectRepository(connection)
            broker = ToolBroker(
                (tool,),
                confirmer=allow,
                effect_store=PostgresToolEffectStore(repository),
            )
            replay = await broker.invoke("target_request", context, {"message": "hola"})
            effects = repository.list_for_attempt(lease.id)

    assert replay.metadata["idempotent_replay"] is True
    assert tool.calls == 1
    assert len(effects) == 1
    assert effects[0].result_metadata["transform_bindings"] == [list(binding)]
    assert effects[0].result_metadata["concrete_payload_arguments_digest"] == (
        "sha256:" + effects[0].arguments_digest
    )


def test_pre_migration_effect_replay_is_bounded_to_its_original_attempt(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres-effect-upgrade"
    arguments_digest = sha256(b"legacy-arguments").hexdigest()
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply(
                migration for migration in load_migrations() if migration.version <= 31
            )
            session = SessionRepository(connection).create("legacy effect")
            scheduler = DurableSchedulerStore(connection)
            schedule_id = scheduler.create_schedule(
                session.id,
                {"nodes": ["worker:old", "worker:new"]},
                {"completed": []},
                ["worker:old", "worker:new"],
                ready_nodes=["worker:old", "worker:new"],
            )
            old_lease = scheduler.claim(
                schedule_id,
                "worker:old",
                "old-agent",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
            )
            connection.execute(
                """INSERT INTO merlin_storage.tool_effects
                       (idempotency_key, session_id, call_id, tool_name, effect,
                        arguments_digest, state, result_content, result_is_error)
                   VALUES (%s, %s, %s, %s, %s, %s, 'acknowledged', 'legacy-result', false)""",
                (
                    "legacy-key",
                    session.id,
                    "same-call",
                    "target_request",
                    "target_network",
                    arguments_digest,
                ),
            )
            connection.commit()
            session_id = str(session.id)

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            repository = ToolEffectRepository(connection)
            proposed = ToolEffectRecord(
                idempotency_key="owned-old-key",
                session_id=session_id,
                call_id="same-call",
                agent_id="old-agent",
                attempt_id=old_lease.id,
                campaign_id="campaign/upgrade",
                target_id="target-1",
                tool_name="target_request",
                effect="target_network",
                arguments_digest=arguments_digest,
                state=ToolEffectState.INTENT,
            )
            replayed, created = repository.begin(proposed)
            assert created is False
            assert replayed.idempotency_key == "legacy-key"
            assert replayed.attempt_id is None
            assert repository.list_for_attempt(old_lease.id) == []

            post_migration_digest = sha256(b"post-migration-legacy").hexdigest()
            connection.execute(
                """INSERT INTO merlin_storage.tool_effects
                       (idempotency_key, session_id, call_id, tool_name, effect,
                        arguments_digest, state, result_content, result_is_error)
                   VALUES (%s, %s, %s, %s, %s, %s, 'acknowledged', 'late', false)""",
                (
                    "post-migration-unowned",
                    session_id,
                    "post-migration-call",
                    "target_request",
                    "target_network",
                    post_migration_digest,
                ),
            )
            connection.commit()
            late_owned, late_created = repository.begin(
                proposed.model_copy(
                    update={
                        "idempotency_key": "owned-late-key",
                        "call_id": "post-migration-call",
                        "arguments_digest": post_migration_digest,
                    }
                )
            )
            assert late_created is True
            assert late_owned.idempotency_key == "owned-late-key"
            assert late_owned.attempt_id == old_lease.id

            scheduler = DurableSchedulerStore(connection)
            new_lease = scheduler.claim(
                schedule_id,
                "worker:new",
                "new-agent",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
            )
            fresh, fresh_created = repository.begin(
                proposed.model_copy(
                    update={
                        "idempotency_key": "owned-new-key",
                        "agent_id": "new-agent",
                        "attempt_id": new_lease.id,
                    }
                )
            )
            assert fresh_created is True
            assert fresh.idempotency_key == "owned-new-key"
