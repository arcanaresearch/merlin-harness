"""Wiring proof: generative arm discovery feeds the live exploration loop.

This is a fixture-driven test with no network, daemon, or provider: a stub
generator and stub arm store prove that a real campaign's exploration wave
calls the orchestrator, persists the admitted arm, journals the generated-arm
summary, and schedules the resulting typed hypothesis through the same
admission path as Lead-derived hypotheses.
"""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

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
    DynamicSpecialistProposal,
    ExplorationCriticDecision,
    ExplorationExperimentPlan,
    FrontierHypothesis,
    LeadAssessment,
    LeadFollowUp,
    ReviewedTargetAdapter,
)
from merlin_agents.campaign_service import _arm_task_observation, _hypothesis_task_observation
from merlin_contracts import AgentTask, project_candidate_outcome

_GENERATED_OBJECTIVE = (
    "Probe cross-tenant isolation between two provisioned identities on the reviewed target."
)


def test_hypothesis_failure_requires_typed_infrastructure_category() -> None:
    task = AgentTask(
        id="arm-worker",
        objective="sealed hypothesis",
        status="quarantined",
        technique_ids=("hypothesis:one",),
    )
    contract = {"id": "hypothesis:one"}
    base = {
        "attempt_id": "attempt:one",
        "reference": "receipt:one",
        "digest": "sha256:" + "a" * 64,
    }

    ambiguous = _hypothesis_task_observation("campaign-1", task, contract, base)
    typed = _hypothesis_task_observation(
        "campaign-1",
        task,
        contract,
        {**base, "infrastructure_failure_category": "missing_response_pointer"},
    )
    completed_typed = _hypothesis_task_observation(
        "campaign-1",
        task.model_copy(update={"status": "completed"}),
        contract,
        {**base, "infrastructure_failure_category": "missing_response_pointer"},
    )

    assert ambiguous["outcome"] == "ambiguous"
    assert typed["outcome"] == "infrastructure_failure"
    assert typed["infrastructure_failure_category"] == "missing_response_pointer"
    assert completed_typed["outcome"] == "infrastructure_failure"
    assert completed_typed["signal"] is None


def test_arm_observation_requires_an_acknowledged_target_attempt() -> None:
    arm = {
        "id": "arm:one",
        "target_id": "target-1",
        "scenario_fingerprint": "scenario-1",
        "surface_id": "chat",
        "technique_id": "technique-1",
        "controller_id": "direct",
        "mutation_id": "baseline",
        "actual_language": "en",
        "state_id": "fresh",
    }
    contract = json.dumps({"id": "hypothesis:one", "arm": arm})
    task = AgentTask(
        id="arm-worker",
        objective=f"Run. Sealed hypothesis contract: {contract}",
        status="completed",
        result="{}",
        technique_ids=("hypothesis:one",),
    )

    assert (
        _arm_task_observation(
            "campaign-1",
            task,
            arm,
            {
                "attempt_id": "attempt:one",
                "reference": "receipt:one",
                "digest": "sha256:" + "a" * 64,
                "target_call_ids": (),
            },
        )
        is None
    )
    infrastructure = _arm_task_observation(
        "campaign-1",
        task,
        arm,
        {
            "attempt_id": "attempt:one",
            "reference": "receipt:one",
            "digest": "sha256:" + "a" * 64,
            "target_call_ids": ("call:one",),
            "infrastructure_failure_category": "missing_response_pointer",
        },
    )
    assert infrastructure is not None
    assert infrastructure["outcome"] == "infrastructure_failure"
    assert infrastructure["signal"] is None


def test_arm_observation_retains_target_signal_when_transform_attribution_is_uncertain() -> None:
    arm = {
        "id": "arm:transform-mismatch",
        "target_id": "target-1",
        "scenario_fingerprint": "scenario-1",
        "surface_id": "chat",
        "technique_id": "technique-1",
        "controller_id": "direct",
        "mutation_id": "encoding/base64",
        "actual_language": "en",
        "state_id": "fresh",
        "execution_reachability": {
            "requested_transform_id": "encoding/base64",
            "applied_transform_id": "baseline",
            "transform_credit_eligible": False,
        },
    }
    contract = json.dumps({"id": "hypothesis:one", "arm": arm})
    task = AgentTask(
        id="arm-worker",
        objective=f"Run. Sealed hypothesis contract: {contract}",
        status="completed",
        result=json.dumps({"outcome": "near_miss", "signal": 0.6}),
        technique_ids=("hypothesis:one",),
    )

    observation = _arm_task_observation(
        "campaign-1",
        task,
        arm,
        {
            "attempt_id": "attempt:one",
            "reference": "receipt:one",
            "digest": "sha256:" + "a" * 64,
            "target_call_ids": ("call:one",),
            "attempt_disposition": "valid",
            "outcome_authority": "target",
        },
    )

    assert observation is not None
    assert observation["outcome"] == "partial_signal"
    assert observation["signal"] == pytest.approx(0.6)
    assert observation["distance_to_success"] == pytest.approx(0.4)
    assert observation["transform_credit_eligible"] is False
    uncertainty = observation["evaluation_uncertainties"][0]
    assert uncertainty["withhold_transform_credit"] is True
    assert uncertainty["affects"] == ["transform_attribution"]


def test_worker_partial_outcome_without_valid_signal_is_ambiguous() -> None:
    task = AgentTask(
        id="arm-worker",
        objective="sealed hypothesis",
        status="completed",
        technique_ids=("hypothesis:one",),
    )
    contract = {"id": "hypothesis:one"}
    committed = {
        "attempt_id": "attempt:one",
        "reference": "receipt:one",
        "digest": "sha256:" + "a" * 64,
        "attempt_disposition": "valid",
        "outcome_authority": "target",
    }

    for document in (
        {"outcome": "near_miss"},
        {"outcome": "partial", "signal": "high"},
        {"outcome": "near_miss", "signal": 1.5},
        {"outcome": "near_miss", "signal": True},
    ):
        measured = _hypothesis_task_observation(
            "campaign-1",
            task.model_copy(update={"result": json.dumps(document)}),
            contract,
            committed,
        )
        assert measured is not None
        assert measured["outcome"] == "ambiguous", document
        assert measured["signal"] is None
        assert measured["distance_to_success"] is None
        assert "without a valid numeric signal" in str(measured["reason"])

    supported = _hypothesis_task_observation(
        "campaign-1",
        task.model_copy(update={"result": json.dumps({"outcome": "partial", "signal": 0.25})}),
        contract,
        committed,
    )
    assert supported is not None
    assert supported["outcome"] == "partial_signal"
    assert supported["signal"] == pytest.approx(0.25)
    assert supported["distance_to_success"] == pytest.approx(0.75)


async def test_unsupported_worker_partial_claim_earns_no_lifecycle_signal() -> None:
    arm = {
        "id": "arm:unsupported-partial",
        "target_id": "target-1",
        "scenario_fingerprint": "scenario-1",
        "surface_id": "chat",
        "technique_id": "technique-1",
        "controller_id": "direct",
        "mutation_id": "baseline",
        "actual_language": "en",
        "state_id": "fresh",
    }
    contract = json.dumps({"id": "hypothesis:one", "arm": arm})
    task = AgentTask(
        id="arm-worker",
        objective=f"Run. Sealed hypothesis contract: {contract}",
        status="completed",
        result=json.dumps({"outcome": "near_miss"}),
        technique_ids=("hypothesis:one",),
    )

    class ArmStore:
        def __init__(self) -> None:
            self.observations = []
            self.lifecycle = []

        def record_arm_observation(self, observation):
            self.observations.append(dict(observation))

        def list_arm_observations(self, _target_id, _scenario):
            return [
                {
                    "identity": {
                        key: arm[key]
                        for key in (
                            "surface_id",
                            "technique_id",
                            "controller_id",
                            "mutation_id",
                            "actual_language",
                            "state_id",
                        )
                    },
                    "outcome": "ambiguous",
                    "partial_signal": 0.0,
                }
            ]

        def set_lifecycle(self, arm_id, status):
            self.lifecycle.append((arm_id, status))

        def transition_hypothesis(self, _observation):
            return None

    arm_store = ArmStore()
    service = CampaignService(object(), object(), object(), arm_store=arm_store)  # type: ignore[arg-type]
    evidence = {
        task.id: {
            "attempt_id": "attempt:one",
            "reference": "receipt:one",
            "digest": "sha256:" + "a" * 64,
            "target_call_ids": ("call:one",),
        }
    }

    await service._record_arm_outcomes("campaign-1", (task,), evidence)  # noqa: SLF001

    assert len(arm_store.observations) == 1
    observation = arm_store.observations[0]
    assert observation["outcome"] == "ambiguous"
    assert observation["signal"] is None
    assert observation["distance_to_success"] is None
    assert arm_store.lifecycle == []


def _preparation(
    session_id: str,
    profile: CampaignProfile = CampaignProfile.EXPLORATION,
    specialist_composition: str = "opinionated",
) -> CampaignPreparation:
    budget = AssignmentBudget(256, 1_048_576, 14_400)
    manifest = CampaignManifest.create(
        id="campaign-1",
        session_id=session_id,
        objective="Explore target",
        profile=profile,
        target=ReviewedTargetAdapter("target-1", "api", "https://target.test", "model"),
        provider_id="provider-1",
        provider_model="model-1",
        provider_effort="default",
        authority_ids=(f"session:{session_id}", "target:target-1"),
        catalogue=(ContextDocument.create("case:prompt", "Test prompt injection"),),
        skill_documents=(ContextDocument.create("skill:review", "Review prompt evidence"),),
        tool_ids=("target_chat",),
        budget=budget,
        specialist_composition=specialist_composition,
    )
    context = AssignmentContext(
        target_profile_id="target-1",
        techniques=(
            AssignmentTechnique(
                "prompt", "Test prompt injection", frozenset({"prompt"}), ("case:prompt",)
            ),
        ),
        tool_ids=("target_chat",),
        skill_ids=("skill:review",),
        provider_id="provider-1",
        authority_ids=manifest.authority_ids,
        budget=budget,
        input_schema="input/v1",
        output_schema="output/v1",
    )
    return CampaignPreparation(manifest, context)


def _admit_followup(
    campaign_id: str,
    profile_id: str,
    objective: str,
    source_task_id: str,
    hypothesis: FrontierHypothesis,
) -> LeadFollowUp:
    decision = ExplorationCriticDecision(
        id=f"critic:{hypothesis.id}",
        campaign_id=campaign_id,
        hypothesis_id=hypothesis.id,
        verdict="accept",
        evidence_refs=(f"receipt:{source_task_id}",),
        closest_known_technique_id="catalogue:closest",
        meaningful_difference_claim="target-derived behavior differs materially",
        alternative_explanation="the observed difference may be transient",
        falsification_test="repeat both arms against the same reviewed state",
        rationale="independent semantic critic accepted the bounded experiment",
        confidence="medium",
        limitations=("requires target execution",),
    )
    plan = ExplorationExperimentPlan(
        id=f"experiment:{hypothesis.id}",
        campaign_id=campaign_id,
        hypothesis_id=hypothesis.id,
        critic_decision_id=decision.id,
        treatment="exercise the sealed target-derived mutation",
        control="repeat without the target-derived mutation",
        expected_treatment_observation="the reviewed predicate changes",
        expected_control_observation="the reviewed predicate remains stable",
        oracle="compare the sealed objective predicate across both arms",
        safety_constraints=("stay within reviewed target authority",),
        cleanup="run the reviewed cleanup contract for both arms",
    )
    return LeadFollowUp(profile_id, objective, (source_task_id,), hypothesis, decision, plan)


@pytest.mark.parametrize(
    ("profile", "composition", "lead_initially_complete"),
    (
        (CampaignProfile.EXPLORATION, "opinionated", False),
        (CampaignProfile.RECOMMENDED, "opinionated", False),
        (CampaignProfile.RECOMMENDED, "autonomous", True),
    ),
)
async def test_exploration_wave_generates_and_schedules_arm(
    profile: CampaignProfile,
    composition: str,
    lead_initially_complete: bool,
) -> None:
    prepared = _preparation("session-1", profile, composition)
    if composition == "autonomous":
        # More than the adaptive floor proves Autonomous runs discovery because
        # it is its lifecycle, not only as a rescue for a tiny initial frontier.
        prepared = replace(
            prepared,
            assignment_context=replace(
                prepared.assignment_context,
                techniques=tuple(
                    AssignmentTechnique(
                        f"prompt-{index}",
                        f"Test prompt-injection variant {index}",
                        frozenset({"prompt"}),
                        ("case:prompt",),
                    )
                    for index in range(12)
                ),
            ),
        )

    class SelectionStore:
        def __init__(self) -> None:
            self.lead = []
            self.terminal = None

        def finish_wave(self, *_args):
            return None

        def add_wave(self, *_args):
            return None

        def append_lead(self, _campaign_id, wave, payload):
            self.lead.append((wave, dict(payload)))

        def finish(self, _campaign_id, status, *, stop_reason=None):
            self.terminal = (status, stop_reason)

        def list_waves(self, _campaign_id):
            return [{"schedule_id": "recon-wave", "wave": 0}]

    class SelectionAgents:
        def __init__(self) -> None:
            self.index = 0
            self.assignments = []
            self.tasks = {
                "recon-wave": (
                    AgentTask(
                        id="campaign-1/recon",
                        objective="map reviewed target surface",
                        status="completed",
                        result="{}",
                    ),
                ),
                "initial": (
                    AgentTask(
                        id="source",
                        objective="record target boundary",
                        status="completed",
                        result="{}",
                        technique_ids=tuple(
                            technique.id for technique in prepared.assignment_context.techniques
                        ),
                    ),
                ),
            }

        async def list_schedule_tasks(self, schedule_id):
            return self.tasks[schedule_id]

        async def committed_task_result_evidence(self, schedule_id, task):
            encoded_contract = task.objective.partition("Sealed hypothesis contract:")[2]
            contract = json.loads(encoded_contract) if encoded_contract else {}
            arm = contract.get("arm", {})
            reachability = arm.get("execution_reachability", {})
            requested_transform = reachability.get("requested_transform_id", "baseline")
            applied_transform = (
                requested_transform if reachability.get("locally_executable") else None
            )
            return {
                "digest": "sha256:" + "a" * 64,
                "reference": f"receipt:{schedule_id}:{task.id}",
                "attempt_id": f"attempt:{schedule_id}:{task.id}",
                "target_call_ids": (f"call:{schedule_id}:{task.id}",),
                "arm_execution_context": {
                    "surface_id": "reviewed-chat-endpoint",
                    "actual_language": "fr",
                    "state_id": "fresh-anonymous-session",
                },
                "target_delivery_receipts": {
                    f"call:{schedule_id}:{task.id}": {
                        "requested_transform_id": requested_transform,
                        "applied_transform_id": applied_transform,
                        "actual_language": "en" if applied_transform else "und",
                        "payload_digest": "sha256:" + "e" * 64,
                        "receipt_digest": "sha256:" + "d" * 64,
                        "language_mismatch": False,
                    }
                },
            }

        async def submit(self, _session_id, assignments, **_options):
            self.index += 1
            schedule_id = f"wave-{self.index}"
            values = tuple(assignments)
            self.assignments.append(values)
            self.tasks[schedule_id] = tuple(
                AgentTask(
                    id=item.id,
                    objective=item.objective,
                    status="completed",
                    result="{}",
                    technique_ids=item.technique_ids,
                )
                for item in values
            )
            return SimpleNamespace(id=schedule_id, execution=None)

        def start_execution(self, live, *, max_workers):
            del max_workers
            live.execution = asyncio.create_task(asyncio.sleep(0))

    class StubArmStore:
        def __init__(self) -> None:
            self.recorded = []
            self.observations = []
            self.lifecycle = []
            self.hypothesis_transitions = []

        def list_arms(self, target_id, scenario_fingerprint):
            assert target_id == "target-1"
            assert scenario_fingerprint
            return []

        def record_arm(self, arm):
            self.recorded.append(dict(arm))

        def list_arm_observations(self, target_id, scenario_fingerprint):
            assert target_id == "target-1"
            assert scenario_fingerprint
            return list(self.observations)

        def record_arm_observation(self, observation):
            self.observations.append(dict(observation))

        def transition_hypothesis(self, observation):
            self.hypothesis_transitions.append(dict(observation))

        def set_lifecycle(self, arm_id, status):
            self.lifecycle.append((arm_id, status))

    def arm_generator(prompt: str) -> list[dict[str, object]]:
        assert "proposing novel assessment arms" in prompt
        return [
            {
                "technique_id": "arm/generated",
                "objective": _GENERATED_OBJECTIVE,
                "semantic_id": "cross-tenant-isolation",
                "strategy_id": "direct",
                "rationale": "derived from committed reconnaissance receipts",
            }
        ]

    initial_followups = (
        (
            LeadFollowUp(
                "dynamic-prompt-portfolio",
                "Test the complete reviewed prompt-injection frontier",
                ("source",),
                specialist=DynamicSpecialistProposal(
                    campaign_id=prepared.manifest.id,
                    label="dynamic-prompt-portfolio",
                    objective="Test the complete reviewed prompt-injection frontier",
                    source_task_ids=("source",),
                    evidence_refs=("receipt:source",),
                    evidence_expectation="attributable target receipts",
                    technique_ids=tuple(
                        technique.id for technique in prepared.assignment_context.techniques
                    ),
                ),
            ),
        )
        if composition == "autonomous"
        else (LeadFollowUp("adversarial-explorer", "repeat the reviewed prompt case", ("source",)),)
    )
    decisions = iter(
        (
            LeadAssessment(
                "rank frontier",
                lead_initially_complete,
                followups=initial_followups,
            ),
            LeadAssessment("frontier wave complete", True),
        )
    )

    async def lead(*_args):
        return next(decisions)

    store = SelectionStore()
    agents = SelectionAgents()
    arm_store = StubArmStore()
    service = CampaignService(
        store,
        agents,
        preparation_factory=_preparation,
        lead_runner=lead,
        arm_store=arm_store,
        arm_generator=arm_generator,
    )  # type: ignore[arg-type]

    async def admit_selected(_campaign_id, _profile, _wave, followups, _evidence, **_scope):
        return tuple(
            replace(
                _admit_followup(
                    prepared.manifest.id,
                    followup.profile_id,
                    followup.objective,
                    followup.source_task_ids[0],
                    followup.hypothesis,
                ),
                arm=followup.arm,
            )
            for followup in followups
            if followup.hypothesis is not None
        )

    service._review_frontier_followups = admit_selected  # type: ignore[method-assign]  # noqa: SLF001

    await service._supervise(  # noqa: SLF001
        prepared.manifest.id,
        prepared.manifest,
        prepared.assignment_context,
        SimpleNamespace(id="initial", execution=None),
    )

    # Reviewed catalog mutations and free-form brainstorm arms share the same
    # durable pool rather than living in disconnected execution paths.
    assert {arm["source"] for arm in arm_store.recorded} == {
        "catalog-mutation",
        "brainstorm",
    }, store.lead
    assert all(arm["target_id"] == "target-1" for arm in arm_store.recorded)
    assert all(arm["scenario_fingerprint"] for arm in arm_store.recorded)
    assert any(arm["objective"] == _GENERATED_OBJECTIVE for arm in arm_store.recorded)
    catalog_execution = tuple(
        arm["execution_reachability"]
        for arm in arm_store.recorded
        if arm["source"] == "catalog-mutation"
    )
    assert all(
        item["requested_instruction_digest"].startswith("sha256:") for item in catalog_execution
    )
    assert all(item["produced_payload_digest"].startswith("sha256:") for item in catalog_execution)
    local = next(item for item in catalog_execution if item["locally_executable"])
    model_mediated = next(item for item in catalog_execution if not item["locally_executable"])
    assert local["execution_class"] == "locally_exact"
    assert local["transform_credit_eligible"] is True
    assert model_mediated["applied_transform_id"] is None
    assert model_mediated["actual_language"] == "und"
    assert model_mediated["verification_status"] == "unverifiable"
    assert model_mediated["execution_class"] == "unavailable"
    assert model_mediated["transform_credit_eligible"] is False
    assert arm_store.observations
    assert arm_store.observations[0]["outcome"] == "ambiguous"
    assert str(arm_store.observations[0]["hypothesis_id"]).startswith("hypothesis:")
    assert (
        arm_store.hypothesis_transitions[0]["hypothesis_id"]
        == arm_store.observations[0]["hypothesis_id"]
    )
    assert set(arm_store.observations[0]["identity"]) == {
        "target_id",
        "scenario_fingerprint",
        "surface_id",
        "technique_id",
        "controller_id",
        "mutation_id",
        "actual_language",
        "state_id",
    }

    # The generated-arm summary is journaled alongside the seed selection.
    selection = next(
        (
            payload["selection"] if payload.get("kind") == "exploration_selection" else payload
            for _, payload in store.lead
            if payload.get("kind") in {"exploration_selection", "adaptive_arm_selection"}
        ),
        None,
    )
    assert selection is not None, store.lead
    assert selection.get("generated_arms"), selection
    assert "one ARM frontier" in selection["arm_bandit"]["history_policy"]
    assert "no positive quality signal" in selection["arm_bandit"]["history_policy"]
    frontier = selection["frontier_selection"] if "frontier_selection" in selection else selection
    assert frontier["rankings"]
    assert all(item["components"] for item in frontier["rankings"])
    assert selection["generated_arms"][0]["source"] == "catalog-mutation"
    assert selection["generated_arms"][0]["axis_coords"]
    if composition == "autonomous":
        assert selection["arm_bandit"]["requested_candidate_floor"] == 10
        assert selection["arm_bandit"]["valid_new_candidates"] >= 10

    # The generated arm reached scheduling as a typed hypothesis on the
    # structural explorer lane.
    assert agents.assignments, "no follow-up wave was dispatched"
    scheduled = agents.assignments[0]
    scheduled_arm_ids = tuple(
        json.loads(assignment.objective.partition("Sealed hypothesis contract:")[2])["arm"]["id"]
        for assignment in scheduled
        if "Sealed hypothesis contract:" in assignment.objective
    )
    assert scheduled_arm_ids == tuple(
        item["id"] for item in selection["generated_arms"][: len(scheduled_arm_ids)]
    )
    assert any(
        assignment.technique_ids
        and assignment.technique_ids[0].startswith("hypothesis:")
        and "Reviewed adaptive mutation" in assignment.objective
        for assignment in scheduled
    ), scheduled


async def test_missing_hypothesis_record_does_not_fail_arm_outcome_recording() -> None:
    arm = {
        "id": "arm:missing-hypothesis",
        "target_id": "target-1",
        "scenario_fingerprint": "scenario-1",
        "surface_id": "chat",
        "technique_id": "technique-1",
        "controller_id": "direct",
        "mutation_id": "baseline",
        "actual_language": "en",
        "state_id": "fresh",
    }
    contract = json.dumps({"id": "hypothesis:missing", "arm": arm})
    task = AgentTask(
        id="arm-worker",
        objective=f"Run. Sealed hypothesis contract: {contract}",
        status="completed",
        result=json.dumps({"outcome": "near_miss", "signal": 0.6}),
        technique_ids=("hypothesis:missing",),
    )

    class Store:
        def __init__(self) -> None:
            self.journal = []

        def append_lead(self, _campaign_id, wave, payload):
            self.journal.append((wave, payload))

    class ArmStore:
        def __init__(self) -> None:
            self.observations = []

        def record_arm_observation(self, observation):
            self.observations.append(dict(observation))

        def list_arm_observations(self, _target_id, _scenario):
            return []

        def set_lifecycle(self, arm_id, status):
            raise AssertionError("lifecycle is not exercised by this test")

        def transition_hypothesis(self, observation):
            raise KeyError(str(observation["hypothesis_id"]))

    store = Store()
    arm_store = ArmStore()
    service = CampaignService(store, object(), object(), arm_store=arm_store)  # type: ignore[arg-type]
    evidence = {
        task.id: {
            "attempt_id": "attempt:one",
            "reference": "receipt:one",
            "digest": "sha256:" + "a" * 64,
            "target_call_ids": ("call:one",),
        }
    }

    await service._record_arm_outcomes("campaign-1", (task,), evidence, wave=3)  # noqa: SLF001

    assert len(arm_store.observations) == 1
    assert len(store.journal) == 1
    wave, payload = store.journal[0]
    assert wave == 3
    assert payload["kind"] == "hypothesis_outcome_unrecorded"
    assert payload["hypothesis"] == "hypothesis:missing"
    assert payload["evaluation_uncertain"] is True


async def test_conflicting_candidate_projection_does_not_abort_outcome_recording() -> None:
    arm = {
        "id": "arm:conflicting-projection",
        "target_id": "target-1",
        "scenario_fingerprint": "scenario-1",
        "surface_id": "chat",
        "technique_id": "technique-1",
        "controller_id": "direct",
        "mutation_id": "baseline",
        "actual_language": "en",
        "state_id": "fresh",
    }
    contract = json.dumps(
        {
            "id": "hypothesis:conflicting-projection",
            "arm": arm,
            "candidate_identity": {
                "campaign_id": "campaign-1",
                "target_id": "target-1",
                "target_generation": "generation-1",
                "scenario_fingerprint": "scenario-1",
                "technique_id": "hypothesis:conflicting-projection",
                "controller_id": "direct",
                "payload_family": "baseline",
                "requested_language": "en",
                "mutation_id": "baseline",
            },
        }
    )
    task = AgentTask(
        id="arm-worker",
        objective=f"Run. Sealed hypothesis contract: {contract}",
        status="completed",
        result="{}",
        technique_ids=("hypothesis:conflicting-projection",),
    )

    class Store:
        def __init__(self) -> None:
            self.journal = []

        def append_lead(self, _campaign_id, wave, payload):
            self.journal.append((wave, payload))

    class ArmStore:
        def record_arm_observation(self, _observation):
            raise AssertionError("invalid projection must not produce an arm observation")

    store = Store()
    service = CampaignService(store, object(), object(), arm_store=ArmStore())  # type: ignore[arg-type]
    evidence = {
        task.id: {
            "attempt_id": "attempt:one",
            "reference": "receipt:one",
            "target_call_ids": ("call:one",),
            "target_evidence_receipts": {
                "call:one": {
                    "candidate_outcome_projection": project_candidate_outcome(
                        candidate_id="sha256:" + "f" * 64,
                        attempt_disposition="valid",
                        outcome_authority="target",
                        outcome="confirmed",
                        evidence_digest="a" * 64,
                    )
                }
            },
        }
    }

    await service._record_arm_outcomes("campaign-1", (task,), evidence, wave=4)  # noqa: SLF001

    assert store.journal == [
        (
            4,
            {
                "kind": "candidate_outcome_unrecorded",
                "task_id": "arm-worker",
                "reason": "candidate outcome projection candidate id conflicts with settlement",
                "evaluation_uncertain": True,
                "retryable": True,
            },
        )
    ]
