from pathlib import Path

import pytest
from merlin_agents import (
    AdmittedDynamicSpecialist,
    AssignmentBudget,
    AssignmentContext,
    AssignmentTechnique,
    DynamicSpecialistProposal,
    WorkerAssignment,
    compile_dynamic_specialist,
    retire_dynamic_specialist,
)
from merlin_agents.specialist_templates import compile_template_proposals, template_by_id
from merlin_storage_postgres import (
    CampaignStore,
    Database,
    DatabaseConfig,
    MigrationRunner,
    SessionRepository,
)


def _context() -> AssignmentContext:
    return AssignmentContext(
        target_profile_id="target-1",
        techniques=(
            AssignmentTechnique(
                "prompt", "Test prompt injection", frozenset({"prompt"}), ("pack/ai-chat",)
            ),
        ),
        tool_ids=("target_chat",),
        skill_ids=(),
        provider_id="provider-1",
        authority_ids=("target:target-1",),
        budget=AssignmentBudget(8, 8_000, 120),
        input_schema="input/v1",
        output_schema="output/v1",
        provider_model="model-1",
    )


def _record(campaign_id: str, label: str) -> AdmittedDynamicSpecialist:
    context = _context()
    ceiling = WorkerAssignment(
        id=f"{campaign_id}/ceiling",
        objective="campaign authority ceiling for dynamic specialist admission",
        profile_id="adversarial-explorer",
        target_profile_id=context.target_profile_id,
        technique_ids=("prompt",),
        guidance_ids=("pack/ai-chat",),
        tool_ids=context.tool_ids,
        skill_ids=context.skill_ids,
        provider_id=context.provider_id,
        authority_ids=context.authority_ids,
        budget=context.budget,
        input_schema=context.input_schema,
        output_schema=context.output_schema,
        provider_model="model-1",
    )
    return compile_dynamic_specialist(
        DynamicSpecialistProposal(
            campaign_id=campaign_id,
            label=label,
            objective="Probe the reviewed refund workflow for abuse",
            source_task_ids=(f"{campaign_id}/wave-0/prompt",),
            evidence_refs=("receipt:source",),
            evidence_expectation="paired treatment and control receipts",
        ),
        parent=ceiling,
        context=context,
        graph_id=campaign_id,
        wave=1,
        active_count=0,
        campaign_count=0,
    )


def test_pg0_dynamic_specialist_ledger_replays_and_retires_across_restart(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-dynamic-specialists"
    campaign_id = "campaign/dynamic-specialists"
    record = _record(campaign_id, "dynamic-refund-abuse")
    retired = retire_dynamic_specialist(record, wave=2, reason="ttl-expired")

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("dynamic specialists")
            store = CampaignStore(connection)
            store.prepare(campaign_id, str(session.id), {"budget": {}}, "sha256:" + "0" * 64)

            admitted = store.record_dynamic_specialist(record.projection())
            assert admitted["state"] == "active"
            # Replay after a simulated crash returns the original immutable row.
            replayed = store.record_dynamic_specialist(record.projection())
            assert replayed["admitted_digest"] == admitted["admitted_digest"]
            conflict = record.projection()
            conflict["admitted_digest"] = "sha256:" + "1" * 64
            with pytest.raises(ValueError, match="identity conflicts"):
                store.record_dynamic_specialist(conflict)

    # A replacement daemon reattaches to the same data directory and observes
    # the admitted record without recreating it.
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            store = CampaignStore(connection)
            rows = store.list_dynamic_specialists(campaign_id)
            assert len(rows) == 1
            restored = AdmittedDynamicSpecialist.from_projection(rows[0]["record"])
            assert restored == record

            retired_row = store.retire_dynamic_specialist(retired.projection())
            assert retired_row["state"] == "retired"
            # Retiring again with the same terminal effect is an idempotent no-op.
            assert store.retire_dynamic_specialist(retired.projection())["state"] == "retired"
            with pytest.raises(ValueError, match="retirement conflicts"):
                store.retire_dynamic_specialist(
                    retire_dynamic_specialist(record, wave=3, reason="failed").projection()
                )
            rows = store.list_dynamic_specialists(campaign_id)
            assert len(rows) == 1
            restored = AdmittedDynamicSpecialist.from_projection(rows[0]["record"])
            assert restored.state == "retired"
            assert restored.retirement_reason == "ttl-expired"


def test_pg0_dynamic_specialist_ledger_persists_template_compiled_specialist(
    tmp_path: Path,
) -> None:
    """Opinionated wave-0 specialists carry no wave-age TTL and must persist.

    The shipped opinionated team template stopped pinning ttl_waves when
    adaptive lifetime evidence replaced wave-age retirement; the durable
    ledger must admit those records with a NULL ttl_waves instead of raising
    TypeError in int().
    """
    data_dir = tmp_path / "postgres-template-specialists"
    campaign_id = "campaign/template-specialists"
    context = _context()
    ceiling = WorkerAssignment(
        id=f"{campaign_id}/ceiling",
        objective="campaign authority ceiling for dynamic specialist admission",
        profile_id="adversarial-explorer",
        target_profile_id=context.target_profile_id,
        technique_ids=("prompt",),
        guidance_ids=("pack/ai-chat",),
        tool_ids=context.tool_ids,
        skill_ids=context.skill_ids,
        provider_id=context.provider_id,
        authority_ids=context.authority_ids,
        budget=context.budget,
        input_schema=context.input_schema,
        output_schema=context.output_schema,
        provider_model="model-1",
    )
    template = template_by_id("merlin-core-specialists", 1)
    proposals = compile_template_proposals(
        template,
        campaign_id=campaign_id,
        source_task_ids=(f"{campaign_id}/wave-0/recon-0",),
        evidence_refs=(f"manifest:{campaign_id}",),
        evidence_expectation="reviewed recon/manifest evidence bound to the template contract",
    )
    assert proposals
    assert all(proposal.ttl_waves is None for proposal in proposals)
    records = [
        compile_dynamic_specialist(
            proposal,
            parent=ceiling,
            context=context,
            graph_id=campaign_id,
            wave=0,
            active_count=index,
            campaign_count=index,
        )
        for index, proposal in enumerate(proposals)
    ]
    assert all(record.ttl_waves is None for record in records)

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("template specialists")
            store = CampaignStore(connection)
            store.prepare(campaign_id, str(session.id), {"budget": {}}, "sha256:" + "0" * 64)

            for record in records:
                admitted = store.record_dynamic_specialist(record.projection())
                assert admitted["state"] == "active"
                assert admitted["ttl_waves"] is None

            rows = store.list_dynamic_specialists(campaign_id)
            assert len(rows) == len(records)
            restored = {AdmittedDynamicSpecialist.from_projection(row["record"]) for row in rows}
            assert restored == set(records)
