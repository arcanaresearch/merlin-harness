"""Packaged-campaign execute reroute: wave-0 dynamic specialist composition."""

import asyncio
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

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
    LeadAssessment,
    LeadFollowUp,
    ReviewedTargetAdapter,
)
from merlin_agents.campaign import CampaignCompilation, compile_attack_campaign
from merlin_agents.campaign_service import _json_value
from merlin_agents.specialist_templates import template_by_id
from merlin_contracts import AgentTask


def _preparation(session_id, objective, profile, *, composition="opinionated"):
    budget = AssignmentBudget(256, 1_048_576, 14_400)
    manifest = CampaignManifest.create(
        id="campaign-1",
        session_id=session_id,
        objective=objective,
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
        specialist_composition=composition,
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


def _journal_kinds(store, kind: str) -> list[dict]:
    return [payload for _, payload in store.lead if payload.get("kind") == kind]


class ExecuteStore:
    """Prepared-campaign row plus the Lead journal the admission ledger replays."""

    def __init__(self, prepared: CampaignPreparation) -> None:
        self.lead: list[tuple[int, dict]] = []
        self.rows = {
            prepared.manifest.id: {
                "id": prepared.manifest.id,
                "session_id": prepared.manifest.session_id,
                "manifest": {
                    "manifest": _json_value(asdict(prepared.manifest)),
                    "assignment_context": _json_value(asdict(prepared.assignment_context)),
                },
                "manifest_digest": prepared.manifest.digest,
                "status": "prepared",
            }
        }

    def get(self, campaign_id):
        return self.rows.get(campaign_id)

    def begin_execution(self, campaign_id, digest, schedule_id):
        row = self.rows[campaign_id]
        assert row["manifest_digest"] == digest
        row.update(status="executing", schedule_id=schedule_id)
        return row

    def append_lead(self, _campaign_id, wave, payload):
        self.lead.append((wave, dict(payload)))
        return payload

    def list_lead(self, _campaign_id):
        return [{"wave": wave, "payload": payload} for wave, payload in self.lead]


class ReconAgents:
    def __init__(self) -> None:
        self.submissions = []
        self.started = []

    async def submit(self, session_id, assignments, **options):
        assert options["start_immediately"] is False
        values = tuple(assignments)
        self.submissions.append((values, options))
        return SimpleNamespace(
            id=options.get("schedule_id") or "schedule-1",
            execution=None,
            assignments=values,
        )

    def start_execution(self, live, *, max_workers):
        self.started.append((live.id, max_workers))


async def test_execute_admits_wave0_template_specialists_exactly_once() -> None:
    prepared = _preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    store = ExecuteStore(prepared)
    agents = ReconAgents()
    service = CampaignService(store, agents, _preparation)  # type: ignore[arg-type]

    result = await service.execute("session-1", "campaign-1", prepared.manifest.digest)

    assert result["status"] == "executing"
    template = template_by_id("merlin-core-specialists", 1)
    admitted = _journal_kinds(store, "dynamic_specialist_admitted")
    assert len(admitted) == len(template.entries)
    for event in admitted:
        assert event["record"]["admitted_wave"] == 0
        assert event["record"]["state"] == "active"
        assert event["creation"] == {
            "kind": "template",
            "template_id": "merlin-core-specialists",
            "template_version": 1,
        }
    # Wave 0 itself stays recon-only; specialists join the initial attack wave.
    ((recon_assignments, _),) = agents.submissions
    assert {item.profile_id for item in recon_assignments} == {"recon"}

    # Simulated restart: re-executing the still-prepared campaign reuses the
    # admitted identities instead of double-admitting.
    store.rows["campaign-1"]["status"] = "prepared"
    recovered = CampaignService(store, agents, _preparation)  # type: ignore[arg-type]
    replay = await recovered.execute("session-1", "campaign-1", prepared.manifest.digest)

    assert replay["status"] == "executing"
    assert len(_journal_kinds(store, "dynamic_specialist_admitted")) == len(template.entries)
    assert not _journal_kinds(store, "dynamic_specialist_rejected")


async def test_execute_schedule_carries_the_profile_grant_universe() -> None:
    """The submitted wave schedule carries the grant registry bootstrap reads."""
    prepared = _preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    store = ExecuteStore(prepared)
    agents = ReconAgents()
    service = CampaignService(store, agents, _preparation)  # type: ignore[arg-type]

    result = await service.execute("session-1", "campaign-1", prepared.manifest.digest)

    assert result["status"] == "executing"
    ((assignments, options),) = agents.submissions
    grants = options["grants"]
    assert assignments
    # Every compiled assignment's grant resolves in the registry, keyed by
    # grant id exactly as ``graph.get("grants", {})`` consumers expect.
    for assignment in assignments:
        assert assignment.grant_id is not None
        grant = grants[assignment.grant_id]
        assert grant.id == assignment.grant_id
        assert grant.effect_classes
    assert grants["profile:recon"].effect_classes >= {"target_read", "target_network"}
    assert grants["grant:offline-automation"].id == "grant:offline-automation"


async def test_execute_autonomous_admits_nothing_without_a_lead_decision() -> None:
    prepared = _preparation(
        "session-1",
        "Assess the reviewed target",
        CampaignProfile.RECOMMENDED,
        composition="autonomous",
    )
    store = ExecuteStore(prepared)
    agents = ReconAgents()
    service = CampaignService(store, agents, _preparation)  # type: ignore[arg-type]

    result = await service.execute("session-1", "campaign-1", prepared.manifest.digest)

    assert result["status"] == "executing"
    assert not _journal_kinds(store, "dynamic_specialist_admitted")
    assert not _journal_kinds(store, "dynamic_specialist_coverage_warning")
    ((recon_assignments, _),) = agents.submissions
    assert {item.profile_id for item in recon_assignments} == {"recon"}

    # The Lead boundary carries the initial-composition request until a typed
    # Lead composition arrives through the follow-up-wave path.
    projections = await service._lead_control_plane_tasks(  # noqa: SLF001
        prepared.manifest, ()
    )
    composition = next(
        json.loads(task.result or "{}")
        for task in projections
        if task.id.endswith("/lead-control/specialist_composition_get")
    )
    assert composition["specialist_composition"] == "autonomous"
    assert composition["wave0_admitted"] is False
    assert composition["composition_pending"] is True
    assert composition["orphaned_technique_ids"] == []
    assert composition["sealed_technique_ids"] == ["prompt"]
    assert "initial-composition" in composition["initial_composition_request"]
    # The Lead knows its reviewed budget and the sealed technique set to cover.
    assert composition["max_concurrent_specialists"] == 6
    assert composition["max_per_campaign_specialists"] == 24
    request = composition["initial_composition_request"]
    assert "exactly one specialist" in request
    assert "technique_ids" in request
    assert "evidence_refs" in request
    # The request names every narrowing field the Lead may emit so a non-
    # specialist narrowing cannot widen the specialist past its ceiling.
    assert "tool_ids" in request
    assert "skill_ids" in request
    assert "authority_ids" in request
    assert "required_capabilities" in request
    # Reuse exposes a completed evidence projection instead of inventing a
    # current-campaign reconnaissance worker.
    assert "<this campaign id>/recon" not in request
    assert "reused reconnaissance appears as a completed evidence projection" in request


async def test_execute_reused_recon_admits_autonomous_composition_before_attack(
    monkeypatch,
) -> None:
    prepared = _preparation(
        "session-1",
        "Assess the reviewed target",
        CampaignProfile.RECOMMENDED,
        composition="autonomous",
    )
    prompt = replace(prepared.assignment_context.techniques[0], execution_role="direct")
    second = AssignmentTechnique(
        "session",
        "Test session handling",
        frozenset({"session"}),
        ("case:prompt",),
        execution_role="direct",
    )
    prepared = CampaignPreparation(
        prepared.manifest,
        replace(
            prepared.assignment_context,
            techniques=(prompt, second),
        ),
    )
    result_digest = "sha256:" + "a" * 64

    class ReusedReconStore(ExecuteStore):
        def latest_recon_result(self, target_id, scenario_fingerprint):
            return {
                "id": "recon-result:reused",
                "target_id": target_id,
                "scenario_fingerprint": scenario_fingerprint,
                "result_digest": result_digest,
                "result": {
                    "applicable_ids": ["prompt", "session"],
                    "inapplicable_ids": [],
                    "result_digest": result_digest,
                },
            }

    store = ReusedReconStore(prepared)
    agents = ReconAgents()
    service = CampaignService(store, agents, _preparation)  # type: ignore[arg-type]
    lead_sources: list[tuple[AgentTask, ...]] = []

    async def compose_before_submit(
        _campaign_id,
        _manifest,
        _wave,
        source_tasks,
        *,
        source_evidence_override=None,
    ):
        assert not agents.submissions
        assert source_evidence_override == {
            "recon-result:reused": {
                "digest": result_digest,
                "reference": "recon-result:reused",
                "source": "reused_reconnaissance",
            }
        }
        lead_sources.append(source_tasks)
        proposal = DynamicSpecialistProposal(
            campaign_id=prepared.manifest.id,
            label="dynamic-injection",
            objective="Test prompt injection",
            source_task_ids=("recon-result:reused",),
            evidence_refs=("recon-result:reused",),
            evidence_expectation="an attributable target response",
            ttl_waves=1,
            technique_ids=("prompt",),
        )
        return LeadAssessment(
            "Initial specialist composition",
            False,
            followups=(
                LeadFollowUp(
                    proposal.label,
                    proposal.objective,
                    proposal.source_task_ids,
                    specialist=proposal,
                ),
            ),
        )

    monkeypatch.setattr(service, "_assess_and_journal", compose_before_submit)

    result = await service.execute("session-1", "campaign-1", prepared.manifest.digest)

    assert result["status"] == "executing"
    assert len(lead_sources) == 1
    (reused_source,) = lead_sources[0]
    assert reused_source.id == "recon-result:reused"
    assert reused_source.status == "completed"
    assert json.loads(reused_source.result or "{}") == {
        "applicable_ids": ["prompt", "session"],
        "inapplicable_ids": [],
        "result_digest": result_digest,
    }
    ((assignments, _),) = agents.submissions
    assert all(item.profile_id != "recon" for item in assignments)
    assert any(item.profile_id.startswith("dynamic-") for item in assignments)
    assert any(
        item.profile_id == "adversarial-explorer" and item.technique_ids == ("session",)
        for item in assignments
    )


async def test_autonomous_composition_is_admitted_before_initial_attack_once(
    monkeypatch,
) -> None:
    prepared = _preparation(
        "session-1",
        "Assess the reviewed target",
        CampaignProfile.RECOMMENDED,
        composition="autonomous",
    )
    second = AssignmentTechnique(
        "session",
        "Test session handling",
        frozenset({"session"}),
        ("case:prompt",),
    )
    context = replace(
        prepared.assignment_context,
        techniques=(*prepared.assignment_context.techniques, second),
    )
    store = ExecuteStore(CampaignPreparation(prepared.manifest, context))
    service = CampaignService(store, object(), _preparation)  # type: ignore[arg-type]
    recon = AgentTask(
        id="campaign-1/wave-0/recon",
        objective="Map the reviewed target",
        status="completed",
        result="Reconnaissance completed",
    )
    lead_calls = 0

    async def assess_once(*_args, **_kwargs):
        nonlocal lead_calls
        lead_calls += 1
        proposal = DynamicSpecialistProposal(
            campaign_id=prepared.manifest.id,
            label="dynamic-injection",
            objective="Test prompt injection",
            source_task_ids=(recon.id,),
            evidence_refs=("merlin-result://recon",),
            evidence_expectation="an attributable target response",
            ttl_waves=1,
            technique_ids=("prompt",),
        )
        return LeadAssessment(
            "Initial specialist composition",
            False,
            followups=(
                LeadFollowUp(
                    proposal.label,
                    proposal.objective,
                    proposal.source_task_ids,
                    specialist=proposal,
                ),
            ),
        )

    monkeypatch.setattr(service, "_assess_and_journal", assess_once)
    evidence = {
        "digest": "sha256:" + "1" * 64,
        "reference": "merlin-result://recon",
    }

    compiled = await service._compile_initial_attack_ownership(  # noqa: SLF001
        prepared.manifest.id,
        prepared.manifest,
        context,
        0,
        (recon,),
        recon,
        evidence,
    )
    replayed = await service._compile_initial_attack_ownership(  # noqa: SLF001
        prepared.manifest.id,
        prepared.manifest,
        context,
        0,
        (recon,),
        recon,
        evidence,
    )

    assert lead_calls == 1
    ownership = {
        assignment.profile_id: assignment.technique_ids for assignment in compiled.assignments
    }
    (dynamic_profile_id,) = tuple(
        profile_id for profile_id in ownership if profile_id.startswith("dynamic-")
    )
    assert ownership[dynamic_profile_id] == ("prompt",)
    assert ownership["adversarial-explorer"] == ("session",)
    assert (
        sum(
            technique_id in assignment.technique_ids
            for assignment in compiled.assignments
            for technique_id in ("prompt", "session")
        )
        == 2
    )
    assert {
        assignment.profile_id: assignment.technique_ids for assignment in replayed.assignments
    } == ownership


async def test_autonomous_lead_projection_uses_reviewed_frontier_during_recon() -> None:
    prepared = _preparation(
        "session-1",
        "Assess the reviewed target",
        CampaignProfile.RECOMMENDED,
        composition="autonomous",
    )
    techniques = tuple(
        AssignmentTechnique(
            f"technique-{index:03d}",
            f"Execute reviewed technique {index:03d}",
            frozenset({"prompt"}),
            ("case:prompt",),
        )
        for index in range(93)
    )
    prepared = CampaignPreparation(
        prepared.manifest,
        replace(prepared.assignment_context, techniques=techniques),
    )
    store = ExecuteStore(prepared)
    service = CampaignService(store, object(), _preparation)  # type: ignore[arg-type]

    projections = await service._lead_control_plane_tasks(  # noqa: SLF001
        prepared.manifest,
        (
            AgentTask(
                id="campaign-1/recon",
                objective="Map the reviewed target",
                status="completed",
                result="recon complete",
                technique_ids=(),
            ),
        ),
    )
    composition = next(
        json.loads(task.result or "{}")
        for task in projections
        if task.id.endswith("/lead-control/specialist_composition_get")
    )

    assert composition["sealed_technique_ids"] == [item.id for item in techniques]
    assert composition["composition_pending"] is True
    assert composition["orphaned_technique_ids"] == []


class SupervisionStore:
    """Journal-only supervision store; dynamic records replay through the journal."""

    def __init__(self) -> None:
        self.waves = []
        self.lead: list[tuple[int, dict]] = []
        self.terminal = None

    def finish_wave(self, campaign_id, wave, status, summary):
        self.waves.append((wave, "finished", status, summary))

    def add_wave(self, campaign_id, wave, kind, schedule_id):
        self.waves.append((wave, kind, "running", schedule_id))

    def append_lead(self, _campaign_id, wave, payload):
        self.lead.append((wave, dict(payload)))
        return payload

    def list_lead(self, _campaign_id):
        return [{"wave": wave, "payload": payload} for wave, payload in self.lead]

    def finish(self, _campaign_id, status):
        self.terminal = status


class SupervisionAgents:
    def __init__(self) -> None:
        self.index = 0
        self.submissions = []
        self.tasks = {}

    async def list_schedule_tasks(self, schedule_id):
        return self.tasks.get(schedule_id, ())

    async def submit(self, session_id, assignments, **options):
        self.index += 1
        schedule_id = options.get("schedule_id") or f"wave-{self.index}"
        values = tuple(assignments)
        self.submissions.append((values, options))
        self.tasks[schedule_id] = tuple(
            AgentTask(
                id=item.id,
                objective=item.objective,
                status="completed",
                result=f"result:{item.profile_id}",
            )
            for item in values
        )
        return SimpleNamespace(id=schedule_id, execution=None, assignments=values)

    def start_execution(self, live, *, max_workers):
        live.execution = asyncio.create_task(asyncio.sleep(0))


async def test_initial_wave_schedules_and_retires_wave0_specialists() -> None:
    prepared = _preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    manifest = prepared.manifest
    context = prepared.assignment_context
    store = SupervisionStore()
    agents = SupervisionAgents()

    async def lead(*_args):
        return LeadAssessment("coverage complete", True)

    service = CampaignService(store, agents, _preparation, lead)  # type: ignore[arg-type]
    # Packaged campaigns admit through execute(); drive the same seam here.
    await service._admit_wave0_specialist_composition(  # noqa: SLF001
        manifest.id, manifest, context
    )
    template = template_by_id("merlin-core-specialists", 1)
    assert len(_journal_kinds(store, "dynamic_specialist_admitted")) == len(template.entries)

    # Merge the admitted wave-0 composition into the attack compilation,
    # exactly as the supervision attack branch does before agenda expansion.
    attack = compile_attack_campaign(manifest, context)
    wave0 = await service._wave0_admitted_compilation(manifest.id, manifest)  # noqa: SLF001
    assert len(wave0.assignments) == len(template.entries)
    merged = CampaignCompilation(
        (*attack.assignments, *wave0.assignments),
        (*attack.worker_contexts, *wave0.worker_contexts),
    )
    structural_count = len(attack.assignments)
    live = await service._start_wave(  # noqa: SLF001
        manifest.id, manifest, 1, "initial", merged
    )

    # The initial attack wave schedules the structural lanes and the admitted
    # wave-0 specialists through the same submit path under the same ceiling.
    ((attack_assignments, attack_options),) = agents.submissions
    dynamic = [item for item in attack_assignments if item.profile_id.startswith("dynamic-")]
    assert len(dynamic) == len(template.entries)
    assert len(attack_assignments) >= structural_count + len(template.entries)
    assert attack_options["max_workers"] <= 8
    assert len(attack_options["worker_contexts"]) == len(attack_assignments)

    # A terminal campaign wave retires wave-0 specialists like follow-up ones.
    await service._supervise(manifest.id, manifest, context, live, initial_wave=1)  # noqa: SLF001

    retired = _journal_kinds(store, "dynamic_specialist_retired")
    assert len(retired) == len(template.entries)
    assert {event["record"]["retirement_reason"] for event in retired} == {"completed"}
    records = await service._durable_dynamic_specialists(manifest.id)  # noqa: SLF001
    assert {record.state for record in records} == {"retired"}
    assert store.terminal == "completed"
