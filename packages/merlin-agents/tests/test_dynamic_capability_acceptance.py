"""Black-box acceptance for next-wave capability expansion and recon reuse."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from types import SimpleNamespace

from merlin_agents import (
    AssignmentBudget,
    AssignmentContext,
    AssignmentTechnique,
    CampaignManifest,
    CampaignProfile,
    CampaignService,
    ContextDocument,
    DynamicSpecialistProposal,
    LeadFollowUp,
    ReviewedTargetAdapter,
    WorkerHandoff,
    WorkerProfile,
    WorkerRuntime,
)
from merlin_agents.campaign import CampaignCompilation
from merlin_agents.campaign_service import _json_value
from merlin_contracts import (
    AgentTask,
    ChatRequest,
    ModelCompleted,
    ModelEvent,
    TextDelta,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolSpec,
)

REQUESTED_TOOL = "mcp__scanner__lookup"


class _Store:
    def __init__(self, manifest: CampaignManifest, context: AssignmentContext) -> None:
        self.messages: list[dict[str, object]] = []
        self.lead: list[dict[str, object]] = []
        self.waves = [{"wave": 1, "schedule_id": "wave-1", "status": "running"}]
        self.row = {
            "id": manifest.id,
            "session_id": manifest.session_id,
            "status": "executing",
            "manifest": {
                "manifest": _json_value(asdict(manifest)),
                "assignment_context": _json_value(asdict(context)),
            },
        }

    def get(self, _campaign_id: str):
        return self.row

    def list_waves(self, _campaign_id: str):
        return list(self.waves)

    def record_worker_message(self, message):
        row = dict(message)
        self.messages.append(row)
        return row

    def append_lead(self, _campaign_id: str, wave: int, payload):
        row = {"wave": wave, "payload": dict(payload)}
        self.lead.append(row)
        return row

    def list_lead(self, _campaign_id: str):
        return list(self.lead)

    def add_wave(self, _campaign_id: str, wave: int, kind: str, schedule_id: str):
        row = {"wave": wave, "kind": kind, "schedule_id": schedule_id, "status": "running"}
        self.waves.append(row)
        return row


class _Agents:
    def __init__(self, source: AgentTask) -> None:
        self.source = source
        self.submitted: tuple[tuple[object, ...], dict[str, object]] | None = None
        self.started = False

    async def list_schedule_tasks(self, _schedule_id: str):
        return [self.source]

    async def submit(self, _session_id: str, assignments, **options):
        values = tuple(assignments)
        self.submitted = (values, dict(options))
        return SimpleNamespace(id=str(options["schedule_id"]), assignments=values, execution=None)

    def start_execution(self, _live, *, max_workers: int):
        assert max_workers == 1
        self.started = True


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, _request: ChatRequest) -> AsyncIterator[ModelEvent]:
        self.calls += 1
        if self.calls == 1:
            yield ToolCall(call_id="expanded-call", name=REQUESTED_TOOL, arguments={})
            yield ModelCompleted(finish_reason="tool_calls")
            return
        yield TextDelta(text='{"finding":"expanded capability executed"}')
        yield ModelCompleted(finish_reason="stop")


class _Tools:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def describe(self):
        return (ToolSpec(name=REQUESTED_TOOL, description="scan", input_schema={}),)

    def effect(self, _name: str) -> str:
        return "provider_network"

    async def invoke(self, name: str, _context: ToolContext, _arguments) -> ToolResult:
        self.calls.append(name)
        return ToolResult(content="scanner receipt")


async def test_worker_request_becomes_exact_next_wave_grant_and_executes_with_reused_recon() -> (
    None
):
    """Exercise request, Lead admission, durable schedule grant, and worker runtime."""
    budget = AssignmentBudget(8, 32_000, 120)
    manifest = CampaignManifest.create(
        id="campaign/dynamic-capability-acceptance",
        session_id="session/acceptance",
        objective="Reuse reconnaissance, then test the newly discovered provider surface",
        profile=CampaignProfile.RECOMMENDED,
        target=ReviewedTargetAdapter("target/acceptance", "api", "https://target.test", "model"),
        provider_id="provider",
        provider_model="model",
        provider_effort="default",
        authority_ids=("session:session/acceptance", "target:target/acceptance"),
        catalogue=(ContextDocument.create("case:scan", "Inspect provider-side metadata"),),
        skill_documents=(),
        tool_ids=("target_chat", "campaign_share", REQUESTED_TOOL),
        budget=budget,
        specialist_composition="autonomous",
    )
    context = AssignmentContext(
        target_profile_id=manifest.target.id,
        techniques=(
            AssignmentTechnique(
                "scan", "Inspect provider-side metadata", frozenset({"provider"}), ("case:scan",)
            ),
        ),
        tool_ids=manifest.tool_ids,
        skill_ids=(),
        provider_id="provider",
        authority_ids=manifest.authority_ids,
        budget=budget,
        input_schema='{"type":"object"}',
        output_schema='{"type":"object","required":["finding"]}',
    )
    source = AgentTask(
        id=f"{manifest.id}/wave-1/injection",
        objective="test the initial surface",
        status="running",
    )
    store = _Store(manifest, context)
    agents = _Agents(source)
    service = CampaignService(store, agents, object())  # type: ignore[arg-type]

    request = await service.share_worker_message(
        ToolContext(
            session_id=manifest.session_id,
            workspace_root="/workspace",
            campaign_id=manifest.id,
            agent_id=source.id,
            call_id="capability-request-1",
        ),
        "lead",
        "Recon found a provider-side metadata surface; schedule a scoped successor.",
        kind="capability_request",
        requested_tool_ids=(REQUESTED_TOOL,),
    )
    request_id = str(request["message_id"])
    assert json.loads(str(store.messages[0]["content"]))["requested_tool_ids"] == [REQUESTED_TOOL]

    reused_recon = AgentTask(
        id="recon-result:reused",
        objective="reused reconnaissance",
        status="completed",
        result="provider surface observed",
    )
    completed_source = AgentTask(
        id=source.id,
        objective=source.objective,
        status="completed",
        result="capability gap discovered",
    )
    proposal = DynamicSpecialistProposal(
        campaign_id=manifest.id,
        label="dynamic-provider-scan",
        objective="Use the requested scanner against the reviewed target",
        source_task_ids=(source.id, reused_recon.id),
        evidence_refs=(request_id, "recon-result:reused"),
        evidence_expectation="an attributable scanner receipt",
        technique_ids=("scan",),
        tool_ids=(REQUESTED_TOOL,),
        grant_effects=frozenset({"provider_network"}),
    )
    approved = LeadFollowUp(
        proposal.label,
        proposal.objective,
        proposal.source_task_ids,
        specialist=proposal,
        capability_request_ids=(request_id,),
        expanded_tool_ids=(REQUESTED_TOOL,),
    )
    compiled = await service._admit_dynamic_specialists(  # noqa: SLF001
        manifest.id,
        manifest,
        context,
        2,
        (approved,),
        source_tasks=(completed_source, reused_recon),
        source_evidence={
            completed_source.id: {"digest": "sha256:" + "1" * 64, "reference": request_id},
            reused_recon.id: {
                "digest": "sha256:" + "2" * 64,
                "reference": reused_recon.id,
            },
        },
    )

    assert len(compiled.assignments) == 1
    assignment = compiled.assignments[0]
    assert assignment.tool_ids == (REQUESTED_TOOL,)
    assert assignment.grant_id is not None
    assert assignment.grant_id.startswith("dynamic-grant:")

    await service._start_wave(  # noqa: SLF001
        manifest.id,
        manifest,
        2,
        "followup",
        CampaignCompilation(compiled.assignments, compiled.worker_contexts),
    )
    assert agents.submitted is not None and agents.started
    submitted_assignments, options = agents.submitted
    grant = options["grants"][assignment.grant_id]  # type: ignore[index]
    assert submitted_assignments == (assignment,)
    assert grant.effect_classes == frozenset({"provider_network"})
    assert grant.target_scope == manifest.target.id

    runtime_tools = _Tools()
    result = await WorkerRuntime(_Provider(), runtime_tools, capability_grant=grant).run(
        WorkerHandoff(
            manifest.session_id,
            "/workspace",
            "model",
            WorkerProfile(
                assignment.profile_id,
                1,
                "dynamic provider scan",
                "Use only the admitted scanner.",
                frozenset({"provider"}),
            ),
            assignment,
            worker_context=compiled.worker_contexts[0],
        )
    )
    assert result.status == "completed"
    assert runtime_tools.calls == [REQUESTED_TOOL]
    assert result.structured == {"finding": "expanded capability executed"}
