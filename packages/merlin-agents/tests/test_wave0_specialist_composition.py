"""Wave-0 initial specialist composition, recomposition, and projections."""

import json
from dataclasses import asdict

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
    LeadFollowUp,
    ReviewedTargetAdapter,
)
from merlin_agents.campaign_service import _json_value
from merlin_agents.specialist_templates import template_by_id
from merlin_contracts import AgentTask


async def preparation(session_id, objective, profile, *, composition="opinionated"):
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


class Store:
    def __init__(self) -> None:
        self.rows = {}

    def prepare(self, campaign_id, session_id, manifest, digest):
        row = {
            "id": campaign_id,
            "session_id": session_id,
            "manifest": manifest,
            "manifest_digest": digest,
            "status": "prepared",
        }
        self.rows[campaign_id] = row
        return row

    def get(self, campaign_id):
        return self.rows.get(campaign_id)


class JournalStore(Store):
    """Lead-journal-only store; dynamic records replay through the journal."""

    def __init__(self) -> None:
        super().__init__()
        self.lead: list[tuple[int, dict]] = []

    def append_lead(self, _campaign_id, wave, payload):
        self.lead.append((wave, dict(payload)))
        return payload

    def list_lead(self, _campaign_id):
        return [{"wave": wave, "payload": payload} for wave, payload in self.lead]

    def list_waves(self, _campaign_id):
        return []


class FinishingJournalStore(JournalStore):
    """Journal store with the terminal finish hook used by _finish_campaign."""

    def __init__(self) -> None:
        super().__init__()
        self.finished: tuple | None = None

    def finish(self, campaign_id, status, stop_reason=None):
        self.finished = (campaign_id, status, stop_reason)


def recon_source_task(**changes: object) -> AgentTask:
    values: dict[str, object] = {
        "id": "campaign-1/wave-0/recon-0",
        "objective": "map the reviewed target surface",
        "status": "running",
        "result": None,
    }
    values.update(changes)
    return AgentTask(**values)  # type: ignore[arg-type]


def completed_source_task(**changes: object) -> AgentTask:
    values: dict[str, object] = {
        "id": "campaign-1/wave-1/prompt-0",
        "objective": "probe the reviewed boundary",
        "status": "completed",
        "result": "{}",
    }
    values.update(changes)
    return AgentTask(**values)  # type: ignore[arg-type]


def completed_evidence(task_id: str):
    return {task_id: {"digest": "sha256:" + "a" * 64, "reference": "receipt:source"}}


def lead_composition_entry(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "label": "dynamic-refund-abuse",
        "objective": "Probe the reviewed refund workflow for abuse",
        "evidence_refs": ["recon:endpoint-map"],
        "evidence_expectation": "paired treatment and control receipts",
        "ttl_waves": 2,
    }
    body.update(changes)
    return body


def replacement_proposal(**changes: object) -> DynamicSpecialistProposal:
    values: dict[str, object] = {
        "campaign_id": "campaign-1",
        "label": "dynamic-consolidated-abuse",
        "objective": "Probe the reviewed abuse workflows",
        "source_task_ids": ("campaign-1/wave-1/prompt-0",),
        "evidence_refs": ("receipt:source",),
        "evidence_expectation": "paired treatment and control receipts",
    }
    values.update(changes)
    return DynamicSpecialistProposal(**values)  # type: ignore[arg-type]


def journal_kinds(store: JournalStore, kind: str) -> list[dict]:
    return [payload for _, payload in store.lead if payload.get("kind") == kind]


async def test_wave0_opinionated_admission_matches_the_reviewed_template() -> None:
    prepared = await preparation(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )
    store = JournalStore()
    service = CampaignService(store, object(), preparation)  # type: ignore[arg-type]
    recon = recon_source_task()

    compiled = await service.admit_initial_specialist_composition(
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        recon_source_task=recon,
        recon_evidence_refs=("recon:endpoint-map", "manifest:reviewed-surface"),
    )

    template = template_by_id("merlin-core-specialists", 1)
    assert len(compiled.assignments) == len(template.entries)
    admitted = journal_kinds(store, "dynamic_specialist_admitted")
    assert len(admitted) == len(template.entries)
    for event in admitted:
        assert event["creation"] == {
            "kind": "template",
            "template_id": "merlin-core-specialists",
            "template_version": 1,
        }
    objectives = {event["record"]["profile"]["instructions"] for event in admitted}
    assert objectives == {entry.objective for entry in template.entries}
    # Restart replay reuses the admitted identities without new journal events.
    recovered = CampaignService(store, object(), preparation)  # type: ignore[arg-type]
    replayed = await recovered.admit_initial_specialist_composition(
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        recon_source_task=recon,
        recon_evidence_refs=("recon:endpoint-map", "manifest:reviewed-surface"),
    )
    assert [item.id for item in replayed.assignments] == [item.id for item in compiled.assignments]
    assert len(journal_kinds(store, "dynamic_specialist_admitted")) == len(template.entries)


async def test_wave0_autonomous_admission_validates_lead_output() -> None:
    prepared = await preparation(
        "session-1",
        "Assess the reviewed target",
        CampaignProfile.RECOMMENDED,
        composition="autonomous",
    )
    store = JournalStore()
    service = CampaignService(store, object(), preparation)  # type: ignore[arg-type]
    recon = recon_source_task()
    malformed = {
        key: value for key, value in lead_composition_entry().items() if key != "objective"
    }

    compiled = await service.admit_initial_specialist_composition(
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        recon_source_task=recon,
        recon_evidence_refs=("recon:endpoint-map",),
        lead_composition=(lead_composition_entry(), malformed),
    )

    assert len(compiled.assignments) == 1
    (admitted,) = journal_kinds(store, "dynamic_specialist_admitted")
    assert admitted["creation"] == {
        "kind": "lead",
        "source_task_ids": ("campaign-1/wave-0/recon-0",),
        "evidence_refs": ("recon:endpoint-map",),
    }
    assert admitted["record"]["ttl_waves"] == 2
    rejected = journal_kinds(store, "dynamic_specialist_rejected")
    assert any("malformed initial composition" in event["reason"] for event in rejected)


async def test_wave0_relaxation_does_not_weaken_later_waves() -> None:
    prepared = await preparation(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )
    store = JournalStore()
    service = CampaignService(store, object(), preparation)  # type: ignore[arg-type]
    recon = recon_source_task()

    # Wave 0 admits against a still-running recon source with manifest evidence.
    compiled = await service.admit_initial_specialist_composition(
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        recon_source_task=recon,
        recon_evidence_refs=("recon:endpoint-map",),
    )
    assert len(compiled.assignments) == 5

    # The same uncompleted source is Lead speculation at wave 1 and fails closed.
    proposal = replacement_proposal(source_task_ids=(recon.id,), evidence_refs=("recon:map",))
    followup_wave = await service._admit_dynamic_specialists(  # noqa: SLF001
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        1,
        (
            LeadFollowUp(
                proposal.label,
                proposal.objective,
                proposal.source_task_ids,
                specialist=proposal,
            ),
        ),
        source_tasks=(recon,),
        source_evidence=None,
    )
    assert followup_wave.assignments == ()
    rejected = journal_kinds(store, "dynamic_specialist_rejected")
    assert any("not completed in-campaign evidence" in event["reason"] for event in rejected)


async def test_dynamic_specialist_grant_effects_subset_checked_against_parent_grant() -> None:
    """Proposals pinning grant effects are admitted iff they narrow the parent grant."""
    prepared = await preparation(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )
    store = JournalStore()
    service = CampaignService(store, object(), preparation)  # type: ignore[arg-type]
    recon = recon_source_task()
    narrowing = replacement_proposal(
        source_task_ids=(recon.id,),
        evidence_refs=("recon:map",),
        grant_effects=frozenset({"target_read"}),
    )
    widening = replacement_proposal(
        label="dynamic-widening-grant",
        source_task_ids=(recon.id,),
        evidence_refs=("recon:map",),
        grant_effects=frozenset({"target_read", "subprocess"}),
    )

    compiled = await service._admit_dynamic_specialists(  # noqa: SLF001
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        0,
        (
            LeadFollowUp(
                narrowing.label,
                narrowing.objective,
                narrowing.source_task_ids,
                specialist=narrowing,
            ),
            LeadFollowUp(
                widening.label,
                widening.objective,
                widening.source_task_ids,
                specialist=widening,
            ),
        ),
        source_tasks=(recon,),
        source_evidence=None,
    )

    # The narrowing proposal is admitted against the adversarial-explorer
    # ceiling grant; the widening one is refused by the subset rule.
    assert len(compiled.assignments) == 1
    admitted = journal_kinds(store, "dynamic_specialist_admitted")
    assert [event["record"]["label"] for event in admitted] == [narrowing.label]
    assert compiled.assignments[0].grant_id == "profile:adversarial-explorer"
    rejected = journal_kinds(store, "dynamic_specialist_rejected")
    assert any(
        event["label"] == "dynamic-widening-grant"
        and "grant widens the parent grant" in event["reason"]
        for event in rejected
    )


async def test_redirect_retires_and_admits_replacement_idempotently() -> None:
    prepared = await preparation(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )
    store = JournalStore()
    service = CampaignService(store, object(), preparation)  # type: ignore[arg-type]
    await service.admit_initial_specialist_composition(
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        recon_source_task=recon_source_task(),
        recon_evidence_refs=("recon:endpoint-map",),
    )
    records = await service._durable_dynamic_specialists("campaign-1")  # noqa: SLF001
    target = records[0]
    source = completed_source_task()
    replacement = replacement_proposal()

    compiled = await service.redirect_dynamic_specialist(
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        1,
        specialist_id=target.id,
        replacement=replacement,
        source_tasks=(source,),
        source_evidence=completed_evidence(source.id),
    )
    assert len(compiled.assignments) == 1
    records = await service._durable_dynamic_specialists("campaign-1")  # noqa: SLF001
    by_id = {record.id: record for record in records}
    assert by_id[target.id].state == "retired"
    assert by_id[target.id].retirement_reason == "redirected"
    assert by_id[compiled.assignments[0].profile_id].state == "active"
    (redirected,) = journal_kinds(store, "dynamic_specialist_redirected")
    assert redirected["from"] == target.id
    assert redirected["admitted"] is True

    # Replaying the same redirect retires nothing and journals nothing twice.
    replayed = await service.redirect_dynamic_specialist(
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        1,
        specialist_id=target.id,
        replacement=replacement,
        source_tasks=(source,),
        source_evidence=completed_evidence(source.id),
    )
    assert [item.id for item in replayed.assignments] == [item.id for item in compiled.assignments]
    assert len(journal_kinds(store, "dynamic_specialist_redirected")) == 1
    retired = journal_kinds(store, "dynamic_specialist_retired")
    assert sum(event["record"]["id"] == target.id for event in retired) == 1


async def test_merge_retires_redundant_and_admits_consolidated() -> None:
    prepared = await preparation(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )
    store = JournalStore()
    service = CampaignService(store, object(), preparation)  # type: ignore[arg-type]
    await service.admit_initial_specialist_composition(
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        recon_source_task=recon_source_task(),
        recon_evidence_refs=("recon:endpoint-map",),
    )
    records = await service._durable_dynamic_specialists("campaign-1")  # noqa: SLF001
    merged_ids = (records[0].id, records[1].id)
    source = completed_source_task()

    compiled = await service.merge_dynamic_specialists(
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        1,
        specialist_ids=merged_ids,
        consolidated=replacement_proposal(),
        source_tasks=(source,),
        source_evidence=completed_evidence(source.id),
    )
    assert len(compiled.assignments) == 1
    records = await service._durable_dynamic_specialists("campaign-1")  # noqa: SLF001
    by_id = {record.id: record for record in records}
    assert all(by_id[specialist_id].state == "retired" for specialist_id in merged_ids)
    assert all(by_id[specialist_id].retirement_reason == "merged" for specialist_id in merged_ids)
    assert by_id[compiled.assignments[0].profile_id].state == "active"
    (merged,) = journal_kinds(store, "dynamic_specialist_merged")
    assert tuple(sorted(merged["merged"])) == tuple(sorted(merged_ids))


async def test_status_projects_composition_creation_and_events() -> None:
    prepared = await preparation(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )
    store = JournalStore()
    service = CampaignService(store, object(), preparation)  # type: ignore[arg-type]
    await service.admit_initial_specialist_composition(
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        recon_source_task=recon_source_task(),
        recon_evidence_refs=("recon:endpoint-map",),
    )
    envelope = {
        "manifest": _json_value(asdict(prepared.manifest)),
        "assignment_context": _json_value(asdict(prepared.assignment_context)),
    }
    store.prepare("campaign-1", "session-1", envelope, "sha256:reviewed")

    projected = await service.status("campaign-1")

    assert projected["dynamic_specialist_policy"]["specialist_composition"] == "opinionated"
    specialists = projected["dynamic_specialists"]
    assert len(specialists) == 5
    for record in specialists:
        assert record["creation"] == {
            "kind": "template",
            "template_id": "merlin-core-specialists",
            "template_version": 1,
        }
    kinds = {event["kind"] for event in projected["dynamic_specialist_events"]}
    assert kinds == {"dynamic_specialist_admitted"}
    assert json.dumps(projected["dynamic_specialist_events"])


async def test_wave0_opinionated_admission_routes_role_models_like_fixed_path() -> None:
    """Wave-0 proposals request the fixed path's per-profile role route."""
    prepared = await preparation(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )
    context = prepared.assignment_context
    routed = AssignmentContext(
        target_profile_id=context.target_profile_id,
        techniques=context.techniques,
        tool_ids=context.tool_ids,
        skill_ids=context.skill_ids,
        provider_id=context.provider_id,
        authority_ids=context.authority_ids,
        budget=context.budget,
        input_schema=context.input_schema,
        output_schema=context.output_schema,
        provider_model="model-1",
        provider_role_models=(("routine", "model-routine"),),
        provider_model_pricing=(("model-1", (1.5, 6.0)), ("model-routine", (2.0, 8.0))),
    )
    store = JournalStore()
    service = CampaignService(store, object(), prepared)  # type: ignore[arg-type]

    compiled = await service.admit_initial_specialist_composition(
        "campaign-1",
        prepared.manifest,
        routed,
        recon_source_task=recon_source_task(),
        recon_evidence_refs=("recon:endpoint-map",),
    )

    assert compiled.assignments
    for assignment in compiled.assignments:
        assert assignment.provider_model == "model-routine"
        assert assignment.provider_input_cost_per_million == 2.0
        assert assignment.provider_output_cost_per_million == 8.0


async def test_recon_failure_finish_retires_active_dynamic_specialists() -> None:
    """Recon failure after wave-0 admission leaves no active specialists."""
    prepared = await preparation(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )
    store = FinishingJournalStore()
    service = CampaignService(store, object(), prepared)  # type: ignore[arg-type]
    await service.admit_initial_specialist_composition(
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        recon_source_task=recon_source_task(),
        recon_evidence_refs=("recon:endpoint-map",),
    )
    records = await service._durable_dynamic_specialists("campaign-1")  # noqa: SLF001
    assert records and all(record.state == "active" for record in records)

    # The recon-failure path terminates the campaign before any attack task
    # runs, so the terminal sweep must retire every wave-0 specialist.
    await service._finish_campaign("campaign-1", "failed", "failed")  # noqa: SLF001

    records = await service._durable_dynamic_specialists("campaign-1")  # noqa: SLF001
    assert records
    for record in records:
        assert record.state == "retired"
        assert record.retirement_reason == "campaign-terminal"
    retired = journal_kinds(store, "dynamic_specialist_retired")
    assert len(retired) == len(records)

    # Replaying the terminal path retires nothing twice.
    await service._finish_campaign("campaign-1", "failed", "failed")  # noqa: SLF001
    assert len(journal_kinds(store, "dynamic_specialist_retired")) == len(records)


class ReconAgents:
    """Minimal agents seam exposing a prior-wave schedule and its evidence."""

    def __init__(self) -> None:
        self.tasks: dict[str, tuple[AgentTask, ...]] = {}
        self.evidence: dict[tuple[str, str], dict[str, object]] = {}

    async def list_schedule_tasks(self, schedule_id: str):
        return self.tasks.get(schedule_id, ())

    async def committed_task_result_evidence(self, schedule_id: str, task: AgentTask):
        return self.evidence.get((schedule_id, task.id))


class ReconWaveStore(JournalStore):
    """Journal store whose ``list_waves`` exposes the reconnaissance schedule."""

    def __init__(self, schedule_id: str) -> None:
        super().__init__()
        self.schedule_id = schedule_id

    def list_waves(self, _campaign_id):
        return [{"schedule_id": self.schedule_id}]


async def test_followup_wave_admission_grounds_completed_recon_source() -> None:
    """Wave >= 1 admission accepts a completed recon task with committed evidence."""
    prepared = await preparation(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )
    schedule_id = "wave-0-schedule"
    store = ReconWaveStore(schedule_id)
    recon = AgentTask(
        id="campaign-1/recon",
        objective="map the reviewed target surface",
        status="completed",
        result="{}",
    )
    agents = ReconAgents()
    agents.tasks[schedule_id] = (recon,)
    agents.evidence[(schedule_id, recon.id)] = {"digest": "sha256:" + "b" * 64}
    service = CampaignService(store, agents, preparation)  # type: ignore[arg-type]

    proposal = replacement_proposal(
        source_task_ids=(recon.id,),
        evidence_refs=("receipt:recon",),
    )
    compiled = await service._admit_dynamic_specialists(  # noqa: SLF001
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        1,
        (
            LeadFollowUp(
                proposal.label,
                proposal.objective,
                proposal.source_task_ids,
                specialist=proposal,
            ),
        ),
        source_tasks=(completed_source_task(),),
        source_evidence=completed_evidence("campaign-1/wave-1/prompt-0"),
    )

    assert len(compiled.assignments) == 1
    assert journal_kinds(store, "dynamic_specialist_admitted")
    assert journal_kinds(store, "dynamic_specialist_rejected") == []


async def test_followup_wave_admission_rejects_recon_without_committed_evidence() -> None:
    """A recon source with no committed evidence still fails closed at wave >= 1."""
    prepared = await preparation(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    )
    schedule_id = "wave-0-schedule"
    store = ReconWaveStore(schedule_id)
    recon = AgentTask(
        id="campaign-1/recon",
        objective="map the reviewed target surface",
        status="completed",
        result="{}",
    )
    agents = ReconAgents()
    agents.tasks[schedule_id] = (recon,)
    service = CampaignService(store, agents, preparation)  # type: ignore[arg-type]

    proposal = replacement_proposal(
        source_task_ids=(recon.id,),
        evidence_refs=("receipt:recon",),
    )
    compiled = await service._admit_dynamic_specialists(  # noqa: SLF001
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        1,
        (
            LeadFollowUp(
                proposal.label,
                proposal.objective,
                proposal.source_task_ids,
                specialist=proposal,
            ),
        ),
        source_tasks=(completed_source_task(),),
        source_evidence=completed_evidence("campaign-1/wave-1/prompt-0"),
    )

    assert compiled.assignments == ()
    rejected = journal_kinds(store, "dynamic_specialist_rejected")
    assert any("not completed in-campaign evidence" in event["reason"] for event in rejected)
