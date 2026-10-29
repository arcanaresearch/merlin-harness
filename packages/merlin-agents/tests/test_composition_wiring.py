"""Wiring tests: SwarmRequest composition, prepare forwarding, wave-0 seam."""

from dataclasses import asdict

import pytest
from merlin_agents import (
    AssessmentPrepareTool,
    AssignmentBudget,
    AssignmentContext,
    AssignmentTechnique,
    CampaignManifest,
    CampaignPreparation,
    CampaignProfile,
    CampaignService,
    ContextDocument,
    DurableAgentService,
    ReviewedTargetAdapter,
)
from merlin_agents.campaign_service import _json_value
from merlin_agents.specialist_templates import template_by_id
from merlin_contracts import SwarmRequest, ToolContext


def _technique(technique_id: str) -> AssignmentTechnique:
    return AssignmentTechnique(
        id=technique_id,
        objective=f"Assess {technique_id}",
        tags=frozenset({technique_id}),
        guidance_ids=("case:prompt",),
    )


def preparation(session_id, objective, profile, *, composition="opinionated"):
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
        techniques=(_technique("prompt"), _technique("exfiltration")),
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
        self.lead: list[tuple[int, dict]] = []

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

    def list_active(self, session_id):
        return [row for row in self.rows.values() if row["session_id"] == session_id]

    def append_lead(self, _campaign_id, wave, payload):
        self.lead.append((wave, dict(payload)))
        return payload

    def list_lead(self, _campaign_id):
        return [{"wave": wave, "payload": payload} for wave, payload in self.lead]

    def list_waves(self, _campaign_id):
        return []


class Provider:
    async def stream(self, request):
        raise AssertionError("composition wiring tests never reach the provider")
        yield


class Tools:
    def describe(self):
        return ()

    async def invoke(self, name, context, arguments):
        raise AssertionError("no tools admitted")


def _factory(prepared: CampaignPreparation):
    async def factory(session_id, objective, profile, adapter=None, budget=None, **kwargs):
        composition = kwargs.get("specialist_composition")
        if composition is None or composition == prepared.manifest.specialist_composition:
            return prepared
        return preparation(session_id, objective, profile, composition=composition)

    return factory


async def test_prepare_forwards_composition_into_the_sealed_manifest() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    store = Store()
    service = CampaignService(store, object(), _factory(prepared))  # type: ignore[arg-type]

    await service.prepare(
        "session-1",
        "Assess the reviewed target",
        CampaignProfile.RECOMMENDED,
        {"interface": "http_api"},
        specialist_composition="autonomous",
    )

    stored = store.rows["campaign-1"]["manifest"]["manifest"]
    assert stored["specialist_composition"] == "autonomous"
    opinionated = preparation(
        "session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED
    ).manifest
    autonomous = preparation(
        "session-1",
        "Assess the reviewed target",
        CampaignProfile.RECOMMENDED,
        composition="autonomous",
    ).manifest
    assert opinionated.digest != autonomous.digest
    assert stored["digest"] == autonomous.digest
    assert stored["objective_contract"] is None


async def test_prepare_without_composition_keeps_the_reviewed_default() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    store = Store()
    service = CampaignService(store, object(), _factory(prepared))  # type: ignore[arg-type]

    await service.prepare(
        "session-1",
        "Assess the reviewed target",
        CampaignProfile.RECOMMENDED,
        {"interface": "http_api"},
    )

    stored = store.rows["campaign-1"]["manifest"]["manifest"]
    assert stored["specialist_composition"] == "opinionated"
    assert stored["digest"] == prepared.manifest.digest
    assert stored["objective_contract"] is None


async def test_prepare_rejects_unknown_composition() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    service = CampaignService(Store(), object(), _factory(prepared))  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="specialist_composition"):
        await service.prepare(
            "session-1",
            "Assess the reviewed target",
            CampaignProfile.RECOMMENDED,
            {"interface": "http_api"},
            specialist_composition="freeform",
        )


def _browser_adapter() -> dict:
    return {
        "interface": "browser",
        "endpoint": "https://target.test",
        "authentication": "browser_session",
        "recon_handoff_id": "handoff:browser",
        "dom_fallback_reason": "no stable stateful API was observed",
        "observed_evidence": ["snapshot call-1", "post-submit snapshot call-2"],
        "browser_steps": [{"step_id": "open", "tool": "browser_navigate", "arguments": {}}],
        "interaction_contract": {
            "input_locator": "textbox: Ask",
            "submit_action": "press Enter",
            "frame_context": None,
            "authenticated_session_constraints": "one authenticated conversation",
            "input_limit": {
                "max_characters": 500,
                "source": "dom_attribute",
                "evidence": "textarea maxlength=500 in snapshot call-1",
            },
            "probe": {
                "message": "What policy factors are relevant to refund eligibility?",
                "objective_relevance": "maps the policy surface without attempting an exploit",
                "sent_message_witness": "operator message appears in transcript",
                "response_witness": "assistant policy response appears after the sent message",
            },
            "lifecycle": {
                "current_state": "active at turn 1",
                "terminal_indicator": "turn counter reaches its documented maximum",
                "resume_action": "not offered while the conversation is active",
                "rating_action": "not offered while the conversation is active",
                "new_session_action": "not offered while the conversation is active",
                "cleanup_action": "close the target tab after campaign cleanup",
            },
            "success_indicator": "assistant policy response appears",
            "evidence_refs": ["snapshot call-1", "post-submit snapshot call-2"],
        },
    }


async def test_prepare_tool_forwards_and_validates_composition() -> None:
    captured: list[dict] = []

    class Service:
        async def resolve_reviewed_adapter(self, session_id, adapter):
            assert session_id == "session-1"
            assert adapter == {"recon_handoff_id": "handoff:browser"}
            return _browser_adapter()

        async def prepare(self, session_id, objective, profile, adapter, **kwargs):
            captured.append(kwargs)
            return {"campaign_id": "campaign-1"}

    context = ToolContext(session_id="session-1", workspace_root="/workspace")
    tool = AssessmentPrepareTool(Service())  # type: ignore[arg-type]

    result = await tool.invoke(
        context,
        {
            "objective": "Assess target",
            "campaign_profile": "recommended",
            "adapter": {"recon_handoff_id": "handoff:browser"},
            "specialist_composition": "autonomous",
        },
    )
    assert result.is_error is False
    assert captured == [{"specialist_composition": "autonomous"}]

    result = await tool.invoke(
        context,
        {
            "objective": "Assess target",
            "campaign_profile": "recommended",
            "adapter": {"recon_handoff_id": "handoff:browser"},
            "specialist_composition": "freeform",
        },
    )
    assert result.is_error is True
    assert "specialist_composition" in result.content
    assert len(captured) == 1

    schema = tool.describe().input_schema
    assert schema["properties"]["specialist_composition"]["enum"] == [
        "opinionated",
        "autonomous",
    ]


async def test_prepare_tool_anchors_objective_to_the_reviewed_target() -> None:
    captured: list[str] = []

    class Service:
        async def resolve_reviewed_adapter(self, session_id, adapter):
            assert session_id == "session-1"
            assert adapter == {"recon_handoff_id": "handoff:browser"}
            return _browser_adapter()

        async def prepare(self, session_id, objective, profile, adapter, **kwargs):
            captured.append(objective)
            return {"campaign_id": "campaign-1"}

    context = ToolContext(session_id="session-1", workspace_root="/workspace")
    tool = AssessmentPrepareTool(Service())  # type: ignore[arg-type]

    result = await tool.invoke(
        context,
        {
            "objective": "Assess the refund policy bot",
            "campaign_profile": "recommended",
            "adapter": {"recon_handoff_id": "handoff:browser"},
        },
    )
    assert result.is_error is False
    assert captured == ["Assess the refund policy bot\nTarget: https://target.test"]

    result = await tool.invoke(
        context,
        {
            "objective": "Assess https://target.test for refund policy overclaim",
            "campaign_profile": "recommended",
            "adapter": {"recon_handoff_id": "handoff:browser"},
        },
    )
    assert result.is_error is False
    assert captured[1] == "Assess https://target.test for refund policy overclaim"


def _active_row(prepared: CampaignPreparation) -> dict:
    return {
        "id": prepared.manifest.id,
        "session_id": prepared.manifest.session_id,
        "manifest": {
            "manifest": _json_value(asdict(prepared.manifest)),
            "assignment_context": _json_value(asdict(prepared.assignment_context)),
        },
        "manifest_digest": prepared.manifest.digest,
        "status": "executing",
    }


async def test_swarm_start_admits_wave0_template_specialists_end_to_end() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    store = Store()
    store.rows["campaign-1"] = _active_row(prepared)
    campaigns = CampaignService(store, object(), _factory(prepared))  # type: ignore[arg-type]
    durable = DurableAgentService(
        Provider(),  # type: ignore[arg-type]
        Tools(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        initial_composition_admitter=campaigns.initial_composition_admitter(),
    )
    request = SwarmRequest(session_id="session-1", objective="Assess the reviewed target")

    assignments, max_workers, profiles = await durable._compose_initial_assignments(  # noqa: SLF001
        request, prepared.assignment_context
    )

    template = template_by_id("merlin-core-specialists", 1)
    assert len(assignments) == len(template.entries)
    assert all(assignment.profile_id.startswith("dynamic-") for assignment in assignments)
    assert max_workers == min(8, len(template.entries))
    assert set(profiles) == {assignment.id for assignment in assignments}
    # The composition owns the routed techniques exactly once; zero-overlap
    # techniques stay with the structural explorer lane (not composed here).
    owned = sorted(
        technique_id for assignment in assignments for technique_id in assignment.technique_ids
    )
    assert owned == ["prompt"]
    owners = [assignment for assignment in assignments if assignment.technique_ids]
    assert len(owners) == 1
    assert '"label":"dynamic-injection"' in owners[0].objective
    admitted = [
        payload for _, payload in store.lead if payload.get("kind") == "dynamic_specialist_admitted"
    ]
    assert len(admitted) == len(template.entries)

    # Replaying the swarm start reuses the admitted identities (idempotent).
    replayed, _, _ = await durable._compose_initial_assignments(  # noqa: SLF001
        request, prepared.assignment_context
    )
    assert [item.id for item in replayed] == [item.id for item in assignments]
    assert sum(
        payload.get("kind") == "dynamic_specialist_admitted" for _, payload in store.lead
    ) == len(template.entries)


async def test_swarm_start_autonomous_admits_nothing_without_a_lead_decision() -> None:
    prepared = preparation(
        "session-1",
        "Assess the reviewed target",
        CampaignProfile.RECOMMENDED,
        composition="autonomous",
    )
    store = Store()
    store.rows["campaign-1"] = _active_row(prepared)
    campaigns = CampaignService(store, object(), _factory(prepared))  # type: ignore[arg-type]
    durable = DurableAgentService(
        Provider(),  # type: ignore[arg-type]
        Tools(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        initial_composition_admitter=campaigns.initial_composition_admitter(),
    )
    request = SwarmRequest(
        session_id="session-1",
        objective="Assess the reviewed target",
        specialist_composition="autonomous",
    )

    # Autonomous composition has no wave-0 source at swarm start; fail closed.
    with pytest.raises(ValueError, match="specialist composition"):
        await durable._compose_initial_assignments(  # noqa: SLF001
            request, prepared.assignment_context
        )
    assert not [
        payload for _, payload in store.lead if payload.get("kind") == "dynamic_specialist_admitted"
    ]


async def test_swarm_start_without_an_active_campaign_fails_closed() -> None:
    prepared = preparation("session-1", "Assess the reviewed target", CampaignProfile.RECOMMENDED)
    campaigns = CampaignService(Store(), object(), _factory(prepared))  # type: ignore[arg-type]
    durable = DurableAgentService(
        Provider(),  # type: ignore[arg-type]
        Tools(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        initial_composition_admitter=campaigns.initial_composition_admitter(),
    )
    request = SwarmRequest(session_id="session-1", objective="Assess the reviewed target")

    with pytest.raises(ValueError, match="specialist composition"):
        await durable._compose_initial_assignments(  # noqa: SLF001
            request, prepared.assignment_context
        )
