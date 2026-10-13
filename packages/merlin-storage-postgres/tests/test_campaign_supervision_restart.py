import asyncio
import json
import re
from dataclasses import asdict
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from merlin_agents import (
    AssignmentBudget,
    AssignmentContext,
    AssignmentTechnique,
    CampaignManifest,
    CampaignPreparation,
    CampaignProfile,
    CampaignService,
    ContextDocument,
    DurableAgentService,
    FrontierHypothesis,
    LeadAssessment,
    LeadFollowUp,
    ObjectiveGateAction,
    ObjectiveGateBudget,
    ObjectiveGateOperator,
    ObjectiveGatePredicate,
    ObjectiveGateRetention,
    ObjectivePhaseGate,
    ReviewedTargetAdapter,
    WorkerAssignment,
    compile_initial_campaign,
)
from merlin_agents.campaign import (
    compile_attack_campaign,
    compile_execution_agenda,
    scenario_fingerprint_for,
)
from merlin_agents.campaign_service import (
    _bind_recon_applicability_evidence,
    _recon_applicability_partition,
)
from merlin_assessments import ReviewedApiTargetTool, StatefulTargetFixtureServer
from merlin_config import ConfigPaths, CredentialCheckpointVault
from merlin_contracts import (
    AgentTask,
    ModelCompleted,
    TextDelta,
    ToolCall,
)
from merlin_contracts.target_adapter_compiler import (
    compile_target_adapter,
    compiled_target_adapter_digest,
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


async def _unused_preparation(*_args, **_kwargs):
    raise AssertionError("preparation is not used by directive recovery")


class _CountingProvider:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def stream(self, request):
        self.calls.append(request.messages[-1].content)
        yield TextDelta(text='{"finding":"checked"}')
        yield ModelCompleted()


class _ValidCountingProvider(_CountingProvider):
    async def stream(self, request):
        self.calls.append(request.messages[-1].content)
        yield TextDelta(
            text=(
                '{"applicable_techniques":[{"technique_id":"prompt",'
                '"classification":"surface_supported",'
                '"rationale":"observed target chat",'
                '"evidence_refs":["exchange:recon"]}],'
                '"inapplicable_techniques":[]}'
            )
        )
        yield ModelCompleted()


class _PartitionProvider(_CountingProvider):
    async def stream(self, request):
        self.calls.append(request.messages[-1].content)
        yield TextDelta(
            text=(
                '{"applicable_techniques":[{"technique_id":"prompt",'
                '"classification":"surface_supported",'
                '"rationale":"observed target chat","evidence_refs":["exchange:recon"]}],'
                '"inapplicable_techniques":[]}'
            )
        )
        yield ModelCompleted()


class _AttackProvider(_CountingProvider):
    async def stream(self, request):
        content = request.messages[-1].content
        self.calls.append(content)
        if any("hypothesis_proposals" in message.content for message in request.messages):
            yield TextDelta(text='{"attempted_techniques":[],"hypothesis_proposals":[]}')
            yield ModelCompleted()
            return
        yield TextDelta(
            text=(
                '{"attempted_techniques":[{"technique_id":"prompt",'
                '"outcome":"refuted","evidence_refs":["exchange:attack"]}]}'
            )
        )
        yield ModelCompleted()


class _FollowupProvider(_CountingProvider):
    async def stream(self, request):
        content = request.messages[-1].content
        self.calls.append(content)
        match = re.search(
            r"hypothesis:[0-9a-f]+",
            "\n".join(message.content for message in request.messages),
        )
        assert match is not None
        yield TextDelta(
            text=(
                '{"attempted_techniques":[{"technique_id":"'
                + match.group(0)
                + '","outcome":"refuted","evidence_refs":["exchange:followup"]}],'
                '"hypothesis_proposals":[]}'
            )
        )
        yield ModelCompleted()


class _NoTools:
    def describe(self):
        return ()

    async def invoke(self, *_args, **_kwargs):
        raise AssertionError("no tools admitted")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "rotate_identity", "expected_score", "expected_gate_status", "expect_attack"),
    (
        ("success", False, 10, "succeeded", True),
        ("predicate-failed", False, 20, "blocked", False),
        ("identity-ambiguous", True, 10, "retryable", False),
    ),
)
async def test_public_execute_orders_reused_recon_gate_recipe_before_attack_with_postgres(
    tmp_path: Path,
    case: str,
    rotate_identity: bool,
    expected_score: int,
    expected_gate_status: str,
    expect_attack: bool,
) -> None:
    data_dir = tmp_path / f"postgres-objective-gate-public-path-{case}"
    campaign_id = f"campaign/objective-gate-public-path-{case}"
    async with StatefulTargetFixtureServer(
        maximum_parallel_states=1,
        state_mode="checkpoint",
        rotate_identity_on_message=rotate_identity,
    ) as target:
        adapter = target.adapter()
        state_capabilities = adapter["state_capabilities"]
        assert isinstance(state_capabilities, dict)
        identity = state_capabilities["identity"]
        assert isinstance(identity, dict)
        identity["baseline_selector"]["step_id"] = "start"
        for rule in state_capabilities["identity_equivalence_rules"]:
            rule["left_selector"]["step_id"] = "start"
        for observation in state_capabilities["observations"]:
            observation["baseline_selector"]["step_id"] = "start"
        compiled_adapter = compile_target_adapter(adapter)
        reviewed_adapter = {
            **adapter,
            "compiled_adapter": json.dumps(
                compiled_adapter.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ),
            "compiled_adapter_digest": compiled_target_adapter_digest(compiled_adapter),
        }
        adapter_document = ContextDocument.create(
            "target:reviewed-interaction-contract",
            "Reviewed stateful fixture adapter.\n"
            + json.dumps(reviewed_adapter, sort_keys=True, separators=(",", ":")),
        )
        provider_calls: list[object] = []

        class AttackProvider:
            async def stream(self, request):
                provider_calls.append(request)
                if any(message.role == "tool" for message in request.messages):
                    yield TextDelta(
                        text=(
                            '{"attempted_techniques":[{"technique_id":"prompt",'
                            '"outcome":"refuted","evidence_refs":["exchange:attack"]}],'
                            '"hypothesis_proposals":[]}'
                        )
                    )
                    yield ModelCompleted()
                    return
                yield ToolCall(
                    call_id=f"attack-{len(provider_calls)}",
                    name="target_request",
                    arguments={"input": "attack"},
                )
                yield ModelCompleted(finish_reason="tool_calls")

        async def allow(*_args) -> bool:
            return True

        async def lead(*_args, **_kwargs):
            return LeadAssessment("bounded public-path proof complete", True)

        with Database(DatabaseConfig(data_dir=data_dir)) as database:
            with (
                database.connect() as scheduler_connection,
                database.connect() as campaign_connection,
                database.connect() as target_connection,
            ):
                MigrationRunner(scheduler_connection).apply()
                session = SessionRepository(campaign_connection).create(
                    "objective gate public path"
                )
                session_id = str(session.id)
                gate = ObjectivePhaseGate(
                    id="baseline",
                    ordinal=0,
                    objective="Establish the reviewed baseline",
                    actions=(ObjectiveGateAction("baseline/message", "interaction", "advance"),),
                    success_predicate=ObjectiveGatePredicate(
                        "objective_score", ObjectiveGateOperator.EQUALS, (expected_score,)
                    ),
                    budget=ObjectiveGateBudget(1, 1, 5),
                    retain_success=ObjectiveGateRetention.CHECKPOINT,
                )
                manifest = CampaignManifest.create(
                    id=campaign_id,
                    session_id=session_id,
                    objective="Assess the reviewed target",
                    profile=CampaignProfile.RECOMMENDED,
                    target=ReviewedTargetAdapter(
                        "target-1", "http_api", target.origin, "message", browser_concurrency=1
                    ),
                    provider_id="provider",
                    provider_model="model",
                    provider_effort="default",
                    authority_ids=(f"session:{session_id}", "target:target-1"),
                    catalogue=(
                        ContextDocument.create("case:prompt", "Probe prompt boundary"),
                        adapter_document,
                    ),
                    skill_documents=(),
                    tool_ids=("target_request",),
                    budget=AssignmentBudget(max_seconds=10),
                    specialist_composition="autonomous",
                    objective_phase_gates=(gate,),
                )
                context = AssignmentContext(
                    target_profile_id="target-1",
                    techniques=(
                        AssignmentTechnique(
                            "prompt",
                            "probe",
                            frozenset({"prompt", "injection"}),
                            ("case:prompt",),
                            execution_role="direct",
                        ),
                    ),
                    tool_ids=("target_request",),
                    skill_ids=(),
                    provider_id="provider",
                    authority_ids=manifest.authority_ids,
                    budget=manifest.budget,
                    input_schema='{"type":"object"}',
                    output_schema='{"type":"object"}',
                )

                async def prepare(*_args, **_kwargs):
                    return CampaignPreparation(manifest, context)

                sessions = CampaignApiSessionRepository(target_connection)
                store = CampaignStore(campaign_connection)
                async with httpx.AsyncClient() as client:
                    target_tool = ReviewedApiTargetTool(
                        sessions,
                        lambda _target_id: adapter,
                        {},
                        checkpoint_store=store,
                        checkpoint_vault=CredentialCheckpointVault(
                            ConfigPaths(tmp_path / "credentials")
                        ),
                        client=client,
                    )
                    scheduler = DurableSchedulerStore(scheduler_connection)
                    agents = DurableAgentService(
                        AttackProvider(),
                        ToolBroker(
                            (target_tool,),
                            confirmer=allow,
                            effect_store=PostgresToolEffectStore(
                                ToolEffectRepository(target_connection)
                            ),
                        ),
                        scheduler,
                        model="model",
                    )
                    service = CampaignService(store, agents, prepare, lead_runner=lead)
                    review = await service.prepare(
                        session_id,
                        manifest.objective,
                        CampaignProfile.RECOMMENDED,
                        objective_phase_gates=(gate,),
                    )
                    fingerprint = scenario_fingerprint_for(manifest.target, manifest.catalogue)
                    recon_digest = "sha256:" + "a" * 64
                    store.record_recon_result(
                        {
                            "id": "recon-result:objective-gate",
                            "target_id": manifest.target.id,
                            "scenario_fingerprint": fingerprint,
                            "result_digest": recon_digest,
                            "result": {
                                "applicable_ids": ["prompt"],
                                "inapplicable_ids": [],
                                "result_digest": recon_digest,
                            },
                        }
                    )

                    executed = await service.execute(
                        session_id,
                        campaign_id,
                        str(review["manifest_digest"]),
                    )
                    assert executed["status"] == "executing"
                    assert store.frontier_counts(campaign_id)["claimed"] == 0

                    for _ in range(8):
                        supervisor = service._supervisors.get(campaign_id)  # noqa: SLF001
                        if supervisor is None:
                            break
                        await asyncio.wait_for(asyncio.shield(supervisor), timeout=10)
                        await asyncio.sleep(0)

                gate_events = [
                    row["payload"]
                    for row in store.list_lead(campaign_id)
                    if row["payload"].get("kind") == "objective_phase_gate_settled"
                ]
                assert gate_events[-1]["status"] == expected_gate_status
                assert gate_events[-1]["frontier_claimed"] is False
                operations = [item["operation"] for item in target.http_log]
                if expect_attack:
                    assert operations.count("message") >= 2
                    assert provider_calls
                    gate_message = operations.index("message")
                    attack_message = operations.index("message", gate_message + 1)
                    assert "restore" in operations[gate_message + 1 : attack_message]
                    checkpoint = store.load_objective_gate_checkpoint(campaign_id, "baseline")
                    assert checkpoint is not None
                    assert checkpoint["checkpoint_handle"]["kind"] == "opaque_reference"
                else:
                    assert store.frontier_counts(campaign_id)["claimed"] == 0
                    assert provider_calls == []


@pytest.mark.asyncio
async def test_concurrent_campaign_execute_rejoins_once_across_pg0_restart(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-concurrent-campaign"
    campaign_id = "campaign/concurrent-execute"
    calls: list[str] = []

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with (
            database.connect() as agent_connection,
            database.connect() as campaign_connection,
        ):
            MigrationRunner(agent_connection).apply()
            session = SessionRepository(agent_connection).create("concurrent campaign")
            session_id = str(session.id)
            manifest = CampaignManifest.create(
                id=campaign_id,
                session_id=session_id,
                objective="Assess the reviewed target",
                profile=CampaignProfile.RECOMMENDED,
                target=ReviewedTargetAdapter(
                    "target-1", "open_ai_compatible", "https://target.test", "model"
                ),
                provider_id="provider",
                provider_model="model",
                provider_effort="default",
                authority_ids=(f"session:{session_id}", "target:target-1"),
                catalogue=(ContextDocument.create("case:prompt", "Probe prompt boundary"),),
                skill_documents=(),
                tool_ids=(),
                budget=AssignmentBudget(),
                specialist_composition="autonomous",
            )
            context = AssignmentContext(
                target_profile_id="target-1",
                techniques=(
                    AssignmentTechnique(
                        "prompt",
                        "probe",
                        frozenset({"prompt", "injection"}),
                        ("case:prompt",),
                    ),
                ),
                tool_ids=(),
                skill_ids=(),
                provider_id="provider",
                authority_ids=manifest.authority_ids,
                budget=manifest.budget,
                input_schema='{"type":"object"}',
                output_schema='{"type":"object"}',
            )

            async def prepare(*_args):
                return CampaignPreparation(manifest, context)

            scheduler = DurableSchedulerStore(agent_connection)
            agents = DurableAgentService(
                _ValidCountingProvider(calls), _NoTools(), scheduler, model="model"
            )
            campaigns = CampaignStore(campaign_connection)
            service = CampaignService(campaigns, agents, prepare)
            review = await service.prepare(
                session_id, manifest.objective, CampaignProfile.RECOMMENDED
            )

            first, second = await asyncio.gather(
                service.execute(session_id, campaign_id, review["manifest_digest"]),
                service.execute(session_id, campaign_id, review["manifest_digest"]),
            )
            assert first == second
            executions = tuple(
                live.execution
                for live in agents._live.values()  # noqa: SLF001
                if live.execution is not None
            )
            assert len(executions) == 1
            await asyncio.gather(*executions)
            assert len(calls) == 1
            assert len(campaigns.list_waves(campaign_id)) == 1
            assert scheduler.load_schedule(first["schedule_id"])["status"] == "completed"
            campaigns.finish_wave(campaign_id, 0, "completed", {"completed": 1, "failed": 0})
            campaigns.finish(campaign_id, "completed")

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with (
            restarted.connect() as agent_connection,
            restarted.connect() as campaign_connection,
        ):
            MigrationRunner(agent_connection).apply()
            scheduler = DurableSchedulerStore(agent_connection)
            campaigns = CampaignStore(campaign_connection)
            agents = DurableAgentService(
                _ValidCountingProvider(calls), _NoTools(), scheduler, model="model"
            )
            service = CampaignService(campaigns, agents, _unused_preparation)
            assert await service.recover() == 0
            assert await agents.recover() == 0
            assert len(calls) == 1
            assert len(campaigns.list_waves(campaign_id)) == 1


def test_campaign_supervision_claim_decision_and_directive_survive_pg0_restart(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres"
    campaign_id = "campaign/supervision-restart"

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("supervision restart")
            store = CampaignStore(connection)
            store.prepare(
                campaign_id,
                str(session.id),
                {"manifest": {"profile": "recommended"}},
                "sha256:supervision-restart",
            )
            store.record_worker_message(
                {
                    "id": "worker-message/committed",
                    "campaign_id": campaign_id,
                    "wave": 1,
                    "source_task_id": "worker/source",
                    "destination": "lead",
                    "content": "The assigned route is unavailable.",
                    "content_digest": "sha256:committed-message",
                    "kind": "reroute_request",
                    "related_task_id": "worker/source",
                    "requested_profile_id": "browser-specialist",
                }
            )
            committed_claim = store.claim_worker_messages(campaign_id, "lead/original", 30)
            assert [row["id"] for row in committed_claim] == ["worker-message/committed"]
            assert store.claim_worker_messages(campaign_id, "lead/contender", 30) == []

            committed_claim_id = str(committed_claim[0]["lead_claim_id"])
            first_decision = store.commit_lead_decision(
                campaign_id,
                1,
                "decision/reroute",
                {"reason": "route unavailable", "source": "worker-message/committed"},
                (
                    {
                        "action": "redirect",
                        "source_node_id": "worker/source",
                        "destination_node_id": "worker/replacement",
                        "instruction": "Continue through the browser route.",
                    },
                ),
                claim_id=committed_claim_id,
                claimant_id="lead/original",
            )
            assert first_decision["decision_id"] == "decision/reroute"
            with pytest.raises(ValueError, match="different payload"):
                store.commit_lead_decision(
                    campaign_id,
                    1,
                    "decision/reroute",
                    {"reason": "changed after persistence"},
                    (),
                )

            store.record_worker_message(
                {
                    "id": "worker-message/orphaned-claim",
                    "campaign_id": campaign_id,
                    "wave": 1,
                    "source_task_id": "worker/blocked",
                    "destination": "lead",
                    "content": "The worker cannot continue with its current capability set.",
                    "content_digest": "sha256:orphaned-message",
                    "kind": "decline",
                    "related_task_id": "worker/blocked",
                    "requested_profile_id": "policy-specialist",
                }
            )
            orphaned_claim = store.claim_worker_messages(campaign_id, "lead/original", 30)
            assert [row["id"] for row in orphaned_claim] == ["worker-message/orphaned-claim"]
            orphaned_claim_id = str(orphaned_claim[0]["lead_claim_id"])

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            MigrationRunner(connection).apply()
            store = CampaignStore(connection)

            assert store.claim_worker_messages(campaign_id, "lead/replacement", 30) == []
            connection.execute(
                """UPDATE merlin_storage.campaign_worker_messages
                   SET lead_claim_expires_at = now() - interval '1 second'
                   WHERE id = %s""",
                ("worker-message/orphaned-claim",),
            )
            connection.commit()

            replacement_claim = store.claim_worker_messages(campaign_id, "lead/replacement", 30)
            assert [row["id"] for row in replacement_claim] == ["worker-message/orphaned-claim"]
            assert replacement_claim[0]["lead_claim_generation"] == 2
            replacement_claim_id = str(replacement_claim[0]["lead_claim_id"])

            with pytest.raises(ValueError, match="stale or empty"):
                store.commit_lead_decision(
                    campaign_id,
                    1,
                    "decision/stale",
                    {"reason": "stale owner"},
                    (),
                    claim_id=orphaned_claim_id,
                    claimant_id="lead/original",
                )

            store.commit_lead_decision(
                campaign_id,
                1,
                "decision/handoff",
                {"reason": "worker capability mismatch"},
                (
                    {
                        "action": "handoff",
                        "source_node_id": "worker/blocked",
                        "destination_node_id": "worker/policy",
                        "instruction": "Continue with the policy specialist.",
                    },
                ),
                claim_id=replacement_claim_id,
                claimant_id="lead/replacement",
            )

            directives = store.list_unapplied_directives(campaign_id)
            assert [row["id"] for row in directives] == [
                "decision/reroute/0",
                "decision/handoff/0",
            ]
            assert directives[0]["source_message_ids"] == ["worker-message/committed"]
            assert directives[1]["source_message_ids"] == ["worker-message/orphaned-claim"]
            for directive in directives:
                effect_reference = f"scheduler://effects/{directive['id']}"
                applied = store.mark_directive_applied(directive["id"], effect_reference)
                assert applied["state"] == "applied"
                assert (
                    store.mark_directive_applied(directive["id"], effect_reference)["state"]
                    == "applied"
                )
                with pytest.raises(ValueError, match="different applied effect"):
                    store.mark_directive_applied(directive["id"], effect_reference + "/changed")

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted_again:
        with restarted_again.connect() as connection:
            MigrationRunner(connection).apply()
            store = CampaignStore(connection)

            assert store.list_pending_worker_messages(campaign_id) == []
            assert store.list_unapplied_directives(campaign_id) == []
            assert [row["decision_id"] for row in store.list_lead(campaign_id)] == [
                "decision/reroute",
                "decision/handoff",
            ]
            receipts = connection.execute(
                """SELECT id, state, effect_reference
                   FROM merlin_storage.campaign_lead_directives
                   WHERE campaign_id = %s ORDER BY id""",
                (campaign_id,),
            ).fetchall()
            assert [row["state"] for row in receipts] == ["applied", "applied"]
            assert all(
                str(row["effect_reference"]).startswith("scheduler://effects/") for row in receipts
            )


@pytest.mark.asyncio
async def test_campaign_service_reconciles_existing_mailbox_effect_after_pg0_restart(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-integrated"
    campaign_id = "campaign/integrated-restart"
    schedule_id: str
    directive_id = "decision/redirect/0"
    message_id = str(uuid5(NAMESPACE_URL, f"lead-directive:{directive_id}"))
    objective = "Continue from the durable browser checkpoint."

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("integrated supervision restart")
            scheduler = DurableSchedulerStore(connection)
            schedule_id = scheduler.create_schedule(
                str(session.id),
                {"assignments": [{"id": "campaign/source"}], "edges": []},
                {"states": {"worker:campaign/source": "completed"}, "last_tick": 1},
                ("worker:campaign/source",),
            )
            _, created = scheduler.send_message_once(
                message_id,
                schedule_id,
                str(session.id),
                "worker:campaign/source",
                "merlin.worker-coordination.v1",
                {"message": objective},
            )
            assert created is True

            campaigns = CampaignStore(connection)
            campaigns.prepare(
                campaign_id,
                str(session.id),
                {"manifest": {"profile": "recommended"}},
                "sha256:integrated-restart",
            )
            campaigns.begin_execution(campaign_id, "sha256:integrated-restart", schedule_id)
            campaigns.add_wave(campaign_id, 1, "initial", schedule_id)
            campaigns.commit_lead_decision(
                campaign_id,
                1,
                "decision/redirect",
                {"summary": "redirect source"},
                (
                    {
                        "action": "redirect",
                        "source_node_id": "campaign/source",
                        "destination_node_id": None,
                        "objective": objective,
                        "reason": "continue",
                    },
                ),
            )
            campaigns.commit_lead_decision(
                campaign_id,
                1,
                "decision/failed",
                {"summary": "failed redirect"},
                (
                    {
                        "action": "redirect",
                        "source_node_id": "campaign/source",
                        "destination_node_id": None,
                        "objective": "Unavailable replacement route.",
                        "reason": "test failed receipt projection",
                    },
                ),
            )
            campaigns.mark_directive_failed(
                "decision/failed/0", "destination capability disappeared"
            )
            assert campaigns.list_unapplied_directives(campaign_id, 1)[0]["id"] == directive_id

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            MigrationRunner(connection).apply()
            scheduler = DurableSchedulerStore(connection)
            campaigns = CampaignStore(connection)
            agents = DurableAgentService(object(), object(), scheduler, model="unused")
            service = CampaignService(
                campaigns,
                agents,
                _unused_preparation,  # type: ignore[arg-type]
            )
            terminal = AgentTask(
                id="campaign/source",
                objective="source",
                status="completed",
                agent_id="worker:campaign/source",
            )

            planned = await service.list_schedule_directives(schedule_id)
            assert "[directive:planned] decision/redirect/0" in planned["campaign/source"][0]
            assert any(
                "[directive:failed] decision/failed/0" in row
                and "failure=destination capability disappeared" in row
                for row in planned["campaign/source"]
            )
            assert await service.list_schedule_directives("unrelated-schedule") == {}
            campaigns.mark_directive_applied(
                "decision/failed/0", "operator-reconciled:test-fixture"
            )

            await service._apply_persisted_directives(  # noqa: SLF001
                campaign_id, str(session.id), schedule_id, 1, (terminal,)
            )
            await service._apply_persisted_directives(  # noqa: SLF001
                campaign_id, str(session.id), schedule_id, 1, (terminal,)
            )

            assert campaigns.list_unapplied_directives(campaign_id, 1) == []
            mailbox_count = connection.execute(
                "SELECT count(*) AS count FROM merlin_storage.scheduler_mailbox WHERE id = %s",
                (message_id,),
            ).fetchone()["count"]
            assert mailbox_count == 1
            receipt = connection.execute(
                """SELECT state, effect_reference
                   FROM merlin_storage.campaign_lead_directives WHERE id = %s""",
                (directive_id,),
            ).fetchone()
            assert receipt["state"] == "applied"
            assert receipt["effect_reference"] == f"mailbox:{message_id}"
            applied = await service.list_schedule_directives(schedule_id)
            assert any(
                "[directive:applied] decision/redirect/0" in row
                and f"effect=mailbox:{message_id}" in row
                for row in applied["campaign/source"]
            )


@pytest.mark.asyncio
async def test_followup_topology_over_seven_restores_without_duplicate_materialization(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-followup-topology"
    campaign_id = "campaign/followup-topology"
    calls: list[str] = []

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with (
            database.connect() as agent_connection,
            database.connect() as campaign_connection,
        ):
            MigrationRunner(agent_connection).apply()
            session = SessionRepository(agent_connection).create("followup topology restart")
            session_id = str(session.id)
            manifest = CampaignManifest.create(
                id=campaign_id,
                session_id=session_id,
                objective="Assess the reviewed target",
                profile=CampaignProfile.EXHAUSTIVE,
                target=ReviewedTargetAdapter(
                    "target-1", "open_ai_compatible", "https://target.test", "model"
                ),
                provider_id="provider",
                provider_model="model",
                provider_effort="default",
                authority_ids=(f"session:{session_id}", "target:target-1"),
                catalogue=(ContextDocument.create("case:prompt", "Probe boundary"),),
                skill_documents=(),
                tool_ids=(),
                budget=AssignmentBudget(),
                specialist_composition="autonomous",
            )
            context = AssignmentContext(
                target_profile_id="target-1",
                techniques=(
                    AssignmentTechnique("prompt", "probe", frozenset({"prompt"}), ("case:prompt",)),
                ),
                tool_ids=(),
                skill_ids=(),
                provider_id="provider",
                authority_ids=manifest.authority_ids,
                budget=manifest.budget,
                input_schema='{"type":"object"}',
                output_schema='{"type":"object"}',
            )
            profiles = tuple(
                dict.fromkeys(
                    item.profile_id
                    for item in compile_initial_campaign(manifest, context).assignments
                )
            )
            followup_profile = next(profile for profile in profiles if profile != "recon")
            followups = tuple(
                LeadFollowUp(
                    followup_profile,
                    f"Novel restart follow-up {index}",
                    ("initial",),
                    FrontierHypothesis(
                        f"surface-{index}",
                        f"assertion-{index}",
                        f"oracle-{index}",
                        f"novelty-{index}",
                    ),
                )
                for index in range(12)
            )
            scheduler = DurableSchedulerStore(agent_connection)
            initial_node = "worker:initial"
            initial_schedule_id = scheduler.create_schedule(
                session_id,
                {"assignments": [{"id": "initial"}], "edges": []},
                {"states": {initial_node: "ready"}, "last_tick": 0},
                (initial_node,),
                ready_nodes=(initial_node,),
            )
            campaigns = CampaignStore(campaign_connection)
            campaigns.prepare(
                campaign_id,
                session_id,
                {"manifest": {"profile": "exhaustive"}},
                "sha256:followup-topology",
            )
            campaigns.begin_execution(campaign_id, "sha256:followup-topology", initial_schedule_id)
            agents = DurableAgentService(
                _FollowupProvider(calls), _NoTools(), scheduler, model="model"
            )
            service = CampaignService(
                campaigns, agents, _unused_preparation, max_worker_concurrency=1
            )
            agent_connection.commit()
            campaign_connection.commit()
            compiled = service._followup_compilation(  # noqa: SLF001
                manifest,
                context,
                1,
                followups,
                source_tasks=(AgentTask(id="initial", objective="source", status="completed"),),
                source_evidence={
                    "initial": {
                        "digest": "sha256:" + "a" * 64,
                        "reference": "receipt:initial",
                    }
                },
            )
            tasks = await service._run_wave(  # noqa: SLF001
                campaign_id, manifest, 1, "followup", compiled
            )
            assert len(tasks) == len(calls) == 12
            assert all(task.status == "completed" for task in tasks)
            assert [row["wave"] for row in campaigns.list_waves(campaign_id)] == [0, 1]

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with (
            restarted.connect() as agent_connection,
            restarted.connect() as campaign_connection,
        ):
            MigrationRunner(agent_connection).apply()
            scheduler = DurableSchedulerStore(agent_connection)
            campaigns = CampaignStore(campaign_connection)
            agents = DurableAgentService(
                _FollowupProvider(calls), _NoTools(), scheduler, model="model"
            )
            service = CampaignService(
                campaigns, agents, _unused_preparation, max_worker_concurrency=1
            )
            assert await agents.recover() == 0
            assert len(calls) == 12
            waves = campaigns.list_waves(campaign_id)
            assert [row["wave"] for row in waves] == [0, 1]
            restored_ids = tuple(
                row["node_id"]
                for row in scheduler.list_nodes(waves[1]["schedule_id"])
                if row["node_id"].startswith("worker:")
            )
            assert len(restored_ids) == len(set(restored_ids)) == 12
            restored_results = tuple(
                row["node_id"]
                for row in scheduler.list_results(waves[1]["schedule_id"])
                if row["node_id"].startswith("worker:") and row["payload"].get("content")
            )
            assert len(restored_results) == len(set(restored_results)) == 12
            assert set(restored_results) == set(restored_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_stage", ("sealed", "partial_handoff"))
async def test_staged_attack_handoff_completes_before_execution_across_pg0_restart(
    tmp_path: Path,
    crash_stage: str,
) -> None:
    data_dir = tmp_path / f"postgres-staged-attack-{crash_stage}"
    campaign_id = f"campaign/staged-attack-{crash_stage}"
    calls: list[str] = []
    session_id: str
    attack_schedule_id = str(uuid5(NAMESPACE_URL, f"merlin.campaign-wave.v1\n{campaign_id}\n1"))
    manifest = CampaignManifest.create(
        id=campaign_id,
        session_id="pending",
        objective="Assess the reviewed target",
        profile=CampaignProfile.RECOMMENDED,
        target=ReviewedTargetAdapter(
            "target-1", "open_ai_compatible", "https://target.test", "model"
        ),
        provider_id="provider",
        provider_model="model",
        provider_effort="default",
        authority_ids=("target:target-1",),
        catalogue=(
            ContextDocument.create("case:prompt", "Probe prompt boundary"),
            ContextDocument.create("case:benign", "Send one benign control message"),
            ContextDocument.create(
                "target:api-recon-decision",
                "Durable API-first reconnaissance decision.\n"
                + json.dumps(
                    {
                        "request_evidence_ref": "request-reviewed",
                        "response_evidence_ref": "response-reviewed",
                    },
                    sort_keys=True,
                ),
            ),
        ),
        skill_documents=(),
        tool_ids=(),
        budget=AssignmentBudget(),
        specialist_composition="autonomous",
    )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("staged attack restart")
            session_id = str(session.id)
            manifest = CampaignManifest.create(
                id=campaign_id,
                session_id=session_id,
                objective=manifest.objective,
                profile=manifest.profile,
                target=manifest.target,
                provider_id=manifest.provider_id,
                provider_model=manifest.provider_model,
                provider_effort=manifest.provider_effort,
                authority_ids=(f"session:{session_id}", "target:target-1"),
                catalogue=manifest.catalogue,
                skill_documents=(),
                tool_ids=(),
                budget=manifest.budget,
                specialist_composition="autonomous",
            )
            context = AssignmentContext(
                target_profile_id="target-1",
                techniques=(
                    AssignmentTechnique(
                        "prompt",
                        "probe",
                        frozenset({"prompt", "injection", "text", "single"}),
                        ("case:prompt",),
                        execution_role="direct",
                        control_technique_id="benign-control",
                        oracle_digest="sha256:" + "1" * 64,
                        control_objective="Send one benign control message",
                        control_guidance_ids=("case:benign",),
                        control_oracle_digest="sha256:" + "1" * 64,
                        comparison_policy="reviewed-benign-differential-v1",
                    ),
                ),
                tool_ids=(),
                skill_ids=(),
                provider_id="provider",
                authority_ids=manifest.authority_ids,
                budget=manifest.budget,
                input_schema='{"type":"object"}',
                output_schema='{"type":"object"}',
            )
            scheduler = DurableSchedulerStore(connection)
            agents = DurableAgentService(
                _PartitionProvider(calls), _NoTools(), scheduler, model="model"
            )
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                campaign_id,
                session_id,
                {
                    "manifest": json.loads(json.dumps(asdict(manifest), default=list)),
                    "assignment_context": json.loads(json.dumps(asdict(context), default=list)),
                },
                manifest.digest,
            )
            recon = WorkerAssignment(
                id=f"{campaign_id}/recon",
                objective="recon",
                profile_id="recon",
                target_profile_id="target-1",
                technique_ids=(),
                guidance_ids=(),
                tool_ids=(),
                skill_ids=(),
                provider_id="provider",
                authority_ids=manifest.authority_ids,
                budget=manifest.budget,
                input_schema='{"type":"object"}',
                output_schema='{"type":"object"}',
            )
            recon_live = await agents.submit(
                session_id,
                (recon,),
                minimum_workers=1,
                start_immediately=False,
                schedule_id=str(uuid5(NAMESPACE_URL, f"merlin.campaign-wave.v1\n{campaign_id}\n0")),
            )
            campaigns.begin_execution(campaign_id, manifest.digest, recon_live.id)
            agents.start_execution(recon_live, max_workers=1)
            assert recon_live.execution is not None
            await recon_live.execution
            campaigns.finish_wave(campaign_id, 0, "completed", {"completed": 1, "failed": 0})

            # Recon exclusions are advisory until independently proven. The
            # staged schedule must therefore preserve the full reviewed
            # frontier across restart.
            applicable_context = context
            attacks = compile_execution_agenda(
                manifest,
                applicable_context,
                compile_attack_campaign(manifest, applicable_context),
            ).assignments
            if crash_stage == "partial_handoff":
                attack_live = await agents.submit(
                    session_id,
                    attacks,
                    minimum_workers=1,
                    start_immediately=False,
                    schedule_id=attack_schedule_id,
                )
                campaigns.add_wave(campaign_id, 1, "initial", attack_live.id)

            async def lesson(*_args):
                return "durable recipe"

            service = CampaignService(
                campaigns,
                agents,
                _unused_preparation,
                interaction_lesson_runner=lesson,
            )
            recon_tasks = tuple(await agents.list_schedule_tasks(recon_live.id))
            applicability = _recon_applicability_partition(
                recon_tasks[0].result or "", tuple(item.id for item in context.techniques)
            )
            assert applicability is not None
            receipt = await agents.committed_task_result_evidence(recon_live.id, recon_tasks[0])
            assert receipt is not None
            applicability = _bind_recon_applicability_evidence(applicability, receipt)
            assert applicability is not None
            await service._seal_recon_applicability(  # noqa: SLF001
                campaign_id, 0, applicability
            )
            if crash_stage == "partial_handoff":
                await service._publish_recon_guidance_to_assignments(  # noqa: SLF001
                    campaign_id,
                    manifest,
                    0,
                    recon_tasks,
                    attacks[:1],
                    applicability=applicability,
                )
            assert len(calls) == 1
            if crash_stage == "partial_handoff":
                assert scheduler.load_schedule(attack_schedule_id)["status"] == "paused"

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with (
            restarted.connect() as agent_connection,
            restarted.connect() as campaign_connection,
        ):
            MigrationRunner(agent_connection).apply()
            scheduler = DurableSchedulerStore(agent_connection)
            campaigns = CampaignStore(campaign_connection)
            agents = DurableAgentService(
                _AttackProvider(calls), _NoTools(), scheduler, model="model"
            )

            async def lesson(*_args):
                return "durable recipe"

            async def lead(*_args):
                return LeadAssessment(
                    "recovered applicable frontier",
                    False,
                )

            service = CampaignService(
                campaigns,
                agents,
                _unused_preparation,
                lead,
                interaction_lesson_runner=lesson,
                max_worker_concurrency=1,
            )
            assert await service.recover() == 1
            for _ in range(200):
                status = await service.status(campaign_id)
                if status["saturation_decision"].get("state") == "awaiting_operator":
                    await service.decide_saturation(session_id, campaign_id, "stop")
                    break
                if status["status"] != "executing":
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("recovered campaign did not reach a terminal boundary")
            await service.wait(campaign_id)
            assert len(campaigns.list_waves(campaign_id)) == 2
            attack_tasks = tuple(await agents.list_schedule_tasks(attack_schedule_id))
            assert attack_tasks
            restored_attack = await agents.reopen(attack_schedule_id)
            semantic_attacks = tuple(
                assignment for assignment in restored_attack.assignments if assignment.technique_ids
            )
            discoveries = tuple(
                assignment
                for assignment in restored_attack.assignments
                if not assignment.technique_ids
            )
            assert len(semantic_attacks) == len(discoveries) == 1
            assert semantic_attacks[0].technique_ids == ("prompt",)
            assert semantic_attacks[0].strategy_id == "direct"
            assert semantic_attacks[0].execution_stage is None
            assert discoveries[0].id.endswith("/target-derived-discovery")
            # One provider call performs reconnaissance and each restored
            # assignment executes exactly once. Routine direct observations do
            # not pay for an unrequested control or replay round.
            expected_calls = 1 + len(restored_attack.assignments)
            assert len(calls) == expected_calls == 3
            assert {task.id for task in attack_tasks} == {
                assignment.id for assignment in restored_attack.assignments
            }
            assert all(task.status == "completed" and task.result for task in attack_tasks)
            committed_evidence = tuple(
                await asyncio.gather(
                    *(
                        agents.committed_task_result_evidence(attack_schedule_id, task)
                        for task in attack_tasks
                    )
                )
            )
            assert all(item is not None for item in committed_evidence)
            assert len(
                {str(item["reference"]) for item in committed_evidence if item is not None}
            ) == len(attack_tasks)
            sealed = [
                row
                for row in campaigns.list_lead(campaign_id)
                if row["payload"].get("kind") == "recon_applicability_sealed"
            ]
            assert len(sealed) == 1
            recipients = agent_connection.execute(
                """SELECT recipient, count(*) AS count
                   FROM merlin_storage.scheduler_mailbox
                   WHERE schedule_id = %s GROUP BY recipient ORDER BY recipient""",
                (attack_schedule_id,),
            ).fetchall()
            recipient_counts = [(row["recipient"], row["count"]) for row in recipients]
            assert recipient_counts == sorted(
                (f"worker:{assignment.id}", 1) for assignment in restored_attack.assignments
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted_again:
        with (
            restarted_again.connect() as agent_connection,
            restarted_again.connect() as campaign_connection,
        ):
            MigrationRunner(agent_connection).apply()
            scheduler = DurableSchedulerStore(agent_connection)
            campaigns = CampaignStore(campaign_connection)
            agents = DurableAgentService(
                _CountingProvider(calls), _NoTools(), scheduler, model="model"
            )
            service = CampaignService(campaigns, agents, _unused_preparation)
            assert await service.recover() == 0
            assert await agents.recover() == 0
            assert len(calls) == expected_calls
            assert len(campaigns.list_waves(campaign_id)) == 2
            sealed = [
                row
                for row in campaigns.list_lead(campaign_id)
                if row["payload"].get("kind") == "recon_applicability_sealed"
            ]
            assert len(sealed) == 1
