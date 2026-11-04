from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import httpx
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
from merlin_agents.campaign import compile_execution_agenda, scenario_fingerprint_for
from merlin_agents.campaign_service import CampaignService, _arm_task_observation, _json_value
from merlin_assessments import ReviewedApiTargetTool, StatefulTargetFixtureServer
from merlin_config import ConfigPaths, CredentialCheckpointVault
from merlin_contracts import ModelCompleted, TextDelta, ToolCall
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


async def _allow(*_args: object) -> bool:
    return True


class ThreeLevelTarget(StatefulTargetFixtureServer):
    def adapter(self) -> dict[str, object]:
        adapter = super().adapter()
        capabilities = adapter["state_capabilities"]
        assert isinstance(capabilities, dict)
        observations = capabilities["observations"]
        assert isinstance(observations, list)
        for item in observations:
            if item["name"] == "progress_stage":
                item["name"] = "level_number"
            elif item["name"] == "objective_score":
                item["name"] = "score"
            elif item["name"] == "remaining_budget":
                item["maximum_value"] = 100
        rules = capabilities["corroboration_rules"]
        assert isinstance(rules, list)
        rules[0]["observation_names"] = ["score", "host_score"]
        observations.append(
            {
                "name": "target_score",
                "role": "custom",
                "baseline_selector": {
                    "step_id": "start",
                    "evaluation_root": "response_body",
                    "pointer": "/state/targetScore",
                },
                "result_selector": {
                    "step_id": "interaction",
                    "evaluation_root": "response_body",
                    "pointer": "/state/targetScore",
                },
                "value_type": "integer",
                "trust_class": "target_reported",
            }
        )
        return adapter

    def __post_init__(self) -> None:
        super().__post_init__()
        self.feedback_level = 1
        self.feedback_score = 0

    def _response(
        self, method: str, operation: str, request: dict[str, object]
    ) -> tuple[int, dict[str, object]]:
        status, payload = super()._response(method, operation, request)
        if operation == "start" and status == 200:
            if self.feedback_score >= 40 and self.feedback_level < 3:
                self.feedback_level += 1
                self.feedback_score = 0
            state = self._states[self._leases[str(payload["leaseId"])]]
            state["stage"] = self.feedback_level
            state["score"] = self.feedback_score
            state["targetScore"] = 40
            state["turnsRemaining"] = 100
        elif operation == "message" and status == 200:
            message = str(request.get("message") or "")
            improves = (
                message in {"payload:direct/base", "payload:direct/replan"}
                and self.feedback_score < 40
                and (message != "payload:direct/base" or self.feedback_score == 0)
            ) or (message == "payload:direct/mutation" and self.feedback_level != 2)
            self.feedback_score = min(40, self.feedback_score + (20 if improves else 0))
            state = self._states[self._leases[str(payload["leaseId"])]]
            state["stage"] = self.feedback_level
            state["score"] = self.feedback_score
            state["targetScore"] = 40
            state["turnsRemaining"] = 100
            state["status"] = (
                "won" if self.feedback_level == 3 and self.feedback_score == 40 else "active"
            )
        else:
            return status, payload
        projected = dict(state)
        payload["state"] = projected
        payload["hostState"] = dict(projected)
        return status, payload


class DeterministicProvider:
    capabilities = SimpleNamespace(vision=False, context_window_tokens=128_000)

    def __init__(self, technique_ids: tuple[str, ...]) -> None:
        self.technique_ids = technique_ids
        self.active = 0
        self.high_water = 0
        self.calls = 0

    async def stream(self, request: object):
        self.active += 1
        self.high_water = max(self.high_water, self.active)
        try:
            messages = request.messages
            assignment = str(messages[1].content or "")
            technique_id = next(item for item in self.technique_ids if item in assignment)
            if any(message.role == "tool" for message in messages):
                yield TextDelta(
                    text=json.dumps(
                        {
                            "attempted_techniques": [
                                {
                                    "technique_id": technique_id,
                                    "outcome": "unclear",
                                    "evidence_refs": [f"fixture-call-{self.calls}"],
                                }
                            ],
                            "hypothesis_proposals": [],
                        }
                    )
                )
                yield ModelCompleted(finish_reason="stop")
                return
            self.calls += 1
            yield ToolCall(
                call_id=f"fixture-call-{self.calls}",
                name="target_request",
                arguments={
                    "input": f"payload:{technique_id}",
                    "strategy_plan": {
                        "observation": "deterministic fixture",
                        "thought": "execute the selected bounded candidate",
                        "strategy": ["direct"],
                        "response": technique_id,
                    },
                },
            )
            yield ModelCompleted(finish_reason="tool_calls")
        finally:
            self.active -= 1


class ArmHistory:
    def __init__(self, arms: dict[str, dict[str, object]]) -> None:
        self.arms = arms
        self.observations: list[dict[str, object]] = []

    def list_arms(self, _target_id: str, _scenario: str):
        return list(self.arms.values())

    def list_arm_observations(self, _target_id: str, _scenario: str):
        return list(self.observations)


def _technique(technique_id: str, semantic_id: str, domain_id: str) -> AssignmentTechnique:
    return AssignmentTechnique(
        id=technique_id,
        objective=f"Execute {technique_id} through the reviewed user channel",
        tags=frozenset({"prompt-injection", "text", "single", domain_id}),
        guidance_ids=("case:feedback",),
        semantic_id=semantic_id,
        execution_role="direct",
        domain_id=domain_id,
        max_target_exchanges=1,
    )


def _arm(manifest: CampaignManifest, technique: AssignmentTechnique) -> dict[str, object]:
    return {
        "id": f"arm:{technique.id}",
        "target_id": manifest.target.id,
        "scenario_fingerprint": scenario_fingerprint_for(
            manifest.target, manifest.catalogue
        ),
        "surface_id": technique.domain_id,
        "semantic_id": technique.resolved_semantic_id,
        "strategy_id": technique.strategy_id,
        "technique_id": technique.id,
        "controller_id": technique.strategy_id,
        "mutation_id": technique.id.rsplit("/", 1)[-1],
        "actual_language": "en",
        "state_id": "shared",
        "objective": technique.objective,
        "source": "seed",
        "status": "trial",
        "rank_components": {"catalogue_seed": 1.0},
        "eligibility_reasons": ("reviewed fixture frontier",),
        "decision_reason": "deterministic feedback fixture",
    }


@pytest.mark.asyncio
async def test_three_level_attack_feedback_uses_native_score_to_mutate_replan_and_stop(
    tmp_path: Path,
) -> None:
    techniques = (
        _technique("relay/authority", "family/relay", "agent-relay"),
        _technique("direct/base", "family/direct", "direct-user-channel"),
        _technique("direct/mutation", "family/direct", "direct-user-channel"),
        _technique("direct/stalled", "family/direct", "direct-user-channel"),
        _technique("direct/replan", "family/replan", "direct-user-channel"),
        _technique("relay/encoded", "family/relay", "agent-relay"),
    )
    technique_by_id = {item.id: item for item in techniques}
    expected = [
        "direct/base",
        "direct/mutation",
        "direct/base",
        "direct/mutation",
        "direct/stalled",
        "direct/stalled",
        "direct/replan",
        "direct/base",
        "direct/mutation",
    ]
    async with ThreeLevelTarget(
        maximum_parallel_states=1, input_limit=128, state_mode="fresh"
    ) as target:
        adapter = target.adapter()
        campaign_id = "campaign/native-feedback-fixture"
        manifest = CampaignManifest.create(
            id=campaign_id,
            session_id="session/native-feedback-fixture",
            objective="Reach each level's native target score",
            profile=CampaignProfile.RECOMMENDED,
            target=ReviewedTargetAdapter(
                id="target/native-feedback-fixture",
                interface="http_api",
                endpoint=target.origin + "/api/message",
            ),
            provider_id="provider",
            provider_model="model",
            provider_effort="default",
            authority_ids=("target:reviewed",),
            catalogue=(
                ContextDocument.create("case:feedback", "Deterministic direct user-message case"),
                ContextDocument.create(
                    "target:reviewed-interaction-contract",
                    "Reviewed interaction contract\n"
                    + json.dumps(adapter, sort_keys=True, separators=(",", ":")),
                ),
            ),
            skill_documents=(),
            tool_ids=("target_request",),
            budget=AssignmentBudget(max_turns=4, max_seconds=60),
        )
        history = ArmHistory({item.id: _arm(manifest, item) for item in techniques})
        provider = DeterministicProvider(tuple(technique_by_id))
        attempts: list[str] = []
        states: list[dict[str, object]] = []
        native: list[object] = []
        with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                session = SessionRepository(connection).create("native feedback fixture")
                manifest = CampaignManifest.create(
                    **{
                        **{
                            name: getattr(manifest, name)
                            for name in (
                                "id",
                                "objective",
                                "profile",
                                "target",
                                "provider_id",
                                "provider_model",
                                "provider_effort",
                                "authority_ids",
                                "catalogue",
                                "skill_documents",
                                "tool_ids",
                                "budget",
                            )
                        },
                        "session_id": str(session.id),
                    }
                )
                context = AssignmentContext(
                    target_profile_id=manifest.target.id,
                    techniques=techniques,
                    tool_ids=manifest.tool_ids,
                    skill_ids=(),
                    provider_id=manifest.provider_id,
                    authority_ids=manifest.authority_ids,
                    budget=manifest.budget,
                    input_schema='{"type":"object"}',
                    output_schema='{"type":"object"}',
                    provider_model=manifest.provider_model,
                )
                campaign_store = CampaignStore(connection)
                campaign_store.prepare(
                    campaign_id,
                    str(session.id),
                    {
                        "manifest": _json_value(asdict(manifest)),
                        "assignment_context": _json_value(asdict(context)),
                    },
                    manifest.digest,
                )
                scheduler = DurableSchedulerStore(connection)
                async with httpx.AsyncClient() as client:
                    api_target = ReviewedApiTargetTool(
                        CampaignApiSessionRepository(connection),
                        lambda _campaign_id: adapter,
                        {},
                        checkpoint_vault=CredentialCheckpointVault(
                            ConfigPaths(tmp_path / "credentials")
                        ),
                        client=client,
                    )
                    broker = ToolBroker(
                        (api_target,),
                        confirmer=_allow,
                        effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
                    )
                    agents = DurableAgentService(
                        provider,
                        broker,
                        scheduler,
                        model=manifest.provider_model,
                        workspace_root=str(tmp_path),
                        lease_seconds=30,
                        campaign_cleanup=api_target.cleanup_assignment,
                    )
                    campaigns = CampaignService(
                        campaign_store,
                        agents,
                        None,
                        arm_store=history,
                        max_worker_concurrency=1,
                    )
                    for wave in range(len(expected) + 1):
                        ordered = await campaigns._prioritize_execution_candidates(  # noqa: SLF001
                            campaign_id,
                            manifest,
                            techniques,
                            wave,
                            selection_width=1,
                        )
                        if not ordered:
                            break
                        selected = ordered[0]
                        selected_context = replace(context, techniques=(selected,))
                        compiled = compile_execution_agenda(
                            manifest,
                            selected_context,
                            compile_attack_campaign(manifest, selected_context),
                            include_discovery=False,
                        )
                        assignment = next(
                            item
                            for item in compiled.assignments
                            if item.technique_ids == (selected.id,)
                        )
                        live = await agents.submit(
                            str(session.id),
                            (assignment,),
                            max_workers=1,
                            minimum_workers=1,
                            worker_contexts={
                                item.assignment_id: item for item in compiled.worker_contexts
                            },
                        )
                        assert live.execution is not None
                        await asyncio.wait_for(live.execution, timeout=30)
                        tasks = tuple(await agents.list_schedule_tasks(live.id))
                        assert len(tasks) == 1 and tasks[0].status == "completed", next(
                            item["error"]
                            for item in scheduler.list_attempts(live.id)
                            if item["node_id"].startswith("worker:")
                        )
                        evidence = await campaigns._source_evidence(str(live.id), tasks)  # noqa: SLF001
                        observation = _arm_task_observation(
                            campaign_id,
                            tasks[0],
                            history.arms[selected.id],
                            evidence[tasks[0].id],
                            contract_override={"id": selected.id},
                        )
                        assert observation is not None
                        history.observations.append(dict(observation))
                        attempts.append(selected.id)
                        states.append(dict(target.http_log[-1]["response"]["state"]))
                        native.append(observation["native_score_observation"])
                        if wave == 0:
                            assert selected.id == "direct/base", (
                                "first attempt must use the direct user channel, not an "
                                f"unrelated relay family: {selected.id}"
                            )
                            assert observation["native_score_observation"] is not None
                    else:
                        pytest.fail(
                            "campaign did not stop after the final native target: "
                            f"attempts={attempts!r}, states={states!r}"
                        )

    assert attempts == expected
    assert provider.high_water == 1
    assert sum(item["operation"] == "message" for item in target.http_log) == len(expected)
    assert [(item["stage"], item["score"], item["targetScore"]) for item in states] == [
        (1, 20, 40),
        (1, 40, 40),
        (2, 20, 40),
        (2, 20, 40),
        (2, 20, 40),
        (2, 20, 40),
        (2, 40, 40),
        (3, 20, 40),
        (3, 40, 40),
    ]
    assert [item["outcome"] for item in native] == [
        "progress",
        "progress",
        "progress",
        "unclear",
        "unclear",
        "unclear",
        "progress",
        "progress",
        "progress",
    ]
    assert states[-1]["status"] == "won"
