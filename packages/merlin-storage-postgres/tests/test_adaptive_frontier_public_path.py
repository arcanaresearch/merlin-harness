import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

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
    LeadAssessment,
    ReviewedTargetAdapter,
)
from merlin_agents.arm_pool import rank_frontier
from merlin_agents.campaign import scenario_fingerprint_for
from merlin_agents.campaign_service import _sealed_candidate_identities
from merlin_contracts import (
    AgentTask,
    TechniqueAttempt,
    candidate_id_for_identity,
    project_candidate_outcome,
)
from merlin_storage_postgres import (
    CampaignStore,
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    SessionRepository,
    TechniqueAttemptRepository,
)
from merlin_storage_postgres.arm_store import CampaignArmStore


@pytest.mark.asyncio
async def test_public_service_adapts_frontier_from_authoritative_pg_outcome_across_restart(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres"
    campaign_id = "campaign/public-adaptive-frontier"
    candidate_ids = ("candidate/a", "candidate/b", "candidate/c")
    live_by_schedule: dict[str, SimpleNamespace] = {}
    tasks_by_schedule: dict[str, tuple[AgentTask, ...]] = {}
    evidence_by_schedule: dict[str, dict[str, dict[str, object]]] = {}
    attempts_by_schedule: dict[str, tuple[dict[str, object], ...]] = {}
    attack_submissions = 0
    first_batch_candidates: list[str] = []

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with (
            database.connect() as scheduler_connection,
            database.connect() as campaign_connection,
            database.connect() as attempt_connection,
            database.connect() as observer_connection,
        ):
            MigrationRunner(scheduler_connection).apply()
            session = SessionRepository(campaign_connection).create("adaptive frontier")
            session_id = str(session.id)
            manifest = CampaignManifest.create(
                id=campaign_id,
                session_id=session_id,
                objective="Assess the reviewed target",
                profile=CampaignProfile.RECOMMENDED,
                target=ReviewedTargetAdapter(
                    "target-adaptive",
                    "http_api",
                    "https://target.example/api",
                    "message",
                    browser_concurrency=2,
                ),
                provider_id="provider",
                provider_model="model",
                provider_effort="default",
                authority_ids=(f"session:{session_id}", "target:target-adaptive"),
                catalogue=tuple(
                    ContextDocument.create(f"case:{suffix}", f"Exercise {candidate_id}")
                    for suffix, candidate_id in zip(("a", "b", "c"), candidate_ids, strict=True)
                ),
                skill_documents=(),
                tool_ids=("target_request",),
                budget=AssignmentBudget(max_seconds=30),
                specialist_composition="opinionated",
            )
            context = AssignmentContext(
                target_profile_id=manifest.target.id,
                techniques=tuple(
                    AssignmentTechnique(
                        candidate_id,
                        f"Exercise {candidate_id}",
                        frozenset({"prompt", "stateful"}),
                        (f"case:{candidate_id.rsplit('/', 1)[-1]}",),
                        language="en",
                        execution_role="direct",
                        control_technique_id=f"control/{candidate_id}",
                        oracle_digest="sha256:" + "d" * 64,
                        control_objective="Send a harmless baseline message",
                        control_guidance_ids=(f"case:{candidate_id.rsplit('/', 1)[-1]}",),
                        control_oracle_digest="sha256:" + "d" * 64,
                        comparison_policy="reviewed-differential-v1",
                    )
                    for candidate_id in candidate_ids
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

            scheduler = DurableSchedulerStore(scheduler_connection)
            attempt_repository = TechniqueAttemptRepository(attempt_connection)

            class Agents:
                async def submit(
                    self, submitted_session_id, assignments, *, schedule_id, **_kwargs
                ):
                    nonlocal attack_submissions
                    assignments = tuple(assignments)
                    node_ids = tuple(item.id for item in assignments)
                    scheduler.create_schedule(
                        submitted_session_id,
                        {"assignments": [{"id": item.id} for item in assignments]},
                        {"states": {item: "completed" for item in node_ids}},
                        node_ids,
                        ready_nodes=node_ids,
                        schedule_id=schedule_id,
                    )
                    is_attack_batch = any(item.technique_ids for item in assignments)
                    first_batch = is_attack_batch and attack_submissions == 0
                    if is_attack_batch:
                        attack_submissions += 1
                    batch_completed = not is_attack_batch or first_batch
                    execution = asyncio.get_running_loop().create_future()
                    if batch_completed:
                        execution.set_result(None)
                    live = SimpleNamespace(
                        id=schedule_id, assignments=assignments, execution=execution
                    )
                    live_by_schedule[schedule_id] = live
                    tasks: list[AgentTask] = []
                    evidence: dict[str, dict[str, object]] = {}
                    attempts: list[dict[str, object]] = []
                    for assignment in assignments:
                        if not assignment.technique_ids:
                            tasks.append(
                                AgentTask(
                                    id=assignment.id,
                                    objective=assignment.objective,
                                    status="completed" if batch_completed else "queued",
                                    result=(
                                        '{"attempted_techniques":[],"hypothesis_proposals":[]}'
                                        if batch_completed
                                        else None
                                    ),
                                )
                            )
                            continue
                        assert len(assignment.technique_ids) == 1
                        technique_id = assignment.technique_ids[0]
                        identities = _sealed_candidate_identities(assignment.objective)
                        assert len(identities) == 1
                        identity = identities[0].projection()
                        signal = None
                        if first_batch:
                            first_batch_candidates.append(technique_id)
                            signal = (0.1, 0.9)[len(first_batch_candidates) - 1]
                        task = AgentTask(
                            id=assignment.id,
                            objective=assignment.objective,
                            status="completed" if first_batch else "queued",
                            result=(
                                json.dumps(
                                    {
                                        "outcome": "partial",
                                        "signal": signal,
                                        "attempted_techniques": [
                                            {
                                                "technique_id": technique_id,
                                                "outcome": "partial",
                                                "evidence_refs": [f"target-call:{technique_id}"],
                                            }
                                        ],
                                        "hypothesis_proposals": [],
                                    }
                                )
                                if first_batch
                                else None
                            ),
                            technique_ids=(technique_id,),
                        )
                        tasks.append(task)
                        if not first_batch:
                            continue
                        attempt_id = str(
                            uuid5(NAMESPACE_URL, f"{campaign_id}:{schedule_id}:{technique_id}")
                        )
                        attempt_repository.begin(
                            TechniqueAttempt(
                                id=attempt_id,
                                campaign_id=campaign_id,
                                wave=0,
                                task_id=task.id,
                                technique_id=technique_id,
                                strategy_id=str(identity["controller_id"]),
                                language=str(identity["requested_language"]),
                                transform_id=str(identity["payload_family"]),
                                control_kind="attack",
                                oracle_digest="sha256:" + "f" * 64,
                            )
                        )
                        exact_candidate_id = candidate_id_for_identity(identity)
                        target_call_id = f"target-call:{technique_id}"
                        projection = project_candidate_outcome(
                            candidate_id=exact_candidate_id,
                            attempt_disposition="valid",
                            outcome_authority="target",
                            outcome="progress",
                            evidence_digest=hashlib.sha256(exact_candidate_id.encode()).hexdigest(),
                        )
                        receipt = {
                            "attempt_disposition": "valid",
                            "outcome_authority": "target",
                            "target_authoritative_outcome": "progress",
                            "candidate_outcome_projection": projection,
                        }
                        evidence[task.id] = {
                            "attempt_id": attempt_id,
                            "digest": "sha256:" + hashlib.sha256(task.id.encode()).hexdigest(),
                            "reference": f"receipt:{task.id}",
                            "target_call_ids": (target_call_id,),
                            **receipt,
                            "target_state_transition": {"outcome": "progress"},
                            "target_evidence_receipts": {target_call_id: receipt},
                            "arm_execution_context": {
                                "surface_id": "chat",
                                "state_id": "state/fresh",
                            },
                            "target_delivery_receipts": {
                                target_call_id: {
                                    "requested_transform_id": identity["payload_family"],
                                    "applied_transform_id": identity["payload_family"],
                                    "actual_language": identity["requested_language"],
                                    "payload_digest": "sha256:" + "a" * 64,
                                    "receipt_digest": "sha256:" + "b" * 64,
                                }
                            },
                            "model_steps": 1,
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        }
                        attempts.append(
                            {
                                "task_id": task.id,
                                "technique_id": technique_id,
                                "state": "partial",
                                "disposition": "valid",
                                "strategy_state": {
                                    "candidate_identity": identity,
                                    "candidate_outcome_projection": projection,
                                    "outcome_authority": {
                                        "kind": "target",
                                        "outcome": "progress",
                                    },
                                },
                            }
                        )
                    tasks_by_schedule[schedule_id] = tuple(tasks)
                    evidence_by_schedule[schedule_id] = evidence
                    attempts_by_schedule[schedule_id] = tuple(attempts)
                    return live

                def start_execution(self, _live, *, max_workers):
                    assert max_workers == 2

                async def list_schedule_tasks(self, schedule_id):
                    return tasks_by_schedule[schedule_id]

                async def completion_projection(self, schedule_id):
                    return {
                        "tasks": tasks_by_schedule[schedule_id],
                        "evidence": evidence_by_schedule[schedule_id],
                    }

                async def technique_attempts(self, schedule_id):
                    return attempts_by_schedule[schedule_id]

                async def schedule_usage(self, schedule_id):
                    calls = len(evidence_by_schedule[schedule_id])
                    return {"turns": calls, "total_tokens": calls * 2}

                def target_exchange_tool_ids(self, _tool_ids):
                    return ("target_request",)

            async def lead(*_args, **_kwargs):
                return LeadAssessment("frontier remains open", False)

            agents = Agents()
            store = CampaignStore(campaign_connection)
            arms = CampaignArmStore(campaign_connection)
            observer_store = CampaignStore(observer_connection)
            observer_arms = CampaignArmStore(observer_connection)

            class ArmStoreProbe:
                def __init__(self, delegate):
                    self.delegate = delegate
                    self.recorded_arms = []
                    self.recorded_observations = []
                    self.record_arm_errors = []
                    self.record_observation_errors = []
                    self.ranking_reads = []

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

                def record_arm(self, arm):
                    self.recorded_arms.append(dict(arm))
                    try:
                        return self.delegate.record_arm(arm)
                    except Exception as error:
                        self.record_arm_errors.append(repr(error))
                        raise

                def record_arm_observation(self, observation):
                    self.recorded_observations.append(dict(observation))
                    try:
                        return self.delegate.record_arm_observation(observation)
                    except Exception as error:
                        self.record_observation_errors.append(repr(error))
                        raise

                def list_arms(self, target_id, scenario_fingerprint):
                    result = self.delegate.list_arms(target_id, scenario_fingerprint)
                    self.ranking_reads.append(("arms", tuple(result)))
                    return result

                def list_arm_observations(self, target_id, scenario_fingerprint):
                    result = self.delegate.list_arm_observations(target_id, scenario_fingerprint)
                    self.ranking_reads.append(("observations", tuple(result)))
                    return result

            arm_probe = ArmStoreProbe(arms)
            service = CampaignService(
                store,
                agents,
                prepare,
                lead_runner=lead,
                arm_store=arm_probe,
                max_worker_concurrency=4,
            )
            service._jev_escalation_client = lambda: None  # type: ignore[method-assign]  # noqa: SLF001
            review = await service.prepare(
                session_id,
                manifest.objective,
                CampaignProfile.RECOMMENDED,
                service_objectives={
                    "schema": "merlin.campaign-service-objectives.v1",
                    "max_orchestration_steps_per_target_call": 2,
                    "max_cost_per_unique_valid_attempt_usd": 0.01,
                    "min_valid_attempt_throughput": 1.0,
                    "evaluate_throughput_after_target_calls": 2,
                },
            )
            scenario = scenario_fingerprint_for(manifest.target, manifest.catalogue)
            recon_digest = "sha256:" + "c" * 64
            store.record_recon_result(
                {
                    "id": "recon-result:public-adaptive-frontier",
                    "target_id": manifest.target.id,
                    "scenario_fingerprint": scenario,
                    "result_digest": recon_digest,
                    "result": {
                        "applicable_ids": list(candidate_ids),
                        "inapplicable_ids": [],
                        "result_digest": recon_digest,
                    },
                }
            )
            # The production DurableAgentService invokes this same composition
            # seam when its swarm starts. This fixture supplies a minimal
            # scheduler facade, so establish the reviewed opinionated ownership
            # before exercising the public CampaignService execution path.
            await service._admit_wave0_specialist_composition(  # noqa: SLF001
                campaign_id, manifest, context
            )

            executed = await service.execute(
                session_id, campaign_id, str(review["manifest_digest"])
            )
            observations: list[dict[str, object]] = []
            for _ in range(1_500):
                active = observer_store.load_active_frontier_batch(campaign_id)
                observations = observer_arms.list_arm_observations(manifest.target.id, scenario)
                if active is not None and int(active["wave"]) == 1 and observations:
                    break
                supervisor = service._supervisors.get(campaign_id)  # noqa: SLF001
                if supervisor is not None and supervisor.done():
                    await supervisor
                await asyncio.sleep(0.01)
            else:
                supervisor = service._supervisors.get(campaign_id)  # noqa: SLF001
                stack = (
                    tuple(
                        f"{frame.f_code.co_name}:{frame.f_lineno}"
                        for frame in supervisor.get_stack()
                    )
                    if supervisor is not None
                    else ()
                )
                scheduled_techniques = tuple(
                    tuple(assignment.technique_ids for assignment in live.assignments)
                    for live in live_by_schedule.values()
                )
                raise AssertionError(
                    "CampaignService did not persist ARM learning and claim its successor; "
                    f"supervisor stack={stack!r}, observations={len(observations)}, "
                    f"arm_calls={arm_probe.recorded_arms!r}, "
                    f"arm_errors={arm_probe.record_arm_errors!r}, "
                    f"observation_calls={arm_probe.recorded_observations!r}, "
                    f"observation_errors={arm_probe.record_observation_errors!r}, "
                    f"attack_submissions={attack_submissions}, "
                    f"schedules={tuple(live_by_schedule)}, "
                    f"techniques={scheduled_techniques!r}, "
                    f"active={active!r}"
                )

            rankings = tuple(
                (
                    item.arm["technique_id"],
                    item.score,
                    tuple(
                        (component.name, component.contribution) for component in item.components
                    ),
                )
                for item in rank_frontier(
                    observer_arms.list_arms(manifest.target.id, scenario), observations
                )
            )
            ranking_reads = tuple(
                (
                    kind,
                    tuple(
                        (
                            row.get("technique_id")
                            or (
                                row.get("identity", {}).get("technique_id")
                                if isinstance(row.get("identity"), dict)
                                else None
                            ),
                            row.get("partial_signal"),
                        )
                        for row in rows
                    ),
                )
                for kind, rows in arm_probe.ranking_reads
            )
            assert len(first_batch_candidates) == 2
            unscored_candidate = next(
                candidate_id
                for candidate_id in candidate_ids
                if candidate_id not in first_batch_candidates
            )
            expected_successor = (unscored_candidate, first_batch_candidates[1])
            assert tuple(item["id"] for item in active["candidates"]) == expected_successor, (
                rankings,
                ranking_reads,
            )
            assert observer_store.frontier_counts(campaign_id)["queued"] == 1
            assert len(observations) == 2
            assert {
                item["identity"]["technique_id"]: item["partial_signal"] for item in observations
            } == {first_batch_candidates[0]: 0.1, first_batch_candidates[1]: 0.9}
            first_wave = store.list_waves(campaign_id)[0]
            assert str(first_wave["status"]) == "completed"
            assert str(first_wave["schedule_id"]) == executed["schedule_id"]
            objectives = await service._campaign_service_objective_state(  # noqa: SLF001
                campaign_id, manifest
            )
            assert objectives["reviewed"] is True
            assert objectives["violations"] == ()
            assert objectives["metrics"]["target_calls"] == 2
            assert objectives["metrics"]["unique_valid_attempts"] == 2
            assert objectives["metrics"]["valid_attempt_throughput"] == 1.0
            assert objectives["metrics"]["cost_per_unique_valid_attempt_usd"] == 0.0

            successor = service._supervisors.pop(campaign_id)  # noqa: SLF001
            successor.cancel()
            await asyncio.gather(successor, return_exceptions=True)
            for live in live_by_schedule.values():
                if not live.execution.done():
                    live.execution.cancel()

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            MigrationRunner(connection).apply()
            store = CampaignStore(connection)
            arms = CampaignArmStore(connection)
            active = store.load_active_frontier_batch(campaign_id)
            assert active is not None
            assert int(active["wave"]) == 1
            assert tuple(item["id"] for item in active["candidates"]) == expected_successor
            assert store.frontier_counts(campaign_id)["queued"] == 1
            assert len(arms.list_arm_observations(manifest.target.id, scenario)) == 2
