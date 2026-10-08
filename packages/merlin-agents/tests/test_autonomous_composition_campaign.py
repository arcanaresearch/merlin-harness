"""Autonomous composition as a real policy: multi-technique Lead decisions.

A single-technique autonomous campaign is a prototype. These tests drive a
manifest carrying seven techniques across six specialty areas (including one
deliberately unclassifiable technique) through the production Lead routes:
typed follow-up-wave proposals (``_typed_hypothesis_followups`` ->
``_admit_dynamic_specialists``) and the wave-0 initial composition
(``admit_initial_specialist_composition`` with ``lead_composition=``).
"""

import json

from merlin_agents import (
    AssignmentBudget,
    AssignmentContext,
    AssignmentTechnique,
    CampaignManifest,
    CampaignPreparation,
    CampaignProfile,
    CampaignService,
    ContextDocument,
    DynamicSpecialistPolicy,
    DynamicSpecialistProposal,
    LeadFollowUp,
    ReviewedTargetAdapter,
    retire_dynamic_specialist,
)
from merlin_agents.campaign_service import _typed_hypothesis_followups
from merlin_contracts import AgentTask

# Seven techniques spanning six specialty areas; ``mystery-heuristic`` matches
# no known specialty, so only a Lead decision can assign it an owner.
TECHNIQUE_IDS = (
    "credential-stuffing",
    "session-fixation",
    "prompt-injection",
    "data-exfiltration",
    "refund-abuse",
    "tool-privilege-escalation",
    "mystery-heuristic",
)


def preparation(session_id, objective, profile, *, policy=None):
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
        specialist_composition="autonomous",
        dynamic_specialist_policy=policy,
    )
    context = AssignmentContext(
        target_profile_id="target-1",
        techniques=tuple(
            AssignmentTechnique(
                id=technique_id,
                objective=f"Assess {technique_id}",
                tags=frozenset({technique_id}),
                guidance_ids=("case:prompt",),
            )
            for technique_id in TECHNIQUE_IDS
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


class JournalStore:
    """Lead-journal-only store; dynamic records replay through the journal."""

    def __init__(self) -> None:
        self.lead: list[tuple[int, dict]] = []

    def append_lead(self, _campaign_id, wave, payload):
        self.lead.append((wave, dict(payload)))
        return payload

    def list_lead(self, _campaign_id):
        return [{"wave": wave, "payload": payload} for wave, payload in self.lead]

    def list_waves(self, _campaign_id):
        return []


def journal_kinds(store: JournalStore, kind: str) -> list[dict]:
    return [payload for _, payload in store.lead if payload.get("kind") == kind]


def recon_source_task(**changes: object) -> AgentTask:
    values: dict[str, object] = {
        "id": "campaign-1/wave-0/recon-0",
        "objective": "map the reviewed target surface",
        "status": "running",
        "result": None,
    }
    values.update(changes)
    return AgentTask(**values)  # type: ignore[arg-type]


def lead_proposal(slug: str, technique_ids, **changes: object) -> dict[str, object]:
    """One typed Lead composition entry as it arrives in a Lead task result."""
    body: dict[str, object] = {
        "profile_id": slug,
        "objective": f"Probe the reviewed {slug.removeprefix('dynamic-')} surface",
        "surface": "reviewed target surface",
        "assertion": f"{slug} exposes a reviewed weakness",
        "oracle": "paired treatment and control receipts",
        "novelty_basis": "reviewed recon/manifest evidence",
        "evidence_refs": ["recon:endpoint-map"],
        "evidence_expectation": "paired treatment and control receipts",
        "technique_ids": list(technique_ids),
    }
    body.update(changes)
    return body


def lead_task(proposals, **changes: object) -> AgentTask:
    values: dict[str, object] = {
        "id": "campaign-1/wave-1/lead-control-0",
        "objective": "compose the specialist team from reviewed evidence",
        "status": "completed",
        "result": json.dumps({"hypothesis_proposals": proposals}),
    }
    values.update(changes)
    return AgentTask(**values)  # type: ignore[arg-type]


def lead_evidence(task: AgentTask):
    return {task.id: {"digest": "sha256:" + "b" * 64, "reference": "receipt:lead"}}


async def admit_lead_wave(service, manifest, context, task, *, wave=1, evidence=True):
    """Drive the production follow-up-wave route: parse, then admit."""
    followups = _typed_hypothesis_followups((task,), campaign_id=manifest.id)
    compiled = await service._admit_dynamic_specialists(  # noqa: SLF001
        manifest.id,
        manifest,
        context,
        wave,
        followups,
        source_tasks=(task,),
        source_evidence=lead_evidence(task) if evidence else None,
    )
    return followups, compiled


async def admitted_by_label(service, campaign_id) -> dict[str, object]:
    records = await service._durable_dynamic_specialists(campaign_id)  # noqa: SLF001
    return {record.label: record for record in records}


async def test_lead_composition_covers_the_multi_technique_manifest() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    manifest = prepared.manifest
    context = prepared.assignment_context
    store = JournalStore()
    service = CampaignService(store, object(), preparation)  # type: ignore[arg-type]
    proposed = {
        "dynamic-credential-session": ("credential-stuffing", "session-fixation"),
        "dynamic-injection-parser": ("prompt-injection",),
        "dynamic-extraction-privacy": ("data-exfiltration",),
        "dynamic-workflow-logic": ("refund-abuse",),
        "dynamic-tool-privilege": ("tool-privilege-escalation",),
        "dynamic-unclassified-probe": ("mystery-heuristic",),
    }
    task = lead_task([lead_proposal(label, techniques) for label, techniques in proposed.items()])

    followups, compiled = await admit_lead_wave(service, manifest, context, task)

    # Every typed entry reaches admission as a specialist follow-up.
    assert len(followups) == len(proposed)
    assert all(followup.specialist is not None for followup in followups)
    assert len(compiled.assignments) == len(proposed)
    # The admitted team's technique ownership matches the Lead's proposal
    # exactly: full coverage of the manifest, partitioned per specialist,
    # never a blanket inherit-everything from the campaign ceiling.
    records = await admitted_by_label(service, manifest.id)
    assert set(records) == set(proposed)
    for label, techniques in proposed.items():
        assert records[label].assignment.technique_ids == techniques
        assert len(records[label].assignment.technique_ids) < len(TECHNIQUE_IDS)
    owned = sorted(
        technique_id
        for record in records.values()
        for technique_id in record.assignment.technique_ids
    )
    assert owned == sorted(TECHNIQUE_IDS)
    # Every admitted specialist narrows or equals the campaign authority ceiling.
    ceiling = service._dynamic_authority_ceiling(manifest, context)  # noqa: SLF001
    for record in records.values():
        assignment = record.assignment
        assert set(assignment.technique_ids) <= set(ceiling.technique_ids)
        assert set(assignment.tool_ids) <= set(ceiling.tool_ids)
        assert set(assignment.skill_ids) <= set(ceiling.skill_ids)
        assert set(assignment.authority_ids) <= set(ceiling.authority_ids)
        assert assignment.budget.narrows(ceiling.budget)
    assert len(journal_kinds(store, "dynamic_specialist_admitted")) == len(proposed)
    assert not journal_kinds(store, "dynamic_specialist_rejected")


async def test_http_api_dynamic_specialist_inherits_only_projectable_tools() -> None:
    budget = AssignmentBudget(64, 65_536, 600)
    tools = (
        "target_chat",
        "target_request",
        "browser_record_api_handoff",
    )
    manifest = CampaignManifest.create(
        id="campaign-1",
        session_id="session-1",
        objective="Assess the reviewed HTTP API target",
        profile=CampaignProfile.RECOMMENDED,
        target=ReviewedTargetAdapter(
            "target-1",
            "http_api",
            "https://target.test/api/message",
            "model",
        ),
        provider_id="provider-1",
        provider_model="model-1",
        provider_effort="default",
        authority_ids=("session:session-1", "target:target-1"),
        catalogue=(ContextDocument.create("case:prompt", "Test prompt injection"),),
        skill_documents=(ContextDocument.create("skill:review", "Review evidence"),),
        tool_ids=tools,
        budget=budget,
        specialist_composition="autonomous",
    )
    context = AssignmentContext(
        target_profile_id="target-1",
        techniques=(
            AssignmentTechnique(
                "prompt-injection",
                "Assess prompt injection",
                frozenset({"prompt-injection"}),
                ("case:prompt",),
            ),
        ),
        tool_ids=tools,
        skill_ids=("skill:review",),
        provider_id="provider-1",
        authority_ids=manifest.authority_ids,
        budget=budget,
        input_schema="input/v1",
        output_schema="output/v1",
    )
    store = JournalStore()
    service = CampaignService(store, object(), preparation)  # type: ignore[arg-type]

    compiled = await service.admit_initial_specialist_composition(
        manifest.id,
        manifest,
        context,
        recon_source_task=recon_source_task(status="completed", result="mapped API"),
        recon_evidence_refs=("merlin-result://recon",),
        lead_composition=(lead_proposal("dynamic-api-injection", ("prompt-injection",)),),
    )

    assert len(compiled.assignments) == 1
    assert compiled.assignments[0].tool_ids == ("target_request",)
    assert not journal_kinds(store, "dynamic_specialist_rejected")


async def test_broadening_proposals_rejected_while_valid_siblings_admit() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    manifest = prepared.manifest
    context = prepared.assignment_context
    store = JournalStore()
    service = CampaignService(store, object(), preparation)  # type: ignore[arg-type]
    task = lead_task(
        [
            lead_proposal(
                "dynamic-narrow-scope",
                ("prompt-injection",),
                tool_ids=["target_chat"],
                budget={"max_turns": 64, "max_tokens": 1024, "max_seconds": 60},
            ),
            lead_proposal(
                "dynamic-fat-budget",
                ("refund-abuse",),
                budget={"max_turns": 512, "max_tokens": 1024, "max_seconds": 60},
            ),
            lead_proposal(
                "dynamic-wide-authority",
                ("data-exfiltration",),
                authority_ids=["session:session-1", "target:target-1", "tenant:other"],
            ),
        ]
    )

    followups, compiled = await admit_lead_wave(service, manifest, context, task)

    # One bad parallel proposal never vetoes its valid siblings.
    assert len(followups) == 3
    assert len(compiled.assignments) == 1
    records = await admitted_by_label(service, manifest.id)
    assert set(records) == {"dynamic-narrow-scope"}
    rejected = journal_kinds(store, "dynamic_specialist_rejected")
    reasons = {event["label"]: event["reason"] for event in rejected}
    assert "broadens budget" in reasons["dynamic-fat-budget"]
    assert "broadens authority_ids" in reasons["dynamic-wide-authority"]
    # The surviving admission is the narrowed shape the Lead asked for.
    ceiling = service._dynamic_authority_ceiling(manifest, context)  # noqa: SLF001
    narrowed = records["dynamic-narrow-scope"].assignment
    assert narrowed.budget.max_turns == 64
    assert narrowed.budget.narrows(ceiling.budget)
    assert narrowed.tool_ids == ("target_chat",)
    assert narrowed.authority_ids == ceiling.authority_ids


async def test_reviewed_policy_caps_the_lead_composition() -> None:
    prepared = preparation(
        "session-1",
        "Assess the reviewed target",
        CampaignProfile.RECOMMENDED,
        policy=DynamicSpecialistPolicy(max_concurrent=2, max_per_campaign=4),
    )
    manifest = prepared.manifest
    context = prepared.assignment_context
    store = JournalStore()
    service = CampaignService(store, object(), preparation)  # type: ignore[arg-type]
    task = lead_task(
        [
            lead_proposal("dynamic-credential-session", ("credential-stuffing",)),
            lead_proposal("dynamic-injection-parser", ("prompt-injection",)),
            lead_proposal("dynamic-workflow-logic", ("refund-abuse",)),
        ]
    )

    _, compiled = await admit_lead_wave(service, manifest, context, task)

    # The reviewed concurrent cap admits the first two and refuses the third.
    assert len(compiled.assignments) == 2
    records = await admitted_by_label(service, manifest.id)
    assert set(records) == {"dynamic-credential-session", "dynamic-injection-parser"}
    rejected = journal_kinds(store, "dynamic_specialist_rejected")
    (capped,) = [event for event in rejected if event["label"] == "dynamic-workflow-logic"]
    assert "concurrent dynamic specialist limit" in capped["reason"]


async def test_wave0_malformed_lead_entries_fail_closed_individually() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    manifest = prepared.manifest
    context = prepared.assignment_context
    store = JournalStore()
    service = CampaignService(store, object(), preparation)  # type: ignore[arg-type]
    blank_label = lead_proposal("dynamic-ignored", ("prompt-injection",), label="  ")
    foreign_slug = lead_proposal(
        "dynamic-ignored", ("prompt-injection",), profile_id="plain-worker"
    )
    missing_evidence = {
        key: value
        for key, value in lead_proposal("dynamic-ignored", ("prompt-injection",)).items()
        if key != "evidence_refs"
    }
    rogue_model = lead_proposal("dynamic-rogue-model", ("prompt-injection",), model="model-rogue")
    valid = lead_proposal("dynamic-lone-valid", ("refund-abuse",))

    compiled = await service.admit_initial_specialist_composition(
        manifest.id,
        manifest,
        context,
        recon_source_task=recon_source_task(),
        recon_evidence_refs=("recon:endpoint-map",),
        lead_composition=(blank_label, foreign_slug, missing_evidence, rogue_model, valid),
    )

    # Each malformed entry fails closed on its own; the valid sibling admits.
    assert len(compiled.assignments) == 1
    records = await admitted_by_label(service, manifest.id)
    assert set(records) == {"dynamic-lone-valid"}
    rejected = journal_kinds(store, "dynamic_specialist_rejected")
    assert any(
        event["label"] == "initial-composition"
        and "3 malformed initial composition proposal(s)" in event["reason"]
        for event in rejected
    )
    (rogue,) = [event for event in rejected if event["label"] == "dynamic-rogue-model"]
    assert "not campaign-admitted" in rogue["reason"]


async def test_wave0_evidence_grounding_relaxes_only_at_wave_zero() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    manifest = prepared.manifest
    context = prepared.assignment_context
    store = JournalStore()
    service = CampaignService(store, object(), preparation)  # type: ignore[arg-type]
    recon = recon_source_task()
    entry = lead_proposal("dynamic-recon-grounded", ("prompt-injection",))

    # Wave 0 admits proposals grounded in the reviewed recon/manifest evidence
    # while the recon source task is still running.
    compiled = await service.admit_initial_specialist_composition(
        manifest.id,
        manifest,
        context,
        recon_source_task=recon,
        recon_evidence_refs=("recon:endpoint-map",),
        lead_composition=(entry,),
    )
    assert len(compiled.assignments) == 1
    (admitted,) = journal_kinds(store, "dynamic_specialist_admitted")
    assert admitted["creation"] == {
        "kind": "lead",
        "source_task_ids": (recon.id,),
        "evidence_refs": ("recon:endpoint-map",),
    }

    # The same uncompleted source is Lead speculation at wave 1: fail closed.
    records = await admitted_by_label(service, manifest.id)
    proposal = DynamicSpecialistProposal(
        campaign_id=manifest.id,
        label="dynamic-late-speculation",
        objective="Probe the reviewed injection surface",
        source_task_ids=(recon.id,),
        evidence_refs=("recon:endpoint-map",),
        evidence_expectation="paired treatment and control receipts",
        technique_ids=("prompt-injection",),
    )
    followup_wave = await service._admit_dynamic_specialists(  # noqa: SLF001
        manifest.id,
        manifest,
        context,
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
    records_after = await admitted_by_label(service, manifest.id)
    assert set(records_after) == set(records)


async def test_restart_replay_reuses_admitted_identities() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    manifest = prepared.manifest
    context = prepared.assignment_context
    store = JournalStore()
    service = CampaignService(store, object(), preparation)  # type: ignore[arg-type]
    task = lead_task(
        [
            lead_proposal("dynamic-credential-session", ("credential-stuffing",)),
            lead_proposal("dynamic-injection-parser", ("prompt-injection",)),
        ]
    )

    _, compiled = await admit_lead_wave(service, manifest, context, task)
    assert len(compiled.assignments) == 2

    # Simulated restart: a fresh CampaignService over the same journal replays
    # the same Lead decision without double-admitting.
    recovered = CampaignService(store, object(), preparation)  # type: ignore[arg-type]
    _, replayed = await admit_lead_wave(recovered, manifest, context, task)

    assert [item.id for item in replayed.assignments] == [item.id for item in compiled.assignments]
    assert len(journal_kinds(store, "dynamic_specialist_admitted")) == 2
    assert not journal_kinds(store, "dynamic_specialist_rejected")


async def test_retired_techniques_are_not_readmitted_under_new_label() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    manifest = prepared.manifest
    context = prepared.assignment_context
    store = JournalStore()
    service = CampaignService(store, object(), preparation)  # type: ignore[arg-type]

    # Wave 1 admits one specialist owning a pinned technique.
    first = lead_task([lead_proposal("dynamic-credential-session", ("credential-stuffing",))])
    _, compiled = await admit_lead_wave(service, manifest, context, first)
    assert len(compiled.assignments) == 1

    # Retire it so the pinned technique is recorded as attempted.
    (admitted,) = await service._durable_dynamic_specialists(manifest.id)  # noqa: SLF001
    retired = retire_dynamic_specialist(admitted, wave=1, reason="ttl-expired")
    await service._persist_dynamic_retirement(manifest.id, 1, retired)  # noqa: SLF001

    # Wave 2 re-proposes the SAME pinned technique under a new label: reject.
    second = lead_task([lead_proposal("dynamic-session-replay", ("credential-stuffing",))])
    _, replayed = await admit_lead_wave(service, manifest, context, second, wave=2)
    assert replayed.assignments == ()
    rejected = journal_kinds(store, "dynamic_specialist_rejected")
    assert any("technique set already attempted" in event["reason"] for event in rejected)

    # A genuinely new pinned technique is still admitted.
    third = lead_task([lead_proposal("dynamic-session-fixation", ("session-fixation",))])
    _, novel = await admit_lead_wave(service, manifest, context, third, wave=2)
    assert len(novel.assignments) == 1


async def test_wave1_dynamic_followups_admit_all_specialists() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    manifest = prepared.manifest
    context = prepared.assignment_context
    store = JournalStore()
    service = CampaignService(store, object(), preparation)  # type: ignore[arg-type]

    # A completed in-campaign source task grounds the Lead's wave-1 composition.
    source = recon_source_task(status="completed")
    evidence = {source.id: {"digest": "sha256:" + "b" * 64, "reference": "receipt:source"}}

    labels = (
        "dynamic-direct-controls",
        "dynamic-direct-overrides",
        "dynamic-multiturn-controls",
        "dynamic-conversation-policy-bypass",
        "dynamic-resource-exhaustion",
    )
    techniques = (
        ("credential-stuffing",),
        ("session-fixation",),
        ("prompt-injection",),
        ("data-exfiltration",),
        ("refund-abuse",),
    )
    followups = []
    for label, pinned in zip(labels, techniques, strict=True):
        proposal = DynamicSpecialistProposal(
            campaign_id=manifest.id,
            label=label,
            objective=f"Probe the reviewed {label.removeprefix('dynamic-')} surface",
            source_task_ids=(source.id,),
            evidence_refs=("receipt:source",),
            evidence_expectation="paired treatment and control receipts",
            technique_ids=pinned,
        )
        followups.append(
            LeadFollowUp(label, proposal.objective, proposal.source_task_ids, specialist=proposal)
        )

    compiled = await service._admit_dynamic_specialists(  # noqa: SLF001
        manifest.id,
        manifest,
        context,
        1,
        tuple(followups),
        source_tasks=(source,),
        source_evidence=evidence,
    )

    # A wave decision carrying dynamic-* followups admits every valid specialist.
    assert len(compiled.assignments) == 5
    records = await admitted_by_label(service, manifest.id)
    assert set(records) == set(labels)
    for label, pinned in zip(labels, techniques, strict=True):
        assert records[label].assignment.technique_ids == pinned
    assert len(journal_kinds(store, "dynamic_specialist_admitted")) == 5
    assert not journal_kinds(store, "dynamic_specialist_rejected")
