"""Autonomous composition coverage guard and daemon-crash recovery.

The coverage guard checks what per-proposal validation cannot: whether the
admitted wave-0 autonomous team covers the sealed manifest technique set
exactly once. Gaps journal a ``dynamic_specialist_coverage_warning`` and
surface in the ``specialist_composition_get`` control-plane projection so the
Lead can emit correcting follow-ups; valid admissions are never rejected
retroactively. The crash tests prove admission, retirement, and scheduling
survive a daemon crash boundary over a durable store.
"""

import json

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
    ReviewedTargetAdapter,
)
from merlin_contracts import AgentTask

TECHNIQUE_IDS = ("prompt-injection", "refund-abuse", "mystery-heuristic")


def preparation(session_id, objective, profile, *, composition="autonomous"):
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


class DurableLedgerStore(JournalStore):
    """Dedicated dynamic-specialist ledger plus Lead journal over shared state.

    State lives in the store, not the service, so abandoning a CampaignService
    mid-wave and rebuilding over the same store simulates a daemon crash.
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: dict[str, dict] = {}

    def record_dynamic_specialist(self, record):
        self.records[record["id"]] = dict(record)
        return record

    def retire_dynamic_specialist(self, record):
        self.records[record["id"]] = dict(record)
        return record

    def list_dynamic_specialists(self, campaign_id):
        return [
            {"record": record}
            for record in self.records.values()
            if record.get("campaign_id") == campaign_id
        ]


class FaultAfterFirstAdmissionStore(DurableLedgerStore):
    """Crashes the daemon on the second proposal's ledger write, mid-wave."""

    def __init__(self) -> None:
        super().__init__()
        self.writes = 0
        self.armed = True

    def record_dynamic_specialist(self, record):
        self.writes += 1
        if self.armed and self.writes == 2:
            raise RuntimeError("daemon crashed mid-admission")
        return super().record_dynamic_specialist(record)


def journal_kinds(store: JournalStore, kind: str) -> list[dict]:
    return [payload for _, payload in store.lead if payload.get("kind") == kind]


def recon_source_task() -> AgentTask:
    return AgentTask(
        id="campaign-1/wave-0/recon-0",
        objective="map the reviewed target surface",
        status="running",
        result=None,
    )


def lead_entry(label: str, technique_ids, **changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "label": label,
        "objective": f"Probe the reviewed {label.removeprefix('dynamic-')} surface",
        "evidence_refs": ["recon:endpoint-map"],
        "evidence_expectation": "paired treatment and control receipts",
        "technique_ids": list(technique_ids),
        "ttl_waves": 1,
    }
    body.update(changes)
    return body


async def admit_wave0(service, prepared, lead_composition):
    return await service.admit_initial_specialist_composition(
        "campaign-1",
        prepared.manifest,
        prepared.assignment_context,
        recon_source_task=recon_source_task(),
        recon_evidence_refs=("recon:endpoint-map",),
        lead_composition=lead_composition,
    )


async def composition_projection(service, prepared) -> dict:
    sealed_task = AgentTask(
        id="campaign-1/wave-0/sealed",
        objective="sealed technique rows",
        status="running",
        result=None,
        technique_ids=TECHNIQUE_IDS,
    )
    tasks = await service._lead_control_plane_tasks(  # noqa: SLF001
        prepared.manifest, (sealed_task,)
    )
    (projected,) = [
        task for task in tasks if task.id.endswith("lead-control/specialist_composition_get")
    ]
    return json.loads(projected.result)


async def test_complete_coverage_journals_no_warning_and_projects_clean() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    store = JournalStore()
    service = CampaignService(store, object(), prepared)  # type: ignore[arg-type]

    compiled = await admit_wave0(
        service,
        prepared,
        (
            lead_entry("dynamic-injection-refund", ("prompt-injection", "refund-abuse")),
            lead_entry("dynamic-unclassified-probe", ("mystery-heuristic",)),
        ),
    )

    assert len(compiled.assignments) == 2
    assert not journal_kinds(store, "dynamic_specialist_coverage_warning")
    projection = await composition_projection(service, prepared)
    assert projection["wave0_admitted"] is True
    assert projection["orphaned_technique_ids"] == []
    assert projection["double_owned_technique_ids"] == []


async def test_partial_coverage_warns_orphans_and_projects_the_gap() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    store = JournalStore()
    service = CampaignService(store, object(), prepared)

    compiled = await admit_wave0(
        service,
        prepared,
        (lead_entry("dynamic-injection-parser", ("prompt-injection",)),),
    )

    # The valid admission stands; only a warning is journaled.
    assert len(compiled.assignments) == 1
    (warning,) = journal_kinds(store, "dynamic_specialist_coverage_warning")
    assert warning["orphaned_technique_ids"] == ("mystery-heuristic", "refund-abuse")
    assert warning["double_owned_technique_ids"] == ()
    projection = await composition_projection(service, prepared)
    assert projection["orphaned_technique_ids"] == ["mystery-heuristic", "refund-abuse"]
    assert projection["double_owned_technique_ids"] == []

    # Restart replay recomputes the same gap but journals it once.
    recovered = CampaignService(store, object(), prepared)  # type: ignore[arg-type]
    await admit_wave0(
        recovered,
        prepared,
        (lead_entry("dynamic-injection-parser", ("prompt-injection",)),),
    )
    assert len(journal_kinds(store, "dynamic_specialist_coverage_warning")) == 1


async def test_double_owned_techniques_are_flagged() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    store = JournalStore()
    service = CampaignService(store, object(), prepared)  # type: ignore[arg-type]

    compiled = await admit_wave0(
        service,
        prepared,
        (
            lead_entry("dynamic-injection-refund", ("prompt-injection", "refund-abuse")),
            lead_entry("dynamic-injection-mystery", ("prompt-injection", "mystery-heuristic")),
        ),
    )

    assert len(compiled.assignments) == 2
    (warning,) = journal_kinds(store, "dynamic_specialist_coverage_warning")
    assert warning["orphaned_technique_ids"] == ()
    assert warning["double_owned_technique_ids"] == ("prompt-injection",)
    projection = await composition_projection(service, prepared)
    assert projection["orphaned_technique_ids"] == []
    assert projection["double_owned_technique_ids"] == ["prompt-injection"]


async def test_opinionated_composition_never_journals_coverage_warnings() -> None:
    prepared = preparation(
        "session-1",
        "Assess the reviewed target",
        CampaignProfile.RECOMMENDED,
        composition="opinionated",
    )
    store = JournalStore()
    service = CampaignService(store, object(), prepared)  # type: ignore[arg-type]

    compiled = await admit_wave0(service, prepared, None)

    assert compiled.assignments
    assert not journal_kinds(store, "dynamic_specialist_coverage_warning")


async def test_admission_retirement_and_scheduling_survive_daemon_crash() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    store = DurableLedgerStore()
    service = CampaignService(store, object(), prepared)  # type: ignore[arg-type]
    composition = (
        lead_entry("dynamic-injection-refund", ("prompt-injection", "refund-abuse")),
        lead_entry("dynamic-unclassified-probe", ("mystery-heuristic",)),
    )

    compiled = await admit_wave0(service, prepared, composition)
    assert len(compiled.assignments) == 2

    # Daemon crash: the service is abandoned mid-wave with no graceful
    # shutdown; only the durable store survives.
    del service
    recovered = CampaignService(store, object(), prepared)  # type: ignore[arg-type]

    # Admitted records replay intact; re-driving the same decision reuses the
    # admitted identities without double-admission.
    replayed = await admit_wave0(recovered, prepared, composition)
    assert [item.id for item in replayed.assignments] == [item.id for item in compiled.assignments]
    assert len(journal_kinds(store, "dynamic_specialist_coverage_warning")) == 0
    admitted = journal_kinds(store, "dynamic_specialist_admitted")
    assert len(admitted) == 2
    records = await recovered._durable_dynamic_specialists("campaign-1")  # noqa: SLF001
    assert len(records) == 2
    assert all(record.state == "active" for record in records)

    # Scheduling resumes: the recovered service restores the active wave-0
    # composition for the initial attack wave.
    restoration = await recovered._wave0_admitted_compilation(  # noqa: SLF001
        "campaign-1", prepared.manifest
    )
    assert [item.id for item in restoration.assignments] == [
        item.id for item in compiled.assignments
    ]

    # Crossing a wave boundary after recovery is not retirement evidence.
    # Completion policy must supply durable frontier/progress/budget evidence
    # rather than silently expiring productive specialists by age.
    await recovered._retire_due_dynamic_specialists("campaign-1", 1)  # noqa: SLF001
    records = await recovered._durable_dynamic_specialists("campaign-1")  # noqa: SLF001
    assert all(record.state == "active" for record in records)
    assert not journal_kinds(store, "dynamic_specialist_retired")
    await recovered._retire_due_dynamic_specialists("campaign-1", 2)  # noqa: SLF001
    assert not journal_kinds(store, "dynamic_specialist_retired")


async def test_mid_admission_crash_is_atomic_and_recovers() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    store = FaultAfterFirstAdmissionStore()
    service = CampaignService(store, object(), prepared)  # type: ignore[arg-type]
    composition = (
        lead_entry("dynamic-injection-parser", ("prompt-injection",)),
        lead_entry("dynamic-workflow-logic", ("refund-abuse",)),
        lead_entry("dynamic-unclassified-probe", ("mystery-heuristic",)),
    )

    # The daemon crashes on the second proposal's ledger write, mid-wave.
    with pytest.raises(RuntimeError, match="daemon crashed"):
        await admit_wave0(service, prepared, composition)

    # Atomicity: the first proposal is fully journaled (ledger record plus
    # admitted event); the crashed and never-attempted proposals are absent
    # from both the ledger and the journal — nothing is half-admitted.
    admitted = journal_kinds(store, "dynamic_specialist_admitted")
    assert len(admitted) == 1
    assert set(store.records) == {admitted[0]["record"]["id"]}

    # Recovery over the same store replays the survivor and admits the rest
    # exactly once; no identity is compiled twice or double-journaled.
    store.armed = False
    del service
    recovered = CampaignService(store, object(), prepared)  # type: ignore[arg-type]
    compiled = await admit_wave0(recovered, prepared, composition)

    assert len(compiled.assignments) == 3
    admitted = journal_kinds(store, "dynamic_specialist_admitted")
    assert len(admitted) == 3
    ids = [event["record"]["id"] for event in admitted]
    assert len(set(ids)) == 3
    assert set(ids) == set(store.records)
    records = await recovered._durable_dynamic_specialists("campaign-1")  # noqa: SLF001
    assert len(records) == 3
    assert all(record.state == "active" for record in records)
    # Full coverage after recovery: no warning, clean projection.
    assert not journal_kinds(store, "dynamic_specialist_coverage_warning")
    projection = await composition_projection(recovered, prepared)
    assert projection["orphaned_technique_ids"] == []
    assert projection["double_owned_technique_ids"] == []
