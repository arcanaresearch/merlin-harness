"""Offline probes of exact technique requests; no provider or target is contacted.

These characterize the current gap, rather than claiming that scope is enforced.
The preparation factory supplies three applicable techniques, as recon would.
"""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

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
    compile_attack_campaign,
)
from merlin_agents.campaign_service import AssessmentPrepareTool, _assignment_context
from merlin_contracts import ChatMessage, ToolContext, ToolResult, ToolSpec
from merlin_tui.operator_tools import CampaignConfigureTool, ConfiguredCampaignTool


class ProbeStore:
    def __init__(self):
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


async def three_technique_preparation(session_id, objective, profile):
    budget = AssignmentBudget(256, 1_048_576, 14_400)
    documents = tuple(
        ContextDocument.create(f"case:{name}", f"Fixture technique {name}")
        for name in ("alpha", "beta", "gamma")
    )
    manifest = CampaignManifest.create(
        id="campaign-scope-probe",
        session_id=session_id,
        objective=objective,
        profile=profile,
        target=ReviewedTargetAdapter("fixture", "api", "https://target.invalid", "fixture"),
        provider_id="fixture",
        provider_model="fixture",
        provider_effort="default",
        authority_ids=(f"session:{session_id}", "target:fixture"),
        catalogue=documents,
        skill_documents=(),
        tool_ids=("target_chat",),
        budget=budget,
    )
    assignment_context = AssignmentContext(
        target_profile_id="fixture",
        techniques=tuple(
            AssignmentTechnique(name, f"Test {name}", frozenset(), (f"case:{name}",))
            for name in ("alpha", "beta", "gamma")
        ),
        tool_ids=manifest.tool_ids,
        skill_ids=(),
        provider_id="fixture",
        authority_ids=manifest.authority_ids,
        budget=budget,
        input_schema="input/v1",
        output_schema="output/v1",
    )
    return CampaignPreparation(manifest, assignment_context)


class ServicePrepareDelegate:
    effect = "workspace_write"

    def __init__(self, service):
        self.service = service

    def describe(self):
        return ToolSpec(name="assessment_prepare", description="probe", input_schema={})

    async def invoke(self, context, arguments):
        result = await self.service.prepare(
            context.session_id,
            arguments["objective"],
            CampaignProfile(arguments["campaign_profile"]),
        )
        return ToolResult(content=json.dumps(result), metadata=result)


@pytest.mark.parametrize("profile", list(CampaignProfile))
@pytest.mark.parametrize("requested", [("alpha",), ("alpha", "beta")])
async def test_exact_request_in_objective_does_not_narrow_review_or_restored_context(
    profile,
    requested,
):
    objective = f"Assess https://target.invalid using ONLY {', '.join(requested)} techniques."
    context = ToolContext(session_id="scope-probe", workspace_root="/tmp", call_id="configure")
    history = []
    loader = AsyncMock(side_effect=lambda _: tuple(history))
    configured = await CampaignConfigureTool(loader).invoke(
        context,
        {"new_campaign": True, "objective": objective, "campaign_profile": str(profile)},
    )
    assert not configured.is_error
    history.append(ChatMessage(role="tool", name="campaign_configure", content=configured.content))
    store = ProbeStore()
    # No scheduler, model, or target client: preparation cannot execute any request.
    service = CampaignService(store, None, three_technique_preparation)
    wrapper = ConfiguredCampaignTool(ServicePrepareDelegate(service), loader)
    review = await wrapper.invoke(context, {"objective": "wrong", "campaign_profile": "quickscan"})
    assert not review.is_error
    envelope = store.rows["campaign-scope-probe"]["manifest"]
    assert envelope["manifest"]["objective"] == objective
    restored = _assignment_context(envelope["assignment_context"])
    admitted = {technique.id for technique in restored.techniques}
    assert admitted == {"alpha", "beta", "gamma"}
    assert admitted - set(requested), "This probe must expose out-of-request techniques."
    # A new tool wrapper (resume) still prepares the whole supplied catalogue.
    resumed = ConfiguredCampaignTool(ServicePrepareDelegate(service), loader)
    assert not (await resumed.invoke(context, {})).is_error
    restored_again = _assignment_context(
        store.rows["campaign-scope-probe"]["manifest"]["assignment_context"]
    )
    assert {item.id for item in restored_again.techniques} == admitted


async def test_no_typed_selection_in_operator_or_preparation_schema():
    configure = CampaignConfigureTool(AsyncMock(return_value=()))
    context = ToolContext(session_id="scope-probe", workspace_root="/tmp", call_id="configure")
    result = await configure.invoke(context, {"technique_ids": ["alpha", "beta"]})
    assert result.is_error
    assert "unknown campaign setting" in result.content
    assert "technique_ids" not in AssessmentPrepareTool(None).describe().input_schema["properties"]


@pytest.mark.parametrize("requested", [("alpha",), ("alpha", "beta")])
async def test_internal_compiler_accepts_exact_ids_but_plain_objective_does_not_select_them(
    requested,
):
    prepared = await three_technique_preparation(
        "scope-probe",
        "ONLY " + ", ".join(requested),
        CampaignProfile.EXPLORATION,
    )
    # Empty tags route these fixture techniques into the structural explorer lane.
    full = compile_attack_campaign(prepared.manifest, prepared.assignment_context)
    selected = compile_attack_campaign(
        prepared.manifest,
        prepared.assignment_context,
        applicable_technique_ids=requested,
    )
    full_ids = {tid for task in full.assignments for tid in task.technique_ids}
    selected_ids = {tid for task in selected.assignments for tid in task.technique_ids}
    assert full_ids == {"alpha", "beta", "gamma"}
    assert selected_ids == set(requested)
    # Recompilation without an explicit selection does not inherit the prior subset.
    again = compile_attack_campaign(prepared.manifest, replace(prepared.assignment_context))
    assert {tid for task in again.assignments for tid in task.technique_ids} == full_ids
