import asyncio
import hashlib
import json
import random
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from merlin_agents import (
    AssignmentBudget,
    AssignmentContext,
    AssignmentTechnique,
    DurableAgentService,
    DynamicSpecialistProposal,
    PortableMemoryQuery,
    PortableMemoryRecord,
    RetryFailureKind,
    ScheduleArtifactTooLarge,
    WorkerAssignment,
    WorkerHandoff,
    WorkerProfile,
    WorkerResult,
    WorkerRuntime,
    compile_dynamic_specialist,
)
from merlin_agents.campaign import ContextDocument, ReviewedTargetAdapter, WorkerContext, _digest
from merlin_agents.campaign_service import CampaignService
from merlin_agents.durable_service import (
    _AdaptiveConcurrency,
    _advisory_evaluation_failure,
    _assignment,
    _capability_grant_narrows,
    _sealed_assignment_candidate_identity,
    _target_control_hold_marker,
    _target_control_hold_projection,
    _TargetAdmissionFencedTools,
    _TargetAdmissionHeld,
    _transport_retry_backoff_seconds,
    _usage_with_assignment_cost,
    _validated_target_evidence_receipts,
    _with_evaluation_uncertainty,
    _worker_context,
)
from merlin_agents.graph import EdgeKind as GraphEdgeKind
from merlin_agents.target_reobservation import (
    extract_target_reobservation_obligations,
    project_target_reobservations,
)
from merlin_contracts import (
    AgentTask,
    CapabilityGrant,
    ChildSwarmRequest,
    ModelCompleted,
    ModelError,
    ProviderFailureKind,
    SwarmRequest,
    TextDelta,
    ToolCall,
    ToolContext,
    ToolEffectRecord,
    ToolEffectState,
    ToolInteractionRole,
    ToolResult,
    ToolSpec,
    candidate_evidence_digest,
    candidate_id_for_identity,
    project_candidate_outcome,
)
from merlin_tools import ToolBroker


def _wave0_admitter(campaign_id: str):
    """Admit one wave-0 dynamic specialist per technique in the selected context."""

    async def admitter(_request, context: AssignmentContext):
        parent = WorkerAssignment(
            id=f"{campaign_id}/dynamic-authority-ceiling",
            objective="campaign authority ceiling for dynamic specialist admission",
            profile_id="adversarial-explorer",
            target_profile_id=context.target_profile_id,
            technique_ids=tuple(item.id for item in context.techniques),
            guidance_ids=tuple(
                dict.fromkeys(
                    guidance_id for item in context.techniques for guidance_id in item.guidance_ids
                )
            ),
            tool_ids=context.tool_ids,
            skill_ids=context.skill_ids,
            provider_id=context.provider_id,
            authority_ids=context.authority_ids,
            budget=context.budget,
            input_schema=context.input_schema,
            output_schema=context.output_schema,
            provider_model=context.provider_model,
            provider_capabilities=context.capabilities_for_model(context.provider_model),
            provider_input_cost_per_million=context.pricing_for_model(context.provider_model)[0],
            provider_output_cost_per_million=context.pricing_for_model(context.provider_model)[1],
        )
        records = []
        for index, technique in enumerate(context.techniques):
            proposal = DynamicSpecialistProposal(
                campaign_id=campaign_id,
                label=f"dynamic-probe-{index}",
                objective=f"Probe the reviewed surface for {technique.id}.",
                source_task_ids=("recon-1",),
                evidence_refs=("merlin-result://recon/recon-1",),
                evidence_expectation="Cite the admitted recon evidence for every claim.",
                technique_ids=(technique.id,),
            )
            records.append(
                compile_dynamic_specialist(
                    proposal,
                    parent=parent,
                    context=context,
                    graph_id=campaign_id,
                    wave=0,
                    active_count=index,
                    campaign_count=index,
                )
            )
        return tuple(records)

    return admitter


def test_delivery_identity_survives_assignment_serialization_with_legacy_defaults() -> None:
    assignment = WorkerAssignment(
        id="campaign/one/worker",
        objective="Deliver the sealed transformed payload.",
        profile_id="injection",
        target_profile_id="target-1",
        technique_ids=("policy",),
        guidance_ids=(),
        tool_ids=("target_chat",),
        skill_ids=(),
        provider_id="provider-1",
        authority_ids=("target:target-1",),
        budget=AssignmentBudget(2, 1024, 60),
        input_schema="input/v1",
        output_schema="output/v1",
        planned_language="lzh",
        requested_language="lzh",
        requested_transform_id="translated",
    )

    restored = _assignment(asdict(assignment))
    legacy = asdict(assignment)
    for name in ("planned_language", "requested_language", "requested_transform_id"):
        legacy.pop(name)

    assert restored.planned_language == restored.requested_language == "lzh"
    assert restored.requested_transform_id == "translated"
    assert _assignment(legacy).planned_language == "und"
    assert _assignment(legacy).requested_language == "und"
    assert _assignment(legacy).requested_transform_id == "baseline"


def test_persisted_adaptive_worker_context_restores_strategy_before_digest_validation() -> None:
    profile = ContextDocument.create("profile:injection@1", "Assess the reviewed target.")
    target = ReviewedTargetAdapter("target-1", "http_api", "https://target.test")
    values = {
        "assignment_id": "adaptive-worker",
        "profile": asdict(profile),
        "guidance": [],
        "skills": [],
        "tool_ids": ("target_request",),
        "target": asdict(target),
        "authority_ids": ("target:target-1",),
        "strategy_id": "hydra-select",
        "strategy_contract": (("version", "merlin.hydra-select.v1"),),
    }
    persisted = dict(values, digest=_digest(values))

    restored = _worker_context(persisted)

    assert isinstance(restored, WorkerContext)
    assert restored.strategy_id == "hydra-select"
    assert restored.strategy_contract == (("version", "merlin.hydra-select.v1"),)


def test_persisted_adaptive_assignment_preserves_execution_mode_provenance() -> None:
    base = WorkerAssignment(
        id="adaptive-worker",
        objective="Run the authorized technique.",
        profile_id="injection",
        target_profile_id="target-1",
        technique_ids=("technique-1",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider-1",
        authority_ids=("target:target-1",),
        budget=AssignmentBudget(2, 1024, 60),
        input_schema="input/v1",
        output_schema="output/v1",
        semantic_technique_id="semantic:technique-1",
        strategy_id="goat",
        strategy_contract=(
            ("strategy_execution_mode", "degraded"),
            ("strategy_execution_reason", "native callbacks unavailable"),
        ),
    )

    restored = _assignment(asdict(base))

    assert restored.strategy_id == "goat"
    assert dict(restored.strategy_contract) == {
        "strategy_execution_mode": "degraded",
        "strategy_execution_reason": "native callbacks unavailable",
    }


class Provider:
    async def stream(self, request):
        if "Split this assessment" in request.messages[0].content:
            yield TextDelta(
                text=(
                    '{"tasks":['
                    '{"id":"headers","objective":"Assess response headers"},'
                    '{"id":"body","objective":"Assess response body"}]}'
                )
            )
        else:
            yield TextDelta(text='{"finding":"checked"}')
        yield ModelCompleted()


class RetryProvider(Provider):
    def __init__(self):
        self.worker_calls = 0

    async def stream(self, request):
        if "Split this assessment" in request.messages[0].content:
            async for event in super().stream(request):
                yield event
            return
        self.worker_calls += 1
        if self.worker_calls <= 2:
            yield ModelError(message="temporary")
        else:
            yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()


class ConcurrencyProvider(Provider):
    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0

    async def stream(self, request):
        if "Split this assessment" in request.messages[0].content:
            async for event in super().stream(request):
                yield event
            return
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(0.01)
            yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()
        finally:
            self.active -= 1


class FindingProvider(Provider):
    async def stream(self, request):
        if "Split this assessment" in request.messages[0].content:
            async for event in super().stream(request):
                yield event
            return
        yield TextDelta(
            text=(
                '{"finding_drafts":[{'
                '"title":"Missing security header",'
                '"severity":"medium",'
                '"summary":"A required response header is absent.",'
                '"reproduction":"Request the assessed endpoint.",'
                '"remediation":"Configure the response header."}]}'
            )
        )
        yield ModelCompleted()


class Tools:
    def describe(self):
        return ()

    async def invoke(self, name, context, arguments):
        raise AssertionError("no tools admitted")

    async def resolve_target_session_id(self, name, context):
        del name
        return context.expected_target_session_id or f"test:{context.target_id or 'target'}"

    async def invoke_with_admission(self, name, context, arguments, admit):
        """Test double for a broker that atomically adopts its admission permit."""
        assert context.call_id is not None
        encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
        ownership = context.attempt_id or context.agent_id or "legacy"
        record = ToolEffectRecord(
            idempotency_key=hashlib.sha256(
                (f"{context.session_id}\0{ownership}\0{context.call_id}\0{name}").encode()
            ).hexdigest(),
            session_id=context.session_id,
            call_id=context.call_id,
            agent_id=context.agent_id,
            attempt_id=context.attempt_id,
            campaign_id=context.campaign_id,
            target_id=context.target_id,
            tool_name=name,
            effect="target_network",
            arguments_digest=hashlib.sha256(encoded).hexdigest(),
            arguments_payload=dict(arguments),
            state=ToolEffectState.INTENT,
        )
        permit_digest = await admit(record)
        assert permit_digest.startswith("sha256:")
        return await self.invoke(
            name,
            context.model_copy(update={"target_effect_permit_digest": permit_digest}),
            arguments,
        )


def test_durable_service_resolves_target_exchange_by_typed_role() -> None:
    class TypedTools(Tools):
        def describe(self):
            return (
                ToolSpec(
                    name="custom_exchange",
                    description="exchange",
                    input_schema={},
                    interaction_role=ToolInteractionRole.TARGET_EXCHANGE,
                ),
                ToolSpec(name="browser_upload", description="upload", input_schema={}),
            )

    service = DurableAgentService(Provider(), TypedTools(), Store())

    assert service.target_exchange_tool_ids(("browser_upload", "custom_exchange")) == (
        "custom_exchange",
    )


async def test_target_effect_fence_binds_trusted_session_before_permit_and_rejects_drift() -> None:
    class Delegate:
        def __init__(self) -> None:
            self.session_id = "browser:context-a"
            self.invocations = 0
            self.invalid_session = False

        def describe(self):
            return ()

        def effect(self, name):
            assert name == "browser_chat_exchange"
            return "target_network"

        async def resolve_target_session_id(self, name, context):
            del name, context
            if self.invalid_session:
                raise ValueError("invalid physical session witness")
            return self.session_id

        async def invoke_with_admission(self, name, context, arguments, admit):
            self.invocations += 1
            assert context.expected_target_session_id == self.session_id
            encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
            record = ToolEffectRecord(
                idempotency_key=hashlib.sha256(encoded).hexdigest(),
                session_id=context.session_id,
                call_id=str(context.call_id),
                agent_id=context.agent_id,
                attempt_id=context.attempt_id,
                campaign_id=context.campaign_id,
                target_id=context.target_id,
                tool_name=name,
                effect="target_network",
                arguments_digest=hashlib.sha256(encoded).hexdigest(),
                arguments_payload=dict(arguments),
                state=ToolEffectState.INTENT,
            )
            await admit(record)
            return ToolResult(content="delivered")

    delegate = Delegate()
    admitted_sessions: list[str | None] = []

    async def admit(_record, target_session_id):
        admitted_sessions.append(target_session_id)
        return "sha256:" + "a" * 64

    tools = _TargetAdmissionFencedTools(delegate, ("browser_chat_exchange",), admit)
    context = ToolContext(
        session_id="daemon-session",
        workspace_root="/tmp",
        call_id="call-1",
        agent_id="worker-1",
        attempt_id="attempt-1",
        campaign_id="campaign/one",
        target_id="target-1",
    )

    result = await tools.invoke("browser_chat_exchange", context, {"prompt": "probe"})
    assert result.is_error is False
    assert admitted_sessions == ["browser:context-a"]

    delegate.session_id = "browser:context-b"
    conflict = await tools.invoke(
        "browser_chat_exchange",
        context.model_copy(update={"expected_target_session_id": "browser:context-a"}),
        {"prompt": "must not dispatch"},
    )
    assert conflict.is_error is True
    assert conflict.metadata["failure_code"] == "target_session_identity_mismatch"
    assert delegate.invocations == 1
    assert admitted_sessions == ["browser:context-a"]

    delegate.invalid_session = True
    invalid = await tools.invoke(
        "browser_chat_exchange",
        context.model_copy(update={"expected_target_session_id": "browser:context-a"}),
        {"prompt": "must still not dispatch"},
    )
    assert invalid.is_error is True
    assert invalid.metadata["failure_code"] == "target_session_identity_unavailable"
    assert delegate.invocations == 1

    delegate.invalid_session = False
    malformed = await tools.invoke(
        "browser_chat_exchange",
        context.model_copy(update={"expected_target_session_id": " browser:context-a"}),
        {"prompt": "must not dispatch with a malformed prebound identity"},
    )
    assert malformed.is_error is True
    assert malformed.metadata["failure_code"] == "target_session_identity_unavailable"
    assert delegate.invocations == 1
    assert admitted_sessions == ["browser:context-a"]

    delegate.session_id = "a" * 256
    accepted_boundary = await tools.invoke(
        "browser_chat_exchange",
        context,
        {"prompt": "accept the canonical session-id boundary"},
    )
    assert accepted_boundary.is_error is False
    assert admitted_sessions[-1] == "a" * 256
    assert delegate.invocations == 2

    delegate.session_id = "b" * 257
    rejected_boundary = await tools.invoke(
        "browser_chat_exchange",
        context,
        {"prompt": "reject before permit or dispatch"},
    )
    assert rejected_boundary.is_error is True
    assert rejected_boundary.metadata["failure_code"] == "target_session_identity_unavailable"
    assert delegate.invocations == 2
    assert admitted_sessions[-1] == "a" * 256


@pytest.mark.parametrize("latched_phase", ("admission", "adoption"))
async def test_target_effect_fence_attributes_latched_admission_before_dispatch(
    latched_phase: str,
) -> None:
    class AdmissionLatched(RuntimeError):
        target_admission_latched = True

    class Delegate:
        def __init__(self) -> None:
            self.dispatched = False

        def describe(self):
            return ()

        def effect(self, _name):
            return "target_network"

        async def resolve_target_session_id(self, _name, _context):
            return "browser:held-context"

        async def invoke_with_admission(self, name, context, arguments, admit):
            encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
            intent = ToolEffectRecord(
                idempotency_key=hashlib.sha256(encoded).hexdigest(),
                session_id=context.session_id,
                call_id=str(context.call_id),
                agent_id=context.agent_id,
                attempt_id=context.attempt_id,
                campaign_id=context.campaign_id,
                target_id=context.target_id,
                tool_name=name,
                effect="target_network",
                arguments_digest=hashlib.sha256(encoded).hexdigest(),
                arguments_payload=dict(arguments),
                state=ToolEffectState.INTENT,
            )
            await admit(intent)
            if latched_phase == "adoption":
                raise AdmissionLatched("permit adoption lost its lane authority")
            self.dispatched = True
            return ToolResult(content="must not dispatch")

    async def reject(_intent, _target_session_id):
        if latched_phase == "admission":
            raise AdmissionLatched("surface transition fenced the stale assignment")
        return "sha256:" + "a" * 64

    delegate = Delegate()
    tools = _TargetAdmissionFencedTools(delegate, ("custom_exchange",), reject)
    context = ToolContext(
        session_id="daemon-session",
        workspace_root="/tmp",
        call_id="call-held",
        agent_id="campaign/held/worker",
        attempt_id="attempt-held",
        technique_attempt_id="technique-held",
        campaign_id="campaign/held",
        target_id="target-held",
    )

    with pytest.raises(_TargetAdmissionHeld) as raised:
        await tools.invoke("custom_exchange", context, {"prompt": "probe"})

    hold = raised.value
    assert delegate.dispatched is False
    assert hold.assignment_id == context.agent_id
    assert hold.attempt_id == context.attempt_id
    assert hold.technique_attempt_id == context.technique_attempt_id
    assert hold.tool_name == "custom_exchange"
    assert hold.call_id == context.call_id
    assert hold.failure_code == "target_session_resolution_unavailable"
    assert hold.target_session_id == "browser:held-context"
    marker = _target_control_hold_marker(hold)
    projection = _target_control_hold_projection(hold, marker)
    assert set(marker) == {
        "schema",
        "assignment_id",
        "attempt_id",
        "technique_attempt_id",
        "tool_name",
        "call_id",
        "failure_code",
        "target_session_id",
        "receipt_sha256",
    }
    assert set(projection) == set(marker) | {
        "task_id",
        "effect_applied",
        "target_effect_attempted",
        "interaction_count",
        "limit_interaction_count",
        "target_call_budget_charge",
        "target_call_ids",
        "target_evidence",
        "target_ui_transitions",
    }
    assert projection["effect_applied"] is False
    assert projection["target_effect_attempted"] is False
    assert projection["interaction_count"] == 0
    assert projection["limit_interaction_count"] == 0
    assert projection["target_call_budget_charge"] == 0
    assert projection["target_call_ids"] == []
    assert projection["target_evidence"] == []
    assert projection["target_ui_transitions"] == []


def test_worker_graph_uses_typed_retry_policy_instead_of_one_retry() -> None:
    assignment = WorkerAssignment(
        id="campaign/retry/worker",
        objective="Exercise transient infrastructure recovery",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("retry",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_seconds=60),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    service = DurableAgentService(Provider(), Tools(), Store(), model="test")

    graph = service._graph_spec("typed-retry", (assignment,))  # noqa: SLF001
    retry = next(edge for edge in graph.edges if edge.kind is GraphEdgeKind.RETRY)

    assert retry.max_retries == 0
    assert retry.retry_policy is not None
    assert RetryFailureKind.POINTER_REPAIR.value in retry.retry_policy.failure_kinds
    assert RetryFailureKind.ADAPTER_TIMEOUT.value in retry.retry_policy.failure_kinds
    assert RetryFailureKind.AUTHORIZATION.value not in retry.retry_policy.failure_kinds
    assert RetryFailureKind.BUDGET.value not in retry.retry_policy.failure_kinds
    assert RetryFailureKind.CONFIGURATION.value not in retry.retry_policy.failure_kinds


def test_capability_probe_graph_stops_identical_infrastructure_retry_storm_early() -> None:
    assignment = WorkerAssignment(
        id="campaign/retry/capability-probe/memory",
        objective="Probe memory harmlessly",
        profile_id="recon",
        target_profile_id="target",
        technique_ids=("capability/memory",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_seconds=60),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    service = DurableAgentService(Provider(), Tools(), Store(), model="test")

    graph = service._graph_spec("probe-retry", (assignment,))  # noqa: SLF001
    retry = next(edge for edge in graph.edges if edge.kind is GraphEdgeKind.RETRY)

    assert retry.retry_policy is not None
    assert retry.retry_policy.max_consecutive_stalls == 1


def test_worker_graph_fail_fast_rules_for_target_effect_and_schema_failures() -> None:
    assignment = WorkerAssignment(
        id="campaign/retry/worker/one",
        objective="Run a reviewed attack",
        profile_id="injection-specialist",
        target_profile_id="target",
        technique_ids=("technique/one",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_seconds=60),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    service = DurableAgentService(Provider(), Tools(), Store(), model="test")

    graph = service._graph_spec("fail-fast", (assignment,))  # noqa: SLF001
    retry = next(edge for edge in graph.edges if edge.kind is GraphEdgeKind.RETRY)

    assert retry.retry_policy is not None
    assert RetryFailureKind.MISSING_TARGET_EFFECT.value in retry.retry_policy.failure_kinds
    assert RetryFailureKind.RESULT_SCHEMA_INVALID.value in retry.retry_policy.failure_kinds
    assert (
        RetryFailureKind.MISSING_TARGET_EFFECT.value
        not in retry.retry_policy.failure_kind_stall_limits
    )
    assert (
        retry.retry_policy.failure_kind_stall_limits[RetryFailureKind.RESULT_SCHEMA_INVALID.value]
        == 1
    )
    assert (
        retry.retry_policy.failure_kind_stall_limits[
            RetryFailureKind.TARGET_ADAPTER_UNHEALTHY.value
        ]
        == 1
    )
    assert RetryFailureKind.TRANSPORT_TRANSIENT.value in retry.retry_policy.failure_kinds
    assert (
        RetryFailureKind.TRANSPORT_TRANSIENT.value
        not in retry.retry_policy.failure_kind_stall_limits
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            WorkerResult(
                task_id="x",
                status="failed",
                error="provider output truncated",
                retry_class="fresh_worker",
                retry_failure_kind=RetryFailureKind.PROVIDER_TRUNCATED,
            ),
            RetryFailureKind.PROVIDER_TRUNCATED,
        ),
        (
            WorkerResult(
                task_id="x",
                status="failed",
                error="response pointer missing",
                retry_class="fresh_worker",
                infrastructure_failure_category="missing_response_pointer",
            ),
            RetryFailureKind.POINTER_REPAIR,
        ),
        (
            WorkerResult(
                task_id="x",
                status="failed",
                error="authorization denied",
                retry_class="terminal",
                retry_failure_kind=RetryFailureKind.AUTHORIZATION,
            ),
            RetryFailureKind.AUTHORIZATION,
        ),
        (
            WorkerResult(
                task_id="x",
                status="failed",
                error="provider incorrectly labelled quota as retryable",
                error_code="provider_quota_exhausted",
                retry_class="safe_provider",
                retry_failure_kind=RetryFailureKind.PROVIDER_TRANSIENT,
            ),
            RetryFailureKind.BUDGET,
        ),
        (
            WorkerResult(
                task_id="x",
                status="failed",
                error="worker completed without executing a required target effect",
                error_code="required_target_effect_missing",
                retry_class="fresh_worker",
                infrastructure_failure_category="missing_target_effect",
            ),
            RetryFailureKind.MISSING_TARGET_EFFECT,
        ),
        (
            WorkerResult(
                task_id="x",
                status="failed",
                error="remote target session is owned by another live assignment",
                error_code="session_isolation_unavailable",
                retry_class="fresh_worker",
                retry_failure_kind=RetryFailureKind.TARGET_ADAPTER_UNHEALTHY,
                infrastructure_failure_category="delivery_failure",
            ),
            RetryFailureKind.TRANSPORT_TRANSIENT,
        ),
        (
            WorkerResult(
                task_id="x",
                status="failed",
                error="strategy contract mismatch",
                error_code="evaluation_contract_drift",
                retry_class="fresh_worker",
                infrastructure_failure_category="evaluation_contract_drift",
            ),
            RetryFailureKind.WORKER_INTERRUPTED,
        ),
    ],
)
def test_retry_failure_classification_is_typed(
    result: WorkerResult, expected: RetryFailureKind
) -> None:
    assert DurableAgentService._retry_failure_kind(result) is expected  # noqa: SLF001


def test_retry_failure_classification_does_not_parse_prose() -> None:
    result = WorkerResult(
        task_id="x",
        status="failed",
        error="authorization timeout truncated pointer scope budget",
        retry_class="terminal",
    )

    assert DurableAgentService._retry_failure_kind(result) is RetryFailureKind.UNKNOWN  # noqa: SLF001


@pytest.mark.asyncio
async def test_session_conflict_retry_does_not_restore_post_failure_checkpoint() -> None:
    store = Store()
    store.results["schedule-session-conflict"] = {
        "partial:conflict": {
            "node_id": "worker:campaign/x/conflicted",
            "generation": 1,
            "schema_id": "schema/partial-result",
            "payload": {
                "retry_failure_kind": RetryFailureKind.TRANSPORT_TRANSIENT.value,
                "worker_checkpoint": {
                    "effect_observed": False,
                    "messages": [
                        {
                            "role": "tool",
                            "content": "session_isolation_unavailable",
                        }
                    ],
                    "target_evidence": [],
                },
            },
        }
    }
    service = DurableAgentService(Provider(), Tools(), store, model="test")

    checkpoint = await service._latest_worker_checkpoint(  # noqa: SLF001
        "schedule-session-conflict", "worker:campaign/x/conflicted"
    )

    assert checkpoint is None


def test_retry_progress_ignores_fresh_receipt_and_session_identifiers() -> None:
    def result(
        call_id: str,
        reference: str,
        session_id: str,
        *,
        response: str = '{"answer":"same","request_id":"one"}',
    ) -> WorkerResult:
        evidence: dict[str, dict[str, object]] = {}
        WorkerRuntime._record_target_evidence(  # noqa: SLF001
            evidence,
            SimpleNamespace(
                session_id=session_id,
                assignment=SimpleNamespace(
                    id="campaign/retry-progress/worker",
                    target_profile_id="target",
                    technique_ids=(),
                ),
                worker_context=None,
                attempt_id=None,
                technique_attempt_id=None,
                candidate_identity=None,
            ),
            ToolCall(
                call_id=call_id,
                name="target_request",
                arguments={"input": "same", "session_id": session_id},
            ),
            ToolResult(
                content=response,
                metadata={
                    "target_session_id": session_id,
                    "target_exchanges": [
                        {
                            "interaction": True,
                            "cleanup": False,
                            "request_id": f"request-{call_id}",
                            "status_code": 200,
                        }
                    ],
                },
            ),
        )
        receipt = next(iter(evidence.values()))
        receipt["reference"] = reference
        return WorkerResult(
            task_id="x",
            status="failed",
            retry_class="fresh_worker",
            target_session_id=session_id,
            checkpoint={
                "effect_observed": True,
                "target_session_generation": 99,
                "target_evidence": [receipt],
                "strategy_state": {
                    "schema": "merlin.crescendo-state.v1",
                    "attempt_id": f"attempt-{call_id}",
                    # A replacement attempt recomputes this digest from state
                    # that includes its fresh attempt id.  It is lineage, not
                    # evidence that the attack or finalization advanced.
                    "checkpoint_digest": f"sha256:checkpoint-{call_id}",
                    "target_session_id": session_id,
                    "steps": [
                        {
                            "call_id": call_id,
                            "exchange_digest": f"digest-{call_id}",
                            "stage": "analysis",
                        }
                    ],
                },
                "finalization_only": True,
                "truncation_finalizer_only": True,
                "finalization_progress": [1, 42],
            },
        )

    first = DurableAgentService._retry_progress_marker(  # noqa: SLF001
        result("call-1", "target-call:call-1", "session-1")
    )
    second = DurableAgentService._retry_progress_marker(  # noqa: SLF001
        result(
            "call-2",
            "target-call:call-2",
            "session-2",
            response='{  "request_id": "two", "answer" : "same" }',
        )
    )

    assert first == second

    changed = result(
        "call-3",
        "target-call:call-3",
        "session-3",
        response='{"request_id":"three","answer":"changed"}',
    )
    assert DurableAgentService._retry_progress_marker(changed) != first  # noqa: SLF001


def test_advisory_evaluation_disagreement_is_structured_without_losing_worker_output() -> None:
    result = WorkerResult(
        task_id="x",
        status="quarantined",
        structured={"finding_drafts": [{"title": "candidate"}]},
        error="target oracle disagreed with the completed exchange",
        checkpoint={"effect_observed": True},
    )

    reason = _advisory_evaluation_failure(result)
    structured = _with_evaluation_uncertainty(
        result.structured,
        source="durable_execution",
        reason=str(reason),
        confidence=0.0,
    )

    assert reason == "target oracle disagreed with the completed exchange"
    assert structured["finding_drafts"] == [{"title": "candidate"}]
    assert structured["evaluation_uncertainties"] == [
        {
            "source": "durable_execution",
            "reason": reason,
            "confidence": 0.0,
        }
    ]


def test_duplicate_effect_quarantine_is_not_misclassified_as_evaluation_uncertainty() -> None:
    result = WorkerResult(
        task_id="x",
        status="quarantined",
        error="GOAT refused to resend an acknowledged target prompt",
        checkpoint={"effect_observed": True},
    )

    assert _advisory_evaluation_failure(result) is None


def test_hydra_presence_globally_serializes_reviewed_api_session_workers() -> None:
    budget = AssignmentBudget(max_turns=8, max_tokens=8_000, max_seconds=120)

    def assignment(task_id: str, *, strategy_id: str = "direct") -> WorkerAssignment:
        return WorkerAssignment(
            id=task_id,
            objective=f"Execute {task_id}",
            profile_id="injection",
            target_profile_id="target-1",
            technique_ids=(f"case:{task_id}",),
            guidance_ids=(),
            tool_ids=("target_request",),
            skill_ids=(),
            provider_id="provider-1",
            authority_ids=("target:target-1",),
            budget=budget,
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
            strategy_id=strategy_id,
            semantic_technique_id=("semantic:hydra" if strategy_id == "hydra-select" else None),
            strategy_contract=(
                (("version", "merlin.hydra-select.v1"),) if strategy_id == "hydra-select" else ()
            ),
            comparison_supported=strategy_id != "hydra-select",
            comparison_block_reason=(
                "fixture isolates graph serialization" if strategy_id == "hydra-select" else None
            ),
        )

    assignments = (
        assignment("baseline-before"),
        assignment("hydra-owner", strategy_id="hydra-select"),
        assignment("baseline-after"),
    )
    service = DurableAgentService(Provider(), Tools(), Store())  # type: ignore[arg-type]

    graph = service._graph_spec("hydra-session-serialization", assignments)  # noqa: SLF001

    api_edges = tuple(
        (edge.source, edge.target)
        for edge in graph.edges
        if edge.id.startswith("target-session-owner:")
    )
    assert api_edges == (
        ("worker:baseline-before", "worker:hydra-owner"),
        ("worker:hydra-owner", "worker:baseline-after"),
    )


def test_browser_api_session_pair_is_claimed_once_within_fanout_bound() -> None:
    """A worker that is both a browser specialist and an adaptive API-session
    owner must not receive a parallel edge from each serialization chain: the
    duplicate inflated per-node fan-out past the admitted bound on small waves
    (run17 wave-0: two assignments, three outgoing edges, bound of two)."""
    service = DurableAgentService(Provider(), Tools(), Store(), model="test")

    def assignment(task_id: str, *, strategy_id: str = "direct") -> WorkerAssignment:
        return WorkerAssignment(
            id=task_id,
            objective=f"Execute {task_id}",
            profile_id="adversarial-explorer",
            target_profile_id="target",
            technique_ids=(f"case:{task_id}",),
            guidance_ids=(),
            tool_ids=("browser_chat_exchange", "target_request"),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(None, None, 60),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
            strategy_id=strategy_id,
            semantic_technique_id=("semantic:goat" if strategy_id == "goat" else None),
            strategy_contract=((("version", "merlin.goat.v1"),) if strategy_id == "goat" else ()),
            comparison_supported=strategy_id != "goat",
            comparison_block_reason=(
                "fixture isolates graph serialization" if strategy_id == "goat" else None
            ),
        )

    assignments = (
        assignment("campaign/x/adversarial-explorer/goat-1", strategy_id="goat"),
        assignment("campaign/x/adversarial-explorer/target-derived-discovery"),
    )

    admitted = service._admit("session", assignments, 2, 0)  # noqa: SLF001

    pairs = [
        (edge.source, edge.target)
        for edge in admitted.spec.edges
        if edge.kind is not GraphEdgeKind.RETRY
    ]
    assert len(pairs) == len(set(pairs))


class Store:
    def __init__(self):
        self.schedules = {}
        self.nodes = {}
        self.results = {}
        self.attempts = {}
        self.created_before_claim = False
        self.lifecycle_events = []

    def append_schedule_event(self, schedule_id, event_type, payload):
        self.lifecycle_events.append((schedule_id, event_type, payload))
        self.schedules[schedule_id]["event_count"] += 1

    def create_schedule(
        self,
        session_id,
        graph,
        checkpoint,
        node_ids,
        *,
        ready_nodes=(),
        schedule_id=None,
        initial_status="running",
        parent_schedule_id=None,
        parent_attempt_id=None,
    ):
        identifier = schedule_id or "schedule-1"
        self.schedules[identifier] = {
            "id": identifier,
            "session_id": session_id,
            "graph": graph,
            "checkpoint": checkpoint,
            "parent_schedule_id": parent_schedule_id,
            "parent_attempt_id": parent_attempt_id,
            "event_count": 1,
            "status": initial_status,
        }
        self.nodes[identifier] = {
            node: {"node_id": node, "state": "ready" if node in ready_nodes else "pending"}
            for node in node_ids
        }
        return identifier

    def resume_schedule(self, schedule_id):
        self.schedules[schedule_id]["status"] = "running"
        self.schedules[schedule_id]["event_count"] += 1

    def pause_schedule(self, schedule_id, *, reason):
        self.schedules[schedule_id]["status"] = "paused"
        for attempt in self.attempts.values():
            if attempt.schedule_id != schedule_id or attempt.state != "running":
                continue
            attempt.state = "expired"
            attempt.error = f"clean pause: {reason}"
            node = self.nodes[schedule_id][attempt.node_id]
            if node["state"] == "leased" and node.get("lease_generation") == attempt.generation:
                node["state"] = "ready"
        self.schedules[schedule_id]["event_count"] += 1

    def load_schedule(self, schedule_id):
        return self.schedules.get(schedule_id)

    def list_schedules(self, session_id, *, limit=50):
        return [row for row in self.schedules.values() if row["session_id"] == session_id][:limit]

    def list_active_schedules(self, *, limit=50):
        return [
            row
            for identifier, row in self.schedules.items()
            if row["status"] in {"running", "paused"}
            or (
                row["status"] == "cancelled"
                and any(
                    node_id.startswith("cleanup:") and node["state"] in {"ready", "leased"}
                    for node_id, node in self.nodes[identifier].items()
                )
            )
        ][:limit]

    def find_active_task(self, task_id):
        node_id = task_id if task_id.startswith("worker:") else f"worker:{task_id}"
        return [
            {**schedule, "node_id": node_id}
            for schedule_id, schedule in self.schedules.items()
            if schedule["status"] == "running"
            and node_id in self.nodes[schedule_id]
            and self.nodes[schedule_id][node_id]["state"]
            not in {"completed", "failed", "cancelled"}
        ]

    def list_nodes(self, schedule_id):
        return list(self.nodes[schedule_id].values())

    def list_attempts(self, schedule_id):
        return [
            vars(attempt).copy()
            for attempt in self.attempts.values()
            if attempt.schedule_id == schedule_id
        ]

    def list_results(self, schedule_id):
        return list(self.results.get(schedule_id, {}).values())

    def restore(self, schedule_id):
        return {
            "schedule": self.schedules[schedule_id],
            "nodes": self.list_nodes(schedule_id),
            "attempts": self.list_attempts(schedule_id),
            "results": self.list_results(schedule_id),
            "events": [],
        }

    def transition_ready(self, schedule_id, node_ids, *, checkpoint, expected_event_count, **_):
        assert self.schedules[schedule_id]["event_count"] == expected_event_count
        for node in node_ids:
            self.nodes[schedule_id][node]["state"] = "ready"
        self._advance(schedule_id, checkpoint)

    def transition_failed(self, schedule_id, failures, *, checkpoint, expected_event_count):
        assert self.schedules[schedule_id]["event_count"] == expected_event_count
        for node_id in failures:
            assert self.nodes[schedule_id][node_id]["state"] == "pending"
            self.nodes[schedule_id][node_id]["state"] = "failed"
        self._advance(schedule_id, checkpoint)

    def claim(
        self,
        schedule_id,
        node_id,
        owner,
        *,
        lease_expires_at,
        attempt_id=None,
        target_admission=None,
    ):
        self.created_before_claim = schedule_id in self.schedules
        node = self.nodes[schedule_id][node_id]
        assert node["state"] == "ready"
        generation = node.get("generation", 0) + 1
        node.update(state="leased", generation=generation, lease_generation=generation)
        identifier = attempt_id or str(uuid4())
        lease = SimpleNamespace(
            id=identifier,
            schedule_id=schedule_id,
            node_id=node_id,
            generation=generation,
            owner=owner,
            lease_expires_at=lease_expires_at,
            heartbeat_at=datetime.now(UTC),
            state="running",
            replaced_attempt_id=None,
            lane_recovery_required=False,
        )
        self.attempts[identifier] = lease
        self.schedules[schedule_id]["event_count"] += 1
        return lease

    def final_target_effect_admitted(
        self,
        schedule_id,
        node_id,
        attempt_id,
        generation,
        *,
        tool_name,
        call_id,
        target_admission,
        effect_intent,
    ):
        lease = self.attempts[attempt_id]
        assert lease.schedule_id == schedule_id
        assert lease.node_id == node_id
        assert lease.generation == generation
        assert self.nodes[schedule_id][node_id]["state"] == "leased"
        assert effect_intent["tool_name"] == tool_name
        assert effect_intent["call_id"] == call_id
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    effect_intent,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )

    def resolve_target_lane_recovery(
        self,
        schedule_id,
        node_id,
        attempt_id,
        generation,
        *,
        target_admission,
        recovery,
    ):
        lease = self.attempts[attempt_id]
        assert lease.schedule_id == schedule_id
        assert lease.node_id == node_id
        assert lease.generation == generation
        lease.lane_recovery_required = False

    def heartbeat(self, attempt_id, generation, *, lease_expires_at):
        lease = self.attempts[attempt_id]
        assert lease.generation == generation
        lease.lease_expires_at = lease_expires_at
        lease.heartbeat_at = datetime.now(UTC)
        self.schedules[lease.schedule_id]["event_count"] += 1
        return lease

    def settle_attempt(
        self,
        attempt_id,
        generation,
        outcome,
        *,
        reason,
        checkpoint,
        target_session_id=None,
        failure_kind=None,
        terminal_result=None,
        target_surface_transition=None,
    ):
        lease = self.attempts[attempt_id]
        assert lease.generation == generation
        if terminal_result is not None:
            commit_partial = getattr(self, "commit_partial", None)
            if callable(commit_partial):
                commit_partial(
                    attempt_id,
                    generation,
                    result=terminal_result,
                )
        lease.target_surface_transition = target_surface_transition
        lease.state = outcome
        lease.error = reason
        lease.failure_kind = failure_kind
        self.nodes[lease.schedule_id][lease.node_id]["state"] = outcome
        self._advance(lease.schedule_id, checkpoint)

    def retry_failed(self, schedule_id, node_id, *, checkpoint, expected_event_count):
        assert self.schedules[schedule_id]["event_count"] == expected_event_count
        assert self.nodes[schedule_id][node_id]["state"] in {"failed", "quarantined"}
        self.nodes[schedule_id][node_id]["state"] = "ready"
        self._advance(schedule_id, checkpoint)

    def commit_reducer(self, attempt_id, generation, *, schema_id, result, checkpoint, **_):
        lease = self.attempts[attempt_id]
        assert lease.generation == generation
        lease.state = "completed"
        self.nodes[lease.schedule_id][lease.node_id]["state"] = "completed"
        self.results.setdefault(lease.schedule_id, {})[lease.node_id] = {
            "node_id": lease.node_id,
            "payload": result,
            "payload_digest": "sha256:test",
            "schema_id": schema_id,
        }
        self._advance(lease.schedule_id, checkpoint)
        return "sha256:test"

    def send_message(self, *args, **kwargs):
        return SimpleNamespace(id="message-1")

    def cancel_schedule(self, schedule_id, *, reason, cleanup_node_ids, checkpoint):
        self.schedules[schedule_id]["status"] = "cancelled"
        cleanup = set(cleanup_node_ids)
        for node_id, node in self.nodes[schedule_id].items():
            if node_id in cleanup and node["state"] in {"pending", "ready"}:
                node["state"] = "ready"
            elif node["state"] in {"pending", "ready", "leased"}:
                node["state"] = "cancelled"
        self._advance(schedule_id, checkpoint)

    def reject_schedule(self, schedule_id, *, reason):
        self.schedules[schedule_id]["status"] = "cancelled"
        for node in self.nodes[schedule_id].values():
            if node["state"] in {"pending", "ready", "leased"}:
                node["state"] = "cancelled"

    def reconcile_cancelled_target_lanes(self, schedule_id):
        assert schedule_id in self.schedules
        return {"released": (), "held": (), "latched": ()}

    def cancel_node(self, schedule_id, node_id, *, reason, checkpoint, idempotency_key=None):
        receipt = (schedule_id, idempotency_key)
        previous = getattr(self, "cancel_receipts", {}).get(receipt)
        if previous is not None:
            if previous != (node_id, reason):
                raise ValueError("cancellation identity conflicts")
            return True
        node = self.nodes[schedule_id][node_id]
        if node["state"] in {"completed", "failed", "cancelled"}:
            return False
        node["state"] = "cancelled"
        for attempt in self.attempts.values():
            if attempt.schedule_id == schedule_id and attempt.node_id == node_id:
                attempt.state = "cancelled"
        self._advance(schedule_id, checkpoint)
        if idempotency_key is not None:
            if not hasattr(self, "cancel_receipts"):
                self.cancel_receipts = {}
            self.cancel_receipts[receipt] = (node_id, reason)
        return True

    def _advance(self, schedule_id, checkpoint):
        self.schedules[schedule_id]["checkpoint"] = checkpoint
        self.schedules[schedule_id]["event_count"] += 1


@pytest.mark.asyncio
async def test_current_target_admission_latches_forwards_exact_compact_projection() -> None:
    expected = (
        {
            "target_session_id": "browser:context-1",
            "state": "latched",
            "revision": 3,
            "authorized_schedule_id": None,
        },
    )

    class LatchStore(Store):
        def current_target_admission_latches(
            self, campaign_id: str, session_id: str, target_id: str
        ) -> tuple[dict[str, object], ...]:
            assert (campaign_id, session_id, target_id) == (
                "campaign-1",
                "session-1",
                "target-1",
            )
            return expected

    service = DurableAgentService(Provider(), Tools(), LatchStore(), model="test")

    assert (
        await service.current_target_admission_latches("campaign-1", "session-1", "target-1")
        == expected
    )


@pytest.mark.asyncio
async def test_provider_authorizer_receives_exact_owning_schedule_id() -> None:
    seen_schedule_ids: list[str | None] = []

    async def authorize(handoff):
        seen_schedule_ids.append(handoff.schedule_id)
        return True

    assignment = WorkerAssignment(
        id="campaign/provider-scope/worker",
        objective="Return a bounded result.",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("technique",),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:target",),
        budget=AssignmentBudget(max_turns=1, max_tokens=1_000, max_seconds=30),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    service = DurableAgentService(
        Provider(),
        Tools(),
        Store(),
        model="test",
        provider_authorizer=authorize,
    )

    live = await service.submit(
        "session",
        (assignment,),
        max_workers=1,
        minimum_workers=1,
        schedule_id="schedule/provider-scope",
    )
    assert live.execution is not None
    await live.execution

    assert seen_schedule_ids == ["schedule/provider-scope"]


@pytest.mark.asyncio
async def test_snapshot_read_is_one_scoped_store_call_and_keeps_result_separate() -> None:
    class SnapshotStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.snapshot_calls = []

        def list_swarm_snapshot_rows(self, session_id, *, campaign_id=None):
            self.snapshot_calls.append((session_id, campaign_id))
            return [
                {
                    "schedule_id": "schedule-1",
                    "session_id": session_id,
                    "schedule_status": "running",
                    "campaign_id": "campaign/one",
                    "durable_specialist_id": "specialist/one",
                    "durable_specialist_display_name": "Prompt injection specialist",
                    "assignment": {
                        "id": "task/one",
                        "target_profile_id": "target/one",
                        "technique_ids": ["technique/one"],
                        "swarm_projection": {
                            "specialist_instance_id": "specialist/one",
                            "specialist_identity_provenance": "durable",
                            "specialist_display_name": "Prompt injection specialist",
                            "task_id": "task/one",
                            "execution_stage": "direct",
                            "candidates": [
                                {
                                    "target_id": "target/one",
                                    "target_generation": "generation/one",
                                    "scenario_fingerprint": "sha256:scenario",
                                    "technique_id": "technique/one",
                                    "controller_id": "pair",
                                    "payload_family": "rot13",
                                    "requested_language": "lzh",
                                    "mutation_id": "mutation/rot13",
                                }
                            ],
                        },
                    },
                    "node_id": "worker:task/one",
                    "node_state": "leased",
                    "current_attempt_id": "attempt/current",
                    "current_attempt_state": "failed",
                    "current_attempt_owner": "worker/current",
                    "evaluation_state": "ambiguous",
                    "evaluation_reason": "validator disagreement",
                    "evaluation_detail": {"evaluation_confidence": 0.4},
                    "successful_result_id": "attempt/previous",
                    "successful_result_payload": {"content": "target reply preserved"},
                }
            ]

        def restore(self, schedule_id):
            raise AssertionError("snapshot reads must not restore schedules")

    store = SnapshotStore()
    service = DurableAgentService(Provider(), Tools(), store)

    snapshots = await service.list_swarm_snapshots("session/one", campaign_id="campaign/one")

    assert store.snapshot_calls == [("session/one", "campaign/one")]
    assignment = snapshots[0].specialists[0].assignments[0]
    assert assignment.current_attempt is not None
    assert assignment.current_attempt.lifecycle.value == "failed"
    assert assignment.successful_result is not None
    assert assignment.successful_result.result_id == "attempt/previous"
    assert assignment.candidate.requested_language == "lzh"
    assert assignment.candidate_evaluation.state.value == "evaluation_uncertain"


@pytest.mark.asyncio
async def test_snapshot_does_not_invent_specialists_for_structural_tasks() -> None:
    class SnapshotStore(Store):
        def list_swarm_snapshot_rows(self, session_id, *, campaign_id=None):
            return [
                {
                    "schedule_id": "schedule-1",
                    "session_id": session_id,
                    "schedule_status": "running",
                    "campaign_id": campaign_id,
                    "durable_specialist_id": None,
                    "durable_specialist_display_name": None,
                    "assignment": {
                        "id": "task/one",
                        "target_profile_id": "target/one",
                        "technique_ids": ["technique/one"],
                        "swarm_projection": {
                            "specialist_instance_id": "task/one",
                            "specialist_identity_provenance": "durable",
                            "specialist_display_name": "Invented specialist",
                            "task_id": "task/one",
                            "execution_stage": "execute",
                            "candidates": [],
                        },
                    },
                    "node_state": "ready",
                }
            ]

    service = DurableAgentService(Provider(), Tools(), SnapshotStore())

    (snapshot,) = await service.list_swarm_snapshots("session/one", campaign_id="campaign/one")

    assert snapshot.specialist_count == 0
    assert snapshot.assignment_count == 1
    assert snapshot.specialists[0].specialist_instance_id == "structural:campaign/one"
    assert snapshot.specialists[0].display_name == "Unassigned/structural tasks"


@pytest.mark.asyncio
async def test_submit_persists_distinct_composition_instances_that_share_profile() -> None:
    store = Store()
    service = DurableAgentService(Provider(), Tools(), store)
    profile = WorkerProfile(
        id="dynamic-shared",
        version=1,
        summary="two separately composed prompt specialists",
        instructions="test",
        specialty_tags=frozenset({"prompt"}),
    )

    def assignment(task_id: str, label: str) -> WorkerAssignment:
        return WorkerAssignment(
            id=task_id,
            objective=(
                "Execute the task.\n\nDynamic specialist contract: " + json.dumps({"label": label})
            ),
            profile_id=profile.id,
            target_profile_id="target/one",
            technique_ids=(f"technique/{label}",),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target/one",),
            budget=AssignmentBudget(max_seconds=60),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )

    await service.submit(
        "session/one",
        (assignment("task/one", "first"), assignment("task/two", "second")),
        start_immediately=False,
        dynamic_profiles={"task/one": profile, "task/two": profile},
    )

    documents = store.schedules["schedule-1"]["graph"]["assignments"]
    identities = [item["swarm_projection"] for item in documents]
    assert [item["specialist_instance_id"] for item in identities] == [
        "task/one",
        "task/two",
    ]
    assert all(item["specialist_identity_provenance"] == "durable" for item in identities)
    assert [item["specialist_display_name"] for item in identities] == [
        "First specialist",
        "Second specialist",
    ]
    assert [_assignment(item).id for item in documents] == ["task/one", "task/two"]


async def test_unavailable_adaptive_strategy_is_terminal_after_one_durable_attempt() -> None:
    class ForbiddenProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, _request):
            self.calls += 1
            raise AssertionError("unavailable strategy must not call the provider")
            yield

    class ForbiddenTargetTools(Tools):
        def __init__(self) -> None:
            self.calls = 0

        def describe(self):
            return (
                ToolSpec(
                    name="target_request",
                    description="reviewed target exchange",
                    input_schema={"type": "object"},
                    interaction_role=ToolInteractionRole.TARGET_EXCHANGE,
                ),
            )

        def effect(self, name):
            assert name == "target_request"
            return "target_network"

        async def invoke(self, name, _context, _arguments):
            self.calls += 1
            raise AssertionError(f"unavailable strategy must not call {name}")

    store = Store()
    provider = ForbiddenProvider()
    tools = ForbiddenTargetTools()
    assignment = WorkerAssignment(
        id="campaign/unavailable/goat",
        objective="Run the unavailable reviewed strategy.",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("technique/goat",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_turns=8, max_tokens=10_000, max_seconds=30),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        strategy_id="goat",
        semantic_technique_id="semantic:goat",
        strategy_contract=(
            ("strategy_execution_mode", "unavailable"),
            ("strategy_execution_reason", "reviewed GOAT contract is absent"),
        ),
    )
    service = DurableAgentService(provider, tools, store, model="test")

    live = await service.submit("session", (assignment,), max_workers=1, minimum_workers=1)
    assert live.execution is not None
    await live.execution

    attempts = [
        item
        for item in store.attempts.values()
        if item.node_id == "worker:campaign/unavailable/goat"
    ]
    assert provider.calls == 0
    assert tools.calls == 0
    assert len(attempts) == 1
    assert attempts[0].state == "failed"
    assert attempts[0].failure_kind == RetryFailureKind.CONFIGURATION.value
    assert store.nodes[live.id]["worker:campaign/unavailable/goat"]["state"] == "failed"
    assert (await service.list_schedule_tasks(live.id))[0].status == "failed"


async def test_completed_degraded_execution_persists_structured_uncertainty() -> None:
    class DegradedProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, _request):
            self.calls += 1
            if self.calls == 1:
                yield ToolCall(
                    call_id="target-1",
                    name="custom_exchange",
                    arguments={"input": "authorized probe"},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield TextDelta(text='{"result":"observed"}')
            yield ModelCompleted()

    class DegradedTools(Tools):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def describe(self):
            return (
                ToolSpec(
                    name="custom_exchange",
                    description="reviewed target exchange",
                    input_schema={"type": "object"},
                    interaction_role=ToolInteractionRole.TARGET_EXCHANGE,
                ),
            )

        def effect(self, name):
            assert name == "custom_exchange"
            return "target_network"

        async def invoke(self, name, _context, _arguments):
            self.calls.append(name)
            return ToolResult(
                content='{"response":"observed"}',
                metadata={"target_session_id": "api:degraded"},
            )

    store = Store()
    provider = DegradedProvider()
    tools = DegradedTools()
    assignment = WorkerAssignment(
        id="campaign/degraded/goat",
        objective="Run one degraded observation.",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("technique/goat",),
        guidance_ids=(),
        tool_ids=("custom_exchange",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_turns=8, max_tokens=10_000, max_seconds=30),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        strategy_id="goat",
        semantic_technique_id="semantic:goat",
        strategy_contract=(
            ("strategy_execution_mode", "degraded"),
            ("strategy_execution_reason", "native callbacks unavailable"),
        ),
    )
    service = DurableAgentService(provider, tools, store, model="test")

    live = await service.submit("session", (assignment,), max_workers=1, minimum_workers=1)
    assert live.execution is not None
    await live.execution

    assert store.results.get(live.id), (
        store.nodes[live.id],
        store.list_attempts(live.id),
    )
    worker_attempt = next(
        item for item in store.attempts.values() if item.node_id == "worker:campaign/degraded/goat"
    )
    assert worker_attempt.state == "completed", vars(worker_attempt)
    assert "worker:campaign/degraded/goat" in store.results[live.id], (
        store.results[live.id],
        store.nodes[live.id],
        store.list_attempts(live.id),
    )
    payload = store.results[live.id]["worker:campaign/degraded/goat"]["payload"]
    expected = {
        "kind": "evaluation_uncertain",
        "source": "runtime.strategy_execution",
        "reason": "native callbacks unavailable",
        "confidence": 1.0,
        "affects": ["execution", "evaluation"],
    }
    assert tools.calls == ["custom_exchange"]
    assert payload["structured"]["strategy_execution"] == {
        "requested_controller": "goat",
        "actual_controller": "direct",
        "execution_mode": "degraded",
        "reason": "native callbacks unavailable",
    }
    assert payload["structured"]["evaluation_uncertainties"] == [expected]
    assert payload["evaluation_uncertainties"] == (expected,)
    assert payload["worker_checkpoint"]["evaluation_uncertainties"] == [expected]
    restarted = DurableAgentService(DegradedProvider(), DegradedTools(), store, model="test")
    restored_task = next(
        task for task in await restarted.list_schedule_tasks(live.id) if task.id == assignment.id
    )
    restored_payload = next(
        row["payload"]
        for row in store.restore(live.id)["results"]
        if row["node_id"] == "worker:campaign/degraded/goat"
    )
    assert restored_task.status == "completed"
    assert restored_payload["structured"]["strategy_execution"]["execution_mode"] == ("degraded")
    assert restored_payload["worker_checkpoint"]["evaluation_uncertainties"] == [expected]


async def test_late_completed_result_is_discarded_after_durable_cancellation() -> None:
    class StaleLeaseError(RuntimeError):
        pass

    class CancelledStore(Store):
        def commit_reducer(self, *args, **kwargs):
            del args, kwargs
            raise StaleLeaseError("attempt does not own the current lease generation")

    store = CancelledStore()
    lease = SimpleNamespace(
        id="attempt-cancelled",
        schedule_id="schedule-1",
        node_id="worker:campaign/task",
        generation=1,
        state="cancelled",
    )
    store.attempts[lease.id] = lease
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    live = SimpleNamespace(
        id="schedule-1",
        scheduler=SimpleNamespace(checkpoint=lambda: {"cancelled": [lease.node_id]}),
    )

    digest = await service._commit(  # noqa: SLF001
        live,
        lease,
        {"content": "late provider result"},
        "schema/result",
    )

    assert digest is None


async def test_ready_retries_transition_after_concurrent_event_advance() -> None:
    class StaleLeaseError(RuntimeError):
        pass

    class RacingStore(Store):
        def transition_ready(self, schedule_id, node_ids, *, checkpoint, expected_event_count, **_):
            if not getattr(self, "_raced", False):
                self._raced = True
                # A concurrent control-plane writer (for example, a Lead cancel
                # directive) advances the event stream between ``_ready``'s
                # snapshot and the guarded transition.
                self.schedules[schedule_id]["event_count"] += 1
                raise StaleLeaseError("scheduler checkpoint base changed")
            super().transition_ready(
                schedule_id,
                node_ids,
                checkpoint=checkpoint,
                expected_event_count=expected_event_count,
            )

    store = RacingStore()
    schedule_id = store.create_schedule(
        "session",
        {"nodes": ["worker:task-1"], "assignments": {"task-1": {}}},
        {"last_tick": 0},
        ["worker:task-1"],
    )
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    live = SimpleNamespace(
        id=schedule_id,
        scheduler=SimpleNamespace(states={}, checkpoint=lambda: {"last_tick": 0}),
    )

    await service._ready(live, ["worker:task-1"])  # noqa: SLF001

    assert store.nodes[schedule_id]["worker:task-1"]["state"] == "ready"
    assert getattr(store, "_raced", False)


async def test_committed_task_result_evidence_projects_completed_target_tool_receipts() -> None:
    store = Store()
    store.results["schedule-1"] = {
        "worker:campaign/recon": {
            "node_id": "worker:campaign/recon",
            "attempt_id": "attempt-1",
            "payload_digest": "a" * 64,
            "schema_id": "schema/result",
            "payload": {
                "usage": {
                    "input_tokens": 144,
                    "output_tokens": 21,
                    "estimated_cost_usd": 0.003,
                },
                "turns_completed": 4,
                "infrastructure_failure_category": "adapter_timeout",
                "content": json.dumps(
                    {
                        "applicable_techniques": [],
                        "arm_execution_context": {
                            "surface_id": "reviewed-chat",
                            "actual_language": "ms",
                            "state_id": "stateless:call-1",
                            "evidence_refs": ["call-1"],
                        },
                    }
                ),
                "structured": {
                    "applicable_techniques": [],
                    "arm_execution_context": {
                        "surface_id": "reviewed-chat",
                        "actual_language": "ms",
                        "state_id": "stateless:call-1",
                        "evidence_refs": ["call-1"],
                    },
                },
                "worker_checkpoint": {
                    "elapsed_seconds": 1.25,
                    "messages": [
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {
                                        "name": "target_chat",
                                        "arguments": "{}",
                                    },
                                },
                                {
                                    "id": "call-unrelated",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": "{}",
                                    },
                                },
                            ],
                        },
                        {"role": "tool", "tool_call_id": "call-1", "content": "reply"},
                        {
                            "role": "tool",
                            "tool_call_id": "call-unrelated",
                            "content": "file",
                        },
                    ],
                },
            },
        }
    }
    service = DurableAgentService(Provider(), Tools(), store)

    evidence = await service.committed_task_result_evidence(
        "schedule-1",
        AgentTask(
            id="campaign/recon",
            objective="recon",
            status="completed",
            result=json.dumps(
                {
                    "applicable_techniques": [],
                    "arm_execution_context": {
                        "surface_id": "reviewed-chat",
                        "actual_language": "ms",
                        "state_id": "stateless:call-1",
                        "evidence_refs": ["call-1"],
                    },
                }
            ),
        ),
    )

    assert evidence is not None
    assert evidence["target_call_ids"] == ("call-1",)
    assert evidence["target_call_documents"] == {"call-1": "reply"}
    assert evidence["acknowledged_tool_calls"] == {
        "call-1": "target_chat",
        "call-unrelated": "read_file",
    }
    assert evidence["attempt_id"] == "attempt-1"
    assert evidence["usage"] == {
        "input_tokens": 144,
        "output_tokens": 21,
        "estimated_cost_usd": 0.003,
    }
    assert evidence["model_steps"] == 4
    assert evidence["model_cost_usd"] == 0.003
    assert evidence["latency_ms"] == 1250
    assert evidence["infrastructure_failure_category"] == "adapter_timeout"
    assert evidence["structured_result"] == {
        "applicable_techniques": [],
        "arm_execution_context": {
            "surface_id": "reviewed-chat",
            "actual_language": "ms",
            "state_id": "stateless:call-1",
            "evidence_refs": ["call-1"],
        },
    }
    assert evidence["arm_execution_context"] == {
        "surface_id": "reviewed-chat",
        "actual_language": "ms",
        "state_id": "stateless:call-1",
        "evidence_refs": ("call-1",),
    }
    assert str(evidence["reference"]).startswith("merlin-result://schedule/schedule-1")


@pytest.mark.parametrize(
    ("projection", "count", "overflow"),
    (
        ("lineage", 32, False),
        ("lineage", 33, True),
        ("effects", 256, False),
        ("effects", 257, True),
    ),
)
async def test_committed_evidence_fails_closed_on_bounded_effect_projection_overflow(
    projection: str,
    count: int,
    overflow: bool,
) -> None:
    attempt_id = "attempt-current"

    class ProjectionStore(Store):
        def replacement_attempt_lineage(self, loaded_attempt_id, *, limit=None):
            assert loaded_attempt_id == attempt_id
            values = (
                tuple(f"attempt-ancestor-{index}" for index in range(count - 1)) + (attempt_id,)
                if projection == "lineage"
                else (attempt_id,)
            )
            return values if limit is None else values[-limit:]

    class ProjectionTools(Tools):
        async def list_effects_for_attempt(self, loaded_attempt_id, *, limit=None):
            if projection == "effects":
                assert loaded_attempt_id == attempt_id
            values = tuple(
                SimpleNamespace() for _ in range(count if projection == "effects" else 0)
            )
            return values if limit is None else values[-limit:]

    store = ProjectionStore()
    store.results["schedule-1"] = {
        "worker:campaign/overflow": {
            "node_id": "worker:campaign/overflow",
            "attempt_id": attempt_id,
            "payload_digest": "a" * 64,
            "schema_id": "schema/result",
            "payload": {
                "content": "completed worker result",
                "worker_checkpoint": {"messages": []},
            },
        }
    }
    service = DurableAgentService(Provider(), ProjectionTools(), store)

    evidence = await service.committed_task_result_evidence(
        "schedule-1",
        AgentTask(
            id="campaign/overflow",
            objective="exercise bounded durable evidence",
            status="completed",
            result="completed worker result",
        ),
    )

    assert evidence is not None
    if not overflow:
        assert str(evidence["digest"]).startswith("sha256:")
        assert str(evidence["reference"]).startswith("merlin-result://")
        assert "durable_effect_projection_error" not in evidence
        return
    expected_code = (
        "replacement_lineage_overflow" if projection == "lineage" else "effect_history_overflow"
    )
    assert evidence["durable_effect_projection_error"] == {
        "schema": "merlin.durable-effect-projection-error.v1",
        "code": expected_code,
        "retryable": False,
    }
    assert "digest" not in evidence
    assert "reference" not in evidence
    assert evidence["attempt_disposition"] == "uncertain"
    assert evidence["outcome_authority"] == "none"
    assert evidence["target_authoritative_outcome"] == "unclear"
    assert evidence["infrastructure_failure_category"] == "effect_reconciliation_required"


async def test_committed_task_evidence_selects_exact_final_schema_result() -> None:
    from merlin_agents.capability_probe_execution import probe_result_from_committed_receipt
    from merlin_agents.capability_probes import ProbeOutcome, plan_capability_probes

    probe = plan_capability_probes().probes[0]
    final_result = "```json\nmodel display text\n```"
    technique_attempt_id = "technique-attempt-probe"

    effects = (
        SimpleNamespace(
            call_id="upload-1",
            tool_name="browser_upload",
            state="acknowledged",
            agent_id="campaign/probe",
            attempt_id="attempt-final",
            result_is_error=False,
            result_metadata={
                "technique_attempt_id": technique_attempt_id,
                "interaction_role": ToolInteractionRole.AUXILIARY.value,
            },
        ),
        SimpleNamespace(
            call_id="target-1",
            tool_name="target_chat",
            state="acknowledged",
            agent_id="campaign/probe",
            attempt_id="attempt-final",
            result_is_error=False,
            result_metadata={
                "technique_attempt_id": technique_attempt_id,
                "interaction_role": ToolInteractionRole.TARGET_EXCHANGE.value,
            },
        ),
    )

    class EvidenceTools(Tools):
        async def list_effects_for_attempt(self, attempt_id, *, limit=None):
            assert attempt_id == "attempt-final"
            return effects if limit is None else effects[-limit:]

    class HistoricalStore(Store):
        def list_results(self, schedule_id):
            assert schedule_id == "schedule-1"
            return [
                {
                    "node_id": "worker:campaign/probe",
                    "attempt_id": "attempt-partial",
                    "schema_id": "schema/partial-result",
                    "payload_digest": "a" * 64,
                    "payload": {"content": final_result},
                },
                {
                    "node_id": "worker:campaign/probe",
                    "attempt_id": "attempt-old-final",
                    "schema_id": "schema/result",
                    "payload_digest": "b" * 64,
                    "payload": {"content": '{"probe_id":"wrong"}'},
                },
                {
                    "node_id": "worker:campaign/probe",
                    "attempt_id": "attempt-final",
                    "schema_id": "schema/result",
                    "payload_digest": "c" * 64,
                    "payload": {
                        "content": final_result,
                        "structured": {
                            "probe_id": probe.id,
                            "transport_available": True,
                            "accepted": True,
                            "gracefully_rejected": False,
                            "summary": "target returned the uploaded nonce",
                            "evidence_ids": ["target-1", "upload-1"],
                        },
                        "worker_checkpoint": {
                            "technique_attempt_id": technique_attempt_id,
                            "messages": [
                                {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "upload-1",
                                            "function": {
                                                "name": "browser_upload",
                                                "arguments": "{}",
                                            },
                                        },
                                        {
                                            "id": "target-1",
                                            "function": {
                                                "name": "target_chat",
                                                "arguments": "{}",
                                            },
                                        },
                                    ],
                                },
                                {"role": "tool", "tool_call_id": "upload-1", "content": "ok"},
                                {
                                    "role": "tool",
                                    "tool_call_id": "target-1",
                                    "content": "nonce",
                                },
                            ],
                        },
                    },
                },
            ]

    service = DurableAgentService(Provider(), EvidenceTools(), HistoricalStore())

    task = AgentTask(
        id="campaign/probe",
        objective="probe document upload",
        status="completed",
        result=final_result,
        technique_ids=(probe.id,),
    )
    evidence = await service.committed_task_result_evidence(
        "schedule-1",
        task,
    )

    assert evidence is not None
    assert evidence["attempt_id"] == "attempt-final"
    assert evidence["structured_result"] == {
        "probe_id": probe.id,
        "transport_available": True,
        "accepted": True,
        "gracefully_rejected": False,
        "summary": "target returned the uploaded nonce",
        "evidence_ids": ["target-1", "upload-1"],
    }
    assert evidence["probe_effect_calls"] == {
        "target-1": "target_chat",
        "upload-1": "browser_upload",
    }
    assert evidence["target_call_ids"] == ("target-1",)
    result = probe_result_from_committed_receipt(probe, task, evidence)
    assert result.outcome is ProbeOutcome.OBSERVED


async def test_committed_evidence_accepts_authenticated_compacted_target_receipt() -> None:
    task_id = "campaign/recon"
    attempt_id = "attempt-1"
    arguments = {"prompt": "x"}
    content = '{"response":"observed"}'
    receipt_body = {
        "call_id": "call-compacted",
        "tool_name": "target_request",
        "reference": "target-call:call-compacted",
        "campaign_id": "campaign/recon",
        "session_id": "session-1",
        "assignment_id": task_id,
        "attempt_id": attempt_id,
        "technique_attempt_id": "technique-attempt-1",
        "target_id": "target-1",
        "target_session_id": "target-session-1",
        "request_sha256": WorkerRuntime._action_fingerprint(  # noqa: SLF001
            "target_request", arguments
        ),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "content_bytes": len(content.encode()),
        "metadata_sha256": "3" * 64,
        "is_error": False,
        "acknowledged": True,
    }
    receipt = {
        **receipt_body,
        "receipt_sha256": WorkerRuntime._json_fingerprint(receipt_body),  # noqa: SLF001
    }
    store = Store()
    store.results["schedule-1"] = {
        "worker:campaign/recon": {
            "node_id": "worker:campaign/recon",
            "attempt_id": attempt_id,
            "payload_digest": "a" * 64,
            "schema_id": "schema/result",
            "payload": {
                "content": '{"applicable_techniques":[]}',
                "target_id": "target-1",
                "target_session_id": "target-session-1",
                "worker_checkpoint": {
                    "messages": [
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call-compacted",
                                    "function": {
                                        "name": "target_request",
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "call-compacted",
                            "content": json.dumps(
                                {
                                    "call_id": "call-compacted",
                                    "content_bytes": len(content.encode()),
                                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                                    "head": content,
                                    "name": "target_request",
                                    "tail": "",
                                    "type": "tool_result_receipt",
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        },
                    ],
                    "technique_attempt_id": "technique-attempt-1",
                    "target_evidence": [receipt],
                },
            },
        }
    }
    store.schedules["schedule-1"] = {"session_id": "session-1"}
    service = DurableAgentService(Provider(), Tools(), store)

    evidence = await service.committed_task_result_evidence(
        "schedule-1",
        AgentTask(
            id=task_id,
            objective="recon",
            status="completed",
            result='{"applicable_techniques":[]}',
        ),
    )

    assert evidence is not None
    assert evidence["target_call_ids"] == ("call-compacted",)
    assert evidence["target_call_documents"] == {}
    assert evidence["target_evidence_receipts"] == {"call-compacted": receipt}

    store.schedules.pop("schedule-1")
    unbound = await service.committed_task_result_evidence(
        "schedule-1",
        AgentTask(
            id=task_id,
            objective="recon",
            status="completed",
            result='{"applicable_techniques":[]}',
        ),
    )
    assert unbound is not None
    assert unbound["target_evidence_receipts"] == {}
    store.schedules["schedule-1"] = {"session_id": "session-1"}

    sentinel_document = {
        "call_id": "call-compacted",
        "content_bytes": 123,
        "content_sha256": "4" * 64,
        "head": "target-authored sentinel-like JSON",
        "name": "target_request",
        "tail": "",
        "type": "tool_result_receipt",
    }
    sentinel_content = json.dumps(sentinel_document, sort_keys=True, separators=(",", ":"))
    sentinel_body = {
        **receipt_body,
        "content_sha256": hashlib.sha256(sentinel_content.encode()).hexdigest(),
        "content_bytes": len(sentinel_content.encode()),
    }
    sentinel_receipt = {
        **sentinel_body,
        "receipt_sha256": WorkerRuntime._json_fingerprint(sentinel_body),  # noqa: SLF001
    }
    checkpoint = store.results["schedule-1"]["worker:campaign/recon"]["payload"][
        "worker_checkpoint"
    ]
    checkpoint["messages"][1]["content"] = sentinel_content
    checkpoint["target_evidence"] = [sentinel_receipt]

    sentinel_evidence = await service.committed_task_result_evidence(
        "schedule-1",
        AgentTask(
            id=task_id,
            objective="recon",
            status="completed",
            result='{"applicable_techniques":[]}',
        ),
    )

    assert sentinel_evidence is not None
    assert sentinel_evidence["target_call_documents"] == {"call-compacted": sentinel_document}


def test_authenticated_target_receipt_survives_runtime_guidance_in_transcript() -> None:
    task = AgentTask(id="campaign/probe", objective="probe", status="completed")
    attempt_id = "attempt-1"
    call_id = "call-1"
    arguments = {"input": "hello"}
    content = '"target reply"'
    receipt_body = {
        "call_id": call_id,
        "tool_name": "target_request",
        "reference": f"target-call:{call_id}",
        "campaign_id": "campaign/probe",
        "session_id": "session-1",
        "assignment_id": task.id,
        "attempt_id": attempt_id,
        "technique_attempt_id": None,
        "target_id": "target-1",
        "target_session_id": "target-session-1",
        "request_sha256": WorkerRuntime._action_fingerprint(  # noqa: SLF001
            "target_request", arguments
        ),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "content_bytes": len(content.encode()),
        "metadata_sha256": "3" * 64,
        "is_error": False,
        "acknowledged": True,
    }
    receipt = {
        **receipt_body,
        "receipt_sha256": WorkerRuntime._json_fingerprint(receipt_body),  # noqa: SLF001
    }
    transcript_content = (
        f"{content}\n\nThe reviewed technique has reached its maximum target exchanges. "
        "Perform no more tool actions."
    )

    admitted = _validated_target_evidence_receipts(
        [receipt],
        task=task,
        attempt_id=attempt_id,
        payload={
            "target_id": "target-1",
            "target_session_id": "target-session-1",
            "worker_checkpoint": {"technique_attempt_id": None},
        },
        request_hashes={call_id: receipt_body["request_sha256"]},
        content_hashes={call_id: hashlib.sha256(transcript_content.encode()).hexdigest()},
        content_bytes={call_id: len(transcript_content.encode())},
    )

    assert admitted == {call_id: receipt}


def test_authenticated_injection_receipt_is_admitted() -> None:
    task = AgentTask(id="campaign/injection", objective="inject", status="completed")
    attempt_id = "attempt-injection"
    call_id = "inject-1"
    arguments = {"content": "reviewed payload"}
    content = '"target reply"'
    receipt_body = {
        "call_id": call_id,
        "tool_name": "target_inject",
        "reference": f"target-call:{call_id}",
        "campaign_id": "campaign/injection",
        "session_id": "session-1",
        "assignment_id": task.id,
        "attempt_id": attempt_id,
        "technique_attempt_id": None,
        "target_id": "target-1",
        "target_session_id": "target-session-1",
        "request_sha256": WorkerRuntime._action_fingerprint(  # noqa: SLF001
            "target_inject", arguments
        ),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "content_bytes": len(content.encode()),
        "metadata_sha256": "3" * 64,
        "is_error": False,
        "acknowledged": True,
    }
    receipt = {
        **receipt_body,
        "receipt_sha256": WorkerRuntime._json_fingerprint(receipt_body),  # noqa: SLF001
    }

    admitted = _validated_target_evidence_receipts(
        [receipt],
        task=task,
        attempt_id=attempt_id,
        payload={
            "target_id": "target-1",
            "target_session_id": "target-session-1",
            "worker_checkpoint": {"technique_attempt_id": None},
        },
        request_hashes={call_id: receipt_body["request_sha256"]},
        content_hashes={call_id: hashlib.sha256(content.encode()).hexdigest()},
        content_bytes={call_id: len(content.encode())},
    )

    assert admitted == {call_id: receipt}


@pytest.mark.parametrize(
    "drifted_field", ["campaign_id", "session_id", "agent_id", "attempt_id", "target_id"]
)
async def test_compacted_target_document_requires_exact_durable_effect_attribution(
    drifted_field: str,
) -> None:
    task_id = "campaign/recon"
    attempt_id = "attempt-1"
    arguments = {"prompt": "x"}
    content = '{"reply":"objective reached"}'
    metadata = {"target_session_id": "target-session-1"}
    receipt_body = {
        "call_id": "call-compacted",
        "tool_name": "target_request",
        "reference": "target-call:call-compacted",
        "campaign_id": "campaign/recon",
        "session_id": "session-1",
        "assignment_id": task_id,
        "attempt_id": attempt_id,
        "technique_attempt_id": None,
        "target_id": "target-1",
        "target_session_id": "target-session-1",
        "request_sha256": WorkerRuntime._action_fingerprint(  # noqa: SLF001
            "target_request", arguments
        ),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "content_bytes": len(content.encode()),
        "metadata_sha256": WorkerRuntime._json_fingerprint(metadata),  # noqa: SLF001
        "is_error": False,
        "acknowledged": True,
    }
    receipt = {
        **receipt_body,
        "receipt_sha256": WorkerRuntime._json_fingerprint(receipt_body),  # noqa: SLF001
    }
    effect_values = {
        "call_id": "call-compacted",
        "tool_name": "target_request",
        "campaign_id": "campaign/recon",
        "session_id": "session-1",
        "agent_id": task_id,
        "attempt_id": attempt_id,
        "target_id": "target-1",
        "state": "acknowledged",
        "arguments_payload": arguments,
        "result_content": content,
        "result_metadata": metadata,
        "result_is_error": False,
    }
    effect_values[drifted_field] = "wrong"

    row = {
        "node_id": f"worker:{task_id}",
        "attempt_id": attempt_id,
        "payload_digest": "a" * 64,
        "payload": {
            "content": '{"applicable_techniques":[]}',
            "target_id": "target-1",
            "target_session_id": "target-session-1",
            "worker_checkpoint": {"messages": [], "target_evidence": [receipt]},
        },
    }
    evidence = DurableAgentService._committed_task_evidence(  # noqa: SLF001
        "schedule-1",
        AgentTask(
            id=task_id,
            objective="recon",
            status="completed",
            result='{"applicable_techniques":[]}',
        ),
        row,
        durable_effects=(SimpleNamespace(**effect_values),),
    )

    assert evidence is not None
    assert evidence["target_call_documents"] == {}


async def test_compacted_target_document_resolves_from_durable_effect_after_restart() -> None:
    task_id = "campaign/recon"
    attempt_id = "attempt-1"
    arguments = {"prompt": "x"}
    content = '{"reply":"objective reached"}'
    delivery = {
        "requested_transform_id": "encoding/base64",
        "applied_transform_id": "encoding/base64",
        "actual_language": "en",
        "receipt_digest": "sha256:" + "d" * 64,
    }
    metadata = {
        "target_session_id": "target-session-1",
        "target_delivery_receipt": delivery,
    }
    receipt_body = {
        "call_id": "call-compacted",
        "tool_name": "target_request",
        "reference": "target-call:call-compacted",
        "campaign_id": "campaign/recon",
        "assignment_id": task_id,
        "attempt_id": attempt_id,
        "technique_attempt_id": None,
        "target_id": "target-1",
        "target_session_id": "target-session-1",
        "request_sha256": WorkerRuntime._action_fingerprint(  # noqa: SLF001
            "target_request", arguments
        ),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "content_bytes": len(content.encode()),
        "metadata_sha256": WorkerRuntime._json_fingerprint(metadata),  # noqa: SLF001
        "is_error": False,
        # Read-only objective-gate observations do not increment the interaction
        # count, so their checkpoint receipt is provisional until the durable
        # tool effect proves acknowledgement.
        "acknowledged": False,
    }
    receipt = {
        **receipt_body,
        "receipt_sha256": WorkerRuntime._json_fingerprint(receipt_body),  # noqa: SLF001
    }
    effect = SimpleNamespace(
        call_id="call-compacted",
        tool_name="target_request",
        campaign_id="campaign/recon",
        session_id="session-1",
        agent_id=task_id,
        attempt_id=attempt_id,
        target_id="target-1",
        state="acknowledged",
        arguments_payload=arguments,
        result_content=content,
        result_metadata=metadata,
        result_is_error=False,
    )

    class EvidenceTools(Tools):
        async def list_effects_for_attempt(self, value, *, limit=None):
            assert value == attempt_id
            values = (effect,)
            return values if limit is None else values[-limit:]

    store = Store()
    store.schedules["schedule-1"] = {"session_id": "session-1"}
    store.results["schedule-1"] = {
        f"worker:{task_id}": {
            "node_id": f"worker:{task_id}",
            "attempt_id": attempt_id,
            "payload_digest": "a" * 64,
            "schema_id": "schema/result",
            "payload": {
                "content": '{"applicable_techniques":[]}',
                "target_id": "target-1",
                "target_session_id": "target-session-1",
                "worker_checkpoint": {"messages": [], "target_evidence": [receipt]},
            },
        }
    }
    service = DurableAgentService(Provider(), EvidenceTools(), store)
    task = AgentTask(
        id=task_id,
        objective="recon",
        status="completed",
        result='{"applicable_techniques":[]}',
    )

    evidence = await service.committed_task_result_evidence(
        "schedule-1",
        task,
    )

    assert evidence is not None
    assert evidence["target_call_documents"] == {"call-compacted": {"reply": "objective reached"}}
    assert evidence["target_delivery_receipts"] == {"call-compacted": delivery}
    adopted_receipt = evidence["target_evidence_receipts"]["call-compacted"]
    assert adopted_receipt["session_id"] == "session-1"
    adopted_body = {key: value for key, value in adopted_receipt.items() if key != "receipt_sha256"}
    assert adopted_receipt["receipt_sha256"] == WorkerRuntime._json_fingerprint(  # noqa: SLF001
        adopted_body
    )

    unmatched = DurableAgentService._committed_task_evidence(  # noqa: SLF001
        "schedule-1",
        task,
        store.results["schedule-1"][f"worker:{task_id}"],
    )
    assert unmatched is not None
    assert unmatched["target_call_ids"] == ()
    assert unmatched["target_evidence_receipts"] == {}


@pytest.mark.parametrize(
    ("effect_role", "credited"),
    (
        (ToolInteractionRole.TARGET_EXCHANGE.value, True),
        (ToolInteractionRole.AUXILIARY.value, False),
    ),
)
def test_custom_typed_target_exchange_requires_matching_durable_effect_role(
    effect_role: str,
    credited: bool,
) -> None:
    task_id = "campaign/custom/worker"
    attempt_id = "attempt-custom"
    call = ToolCall(
        call_id="custom-call",
        name="custom_exchange",
        arguments={"input": "authorized probe"},
    )
    metadata = {
        "interaction_role": ToolInteractionRole.TARGET_EXCHANGE.value,
        "target_session_id": "target-session-custom",
        "target_exchanges": [
            {
                "interaction": True,
                "cleanup": False,
                "request_id": "custom-request",
                "status_code": 200,
            }
        ],
    }
    result = ToolResult(content='{"reply":"target observation"}', metadata=metadata)
    target_evidence: dict[str, dict[str, object]] = {}
    WorkerRuntime._record_target_evidence(  # noqa: SLF001
        target_evidence,
        SimpleNamespace(
            session_id="session-custom",
            assignment=SimpleNamespace(
                id=task_id,
                target_profile_id="target-custom",
                technique_ids=(),
            ),
            worker_context=None,
            attempt_id=attempt_id,
            technique_attempt_id=None,
            candidate_identity=None,
        ),
        call,
        result,
    )
    receipt = target_evidence[call.call_id]
    row = {
        "node_id": f"worker:{task_id}",
        "attempt_id": attempt_id,
        "payload_digest": "a" * 64,
        "payload": {
            "content": result.content,
            "target_id": "target-custom",
            "target_session_id": "target-session-custom",
            "worker_checkpoint": {
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": call.call_id,
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments),
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": result.content,
                    },
                ],
                "technique_attempt_id": None,
                "target_evidence": [receipt],
            },
        },
    }
    effect = SimpleNamespace(
        call_id=call.call_id,
        tool_name=call.name,
        campaign_id="campaign/custom",
        session_id="session-custom",
        agent_id=task_id,
        attempt_id=attempt_id,
        target_id="target-custom",
        state="acknowledged",
        effect="target_network",
        arguments_payload=call.arguments,
        result_content=result.content,
        result_metadata={**metadata, "interaction_role": effect_role},
        result_is_error=False,
    )

    evidence = DurableAgentService._committed_task_evidence(  # noqa: SLF001
        "schedule-custom",
        AgentTask(
            id=task_id,
            objective="exercise the custom target transport",
            status="completed",
            result=result.content,
        ),
        row,
        durable_effects=(effect,),
        evidence_attempt_ids=(attempt_id,),
    )

    assert evidence is not None
    assert evidence["target_call_ids"] == ((call.call_id,) if credited else ())
    assert bool(evidence["target_evidence_receipts"]) is credited


async def test_preupgrade_error_transition_receipt_is_adopted_after_restart_without_replay() -> (
    None
):
    task_id = "campaign/legacy-transition/worker"
    attempt_id = "attempt-legacy-transition"
    call_id = "call-legacy-transition"
    arguments = {"prompt": "authorized probe"}
    content = "reviewed controls changed before delivery"
    transition = {
        "schema": "merlin.target-ui-transition.v1",
        "material": True,
        "adapter_stale": True,
        "before_digest": "sha256:" + hashlib.sha256(b"legacy-before").hexdigest(),
        "after_digest": "sha256:" + hashlib.sha256(b"legacy-after").hexdigest(),
        "changed_dimensions": ["controls"],
        "added_controls": [{"role": "button", "name": "Continue"}],
        "removed_controls": [],
        "controls_truncated": False,
        "evidence_refs": ["browser:legacy-after"],
        "reobservation_recommended": True,
    }
    metadata = {
        "target_session_id": "browser:legacy-context",
        "target_effect_attempted": False,
        "target_call_budget_charge": 0,
        "limit_interaction_count": 0,
        "target_exchanges": [],
        "target_ui_transition": transition,
    }
    legacy_body = {
        "call_id": call_id,
        "tool_name": "target_request",
        "reference": f"target-call:{call_id}",
        "campaign_id": "campaign/legacy-transition",
        "assignment_id": task_id,
        "attempt_id": attempt_id,
        "technique_attempt_id": None,
        "target_id": "target-1",
        "target_session_id": "browser:legacy-context",
        "request_sha256": WorkerRuntime._action_fingerprint(  # noqa: SLF001
            "target_request", arguments
        ),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "content_bytes": len(content.encode()),
        "metadata_sha256": WorkerRuntime._json_fingerprint(  # noqa: SLF001
            WorkerRuntime._semantic_value(metadata)  # noqa: SLF001
        ),
        "is_error": True,
        "acknowledged": False,
        "target_effect_attempted": False,
        "interaction_count": 0,
        "limit_interaction_count": 0,
        "target_call_budget_charge": 0,
        "attempt_disposition": "infrastructure_failed",
        "target_ui_transition": transition,
    }
    legacy_receipt = {
        **legacy_body,
        "receipt_sha256": WorkerRuntime._json_fingerprint(legacy_body),  # noqa: SLF001
    }
    effect = SimpleNamespace(
        call_id=call_id,
        tool_name="target_request",
        campaign_id="campaign/legacy-transition",
        session_id="session-1",
        agent_id=task_id,
        attempt_id=attempt_id,
        target_id="target-1",
        state="acknowledged",
        effect="target_network",
        arguments_payload=arguments,
        result_content=content,
        result_metadata=metadata,
        result_is_error=True,
    )

    class NoReplayProvider(Provider):
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            del request
            self.calls += 1
            raise AssertionError("legacy receipt adoption must not replay the provider")
            yield  # pragma: no cover

    class EvidenceTools(Tools):
        def __init__(self) -> None:
            self.invocations = 0

        async def invoke(self, name, context, values):
            del name, context, values
            self.invocations += 1
            raise AssertionError("legacy receipt adoption must not replay the target")

        async def list_effects_for_attempt(self, value, *, limit=None):
            assert value == attempt_id
            effects = (effect,)
            return effects if limit is None else effects[-limit:]

    row = {
        "node_id": f"worker:{task_id}",
        "attempt_id": attempt_id,
        "payload_digest": "a" * 64,
        "payload": {
            "content": content,
            "target_id": "target-1",
            "target_session_id": "browser:legacy-context",
            "worker_checkpoint": {
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "function": {
                                    "name": "target_request",
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": call_id, "content": content},
                ],
                "technique_attempt_id": None,
                "target_evidence": [legacy_receipt],
            },
        },
    }
    store = Store()
    store.schedules["schedule-legacy"] = {"session_id": "session-1"}
    provider = NoReplayProvider()
    tools = EvidenceTools()
    service = DurableAgentService(provider, tools, store)
    task = AgentTask(id=task_id, objective="probe", status="failed", result=content)

    evidence = await service._committed_task_evidence_with_authority(  # noqa: SLF001
        "schedule-legacy", task, row
    )

    assert evidence is not None
    adopted = evidence["target_ui_transition_receipts"][call_id]
    assert adopted["session_id"] == "session-1"
    assert provider.calls == 0
    assert tools.invocations == 0


def test_transition_receipts_require_complete_call_pair_and_canonical_payload() -> None:
    task = AgentTask(
        id="campaign/one/surface-drift",
        objective="observe target surface drift",
        status="failed",
        result="reviewed surface changed",
    )
    call_id = "surface-drift"
    attempt_id = "attempt-surface-drift"
    arguments = {"prompt": "authorized probe"}
    content = task.result or ""
    transition = {
        "schema": "merlin.target-ui-transition.v1",
        "material": True,
        "adapter_stale": True,
        "before_digest": "sha256:" + hashlib.sha256(b"before").hexdigest(),
        "after_digest": "sha256:" + hashlib.sha256(b"after").hexdigest(),
        "changed_dimensions": ["controls"],
        "added_controls": [{"role": "button", "name": "Continue"}],
        "removed_controls": [],
        "controls_truncated": False,
        "evidence_refs": ["browser:after"],
        "reobservation_recommended": True,
    }

    def effect_record(
        *,
        projected_transition: dict[str, object],
        effect_content: str = content,
        effect_call_id: str = call_id,
        effect_tool: str = "browser_chat_exchange",
        effect_is_error: bool = True,
        effect_kind: str = "target_network",
        effect_session: str = "browser:context-1",
    ) -> SimpleNamespace:
        interaction_count = 0 if effect_is_error else 1
        return SimpleNamespace(
            call_id=effect_call_id,
            state="acknowledged",
            session_id="session-1",
            agent_id=task.id,
            attempt_id=attempt_id,
            campaign_id="campaign/one",
            target_id="target-1",
            tool_name=effect_tool,
            effect=effect_kind,
            arguments_payload=arguments,
            result_content=effect_content,
            result_is_error=effect_is_error,
            result_metadata={
                "interaction_role": ToolInteractionRole.TARGET_EXCHANGE.value,
                "target_session_id": effect_session,
                "target_exchanges": [
                    {"interaction": True, "cleanup": False, "status_code": 200}
                    for _ in range(interaction_count)
                ],
                "target_effect_attempted": not effect_is_error,
                "target_call_budget_charge": interaction_count,
                "limit_interaction_count": interaction_count,
                "target_ui_transition": projected_transition,
            },
        )

    def committed(
        *,
        messages: list[dict[str, object]],
        projected_transition: dict[str, object],
        effect_transition: dict[str, object] | None = None,
        effect_tool: str = "browser_chat_exchange",
        effect_is_error: bool = True,
        effect_kind: str = "target_network",
        effect_session: str = "browser:context-1",
        receipt_overrides: Mapping[str, object] | None = None,
        extra_receipts: tuple[Mapping[str, object], ...] = (),
        extra_effects: tuple[SimpleNamespace, ...] = (),
        legacy_session: bool = False,
        expected_campaign_session: str = "session-1",
    ) -> Mapping[str, object]:
        receipt_metadata = effect_record(
            projected_transition=projected_transition,
            effect_is_error=effect_is_error,
            effect_kind=effect_kind,
            effect_session=effect_session,
        ).result_metadata
        interaction_count = 0 if effect_is_error else 1
        receipt_body = {
            "call_id": call_id,
            "tool_name": "browser_chat_exchange",
            "reference": f"target-call:{call_id}",
            "campaign_id": "campaign/one",
            "session_id": "session-1",
            "assignment_id": task.id,
            "attempt_id": attempt_id,
            "technique_attempt_id": None,
            "target_id": "target-1",
            "target_session_id": "browser:context-1",
            "request_sha256": WorkerRuntime._action_fingerprint(  # noqa: SLF001
                "browser_chat_exchange", arguments
            ),
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "content_bytes": len(content.encode()),
            "metadata_sha256": WorkerRuntime._json_fingerprint(  # noqa: SLF001
                WorkerRuntime._semantic_value(receipt_metadata)  # noqa: SLF001
            ),
            "is_error": effect_is_error,
            "acknowledged": not effect_is_error,
            "target_effect_attempted": not effect_is_error,
            "interaction_count": interaction_count,
            "limit_interaction_count": interaction_count,
            "target_call_budget_charge": interaction_count,
            "attempt_disposition": "infrastructure_failed" if effect_is_error else "valid",
            "target_ui_transition": projected_transition,
            **(
                {
                    "candidate_outcome_projection": project_candidate_outcome(
                        candidate_id=task.id,
                        attempt_disposition="valid",
                        outcome_authority="none",
                        outcome="unclear",
                        evidence_digest=hashlib.sha256(content.encode()).hexdigest(),
                    )
                }
                if not effect_is_error
                else {}
            ),
            **dict(receipt_overrides or {}),
        }
        if legacy_session:
            receipt_body.pop("session_id")
        receipt = {
            **receipt_body,
            "receipt_sha256": WorkerRuntime._json_fingerprint(receipt_body),  # noqa: SLF001
        }
        evidence = DurableAgentService._committed_task_evidence(  # noqa: SLF001
            "schedule-1",
            task,
            {
                "node_id": f"worker:{task.id}",
                "attempt_id": attempt_id,
                "payload_digest": "a" * 64,
                "payload": {
                    "content": content,
                    "target_id": "target-1",
                    "target_session_id": "browser:context-1",
                    "worker_checkpoint": {
                        "messages": messages,
                        "technique_attempt_id": None,
                        "target_evidence": [receipt, *extra_receipts],
                    },
                },
            },
            durable_effects=(
                effect_record(
                    projected_transition=(effect_transition or projected_transition),
                    effect_tool=effect_tool,
                    effect_is_error=effect_is_error,
                    effect_kind=effect_kind,
                    effect_session=effect_session,
                ),
                *extra_effects,
            ),
            expected_session_id=expected_campaign_session,
        )
        assert evidence is not None
        return evidence

    complete_messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "function": {
                        "name": "browser_chat_exchange",
                        "arguments": json.dumps(arguments),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]

    admitted = committed(messages=complete_messages, projected_transition=transition)
    assert tuple(admitted["target_ui_transition_receipts"]) == (call_id,)

    legacy_admitted = committed(
        messages=complete_messages,
        projected_transition=transition,
        legacy_session=True,
    )
    legacy_receipt = legacy_admitted["target_ui_transition_receipts"][call_id]
    assert legacy_receipt["session_id"] == "session-1"
    legacy_wrong_schedule = committed(
        messages=complete_messages,
        projected_transition=transition,
        legacy_session=True,
        expected_campaign_session="other-session",
    )
    assert legacy_wrong_schedule["target_ui_transition_receipts"] == {}
    legacy_ambiguous_effect = committed(
        messages=complete_messages,
        projected_transition=transition,
        legacy_session=True,
        extra_effects=(effect_record(projected_transition=transition),),
    )
    assert legacy_ambiguous_effect["target_ui_transition_receipts"] == {}

    duplicate_receipt = committed(
        messages=complete_messages,
        projected_transition=transition,
        extra_receipts=({"call_id": call_id},),
    )
    assert duplicate_receipt["target_ui_transition_receipts"] == {}

    for field in ("attempt_id", "acknowledged"):
        malformed_receipt = committed(
            messages=complete_messages,
            projected_transition=transition,
            receipt_overrides={field: ["not-scalar"]},
        )
        assert malformed_receipt["target_ui_transition_receipts"] == {}

    successful = committed(
        messages=complete_messages,
        projected_transition=transition,
        effect_is_error=False,
    )
    assert tuple(successful["target_evidence_receipts"]) == (call_id,)

    rewritten_success = committed(
        messages=complete_messages,
        projected_transition={
            **transition,
            "after_digest": "sha256:" + hashlib.sha256(b"forged-success").hexdigest(),
        },
        effect_transition=transition,
        effect_is_error=False,
    )
    assert rewritten_success["target_evidence_receipts"] == {}

    wrong_session_success = committed(
        messages=complete_messages,
        projected_transition=transition,
        effect_is_error=False,
        effect_session="browser:other-context",
    )
    assert wrong_session_success["target_evidence_receipts"] == {}

    wrong_campaign_session_success = committed(
        messages=complete_messages,
        projected_transition=transition,
        effect_is_error=False,
        receipt_overrides={"session_id": "other-session"},
    )
    assert wrong_campaign_session_success["target_evidence_receipts"] == {}

    wrong_effect_success = committed(
        messages=complete_messages,
        projected_transition=transition,
        effect_is_error=False,
        effect_kind="filesystem_read",
    )
    assert wrong_effect_success["target_evidence_receipts"] == {}

    wrong_count_success = committed(
        messages=complete_messages,
        projected_transition=transition,
        effect_is_error=False,
        receipt_overrides={"interaction_count": 0},
    )
    assert wrong_count_success["target_evidence_receipts"] == {}

    forged_outcome_success = committed(
        messages=complete_messages,
        projected_transition=transition,
        effect_is_error=False,
        receipt_overrides={
            "outcome_authority": "target",
            "target_authoritative_outcome": "confirmed",
            "candidate_outcome_projection": project_candidate_outcome(
                candidate_id=task.id,
                attempt_disposition="valid",
                outcome_authority="target",
                outcome="confirmed",
                evidence_digest=hashlib.sha256(content.encode()).hexdigest(),
            ),
        },
    )
    assert forged_outcome_success["target_evidence_receipts"] == {}

    redirected_candidate = committed(
        messages=complete_messages,
        projected_transition=transition,
        effect_is_error=False,
        receipt_overrides={
            "candidate_outcome_projection": project_candidate_outcome(
                candidate_id="different-candidate",
                attempt_disposition="valid",
                outcome_authority="none",
                outcome="unclear",
                evidence_digest=hashlib.sha256(content.encode()).hexdigest(),
            )
        },
    )
    assert redirected_candidate["target_evidence_receipts"] == {}

    same_group_later_result = json.loads(json.dumps(complete_messages))
    same_group_later_result[0]["tool_calls"].append(
        {
            "id": "later-target-result",
            "function": {
                "name": "browser_chat_exchange",
                "arguments": json.dumps({"prompt": "later"}),
            },
        }
    )
    same_group_later_result.append(
        {
            "role": "tool",
            "tool_call_id": "later-target-result",
            "content": "later result",
        }
    )
    not_terminal = committed(
        messages=same_group_later_result,
        projected_transition=transition,
    )
    assert not_terminal["target_ui_transition_receipts"] == {}

    ambiguous_effect = committed(
        messages=complete_messages,
        projected_transition=transition,
        extra_effects=(
            effect_record(
                projected_transition=transition,
                effect_tool="target_request",
            ),
        ),
    )
    assert ambiguous_effect["target_ui_transition_receipts"] == {}

    altered_content = json.loads(json.dumps(complete_messages))
    altered_content[1]["content"] = "tampered reviewed response"
    altered = committed(messages=altered_content, projected_transition=transition)
    assert altered["target_ui_transition_receipts"] == {}

    fake_marker_content = (
        "ordinary target text "
        f"[durable checkpoint elided 3 characters; sha256:{'f' * 64}; bytes:99]"
    )
    fake_marker_receipt_body = {
        "call_id": call_id,
        "tool_name": "browser_chat_exchange",
        "reference": f"target-call:{call_id}",
        "campaign_id": "campaign/one",
        "session_id": "session-1",
        "assignment_id": task.id,
        "attempt_id": attempt_id,
        "technique_attempt_id": None,
        "target_id": "target-1",
        "target_session_id": "browser:context-1",
        "request_sha256": WorkerRuntime._action_fingerprint(  # noqa: SLF001
            "browser_chat_exchange", arguments
        ),
        "content_sha256": hashlib.sha256(fake_marker_content.encode()).hexdigest(),
        "content_bytes": len(fake_marker_content.encode()),
        "metadata_sha256": WorkerRuntime._json_fingerprint(  # noqa: SLF001
            WorkerRuntime._semantic_value(  # noqa: SLF001
                effect_record(projected_transition=transition).result_metadata
            )
        ),
        "is_error": True,
        "acknowledged": False,
        "target_effect_attempted": False,
        "interaction_count": 0,
        "limit_interaction_count": 0,
        "target_call_budget_charge": 0,
        "attempt_disposition": "infrastructure_failed",
        "target_ui_transition": transition,
    }
    fake_marker_receipt = {
        **fake_marker_receipt_body,
        "receipt_sha256": WorkerRuntime._json_fingerprint(fake_marker_receipt_body),  # noqa: SLF001
    }
    fake_marker_messages = json.loads(json.dumps(complete_messages))
    fake_marker_messages[1]["content"] = fake_marker_content
    fake_marker_evidence = DurableAgentService._committed_task_evidence(  # noqa: SLF001
        "schedule-1",
        task,
        {
            "node_id": f"worker:{task.id}",
            "attempt_id": attempt_id,
            "payload_digest": "c" * 64,
            "payload": {
                "content": content,
                "target_id": "target-1",
                "target_session_id": "browser:context-1",
                "worker_checkpoint": {
                    "messages": fake_marker_messages,
                    "technique_attempt_id": None,
                    "target_evidence": [fake_marker_receipt],
                },
            },
        },
        durable_effects=(
            effect_record(
                projected_transition=transition,
                effect_content=fake_marker_content,
            ),
        ),
    )
    assert fake_marker_evidence is not None
    assert fake_marker_evidence["target_ui_transition_receipts"] == {call_id: fake_marker_receipt}

    exact_marker_content = WorkerRuntime._durable_checkpoint_text(  # noqa: SLF001
        "z" * 20_000,
        16_384,
    )
    assert len(exact_marker_content) == 16_384
    exact_marker_receipt_body = {
        **fake_marker_receipt_body,
        "content_sha256": hashlib.sha256(exact_marker_content.encode()).hexdigest(),
        "content_bytes": len(exact_marker_content.encode()),
    }
    exact_marker_receipt = {
        **exact_marker_receipt_body,
        "receipt_sha256": WorkerRuntime._json_fingerprint(exact_marker_receipt_body),  # noqa: SLF001
    }
    exact_marker_messages = json.loads(json.dumps(complete_messages))
    exact_marker_messages[1]["content"] = exact_marker_content
    exact_marker_evidence = DurableAgentService._committed_task_evidence(  # noqa: SLF001
        "schedule-1",
        task,
        {
            "node_id": f"worker:{task.id}",
            "attempt_id": attempt_id,
            "payload_digest": "d" * 64,
            "payload": {
                "content": content,
                "target_id": "target-1",
                "target_session_id": "browser:context-1",
                "worker_checkpoint": {
                    "messages": exact_marker_messages,
                    "technique_attempt_id": None,
                    "target_evidence": [exact_marker_receipt],
                },
            },
        },
        durable_effects=(
            effect_record(
                projected_transition=transition,
                effect_content=exact_marker_content,
            ),
        ),
    )
    assert exact_marker_evidence is not None
    assert exact_marker_evidence["target_ui_transition_receipts"] == {call_id: exact_marker_receipt}

    elided_original = (
        content
        + f"[durable checkpoint elided 5 characters; sha256:{'e' * 64}; bytes:77]"
        + "x" * 20_000
    )
    elided_content = WorkerRuntime._durable_checkpoint_text(  # noqa: SLF001
        elided_original,
        16_384,
    )
    assert elided_content.count("[durable checkpoint elided") == 2
    elided_receipt_hash = hashlib.sha256(elided_original.encode()).hexdigest()
    elided_receipt_body = {
        "call_id": call_id,
        "tool_name": "browser_chat_exchange",
        "reference": f"target-call:{call_id}",
        "campaign_id": "campaign/one",
        "session_id": "session-1",
        "assignment_id": task.id,
        "attempt_id": attempt_id,
        "technique_attempt_id": None,
        "target_id": "target-1",
        "target_session_id": "browser:context-1",
        "request_sha256": WorkerRuntime._action_fingerprint(  # noqa: SLF001
            "browser_chat_exchange", arguments
        ),
        "content_sha256": elided_receipt_hash,
        "content_bytes": len(elided_original.encode()),
        "metadata_sha256": WorkerRuntime._json_fingerprint(  # noqa: SLF001
            WorkerRuntime._semantic_value(  # noqa: SLF001
                effect_record(projected_transition=transition).result_metadata
            )
        ),
        "is_error": True,
        "acknowledged": False,
        "target_effect_attempted": False,
        "interaction_count": 0,
        "limit_interaction_count": 0,
        "target_call_budget_charge": 0,
        "attempt_disposition": "infrastructure_failed",
        "target_ui_transition": transition,
    }
    elided_receipt = {
        **elided_receipt_body,
        "receipt_sha256": WorkerRuntime._json_fingerprint(elided_receipt_body),  # noqa: SLF001
    }
    elided_messages = json.loads(json.dumps(complete_messages))
    elided_messages[1]["content"] = elided_content
    elided_evidence = DurableAgentService._committed_task_evidence(  # noqa: SLF001
        "schedule-1",
        task,
        {
            "node_id": f"worker:{task.id}",
            "attempt_id": attempt_id,
            "payload_digest": "b" * 64,
            "payload": {
                "content": content,
                "target_id": "target-1",
                "target_session_id": "browser:context-1",
                "worker_checkpoint": {
                    "messages": elided_messages,
                    "technique_attempt_id": None,
                    "target_evidence": [elided_receipt],
                },
            },
        },
        durable_effects=(
            effect_record(
                projected_transition=transition,
                effect_content=elided_original,
            ),
        ),
    )
    assert elided_evidence is not None
    assert elided_evidence["target_ui_transition_receipts"] == {call_id: elided_receipt}

    ghost = committed(messages=[], projected_transition=transition)
    assert ghost["target_ui_transition_receipts"] == {}

    noncanonical = committed(
        messages=complete_messages,
        projected_transition={**transition, "unbounded_extra": "x" * 10_000},
    )
    assert noncanonical["target_ui_transition_receipts"] == {}

    duplicate_pair = committed(
        messages=[*complete_messages, *json.loads(json.dumps(complete_messages))],
        projected_transition=transition,
    )
    assert duplicate_pair["target_ui_transition_receipts"] == {}

    interrupted_pair_messages = json.loads(json.dumps(complete_messages))
    interrupted_pair_messages.insert(
        1,
        {"role": "assistant", "content": "intervening provider turn"},
    )
    interrupted_pair = committed(
        messages=interrupted_pair_messages,
        projected_transition=transition,
    )
    assert interrupted_pair["target_ui_transition_receipts"] == {}

    wrong_tool_messages = json.loads(json.dumps(complete_messages))
    wrong_tool_messages[0]["tool_calls"][0]["function"]["name"] = "read_file"
    wrong_tool = committed(
        messages=wrong_tool_messages,
        projected_transition=transition,
        effect_tool="read_file",
    )
    assert wrong_tool["target_ui_transition_receipts"] == {}

    rewritten_transition = {
        **transition,
        "after_digest": "sha256:" + hashlib.sha256(b"rewritten-after").hexdigest(),
    }
    rewritten_receipt = committed(
        messages=complete_messages,
        projected_transition=rewritten_transition,
        effect_transition=transition,
    )
    assert rewritten_receipt["target_ui_transition_receipts"] == {}

    later_effect = committed(
        messages=complete_messages,
        projected_transition=transition,
        extra_effects=(
            effect_record(
                projected_transition=transition,
                effect_call_id="later-target-call",
            ),
        ),
    )
    assert later_effect["target_ui_transition_receipts"] == {}


@pytest.mark.parametrize(
    ("tool_name", "effect_kind"),
    (("browser_click", "target_network"), ("browser_navigate", "target_read")),
)
def test_generic_browser_transition_projects_to_lead_reobservation_without_attack_credit(
    tool_name: str,
    effect_kind: str,
) -> None:
    assignment_id = "campaign/generic-projection/worker"
    campaign_id = "campaign/generic-projection"
    session_id = "session-generic-projection"
    attempt_id = "attempt-generic-projection"
    target_id = "target-generic-projection"
    target_session_id = "browser:generic-projection"
    compiled_adapter_digest = "sha256:" + hashlib.sha256(b"compiled-adapter").hexdigest()
    transition = {
        "schema": "merlin.target-ui-transition.v1",
        "material": True,
        "adapter_stale": False,
        "before_digest": "sha256:" + hashlib.sha256(b"generic-before").hexdigest(),
        "after_digest": "sha256:" + hashlib.sha256(b"generic-after").hexdigest(),
        "changed_dimensions": ["dialog"],
        "added_controls": [{"role": "dialog", "name": "Level complete"}],
        "removed_controls": [],
        "controls_truncated": False,
        "evidence_refs": ["browser:generic-after"],
        "reobservation_recommended": True,
    }
    arguments = (
        {"tab_ref": "tab:1", "url": "https://target.test/level-2"}
        if tool_name == "browser_navigate"
        else {"tab_ref": "tab:1", "element_ref": "button:next-level"}
    )
    call = ToolCall(call_id=f"generic-{tool_name}", name=tool_name, arguments=arguments)
    metadata = {
        "interaction_role": ToolInteractionRole.AUXILIARY.value,
        "effect": effect_kind,
        "campaign_id": campaign_id,
        "target_profile_id": target_id,
        "compiled_adapter_digest": compiled_adapter_digest,
        "target_effect_attempted": True,
        "target_call_budget_charge": 1,
        "target_session_id": target_session_id,
        "target_ui_transition": transition,
    }
    result = ToolResult(content="reviewed browser surface changed", metadata=metadata)
    target_evidence: dict[str, dict[str, object]] = {}
    WorkerRuntime._record_target_evidence(  # noqa: SLF001
        target_evidence,
        SimpleNamespace(
            session_id=session_id,
            assignment=SimpleNamespace(
                id=assignment_id,
                target_profile_id=target_id,
                technique_ids=(),
            ),
            worker_context=None,
            attempt_id=attempt_id,
            technique_attempt_id=None,
            candidate_identity=None,
        ),
        call,
        result,
    )
    receipt = target_evidence[call.call_id]
    task = AgentTask(
        id=assignment_id,
        objective="Advance the reviewed browser surface.",
        status="completed",
        result=result.content,
    )
    committed = DurableAgentService._committed_task_evidence(  # noqa: SLF001
        "schedule-generic-projection",
        task,
        {
            "node_id": f"worker:{assignment_id}",
            "attempt_id": attempt_id,
            "payload_digest": "d" * 64,
            "payload": {
                "content": result.content,
                "target_id": target_id,
                "target_session_id": target_session_id,
                "worker_checkpoint": {
                    "messages": [
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": call.call_id,
                                    "function": {
                                        "name": tool_name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "content": result.content,
                        },
                    ],
                    "technique_attempt_id": None,
                    "target_evidence": [receipt],
                },
            },
        },
        durable_effects=(
            SimpleNamespace(
                call_id=call.call_id,
                state="acknowledged",
                session_id=session_id,
                agent_id=assignment_id,
                attempt_id=attempt_id,
                campaign_id=campaign_id,
                target_id=target_id,
                tool_name=tool_name,
                effect=effect_kind,
                arguments_payload=arguments,
                result_content=result.content,
                result_is_error=False,
                result_metadata=metadata,
            ),
        ),
        evidence_attempt_ids=(attempt_id,),
    )

    assert committed is not None
    assert committed["target_call_ids"] == ()
    assert committed["target_evidence_receipts"] == {}
    assert committed["target_ui_transition_receipts"] == {call.call_id: receipt}
    obligations = extract_target_reobservation_obligations(
        campaign_id=campaign_id,
        session_id=session_id,
        target_id=target_id,
        source_schedule_id="schedule-generic-projection",
        source_wave=1,
        task_ids=(assignment_id,),
        evidence={assignment_id: committed},
    )
    assert len(obligations) == 1
    assert obligations[0].target_call_id == call.call_id
    assert obligations[0].receipt_projection == "target_ui_transition_receipts"
    projection = project_target_reobservations(
        [
            {
                "sequence": index,
                "payload": obligation.observed_payload(campaign_id, target_id),
            }
            for index, obligation in enumerate(obligations, 1)
        ]
    )
    assert projection.lead_projection()["generation"]["status"] == "ready"  # type: ignore[index]


async def test_target_admission_hold_checkpoint_restores_without_provider_or_tool_dispatch(
    monkeypatch,
) -> None:
    class HoldStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.partial_results: list[dict[str, object]] = []

        def commit_partial(self, attempt_id, generation, *, result, applied_message_ids=()):
            del applied_message_ids
            lease = self.attempts[attempt_id]
            assert lease.generation == generation
            payload = dict(result)
            self.partial_results.append(payload)
            self.results.setdefault(lease.schedule_id, {})[lease.node_id] = {
                "node_id": lease.node_id,
                "attempt_id": attempt_id,
                "generation": generation,
                "schema_id": "schema/partial-result",
                "payload_digest": "sha256:" + "d" * 64,
                "payload": payload,
            }
            return "sha256:" + "d" * 64

    assignment = WorkerAssignment(
        id="campaign/held-restart/worker",
        objective="probe the reviewed target",
        profile_id="injection",
        target_profile_id="target-held",
        technique_ids=("technique-held",),
        guidance_ids=(),
        tool_ids=("custom_exchange",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_turns=2, max_tokens=2_000, max_seconds=30),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    original_run = WorkerRuntime.run

    async def hold_before_dispatch(_runtime, handoff, _control=None, *, checkpoint=None):
        del checkpoint
        raise _TargetAdmissionHeld(
            assignment_id=handoff.assignment.id,
            attempt_id=handoff.attempt_id,
            technique_attempt_id=handoff.technique_attempt_id,
            tool_name="custom_exchange",
            call_id="call-held-restart",
            failure_code="target_session_resolution_unavailable",
            target_session_id="browser:held-restart",
        )

    monkeypatch.setattr(WorkerRuntime, "run", hold_before_dispatch)
    store = HoldStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    live = await service.submit(
        "session-held-restart",
        (assignment,),
        max_workers=1,
        minimum_workers=1,
    )
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=1)

    partial = next(item for item in store.partial_results if item.get("partial") is True)
    checkpoint = partial["worker_checkpoint"]
    assert isinstance(checkpoint, Mapping)
    marker = checkpoint["target_control_hold"]
    assert isinstance(marker, Mapping)
    assert checkpoint["target_session_id"] == "browser:held-restart"
    assert marker["assignment_id"] == assignment.id
    assert marker["call_id"] == "call-held-restart"
    assert marker["target_session_id"] == "browser:held-restart"

    class CountingProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, _request):
            self.calls += 1
            raise AssertionError("provider must not run for a restored control hold")
            yield ModelCompleted()

    class CountingTools:
        def __init__(self) -> None:
            self.calls = 0

        def describe(self):
            return ()

        async def invoke(self, _name, _context, _arguments):
            self.calls += 1
            raise AssertionError("tool must not run for a restored control hold")

    provider = CountingProvider()
    tools = CountingTools()
    restored = await original_run(
        WorkerRuntime(provider, tools),
        WorkerHandoff(
            "session-held-restart",
            ".",
            "test",
            WorkerProfile("injection", 1, "", "", frozenset()),
            assignment,
            attempt_id=str(marker["attempt_id"]),
            technique_attempt_id=(
                str(marker["technique_attempt_id"])
                if marker["technique_attempt_id"] is not None
                else None
            ),
        ),
        checkpoint=checkpoint,
    )

    assert restored.status == "cancelled"
    assert restored.error_code == "target_admission_latched"
    assert restored.target_session_id == "browser:held-restart"
    assert provider.calls == 0
    assert tools.calls == 0


def test_committed_evidence_projects_typed_target_admission_hold() -> None:
    task = AgentTask(
        id="campaign/held/worker",
        objective="probe the reviewed target",
        status="cancelled",
        result="",
    )
    row = {
        "node_id": f"worker:{task.id}",
        "attempt_id": "attempt-held",
        "payload_digest": "a" * 64,
        "schema_id": "schema/partial-result",
        "payload": {
            "content": "",
            "error_code": "target_admission_latched",
            "target_id": "target-1",
            "worker_checkpoint": {"messages": []},
        },
    }

    committed = DurableAgentService._committed_task_evidence(  # noqa: SLF001
        "schedule-held",
        task,
        row,
    )

    assert committed is not None
    assert committed["error_code"] == "target_admission_latched"
    assert "infrastructure_failure_category" not in committed


async def test_target_admission_hold_survives_terminal_settlement_projection() -> None:
    class EvidenceStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.technique_terminal: list[tuple[str, str, dict[str, object]]] = []

        def commit_partial(self, attempt_id, generation, *, result, applied_message_ids=()):
            del applied_message_ids
            lease = self.attempts[attempt_id]
            assert lease.generation == generation
            row = {
                "node_id": lease.node_id,
                "attempt_id": attempt_id,
                "generation": generation,
                "schema_id": "schema/partial-result",
                "payload_digest": "sha256:" + "d" * 64,
                "payload": dict(result),
            }
            self.results.setdefault(lease.schedule_id, {})[lease.node_id] = row
            return row["payload_digest"]

        def terminalize_technique_attempt(self, attempt_id, state, **metadata):
            self.technique_terminal.append((attempt_id, state, dict(metadata)))

    assignment = WorkerAssignment(
        id="campaign/held/worker",
        objective="probe the reviewed target",
        profile_id="injection",
        target_profile_id="target-1",
        technique_ids=("technique-1",),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_turns=2, max_tokens=2_000, max_seconds=30),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    store = EvidenceStore()
    arm_outcomes: list[Mapping[str, object]] = []
    service = DurableAgentService(
        Provider(),
        Tools(),
        store,
        model="test",
        arm_outcome_recorder=arm_outcomes.append,
    )
    live = await service.submit(
        "session-held",
        (assignment,),
        max_workers=1,
        minimum_workers=1,
        start_immediately=False,
    )
    start_handle = live.scheduler.claim("start", "scheduler", 1)
    start_lease = await service._claim(live, start_handle)  # noqa: SLF001
    live.scheduler.complete(start_handle, "start", 2)
    await service._commit(  # noqa: SLF001
        live,
        start_lease,
        {"started": True},
        "schema/envelope",
    )
    await service._ready(live, live.scheduler.ready_nodes())  # noqa: SLF001
    node_id = f"worker:{assignment.id}"
    worker_handle = live.scheduler.claim(node_id, node_id, 4)
    worker_lease = await service._claim(live, worker_handle, assignment)  # noqa: SLF001
    result = WorkerResult(
        task_id=assignment.id,
        status="cancelled",
        error="surface generation changed before delivery",
        error_code="target_admission_latched",
        retry_class="terminal",
        infrastructure_failure_category=None,
        checkpoint={"messages": []},
    )
    ticks = iter(range(5, 20))

    await service._settle_worker_outcome(  # noqa: SLF001
        live,
        (worker_handle, worker_lease, result, None, (), "technique-attempt-held"),
        lambda: next(ticks),
        _AdaptiveConcurrency(1),
    )

    projection = await service.completion_projection(live.id)
    assert projection["tasks"][0].status == "cancelled"
    committed = projection["evidence"][assignment.id]
    assert committed["error_code"] == "target_admission_latched"
    assert "infrastructure_failure_category" not in committed
    assert arm_outcomes == []
    assert [item[:2] for item in store.technique_terminal] == [
        ("technique-attempt-held", "cancelled")
    ]


@pytest.mark.parametrize("target_effect_attempted", (False, True))
async def test_terminal_surface_drift_commits_transition_without_replacement(
    target_effect_attempted: bool,
) -> None:
    transition = {
        "schema": "merlin.target-ui-transition.v1",
        "material": True,
        "adapter_stale": True,
        "before_digest": "sha256:" + hashlib.sha256(b"surface-before").hexdigest(),
        "after_digest": "sha256:" + hashlib.sha256(b"surface-after").hexdigest(),
        "changed_dimensions": ["controls"],
        "added_controls": [
            {
                "role": "button",
                "name": "Continue",
                "frame_ref": "frame-1",
                "input_type": "button",
            }
        ],
        "removed_controls": [],
        "controls_truncated": False,
        "evidence_refs": ["browser:surface-after"],
        "reobservation_recommended": True,
    }

    class PartialStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.partial_rows: list[dict[str, object]] = []
            self.retry_failed_calls = 0
            self.transition_checkpoint_failures = 0

        def commit_partial(self, attempt_id, generation, *, result, applied_message_ids=()):
            del applied_message_ids
            lease = self.attempts[attempt_id]
            assert lease.generation == generation
            payload = dict(result)
            checkpoint = payload.get("worker_checkpoint")
            transition_checkpoint = bool(
                isinstance(checkpoint, Mapping)
                and any(
                    isinstance(item, Mapping) and item.get("target_ui_transition") is not None
                    for item in checkpoint.get("target_evidence", ())
                )
            )
            if (
                transition_checkpoint
                and set(payload) == {"worker_checkpoint", "partial"}
                and self.transition_checkpoint_failures == 0
            ):
                self.transition_checkpoint_failures += 1
                raise RuntimeError("injected transition checkpoint write failure")
            payload_digest = hashlib.sha256(
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode()
            ).hexdigest()
            row = {
                "node_id": lease.node_id,
                "attempt_id": attempt_id,
                "generation": generation,
                "schema_id": "schema/partial-result",
                "payload_digest": payload_digest,
                "payload": payload,
            }
            self.partial_rows.append(row)
            self.results.setdefault(lease.schedule_id, {})[
                f"partial:{attempt_id}:{len(self.partial_rows)}"
            ] = row
            return payload_digest

        def retry_failed(self, schedule_id, node_id, *, checkpoint, expected_event_count):
            self.retry_failed_calls += 1
            return super().retry_failed(
                schedule_id,
                node_id,
                checkpoint=checkpoint,
                expected_event_count=expected_event_count,
            )

    class SurfaceProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            assert self.calls == 1, "terminal surface drift must not start a replacement worker"
            assert request.tools
            yield ToolCall(
                call_id="surface-drift",
                name="browser_chat_exchange",
                arguments={"prompt": "authorized probe"},
            )
            yield ModelCompleted(finish_reason="tool_calls")

    class EffectStore:
        def __init__(self) -> None:
            self.records = {}

        async def begin(self, record):
            existing = self.records.get(record.idempotency_key)
            if existing is not None:
                return existing, False
            self.records[record.idempotency_key] = record
            return record, True

        async def adopt_permitted(self, record, permit_digest):
            expected = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        record.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            )
            assert permit_digest == expected
            return await self.begin(record)

        async def finish(self, record):
            self.records[record.idempotency_key] = record
            return record

        async def list_for_session(self, session_id, *, tool_name=None):
            return [
                item
                for item in self.records.values()
                if item.session_id == session_id
                and (tool_name is None or item.tool_name == tool_name)
            ]

        async def list_for_attempt(self, attempt_id, *, limit=None):
            values = [item for item in self.records.values() if item.attempt_id == attempt_id]
            return values if limit is None else values[-limit:]

        async def list_for_campaign(self, campaign_id, *, limit=None):
            records = [item for item in self.records.values() if item.campaign_id == campaign_id]
            return records if limit is None else records[-limit:]

    class SurfaceTool:
        effect = "target_network"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def describe(self):
            return ToolSpec(
                name="browser_chat_exchange",
                description="Reviewed browser exchange",
                input_schema={"type": "object"},
                interaction_role=ToolInteractionRole.TARGET_EXCHANGE,
            )

        def resolve_target_session_id(self, context):
            del context
            return "browser:context-1"

        async def invoke(self, context, arguments):
            del context, arguments
            self.calls.append("browser_chat_exchange")
            interaction_count = int(target_effect_attempted)
            return ToolResult(
                content="",
                is_error=True,
                metadata={
                    **(
                        {
                            "failure_code": "target_delivery_rejected",
                            "target_delivery_rejected": True,
                            "retryable": True,
                        }
                        if not target_effect_attempted
                        else {}
                    ),
                    "target_effect_attempted": target_effect_attempted,
                    "target_call_budget_charge": interaction_count,
                    "limit_interaction_count": interaction_count,
                    "target_exchanges": (
                        [{"interaction": True, "cleanup": False}] if target_effect_attempted else []
                    ),
                    "target_session_id": "browser:context-1",
                    "target_ui_transition": transition,
                },
            )

    assignment = WorkerAssignment(
        id="campaign/surface-drift/worker",
        objective="Run one authorized browser exchange.",
        profile_id="recon",
        target_profile_id="target-surface",
        technique_ids=("surface-drift",),
        guidance_ids=(),
        tool_ids=("browser_chat_exchange",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_turns=2, max_tokens=20_000, max_seconds=30),
        max_target_exchanges=1,
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        strategy_id="direct",
    )
    profile = ContextDocument.create("profile:recon@1", "Observe the reviewed browser surface.")
    target = ReviewedTargetAdapter(
        "target-surface",
        "browser",
        "https://target.test/chat",
        "web-chat",
        authentication="ambient authenticated browser context",
    )
    context_values = {
        "assignment_id": assignment.id,
        "profile": asdict(profile),
        "guidance": [],
        "skills": [],
        "tool_ids": assignment.tool_ids,
        "target": asdict(target),
        "authority_ids": assignment.authority_ids,
        "strategy_id": "direct",
        "strategy_contract": (),
    }
    worker_context = WorkerContext(
        assignment_id=assignment.id,
        profile=profile,
        guidance=(),
        skills=(),
        tool_ids=assignment.tool_ids,
        target=target,
        authority_ids=assignment.authority_ids,
        digest=_digest(context_values),
    )
    provider = SurfaceProvider()

    async def allow(*_args):
        return True

    surface_tool = SurfaceTool()
    tools = ToolBroker(
        (surface_tool,),
        confirmer=allow,
        effect_store=EffectStore(),
    )
    store = PartialStore()
    service = DurableAgentService(provider, tools, store, model="test")

    live = await service.submit(
        "session-surface-drift",
        (assignment,),
        max_workers=1,
        minimum_workers=1,
        worker_contexts={assignment.id: worker_context},
    )
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=1)

    node_id = f"worker:{assignment.id}"
    task = (await service.list_schedule_tasks(live.id))[0]
    attempts = [attempt for attempt in store.attempts.values() if attempt.node_id == node_id]
    assert task.status == "failed"
    assert store.nodes[live.id][node_id]["state"] == "failed"
    assert live.scheduler.status(node_id).value == "failed"
    assert len(attempts) == 1
    assert attempts[0].state == "failed"
    assert store.retry_failed_calls == 0
    assert store.transition_checkpoint_failures == 1
    assert provider.calls == 1
    assert surface_tool.calls == ["browser_chat_exchange"]

    row = next(row for row in reversed(store.partial_rows) if row["payload"].get("partial"))
    payload = row["payload"]
    checkpoint = payload["worker_checkpoint"]
    (receipt,) = checkpoint["target_evidence"]
    assert row["schema_id"] == "schema/partial-result"
    assert payload["retry_failure_kind"] == RetryFailureKind.UNKNOWN.value
    assert payload["target_id"] == "target-surface"
    assert payload["target_session_id"] == "browser:context-1"
    assert payload["infrastructure_failure_category"] is None
    assert "Lead/operator re-observation" in payload["error"]
    assert "provider rejected" not in payload["error"]
    assert receipt["assignment_id"] == assignment.id
    assert receipt["attempt_id"] == row["attempt_id"]
    assert receipt["target_id"] == payload["target_id"]
    assert receipt["target_session_id"] == payload["target_session_id"]
    assert receipt["target_effect_attempted"] is target_effect_attempted
    assert receipt["target_call_budget_charge"] == int(target_effect_attempted)

    committed = DurableAgentService._committed_task_evidence(  # noqa: SLF001
        live.id,
        task,
        row,
        durable_effects=await tools.list_effects_for_attempt(row["attempt_id"]),
        evidence_attempt_ids=(row["attempt_id"],),
    )
    assert committed is not None
    assert committed["target_call_ids"] == ()
    assert committed["target_evidence_receipts"] == {}
    assert committed["target_ui_transition_receipts"] == {"surface-drift": receipt}


@pytest.mark.parametrize("transition_case", ("success", "malformed"))
async def test_crash_after_durable_material_transition_does_not_replace_worker(
    monkeypatch,
    transition_case: str,
) -> None:
    transition = {
        "schema": "merlin.target-ui-transition.v1",
        "material": True,
        "adapter_stale": False,
        "before_digest": "sha256:" + hashlib.sha256(b"before-win").hexdigest(),
        "after_digest": "sha256:" + hashlib.sha256(b"after-win").hexdigest(),
        "changed_dimensions": ["controls"],
        "added_controls": [
            {
                "role": "dialog",
                "name": "Level complete",
                "frame_ref": "main",
                "surface_scope": "modal",
            }
        ],
        "removed_controls": [],
        "controls_truncated": False,
        "evidence_refs": ["browser:after-win"],
        "reobservation_recommended": True,
        "transition_digest": "sha256:" + hashlib.sha256(b"raw-transition").hexdigest(),
        "url_change": None,
        "frame_change": None,
        "added_visible_text": ["Congratulations! You pass the level."],
    }
    if transition_case == "malformed":
        transition["after_digest"] = transition["before_digest"]

    class EffectStore:
        def __init__(self) -> None:
            self.records = {}

        async def begin(self, record):
            existing = self.records.get(record.idempotency_key)
            if existing is not None:
                return existing, False
            self.records[record.idempotency_key] = record
            return record, True

        async def adopt_permitted(self, record, permit_digest):
            expected = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        record.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            )
            assert permit_digest == expected
            return await self.begin(record)

        async def finish(self, record):
            self.records[record.idempotency_key] = record
            return record

        async def list_for_session(self, session_id, *, tool_name=None):
            return [
                item
                for item in self.records.values()
                if item.session_id == session_id
                and (tool_name is None or item.tool_name == tool_name)
            ]

        async def list_for_attempt(self, attempt_id, *, limit=None):
            values = [item for item in self.records.values() if item.attempt_id == attempt_id]
            return values if limit is None else values[-limit:]

        async def list_for_campaign(self, campaign_id, *, limit=None):
            records = [item for item in self.records.values() if item.campaign_id == campaign_id]
            return records if limit is None else records[-limit:]

    class SurfaceTool:
        effect = "target_network"

        def __init__(self) -> None:
            self.calls = 0

        def describe(self):
            return ToolSpec(
                name="browser_chat_exchange",
                description="Reviewed browser exchange",
                input_schema={"type": "object"},
                interaction_role=ToolInteractionRole.TARGET_EXCHANGE,
            )

        def resolve_target_session_id(self, context):
            del context
            return "browser:context-1"

        async def invoke(self, context, arguments):
            del context, arguments
            self.calls += 1
            assert self.calls == 1, "material transition must fence replacement delivery"
            malformed = transition_case == "malformed"
            return ToolResult(
                content="invalid transition observation" if malformed else "",
                is_error=malformed,
                metadata={
                    "target_session_id": "browser:context-1",
                    "target_exchanges": (
                        []
                        if malformed
                        else [
                            {
                                "interaction": True,
                                "cleanup": False,
                                "request_id": "request-1",
                                "status_code": 200,
                            }
                        ]
                    ),
                    "target_effect_attempted": False if malformed else True,
                    "target_call_budget_charge": 0 if malformed else 1,
                    "limit_interaction_count": 0 if malformed else 1,
                    "target_ui_transition": transition,
                    "oracle_observations": (
                        []
                        if malformed
                        else [
                            {
                                "purpose": "objective",
                                "target_authoritative": True,
                                "matched": True,
                            }
                        ]
                    ),
                },
            )

    async def allow(*_args):
        return True

    async def crash_after_ack(self, handoff, control=None, *, checkpoint=None):
        del control, checkpoint
        result = await self._tools.invoke(  # noqa: SLF001
            "browser_chat_exchange",
            ToolContext(
                session_id=handoff.session_id,
                workspace_root=handoff.workspace_root,
                call_id="crash-transition",
                agent_id=handoff.assignment.id,
                attempt_id=handoff.attempt_id,
                technique_attempt_id=handoff.technique_attempt_id,
                campaign_id="campaign/crash-transition",
                target_id=handoff.assignment.target_profile_id,
            ),
            {"prompt": "authorized probe"},
        )
        assert result.metadata["target_ui_transition"] == transition
        raise RuntimeError("injected crash after durable target acknowledgement")

    monkeypatch.setattr(WorkerRuntime, "run", crash_after_ack)

    class CrashStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.retry_failed_calls = 0

        def retry_failed(self, *args, **kwargs):
            self.retry_failed_calls += 1
            return super().retry_failed(*args, **kwargs)

        def commit_reducer(self, attempt_id, generation, **kwargs):
            digest = super().commit_reducer(attempt_id, generation, **kwargs)
            lease = self.attempts[attempt_id]
            self.results[lease.schedule_id][lease.node_id]["attempt_id"] = attempt_id
            return digest

    assignment = WorkerAssignment(
        id="campaign/crash-transition/worker",
        objective="Run one authorized browser exchange.",
        profile_id="recon",
        target_profile_id="target-surface",
        technique_ids=("surface-transition",),
        guidance_ids=(),
        tool_ids=("browser_chat_exchange",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_turns=2, max_tokens=20_000, max_seconds=30),
        max_target_exchanges=1,
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        strategy_id="direct",
    )
    profile = ContextDocument.create("profile:recon@1", "Observe the reviewed surface.")
    target = ReviewedTargetAdapter(
        "target-surface",
        "browser",
        "https://target.test/chat",
        "web-chat",
        authentication="ambient authenticated browser context",
    )
    context_values = {
        "assignment_id": assignment.id,
        "profile": asdict(profile),
        "guidance": [],
        "skills": [],
        "tool_ids": assignment.tool_ids,
        "target": asdict(target),
        "authority_ids": assignment.authority_ids,
        "strategy_id": "direct",
        "strategy_contract": (),
    }
    worker_context = WorkerContext(
        assignment_id=assignment.id,
        profile=profile,
        guidance=(),
        skills=(),
        tool_ids=assignment.tool_ids,
        target=target,
        authority_ids=assignment.authority_ids,
        digest=_digest(context_values),
    )
    target_tool = SurfaceTool()
    broker = ToolBroker(
        (target_tool,),
        confirmer=allow,
        effect_store=EffectStore(),
    )
    store = CrashStore()
    service = DurableAgentService(Provider(), broker, store, model="test")

    live = await service.submit(
        "session-crash-transition",
        (assignment,),
        max_workers=1,
        minimum_workers=1,
        worker_contexts={assignment.id: worker_context},
    )
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=1)

    node_id = f"worker:{assignment.id}"
    attempts = [attempt for attempt in store.attempts.values() if attempt.node_id == node_id]
    assert store.nodes[live.id][node_id]["state"] == (
        "quarantined" if transition_case == "malformed" else "completed"
    )
    assert len(attempts) == 1
    assert store.retry_failed_calls == 0
    assert target_tool.calls == 1
    assert any(
        event_type == "worker_crash_transition_recovered"
        for _, event_type, _ in store.lifecycle_events
    )
    if transition_case == "malformed":
        assert node_id not in store.results.get(live.id, {})
        return
    effect = next(iter(broker._effect_store.records.values()))  # noqa: SLF001
    payload = store.results[live.id][node_id]["payload"]
    crash_receipt = payload["worker_checkpoint"]["target_evidence"][0]
    normal_receipts = {}
    WorkerRuntime._record_target_evidence(  # noqa: SLF001
        normal_receipts,
        WorkerHandoff(
            live.session_id,
            ".",
            "test",
            WorkerProfile("recon", 1, "", profile.content, frozenset()),
            assignment,
            worker_context=worker_context,
            attempt_id=attempts[0].id,
        ),
        ToolCall(
            call_id=str(effect.call_id),
            name=str(effect.tool_name),
            arguments=dict(effect.arguments_payload or {}),
        ),
        ToolResult(
            content=str(effect.result_content or ""),
            is_error=bool(effect.result_is_error),
            metadata=dict(effect.result_metadata),
        ),
    )
    assert crash_receipt == normal_receipts["crash-transition"]
    assert crash_receipt["target_authoritative_outcome"] == "confirmed"
    assert crash_receipt["outcome_authority"] == "target"
    assert crash_receipt["candidate_outcome_projection"]["outcome"] == "confirmed"

    task = (await service.list_schedule_tasks(live.id))[0]
    committed = await service.committed_task_result_evidence(live.id, task)
    assert committed is not None
    assert committed["target_evidence_receipts"] == {"crash-transition": crash_receipt}


async def test_replacement_result_inherits_authenticated_ancestor_evidence_after_restart() -> None:
    task_id = "campaign/recon"
    ancestor_attempt_id = "attempt-before-restart"
    replacement_attempt_id = "attempt-after-restart"
    arguments = {"input": "inspect target"}
    content = '{"response":"target observation"}'
    metadata = {"target_session_id": "target-session-1"}
    receipt_body = {
        "call_id": "call-before-restart",
        "tool_name": "target_request",
        "reference": "target-call:call-before-restart",
        "campaign_id": "campaign/recon",
        "session_id": "session-1",
        "assignment_id": task_id,
        "attempt_id": ancestor_attempt_id,
        "technique_attempt_id": "technique-attempt-1",
        "target_id": "target-1",
        "target_session_id": "target-session-1",
        "request_sha256": WorkerRuntime._action_fingerprint(  # noqa: SLF001
            "target_request", arguments
        ),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "content_bytes": len(content.encode()),
        "metadata_sha256": WorkerRuntime._json_fingerprint(metadata),  # noqa: SLF001
        "is_error": False,
        "acknowledged": True,
    }
    receipt = {
        **receipt_body,
        "receipt_sha256": WorkerRuntime._json_fingerprint(receipt_body),  # noqa: SLF001
    }
    ancestor_effect = SimpleNamespace(
        call_id="call-before-restart",
        tool_name="target_request",
        campaign_id="campaign/recon",
        session_id="session-1",
        agent_id=task_id,
        attempt_id=ancestor_attempt_id,
        target_id="target-1",
        state="acknowledged",
        arguments_payload=arguments,
        result_content=content,
        result_metadata=metadata,
        result_is_error=False,
    )

    class ReplacementStore(Store):
        def replacement_attempt_lineage(self, attempt_id, *, limit=None):
            assert attempt_id == replacement_attempt_id
            values = (ancestor_attempt_id, replacement_attempt_id)
            return values if limit is None else values[-limit:]

    class LineageTools(Tools):
        def __init__(self) -> None:
            self.loaded_attempt_ids: list[str] = []

        async def list_effects_for_attempt(self, attempt_id, *, limit=None):
            self.loaded_attempt_ids.append(attempt_id)
            values = (ancestor_effect,) if attempt_id == ancestor_attempt_id else ()
            return values if limit is None else values[-limit:]

    store = ReplacementStore()
    store.schedules["schedule-1"] = {
        "id": "schedule-1",
        "session_id": "session-1",
    }
    store.results["schedule-1"] = {
        f"worker:{task_id}": {
            "node_id": f"worker:{task_id}",
            "attempt_id": replacement_attempt_id,
            "payload_digest": "a" * 64,
            "schema_id": "schema/result",
            "payload": {
                "content": '{"applicable_techniques":[]}',
                "target_id": "target-1",
                "target_session_id": "target-session-1",
                "worker_checkpoint": {
                    "messages": [],
                    "technique_attempt_id": "technique-attempt-1",
                    # The replacement restores this immutable daemon receipt. Its
                    # signed provenance continues to name the ancestor attempt.
                    "target_evidence": [receipt],
                },
            },
        }
    }
    tools = LineageTools()

    # A new service instance models result projection after daemon restart.
    restarted_service = DurableAgentService(Provider(), tools, store)
    evidence = await restarted_service.committed_task_result_evidence(
        "schedule-1",
        AgentTask(
            id=task_id,
            objective="recon",
            status="completed",
            result='{"applicable_techniques":[]}',
        ),
    )

    assert evidence is not None
    assert evidence["attempt_id"] == replacement_attempt_id
    assert evidence["evidence_attempt_ids"] == (
        ancestor_attempt_id,
        replacement_attempt_id,
    )
    assert evidence["inherited_target_call_ids"] == ("call-before-restart",)
    assert evidence["target_call_ids"] == ("call-before-restart",)
    assert evidence["target_call_documents"] == {
        "call-before-restart": {"response": "target observation"}
    }
    assert evidence["target_evidence_receipts"] == {"call-before-restart": receipt}
    assert tools.loaded_attempt_ids == [ancestor_attempt_id, replacement_attempt_id]


@pytest.mark.parametrize(
    "tampered_field",
    (
        "campaign_id",
        "session_id",
        "assignment_id",
        "target_id",
        "target_session_id",
        "technique_attempt_id",
        "effect_attempt_id",
    ),
)
def test_inherited_target_evidence_rejects_cross_lineage_attribution(
    tampered_field: str,
) -> None:
    task_id = "campaign/fixture-id/recon"
    ancestor_attempt_id = "attempt-before-restart"
    replacement_attempt_id = "attempt-after-restart"
    call_id = "call-before-restart"
    arguments = {"input": "inspect target"}
    content = '{"response":"target observation"}'
    metadata = {"target_session_id": "target-session-1"}
    receipt_body = {
        "call_id": call_id,
        "tool_name": "target_request",
        "reference": f"target-call:{call_id}",
        "campaign_id": "campaign/fixture-id",
        "session_id": "session-1",
        "assignment_id": task_id,
        "attempt_id": ancestor_attempt_id,
        "technique_attempt_id": "technique-attempt-1",
        "target_id": "target-1",
        "target_session_id": "target-session-1",
        "request_sha256": WorkerRuntime._action_fingerprint(  # noqa: SLF001
            "target_request", arguments
        ),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "content_bytes": len(content.encode()),
        "metadata_sha256": WorkerRuntime._json_fingerprint(metadata),  # noqa: SLF001
        "is_error": False,
        "acknowledged": True,
    }
    if tampered_field != "effect_attempt_id":
        receipt_body[tampered_field] = f"wrong-{tampered_field}"
    receipt = {
        **receipt_body,
        "receipt_sha256": WorkerRuntime._json_fingerprint(receipt_body),  # noqa: SLF001
    }
    effect = SimpleNamespace(
        call_id=call_id,
        tool_name="target_request",
        campaign_id="campaign/fixture-id",
        session_id="session-1",
        agent_id=task_id,
        attempt_id=(
            replacement_attempt_id if tampered_field == "effect_attempt_id" else ancestor_attempt_id
        ),
        target_id="target-1",
        state="acknowledged",
        arguments_payload=arguments,
        result_content=content,
        result_metadata=metadata,
        result_is_error=False,
    )
    result_text = '{"applicable_techniques":[]}'
    row = {
        "node_id": f"worker:{task_id}",
        "attempt_id": replacement_attempt_id,
        "payload_digest": "a" * 64,
        "payload": {
            "content": result_text,
            "target_id": "target-1",
            "target_session_id": "target-session-1",
            "worker_checkpoint": {
                "messages": [],
                "technique_attempt_id": "technique-attempt-1",
                "target_evidence": [receipt],
            },
        },
    }

    evidence = DurableAgentService._committed_task_evidence(  # noqa: SLF001
        "schedule-1",
        AgentTask(id=task_id, objective="recon", status="completed", result=result_text),
        row,
        durable_effects=(effect,),
        evidence_attempt_ids=(ancestor_attempt_id, replacement_attempt_id),
        expected_session_id="session-1",
    )

    assert evidence is not None
    assert evidence["target_call_documents"] == {}
    if tampered_field == "effect_attempt_id":
        # The immutable signed receipt still proves the call occurred, but an
        # effect persisted under another attempt cannot supply its document.
        assert evidence["target_call_ids"] == (call_id,)
        assert evidence["inherited_target_call_ids"] == (call_id,)
    else:
        assert evidence["target_call_ids"] == ()
        assert evidence["target_evidence_receipts"] == {}
        assert "inherited_target_call_ids" not in evidence


async def test_committed_evidence_rejects_wrong_assignment_compacted_receipt() -> None:
    receipt_body = {
        "call_id": "call-compacted",
        "tool_name": "target_request",
        "reference": "target-call:call-compacted",
        "campaign_id": "campaign/recon",
        "assignment_id": "campaign/other",
        "attempt_id": "attempt-1",
        "technique_attempt_id": None,
        "target_id": "target-1",
        "target_session_id": "target-session-1",
        "request_sha256": "1" * 64,
        "content_sha256": "2" * 64,
        "content_bytes": 20,
        "metadata_sha256": "3" * 64,
        "is_error": False,
        "acknowledged": True,
    }
    receipt = {
        **receipt_body,
        "receipt_sha256": WorkerRuntime._json_fingerprint(receipt_body),  # noqa: SLF001
    }
    row = {
        "node_id": "worker:campaign/recon",
        "attempt_id": "attempt-1",
        "payload_digest": "a" * 64,
        "payload": {
            "content": "result",
            "target_id": "target-1",
            "target_session_id": "target-session-1",
            "worker_checkpoint": {"messages": [], "target_evidence": [receipt]},
        },
    }

    evidence = DurableAgentService._committed_task_evidence(  # noqa: SLF001
        "schedule-1",
        AgentTask(id="campaign/recon", objective="recon", status="completed", result="result"),
        row,
    )

    assert evidence is not None
    assert evidence["target_call_ids"] == ()
    assert evidence["target_evidence_receipts"] == {}


async def test_schedule_usage_uses_latest_cumulative_attempt_per_worker() -> None:
    store = Store()
    store.schedules["schedule-1"] = {"id": "schedule-1"}
    store.results["schedule-1"] = {
        "attempt-old": {
            "node_id": "worker:a",
            "payload": {
                "worker_checkpoint": {
                    "usage": {"total_tokens": 80, "input_tokens": 60, "output_tokens": 20},
                    "turns_completed": 2,
                }
            },
        },
        "attempt-restored": {
            "node_id": "worker:a",
            "payload": {
                "usage": {"total_tokens": 120, "input_tokens": 90, "output_tokens": 30},
                "turns_completed": 3,
            },
        },
        "attempt-b": {
            "node_id": "worker:b",
            "payload": {
                "usage": {
                    "total_tokens": 50,
                    "input_tokens": 40,
                    "output_tokens": 10,
                    "estimated_cost_usd": 0.25,
                },
                "turns_completed": 1,
            },
        },
    }
    service = DurableAgentService(Provider(), Tools(), store, model="test")

    usage = await service.schedule_usage("schedule-1")

    assert usage == {
        "input_tokens": 130,
        "output_tokens": 40,
        "total_tokens": 170,
        "estimated_cost_usd": 0.25,
        "turns": 4,
    }


async def test_schedule_usage_omits_cost_when_unreported() -> None:
    store = Store()
    store.schedules["schedule-1"] = {"id": "schedule-1"}
    store.results["schedule-1"] = {
        "attempt-a": {
            "node_id": "worker:a",
            "payload": {
                "usage": {"total_tokens": 120, "input_tokens": 90, "output_tokens": 30},
                "turns_completed": 3,
            },
        },
        "attempt-b": {
            "node_id": "worker:b",
            "payload": {
                "usage": {"total_tokens": 50, "estimated_cost_usd": None},
                "turns_completed": 1,
            },
        },
    }
    service = DurableAgentService(Provider(), Tools(), store, model="test")

    usage = await service.schedule_usage("schedule-1")

    # Unknown cost stays absent rather than reading as a genuine $0.
    assert "estimated_cost_usd" not in usage
    assert usage["total_tokens"] == 170


async def test_schedule_usage_preserves_provider_cache_token_partitions() -> None:
    store = Store()
    store.schedules["schedule-1"] = {"id": "schedule-1"}
    store.results["schedule-1"] = {
        "attempt-old": {
            "node_id": "worker:a",
            "payload": {
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "total_tokens": 12,
                    "uncached_input_tokens": 7,
                    "cache_read_input_tokens": 2,
                    "cache_creation_input_tokens": 1,
                }
            },
        },
        "attempt-restored": {
            "node_id": "worker:a",
            "payload": {
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 4,
                    "total_tokens": 24,
                    "uncached_input_tokens": 12,
                    "cache_read_input_tokens": 6,
                    "cache_creation_input_tokens": 2,
                }
            },
        },
        "attempt-b": {
            "node_id": "worker:b",
            "payload": {
                "usage": {
                    "input_tokens": 8,
                    "output_tokens": 1,
                    "total_tokens": 9,
                    "uncached_input_tokens": 3,
                    "cache_read_input_tokens": 4,
                    "cache_creation_input_tokens": 1,
                }
            },
        },
    }
    service = DurableAgentService(Provider(), Tools(), store, model="test")

    usage = await service.schedule_usage("schedule-1")

    assert usage["uncached_input_tokens"] == 15
    assert usage["cache_read_input_tokens"] == 10
    assert usage["cache_creation_input_tokens"] == 3


async def test_schedule_usage_estimates_missing_cost_from_pinned_assignment_pricing() -> None:
    store = Store()
    store.schedules["schedule-1"] = {"id": "schedule-1"}
    store.results["schedule-1"] = {
        "attempt-a": {
            "node_id": "worker:a",
            "payload": {
                "usage": {"total_tokens": 120, "input_tokens": 90, "output_tokens": 30},
                "provider_pricing": {
                    "input_cost_per_million": 1.0,
                    "output_cost_per_million": 2.0,
                },
            },
        }
    }
    service = DurableAgentService(Provider(), Tools(), store, model="test")

    usage = await service.schedule_usage("schedule-1")

    assert usage["estimated_cost_usd"] == pytest.approx(0.00015)


async def test_schedule_usage_estimates_cost_with_cache_read_pricing() -> None:
    store = Store()
    store.schedules["schedule-1"] = {"id": "schedule-1"}
    store.results["schedule-1"] = {
        "attempt-a": {
            "node_id": "worker:a",
            "payload": {
                "usage": {
                    "total_tokens": 120,
                    "input_tokens": 90,
                    "output_tokens": 30,
                    "uncached_input_tokens": 60,
                    "cache_read_input_tokens": 30,
                },
                "provider_pricing": {
                    "input_cost_per_million": 1.0,
                    "output_cost_per_million": 2.0,
                    "cache_read_input_cost_per_million": 0.25,
                },
            },
        }
    }
    service = DurableAgentService(Provider(), Tools(), store, model="test")

    usage = await service.schedule_usage("schedule-1")

    # (60 uncached * 1.0 + 30 cache reads * 0.25 + 30 output * 2.0) / 1e6
    assert usage["estimated_cost_usd"] == pytest.approx(0.0001275)


async def test_schedule_usage_without_cache_rate_bills_cache_reads_at_input_rate() -> None:
    store = Store()
    store.schedules["schedule-1"] = {"id": "schedule-1"}
    store.results["schedule-1"] = {
        "attempt-a": {
            "node_id": "worker:a",
            "payload": {
                "usage": {
                    "total_tokens": 120,
                    "input_tokens": 90,
                    "output_tokens": 30,
                    "uncached_input_tokens": 60,
                    "cache_read_input_tokens": 30,
                },
                "provider_pricing": {
                    "input_cost_per_million": 1.0,
                    "output_cost_per_million": 2.0,
                },
            },
        }
    }
    service = DurableAgentService(Provider(), Tools(), store, model="test")

    usage = await service.schedule_usage("schedule-1")

    # Cache reads fall back to the full input rate (documented upper bound).
    assert usage["estimated_cost_usd"] == pytest.approx(0.00015)


class FindingSink:
    def __init__(self, store):
        self.store = store
        self.created = []

    async def create(self, session_id, draft):
        reference = draft.evidence_references[0]
        node_id = reference.split("/node/", 1)[1].split("/attempt/", 1)[0]
        assert node_id in self.store.results["schedule-1"]
        self.created.append((session_id, draft))
        return draft


class MemoryProvider:
    def __init__(self, store, *, active: bool) -> None:
        self.store = store
        self.active = active
        self.queries = []
        self.uses = []

    async def retrieve(self, query):
        self.queries.append(query)
        if not self.active:
            return ()  # quarantined records are never returned by the provider seam
        return (
            PortableMemoryRecord(
                id="lesson/accepted",
                kind="sealed_lesson",
                content="Use the reviewed boundary lesson.",
                provenance_source="merlin://repo/acme",
                reviewed_by=("operator/alice",),
                digest="sha256:accepted",
            ),
        )

    async def record_turn_use(self, query, turn_id, record_ids):
        # Durable result commit is visible before exact memory attribution.
        assert f"worker:{query.task_id}" in self.store.results["schedule-1"]
        self.uses.append((query, turn_id, record_ids))


async def test_cancel_schedule_updates_live_task_projection_before_persisting() -> None:
    store = Store()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/task-{index}",
            objective="Assess",
            profile_id=profile,
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for index, profile in enumerate(("recon", "injection"))
    )
    live = await service.submit("session", assignments, max_workers=2, start_immediately=False)

    await service.cancel_schedule(live.id, reason="target-terminal")

    tasks = await service.list_schedule_tasks(live.id)
    assert {task.status for task in tasks} == {"cancelled"}
    checkpoint = store.schedules[live.id]["checkpoint"]
    worker_states = {
        node_id: state["status"]
        for node_id, state in checkpoint["nodes"].items()
        if node_id.startswith("worker:")
    }
    assert set(worker_states.values()) == {"canceled"}


async def test_submit_rejects_oversized_schedule_before_calling_store(monkeypatch) -> None:
    monkeypatch.setattr("merlin_agents.durable_service._MAX_SCHEDULE_JSONB_BYTES", 1_024)
    store = Store()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    assignment = WorkerAssignment(
        id="campaign/oversized",
        objective="x" * 2_000,
        profile_id="recon",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )

    with pytest.raises(ScheduleArtifactTooLarge, match="reduce the execution batch") as raised:
        await service.submit(
            "session",
            (assignment,),
            max_workers=1,
            minimum_workers=1,
            start_immediately=False,
        )

    assert raised.value.artifact == "graph"
    assert raised.value.actual_bytes > raised.value.limit_bytes == 1_024
    assert store.schedules == {}


async def test_reject_persisted_schedule_does_not_hydrate_obsolete_assignment() -> None:
    store = Store()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    assignment = WorkerAssignment(
        id="campaign/task",
        objective="Assess",
        profile_id="recon",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit(
        "session", (assignment,), max_workers=1, minimum_workers=1, start_immediately=False
    )
    store.schedules[live.id]["graph"]["assignments"][0]["obsolete_field"] = True

    await service.reject_persisted_schedule(live.id, reason="incompatible persisted state")

    assert store.schedules[live.id]["status"] == "cancelled"
    assert store.nodes[live.id]["worker:campaign/task"]["state"] == "cancelled"


async def test_schedule_progress_projects_checkpoint_phase_and_counters() -> None:
    store = Store()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    assignment = WorkerAssignment(
        id="campaign/task",
        objective="Assess",
        profile_id="recon",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit(
        "session", (assignment,), start_immediately=False, minimum_workers=1
    )
    node_id = "worker:campaign/task"
    store.nodes[live.id][node_id]["state"] = "leased"
    store.results[live.id] = {
        node_id: {
            "node_id": node_id,
            "payload_digest": "sha256:checkpoint",
            "payload": {
                "worker_checkpoint": {
                    "active_phase": "target",
                    "turns_completed": 3,
                    "action_count": 2,
                }
            },
        }
    }

    progress = await service.schedule_progress(live.id)

    assert (node_id, "leased", "target", 3, 2, "sha256:checkpoint") in progress
    assert any(row[0] == "cleanup:campaign/task" and row[2] == "cleanup" for row in progress)


async def test_completion_projection_keeps_public_gates_and_loads_results_once() -> None:
    class CountingStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.result_reads = 0

        def list_results(self, schedule_id):
            self.result_reads += 1
            return super().list_results(schedule_id)

    store = CountingStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/task-{index}",
            objective="Assess",
            profile_id="recon",
            target_profile_id="target",
            technique_ids=(f"technique-{index}",),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for index in range(100)
    )
    live = await service.submit("session", assignments, start_immediately=False, minimum_workers=1)

    projection = await service.completion_projection(live.id)

    assert len(projection["tasks"]) == 100
    assert store.result_reads == 1
    for method in (
        "baseline_gate",
        "crescendo_gate",
        "goat_gate",
        "hydra_select_gate",
        "validation_is_independent",
        "schedule_progress",
    ):
        assert callable(getattr(service, method))


async def test_active_activity_uses_each_exact_schedule_and_mailbox_projects_delivery_state() -> (
    None
):
    class ProjectionStore(Store):
        def __init__(self):
            super().__init__()
            self.restored = []

        def list_active_schedules(self, *, limit=50):
            return (
                {"id": "schedule-one", "session_id": "shared-session"},
                {"id": "schedule-two", "session_id": "shared-session"},
            )

        def restore(self, schedule_id):
            self.restored.append(schedule_id)
            suffix = "one" if schedule_id == "schedule-one" else "two"
            return {
                "attempts": [{"id": f"attempt-{suffix}", "node_id": f"worker:task-{suffix}"}],
                "events": [
                    {
                        "type": "checkpointed",
                        "created_at": "2026-08-30T12:00:00+00:00",
                        "payload": {
                            "attempt_id": f"attempt-{suffix}",
                            "phase": "thinking",
                            "action_count": 1,
                        },
                    }
                ],
                "mailbox": [
                    {
                        "recipient": f"worker:task-{suffix}",
                        "sender": "lead",
                        "payload": {"message": f"guidance {suffix}"},
                        "claimed_by_attempt": (f"attempt-{suffix}" if suffix == "two" else None),
                        "acknowledged_at": None,
                    },
                    {
                        "recipient": f"worker:task-{suffix}",
                        "sender": "lead",
                        "payload": {"message": "already applied"},
                        "claimed_by_attempt": f"attempt-{suffix}",
                        "acknowledged_at": "2026-08-30T12:01:00+00:00",
                    },
                ],
            }

    store = ProjectionStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")

    activity = await service.list_active_activity()
    mailbox_one = await service.list_schedule_mailbox("schedule-one")
    mailbox_two = await service.list_schedule_mailbox("schedule-two")

    assert set(activity) == {"worker:task-one", "worker:task-two"}
    assert "[checkpoint] · thinking · 1 actions" in activity["worker:task-one"][0]
    assert mailbox_one == {"worker:task-one": ("[pending] lead: guidance one",)}
    assert mailbox_two == {"worker:task-two": ("[claimed] lead: guidance two",)}
    assert store.restored[:2] == ["schedule-one", "schedule-two"]


async def test_cancel_task_accepts_task_or_node_id_and_preserves_schedule() -> None:
    store = Store()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/task-{index}",
            objective="Assess",
            profile_id=profile,
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for index, profile in enumerate(("recon", "injection"))
    )
    live = await service.submit("session", assignments, max_workers=2, start_immediately=False)

    selected = await service.cancel_task("different-console-session", "campaign/task-0")

    assert selected == "worker:campaign/task-0"
    assert store.schedules[live.id]["status"] == "running"
    tasks = await service.list_schedule_tasks(live.id)
    assert {task.id: task.status for task in tasks} == {
        "campaign/task-0": "cancelled",
        "campaign/task-1": "queued",
    }
    with pytest.raises(ValueError, match="already terminal"):
        await service.cancel_task("session", "worker:campaign/task-0")


async def test_cancel_task_persists_fence_before_interrupting_live_worker() -> None:
    started = asyncio.Event()
    closed = asyncio.Event()
    observed: dict[str, bool] = {}

    class BlockingProvider:
        async def stream(self, request):
            del request
            try:
                started.set()
                await asyncio.Event().wait()
                yield ModelCompleted()  # pragma: no cover
            finally:
                closed.set()

    class OrderingStore(Store):
        def cancel_node(self, *args, **kwargs):
            observed["worker_open_at_durable_cancel"] = not closed.is_set()
            return super().cancel_node(*args, **kwargs)

    store = OrderingStore()
    service = DurableAgentService(BlockingProvider(), Tools(), store, model="test")
    assignment = WorkerAssignment(
        id="campaign/cancel-fence/task",
        objective="Assess",
        profile_id="recon",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit("session", (assignment,), max_workers=1, minimum_workers=1)
    assert live.execution is not None
    await asyncio.wait_for(started.wait(), timeout=1)

    await service.cancel_task("session", assignment.id)
    await asyncio.wait_for(live.execution, timeout=1)

    assert observed == {"worker_open_at_durable_cancel": True}
    assert closed.is_set()
    assert store.nodes[live.id][f"worker:{assignment.id}"]["state"] == "cancelled"


@pytest.mark.parametrize("directive", [False, True])
async def test_exact_task_cancel_interrupts_live_provider_and_runs_cleanup(
    directive: bool,
) -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    class BlockingProvider:
        async def stream(self, request):
            del request
            try:
                started.set()
                yield TextDelta(text='{"partial":"evidence"}')
                await asyncio.Event().wait()
            finally:
                closed.set()

    store = Store()
    service = DurableAgentService(BlockingProvider(), Tools(), store, model="test")
    assignment = WorkerAssignment(
        id="campaign/live-task",
        objective="Assess",
        profile_id="recon",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit("session", (assignment,), max_workers=1, minimum_workers=1)
    assert live.execution is not None
    await asyncio.wait_for(started.wait(), timeout=1)

    if directive:
        selected = await service.cancel_task_once(
            "session",
            assignment.id,
            schedule_id=live.id,
            cancellation_id="directive/cancel-1",
        )
    else:
        selected = await service.cancel_task("different-console", assignment.id)
    await asyncio.wait_for(live.execution, timeout=1)

    if directive:
        assert (
            await service.cancel_task_once(
                "session",
                assignment.id,
                schedule_id=live.id,
                cancellation_id="directive/cancel-1",
            )
            == f"worker:{assignment.id}"
        )
        # A distinct directive identity targeting an already-cancelled node is
        # an idempotent no-op, not an error.
        assert (
            await service.cancel_task_once(
                "session",
                assignment.id,
                schedule_id=live.id,
                cancellation_id="directive/unrelated",
            )
            == f"worker:{assignment.id}"
        )

    assert selected == f"worker:{assignment.id}"
    assert closed.is_set()
    assert store.nodes[live.id][f"worker:{assignment.id}"]["state"] == "cancelled"
    assert store.nodes[live.id][f"cleanup:{assignment.id}"]["state"] == "completed"


async def test_cancel_task_once_terminal_node_is_idempotent_noop() -> None:
    """A Lead cancel directive for an already-terminal node must not write a
    durable cancel that races the scheduler's guarded pending transition."""

    class RecordingStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_node_calls = 0

        def cancel_node(self, *args, **kwargs):
            self.cancel_node_calls += 1
            return super().cancel_node(*args, **kwargs)

    store = RecordingStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    assignment = WorkerAssignment(
        id="campaign/terminal-task",
        objective="Assess",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit(
        "session", (assignment,), max_workers=1, minimum_workers=1, start_immediately=False
    )
    # The scheduler has already terminalized the node in memory (for example, a
    # dependency failure), but its durable pending -> failed transition has not
    # committed yet.
    tick = int(live.scheduler.checkpoint()["last_tick"]) + 1
    live.scheduler.cancel(f"worker:{assignment.id}", "scheduler-failure", tick)

    selected = await service.cancel_task_once(
        "session", assignment.id, schedule_id=live.id, cancellation_id="directive/cancel-1"
    )

    assert selected == f"worker:{assignment.id}"
    assert store.cancel_node_calls == 0
    assert store.nodes[live.id][f"worker:{assignment.id}"]["state"] != "cancelled"


async def test_campaign_cleanup_failure_remains_retryable() -> None:
    attempts = 0

    async def campaign_cleanup(_assignment_id: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("cleanup transport failed")

    store = Store()
    service = DurableAgentService(
        Provider(), Tools(), store, model="test", campaign_cleanup=campaign_cleanup
    )
    assignment = WorkerAssignment(
        id="campaign/cleanup-retry/injection",
        objective="Assess",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit(
        "session", (assignment,), start_immediately=False, minimum_workers=1
    )
    worker = f"worker:{assignment.id}"
    cleanup = f"cleanup:{assignment.id}"
    live.scheduler.cancel(worker, "operator", 1)
    await service._ready(live, live.scheduler.ready_nodes())

    with pytest.raises(RuntimeError, match="cleanup transport failed"):
        await service._run_cleanup(live, 2)

    assert live.scheduler.status(cleanup).value == "ready"
    assert store.nodes[live.id][cleanup]["state"] == "ready"
    await service._run_cleanup(live, 3)

    assert attempts == 2
    assert store.nodes[live.id][cleanup]["state"] == "completed"


async def test_cleanup_unresolved_backs_off_then_completes_after_exact_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    class CleanupUnresolved(RuntimeError):
        cleanup_unresolved = True
        infrastructure_failure_category = "unhealthy_target_session"

    async def campaign_cleanup(_assignment_id: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise CleanupUnresolved("cleanup acknowledgement unavailable")

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("merlin_agents.durable_service.asyncio.sleep", record_sleep)
    store = Store()
    service = DurableAgentService(
        Provider(),
        Tools(),
        store,
        model="test",
        campaign_cleanup=campaign_cleanup,
        provider_retry_backoff_seconds=0.25,
    )
    assignment = WorkerAssignment(
        id="campaign/cleanup-unresolved/injection",
        objective="Assess",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit(
        "session", (assignment,), start_immediately=False, minimum_workers=1
    )
    worker = f"worker:{assignment.id}"
    cleanup = f"cleanup:{assignment.id}"
    live.scheduler.cancel(worker, "operator", 1)
    await service._ready(live, live.scheduler.ready_nodes())

    with pytest.raises(CleanupUnresolved, match="acknowledgement unavailable"):
        await service._run_cleanup(live, 2)

    assert sleeps == [0.25]
    assert live.scheduler.status(cleanup).value == "ready"
    assert store.nodes[live.id][cleanup]["state"] == "ready"

    await service._run_cleanup(live, 3)

    assert attempts == 2
    assert store.nodes[live.id][cleanup]["state"] == "completed"


async def test_repeated_cleanup_unresolved_obeys_attempt_budget_without_attack_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    class CleanupUnresolved(RuntimeError):
        cleanup_unresolved = True
        infrastructure_failure_category = "unhealthy_target_session"

    async def campaign_cleanup(_assignment_id: str) -> None:
        raise CleanupUnresolved("cleanup remains unresolved")

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("merlin_agents.durable_service.asyncio.sleep", record_sleep)
    store = Store()
    service = DurableAgentService(
        Provider(),
        Tools(),
        store,
        model="test",
        campaign_cleanup=campaign_cleanup,
        cleanup_max_attempts=2,
        provider_retry_backoff_seconds=0.5,
    )
    assignment = WorkerAssignment(
        id="campaign/cleanup-budget/injection",
        objective="Assess",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit(
        "session", (assignment,), start_immediately=False, minimum_workers=1
    )
    worker = f"worker:{assignment.id}"
    cleanup = f"cleanup:{assignment.id}"
    live.scheduler.cancel(worker, "operator", 1)
    await service._ready(live, live.scheduler.ready_nodes())

    for tick in (2, 3):
        with pytest.raises(CleanupUnresolved, match="remains unresolved"):
            await service._run_cleanup(live, tick)

    assert sleeps == [0.5, 0.5]
    assert live.scheduler.status(worker).value == "canceled"
    assert live.scheduler.status(cleanup).value == "failed"
    assert store.nodes[live.id][worker]["state"] != "failed"
    assert store.nodes[live.id][cleanup]["state"] == "failed"


async def test_operator_resume_rearms_only_exhausted_cleanup_unresolved_epoch() -> None:
    cleanup_acknowledged = False

    class CleanupUnresolved(RuntimeError):
        cleanup_unresolved = True

    async def campaign_cleanup(_assignment_id: str) -> None:
        if not cleanup_acknowledged:
            raise CleanupUnresolved("清理状态未知")

    class CompleteStore(Store):
        def complete_schedule(self, schedule_id: str) -> None:
            self.schedules[schedule_id]["status"] = "completed"
            self.schedules[schedule_id]["event_count"] += 1

    store = CompleteStore()
    service = DurableAgentService(
        Provider(),
        Tools(),
        store,
        model="test",
        campaign_cleanup=campaign_cleanup,
        cleanup_max_attempts=2,
        provider_retry_backoff_seconds=0,
    )
    assignment = WorkerAssignment(
        id="campaign/cleanup-resume/injection",
        objective="Assess",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    pending_assignment = WorkerAssignment(
        id="campaign/cleanup-resume/remaining-worker",
        objective="Continue the authorized campaign after resume",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit(
        "session",
        (assignment, pending_assignment),
        start_immediately=False,
        minimum_workers=1,
    )
    worker = f"worker:{assignment.id}"
    pending_worker = f"worker:{pending_assignment.id}"
    cleanup = f"cleanup:{assignment.id}"
    live.scheduler.cancel(worker, "operator", 1)
    await service._ready(live, live.scheduler.ready_nodes())

    for tick in (2, 3):
        with pytest.raises(CleanupUnresolved, match="清理状态未知"):
            await service._run_cleanup(live, tick)

    assert live.scheduler.status(cleanup).value == "failed"
    attempts = store.attempts.values()
    exhausted_attempts = [attempt for attempt in attempts if attempt.node_id == cleanup]
    assert len(exhausted_attempts) == 2
    assert {attempt.failure_kind for attempt in exhausted_attempts} == {"cleanup_unresolved"}

    await service.pause_schedule(live.id, reason="operator-cleanup-repair")
    cleanup_acknowledged = True
    restarted = DurableAgentService(
        Provider(),
        Tools(),
        store,
        model="test",
        campaign_cleanup=campaign_cleanup,
        cleanup_max_attempts=2,
        provider_retry_backoff_seconds=0,
    )
    resumed = await restarted.resume_schedule(live.id)
    assert resumed.execution is not None
    await resumed.execution

    attempts = store.attempts.values()
    cleanup_attempts = [attempt for attempt in attempts if attempt.node_id == cleanup]
    assert len(cleanup_attempts) == 3
    assert cleanup_attempts[-1].generation > exhausted_attempts[-1].generation
    assert resumed.scheduler.status(worker).value == "canceled"
    assert store.nodes[live.id][worker]["state"] != "failed"
    assert store.nodes[live.id][cleanup]["state"] == "completed"
    assert resumed.scheduler.status(pending_worker).value == "completed"
    assert store.nodes[live.id][pending_worker]["state"] == "completed"
    assert store.schedules[live.id]["status"] == "completed"


async def test_operator_resume_does_not_parse_cleanup_failure_reason_text() -> None:
    class CompleteStore(Store):
        def complete_schedule(self, schedule_id: str) -> None:
            self.schedules[schedule_id]["status"] = "completed"
            self.schedules[schedule_id]["event_count"] += 1

    async def campaign_cleanup(_assignment_id: str) -> None:
        raise RuntimeError("target-cleanup-unresolved: ordinary application failure")

    store = CompleteStore()
    service = DurableAgentService(
        Provider(),
        Tools(),
        store,
        model="test",
        campaign_cleanup=campaign_cleanup,
        cleanup_max_attempts=1,
        provider_retry_backoff_seconds=0,
    )
    assignment = WorkerAssignment(
        id="campaign/cleanup-reason/injection",
        objective="Assess",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit(
        "session", (assignment,), start_immediately=False, minimum_workers=1
    )
    worker = f"worker:{assignment.id}"
    cleanup = f"cleanup:{assignment.id}"
    live.scheduler.cancel(worker, "operator", 1)
    await service._ready(live, live.scheduler.ready_nodes())
    with pytest.raises(RuntimeError, match="ordinary application failure"):
        await service._run_cleanup(live, 2)
    await service.pause_schedule(live.id, reason="operator")

    resumed = await service.resume_schedule(live.id)
    assert resumed.execution is not None
    await resumed.execution

    cleanup_attempts = [
        attempt for attempt in store.attempts.values() if attempt.node_id == cleanup
    ]
    assert len(cleanup_attempts) == 1
    assert cleanup_attempts[0].failure_kind is None
    assert resumed.scheduler.status(cleanup).value == "failed"


async def test_cleanup_rearm_persistence_failure_repauses_schedule() -> None:
    class FailingRetryStore(Store):
        fail_retry_persistence = False

        def retry_failed(self, *args, **kwargs):
            if self.fail_retry_persistence:
                raise OSError("retry persistence unavailable")
            return super().retry_failed(*args, **kwargs)

    class CleanupUnresolved(RuntimeError):
        cleanup_unresolved = True

    async def campaign_cleanup(_assignment_id: str) -> None:
        raise CleanupUnresolved("cleanup unknown")

    store = FailingRetryStore()
    service = DurableAgentService(
        Provider(),
        Tools(),
        store,
        model="test",
        campaign_cleanup=campaign_cleanup,
        cleanup_max_attempts=1,
        provider_retry_backoff_seconds=0,
    )
    assignment = WorkerAssignment(
        id="campaign/cleanup-rearm-failure/injection",
        objective="Assess",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit(
        "session", (assignment,), start_immediately=False, minimum_workers=1
    )
    worker = f"worker:{assignment.id}"
    live.scheduler.cancel(worker, "operator", 1)
    await service._ready(live, live.scheduler.ready_nodes())
    with pytest.raises(CleanupUnresolved):
        await service._run_cleanup(live, 2)
    await service.pause_schedule(live.id, reason="operator")
    store.fail_retry_persistence = True

    with pytest.raises(OSError, match="retry persistence unavailable"):
        await service.resume_schedule(live.id)

    assert store.schedules[live.id]["status"] == "paused"


async def test_schedule_cancel_interrupts_all_live_provider_streams() -> None:
    started = asyncio.Event()
    started_count = 0
    closed = 0

    class BlockingProvider:
        async def stream(self, request):
            nonlocal closed, started_count
            del request
            try:
                started_count += 1
                if started_count == 2:
                    started.set()
                yield TextDelta(text='{"partial":"evidence"}')
                await asyncio.Event().wait()
            finally:
                closed += 1

    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/live-{index}",
            objective="Assess",
            profile_id=profile,
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for index, profile in enumerate(("recon", "injection"))
    )
    store = Store()
    service = DurableAgentService(BlockingProvider(), Tools(), store, model="test")
    live = await service.submit("session", assignments, max_workers=2)
    assert live.execution is not None
    execution = live.execution
    await asyncio.wait_for(started.wait(), timeout=1)

    await service.cancel_schedule(live.id, reason="operator-campaign-cancel")
    assert execution.done()
    assert live.execution is None

    assert closed == 2
    assert store.schedules[live.id]["status"] == "cancelled"
    assert all(
        store.nodes[live.id][f"cleanup:{assignment.id}"]["state"] == "completed"
        for assignment in assignments
    )


async def test_ready_task_cancel_does_not_move_clock_past_running_worker_completion() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider:
        async def stream(self, request):
            del request
            started.set()
            await release.wait()
            yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()

    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/clock-race-{index}",
            objective="Assess",
            profile_id="recon",
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for index in range(2)
    )
    store = Store()
    service = DurableAgentService(BlockingProvider(), Tools(), store, model="test")
    live = await service.submit("session-clock-race", assignments, max_workers=1)
    assert live.execution is not None
    await asyncio.wait_for(started.wait(), timeout=1)

    await service.cancel_task("session-clock-race", assignments[1].id)
    release.set()
    await asyncio.wait_for(live.execution, timeout=1)

    assert live.scheduler.status(f"worker:{assignments[0].id}").value == "completed"
    assert live.scheduler.status(f"worker:{assignments[1].id}").value == "canceled"


async def test_cancelled_worker_checkpoint_race_does_not_tear_down_sibling() -> None:
    import threading

    class StaleLeaseError(RuntimeError):
        pass

    class Provider:
        async def stream(self, request):
            del request
            yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()

    class CheckpointRaceStore(Store):
        def __init__(self):
            super().__init__()
            self.checkpoint_entered = threading.Event()
            self.checkpoint_release = threading.Event()
            self.late_checkpoints = 0

        def commit_partial(self, attempt_id, generation, *, result, applied_message_ids=()):
            lease = self.attempts[attempt_id]
            if lease.node_id == "worker:campaign/checkpoint-race-0" and lease.state == "running":
                # Hold the first checkpoint in flight so the durable cancel commits
                # while the worker's checkpoint write is still racing it.
                self.checkpoint_entered.set()
                self.checkpoint_release.wait(timeout=5)
            if lease.state == "cancelled":
                self.late_checkpoints += 1
                raise StaleLeaseError("attempt does not own the current lease generation")

    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/checkpoint-race-{index}",
            objective="Assess",
            profile_id="recon",
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for index in range(2)
    )
    store = CheckpointRaceStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    live = await service.submit("session-checkpoint-race", assignments, max_workers=2)
    assert live.execution is not None
    entered = await asyncio.to_thread(store.checkpoint_entered.wait, 5)
    assert entered, "worker never wrote a checkpoint"

    await service.cancel_task_once(
        "session-checkpoint-race",
        assignments[0].id,
        schedule_id=live.id,
        cancellation_id="directive-1",
    )
    store.checkpoint_release.set()
    await asyncio.wait_for(live.execution, timeout=5)

    assert store.late_checkpoints >= 1
    assert live.scheduler.status(f"worker:{assignments[0].id}").value == "canceled"
    assert live.scheduler.status(f"worker:{assignments[1].id}").value == "completed"


async def test_reopen_resumes_cleanup_owed_by_cancelled_schedule() -> None:
    store = Store()
    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/cancel-recovery-{index}",
            objective="Assess",
            profile_id=profile,
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=(),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for index, profile in enumerate(("recon", "injection"))
    )
    first = DurableAgentService(Provider(), Tools(), store, model="test")
    live = await first.submit("session-cancel-recovery", assignments, start_immediately=False)
    cleanup = [node.id for node in live.graph.spec.nodes if node.kind == "cleanup"]
    for node in reversed(live.graph.spec.nodes):
        if node.id not in cleanup:
            live.scheduler.cancel(node.id, "operator", 1)
    store.cancel_schedule(
        live.id,
        reason="operator",
        cleanup_node_ids=cleanup,
        checkpoint=live.scheduler.checkpoint(),
    )
    # Reproduce an older/crash-interrupted cancellation boundary: durable node
    # rows own ready cleanup work, while the last checkpoint still has stale
    # prerequisite and cleanup states.
    checkpoint_nodes = store.schedules[live.id]["checkpoint"]["nodes"]
    for node_id, state in checkpoint_nodes.items():
        if node_id.startswith("cleanup:"):
            state["status"] = "pending"
            state["ready_since_tick"] = None
        elif node_id.startswith("worker:"):
            state["status"] = "pending"
            state["disposition"] = None

    recovered = await DurableAgentService(Provider(), Tools(), store, model="test").reopen(live.id)
    assert recovered.execution is not None
    await asyncio.wait_for(recovered.execution, timeout=1)

    assert all(store.nodes[live.id][node_id]["state"] == "completed" for node_id in cleanup)
    assert store.schedules[live.id]["status"] == "cancelled"


async def test_recover_reopens_each_active_schedule_exactly_once() -> None:
    store = Store()
    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/recover-{index}",
            objective="Assess",
            profile_id=profile,
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=(),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for index, profile in enumerate(("recon", "injection"))
    )
    creator = DurableAgentService(Provider(), Tools(), store, model="test")
    live = await creator.submit("session-recover", assignments, start_immediately=False)

    recovered_service = DurableAgentService(Provider(), Tools(), store, model="test")
    assert await recovered_service.recover() == 1
    first = recovered_service._live[live.id]  # noqa: SLF001
    assert await recovered_service.recover() == 0
    assert await recovered_service.reopen(live.id) is first
    assert first.execution is not None
    await asyncio.wait_for(first.execution, timeout=1)


async def test_recover_does_not_start_deterministic_staged_schedule() -> None:
    store = Store()
    planner = DurableAgentService(Provider(), Tools(), store, model="test")
    assignments = await planner._plan("Assess target", 2)  # noqa: SLF001
    live = await planner.submit(
        "session-staged",
        assignments,
        start_immediately=False,
        minimum_workers=1,
        schedule_id="2be4799f-df25-5d8f-9717-f8fb345899b1",
    )
    assert store.schedules[live.id]["status"] == "paused"

    recovered = DurableAgentService(Provider(), Tools(), store, model="test")
    assert await recovered.recover() == 1
    assert recovered._live[live.id].execution is None  # noqa: SLF001
    assert store.attempts == {}


@pytest.mark.asyncio
async def test_recover_quarantines_invalid_schedule_and_continues() -> None:
    store = Store()
    store.create_schedule(
        "session-invalid",
        {"kind": "legacy"},
        {"last_tick": 0},
        ["worker:legacy"],
        ready_nodes=["worker:legacy"],
        schedule_id="schedule-invalid",
    )
    service = DurableAgentService(Provider(), Tools(), store, model="test")

    assert await service.recover() == 0
    assert store.schedules["schedule-invalid"]["status"] == "cancelled"
    assert store.nodes["schedule-invalid"]["worker:legacy"]["state"] == "cancelled"


async def test_full_specialist_browser_graph_advances_every_serialized_worker() -> None:
    class HandoffProvider(Provider):
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if "Worker profile: recon" in request.messages[0].content:
                yield TextDelta(
                    text=(
                        '{"interaction_contract":{"input_locator":"textarea#chat",'
                        '"submit_action":"Enter","frame_context":null,'
                        '"authenticated_session_constraints":"shared profile",'
                        '"input_limit":{"max_characters":500,"source":"dom_attribute",'
                        '"evidence":"textarea maxlength=500"},'
                        '"probe":{"message":"Explain refund eligibility",'
                        '"objective_relevance":"maps refund policy",'
                        '"sent_message_witness":"message echo",'
                        '"response_witness":"assistant reply"},'
                        '"lifecycle":{"current_state":"active","terminal_indicator":'
                        '"turn counter exhausted","resume_action":null,"rating_action":null,'
                        '"new_session_action":null,"cleanup_action":"release browser"},'
                        '"success_indicator":"new assistant message",'
                        '"evidence_refs":["snapshot:before","snapshot:after-send"]}}'
                    )
                )
            else:
                yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()

    class BrowserTools(Tools):
        def describe(self):
            return (
                SimpleNamespace(name="browser_navigate", description="navigate", input_schema={}),
            )

        def effect(self, name):
            return "target_network"

    provider = HandoffProvider()
    store = Store()
    service = DurableAgentService(provider, BrowserTools(), store, model="test")
    profile_ids = (
        "recon",
        "auth-session",
        "injection",
        "data-exposure",
        "business-logic",
        "capability-abuse",
        "adversarial-explorer",
    )
    assignments = tuple(
        WorkerAssignment(
            id=f"specialist-{index}",
            objective="Assess the reviewed target",
            profile_id=profile_id,
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=("browser_navigate",),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(4, 9_000, 60),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
            provider_model=f"worker-model-{index}",
        )
        for index, profile_id in enumerate(profile_ids)
    )

    admitted = service._admit("session", assignments, 7, 0)  # noqa: SLF001

    assert len(admitted.spec.nodes) == 16
    assert len(admitted.spec.edges) == 46
    recon_node = "worker:specialist-0"
    browser_specialist_nodes = {f"worker:specialist-{index}" for index in range(1, 7)}
    recon_edges = {
        (edge.source, edge.target)
        for edge in admitted.spec.edges
        if edge.id.startswith("recon-prerequisite:")
    }
    assert recon_edges == {(recon_node, node) for node in browser_specialist_nodes}
    assert {
        (edge.source, edge.target)
        for edge in admitted.spec.edges
        if edge.id.startswith("target-session-owner:")
    } == {(f"worker:specialist-{index}", f"worker:specialist-{index + 1}") for index in range(1, 6)}

    live = await service.submit("session", assignments, max_workers=7)
    assert live.execution is not None
    await live.execution

    tasks = await service.list_schedule_tasks(live.id)
    assert len(tasks) == 7
    assert {task.status for task in tasks} == {"completed"}
    assert len(provider.requests) == 7
    assert [request.model for request in provider.requests] == [
        f"worker-model-{index}" for index in range(7)
    ]
    assert {
        row["payload"]["provider_model"]
        for row in store.results[live.id].values()
        if str(row["node_id"]).startswith("worker:")
    } == {f"worker-model-{index}" for index in range(7)}
    for request in provider.requests[1:]:
        handoff = next(
            message.content
            for message in request.messages
            if "DURABLE RECON PREDECESSOR RESULT" in message.content
        )
        assert "result_digest: sha256:test" in handoff
        assert "textarea#chat" in handoff


def test_capability_probes_bypass_browser_lifetime_serialization_without_recon_dependency() -> None:
    service = DurableAgentService(Provider(), Tools(), Store(), model="test")

    def assignment(
        task_id: str, profile_id: str, technique_ids: tuple[str, ...]
    ) -> WorkerAssignment:
        return WorkerAssignment(
            id=task_id,
            objective="Assess the reviewed target",
            profile_id=profile_id,
            target_profile_id="target",
            technique_ids=technique_ids,
            guidance_ids=(),
            tool_ids=("browser_chat_exchange",),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(4, 9_000, 60),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )

    recon = assignment("campaign/x/recon", "recon", ())
    probe = assignment("campaign/x/capability-probe/memory", "recon", ("capability/memory",))
    specialist = assignment("campaign/x/auth-session", "auth-session", ("technique-auth",))

    graph = service._graph_spec("wave0", (probe, recon, specialist))  # noqa: SLF001
    admitted = service._admit(  # noqa: SLF001
        "session", (probe, recon, specialist), max_workers=3, delegation_depth=0
    )
    assert len(admitted.spec.nodes) == 8

    probe_node = f"worker:{probe.id}"
    recon_node = f"worker:{recon.id}"
    assert {
        (edge.source, edge.target)
        for edge in graph.edges
        if edge.id.startswith("recon-prerequisite:")
    } == {(recon_node, f"worker:{specialist.id}")}
    assert not any(
        probe_node in (edge.source, edge.target)
        for edge in graph.edges
        if edge.id.startswith("recon-prerequisite:")
    )
    # Regression: local throwaway scopes previously bypassed physical ownership.
    assert any(
        edge.id.startswith("target-session-owner:") and probe_node in (edge.source, edge.target)
        for edge in graph.edges
    )


def test_browser_lifetime_serialization_is_scoped_per_target_and_recon() -> None:
    service = DurableAgentService(Provider(), Tools(), Store(), model="test")

    def assignment(task_id: str, target_id: str, profile_id: str) -> WorkerAssignment:
        return WorkerAssignment(
            id=task_id,
            objective="Assess one reviewed target",
            profile_id=profile_id,
            target_profile_id=target_id,
            technique_ids=(),
            guidance_ids=(),
            tool_ids=("browser_chat_exchange",),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(4, 9_000, 60),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )

    attack_a = assignment("campaign/a/attack", "target-a", "injection")
    recon_b = assignment("campaign/b/recon", "target-b", "recon")
    recon_a = assignment("campaign/a/recon", "target-a", "recon")
    attack_b = assignment("campaign/b/attack", "target-b", "injection")

    graph = service._graph_spec(  # noqa: SLF001
        "wave", (attack_a, recon_b, recon_a, attack_b)
    )
    serialization = {
        (edge.source, edge.target)
        for edge in graph.edges
        if edge.id.startswith(("target-session-owner:", "recon-prerequisite:"))
    }

    assert serialization == {
        (f"worker:{recon_a.id}", f"worker:{attack_a.id}"),
        (f"worker:{recon_b.id}", f"worker:{attack_b.id}"),
    }
    service._admit(  # noqa: SLF001
        "session", (attack_a, recon_b, recon_a, attack_b), max_workers=4, delegation_depth=0
    )


def test_capability_probes_bypass_reviewed_session_lane_serialization() -> None:
    service = DurableAgentService(Provider(), Tools(), Store(), model="test")

    def assignment(
        assignment_id: str, profile: str, technique_ids: tuple[str, ...], tool: str
    ) -> WorkerAssignment:
        return WorkerAssignment(
            id=assignment_id,
            objective="Assess one reviewed target",
            profile_id=profile,
            target_profile_id="target",
            technique_ids=technique_ids,
            guidance_ids=(),
            tool_ids=(tool,),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(4, 9_000, 60),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
            target_session_capacity=1,
        )

    recon = assignment("campaign/x/recon", "recon", ("recon",), "browser_chat_exchange")
    attack = assignment("campaign/x/attack", "injection", ("technique/one",), "target_request")
    probes = tuple(
        assignment(
            f"campaign/x/capability-probe/{kind}",
            "recon",
            (f"capability/{kind}",),
            "target_request",
        )
        for kind in ("memory", "tools")
    )

    graph = service._graph_spec("wave", (recon, attack, *probes))  # noqa: SLF001
    serialization = {
        (edge.source, edge.target)
        for edge in graph.edges
        if edge.id.startswith("target-session-owner:")
    }

    # Every target-using assignment, including probes, gets a physical lane.
    probe_nodes = {f"worker:{probe.id}" for probe in probes}
    assert probe_nodes <= {node for edge in serialization for node in edge}
    assert len(serialization) == len(probes) + 1


def test_verified_target_capacity_two_builds_two_independent_lanes() -> None:
    service = DurableAgentService(Provider(), Tools(), Store(), model="test")
    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/x/worker-{index}",
            objective="Assess one reviewed target",
            profile_id="injection",
            target_profile_id="target",
            technique_ids=(f"technique-{index}",),
            guidance_ids=(),
            tool_ids=("target_request",),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(4, 9_000, 60),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
            target_session_capacity=2,
        )
        for index in range(4)
    )

    graph = service._graph_spec("wave", assignments)  # noqa: SLF001

    assert {
        (edge.source, edge.target)
        for edge in graph.edges
        if edge.id.startswith("target-session-owner:")
    } == {
        (f"worker:{assignments[0].id}", f"worker:{assignments[2].id}"),
        (f"worker:{assignments[1].id}", f"worker:{assignments[3].id}"),
    }


@pytest.mark.parametrize("target_tool_name", ["target_request", "browser_chat_exchange"])
async def test_target_assignment_releases_cleanup_before_next_assignment(
    target_tool_name: str,
) -> None:
    first_target_started = asyncio.Event()
    release_first_target = asyncio.Event()
    unrelated_completed = asyncio.Event()
    first_cleanup_completed = asyncio.Event()
    events: list[str] = []

    class LifetimeProvider(Provider):
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def stream(self, request):
            count = self.calls.get(request.budget_id, 0)
            self.calls[request.budget_id] = count + 1
            if request.budget_id == "campaign/x/offline":
                events.append("offline")
                unrelated_completed.set()
                yield TextDelta(text='{"finding":"offline complete"}')
                yield ModelCompleted()
                return
            if count < 2:
                yield ToolCall(
                    call_id=f"call-{request.budget_id}-{count}",
                    name=target_tool_name,
                    arguments={"input": f"turn-{count}"},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield TextDelta(text='{"finding":"target complete"}')
            yield ModelCompleted()

    class LifetimeTools(Tools):
        def describe(self):
            return (SimpleNamespace(name=target_tool_name, description="request", input_schema={}),)

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            turn = str(arguments["input"])
            events.append(f"target:{context.agent_id}:{turn}")
            if context.agent_id == "campaign/x/first-target" and turn == "turn-0":
                first_target_started.set()
                await release_first_target.wait()
            if context.agent_id == "campaign/x/attack":
                assert first_cleanup_completed.is_set()
            return ToolResult(
                content='{"response":"acknowledged"}',
                metadata={"target_session_id": f"api:{context.agent_id}"},
            )

    async def cleanup_assignment(assignment_id: str) -> None:
        events.append(f"cleanup:{assignment_id}")
        if assignment_id == "campaign/x/first-target":
            first_cleanup_completed.set()

    def assignment(
        assignment_id: str,
        *,
        target_tool: bool,
        technique_id: str,
    ) -> WorkerAssignment:
        return WorkerAssignment(
            id=assignment_id,
            objective="Exercise one reviewed assignment",
            profile_id="recon" if "capability-probe" in assignment_id else "injection",
            target_profile_id="target",
            technique_ids=(technique_id,),
            guidance_ids=(),
            tool_ids=(target_tool_name,) if target_tool else (),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",) if target_tool else ("session",),
            budget=AssignmentBudget(max_turns=6, max_tokens=8_000, max_seconds=60),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )

    first = assignment(
        "campaign/x/first-target",
        target_tool=True,
        technique_id="technique/first",
    )
    attack = assignment("campaign/x/attack", target_tool=True, technique_id="technique/attack")
    offline = assignment("campaign/x/offline", target_tool=False, technique_id="analysis/offline")
    service = DurableAgentService(
        LifetimeProvider(),
        LifetimeTools(),
        Store(),
        model="test",
        campaign_cleanup=cleanup_assignment,
    )

    live = await service.submit("session", (first, attack, offline), max_workers=3)
    assert live.execution is not None
    await asyncio.wait_for(first_target_started.wait(), timeout=1)
    await asyncio.wait_for(unrelated_completed.wait(), timeout=1)
    assert not any(event.startswith("target:campaign/x/attack") for event in events)

    release_first_target.set()
    await asyncio.wait_for(live.execution, timeout=2)

    assert events.index("cleanup:campaign/x/first-target") < next(
        index for index, event in enumerate(events) if event.startswith("target:campaign/x/attack")
    )
    assert sum(event.startswith("target:campaign/x/first-target") for event in events) == 2
    assert {task.status for task in await service.list_schedule_tasks(live.id)} == {"completed"}


async def test_failed_target_assignment_is_cleaned_before_successor_runs() -> None:
    first_cleaned = asyncio.Event()

    class FailureProvider(Provider):
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def stream(self, request):
            if request.budget_id.endswith("/first"):
                yield ModelError(message="provider rejected request", retryable=False)
                return
            calls = self.calls.get(request.budget_id, 0)
            self.calls[request.budget_id] = calls + 1
            if calls == 0:
                yield ToolCall(call_id="target", name="target_request", arguments={"input": "go"})
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield TextDelta(text='{"finding":"done"}')
            yield ModelCompleted()

    class TargetTools(Tools):
        def describe(self):
            return (SimpleNamespace(name="target_request", description="request", input_schema={}),)

        async def invoke(self, name, context, arguments):
            assert first_cleaned.is_set()
            return ToolResult(content='{"response":"ok"}')

    async def cleanup_assignment(assignment_id: str) -> None:
        if assignment_id.endswith("/first"):
            first_cleaned.set()

    def assignment(suffix: str) -> WorkerAssignment:
        return WorkerAssignment(
            id=f"campaign/failure/{suffix}",
            objective="Assess one target",
            profile_id="injection",
            target_profile_id="target",
            technique_ids=(f"technique/{suffix}",),
            guidance_ids=(),
            tool_ids=("target_request",),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(4, 9_000, 60),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )

    service = DurableAgentService(
        FailureProvider(), TargetTools(), Store(), model="test", campaign_cleanup=cleanup_assignment
    )
    live = await service.submit(
        "session", (assignment("first"), assignment("second")), max_workers=2
    )

    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=2)
    assert first_cleaned.is_set()


async def test_reopen_cleans_terminal_predecessor_before_ready_successor() -> None:
    cleaned = asyncio.Event()

    class RecoveredProvider(Provider):
        async def stream(self, request):
            assert cleaned.is_set()
            yield TextDelta(text='{"finding":"resumed"}')
            yield ModelCompleted()

    async def cleanup_assignment(assignment_id: str) -> None:
        if assignment_id.endswith("/first"):
            cleaned.set()

    def assignment(suffix: str) -> WorkerAssignment:
        return WorkerAssignment(
            id=f"campaign/crash/{suffix}",
            objective="Assess one target",
            profile_id="injection",
            target_profile_id="target",
            technique_ids=(f"technique/{suffix}",),
            guidance_ids=(),
            tool_ids=("target_request",),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(4, 9_000, 60),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )

    assignments = (assignment("first"), assignment("second"))
    store = Store()
    creator = DurableAgentService(Provider(), Tools(), store, model="test")
    live = await creator.submit("session", assignments, max_workers=2, start_immediately=False)
    start = live.scheduler.claim("start", "scheduler", 1)
    live.scheduler.complete(start, "start", 2)
    first_node = f"worker:{assignments[0].id}"
    handle = live.scheduler.claim(first_node, "crashed-owner", 3)
    live.scheduler.complete(handle, "sha256:first", 4)
    checkpoint = live.scheduler.checkpoint()
    store.schedules[live.id]["checkpoint"] = checkpoint
    store.schedules[live.id]["status"] = "paused"
    for node_id, state in checkpoint["nodes"].items():
        store.nodes[live.id][node_id]["state"] = state["status"]

    recovered_service = DurableAgentService(
        RecoveredProvider(),
        Tools(),
        store,
        model="test",
        campaign_cleanup=cleanup_assignment,
    )
    await recovered_service.reopen(live.id)
    recovered = await recovered_service.resume_schedule(live.id)

    assert recovered.execution is not None
    await asyncio.wait_for(recovered.execution, timeout=2)
    assert cleaned.is_set()


def test_api_baseline_agendas_use_independent_verified_capacity_lanes() -> None:
    service = DurableAgentService(Provider(), Tools(), Store(), model="test")
    assignments = []
    for agenda in ("one", "two"):
        predecessor = None
        for stage in ("control", "direct", "replay"):
            assignment_id = f"baseline/{agenda}/{stage}"
            assignments.append(
                WorkerAssignment(
                    id=assignment_id,
                    objective=f"{stage} {agenda}",
                    profile_id="injection",
                    target_profile_id="target",
                    technique_ids=(f"technique-{agenda}",),
                    guidance_ids=(),
                    tool_ids=("target_request",),
                    skill_ids=(),
                    provider_id="provider",
                    authority_ids=("target:reviewed",),
                    budget=AssignmentBudget(None, None, 60),
                    input_schema='{"type":"object"}',
                    output_schema='{"type":"object"}',
                    agenda_id=f"agenda/{agenda}",
                    execution_stage=stage,
                    pair_id=f"control-{agenda}",
                    predecessor_assignment_id=predecessor,
                    oracle_digest="sha256:" + "3" * 64,
                    semantic_technique_id=f"technique-{agenda}",
                    target_session_policy="same-reviewed-session",
                    target_session_capacity=2,
                    comparison_policy="reviewed-benign-differential-v1",
                    reviewed_adapter_digest="sha256:" + "a" * 64,
                    comparison_dimensions=(
                        ("modality", "text"),
                        ("turn", "single"),
                        ("language", "und"),
                    ),
                )
            )
            predecessor = assignment_id

    hostile_order = (
        assignments[5],
        assignments[1],
        assignments[3],
        assignments[2],
        assignments[0],
        assignments[4],
    )
    graph = service._graph_spec("baseline", hostile_order)  # noqa: SLF001
    service._admit(  # noqa: SLF001
        "session", hostile_order, max_workers=2, delegation_depth=0
    )
    dependencies = {
        (edge.source, edge.target, edge.kind.value)
        for edge in graph.edges
        if edge.id.startswith("baseline-agenda:")
    }

    assert dependencies == {
        (
            f"worker:{assignments[index - 1].id}",
            f"worker:{assignments[index].id}",
            "dependency",
        )
        for index in (1, 2, 4, 5)
    }
    assert not any(
        source.split("/")[1] != target.split("/")[1] for source, target, _kind in dependencies
    )


def test_baseline_target_lanes_never_cross_target_identity() -> None:
    service = DurableAgentService(Provider(), Tools(), Store(), model="test")

    def assignment(target_id: str, stage: str, predecessor: str | None) -> WorkerAssignment:
        return WorkerAssignment(
            id=f"baseline/{target_id}/{stage}",
            objective=f"{stage} {target_id}",
            profile_id="injection",
            target_profile_id=target_id,
            technique_ids=(f"technique-{target_id}",),
            guidance_ids=(),
            tool_ids=("target_request",),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(None, None, 60),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
            agenda_id=f"agenda/{target_id}",
            execution_stage=stage,
            pair_id=f"pair/{target_id}",
            predecessor_assignment_id=predecessor,
            oracle_digest="sha256:" + "3" * 64,
            semantic_technique_id=f"technique-{target_id}",
            target_session_policy="same-reviewed-session",
            target_session_capacity=1,
            comparison_policy="reviewed-benign-differential-v1",
            reviewed_adapter_digest="sha256:" + "a" * 64,
            comparison_dimensions=(("modality", "text"),),
        )

    control_a = assignment("target-a", "control", None)
    control_b = assignment("target-b", "control", None)
    direct_a = assignment("target-a", "direct", control_a.id)
    direct_b = assignment("target-b", "direct", control_b.id)
    graph = service._graph_spec(  # noqa: SLF001
        "baseline", (control_a, control_b, direct_a, direct_b)
    )

    assert not any(
        edge.source.startswith("worker:baseline/target-a/")
        and edge.target.startswith("worker:baseline/target-b/")
        or edge.source.startswith("worker:baseline/target-b/")
        and edge.target.startswith("worker:baseline/target-a/")
        for edge in graph.edges
    )


async def test_recon_handoff_is_reloaded_from_durable_result_after_restart() -> None:
    store = Store()
    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/{profile_id}",
            objective="Assess",
            profile_id=profile_id,
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=("browser_navigate",),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(4, 4_000, 60),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for profile_id in ("recon", "injection")
    )
    store.results["persisted-schedule"] = {
        "worker:campaign/recon": {
            "node_id": "worker:campaign/recon",
            "payload": {
                "content": '{"input":"textarea#chat","submit":"Enter"}',
                "structured": None,
            },
            "payload_digest": "sha256:persisted-recon",
            "schema_id": "schema/result",
        }
    }

    restarted = DurableAgentService(Provider(), Tools(), store, model="test")
    reloaded_live = SimpleNamespace(id="persisted-schedule", assignments=assignments)
    message = await restarted._recon_predecessor_message(  # noqa: SLF001
        reloaded_live, assignments[1]
    )

    assert message is not None
    assert "result_digest: sha256:persisted-recon" in message
    assert "textarea#chat" in message
    assert "submit" in message

    del store.results["persisted-schedule"]["worker:campaign/recon"]["payload_digest"]
    unverified = await restarted._recon_predecessor_message(  # noqa: SLF001
        reloaded_live, assignments[1]
    )
    assert unverified is not None
    assert "EVALUATION UNCERTAIN — RECON PREDECESSOR UNVERIFIABLE" in unverified
    assert "Continue safe authorized discovery" in unverified


async def test_durable_service_persists_before_execution_and_reopens_projection() -> None:
    store = Store()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    events = [
        event
        async for event in service.start_swarm(
            SwarmRequest(session_id="session-1", objective="Assess target", max_workers=2)
        )
    ]
    assert store.created_before_claim
    assert store.schedules["schedule-1"]["graph"]["max_workers"] == 2
    assert events[0].state == "admitted"
    assert store.nodes["schedule-1"]["reducer"]["state"] == "completed"
    tasks = await service.list_tasks("session-1")
    assert {task.status for task in tasks} == {"completed"}
    reopened = await DurableAgentService(Provider(), Tools(), store, model="test").reopen(
        "schedule-1"
    )
    assert reopened.scheduler.status("reducer").value == "completed"


async def test_projection_exposes_retry_number_and_replaced_attempt() -> None:
    store = Store()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    assignments = await service._plan("Assess target", 2)  # noqa: SLF001
    live = await service.submit(
        "session-1", assignments, max_workers=2, start_immediately=False, minimum_workers=1
    )
    node_id = next(iter(store.nodes[live.id]))
    first = store.claim(
        live.id,
        node_id,
        "agent-1",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    store.settle_attempt(
        first.id,
        first.generation,
        "failed",
        reason="transient provider failure",
        checkpoint={"worker": "failed"},
    )
    store.retry_failed(
        live.id,
        node_id,
        checkpoint={"worker": "ready"},
        expected_event_count=store.schedules[live.id]["event_count"],
    )
    replacement = store.claim(
        live.id,
        node_id,
        "agent-2",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    replacement.replaced_attempt_id = first.id

    projection = await service._projection(live.id)  # noqa: SLF001

    projected = {attempt.attempt_id: attempt for attempt in projection.attempts}
    assert projected[first.id].retry_number == 0
    assert projected[first.id].replaced_attempt_id is None
    assert projected[replacement.id].retry_number == 1
    assert projected[replacement.id].replaced_attempt_id == first.id


async def test_reopen_finalizes_running_schedule_with_terminal_reducer_and_cleanup() -> None:
    store = Store()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    _ = [
        event
        async for event in service.start_swarm(
            SwarmRequest(session_id="session-1", objective="Assess target", max_workers=2)
        )
    ]
    assert store.schedules["schedule-1"]["status"] == "running"
    completed: list[str] = []

    def complete_schedule(schedule_id: str) -> None:
        assert all(
            node["state"] in {"completed", "failed", "cancelled"}
            for node in store.nodes[schedule_id].values()
        )
        store.schedules[schedule_id]["status"] = "completed"
        completed.append(schedule_id)

    store.complete_schedule = complete_schedule  # type: ignore[attr-defined]

    reopened = await DurableAgentService(Provider(), Tools(), store, model="test").reopen(
        "schedule-1"
    )

    assert reopened.execution is None
    assert completed == ["schedule-1"]
    assert store.schedules["schedule-1"]["status"] == "completed"


async def test_reopen_restores_the_reviewed_worker_concurrency_limit() -> None:
    store = Store()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    assignments = await service._plan("Assess target", 2)  # noqa: SLF001
    live = await service.submit(
        "session-1", assignments, max_workers=8, start_immediately=False, minimum_workers=1
    )

    reopened_service = DurableAgentService(Provider(), Tools(), store, model="different-default")
    observed: list[int] = []

    async def capture_resume(_live, _running, max_workers):
        observed.append(max_workers)

    reopened_service._resume = capture_resume  # type: ignore[method-assign]  # noqa: SLF001
    reopened = await reopened_service.reopen(live.id)
    assert reopened.execution is not None
    await reopened.execution

    assert observed == [8]
    assert {assignment.provider_model for assignment in reopened.assignments} == {"test"}


async def test_execution_width_is_independent_from_admitted_worker_count() -> None:
    store = Store()
    provider = ConcurrencyProvider()
    service = DurableAgentService(provider, Tools(), store, model="test")
    assignments = await service._plan("Assess target", 2)  # noqa: SLF001

    live = await service.submit("session-1", assignments, max_workers=1)
    assert live.execution is not None
    await live.execution

    assert len(assignments) == 2
    assert provider.maximum_active == 1


async def test_topology_can_exceed_eight_while_execution_width_remains_capped() -> None:
    store = Store()
    provider = ConcurrencyProvider()
    service = DurableAgentService(provider, Tools(), store, model="test")
    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/task-{index}",
            objective=f"Assess frontier {index}",
            profile_id="injection",
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for index in range(12)
    )

    live = await service.submit("session", assignments, max_workers=3)
    assert live.execution is not None
    await live.execution

    assert len(live.assignments) == 12
    assert provider.maximum_active == 3


async def test_worker_dispatch_follows_scheduler_fair_ready_order() -> None:
    class OrderingProvider(Provider):
        def __init__(self) -> None:
            self.objectives: list[str] = []

        async def stream(self, request):
            self.objectives.append(request.messages[-1].content)
            yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()

    store = Store()
    provider = OrderingProvider()
    service = DurableAgentService(provider, Tools(), store, model="test")
    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/task-{suffix}",
            objective=f"Assess frontier {suffix}",
            profile_id="injection",
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for suffix in ("z", "a", "m")
    )

    live = await service.submit("session", assignments, max_workers=1)
    assert live.execution is not None
    await live.execution

    assert [
        next(suffix for suffix in ("a", "m", "z") if f"frontier {suffix}" in prompt)
        for prompt in provider.objectives
    ] == ["a", "m", "z"]


async def test_fresh_worker_retry_runs_after_untouched_ready_assignments() -> None:
    class RetryOrderingProvider(Provider):
        def __init__(self) -> None:
            self.order: list[str] = []
            self.failed_a = False

        async def stream(self, request):
            prompt = request.messages[-1].content
            suffix = next(suffix for suffix in ("a", "b", "c") if f"frontier {suffix}" in prompt)
            self.order.append(suffix)
            if suffix == "a" and not self.failed_a:
                self.failed_a = True
                yield ModelError(message="replace worker", retryable=False)
                return
            yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()

    store = Store()
    provider = RetryOrderingProvider()
    service = DurableAgentService(provider, Tools(), store, model="test")
    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/task-{suffix}",
            objective=f"Assess frontier {suffix}",
            profile_id="injection",
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for suffix in ("a", "b", "c")
    )

    live = await service.submit("session", assignments, max_workers=1)
    assert live.execution is not None
    await live.execution

    assert provider.order == ["a", "b", "c", "a"]
    tasks = await service.list_schedule_tasks(live.id)
    assert all(task.status == "completed" for task in tasks)


async def test_provider_error_code_and_message_surface_in_attempt_reason() -> None:
    class RejectingProvider(Provider):
        async def stream(self, request):
            yield ModelError(
                message="HTTP 400: model rejected the request",
                code="bad_request",
                retryable=False,
            )

    class ReasonRecordingStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.reasons: list[str] = []

        def settle_attempt(self, attempt_id, generation, outcome, *, reason, checkpoint, **kwargs):
            if outcome == "failed":
                self.reasons.append(reason)
            super().settle_attempt(
                attempt_id, generation, outcome, reason=reason, checkpoint=checkpoint, **kwargs
            )

    store = ReasonRecordingStore()
    service = DurableAgentService(RejectingProvider(), Tools(), store, model="test")
    assignment = WorkerAssignment(
        id="campaign/task-a",
        objective="Assess frontier a",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )

    live = await service.submit("session", (assignment,), max_workers=1, minimum_workers=1)
    assert live.execution is not None
    await live.execution

    tasks = await service.list_schedule_tasks(live.id)
    assert all(task.status == "failed" for task in tasks)
    assert store.reasons
    assert all(
        reason.startswith("provider rejected the request (bad_request): HTTP 400")
        for reason in store.reasons
    )


async def test_schedule_terminal_failure_kinds_reports_provider_quota() -> None:
    class QuotaProvider(Provider):
        async def stream(self, request):
            yield ModelError(
                message="HTTP 402: insufficient balance",
                code="provider_quota_exhausted",
                retryable=False,
                failure_kind=ProviderFailureKind.QUOTA,
            )

    store = Store()
    service = DurableAgentService(QuotaProvider(), Tools(), store, model="test")
    assignment = WorkerAssignment(
        id="campaign/task-quota",
        objective="Assess frontier quota",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )

    live = await service.submit("session", (assignment,), max_workers=1, minimum_workers=1)
    assert live.execution is not None
    await live.execution

    tasks = await service.list_schedule_tasks(live.id)
    assert all(task.status == "failed" for task in tasks)
    assert await service.schedule_terminal_failure_kinds(live.id) == (
        RetryFailureKind.BUDGET.value,
    )


async def test_exhausted_retryable_infrastructure_keeps_attempt_open() -> None:
    identity = {
        "campaign_id": "campaign/transient-exhausted",
        "target_id": "target",
        "target_generation": "generation-1",
        "scenario_fingerprint": "scenario-1",
        "technique_id": "technique/one",
        "controller_id": "direct",
        "payload_family": "baseline",
        "requested_language": "und",
        "mutation_id": "baseline",
    }

    class TransientProvider(Provider):
        async def stream(self, request):
            if "transient-exhausted" in request.messages[-1].content:
                yield ModelError(
                    message="provider timed out before target execution",
                    code="read_timeout",
                    retryable=True,
                )
                return
            yield TextDelta(text='{"finding":"sibling completed"}')
            yield ModelCompleted()

    class AttemptStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.terminalizations = []

        def bind_technique_attempt(self, *args, **metadata):
            return "technique-attempt-1"

        def terminalize_technique_attempt(self, attempt_id, state, **metadata):
            raise AssertionError(
                "retryable infrastructure must not terminalize an unexecuted technique"
            )

    assignment = WorkerAssignment(
        id="campaign/transient-exhausted/worker",
        objective=(
            "execute\n\nSealed candidate identities: "
            + json.dumps([identity], sort_keys=True, separators=(",", ":"))
        ),
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("technique/one",),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_seconds=60),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    store = AttemptStore()
    service = DurableAgentService(
        TransientProvider(),
        Tools(),
        store,
        model="test",
        max_provider_retries=0,
    )

    sibling = WorkerAssignment(
        id="campaign/transient-exhausted/sibling",
        objective="sibling-order-only-work",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_seconds=60),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit(
        "session-transient-exhausted",
        (assignment, sibling),
        max_workers=1,
        minimum_workers=1,
    )
    assert live.execution is not None
    await live.execution

    tasks = {task.id: task for task in await service.list_schedule_tasks(live.id)}
    assert tasks[sibling.id].status == "completed"
    assert tasks[assignment.id].status == "failed"


async def test_exhausted_protocol_invalid_attempt_preserves_regeneration_input() -> None:
    identity = {
        "campaign_id": "campaign/protocol-invalid",
        "target_id": "target",
        "target_generation": "generation-1",
        "scenario_fingerprint": "scenario-1",
        "technique_id": "technique/one",
        "controller_id": "direct",
        "payload_family": "baseline",
        "requested_language": "und",
        "mutation_id": "baseline",
    }
    rendered_input = "payload that exceeds the reviewed target limit"
    constraint = {
        "kind": "string_length",
        "path": "/input",
        "maximum": 12,
        "actual": len(rendered_input),
        "satisfied": False,
    }

    class ProtocolProvider(Provider):
        async def stream(self, request):
            if request.messages[-1].role == "tool":
                yield TextDelta(text='{"outcome":"request rejected by protocol"}')
                yield ModelCompleted()
                return
            yield ToolCall(
                call_id=f"protocol-invalid-{request.budget_id}",
                name="target_request",
                arguments={"input": rendered_input},
            )
            yield ModelCompleted(finish_reason="tool_calls")

    class ProtocolTools(Tools):
        def describe(self):
            return (
                ToolSpec(
                    name="target_request",
                    description="target",
                    input_schema={"type": "object"},
                    interaction_role=ToolInteractionRole.TARGET_EXCHANGE,
                ),
            )

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            return ToolResult(
                content="request input exceeds maximum length",
                is_error=True,
                metadata={
                    "attempt_disposition": "protocol_invalid",
                    "disposition_reason": "rendered request exceeded reviewed maximum",
                    "no_target_effect": True,
                    "target_call_budget_charge": 0,
                    "limit_interaction_count": 0,
                    "request_constraint_observations": [constraint],
                    "target_exchanges": [
                        {
                            "interaction": False,
                            "cleanup": False,
                            "outcome": "request_constraint_violation",
                            "request": dict(arguments),
                        }
                    ],
                },
            )

    class AttemptStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.terminalizations = []

        def bind_technique_attempt(self, *args, **metadata):
            return "technique-attempt-1"

        def terminalize_technique_attempt(self, attempt_id, state, **metadata):
            self.terminalizations.append((attempt_id, state, metadata))

        def commit_partial(self, attempt_id, generation, *, result, applied_message_ids=()):
            del applied_message_ids
            lease = self.attempts[attempt_id]
            assert lease.generation == generation
            partials = self.results.setdefault(lease.schedule_id, {})
            partials[f"partial:{attempt_id}:{len(partials)}"] = {
                "node_id": lease.node_id,
                "generation": generation,
                "schema_id": "schema/partial-result",
                "payload": result,
            }

    assignment = WorkerAssignment(
        id="campaign/protocol-invalid/worker",
        objective=(
            "execute\n\nSealed candidate identities: "
            + json.dumps([identity], sort_keys=True, separators=(",", ":"))
        ),
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("technique/one",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_seconds=60),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    store = AttemptStore()
    service = DurableAgentService(ProtocolProvider(), ProtocolTools(), store, model="test")

    live = await service.submit(
        "session-protocol-invalid", (assignment,), max_workers=1, minimum_workers=1
    )
    assert live.execution is not None
    await live.execution

    assert len(store.terminalizations) == 1
    attempt_id, state, metadata = store.terminalizations[0]
    assert attempt_id == "technique-attempt-1"
    assert state == "blocked"
    assert metadata["disposition"] == "protocol_invalid"
    assert metadata["disposition_reason"] == "rendered request exceeded reviewed maximum"
    protocol_invalid_request = metadata["strategy_state"]["protocol_invalid_request"]
    assert protocol_invalid_request["rendered_input"] == rendered_input
    assert protocol_invalid_request["constraint"] == constraint
    assert protocol_invalid_request["parent_candidate_id"] == "technique/one"
    assert metadata["evidence_refs"]


async def test_durable_provider_failure_finalizes_receipt_without_replaying_target() -> None:
    class PartialStore(Store):
        def commit_partial(self, attempt_id, generation, *, result, applied_message_ids=()):
            del applied_message_ids
            lease = self.attempts[attempt_id]
            assert lease.generation == generation
            partials = self.results.setdefault(lease.schedule_id, {})
            partials[f"partial:{attempt_id}:{len(partials)}"] = {
                "node_id": lease.node_id,
                "generation": generation,
                "schema_id": "schema/partial-result",
                "payload": result,
            }

    class FinalizerProvider(Provider):
        def __init__(self) -> None:
            self.calls = 0
            self.requests = []

        async def stream(self, request):
            self.calls += 1
            self.requests.append(request)
            if self.calls == 1:
                yield ToolCall(
                    call_id="target-once",
                    name="target_request",
                    arguments={"input": "one authorized attempt"},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            if self.calls == 2:
                yield ModelError(
                    message="provider timed out while formatting the result",
                    code="read_timeout",
                    retryable=True,
                )
                return
            yield TextDelta(text='{"finding":"receipt finalized"}')
            yield ModelCompleted()

    class TargetTools(Tools):
        def __init__(self) -> None:
            self.calls = 0

        def describe(self):
            return (
                ToolSpec(
                    name="target_request",
                    description="target",
                    input_schema={"type": "object"},
                    interaction_role=ToolInteractionRole.TARGET_EXCHANGE,
                ),
            )

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            self.calls += 1
            return ToolResult(
                content='{"response":"observed"}',
                metadata={
                    "target_session_id": "api:one",
                    "target_exchanges": [{"interaction": True, "cleanup": False}],
                },
            )

    assignment = WorkerAssignment(
        id="campaign/finalizer/worker",
        objective="Perform one target attempt and finalize its receipt",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("technique/finalizer",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_seconds=60),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    provider = FinalizerProvider()
    tools = TargetTools()
    store = PartialStore()
    service = DurableAgentService(
        provider,
        tools,
        store,
        model="test",
        max_provider_retries=0,
    )

    live = await service.submit(
        "session-finalizer", (assignment,), max_workers=1, minimum_workers=1
    )
    assert live.execution is not None
    await live.execution

    tasks = await service.list_schedule_tasks(live.id)
    assert tasks[0].status == "completed"
    assert tools.calls == 1
    assert provider.calls == 3
    assert provider.requests[-1].tools == []
    failed_attempts = [attempt for attempt in store.attempts.values() if attempt.state == "failed"]
    assert [attempt.failure_kind for attempt in failed_attempts] == [
        RetryFailureKind.PROVIDER_TRANSIENT.value
    ]
    retry_records = [
        record["payload"]
        for record in store.results[live.id].values()
        if record["schema_id"] == "schema/partial-result"
        and record["payload"].get("retry_failure_kind")
    ]
    assert retry_records[-1]["retry_failure_kind"] == RetryFailureKind.PROVIDER_TRANSIENT.value


@pytest.mark.parametrize(
    ("failure_kind", "code"),
    [
        (ProviderFailureKind.AUTHORIZATION, "provider_auth_error"),
        (ProviderFailureKind.QUOTA, "provider_quota_exhausted"),
        (ProviderFailureKind.CONFIGURATION, "provider_configuration_error"),
    ],
)
async def test_hard_provider_failure_after_effect_keeps_exact_attempt_observed(
    failure_kind: ProviderFailureKind, code: str
) -> None:
    identity = {
        "campaign_id": "campaign/hard-finalizer",
        "target_id": "target",
        "target_generation": "generation-1",
        "scenario_fingerprint": "scenario-1",
        "technique_id": "technique/one",
        "controller_id": "direct",
        "payload_family": "baseline",
        "requested_language": "und",
        "mutation_id": "baseline",
    }

    class HardFailureProvider(Provider):
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield ToolCall(
                    call_id="target-once",
                    name="target_request",
                    arguments={"input": "one authorized attempt"},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield ModelError(
                message="hard provider failure during finalization",
                code=code,
                retryable=True,
                failure_kind=failure_kind,
            )

    class TargetTools(Tools):
        def __init__(self) -> None:
            self.calls = 0

        def describe(self):
            return (
                ToolSpec(
                    name="target_request",
                    description="target",
                    input_schema={"type": "object"},
                    interaction_role=ToolInteractionRole.TARGET_EXCHANGE,
                ),
            )

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            self.calls += 1
            return ToolResult(
                content='{"response":"observed"}',
                metadata={
                    "target_session_id": "api:one",
                    "target_exchanges": [{"interaction": True, "cleanup": False}],
                },
            )

    class AttemptStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.observed = []
            self.terminalized = []
            self.settlement_checkpoint = None
            self.partial_result = None

        def bind_technique_attempt(self, *args, **metadata):
            return "technique-attempt-1"

        def observe_technique_attempt(self, attempt_id, **metadata):
            self.observed.append((attempt_id, metadata))

        def terminalize_technique_attempt(self, *args, **metadata):
            self.terminalized.append((args, metadata))

        def commit_partial(self, attempt_id, generation, *, result, applied_message_ids=()):
            del attempt_id, generation, applied_message_ids
            self.partial_result = result

        def settle_attempt(self, attempt_id, generation, outcome, *, reason, checkpoint, **kwargs):
            self.settlement_checkpoint = checkpoint
            super().settle_attempt(
                attempt_id,
                generation,
                outcome,
                reason=reason,
                checkpoint=checkpoint,
                **kwargs,
            )

    assignment = WorkerAssignment(
        id="campaign/hard-finalizer/worker",
        objective=(
            "execute\n\nSealed candidate identities: "
            + json.dumps([identity], sort_keys=True, separators=(",", ":"))
        ),
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("technique/one",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_seconds=60),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    provider = HardFailureProvider()
    tools = TargetTools()
    store = AttemptStore()
    service = DurableAgentService(provider, tools, store, model="test")

    live = await service.submit(
        "session-hard-finalizer", (assignment,), max_workers=1, minimum_workers=1
    )
    assert live.execution is not None
    await live.execution

    assert provider.calls == 2
    assert tools.calls == 1
    assert store.terminalized == []
    assert len(store.observed) == 1
    attempt_id, metadata = store.observed[0]
    assert attempt_id == "technique-attempt-1"
    assert metadata["evidence_refs"] == ("target-call:target-once",)
    node_checkpoint = store.settlement_checkpoint["nodes"][f"worker:{assignment.id}"]
    assert node_checkpoint["status"] == "failed"
    assert store.partial_result["worker_checkpoint"]["evaluation_uncertainties"]


@pytest.mark.parametrize("replacement_delivers", [False, True])
async def test_worker_cannot_complete_target_assignment_without_target_effect(
    replacement_delivers: bool,
) -> None:
    class NarrationOnlyProvider(Provider):
        def __init__(self) -> None:
            self.worker_calls = 0

        async def stream(self, request):
            self.worker_calls += 1
            if replacement_delivers and self.worker_calls == 2:
                yield ToolCall(call_id="replacement-target", name="target_request", arguments={})
                yield ModelCompleted()
                return
            yield TextDelta(text='{"control_outcome":"negative"}')
            yield ModelCompleted()

    class TargetTools(Tools):
        def __init__(self):
            self.calls = 0

        def describe(self):
            return (SimpleNamespace(name="target_request", description="request", input_schema={}),)

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            self.calls += 1
            return ToolResult(
                content='{"response":"observed"}',
                metadata={
                    "target_session_id": "api:replacement",
                    "target_exchanges": [{"interaction": True, "cleanup": False}],
                },
            )

    class FailureStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.failure_reasons: list[str] = []

        def settle_attempt(self, attempt_id, generation, outcome, *, reason, checkpoint, **kwargs):
            if outcome == "failed":
                self.failure_reasons.append(reason)
            super().settle_attempt(
                attempt_id,
                generation,
                outcome,
                reason=reason,
                checkpoint=checkpoint,
                **kwargs,
            )

    provider = NarrationOnlyProvider()
    store = FailureStore()
    tools = TargetTools()
    service = DurableAgentService(provider, tools, store, model="test")
    assignment = WorkerAssignment(
        id="campaign/narration-only",
        objective="Execute one target control",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("control",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )

    live = await service.submit("session", (assignment,), max_workers=1, minimum_workers=1)
    assert live.execution is not None
    await live.execution

    tasks = await service.list_schedule_tasks(live.id)
    assert tasks[0].status == ("completed" if replacement_delivers else "failed")
    # Retry missing delivery through the ordinary stall policy, but never claim
    # successful coverage from a sequence of narration-only workers.
    assert 1 < provider.worker_calls <= 5
    assert tools.calls == int(replacement_delivers)
    assert store.failure_reasons
    assert all("missing_target_effect" in reason for reason in store.failure_reasons)


async def test_target_adapter_circuit_defers_related_work_and_releases_sibling() -> None:
    class TargetProvider(Provider):
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def stream(self, request):
            count = self.calls.get(request.budget_id, 0)
            self.calls[request.budget_id] = count + 1
            if (
                request.budget_id.endswith("b-healthy")
                and count
                or request.budget_id.endswith("a-unhealthy")
                and count >= 2
            ):
                yield TextDelta(text='{"finding":"healthy target completed"}')
                yield ModelCompleted()
                return
            yield ToolCall(
                call_id=f"call-{request.budget_id}-{len(request.messages)}",
                name="target_request",
                arguments={"input": "same request"},
            )
            yield ModelCompleted(finish_reason="tool_calls")

    class TargetTools(Tools):
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.agent_calls: dict[str, int] = {}

        def describe(self):
            return (SimpleNamespace(name="target_request", description="request", input_schema={}),)

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            self.calls.append(context.agent_id)
            count = self.agent_calls.get(context.agent_id, 0)
            self.agent_calls[context.agent_id] = count + 1
            if context.agent_id.endswith("unhealthy") and count == 0:
                return ToolResult(
                    content="reviewed API response pointer missing at step chat",
                    is_error=True,
                )
            return ToolResult(
                content='{"response":"acknowledged"}',
                metadata={"target_session_id": "api:healthy"},
            )

    class TargetStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.reasons: list[str] = []
            self.adapter_circuit_state: str | None = None
            self.adapter_retry_at: datetime | None = None

        def admit_target_adapter_probe(self, _scope, **_):
            if self.adapter_circuit_state is None:
                return "closed"
            if self.adapter_circuit_state == "open":
                self.adapter_circuit_state = "half_open"
                return "half_open"
            return None

        def target_adapter_retry_at(self, _scope):
            return self.adapter_retry_at

        def record_target_adapter_failure(self, _scope, **_):
            self.adapter_circuit_state = "open"
            self.adapter_retry_at = datetime.now(UTC)

        def record_target_adapter_success(self, _scope, **_):
            if self.adapter_circuit_state == "half_open":
                self.adapter_circuit_state = None
                self.adapter_retry_at = None

        def settle_attempt(self, attempt_id, generation, outcome, *, reason, checkpoint, **kwargs):
            self.reasons.append(reason)
            super().settle_attempt(
                attempt_id, generation, outcome, reason=reason, checkpoint=checkpoint, **kwargs
            )

    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/adapter-{suffix}",
            objective="Execute one target request",
            profile_id="injection",
            target_profile_id="target",
            technique_ids=(suffix,),
            guidance_ids=(),
            tool_ids=("target_request",),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for suffix in ("a-unhealthy", "b-healthy")
    )
    store = TargetStore()
    tools = TargetTools()
    service = DurableAgentService(TargetProvider(), tools, store, model="test")

    live = await service.submit("session-adapter", assignments, max_workers=1)
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=1)

    tasks = {task.id: task for task in await service.list_schedule_tasks(live.id)}
    assert tasks[assignments[0].id].status == "completed"
    assert tasks[assignments[1].id].status == "completed"
    unhealthy_attempt = next(
        attempt for attempt in store.attempts.values() if attempt.node_id.endswith("unhealthy")
    )
    assert unhealthy_attempt.state == "quarantined"
    assert any("target adapter unhealthy" in reason for reason in store.reasons)
    assert tools.calls.count(assignments[0].id) == 2
    assert tools.calls.count(assignments[1].id) == 1


async def test_capability_probe_failure_never_trips_target_adapter_circuit() -> None:
    class FailingTools(Tools):
        def __init__(self) -> None:
            self.calls = 0

        def describe(self):
            return (SimpleNamespace(name="target_request", description="request", input_schema={}),)

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            self.calls += 1
            return ToolResult(
                content="reviewed API response pointer missing at step chat",
                is_error=True,
            )

    class CircuitStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.admissions: list[str] = []
            self.failures: list[str] = []

        def admit_target_adapter_probe(self, scope, **_):
            self.admissions.append(scope)
            return "closed"

        def target_adapter_retry_at(self, _scope):
            return datetime.now(UTC)

        def record_target_adapter_failure(self, scope, **_):
            self.failures.append(scope)

    class ProbeProvider(Provider):
        async def stream(self, request):
            yield ToolCall(
                call_id=f"call-{len(request.messages)}",
                name="target_request",
                arguments={"input": "harmless nonce"},
            )
            yield ModelCompleted(finish_reason="tool_calls")

    probe = WorkerAssignment(
        id="campaign/x/capability-probe/memory",
        objective="Probe whether the target remembers a benign nonce",
        profile_id="recon",
        target_profile_id="target",
        technique_ids=("capability/memory",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    store = CircuitStore()
    tools = FailingTools()
    service = DurableAgentService(ProbeProvider(), tools, store, model="test")

    live = await service.submit("session-probe", (probe,), max_workers=1, minimum_workers=1)
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=1)

    tasks = {task.id: task for task in await service.list_schedule_tasks(live.id)}
    assert tasks[probe.id].status in {"failed", "quarantined"}
    assert store.admissions == []
    assert store.failures == []
    # The unresolved probe remains retryable after repair, but an identical
    # no-progress failure gets only one replacement before this schedule stops.
    assert tools.calls == 3
    retry_state = store.schedules[live.id]["checkpoint"]["nodes"][f"worker:{probe.id}"]
    assert retry_state["consecutive_retry_stalls"] == 2


@pytest.mark.parametrize("restart_after_second_5xx", (False, True))
@pytest.mark.parametrize("status_code", (429, 500))
@pytest.mark.parametrize("target_tool", ("target_request", "target_chat"))
async def test_capability_probe_server_error_runs_bounded_replacement(
    restart_after_second_5xx: bool,
    status_code: int,
    target_tool: str,
) -> None:
    class LineageStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.retry_transitions = 0
            self.interrupt_retry = restart_after_second_5xx

        def claim(
            self,
            schedule_id,
            node_id,
            owner,
            *,
            lease_expires_at,
            attempt_id=None,
            target_admission=None,
        ):
            prior = [
                attempt
                for attempt in self.attempts.values()
                if attempt.schedule_id == schedule_id and attempt.node_id == node_id
            ]
            lease = super().claim(
                schedule_id,
                node_id,
                owner,
                lease_expires_at=lease_expires_at,
                attempt_id=attempt_id,
                target_admission=target_admission,
            )
            lease.replaced_attempt_id = prior[-1].id if prior else None
            return lease

        def replacement_attempt_lineage(self, attempt_id, *, limit=None):
            values = []
            current = self.attempts[attempt_id]
            while current is not None:
                values.append(current.id)
                replaced = getattr(current, "replaced_attempt_id", None)
                current = self.attempts.get(replaced)
            values = tuple(reversed(values))
            return values if limit is None else values[-limit:]

        def settle_attempt(
            self,
            attempt_id,
            generation,
            outcome,
            *,
            reason,
            checkpoint,
            target_session_id=None,
            failure_kind=None,
            **kwargs,
        ):
            super().settle_attempt(
                attempt_id,
                generation,
                outcome,
                reason=reason,
                checkpoint=checkpoint,
                target_session_id=target_session_id,
                failure_kind=failure_kind,
                **kwargs,
            )
            self.attempts[attempt_id].target_session_id = target_session_id

        def commit_partial(self, attempt_id, generation, *, result, applied_message_ids=()):
            lease = self.attempts[attempt_id]
            assert lease.generation == generation
            partials = self.results.setdefault(lease.schedule_id, {})
            partials[f"partial:{attempt_id}:{len(partials)}"] = {
                "node_id": lease.node_id,
                "generation": generation,
                "schema_id": "schema/partial-result",
                "payload": result,
            }

        def retry_failed(self, schedule_id, node_id, *, checkpoint, expected_event_count):
            self.retry_transitions += 1
            if self.interrupt_retry and self.retry_transitions == 2:
                self.interrupt_retry = False
                raise RuntimeError("simulated restart after second probe HTTP 5xx")
            super().retry_failed(
                schedule_id,
                node_id,
                checkpoint=checkpoint,
                expected_event_count=expected_event_count,
            )

    class ServerErrorThenSuccessTools(Tools):
        def __init__(self) -> None:
            self.calls = 0
            self.inputs: list[str] = []
            self.lineages: list[tuple[str, ...]] = []

        def describe(self):
            return (SimpleNamespace(name=target_tool, description="request", input_schema={}),)

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            self.calls += 1
            input_key = "prompt" if target_tool == "target_chat" else "input"
            self.inputs.append(str(arguments[input_key]))
            self.lineages.append(tuple(context.effect_attempt_lineage))
            if self.calls <= 2:
                return ToolResult(
                    content=json.dumps(
                        {
                            "error": (
                                "Rate limit exceeded"
                                if status_code == 429
                                else "Internal Server Error"
                            ),
                            "message": "deepseek_empty_content",
                            "statusCode": status_code,
                            "retryAfter": 0,
                        }
                    ),
                    metadata={
                        "target_session_id": "api:probe",
                        "target_session_generation": 1,
                        "target_exchanges": [
                            {
                                "interaction": True,
                                "cleanup": False,
                                "status_code": status_code,
                                "response_body": {"retryAfter": 0},
                            }
                        ],
                    },
                )
            return ToolResult(
                content='{"response":"nonce"}',
                metadata={
                    "target_session_id": "api:probe",
                    "target_exchanges": [
                        {"interaction": True, "cleanup": False, "status_code": 200}
                    ],
                },
            )

    class ProbeProvider(Provider):
        def __init__(self) -> None:
            self.calls = 0
            self.retry_directives: list[tuple[str, ...]] = []

        async def stream(self, request):
            self.calls += 1
            pending: list[str] = []
            for message in request.messages:
                if pending:
                    assert message.role == "tool"
                    assert message.tool_call_id in pending
                    pending.remove(str(message.tool_call_id))
                if message.role == "assistant" and message.tool_calls:
                    pending = [
                        str(call["id"])
                        for call in message.tool_calls
                        if isinstance(call, dict) and call.get("id")
                    ]
            assert pending == []
            directives = tuple(
                str(message.content)
                for message in request.messages
                if "CAPABILITY PROBE TRANSPORT RETRY" in str(message.content)
            )
            self.retry_directives.append(directives)
            assert len(directives) <= 1
            yield ToolCall(
                call_id=f"call-{self.calls}-{len(request.messages)}",
                name=target_tool,
                # Deliberately ignore the retry directive, matching the UAT11
                # provider behavior. The runtime owns the typed fresh-effect nonce.
                arguments={
                    ("prompt" if target_tool == "target_chat" else "input"): (
                        "harmless nonce initial"
                    )
                },
            )
            yield ModelCompleted(finish_reason="tool_calls")

    probe = WorkerAssignment(
        id="campaign/x/capability-probe/non-advertised-language-ms",
        objective="Probe a harmless Malay response",
        profile_id="recon",
        target_profile_id="target",
        technique_ids=("capability/non_advertised_language/ms",),
        guidance_ids=(),
        tool_ids=(target_tool,),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        max_target_exchanges=1,
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    store = LineageStore()
    tools = ServerErrorThenSuccessTools()
    provider = ProbeProvider()
    service = DurableAgentService(provider, tools, store, model="test")

    live = await service.submit("session-probe", (probe,), max_workers=1, minimum_workers=1)
    assert live.execution is not None
    if restart_after_second_5xx:
        with pytest.raises(RuntimeError, match="simulated restart"):
            await asyncio.wait_for(live.execution, timeout=1)
        recovered = DurableAgentService(provider, tools, store, model="test")
        reopened = await recovered.reopen(live.id)
        assert reopened.execution is not None
        await asyncio.wait_for(reopened.execution, timeout=1)
    else:
        await asyncio.wait_for(live.execution, timeout=1)

    tasks = {task.id: task for task in await service.list_schedule_tasks(live.id)}
    assert tasks[probe.id].status == "completed"
    assert tools.calls == 3
    assert tools.inputs[0] == "harmless nonce initial"
    assert "MERLIN-PROBE-" in tools.inputs[1]
    assert "MERLIN-PROBE-" in tools.inputs[2]
    assert tools.inputs[1] != tools.inputs[2]
    assert [len(lineage) for lineage in tools.lineages] == [1, 2, 3]
    assert [len(directives) for directives in provider.retry_directives] == [0, 1, 1]
    assert provider.retry_directives[1][0].split("`")[1] in tools.inputs[1]
    assert provider.retry_directives[2][0].split("`")[1] in tools.inputs[2]
    attempts = [
        attempt for attempt in store.attempts.values() if attempt.node_id == f"worker:{probe.id}"
    ]
    assert len(attempts) == 3
    assert attempts[0].state == "failed"
    assert attempts[0].target_session_id == "api:probe"
    assert attempts[1].state == "failed"
    assert attempts[2].state == "completed"
    worker_results = [
        result
        for result in store.results[live.id].values()
        if result["node_id"] == f"worker:{probe.id}" and result["schema_id"] == "schema/result"
    ]
    assert len(worker_results) == 1


@pytest.mark.parametrize("initial_status_code", (400, 500))
async def test_retryable_delivery_failure_retries_with_fresh_worker_before_exchange_bound(
    initial_status_code: int,
) -> None:
    class RetryStore(Store):
        def claim(
            self,
            schedule_id,
            node_id,
            owner,
            *,
            lease_expires_at,
            attempt_id=None,
            target_admission=None,
        ):
            prior = [
                attempt
                for attempt in self.attempts.values()
                if attempt.schedule_id == schedule_id and attempt.node_id == node_id
            ]
            lease = super().claim(
                schedule_id,
                node_id,
                owner,
                lease_expires_at=lease_expires_at,
                attempt_id=attempt_id,
                target_admission=target_admission,
            )
            lease.replaced_attempt_id = prior[-1].id if prior else None
            return lease

        def replacement_attempt_lineage(self, attempt_id, *, limit=None):
            values = []
            current = self.attempts[attempt_id]
            while current is not None:
                values.append(current.id)
                current = self.attempts.get(getattr(current, "replaced_attempt_id", None))
            values = tuple(reversed(values))
            return values if limit is None else values[-limit:]

        def commit_partial(self, attempt_id, generation, *, result, applied_message_ids=()):
            lease = self.attempts[attempt_id]
            assert lease.generation == generation
            partials = self.results.setdefault(lease.schedule_id, {})
            partials[f"partial:{attempt_id}:{len(partials)}"] = {
                "node_id": lease.node_id,
                "generation": generation,
                "schema_id": "schema/partial-result",
                "payload": result,
            }

    class DeliveryFailureThenSuccessTools(Tools):
        def __init__(self) -> None:
            self.attempt_ids: list[str] = []

        def describe(self):
            return (ToolSpec(name="target_request", description="request", input_schema={}),)

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            self.attempt_ids.append(str(context.attempt_id))
            status_code = initial_status_code if len(self.attempt_ids) == 1 else 200
            return ToolResult(
                content=(
                    json.dumps(
                        {
                            "error": (
                                "Message too long"
                                if status_code == 400
                                else "Internal Server Error"
                            ),
                            "statusCode": status_code,
                        }
                    )
                    if status_code != 200
                    else '{"response":"delivered"}'
                ),
                is_error=status_code != 200,
                metadata={
                    "target_session_id": "api:ordinary-worker",
                    "failure_code": ("target_delivery_rejected" if status_code == 400 else None),
                    "target_delivery_rejected": status_code == 400,
                    "retryable": status_code != 200,
                    "infrastructure_failure_category": (
                        "delivery_failure" if status_code != 200 else None
                    ),
                    "current_target_exchanges": [
                        {
                            "interaction": True,
                            "cleanup": False,
                            "status_code": status_code,
                        }
                    ],
                },
            )

    class OneTargetCallPerAttemptProvider(Provider):
        def __init__(self) -> None:
            self.tool_availability: list[bool] = []
            self.message_payloads: list[str] = []

        async def stream(self, request):
            tools_available = bool(request.tools)
            self.tool_availability.append(tools_available)
            self.message_payloads.append(
                json.dumps([message.model_dump(mode="json") for message in request.messages])
            )
            if tools_available:
                yield ToolCall(
                    call_id=f"ordinary-call-{len(self.tool_availability)}",
                    name="target_request",
                    arguments={"input": "authorized test"},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield TextDelta(text='{"finding":"target replied after retry"}')
            yield ModelCompleted()

    assignment = WorkerAssignment(
        id="campaign/x/ordinary-http-retry",
        objective="Execute one authorized target attempt",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("case:ordinary-http-retry",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_seconds=60),
        max_target_exchanges=1,
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    store = RetryStore()
    tools = DeliveryFailureThenSuccessTools()
    provider = OneTargetCallPerAttemptProvider()
    service = DurableAgentService(provider, tools, store, model="test")

    live = await service.submit(
        "session-http-retry", (assignment,), max_workers=1, minimum_workers=1
    )
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=1)

    tasks = {task.id: task for task in await service.list_schedule_tasks(live.id)}
    attempts = [
        attempt
        for attempt in store.attempts.values()
        if attempt.node_id == f"worker:{assignment.id}"
    ]
    assert tasks[assignment.id].status == "completed"
    assert provider.tool_availability == [True, True, False]
    assert "ordinary-call-1" not in provider.message_payloads[1]
    assert len(tools.attempt_ids) == 2
    assert tools.attempt_ids[0] != tools.attempt_ids[1]
    assert [attempt.state for attempt in attempts] == ["failed", "completed"]
    expected_error = (
        "Message too long" if initial_status_code == 400 else "target transport returned HTTP 500"
    )
    assert expected_error in attempts[0].error
    assert "missing_target_effect" not in attempts[0].error


@pytest.mark.parametrize("statuses", ((429,), (500,), (429, 500)))
async def test_repeated_capability_probe_http_failure_is_bounded_and_exact_args_are_not_resent(
    statuses: tuple[int, ...],
) -> None:
    class LineageStore(Store):
        def claim(
            self,
            schedule_id,
            node_id,
            owner,
            *,
            lease_expires_at,
            attempt_id=None,
            target_admission=None,
        ):
            prior = [
                attempt
                for attempt in self.attempts.values()
                if attempt.schedule_id == schedule_id and attempt.node_id == node_id
            ]
            lease = super().claim(
                schedule_id,
                node_id,
                owner,
                lease_expires_at=lease_expires_at,
                attempt_id=attempt_id,
                target_admission=target_admission,
            )
            lease.replaced_attempt_id = prior[-1].id if prior else None
            return lease

        def replacement_attempt_lineage(self, attempt_id, *, limit=None):
            values = []
            current = self.attempts[attempt_id]
            while current is not None:
                values.append(current.id)
                current = self.attempts.get(getattr(current, "replaced_attempt_id", None))
            values = tuple(reversed(values))
            return values if limit is None else values[-limit:]

        def commit_partial(self, attempt_id, generation, *, result, applied_message_ids=()):
            lease = self.attempts[attempt_id]
            assert lease.generation == generation
            partials = self.results.setdefault(lease.schedule_id, {})
            partials[f"partial:{attempt_id}:{len(partials)}"] = {
                "node_id": lease.node_id,
                "generation": generation,
                "schema_id": "schema/partial-result",
                "payload": result,
            }

    class IdempotentHttp500Tools(Tools):
        def __init__(self) -> None:
            self.invocations = 0
            self.actual_target_calls = 0
            self.prior: ToolResult | None = None
            self.prior_arguments: object | None = None
            self.inputs: list[str] = []

        def describe(self):
            return (SimpleNamespace(name="target_request", description="request", input_schema={}),)

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            self.invocations += 1
            self.inputs.append(str(arguments["input"]))
            if (
                self.prior is not None
                and len(context.effect_attempt_lineage) > 1
                and arguments == self.prior_arguments
            ):
                return self.prior.model_copy(
                    update={
                        "metadata": {
                            **self.prior.metadata,
                            "idempotent_replacement_adoption": True,
                        }
                    }
                )
            self.actual_target_calls += 1
            status_code = statuses[(self.actual_target_calls - 1) % len(statuses)]
            self.prior_arguments = arguments
            self.prior = ToolResult(
                content=json.dumps(
                    {
                        "error": (
                            "Rate limit exceeded" if status_code == 429 else "Internal Server Error"
                        ),
                        "statusCode": status_code,
                        "retryAfter": 0,
                    }
                ),
                metadata={
                    "target_session_id": "api:probe",
                    "target_exchanges": [
                        {
                            "interaction": True,
                            "cleanup": False,
                            "status_code": status_code,
                            "response_body": {"retryAfter": 0},
                        }
                    ],
                },
            )
            return self.prior

    class SameNonceProvider(Provider):
        async def stream(self, request):
            directive = next(
                (
                    str(message.content)
                    for message in request.messages
                    if "CAPABILITY PROBE TRANSPORT RETRY" in str(message.content)
                ),
                None,
            )
            yield ToolCall(
                call_id=f"call-{len(request.messages)}",
                name="target_request",
                arguments={
                    "input": (
                        directive.split("`")[1]
                        if directive is not None and len(statuses) > 1
                        else "same harmless nonce"
                    )
                },
            )
            yield ModelCompleted(finish_reason="tool_calls")

    probe = WorkerAssignment(
        id="campaign/x/capability-probe/non-advertised-language-ms-bounded",
        objective="Probe a harmless Malay response",
        profile_id="recon",
        target_profile_id="target",
        technique_ids=("capability/non_advertised_language/ms",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    store = LineageStore()
    tools = IdempotentHttp500Tools()
    service = DurableAgentService(SameNonceProvider(), tools, store, model="test")

    live = await service.submit("session-probe", (probe,), max_workers=1, minimum_workers=1)
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=1)

    task = next(item for item in await service.list_schedule_tasks(live.id) if item.id == probe.id)
    assert task.status == "failed"
    assert tools.actual_target_calls == tools.invocations == 3
    assert len(set(tools.inputs)) == 3
    assert tools.inputs[0] == "same harmless nonce"
    assert all("MERLIN-PROBE-" in value for value in tools.inputs[1:])
    attempts = [
        attempt for attempt in store.attempts.values() if attempt.node_id == f"worker:{probe.id}"
    ]
    assert len(attempts) == 3
    retry_state = store.schedules[live.id]["checkpoint"]["nodes"][f"worker:{probe.id}"]
    assert retry_state["consecutive_retry_stalls"] == 2
    from merlin_agents.capability_probe_execution import probe_result_from_committed_receipt
    from merlin_agents.capability_probes import ProbeOutcome, plan_capability_probes

    planned = plan_capability_probes(candidate_languages=("ms",)).probes[-1]
    classified = probe_result_from_committed_receipt(planned, task, None)
    assert classified.outcome is ProbeOutcome.INCONCLUSIVE


async def test_open_adapter_circuit_leaves_dependents_pending_and_runs_other_scope() -> None:
    other_started = asyncio.Event()
    release_other = asyncio.Event()

    class TargetProvider(Provider):
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def stream(self, request):
            count = self.calls.get(request.budget_id, 0)
            self.calls[request.budget_id] = count + 1
            if count:
                yield TextDelta(text='{"finding":"completed"}')
                yield ModelCompleted()
                return
            yield ToolCall(
                call_id=f"call-{request.budget_id}",
                name="target_request",
                arguments={"input": request.budget_id},
            )
            yield ModelCompleted(finish_reason="tool_calls")

    class TargetTools(Tools):
        def describe(self):
            return (SimpleNamespace(name="target_request", description="request", input_schema={}),)

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            if context.agent_id == "campaign/b-other":
                other_started.set()
                await release_other.wait()
            return ToolResult(
                content='{"response":"acknowledged"}',
                metadata={"target_session_id": f"api:{context.agent_id}"},
            )

    class CircuitStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.blocked_scope: str | None = None
            self.retry_at = datetime.now(UTC) + timedelta(milliseconds=50)
            self.half_open = False

        def admit_target_adapter_probe(self, scope, **_):
            if self.blocked_scope is None:
                self.blocked_scope = scope
            if scope != self.blocked_scope:
                return "closed"
            if self.half_open:
                return None
            if datetime.now(UTC) < self.retry_at:
                return None
            self.half_open = True
            return "half_open"

        def target_adapter_retry_at(self, scope):
            return self.retry_at if scope == self.blocked_scope else None

        def record_target_adapter_success(self, scope, **_):
            if scope == self.blocked_scope and self.half_open:
                self.blocked_scope = None
                self.half_open = False

    def assignment(
        task_id: str,
        target: str,
        *,
        stage: str | None = None,
        predecessor: str | None = None,
    ) -> WorkerAssignment:
        return WorkerAssignment(
            id=task_id,
            objective="Execute one target request",
            profile_id="injection",
            target_profile_id=target,
            technique_ids=(task_id,),
            guidance_ids=(),
            tool_ids=("target_request",),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
            agenda_id="agenda/shared" if stage is not None else None,
            execution_stage=stage,
            pair_id="control/shared" if stage is not None else None,
            predecessor_assignment_id=predecessor,
            oracle_digest="sha256:" + "7" * 64 if stage is not None else None,
            semantic_technique_id=task_id if stage is not None else None,
            target_session_policy="same-reviewed-session" if stage is not None else None,
            comparison_supported=True,
            comparison_policy="reviewed-benign-differential-v1",
            reviewed_adapter_digest="sha256:" + "8" * 64,
            comparison_dimensions=(("modality", "text"), ("turn", "single"), ("language", "en")),
        )

    control = assignment("campaign/a-control", "shared", stage="control")
    other = assignment("campaign/b-other", "other")
    direct = assignment("campaign/z-direct", "shared", stage="direct", predecessor=control.id)
    store = CircuitStore()
    service = DurableAgentService(TargetProvider(), TargetTools(), store, model="test")

    live = await service.submit("session-scopes", (control, other, direct), max_workers=2)
    assert live.execution is not None
    await asyncio.wait_for(other_started.wait(), timeout=1)

    nodes = store.nodes[live.id]
    assert nodes[f"worker:{control.id}"]["state"] == "ready"
    assert nodes[f"worker:{control.id}"].get("generation", 0) == 0
    assert nodes[f"worker:{direct.id}"]["state"] == "pending"
    assert nodes[f"worker:{other.id}"]["state"] == "leased"

    release_other.set()
    await asyncio.wait_for(live.execution, timeout=1)
    tasks = {task.id: task for task in await service.list_schedule_tasks(live.id)}
    assert {task.status for task in tasks.values()} == {"completed"}


async def test_failed_half_open_probe_reopens_circuit_without_adapter_misclassification() -> None:
    class FailingProvider(Provider):
        async def stream(self, request):
            yield ModelError(message="provider rejected probe", code="invalid_request")

    class ProbeStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.state = "open"
            self.failures = 0
            self.releases = 0

        def admit_target_adapter_probe(self, _scope, **_):
            if self.state != "open":
                return None
            self.state = "half_open"
            return "half_open"

        def target_adapter_retry_at(self, _scope):
            return datetime.now(UTC)

        def record_target_adapter_failure(self, _scope, **_):
            self.state = "open"
            self.failures += 1

        def record_target_adapter_success(self, _scope, **_):
            self.state = "closed"

        def release_target_adapter_probe(self, _scope, _probe_token):
            self.state = "open"
            self.releases += 1
            return True

    assignment = WorkerAssignment(
        id="campaign/probe",
        objective="Probe target adapter",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("probe",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        comparison_supported=True,
        comparison_policy="reviewed-benign-differential-v1",
        reviewed_adapter_digest="sha256:" + "9" * 64,
        comparison_dimensions=(("modality", "text"), ("turn", "single"), ("language", "en")),
    )
    store = ProbeStore()
    service = DurableAgentService(FailingProvider(), Tools(), store, model="test")

    live = await service.submit("session-probe", (assignment,), max_workers=1, minimum_workers=1)
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=1)

    task = (await service.list_schedule_tasks(live.id))[0]
    assert task.status == "failed"
    assert store.state == "open"
    assert store.failures == 0
    # A provider-declared invalid request is not transient infrastructure, so
    # the half-open probe is released once and is not routinely replayed.
    assert store.releases == 1
    worker_attempts = [
        attempt for attempt in store.attempts.values() if attempt.node_id == "worker:campaign/probe"
    ]
    assert {attempt.state for attempt in worker_attempts} == {"failed"}


async def test_cancelled_half_open_probe_is_neutrally_released() -> None:
    started = asyncio.Event()

    class BlockingProvider(Provider):
        async def stream(self, request):
            started.set()
            await asyncio.Event().wait()
            yield ModelCompleted()

    class BlockingTools(Tools):
        def describe(self):
            return (SimpleNamespace(name="target_request", description="request", input_schema={}),)

        def effect(self, name):
            return "target_network"

    class ProbeStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.state = "open"
            self.failures = 0
            self.releases = 0

        def admit_target_adapter_probe(self, _scope, **_):
            if self.state != "open":
                return None
            self.state = "half_open"
            return "half_open"

        def target_adapter_retry_at(self, _scope):
            return datetime.now(UTC)

        def record_target_adapter_failure(self, _scope, **_):
            self.failures += 1

        def release_target_adapter_probe(self, _scope, _probe_token):
            self.state = "open"
            self.releases += 1
            return True

    assignment = WorkerAssignment(
        id="campaign/cancelled-probe",
        objective="Cancel target adapter probe",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("cancelled-probe",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        comparison_supported=True,
        comparison_policy="reviewed-benign-differential-v1",
        reviewed_adapter_digest="sha256:" + "c" * 64,
        comparison_dimensions=(("modality", "text"),),
    )
    store = ProbeStore()
    service = DurableAgentService(BlockingProvider(), BlockingTools(), store, model="test")

    live = await service.submit(
        "session-cancelled-probe", (assignment,), max_workers=1, minimum_workers=1
    )
    assert live.execution is not None
    await asyncio.wait_for(started.wait(), timeout=1)
    await service.cancel_task("session-cancelled-probe", assignment.id)
    await asyncio.wait_for(live.execution, timeout=1)

    assert (await service.list_schedule_tasks(live.id))[0].status == "cancelled"
    assert store.failures == 0
    assert store.releases == 1
    assert store.state == "open"


async def test_cached_only_half_open_probe_is_neutrally_released() -> None:
    class CachedProvider(Provider):
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield ToolCall(
                    call_id="cached-target",
                    name="target_request",
                    arguments={"input": "cached"},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield TextDelta(text='{"finding":"cached receipt only"}')
            yield ModelCompleted()

    class CachedTools(Tools):
        def __init__(self) -> None:
            self.calls = 0

        def describe(self):
            return (SimpleNamespace(name="target_request", description="request", input_schema={}),)

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            self.calls += 1
            return ToolResult(
                content='{"response":"cached"}',
                metadata={
                    "idempotent_replay": True,
                    "target_session_id": "api:cached",
                },
            )

    class ProbeStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.state = "open"
            self.failures = 0
            self.releases = 0

        def admit_target_adapter_probe(self, _scope, **_):
            if self.state != "open":
                return None
            self.state = "half_open"
            return "half_open"

        def target_adapter_retry_at(self, _scope):
            return datetime.now(UTC)

        def record_target_adapter_failure(self, _scope, **_):
            self.failures += 1

        def release_target_adapter_probe(self, _scope, _probe_token):
            self.state = "open"
            self.releases += 1
            return True

    assignment = WorkerAssignment(
        id="campaign/cached-probe",
        objective="Use one cached target receipt",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("cached-probe",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        comparison_supported=True,
        comparison_policy="reviewed-benign-differential-v1",
        reviewed_adapter_digest="sha256:" + "d" * 64,
        comparison_dimensions=(("modality", "text"),),
    )
    store = ProbeStore()
    tools = CachedTools()
    service = DurableAgentService(CachedProvider(), tools, store, model="test")

    live = await service.submit(
        "session-cached-probe", (assignment,), max_workers=1, minimum_workers=1
    )
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=1)

    assert (await service.list_schedule_tasks(live.id))[0].status == "failed"
    assert store.failures == 0
    # Each no-delivery replacement neutrally releases its circuit probe; missing
    # delivery must never count as evidence that the adapter is unhealthy.
    assert store.releases > 1
    retry_state = store.schedules[live.id]["checkpoint"]["nodes"][f"worker:{assignment.id}"]
    assert retry_state["consecutive_retry_stalls"] > 1
    assert store.state == "open"
    assert tools.calls == 1


async def test_reopen_waits_for_durable_adapter_cooldown_then_runs_probe() -> None:
    class TargetProvider(Provider):
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls > 1:
                yield TextDelta(text='{"finding":"completed after restart"}')
                yield ModelCompleted()
                return
            yield ToolCall(
                call_id="restart-target",
                name="target_request",
                arguments={"input": "restart probe"},
            )
            yield ModelCompleted(finish_reason="tool_calls")

    class TargetTools(Tools):
        def describe(self):
            return (SimpleNamespace(name="target_request", description="request", input_schema={}),)

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            return ToolResult(
                content='{"response":"acknowledged"}',
                metadata={"target_session_id": "api:restart"},
            )

    class RestartCircuitStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.state = "open"
            self.retry_at = datetime.now(UTC) + timedelta(milliseconds=20)

        def admit_target_adapter_probe(self, _scope, **_):
            if self.state == "closed":
                return "closed"
            if self.state == "half_open" or datetime.now(UTC) < self.retry_at:
                return None
            self.state = "half_open"
            return "half_open"

        def target_adapter_retry_at(self, _scope):
            return self.retry_at

        def record_target_adapter_success(self, _scope, **_):
            self.state = "closed"

        def record_target_adapter_failure(self, _scope, **_):
            self.state = "open"
            self.retry_at = datetime.now(UTC) + timedelta(milliseconds=20)

    assignment = WorkerAssignment(
        id="campaign/restart-probe",
        objective="Resume target adapter probe",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("restart-probe",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        comparison_supported=True,
        comparison_policy="reviewed-benign-differential-v1",
        reviewed_adapter_digest="sha256:" + "a" * 64,
        comparison_dimensions=(("modality", "text"), ("turn", "single"), ("language", "en")),
    )
    store = RestartCircuitStore()
    original = DurableAgentService(TargetProvider(), TargetTools(), store, model="test")
    live = await original.submit(
        "session-restart",
        (assignment,),
        max_workers=1,
        minimum_workers=1,
        start_immediately=False,
    )

    provider = TargetProvider()
    restarted = DurableAgentService(provider, TargetTools(), store, model="test")
    reopened = await restarted.reopen(live.id)
    assert reopened.execution is not None
    await asyncio.wait_for(reopened.execution, timeout=1)

    task = (await restarted.list_schedule_tasks(live.id))[0]
    assert task.status == "completed"
    assert store.state == "closed"
    assert provider.calls == 2


async def test_adapter_cooldown_wait_is_bounded_and_wakes_for_task_cancellation() -> None:
    checked = asyncio.Event()

    class WaitingCircuitStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.admission_checks = 0
            self.retry_at = datetime.now(UTC) + timedelta(seconds=10)

        def admit_target_adapter_probe(self, _scope, **_):
            self.admission_checks += 1
            checked.set()
            return None

        def target_adapter_retry_at(self, _scope):
            return self.retry_at

    assignment = WorkerAssignment(
        id="campaign/waiting-probe",
        objective="Wait for target adapter",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("waiting-probe",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        comparison_supported=True,
        comparison_policy="reviewed-benign-differential-v1",
        reviewed_adapter_digest="sha256:" + "b" * 64,
        comparison_dimensions=(("modality", "text"), ("turn", "single"), ("language", "en")),
    )
    store = WaitingCircuitStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    live = await service.submit("session-waiting", (assignment,), max_workers=1, minimum_workers=1)
    assert live.execution is not None
    await asyncio.wait_for(checked.wait(), timeout=1)
    await asyncio.sleep(0.1)
    assert store.admission_checks == 1

    await service.cancel_task("session-waiting", assignment.id)
    await asyncio.wait_for(live.execution, timeout=1)

    task = (await service.list_schedule_tasks(live.id))[0]
    assert task.status == "cancelled"
    assert store.admission_checks == 1


async def test_overdue_denied_adapter_admission_uses_lease_grace_poll_floor() -> None:
    checked = asyncio.Event()

    class OverdueCircuitStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.admission_checks = 0

        def admit_target_adapter_probe(self, _scope, **_):
            self.admission_checks += 1
            checked.set()
            return None

        def target_adapter_retry_at(self, _scope):
            return datetime.now(UTC) - timedelta(seconds=1)

    assignment = WorkerAssignment(
        id="campaign/overdue-probe",
        objective="Wait for an already-owned target adapter probe",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("overdue-probe",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    store = OverdueCircuitStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    live = await service.submit("session-overdue", (assignment,), max_workers=1, minimum_workers=1)
    assert live.execution is not None
    await asyncio.wait_for(checked.wait(), timeout=1)
    await asyncio.sleep(0.02)
    assert store.admission_checks == 1

    await service.cancel_task("session-overdue", assignment.id)
    await asyncio.wait_for(live.execution, timeout=1)


async def test_denied_adapter_admission_requires_durable_retry_timestamp() -> None:
    class MissingRetryCircuitStore(Store):
        def admit_target_adapter_probe(self, _scope, **_):
            return None

    assignment = WorkerAssignment(
        id="campaign/missing-retry",
        objective="Reject an invalid circuit store contract",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("missing-retry",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    service = DurableAgentService(Provider(), Tools(), MissingRetryCircuitStore(), model="test")
    live = await service.submit(
        "session-missing-retry", (assignment,), max_workers=1, minimum_workers=1
    )
    assert live.execution is not None

    with pytest.raises(RuntimeError, match="without durable retry_at support"):
        await live.execution


async def test_unsupported_baseline_cell_is_blocked_without_provider_or_target_execution() -> None:
    class CountingProvider(Provider):
        def __init__(self) -> None:
            self.worker_calls = 0

        async def stream(self, request):
            self.worker_calls += 1
            async for event in super().stream(request):
                yield event

    class TargetTools(Tools):
        def describe(self):
            return (SimpleNamespace(name="target_request", description="request", input_schema={}),)

        def effect(self, name):
            return "target_network"

    provider = CountingProvider()
    store = Store()
    tools = TargetTools()
    service = DurableAgentService(provider, tools, store, model="test")
    assignment = WorkerAssignment(
        id="campaign/injection/unsupported/control",
        objective="Run an unsupported multi-turn control",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("technique/multiturn",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_turns=8, max_tokens=8_000, max_seconds=120),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        agenda_id="agenda/unsupported",
        execution_stage="control",
        pair_id="control/unsupported",
        oracle_digest="sha256:" + "4" * 64,
        semantic_technique_id="technique/multiturn",
        target_session_policy="same-reviewed-session",
        comparison_supported=False,
        comparison_block_reason="multi-turn comparison lacks a reviewed full-sequence adapter",
        comparison_policy="reviewed-benign-differential-v1",
        reviewed_adapter_digest="sha256:" + "a" * 64,
        comparison_dimensions=(
            ("modality", "text"),
            ("turn", "multi"),
            ("language", "en"),
        ),
    )

    live = await service.submit(
        "session",
        (assignment,),
        max_workers=1,
        minimum_workers=1,
        start_immediately=False,
    )
    restarted = DurableAgentService(provider, tools, store, model="test")
    reopened = await restarted.reopen(live.id)
    assert reopened.execution is not None
    await reopened.execution

    tasks = await restarted.list_schedule_tasks(live.id)
    assert provider.worker_calls > 0
    assert tasks[0].status != "quarantined"
    attempt = next(
        item for item in store.attempts.values() if item.node_id == f"worker:{assignment.id}"
    )
    assert attempt.state != "quarantined"


async def test_resume_waits_until_inherited_durable_lease_is_no_longer_running() -> None:
    store = Store()
    attempts = [
        {
            "id": "attempt-1",
            "schedule_id": "schedule-1",
            "node_id": "worker:headers",
            "generation": 1,
            "state": "running",
            "lease_expires_at": datetime.now(UTC),
        }
    ]
    sweeps = 0
    executed = False

    class RestartService(DurableAgentService):
        async def recover_expired(self, schedule_id):
            nonlocal sweeps
            assert schedule_id == "schedule-1"
            sweeps += 1
            if sweeps == 2:
                attempts[0]["state"] = "expired"
            return ()

        async def _execute(self, live, max_workers):
            nonlocal executed
            assert max_workers == 1
            executed = True

    store.list_attempts = lambda schedule_id: [dict(item) for item in attempts]
    service = RestartService(Provider(), Tools(), store, model="test")
    await service._resume(SimpleNamespace(id="schedule-1"), attempts, 1)  # noqa: SLF001

    assert sweeps == 2
    assert executed


async def test_assignment_context_replaces_model_planning_placeholders() -> None:
    store = Store()

    async def selected_context(_request):
        return AssignmentContext(
            target_profile_id="selected-target",
            techniques=(
                AssignmentTechnique(
                    "pack/case-prompt",
                    "Use the enabled prompt case",
                    frozenset({"prompt", "injection"}),
                    ("pack:pack", "case:pack/case-prompt"),
                ),
                AssignmentTechnique(
                    "pack/case-privacy",
                    "Use the enabled privacy case",
                    frozenset({"privacy", "disclosure"}),
                    ("pack:pack", "case:pack/case-privacy"),
                ),
            ),
            tool_ids=("target_chat",),
            skill_ids=("review-skill",),
            provider_id="attacker-provider",
            authority_ids=("target:selected-target",),
            budget=AssignmentBudget(4, 4_000, 60),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )

    service = DurableAgentService(
        Provider(),
        Tools(),
        store,
        model="test",
        assignment_context=selected_context,
        initial_composition_admitter=_wave0_admitter("campaign-selected"),
    )
    _ = [
        event
        async for event in service.start_swarm(
            SwarmRequest(session_id="session-selected", objective="Assess", max_workers=2)
        )
    ]

    persisted = store.schedules["schedule-1"]["graph"]["assignments"]
    assert len(persisted) == 2
    assert {item["target_profile_id"] for item in persisted} == {"selected-target"}
    assert {tuple(item["technique_ids"]) for item in persisted} == {
        ("pack/case-privacy",),
        ("pack/case-prompt",),
    }
    assert all(item["skill_ids"] == ("review-skill",) for item in persisted)


async def test_child_delegation_narrows_exact_parent_assignment_and_preserves_route() -> None:
    store = Store()
    service = DurableAgentService(Provider(), Tools(), store, model="service-default")
    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/delegate/{profile}",
            objective="Assess",
            profile_id=profile,
            target_profile_id="target",
            technique_ids=("technique",),
            guidance_ids=("guidance",),
            tool_ids=("read",) if profile == "recon" else ("write",),
            skill_ids=("skill",),
            provider_id="provider-route",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
            provider_model="parent-model",
            provider_capabilities=("streaming", "tools"),
            provider_input_cost_per_million=1.0,
            provider_output_cost_per_million=2.0,
        )
        for profile in ("recon", "injection")
    )
    parent = await service.submit(
        "session-delegate", assignments, delegation_depth=1, start_immediately=False
    )
    parent_assignment = assignments[0]
    parent_node_id = f"worker:{parent_assignment.id}"
    store.attempts["attempt-parent"] = SimpleNamespace(
        id="attempt-parent",
        schedule_id=parent.id,
        node_id=parent_node_id,
        generation=1,
        owner="parent",
        lease_expires_at=datetime.now(UTC),
        state="running",
    )

    with pytest.raises(ValueError, match="child tools widen"):
        await service.request_child(
            ChildSwarmRequest(
                session_id="session-delegate",
                parent_schedule_id=parent.id,
                parent_node_id=parent_node_id,
                parent_attempt_id="attempt-parent",
                objective="Narrow child",
                tool_names=("write",),
                grant_ids=("target:reviewed",),
                skill_ids=("skill",),
                remaining_delegation_depth=0,
            )
        )

    projection = await service.request_child(
        ChildSwarmRequest(
            session_id="session-delegate",
            parent_schedule_id=parent.id,
            parent_node_id=parent_node_id,
            parent_attempt_id="attempt-parent",
            objective="Narrow child",
            provider_routes=("provider-route",),
            tool_names=("read",),
            grant_ids=("target:reviewed",),
            skill_ids=("skill",),
            remaining_delegation_depth=0,
        )
    )
    child = service._live[projection.schedule_id]  # noqa: SLF001
    assert store.schedules[projection.schedule_id]["parent_schedule_id"] == parent.id
    assert store.schedules[projection.schedule_id]["parent_attempt_id"] == "attempt-parent"
    assert child.assignments
    assert all(item.provider_model == "parent-model" for item in child.assignments)
    assert all(item.provider_capabilities == ("streaming", "tools") for item in child.assignments)
    assert all(item.tool_ids == ("read",) for item in child.assignments)
    assert all(item.authority_ids == ("target:reviewed",) for item in child.assignments)
    if child.execution is not None:
        await child.execution


async def test_child_delegation_compares_grant_effect_sets_when_grants_resolve() -> None:
    store = Store()
    service = DurableAgentService(Provider(), Tools(), store, model="service-default")
    grants = {
        "grant:parent": CapabilityGrant(
            id="grant:parent",
            effect_classes=frozenset({"target_read", "target_network"}),
        ),
        "grant:narrow": CapabilityGrant(
            id="grant:narrow", effect_classes=frozenset({"target_read"})
        ),
        "grant:wide": CapabilityGrant(
            id="grant:wide", effect_classes=frozenset({"target_read", "subprocess"})
        ),
    }
    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/delegate/{profile}",
            objective="Assess",
            profile_id=profile,
            target_profile_id="target",
            technique_ids=("technique",),
            guidance_ids=("guidance",),
            tool_ids=("read",) if profile == "recon" else ("write",),
            skill_ids=("skill",),
            provider_id="provider-route",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
            grant_id="grant:parent" if profile == "recon" else None,
        )
        for profile in ("recon", "injection")
    )
    parent = await service.submit(
        "session-grants",
        assignments,
        delegation_depth=1,
        start_immediately=False,
        grants=grants,
    )
    assert set(parent.grants) == set(grants)
    persisted_grants = store.schedules[parent.id]["graph"]["grants"]
    assert set(persisted_grants) == set(grants)
    assert set(persisted_grants["grant:parent"]["effect_classes"]) == {
        "target_read",
        "target_network",
    }
    parent_assignment = assignments[0]
    parent_node_id = f"worker:{parent_assignment.id}"
    store.attempts["attempt-parent"] = SimpleNamespace(
        id="attempt-parent",
        schedule_id=parent.id,
        node_id=parent_node_id,
        generation=1,
        owner="parent",
        lease_expires_at=datetime.now(UTC),
        state="running",
    )

    def request(grant_ids: tuple[str, ...]) -> ChildSwarmRequest:
        return ChildSwarmRequest(
            session_id="session-grants",
            parent_schedule_id=parent.id,
            parent_node_id=parent_node_id,
            parent_attempt_id="attempt-parent",
            objective="Narrow child",
            tool_names=("read",),
            grant_ids=grant_ids,
            skill_ids=("skill",),
            remaining_delegation_depth=0,
        )

    # An effect-widening child grant is refused even though grant names are
    # not compared when both sides resolve.
    with pytest.raises(ValueError, match="child grant effects widen the parent grant"):
        await service.request_child(request(("grant:wide",)))
    # A typed parent never falls back to the legacy name path when a requested
    # grant is absent from its durable registry.
    with pytest.raises(ValueError, match="typed capability grant is absent"):
        await service.request_child(request(("grant:missing",)))

    projection = await service.request_child(request(("grant:narrow",)))
    child = service._live[projection.schedule_id]  # noqa: SLF001
    # The child schedule and assignment both carry the exact narrowed grant;
    # execution therefore cannot fall back to legacy tool-name admission.
    assert set(child.grants) == set(grants)
    assert all(item.grant_id == "grant:narrow" for item in child.assignments)
    assert all(item.authority_ids == ("grant:narrow",) for item in child.assignments)
    if child.execution is not None:
        await child.execution


async def test_child_delegation_inherits_typed_parent_grant_when_not_explicitly_narrowed() -> None:
    store = Store()
    service = DurableAgentService(Provider(), Tools(), store, model="service-default")
    grant = CapabilityGrant(
        id="grant:parent",
        effect_classes=frozenset({"target_read"}),
        target_scope="target",
    )
    assignment = WorkerAssignment(
        id="campaign/delegate/recon",
        objective="Assess",
        profile_id="recon",
        target_profile_id="target",
        technique_ids=("technique",),
        guidance_ids=("guidance",),
        tool_ids=("read",),
        skill_ids=("skill",),
        provider_id="provider-route",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        grant_id=grant.id,
    )
    parent = await service.submit(
        "session-grant-inherit",
        (assignment,),
        minimum_workers=1,
        delegation_depth=1,
        start_immediately=False,
        grants={grant.id: grant},
    )
    parent_node_id = f"worker:{assignment.id}"
    store.attempts["attempt-parent"] = SimpleNamespace(
        id="attempt-parent",
        schedule_id=parent.id,
        node_id=parent_node_id,
        generation=1,
        owner="parent",
        lease_expires_at=datetime.now(UTC),
        state="running",
    )

    projection = await service.request_child(
        ChildSwarmRequest(
            session_id="session-grant-inherit",
            parent_schedule_id=parent.id,
            parent_node_id=parent_node_id,
            parent_attempt_id="attempt-parent",
            objective="Narrow child",
            tool_names=("read",),
            skill_ids=("skill",),
            remaining_delegation_depth=0,
        )
    )

    child = service._live[projection.schedule_id]  # noqa: SLF001
    assert all(item.grant_id == grant.id for item in child.assignments)
    assert child.graph.authority.grants == frozenset({grant.id})
    if child.execution is not None:
        await child.execution


async def test_child_delegation_rejects_target_scope_broadening() -> None:
    store = Store()
    service = DurableAgentService(Provider(), Tools(), store, model="service-default")
    parent_grant = CapabilityGrant(
        id="grant:parent",
        effect_classes=frozenset({"target_read"}),
        target_scope="target",
    )
    broad_grant = CapabilityGrant(
        id="grant:broad",
        effect_classes=frozenset({"target_read"}),
    )
    assignment = WorkerAssignment(
        id="campaign/delegate/recon",
        objective="Assess",
        profile_id="recon",
        target_profile_id="target",
        technique_ids=("technique",),
        guidance_ids=("guidance",),
        tool_ids=("read",),
        skill_ids=("skill",),
        provider_id="provider-route",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        grant_id=parent_grant.id,
    )
    parent = await service.submit(
        "session-grant-scope",
        (assignment,),
        minimum_workers=1,
        delegation_depth=1,
        start_immediately=False,
        grants={parent_grant.id: parent_grant, broad_grant.id: broad_grant},
    )
    parent_node_id = f"worker:{assignment.id}"
    store.attempts["attempt-parent"] = SimpleNamespace(
        id="attempt-parent",
        schedule_id=parent.id,
        node_id=parent_node_id,
        generation=1,
        owner="parent",
        lease_expires_at=datetime.now(UTC),
        state="running",
    )

    with pytest.raises(ValueError, match="widen the parent grant"):
        await service.request_child(
            ChildSwarmRequest(
                session_id="session-grant-scope",
                parent_schedule_id=parent.id,
                parent_node_id=parent_node_id,
                parent_attempt_id="attempt-parent",
                objective="Narrow child",
                tool_names=("read",),
                grant_ids=(broad_grant.id,),
                skill_ids=("skill",),
                remaining_delegation_depth=0,
            )
        )


def test_typed_capability_grant_subset_covers_all_authority_bounds() -> None:
    now = datetime.now(UTC)
    parent = CapabilityGrant(
        id="grant:parent",
        effect_classes=frozenset({"target_read", "target_network"}),
        target_scope="target",
        max_concurrency=3,
        evidence="delivery-receipt",
        expires_at=now + timedelta(hours=2),
    )
    child = CapabilityGrant(
        id="grant:child",
        effect_classes=frozenset({"target_read"}),
        target_scope="target",
        max_concurrency=1,
        evidence="delivery-receipt",
        expires_at=now + timedelta(hours=1),
    )

    assert _capability_grant_narrows(child, parent)
    assert not _capability_grant_narrows(child.model_copy(update={"target_scope": None}), parent)
    assert not _capability_grant_narrows(child.model_copy(update={"max_concurrency": None}), parent)
    assert not _capability_grant_narrows(child.model_copy(update={"expires_at": None}), parent)
    assert not _capability_grant_narrows(child.model_copy(update={"evidence": None}), parent)


async def test_reopen_restores_schedule_grants() -> None:
    store = Store()
    service = DurableAgentService(Provider(), Tools(), store, model="service-default")
    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/reopen/{profile}",
            objective="Assess",
            profile_id=profile,
            target_profile_id="target",
            technique_ids=("technique",),
            guidance_ids=("guidance",),
            tool_ids=("read",),
            skill_ids=("skill",),
            provider_id="provider-route",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for profile in ("recon", "injection")
    )
    parent = await service.submit(
        "session-reopen",
        assignments,
        start_immediately=False,
        grants={
            "grant:parent": CapabilityGrant(
                id="grant:parent", effect_classes=frozenset({"target_read"})
            )
        },
    )
    del service._live[parent.id]  # noqa: SLF001
    reopened = await service.reopen(parent.id, resume_execution=False)
    assert reopened.grants["grant:parent"].effect_classes == frozenset({"target_read"})
    # Schedules sealed before grants existed reopen with an empty registry.
    store.schedules[parent.id]["graph"].pop("grants")
    del service._live[parent.id]  # noqa: SLF001
    legacy = await service.reopen(parent.id, resume_execution=False)
    assert legacy.grants == {}


async def test_worker_provider_calls_use_campaign_budget_ledger_callbacks() -> None:
    store = Store()
    calls = []

    async def selected_context(_request):
        return AssignmentContext(
            target_profile_id="target",
            techniques=(
                AssignmentTechnique(
                    "campaign/budget/injection",
                    "Probe",
                    frozenset({"injection"}),
                    (),
                ),
                AssignmentTechnique(
                    "campaign/budget/data",
                    "Probe data exposure",
                    frozenset({"data", "disclosure"}),
                    (),
                ),
            ),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=(),
            budget=AssignmentBudget(max_turns=4, max_tokens=100_000, max_cost_usd=1.0),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
            provider_model="test",
            provider_model_pricing=(("test", (10.0, 20.0)),),
        )

    async def reserve(campaign_id, reservation_id, actor_kind, tokens, turns, cost_usd):
        calls.append(("reserve", campaign_id, reservation_id, actor_kind, tokens, turns, cost_usd))
        return tokens

    async def settle(campaign_id, reservation_id, tokens, turns, cost_usd):
        calls.append(("settle", campaign_id, reservation_id, tokens, turns, cost_usd))

    async def release(campaign_id, reservation_id):
        calls.append(("release", campaign_id, reservation_id))

    async def remaining_seconds(campaign_id):
        calls.append(("deadline", campaign_id))
        return 30.0

    service = DurableAgentService(
        Provider(),
        Tools(),
        store,
        model="test",
        assignment_context=selected_context,
        initial_composition_admitter=_wave0_admitter("campaign/budget"),
        campaign_budget_reserver=reserve,
        campaign_budget_settler=settle,
        campaign_budget_releaser=release,
        campaign_deadline_provider=remaining_seconds,
        campaign_cost_per_token_upper_bound=0.00001,
        campaign_model_cost_per_token_upper_bound=lambda selected: (
            0.00003 if selected == "test" else None
        ),
    )

    _ = [
        event
        async for event in service.start_swarm(
            SwarmRequest(session_id="session-budget", objective="Assess", max_workers=2)
        )
    ]

    assert calls[0] == ("deadline", "campaign/budget")
    reservation = next(call for call in calls if call[0] == "reserve")
    assert reservation[0:2] == ("reserve", "campaign/budget")
    assert reservation[2].endswith("/provider/1")
    assert reservation[3] == "worker"
    assert reservation[4] == 24_999  # conservative floor after decimal-rate conversion
    assert reservation[5] == 1
    assert reservation[6] == pytest.approx(0.49998)
    settlements = [call for call in calls if call[0] == "settle"]
    assert len(settlements) == 2
    assert all(call[0] != "release" for call in calls)


async def test_failed_recon_terminalizes_dependent_worker_and_finishes_cleanup() -> None:
    store = Store()
    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/dependency/{profile}",
            objective="Assess",
            profile_id=profile,
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=(),
            budget=AssignmentBudget(max_turns=4, max_tokens=100_000),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for profile in ("recon", "injection")
    )

    async def deny_budget(*_args):
        return None

    service = DurableAgentService(
        Provider(),
        Tools(),
        store,
        model="test",
        campaign_budget_reserver=deny_budget,
    )
    live = await service.submit("session-dependency", assignments, max_workers=1)
    assert live.execution is not None

    await asyncio.wait_for(live.execution, timeout=1)

    tasks = await service.list_schedule_tasks(live.id)
    assert [(task.id.rsplit("/", 1)[-1], task.status) for task in tasks] == [
        ("recon", "failed"),
        ("injection", "failed"),
    ]
    assert store.nodes[live.id]["cleanup:campaign/dependency/recon"]["state"] == "completed"
    assert store.nodes[live.id]["cleanup:campaign/dependency/injection"]["state"] == "completed"
    assert store.nodes[live.id]["reducer"]["state"] == "completed"


async def test_durable_memory_is_scoped_and_recorded_only_after_worker_commit() -> None:
    store = Store()
    memory = MemoryProvider(store, active=True)

    def scope(session_id, assignment):
        return PortableMemoryQuery(
            session_id=session_id,
            task_id=assignment.id,
            repository_id="repo/acme",
            target_id=assignment.target_profile_id,
            provider_id=assignment.provider_id,
            operator_id="operator/alice",
            scope_ids=("scope/security",),
            tags=assignment.technique_ids,
        )

    service = DurableAgentService(
        Provider(),
        Tools(),
        store,
        model="test",
        portable_memory=memory,
        portable_memory_scope=scope,
    )
    _ = [
        event
        async for event in service.start_swarm(
            SwarmRequest(session_id="session-memory", objective="Assess target", max_workers=2)
        )
    ]

    assert len(memory.queries) == 2
    assert {query.repository_id for query in memory.queries} == {"repo/acme"}
    assert {query.operator_id for query in memory.queries} == {"operator/alice"}
    assert len(memory.uses) == 2
    assert all(ids == ("lesson/accepted",) for _, _, ids in memory.uses)


async def test_quarantined_or_no_active_memory_is_not_injected_or_recorded() -> None:
    store = Store()
    memory = MemoryProvider(store, active=False)

    def scope(session_id, assignment):
        return PortableMemoryQuery(
            session_id=session_id,
            task_id=assignment.id,
            repository_id="repo/acme",
            target_id=assignment.target_profile_id,
            provider_id=assignment.provider_id,
            operator_id="operator/alice",
        )

    service = DurableAgentService(
        Provider(),
        Tools(),
        store,
        model="test",
        portable_memory=memory,
        portable_memory_scope=scope,
    )
    _ = [
        event
        async for event in service.start_swarm(
            SwarmRequest(session_id="session-empty", objective="Assess target", max_workers=2)
        )
    ]

    assert len(memory.queries) == 2
    assert memory.uses == []


def test_portable_memory_requires_explicit_scope_provider() -> None:
    with pytest.raises(ValueError, match="explicit scope"):
        DurableAgentService(
            Provider(), Tools(), Store(), portable_memory=MemoryProvider(Store(), active=True)
        )


async def test_failed_workers_are_retried_once_with_new_fenced_generations() -> None:
    store = Store()
    provider = RetryProvider()
    service = DurableAgentService(provider, Tools(), store, model="test")
    async for _ in service.start_swarm(
        SwarmRequest(session_id="session-1", objective="Assess target", max_workers=2)
    ):
        pass
    worker_nodes = [node for node in store.nodes["schedule-1"] if node.startswith("worker:")]
    assert {store.nodes["schedule-1"][node]["generation"] for node in worker_nodes} == {2}
    assert {store.nodes["schedule-1"][node]["state"] for node in worker_nodes} == {"completed"}


async def test_only_committed_attributable_structured_findings_reach_sink() -> None:
    store = Store()
    sink = FindingSink(store)
    service = DurableAgentService(
        FindingProvider(), Tools(), store, model="test", finding_sink=sink
    )

    async for _ in service.start_swarm(
        SwarmRequest(session_id="session-1", objective="Assess target", max_workers=2)
    ):
        pass

    assert len(sink.created) == 2
    for session_id, draft in sink.created:
        assert session_id == "session-1"
        assert draft.title == "Missing security header"
        assert draft.severity == "medium"
        assert draft.creator in {"headers", "body"}
        assert draft.target_id == "target"
        assert draft.technique_id == draft.creator
        result = store.results["schedule-1"][f"worker:{draft.creator}"]["payload"]
        assert result["terminal_prompt_state"] == "captured"
        assert result["terminal_prompt_digest"].startswith("sha256:")
        reference = draft.evidence_references[0]
        assert reference.startswith("merlin-result://schedule/schedule-1/node/worker:")
        assert "/attempt/" in reference
        assert (
            f"?digest=sha256:test&terminal_prompt_digest={result['terminal_prompt_digest']}"
            in reference
        )


async def test_finding_attribution_rejects_non_admitted_and_ambiguous_techniques() -> None:
    store = Store()

    class Sink:
        def __init__(self) -> None:
            self.created = []

        async def create(self, session_id, draft):
            self.created.append((session_id, draft))

    sink = Sink()
    service = DurableAgentService(Provider(), Tools(), store, model="test", finding_sink=sink)
    assignment = WorkerAssignment(
        id="task",
        objective="Assess target",
        profile_id="recon",
        target_profile_id="reviewed-target",
        technique_ids=("technique/one", "technique/two"),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="test",
        authority_ids=("session",),
        budget=AssignmentBudget(3, 10, 30),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit(
        "session-1",
        (assignment,),
        max_workers=1,
        start_immediately=False,
        minimum_workers=1,
    )
    lease = SimpleNamespace(node_id="worker:task", id="attempt-1")
    base = {
        "title": "Attributed finding",
        "severity": "medium",
        "summary": "Observed behavior.",
        "reproduction": "Replay the exchange.",
        "remediation": "Enforce the boundary.",
    }

    await service._persist_finding_drafts(  # noqa: SLF001
        live,
        lease,
        {"finding_drafts": [base, {**base, "technique_id": "technique/foreign"}]},
        "sha256:test",
    )
    assert sink.created == []

    await service._persist_finding_drafts(  # noqa: SLF001
        live,
        lease,
        {"finding_drafts": [{**base, "technique_id": "technique/two"}]},
        "sha256:test",
    )
    assert len(sink.created) == 1
    _, draft = sink.created[0]
    assert draft.target_id == "reviewed-target"
    assert draft.technique_id == "technique/two"


async def test_finding_draft_drops_are_journaled_with_reasons() -> None:
    store = Store()

    class Sink:
        def __init__(self) -> None:
            self.created = []

        async def create(self, session_id, draft):
            self.created.append((session_id, draft))

    sink = Sink()
    service = DurableAgentService(Provider(), Tools(), store, model="test", finding_sink=sink)
    assignment = WorkerAssignment(
        id="task",
        objective="Assess target",
        profile_id="recon",
        target_profile_id="reviewed-target",
        technique_ids=("technique/one",),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="test",
        authority_ids=("session",),
        budget=AssignmentBudget(3, 10, 30),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit(
        "session-1",
        (assignment,),
        max_workers=1,
        start_immediately=False,
        minimum_workers=1,
    )
    lease = SimpleNamespace(node_id="worker:task", id="attempt-1")

    await service._persist_finding_drafts(  # noqa: SLF001
        live,
        lease,
        {
            "finding_drafts": [
                # The production failure shape: rich free-form keys, none of the
                # required summary/reproduction/remediation fields.
                {
                    "title": "Session budget rotates",
                    "severity": "medium",
                    "impact": "Unbounded billed exchanges per account.",
                    "observed_effect": "Session auto-rotated at turn 18.",
                    "technique_id": "technique/one",
                },
                "not-an-object",
            ]
        },
        "sha256:test",
    )

    assert sink.created == []
    drops = [
        payload
        for _, event_type, payload in store.lifecycle_events
        if event_type == "finding_draft_dropped"
    ]
    assert len(drops) == 2
    by_reason = {drop["reason"]: drop for drop in drops}
    assert by_reason["missing_required_fields"]["missing_fields"] == (
        "summary",
        "reproduction",
        "remediation",
    )
    assert by_reason["missing_required_fields"]["title"] == "Session budget rotates"
    assert "candidate_not_object" in by_reason


@pytest.mark.parametrize(
    ("verdict", "expected_count"),
    [("positive", 1), ("negative", 1), ("ambiguous", 1), ("blocked", 1)],
)
async def test_replay_finding_drafts_require_positive_comparison(
    verdict: str, expected_count: int
) -> None:
    store = Store()
    store.baseline_gate = lambda _schedule_id: {verdict: ("agenda/one",)}
    sink = SimpleNamespace(created=[])

    async def create(session_id, draft):
        sink.created.append((session_id, draft))

    sink.create = create
    service = DurableAgentService(Provider(), Tools(), store, model="test", finding_sink=sink)
    assignment = WorkerAssignment(
        id="replay",
        objective="Replay",
        profile_id="injection",
        target_profile_id="reviewed-target",
        technique_ids=("technique/one",),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="test",
        authority_ids=("session",),
        budget=AssignmentBudget(3, 10, 30),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        agenda_id="agenda/one",
        execution_stage="replay",
        pair_id="control/one",
        predecessor_assignment_id="direct",
        oracle_digest="sha256:" + "1" * 64,
        semantic_technique_id="technique/one",
        target_session_policy="same-reviewed-session",
        comparison_policy="reviewed-benign-differential-v1",
        reviewed_adapter_digest="sha256:" + "2" * 64,
        comparison_dimensions=(
            ("modality", "text"),
            ("turn", "single"),
            ("language", "und"),
        ),
    )
    live = SimpleNamespace(id="schedule-1", session_id="session-1", assignments=(assignment,))
    candidate = {
        "title": "Model claimed success",
        "severity": "medium",
        "summary": "Untrusted prose.",
        "reproduction": "Replay.",
        "remediation": "Enforce.",
    }

    await service._persist_finding_drafts(  # noqa: SLF001
        live,
        SimpleNamespace(node_id="worker:replay", id="attempt-1"),
        {"finding_drafts": [candidate]},
        "sha256:test",
    )

    assert len(sink.created) == expected_count


@pytest.mark.parametrize(
    ("verdict", "expected_count"),
    [("positive", 1), ("negative", 1), ("ambiguous", 1)],
)
async def test_direct_finding_drafts_follow_two_stage_comparison(
    verdict: str, expected_count: int
) -> None:
    store = Store()
    store.baseline_gate = lambda _schedule_id: {verdict: ("agenda/one",)}
    sink = SimpleNamespace(created=[])

    async def create(session_id, draft):
        sink.created.append((session_id, draft))

    sink.create = create
    service = DurableAgentService(Provider(), Tools(), store, model="test", finding_sink=sink)
    assignment = WorkerAssignment(
        id="direct",
        objective="Attempt reviewed direct prompt",
        profile_id="injection",
        target_profile_id="reviewed-target",
        technique_ids=("technique/one",),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="test",
        authority_ids=("session",),
        budget=AssignmentBudget(3, 10, 30),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        agenda_id="agenda/one",
        execution_stage="direct",
        pair_id="control/one",
        predecessor_assignment_id="control",
        oracle_digest="sha256:" + "1" * 64,
        semantic_technique_id="technique/one",
        target_session_policy="same-reviewed-session",
        comparison_policy="reviewed-benign-differential-v1",
        reviewed_adapter_digest="sha256:" + "2" * 64,
        comparison_dimensions=(
            ("modality", "text"),
            ("turn", "single"),
            ("language", "und"),
        ),
    )
    live = SimpleNamespace(id="schedule-1", session_id="session-1", assignments=(assignment,))
    candidate = {
        "title": "Oracle-supported direct behavior",
        "severity": "high",
        "summary": "The direct exchange matched the reviewed oracle.",
        "reproduction": "Repeat only when validation policy requires it.",
        "remediation": "Enforce the reviewed boundary.",
        "causal_ambiguity": False,
        "impact_level": 3,
        "requires_independent_reproduction": True,
    }

    await service._persist_finding_drafts(  # noqa: SLF001
        live,
        SimpleNamespace(node_id="worker:direct", id="attempt-1"),
        {"finding_drafts": [candidate]},
        "sha256:test",
    )

    assert len(sink.created) == expected_count
    if expected_count:
        _, draft = sink.created[0]
        assert draft.causal_ambiguity is True
        assert draft.impact_level == 3
        assert draft.requires_independent_reproduction


async def test_evaluator_gate_cannot_terminalize_observed_attempt() -> None:
    class TerminalStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.terminalizations = []

        def baseline_gate(self, _schedule_id):
            raise AssertionError("evaluator gate must not own attempt outcome")

        def terminalize_technique_attempt(self, attempt_id, state, **metadata):
            self.terminalizations.append((attempt_id, state, metadata))

    store = TerminalStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    assignment = SimpleNamespace(
        id="direct",
        agenda_id="agenda/one",
        execution_stage="direct",
        strategy_id="direct",
        target_profile_id="target",
        # Planning metadata may describe the catalogue default; only the
        # explicitly requested execution language is the candidate axis.
        planned_language="fr",
        requested_language="und",
        requested_transform_id="baseline",
        semantic_technique_id="technique/one",
        technique_ids=("technique/one",),
        objective="ordinary execution",
    )

    await service._terminalize_observed_technique_attempt(  # noqa: SLF001
        SimpleNamespace(id="schedule-1"),
        assignment,
        "technique-attempt-1",
        digest="sha256:direct-evidence",
        usage={"total_tokens": 7},
        checkpoint={"target_evidence": []},
    )

    assert store.terminalizations == []


async def test_sealed_direct_candidate_binds_attempt_without_oracle_digest() -> None:
    identity = {
        "campaign_id": "campaign/one",
        "target_id": "target",
        "target_generation": "generation-1",
        "scenario_fingerprint": "scenario-1",
        "technique_id": "technique/one",
        "controller_id": "direct",
        "payload_family": "baseline",
        "requested_language": "und",
        "mutation_id": "baseline",
    }

    class BindingStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.bindings = []
            self.terminalizations = []

        def bind_technique_attempt(self, *args, **metadata):
            self.bindings.append((args, metadata))
            return "technique-attempt-1"

        def terminalize_technique_attempt(self, attempt_id, state, **metadata):
            self.terminalizations.append((attempt_id, state, metadata))

    store = BindingStore()
    assignment = WorkerAssignment(
        id="campaign/one/direct",
        objective=(
            "execute\n\nSealed candidate identities: "
            + json.dumps([identity], sort_keys=True, separators=(",", ":"))
        ),
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("technique/one",),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("session",),
        budget=AssignmentBudget(3, 10_000, 30),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    service = DurableAgentService(Provider(), Tools(), store, model="test")

    live = await service.submit("session", (assignment,), max_workers=1, minimum_workers=1)
    assert live.execution is not None
    await live.execution

    assert len(store.bindings) == 1
    args, metadata = store.bindings[0]
    assert args[0] == live.id
    assert args[1] == assignment.id
    assert metadata["oracle_digest"] == (
        "sha256:"
        + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert metadata["strategy_state"]["candidate_identity"] == identity


async def test_sealed_variant_binds_executable_technique_id() -> None:
    executable_technique_id = "technique/one@lang=en@variant=native"
    identity = {
        "campaign_id": "campaign/one",
        "target_id": "target",
        "target_generation": "generation-1",
        "scenario_fingerprint": "scenario-1",
        "technique_id": executable_technique_id,
        "controller_id": "crescendo",
        "payload_family": "native",
        "requested_language": "en",
        "mutation_id": "mutation-1",
    }

    class BindingStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.bindings = []

        def bind_technique_attempt(self, *args, **metadata):
            self.bindings.append((args, metadata))
            return "technique-attempt-1"

    store = BindingStore()
    assignment = WorkerAssignment(
        id="campaign/one/crescendo",
        objective=(
            "execute\n\nSealed candidate identities: "
            + json.dumps([identity], sort_keys=True, separators=(",", ":"))
        ),
        profile_id="adversarial-explorer",
        target_profile_id="target",
        technique_ids=(executable_technique_id,),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("session",),
        budget=AssignmentBudget(3, 10_000, 30),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        semantic_technique_id="technique/one",
        strategy_id="crescendo",
        requested_language="en",
        requested_transform_id="native",
    )
    service = DurableAgentService(Provider(), Tools(), store, model="test")

    live = await service.submit("session", (assignment,), max_workers=1, minimum_workers=1)
    assert live.execution is not None
    await live.execution

    assert len(store.bindings) == 1
    _, metadata = store.bindings[0]
    assert metadata["technique_id"] == executable_technique_id
    assert metadata["strategy_state"]["candidate_identity"] == identity


def test_sealed_assignment_candidate_identity_uses_executable_id() -> None:
    executable_technique_id = "technique/one@lang=en@variant=native"
    identity = {
        "campaign_id": "campaign/one",
        "target_id": "target",
        "target_generation": "generation-1",
        "scenario_fingerprint": "scenario-1",
        "technique_id": executable_technique_id,
        "controller_id": "crescendo",
        "payload_family": "native",
        "requested_language": "en",
        "mutation_id": "mutation-1",
    }
    assignment = SimpleNamespace(
        id="campaign/one/crescendo",
        objective=(
            "execute\n\nSealed candidate identities: "
            + json.dumps([identity], sort_keys=True, separators=(",", ":"))
        ),
        target_profile_id="target",
        technique_ids=(executable_technique_id,),
        semantic_technique_id="technique/one",
        strategy_id="crescendo",
        requested_language="en",
        requested_transform_id="native",
    )

    assert _sealed_assignment_candidate_identity(assignment, sealed_arm=None) == identity


@pytest.mark.parametrize("outcome", ["confirmed", "refuted"])
async def test_target_authoritative_receipt_terminalizes_exact_candidate(outcome: str) -> None:
    executable_technique_id = "technique/one@lang=und@variant=native"
    identity = {
        "campaign_id": "campaign/one",
        "target_id": "target",
        "target_generation": "generation-1",
        "scenario_fingerprint": "scenario-1",
        "technique_id": executable_technique_id,
        "controller_id": "direct",
        "payload_family": "baseline",
        "requested_language": "und",
        "mutation_id": "baseline",
    }

    class TerminalStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.terminalizations = []

        def terminalize_technique_attempt(self, attempt_id, state, **metadata):
            self.terminalizations.append((attempt_id, state, metadata))

    store = TerminalStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    assignment = SimpleNamespace(
        id="campaign/one/direct",
        agenda_id="agenda/one",
        execution_stage="direct",
        strategy_id="direct",
        target_profile_id="target",
        planned_language="und",
        requested_language="und",
        requested_transform_id="baseline",
        semantic_technique_id="technique/one",
        technique_ids=(executable_technique_id,),
        objective=(
            "execute\n\nSealed candidate identities: "
            + json.dumps([identity], sort_keys=True, separators=(",", ":"))
        ),
    )

    await service._terminalize_observed_technique_attempt(  # noqa: SLF001
        SimpleNamespace(id="schedule-1"),
        assignment,
        "technique-attempt-1",
        digest="sha256:direct-evidence",
        usage={"total_tokens": 7},
        checkpoint={
            "target_evidence": [
                {
                    "call_id": "target-call-1",
                    "technique_attempt_id": "technique-attempt-1",
                    "outcome_authority": "target",
                    "attempt_disposition": "valid",
                    "physical_state_identity": {"selected_digest": "sha256:state-1"},
                    "state_identity_match": True,
                    "target_authoritative_outcome": outcome,
                }
            ]
        },
    )

    assert store.terminalizations == [
        (
            "technique-attempt-1",
            outcome,
            {
                "reason": f"target-owned {outcome} outcome",
                "evidence_refs": ("sha256:direct-evidence",),
                "usage": {"total_tokens": 7},
                "disposition": "valid",
                "disposition_reason": "target-authored outcome matched the reviewed oracle",
                "physical_state_identity": "sha256:state-1",
                "state_identity_match": True,
                "strategy_state": {
                    "agenda_id": "agenda/one",
                    "execution_stage": "direct",
                    "candidate_identity": identity,
                    "outcome_authority": {
                        "kind": "target",
                        "outcome": outcome,
                        "evidence_refs": ["sha256:direct-evidence"],
                        "target_call_ids": ["target-call-1"],
                    },
                    "attempt_disposition": {
                        "kind": "valid",
                        "reason": "target-authored outcome matched the reviewed oracle",
                    },
                    "candidate_outcome_projection": project_candidate_outcome(
                        candidate_id=candidate_id_for_identity(identity),
                        attempt_disposition="valid",
                        outcome_authority="target",
                        outcome=outcome,
                        evidence_digest=candidate_evidence_digest(("sha256:direct-evidence",)),
                    ),
                    "physical_state_identity": "sha256:state-1",
                    "state_identity_match": True,
                },
            },
        )
    ]


async def test_contaminated_target_receipt_terminalizes_retryably_without_quality() -> None:
    class TerminalStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.terminalizations = []

        def terminalize_technique_attempt(self, attempt_id, state, **metadata):
            self.terminalizations.append((attempt_id, state, metadata))

    store = TerminalStore()
    observations: list[dict[str, object]] = []
    service = DurableAgentService(
        Provider(),
        Tools(),
        store,
        model="test",
        arm_outcome_recorder=observations.append,
    )
    assignment = SimpleNamespace(
        id="campaign/one/direct",
        agenda_id="agenda/one",
        execution_stage="direct",
        strategy_id="direct",
        target_profile_id="target",
        planned_language="und",
        requested_language="und",
        requested_transform_id="baseline",
        semantic_technique_id="technique/one",
        technique_ids=("technique/one",),
        objective="ordinary execution",
    )

    await service._terminalize_observed_technique_attempt(  # noqa: SLF001
        SimpleNamespace(id="schedule-1"),
        assignment,
        "technique-attempt-1",
        digest="sha256:contaminated-evidence",
        usage={"total_tokens": 7},
        checkpoint={
            "target_evidence": [
                {
                    "call_id": "target-call-clean",
                    "technique_attempt_id": "technique-attempt-1",
                    "outcome_authority": "target",
                    "attempt_disposition": "valid",
                    "target_authoritative_outcome": "confirmed",
                },
                {
                    "call_id": "target-call-1",
                    "technique_attempt_id": "technique-attempt-1",
                    "outcome_authority": "target",
                    "attempt_disposition": "contaminated",
                    "target_authoritative_outcome": "refuted",
                },
            ]
        },
    )

    assert store.terminalizations == [
        (
            "technique-attempt-1",
            "ambiguous",
            {
                "reason": "target attempt ended with contaminated evidence",
                "evidence_refs": ("sha256:contaminated-evidence",),
                "usage": {"total_tokens": 7},
                "strategy_state": {
                    "agenda_id": "agenda/one",
                    "execution_stage": "direct",
                    "attempt_disposition": {
                        "kind": "contaminated",
                        "reason": "target attempt ended with contaminated evidence",
                    },
                },
                "disposition": "contaminated",
                "disposition_reason": "target attempt ended with contaminated evidence",
                "physical_state_identity": None,
                "state_identity_match": None,
            },
        )
    ]
    assert observations == []


async def test_protocol_invalid_attempt_persists_bounded_child_without_quality_credit() -> None:
    class TerminalStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.terminalizations = []

        def terminalize_technique_attempt(self, attempt_id, state, **metadata):
            self.terminalizations.append((attempt_id, state, metadata))

    store = TerminalStore()
    observations: list[dict[str, object]] = []
    service = DurableAgentService(
        Provider(),
        Tools(),
        store,
        model="test",
        arm_outcome_recorder=observations.append,
    )
    assignment = SimpleNamespace(
        id="campaign/one/direct",
        agenda_id="agenda/one",
        execution_stage="direct",
        strategy_id="direct",
        target_profile_id="target",
        planned_language="und",
        requested_language="und",
        requested_transform_id="baseline",
        semantic_technique_id="technique/one",
        technique_ids=("technique/one",),
        objective="ordinary execution",
    )
    child = {
        "id": "candidate/child",
        "variant_id": "bounded-1",
        "variant": "short payload",
        "reason": "target accepts at most 500 characters",
        "parent_candidate_id": "candidate/parent",
    }

    await service._terminalize_observed_technique_attempt(  # noqa: SLF001
        SimpleNamespace(id="schedule-1"),
        assignment,
        "technique-attempt-1",
        digest="sha256:protocol-evidence",
        usage={"total_tokens": 7},
        checkpoint={
            "target_evidence": [
                {
                    "call_id": "target-call-1",
                    "technique_attempt_id": "technique-attempt-1",
                    "attempt_disposition": "protocol_invalid",
                    "disposition_reason": "rendered request exceeded reviewed maximum",
                    "bounded_child_candidate": child,
                }
            ]
        },
    )

    assert store.terminalizations == [
        (
            "technique-attempt-1",
            "ambiguous",
            {
                "reason": "rendered request exceeded reviewed maximum",
                "evidence_refs": ("sha256:protocol-evidence",),
                "usage": {"total_tokens": 7},
                "strategy_state": {
                    "agenda_id": "agenda/one",
                    "execution_stage": "direct",
                    "attempt_disposition": {
                        "kind": "protocol_invalid",
                        "reason": "rendered request exceeded reviewed maximum",
                    },
                    "bounded_child_candidate": child,
                },
                "disposition": "protocol_invalid",
                "disposition_reason": "rendered request exceeded reviewed maximum",
                "physical_state_identity": None,
                "state_identity_match": None,
            },
        )
    ]
    assert observations == []


async def test_terminalized_attempt_upserts_arm_outcome_with_pinned_cost() -> None:
    identity = {
        "campaign_id": "campaign/one",
        "target_id": "target",
        "target_generation": "generation-1",
        "scenario_fingerprint": "scenario-1",
        "technique_id": "technique/one",
        "controller_id": "direct",
        "payload_family": "baseline",
        "requested_language": "und",
        "mutation_id": "baseline",
    }
    contract = {
        "id": "hypothesis:one",
        "candidate_identity": identity,
        "arm": {
            "id": "arm-1",
            "target_id": "target",
            "scenario_fingerprint": "scenario-1",
            "surface_id": "surface-1",
            "technique_id": "technique/one",
            "controller_id": "direct",
            "mutation_id": "baseline",
            "actual_language": "und",
            "state_id": "state-1",
        },
    }

    class TerminalStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.terminalizations = []

        def terminalize_technique_attempt(self, attempt_id, state, **metadata):
            self.terminalizations.append((attempt_id, state, metadata))

    observations: list[dict[str, object]] = []
    store = TerminalStore()
    service = DurableAgentService(
        Provider(),
        Tools(),
        store,
        model="test",
        arm_outcome_recorder=observations.append,
    )
    assignment = SimpleNamespace(
        id="campaign/one/direct",
        agenda_id="agenda/one",
        execution_stage="direct",
        strategy_id="direct",
        target_profile_id="target",
        planned_language="und",
        requested_language="und",
        requested_transform_id="baseline",
        semantic_technique_id="technique/one",
        technique_ids=("technique/one",),
        provider_input_cost_per_million=1.0,
        provider_output_cost_per_million=2.0,
        objective=(
            "execute\n\nSealed hypothesis contract: "
            + json.dumps(contract, sort_keys=True, separators=(",", ":"))
        ),
    )

    await service._terminalize_observed_technique_attempt(  # noqa: SLF001
        SimpleNamespace(id="schedule-1"),
        assignment,
        "technique-attempt-1",
        digest="sha256:direct-evidence",
        usage={"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
        checkpoint={
            "target_evidence": [
                {
                    "call_id": "target-call-1",
                    "technique_attempt_id": "technique-attempt-1",
                    "outcome_authority": "target",
                    "attempt_disposition": "valid",
                    "target_authoritative_outcome": "confirmed",
                }
            ]
        },
    )

    assert len(observations) == 1
    observation = observations[0]
    assert observation["campaign_id"] == "campaign/one"
    assert observation["arm_id"] == "arm-1"
    assert observation["technique_attempt_id"] == "technique-attempt-1"
    assert observation["outcome"] == "authoritative_success"
    assert observation["attempt_disposition"] == "valid"
    assert observation["outcome_authority"] == "target"
    assert observation["target_calls"] == 1
    assert observation["evidence_refs"] == ("sha256:direct-evidence",)
    assert observation["model_cost_usd"] == pytest.approx(0.002)
    usage = observation["usage"]
    assert isinstance(usage, dict)
    assert usage["estimated_cost_usd"] == pytest.approx(0.002)
    assert usage["cost_source"] == "pinned_assignment_pricing"


def test_usage_with_assignment_cost_applies_cache_read_rate() -> None:
    assignment = SimpleNamespace(
        provider_input_cost_per_million=1.0,
        provider_output_cost_per_million=2.0,
        provider_cache_read_input_cost_per_million=0.25,
    )

    usage = _usage_with_assignment_cost(
        {
            "input_tokens": 1000,
            "output_tokens": 500,
            "total_tokens": 1500,
            "uncached_input_tokens": 600,
            "cache_read_input_tokens": 400,
        },
        assignment,
    )

    # (600 * 1.0 + 400 * 0.25 + 500 * 2.0) / 1e6
    assert usage["estimated_cost_usd"] == pytest.approx(0.0017)
    assert usage["cost_source"] == "pinned_assignment_pricing"


def test_usage_with_assignment_cost_without_cache_rate_uses_input_rate() -> None:
    assignment = SimpleNamespace(
        provider_input_cost_per_million=1.0,
        provider_output_cost_per_million=2.0,
        provider_cache_read_input_cost_per_million=None,
    )

    usage = _usage_with_assignment_cost(
        {
            "input_tokens": 1000,
            "output_tokens": 500,
            "total_tokens": 1500,
            "uncached_input_tokens": 600,
            "cache_read_input_tokens": 400,
        },
        assignment,
    )

    # Unknown cache rate: cache reads billed at the full input rate.
    assert usage["estimated_cost_usd"] == pytest.approx(0.002)


def test_usage_with_assignment_cost_legacy_usage_without_split_is_unchanged() -> None:
    assignment = SimpleNamespace(
        provider_input_cost_per_million=1.0,
        provider_output_cost_per_million=2.0,
        provider_cache_read_input_cost_per_million=0.25,
    )

    usage = _usage_with_assignment_cost(
        {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
        assignment,
    )

    assert usage["estimated_cost_usd"] == pytest.approx(0.002)


@pytest.mark.parametrize(
    ("axis", "value"),
    [
        ("requested_language", "fr"),
        ("requested_transform_id", "encoding/base64"),
        ("strategy_id", "crescendo"),
    ],
)
async def test_target_authority_cannot_resolve_mismatched_execution_axis(
    axis: str, value: str
) -> None:
    identity = {
        "campaign_id": "campaign/one",
        "target_id": "target",
        "target_generation": "generation-1",
        "scenario_fingerprint": "scenario-1",
        "technique_id": "technique/one",
        "controller_id": "direct",
        "payload_family": "baseline",
        "requested_language": "und",
        "mutation_id": "baseline",
    }

    class TerminalStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.terminalizations = []

        def terminalize_technique_attempt(self, *args, **metadata):
            self.terminalizations.append((args, metadata))

    values = {
        "id": "campaign/one/direct",
        "agenda_id": "agenda/one",
        "execution_stage": "direct",
        "strategy_id": "direct",
        "target_profile_id": "target",
        "planned_language": "und",
        "requested_language": "und",
        "requested_transform_id": "baseline",
        "semantic_technique_id": "technique/one",
        "technique_ids": ("technique/one",),
        "objective": (
            "execute\n\nSealed candidate identities: "
            + json.dumps([identity], sort_keys=True, separators=(",", ":"))
        ),
    }
    values[axis] = value
    store = TerminalStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")

    await service._terminalize_observed_technique_attempt(  # noqa: SLF001
        SimpleNamespace(id="schedule-1"),
        SimpleNamespace(**values),
        "technique-attempt-1",
        digest="sha256:direct-evidence",
        usage={},
        checkpoint={
            "target_evidence": [
                {
                    "call_id": "target-call-1",
                    "technique_attempt_id": "technique-attempt-1",
                    "outcome_authority": "target",
                    "target_authoritative_outcome": "confirmed",
                }
            ]
        },
    )

    assert store.terminalizations == []


async def test_target_authority_cannot_resolve_mismatched_sealed_mutation() -> None:
    identity = {
        "campaign_id": "campaign/one",
        "target_id": "target",
        "target_generation": "generation-1",
        "scenario_fingerprint": "scenario-1",
        "technique_id": "technique/one",
        "controller_id": "direct",
        "payload_family": "baseline",
        "requested_language": "und",
        "mutation_id": "mutation/original",
    }
    arm = {
        "id": "arm/one",
        "target_id": "target",
        "scenario_fingerprint": "scenario-1",
        "surface_id": "surface/one",
        "technique_id": "technique/one",
        "controller_id": "direct",
        "mutation_id": "mutation/different",
        "actual_language": "und",
        "state_id": "state/one",
    }

    class TerminalStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.terminalizations = []

        def terminalize_technique_attempt(self, *args, **metadata):
            self.terminalizations.append((args, metadata))

    store = TerminalStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    assignment = SimpleNamespace(
        id="campaign/one/direct",
        agenda_id="agenda/one",
        execution_stage="direct",
        strategy_id="direct",
        target_profile_id="target",
        planned_language="und",
        requested_language="und",
        requested_transform_id="baseline",
        semantic_technique_id="technique/one",
        technique_ids=("technique/one",),
        objective=(
            "execute\n\nSealed hypothesis contract: "
            + json.dumps(
                {
                    "candidate_identity": identity,
                    "candidate_identities": [identity],
                    "arm": arm,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
    )

    await service._terminalize_observed_technique_attempt(  # noqa: SLF001
        SimpleNamespace(id="schedule-1"),
        assignment,
        "technique-attempt-1",
        digest="sha256:direct-evidence",
        usage={},
        checkpoint={
            "target_evidence": [
                {
                    "call_id": "target-call-1",
                    "technique_attempt_id": "technique-attempt-1",
                    "outcome_authority": "target",
                    "target_authoritative_outcome": "confirmed",
                }
            ]
        },
    )

    assert store.terminalizations == []


@pytest.mark.parametrize("strategy_id", ["goat", "hydra-select"])
async def test_adaptive_positive_receipt_creates_one_stable_provisional_finding(
    strategy_id: str,
) -> None:
    assignment = SimpleNamespace(
        id="adaptive",
        execution_stage="attack",
        strategy_id=strategy_id,
        technique_ids=("technique/one",),
        target_profile_id="reviewed-target",
    )
    receipt = {
        "semantic_technique_id": "technique/one",
        "evidence_verdict": "positive",
        "receipt_digest": "sha256:" + "a" * 64,
    }

    class GateStore(Store):
        def goat_gate(self, _schedule_id):
            return {"positive": ("adaptive",), "strategies": {"adaptive": receipt}}

        def hydra_select_gate(self, _schedule_id):
            return {"positive": ("adaptive",), "strategies": {"adaptive": receipt}}

    class IdempotentSink:
        def __init__(self) -> None:
            self.created = {}

        async def create(self, session_id, draft):
            self.created[draft.stable_identity()] = (session_id, draft)
            return draft

    sink = IdempotentSink()
    service = DurableAgentService(Provider(), Tools(), GateStore(), model="test", finding_sink=sink)
    live = SimpleNamespace(id="schedule-1", session_id="session-1", assignments=(assignment,))
    service._live["schedule-1"] = live  # noqa: SLF001

    # The positive receipt already exists, modelling a crash after result commit but
    # before the original Finding write. Recovery reconciles the outbox idempotently.
    await service.reconcile_adaptive_findings("schedule-1")
    await service.reconcile_adaptive_findings("schedule-1")

    assert len(sink.created) == 1
    session_id, draft = next(iter(sink.created.values()))
    assert session_id == "session-1"
    assert draft.validation == "unvalidated"
    assert draft.creator == "adaptive"
    assert draft.technique_id == "technique/one"
    assert receipt["receipt_digest"] in draft.evidence_references[0]


@pytest.mark.parametrize("strategy_id", ["goat", "hydra-select"])
async def test_adaptive_negative_receipt_creates_no_finding(strategy_id: str) -> None:
    assignment = SimpleNamespace(
        id="adaptive",
        execution_stage="attack",
        strategy_id=strategy_id,
        technique_ids=("technique/one",),
        target_profile_id="reviewed-target",
    )

    class GateStore(Store):
        def goat_gate(self, _schedule_id):
            return {"negative": ("adaptive",), "positive": (), "strategies": {}}

        hydra_select_gate = goat_gate

    sink = SimpleNamespace(created=[])

    async def create(session_id, draft):
        sink.created.append((session_id, draft))

    sink.create = create
    service = DurableAgentService(Provider(), Tools(), GateStore(), model="test", finding_sink=sink)
    await service._persist_finding_drafts(  # noqa: SLF001
        SimpleNamespace(id="schedule-1", session_id="session-1", assignments=(assignment,)),
        SimpleNamespace(node_id="worker:adaptive", id="attempt-1"),
        {"finding_drafts": [{"title": "forged"}]},
        "sha256:result",
    )

    assert sink.created == []


async def test_free_text_and_non_candidate_json_do_not_create_findings() -> None:
    store = Store()
    sink = FindingSink(store)
    service = DurableAgentService(Provider(), Tools(), store, model="test", finding_sink=sink)

    async for _ in service.start_swarm(
        SwarmRequest(session_id="session-1", objective="Assess target", max_workers=2)
    ):
        pass

    assert sink.created == []


async def test_cancel_schedule_persists_durable_cancel_before_stopping_tasks() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider:
        async def stream(self, request):
            del request
            started.set()
            await release.wait()
            yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()

    observed = {}

    class GuardedStore(Store):
        def cancel_schedule(self, schedule_id, *, reason, cleanup_node_ids, checkpoint):
            # At the durable cancel boundary, in-memory tasks must still be
            # running: a crash here must not leave live work against a store
            # that shows the schedule active.
            observed["execution_done"] = execution.done()
            super().cancel_schedule(
                schedule_id,
                reason=reason,
                cleanup_node_ids=cleanup_node_ids,
                checkpoint=checkpoint,
            )

    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/cancel-order-{index}",
            objective="Assess",
            profile_id="recon",
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for index in range(2)
    )
    store = GuardedStore()
    service = DurableAgentService(BlockingProvider(), Tools(), store, model="test")
    live = await service.submit("session-cancel-order", assignments, max_workers=1)
    execution = live.execution
    assert execution is not None
    await asyncio.wait_for(started.wait(), timeout=1)

    await service.cancel_schedule(live.id, reason="operator-cancel-order")

    assert observed["execution_done"] is False
    assert execution.done()
    assert store.schedules[live.id]["status"] == "cancelled"


async def test_cancel_schedule_reconciles_target_lanes_after_local_unwind() -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    class BlockingProvider:
        async def stream(self, request):
            del request
            try:
                started.set()
                await asyncio.Event().wait()
                yield ModelCompleted()  # pragma: no cover
            finally:
                closed.set()

    class ReconciliationStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.reconciliations: list[tuple[str, bool]] = []

        def reconcile_cancelled_target_lanes(self, schedule_id):
            self.reconciliations.append((schedule_id, closed.is_set()))
            return {
                "released": (),
                "held": ("attempt-with-acknowledged-effect",),
                "latched": (),
            }

    store = ReconciliationStore()
    service = DurableAgentService(BlockingProvider(), Tools(), store, model="test")
    assignment = WorkerAssignment(
        id="campaign/cancel-reconcile/task",
        objective="Assess",
        profile_id="recon",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit("session", (assignment,), max_workers=1, minimum_workers=1)
    await asyncio.wait_for(started.wait(), timeout=1)

    await service.cancel_schedule(live.id, reason="operator")

    assert store.reconciliations == [(live.id, True)]
    assert store.schedules[live.id]["status"] == "cancelled"


async def test_reject_schedule_reconciliation_fails_closed_on_ambiguous_projection() -> None:
    class AmbiguousStore(Store):
        def reconcile_cancelled_target_lanes(self, schedule_id):
            assert schedule_id in self.schedules
            return {
                "released": ("attempt-1",),
                "held": ("attempt-1",),
                "latched": (),
            }

    store = AmbiguousStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    assignment = WorkerAssignment(
        id="campaign/reject-reconcile/task",
        objective="Assess",
        profile_id="recon",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit(
        "session", (assignment,), max_workers=1, minimum_workers=1, start_immediately=False
    )

    with pytest.raises(RuntimeError, match="reconciliation is ambiguous"):
        await service.reject_persisted_schedule(live.id, reason="incompatible")

    assert store.schedules[live.id]["status"] == "cancelled"


async def test_pause_resume_preserves_same_source_lane_without_cancel_reconciliation() -> None:
    class TrackingStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_reconciliations = 0

        def reconcile_cancelled_target_lanes(self, schedule_id):
            assert schedule_id in self.schedules
            self.cancel_reconciliations += 1
            return {"released": (), "held": (), "latched": ()}

    store = TrackingStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    assignment = WorkerAssignment(
        id="campaign/pause-lane/task",
        objective="Assess",
        profile_id="recon",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit(
        "session", (assignment,), max_workers=1, minimum_workers=1, start_immediately=False
    )

    await service.pause_schedule(live.id, reason="operator")
    resumed = await service.resume_schedule(live.id)
    assert resumed.execution is not None
    await asyncio.wait_for(resumed.execution, timeout=1)

    assert store.cancel_reconciliations == 0
    assert store.nodes[live.id][f"worker:{assignment.id}"]["state"] == "completed"


async def test_late_cancelled_commit_reconciles_without_attack_failure() -> None:
    class StaleLeaseError(RuntimeError):
        pass

    class CancelledCommitStore(Store):
        def __init__(self) -> None:
            super().__init__()
            self.reconciliations = 0

        def commit_reducer(self, *args, **kwargs):
            del args, kwargs
            raise StaleLeaseError("attempt was cancelled")

        def list_attempts(self, schedule_id):
            assert schedule_id == "schedule-1"
            return [{"id": "attempt-1", "generation": 1, "state": "cancelled"}]

        def reconcile_cancelled_target_lanes(self, schedule_id):
            assert schedule_id == "schedule-1"
            self.reconciliations += 1
            return {"released": (), "held": ("attempt-1",), "latched": ()}

    store = CancelledCommitStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    live = SimpleNamespace(
        id="schedule-1",
        scheduler=SimpleNamespace(checkpoint=lambda: {}),
    )
    lease = SimpleNamespace(id="attempt-1", generation=1)

    digest = await service._commit(  # noqa: SLF001
        live,
        lease,
        {"content": "late", "target_session_id": "browser:session"},
        "schema/result",
    )

    assert digest is None
    assert store.reconciliations == 1


async def test_cancel_schedule_store_failure_leaves_tasks_running() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider:
        async def stream(self, request):
            del request
            started.set()
            await release.wait()
            yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()

    class FailingStore(Store):
        def cancel_schedule(self, schedule_id, *, reason, cleanup_node_ids, checkpoint):
            raise RuntimeError("store unavailable")

    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/cancel-failure-{index}",
            objective="Assess",
            profile_id="recon",
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for index in range(2)
    )
    store = FailingStore()
    service = DurableAgentService(BlockingProvider(), Tools(), store, model="test")
    live = await service.submit("session-cancel-failure", assignments, max_workers=1)
    execution = live.execution
    assert execution is not None
    await asyncio.wait_for(started.wait(), timeout=1)

    with pytest.raises(RuntimeError, match="store unavailable"):
        await service.cancel_schedule(live.id, reason="operator-cancel-failure")

    # The durable cancel did not land, so in-memory work must not be unwound.
    assert not execution.done()
    assert store.schedules[live.id]["status"] != "cancelled"

    release.set()
    await asyncio.gather(execution, return_exceptions=True)


async def test_lease_keeper_covers_slow_setup_and_first_provider_turn() -> None:
    """A worker whose setup (campaign deadline lookup) and first provider turn
    outlast the lease must keep its lease: the keeper starts with the claim,
    not after setup completes."""

    class SlowProvider:
        async def stream(self, request):
            del request
            await asyncio.sleep(1.0)
            yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()

    class StaleLeaseError(RuntimeError):
        pass

    class ExpiryStore(Store):
        def __init__(self):
            super().__init__()
            self.heartbeats = 0

        def heartbeat(self, attempt_id, generation, *, lease_expires_at):
            lease = self.attempts[attempt_id]
            if lease.lease_expires_at <= datetime.now(UTC):
                raise StaleLeaseError("attempt does not own the current lease generation")
            self.heartbeats += 1
            return super().heartbeat(attempt_id, generation, lease_expires_at=lease_expires_at)

    async def slow_deadline(_campaign_id):
        await asyncio.sleep(0.8)
        return None

    store = ExpiryStore()
    service = DurableAgentService(
        SlowProvider(),
        Tools(),
        store,
        model="test",
        lease_seconds=0.5,
        campaign_deadline_provider=slow_deadline,
    )
    assignment = WorkerAssignment(
        id="campaign/c1/live-task",
        objective="Assess",
        profile_id="recon",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit("session", (assignment,), max_workers=1, minimum_workers=1)
    assert live.execution is not None

    await asyncio.wait_for(live.execution, timeout=10)

    # Lease (0.5s) is shorter than setup (0.8s) plus the first provider turn
    # (1.0s): only continuous heartbeats from claim onward keep it alive.
    assert store.heartbeats >= 3
    assert store.nodes[live.id][f"worker:{assignment.id}"]["state"] == "completed"


async def test_lease_keeper_survives_event_loop_starvation() -> None:
    """A provider turn that blocks the event loop past the lease TTL must not
    lose the lease: a dedicated keeper thread keeps heartbeating while the
    asyncio keeper is starved."""

    class BlockingProvider:
        async def stream(self, request):
            del request
            # Deliberate synchronous block: starves the whole event loop.
            time.sleep(1.5)  # noqa: ASYNC251
            yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()

    class StaleLeaseError(RuntimeError):
        pass

    class ExpiryStore(Store):
        def __init__(self):
            super().__init__()
            self.heartbeats = 0
            self.stale_heartbeats = 0

        def heartbeat(self, attempt_id, generation, *, lease_expires_at):
            lease = self.attempts[attempt_id]
            if lease.lease_expires_at <= datetime.now(UTC):
                self.stale_heartbeats += 1
                raise StaleLeaseError("attempt does not own the current lease generation")
            self.heartbeats += 1
            return super().heartbeat(attempt_id, generation, lease_expires_at=lease_expires_at)

    store = ExpiryStore()
    service = DurableAgentService(
        BlockingProvider(),
        Tools(),
        store,
        model="test",
        lease_seconds=0.5,
    )
    assignment = WorkerAssignment(
        id="campaign/c1/live-task",
        objective="Assess",
        profile_id="recon",
        target_profile_id="target",
        technique_ids=(),
        guidance_ids=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    live = await service.submit("session", (assignment,), max_workers=1, minimum_workers=1)
    assert live.execution is not None

    await asyncio.wait_for(live.execution, timeout=10)

    # The provider turn (1.5s) starves the loop for 3x the lease (0.5s): only
    # heartbeats landing from off the event loop keep the attempt leased.
    assert store.stale_heartbeats == 0
    assert store.heartbeats >= 3
    assert store.nodes[live.id][f"worker:{assignment.id}"]["state"] == "completed"


class StaleLeaseError(RuntimeError):
    pass


class FencingStore(Store):
    """Store double with PostgreSQL lease fencing: stale claims/commits raise."""

    def claim(
        self,
        schedule_id,
        node_id,
        owner,
        *,
        lease_expires_at,
        attempt_id=None,
        target_admission=None,
    ):
        node = self.nodes[schedule_id][node_id]
        if node["state"] != "ready":
            raise StaleLeaseError("scheduler node is not ready")
        return super().claim(
            schedule_id,
            node_id,
            owner,
            lease_expires_at=lease_expires_at,
            attempt_id=attempt_id,
            target_admission=target_admission,
        )

    def commit_reducer(self, attempt_id, generation, **kwargs):
        lease = self.attempts[attempt_id]
        node = self.nodes[lease.schedule_id][lease.node_id]
        if lease.generation != node.get("generation") or node["state"] != "leased":
            raise StaleLeaseError("attempt does not own the current lease generation")
        return super().commit_reducer(attempt_id, generation, **kwargs)

    def heartbeat(self, attempt_id, generation, *, lease_expires_at):
        lease = self.attempts[attempt_id]
        if lease.state != "running":
            raise StaleLeaseError("attempt does not own the current lease generation")
        return super().heartbeat(attempt_id, generation, lease_expires_at=lease_expires_at)

    def find_overdue_attempts(self, *, at, limit=100):
        return [
            vars(attempt).copy()
            for attempt in self.attempts.values()
            if attempt.state == "running" and attempt.lease_expires_at <= at
        ][:limit]

    def expire(self, attempt_id, generation):
        lease = self.attempts[attempt_id]
        if lease.generation != generation or lease.state != "running":
            raise StaleLeaseError("attempt does not own the current lease generation")
        lease.state = "expired"
        node = self.nodes[lease.schedule_id][lease.node_id]
        node["state"] = "ready"


@pytest.mark.parametrize(
    "transition_case",
    (
        "success",
        "pre_effect",
        "malformed",
        "ambiguous",
        "continued",
        "wrong_session",
        "unauthorized_tool",
        "generic_click",
        "generic_navigate",
        "generic_nonmaterial",
        "generic_intent",
        "generic_ambiguous",
        "generic_missing_transition",
        "generic_fill_submit",
        "generic_invalid_prior",
    ),
)
async def test_restart_adopts_durable_material_transition_before_provider_or_target_call(
    transition_case: str,
) -> None:
    generic_case = transition_case.startswith("generic_")
    generic_tool_name = (
        "browser_navigate"
        if transition_case == "generic_navigate"
        else "browser_click"
        if generic_case
        else None
    )
    compiled_adapter_digest = "sha256:" + hashlib.sha256(b"restart-compiled").hexdigest()
    transition = {
        "schema": "merlin.target-ui-transition.v1",
        "material": True,
        "adapter_stale": False,
        "before_digest": "sha256:" + hashlib.sha256(b"restart-before").hexdigest(),
        "after_digest": "sha256:" + hashlib.sha256(b"restart-after").hexdigest(),
        "changed_dimensions": ["controls"],
        "added_controls": [
            {
                "role": "dialog",
                "name": "Level complete",
                "frame_ref": "main",
                "surface_scope": "modal",
            }
        ],
        "removed_controls": [],
        "controls_truncated": False,
        "evidence_refs": ["browser:restart-after"],
        "reobservation_recommended": True,
        "transition_digest": "sha256:" + hashlib.sha256(b"raw-restart").hexdigest(),
        "url_change": None,
        "title_change": None,
        "frame_change": None,
        "observation_kind_change": None,
        "reviewed_state_changes": [],
        "state_changes_truncated": False,
        "reviewed_control_state_changes": [],
        "added_visible_text": ["Congratulations! You pass the level."],
    }
    if transition_case in {"pre_effect", "malformed"}:
        transition["adapter_stale"] = True
    if transition_case == "malformed":
        transition["after_digest"] = transition["before_digest"]
    if transition_case == "generic_nonmaterial":
        transition.update(
            {
                "material": False,
                "after_digest": transition["before_digest"],
                "changed_dimensions": [],
                "added_controls": [],
                "added_visible_text": [],
                "reobservation_recommended": False,
                "transition_digest": "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        [transition["before_digest"], transition["before_digest"]],
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            }
        )
    unchanged_digest = "sha256:" + hashlib.sha256(b"restart-unchanged").hexdigest()
    unchanged_transition = {
        "schema": "merlin.target-ui-transition.v1",
        "material": False,
        "adapter_stale": False,
        "before_digest": unchanged_digest,
        "after_digest": unchanged_digest,
        "transition_digest": "sha256:"
        + hashlib.sha256(
            json.dumps(
                [unchanged_digest, unchanged_digest],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "changed_dimensions": [],
        "url_change": None,
        "title_change": None,
        "frame_change": None,
        "observation_kind_change": None,
        "added_controls": [],
        "removed_controls": [],
        "controls_truncated": False,
        "reviewed_state_changes": [],
        "state_changes_truncated": False,
        "reviewed_control_state_changes": [],
        "added_visible_text": [],
        "evidence_refs": ["browser:restart-unchanged"],
        "reobservation_recommended": False,
    }

    class EffectStore:
        def __init__(self) -> None:
            self.records = {}

        async def begin(self, record):
            existing = self.records.get(record.idempotency_key)
            if existing is not None:
                return existing, False
            self.records[record.idempotency_key] = record
            return record, True

        async def adopt_permitted(self, record, permit_digest):
            expected = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        record.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            )
            assert permit_digest == expected
            return await self.begin(record)

        async def finish(self, record):
            self.records[record.idempotency_key] = record
            return record

        async def list_for_session(self, session_id, *, tool_name=None):
            return [
                item
                for item in self.records.values()
                if item.session_id == session_id
                and (tool_name is None or item.tool_name == tool_name)
            ]

        async def list_for_attempt(self, attempt_id, *, limit=None):
            values = [item for item in self.records.values() if item.attempt_id == attempt_id]
            return values if limit is None else values[-limit:]

        async def list_for_campaign(self, campaign_id, *, limit=None):
            records = [item for item in self.records.values() if item.campaign_id == campaign_id]
            return records if limit is None else records[-limit:]

    class SurfaceTool:
        effect = "target_read" if generic_tool_name == "browser_navigate" else "target_network"

        def __init__(self) -> None:
            self.calls = 0

        def describe(self):
            return ToolSpec(
                name=generic_tool_name or "browser_chat_exchange",
                description="Reviewed browser exchange",
                input_schema={"type": "object"},
                interaction_role=(
                    ToolInteractionRole.AUXILIARY
                    if generic_tool_name is not None
                    else ToolInteractionRole.TARGET_EXCHANGE
                ),
            )

        async def resolve_target_session_id(self, _context):
            return "browser:restart-context"

        async def invoke(self, context, arguments):
            del context, arguments
            self.calls += 1
            assert self.calls <= (2 if transition_case in {"ambiguous", "continued"} else 1)
            if generic_tool_name is not None:
                return ToolResult(
                    content="generic reviewed browser surface changed",
                    metadata={
                        "effect": self.effect,
                        "campaign_id": "campaign/restart-transition",
                        "target_profile_id": "target-surface",
                        "compiled_adapter_digest": compiled_adapter_digest,
                        "target_effect_attempted": True,
                        "target_call_budget_charge": 1,
                        "target_session_id": "browser:restart-context",
                        **(
                            {}
                            if transition_case == "generic_missing_transition"
                            else {"target_ui_transition": transition}
                        ),
                    },
                )
            if self.calls == 2 and transition_case == "continued":
                return ToolResult(
                    content="stale worker continued after the transition",
                    metadata={
                        "target_session_id": "browser:restart-context",
                        "target_exchanges": [
                            {
                                "interaction": True,
                                "cleanup": False,
                                "request_id": "continued-request",
                                "status_code": 200,
                            }
                        ],
                        "target_effect_attempted": True,
                        "target_call_budget_charge": 1,
                        "limit_interaction_count": 1,
                    },
                )
            pre_effect_error = transition_case in {"pre_effect", "malformed"}
            return ToolResult(
                content=(
                    "reviewed controls changed before delivery"
                    if pre_effect_error
                    else "level completed before daemon restart"
                ),
                is_error=pre_effect_error,
                metadata={
                    "target_session_id": "browser:restart-context",
                    "target_exchanges": (
                        []
                        if pre_effect_error
                        else [
                            {
                                "interaction": True,
                                "cleanup": False,
                                "request_id": "restart-request",
                                "status_code": 200,
                            }
                        ]
                    ),
                    "target_effect_attempted": False if pre_effect_error else True,
                    "target_call_budget_charge": 0 if pre_effect_error else 1,
                    "limit_interaction_count": 0 if pre_effect_error else 1,
                    "target_ui_transition": transition,
                },
            )

    class PriorSurfaceTool:
        effect = "target_network"

        def __init__(self) -> None:
            self.calls = 0

        def describe(self):
            return ToolSpec(
                name="browser_fill",
                description="Reviewed browser fill",
                input_schema={"type": "object"},
                interaction_role=ToolInteractionRole.AUXILIARY,
            )

        async def resolve_target_session_id(self, _context):
            return "browser:restart-context"

        async def invoke(self, context, arguments):
            del context, arguments
            self.calls += 1
            return ToolResult(
                content="generic reviewed fill settled unchanged",
                metadata={
                    "effect": self.effect,
                    "campaign_id": "campaign/restart-transition",
                    "target_profile_id": "target-surface",
                    "compiled_adapter_digest": (
                        "sha256:" + "0" * 64
                        if transition_case == "generic_invalid_prior"
                        else compiled_adapter_digest
                    ),
                    "target_effect_attempted": True,
                    "target_call_budget_charge": 1,
                    "target_session_id": "browser:restart-context",
                    "target_ui_transition": unchanged_transition,
                },
            )

    class RestartStore(FencingStore):
        def commit_reducer(self, attempt_id, generation, **kwargs):
            digest = super().commit_reducer(attempt_id, generation, **kwargs)
            lease = self.attempts[attempt_id]
            self.results[lease.schedule_id][lease.node_id]["attempt_id"] = attempt_id
            return digest

        def commit_partial(self, attempt_id, generation, *, result, applied_message_ids=()):
            del applied_message_ids
            lease = self.attempts[attempt_id]
            assert lease.generation == generation
            payload = dict(result)
            payload_digest = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest()
            row = {
                "node_id": lease.node_id,
                "attempt_id": attempt_id,
                "generation": generation,
                "schema_id": "schema/partial-result",
                "payload_digest": payload_digest,
                "payload": payload,
            }
            self.results.setdefault(lease.schedule_id, {})[f"partial:{attempt_id}"] = row
            return payload_digest

        def claim(
            self,
            schedule_id,
            node_id,
            owner,
            *,
            lease_expires_at,
            attempt_id=None,
            target_admission=None,
        ):
            prior = next(
                (
                    attempt
                    for attempt in reversed(tuple(self.attempts.values()))
                    if attempt.schedule_id == schedule_id
                    and attempt.node_id == node_id
                    and attempt.state == "expired"
                ),
                None,
            )
            lease = super().claim(
                schedule_id,
                node_id,
                owner,
                lease_expires_at=lease_expires_at,
                attempt_id=attempt_id,
                target_admission=target_admission,
            )
            lease.replaced_attempt_id = prior.id if prior is not None else None
            return lease

        def replacement_attempt_lineage(self, attempt_id, *, limit=None):
            lineage = []
            current = self.attempts[attempt_id]
            while current is not None:
                lineage.append(current.id)
                replaced = getattr(current, "replaced_attempt_id", None)
                current = self.attempts.get(replaced) if replaced is not None else None
            lineage = tuple(reversed(lineage))
            return lineage if limit is None else lineage[-limit:]

    class NoWorkerProvider(Provider):
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            del request
            self.calls += 1
            raise AssertionError("restart transition recovery must not call the provider")
            yield  # pragma: no cover

    async def allow(*_args):
        return True

    assignment = WorkerAssignment(
        id="campaign/restart-transition/worker",
        objective="Run one authorized browser exchange.",
        profile_id="recon",
        target_profile_id="target-surface",
        technique_ids=("surface-transition",),
        guidance_ids=(),
        tool_ids=(
            ("browser_fill", "browser_click")
            if transition_case in {"generic_fill_submit", "generic_invalid_prior"}
            else ((generic_tool_name or "browser_chat_exchange"),)
        ),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_turns=2, max_tokens=20_000, max_seconds=30),
        max_target_exchanges=1,
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        strategy_id="direct",
        strategy_contract=(
            (("compiled_adapter_digest", compiled_adapter_digest),)
            if generic_tool_name is not None
            else ()
        ),
    )
    profile = ContextDocument.create("profile:recon@1", "Observe the reviewed surface.")
    target = ReviewedTargetAdapter(
        "target-surface",
        "browser",
        "https://target.test/chat",
        "web-chat",
        authentication="ambient authenticated browser context",
    )
    worker_context = WorkerContext(
        assignment_id=assignment.id,
        profile=profile,
        guidance=(),
        skills=(),
        tool_ids=assignment.tool_ids,
        target=target,
        authority_ids=assignment.authority_ids,
        strategy_id=assignment.strategy_id,
        strategy_contract=assignment.strategy_contract,
        digest=_digest(
            {
                "assignment_id": assignment.id,
                "profile": asdict(profile),
                "guidance": [],
                "skills": [],
                "tool_ids": assignment.tool_ids,
                "target": asdict(target),
                "authority_ids": assignment.authority_ids,
                "strategy_id": "direct",
                "strategy_contract": assignment.strategy_contract,
            }
        ),
    )
    surface_tool = SurfaceTool()
    prior_surface_tool = PriorSurfaceTool()
    effect_store = EffectStore()
    broker = ToolBroker(
        (
            (prior_surface_tool, surface_tool)
            if transition_case in {"generic_fill_submit", "generic_invalid_prior"}
            else (surface_tool,)
        ),
        confirmer=allow,
        effect_store=effect_store,
    )
    store = RestartStore()
    first = DurableAgentService(NoWorkerProvider(), broker, store, model="test", lease_seconds=0.05)
    live = await first.submit(
        "session-restart-transition",
        (assignment,),
        max_workers=1,
        minimum_workers=1,
        worker_contexts={assignment.id: worker_context},
        start_immediately=False,
    )

    start_handle = live.scheduler.claim("start", "scheduler", 1)
    start_lease = await first._claim(live, start_handle)  # noqa: SLF001
    live.scheduler.complete(start_handle, "start", 2)
    await first._commit(live, start_lease, {"started": True}, "schema/envelope")  # noqa: SLF001
    await first._ready(live, live.scheduler.ready_nodes())  # noqa: SLF001
    node_id = f"worker:{assignment.id}"
    worker_handle = live.scheduler.claim(node_id, node_id, 4)
    worker_lease = await first._claim(live, worker_handle)  # noqa: SLF001
    store.schedules[live.id]["checkpoint"] = live.scheduler.checkpoint()

    if transition_case in {"generic_fill_submit", "generic_invalid_prior"}:
        await broker.invoke(
            "browser_fill",
            ToolContext(
                session_id=live.session_id,
                workspace_root=".",
                call_id="restart-fill",
                agent_id=assignment.id,
                attempt_id=worker_lease.id,
                campaign_id="campaign/restart-transition",
                target_id=assignment.target_profile_id,
                compiled_adapter_digest=compiled_adapter_digest,
            ),
            {"tab_ref": "tab:1", "element_ref": "textbox:message", "value": "probe"},
        )
    await broker.invoke(
        generic_tool_name or "browser_chat_exchange",
        ToolContext(
            session_id=live.session_id,
            workspace_root=".",
            call_id="restart-transition",
            agent_id=assignment.id,
            attempt_id=worker_lease.id,
            campaign_id="campaign/restart-transition",
            target_id=assignment.target_profile_id,
            compiled_adapter_digest=(
                compiled_adapter_digest if generic_tool_name is not None else None
            ),
        ),
        (
            {"tab_ref": "tab:1", "url": "https://target.test/level-2"}
            if generic_tool_name == "browser_navigate"
            else {"tab_ref": "tab:1", "element_ref": "button:next"}
            if generic_tool_name == "browser_click"
            else {"prompt": "authorized probe"}
        ),
    )
    if transition_case in {"generic_intent", "generic_ambiguous"}:
        key, effect = next(iter(effect_store.records.items()))
        effect_store.records[key] = effect.model_copy(
            update={
                "state": (
                    ToolEffectState.INTENT
                    if transition_case == "generic_intent"
                    else ToolEffectState.AMBIGUOUS
                )
            }
        )
    if transition_case in {"ambiguous", "continued"}:
        await broker.invoke(
            "browser_chat_exchange",
            ToolContext(
                session_id=live.session_id,
                workspace_root=".",
                call_id="restart-transition-2",
                agent_id=assignment.id,
                attempt_id=worker_lease.id,
                campaign_id="campaign/restart-transition",
                target_id=assignment.target_profile_id,
            ),
            {"prompt": "stale follow-up"},
        )
    if transition_case in {"wrong_session", "unauthorized_tool"}:
        key, effect = next(iter(effect_store.records.items()))
        effect_store.records[key] = effect.model_copy(
            update=(
                {"session_id": "unrelated-session"}
                if transition_case == "wrong_session"
                else {"tool_name": "unreviewed_target_exchange"}
            )
        )
    worker_lease.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    first._live.clear()  # noqa: SLF001 - simulate daemon process loss

    provider = NoWorkerProvider()
    replacement = DurableAgentService(provider, broker, store, model="test", lease_seconds=0.05)
    reopened = await replacement.reopen(live.id)
    assert reopened.execution is not None
    await asyncio.wait_for(reopened.execution, timeout=2)

    attempts = [attempt for attempt in store.attempts.values() if attempt.node_id == node_id]
    assert len(attempts) == 2
    assert (
        store.nodes[live.id][node_id]["state"]
        == {
            "success": "completed",
            "pre_effect": "failed",
            "malformed": "quarantined",
            "ambiguous": "quarantined",
            "continued": "quarantined",
            "wrong_session": "quarantined",
            "unauthorized_tool": "quarantined",
            "generic_click": "completed",
            "generic_navigate": "completed",
            "generic_nonmaterial": "completed",
            "generic_intent": "quarantined",
            "generic_ambiguous": "quarantined",
            "generic_missing_transition": "quarantined",
            "generic_fill_submit": "completed",
            "generic_invalid_prior": "quarantined",
        }[transition_case]
    )
    assert provider.calls == 0
    assert surface_tool.calls == (2 if transition_case in {"ambiguous", "continued"} else 1)
    assert len(effect_store.records) == (
        2
        if transition_case
        in {"ambiguous", "continued", "generic_fill_submit", "generic_invalid_prior"}
        else 1
    )
    if transition_case in {"generic_fill_submit", "generic_invalid_prior"}:
        assert prior_surface_tool.calls == 1
    assert any(
        event_type == "worker_restart_transition_recovered"
        for _, event_type, _ in store.lifecycle_events
    )
    task = next(
        item for item in await replacement.list_schedule_tasks(live.id) if item.id == assignment.id
    )
    if transition_case in {
        "success",
        "generic_click",
        "generic_navigate",
        "generic_nonmaterial",
        "generic_fill_submit",
    }:
        committed = await replacement.committed_task_result_evidence(live.id, task)
    else:
        row = next(
            item
            for item in reversed(store.list_results(live.id))
            if item.get("node_id") == node_id and item.get("schema_id") == "schema/partial-result"
        )
        if transition_case in {
            "generic_intent",
            "generic_ambiguous",
            "generic_missing_transition",
            "generic_invalid_prior",
        }:
            expected_category = (
                "adapter_state_integrity"
                if transition_case == "generic_invalid_prior"
                else "effect_reconciliation_required"
            )
            assert row["payload"]["infrastructure_failure_category"] == expected_category
        committed = DurableAgentService._committed_task_evidence(  # noqa: SLF001
            live.id,
            task,
            row,
            durable_effects=tuple(broker._effect_store.records.values()),  # noqa: SLF001
            evidence_attempt_ids=tuple(item.id for item in attempts),
        )
    assert committed is not None
    projection = await replacement.completion_projection(live.id)
    assert projection["evidence"][assignment.id] == committed
    lead_evidence = await CampaignService._source_evidence(  # noqa: SLF001
        SimpleNamespace(_agents=replacement),
        live.id,
        (task,),
    )
    assert lead_evidence[assignment.id] == committed
    assert committed["attempt_id"] == attempts[-1].id
    assert committed["evidence_attempt_ids"] == tuple(item.id for item in attempts)
    if transition_case == "success":
        assert tuple(committed["target_evidence_receipts"]) == ("restart-transition",)
        assert committed["target_ui_transition_receipts"] == {}
        receipt = committed["target_evidence_receipts"]["restart-transition"]
        assert receipt["attempt_id"] == attempts[0].id
        assert receipt["acknowledged"] is True
        assert receipt["candidate_outcome_projection"]["outcome"] == "unclear"
    elif transition_case in {"generic_click", "generic_navigate", "generic_fill_submit"}:
        assert committed["target_evidence_receipts"] == {}
        assert tuple(committed["target_ui_transition_receipts"]) == ("restart-transition",)
        receipt = committed["target_ui_transition_receipts"]["restart-transition"]
        assert receipt["tool_name"] == generic_tool_name
        assert receipt["generic_browser_effect"] is True
        assert receipt["generic_browser_effect_class"] == surface_tool.effect
        assert receipt["interaction_count"] == 0
        assert receipt["limit_interaction_count"] == 0
        assert receipt["target_call_budget_charge"] == 1
        assert committed["target_call_ids"] == ()
        obligations = extract_target_reobservation_obligations(
            campaign_id="campaign/restart-transition",
            session_id=live.session_id,
            target_id=assignment.target_profile_id,
            source_schedule_id=live.id,
            source_wave=1,
            task_ids=(assignment.id,),
            evidence={assignment.id: committed},
        )
        assert len(obligations) == 1
        assert obligations[0].target_call_id == "restart-transition"
        projection = project_target_reobservations(
            [
                {
                    "sequence": 1,
                    "payload": obligations[0].observed_payload(
                        "campaign/restart-transition",
                        assignment.target_profile_id,
                    ),
                }
            ]
        )
        assert projection.lead_projection()["generation"]["status"] == "ready"  # type: ignore[index]
    elif transition_case == "generic_nonmaterial":
        assert committed["target_call_ids"] == ()
        assert committed["target_evidence_receipts"] == {}
        assert committed["target_ui_transition_receipts"] == {}
    elif transition_case == "pre_effect":
        assert committed["target_evidence_receipts"] == {}
        assert tuple(committed["target_ui_transition_receipts"]) == ("restart-transition",)
        receipt = committed["target_ui_transition_receipts"]["restart-transition"]
        assert receipt["attempt_id"] == attempts[0].id
        assert receipt["target_effect_attempted"] is False
        assert receipt["interaction_count"] == 0
        assert receipt["limit_interaction_count"] == 0
        assert receipt["target_call_budget_charge"] == 0
        assert committed["target_call_ids"] == ()
    else:
        assert committed["target_evidence_receipts"] == {}
        assert committed["target_ui_transition_receipts"] == {}


def _fencing_assignments(prefix: str, count: int = 2) -> tuple[WorkerAssignment, ...]:
    return tuple(
        WorkerAssignment(
            id=f"{prefix}-{index}",
            objective="Assess",
            profile_id="recon",
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for index in range(count)
    )


async def test_local_projection_lease_survives_maximum_concurrency_burst() -> None:
    """Sibling claims must not expire an otherwise healthy worker locally."""

    class OneSlowProvider(Provider):
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            del request
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(0.5)
            yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()

    store = FencingStore()
    service = DurableAgentService(OneSlowProvider(), Tools(), store, model="test")
    live = await service.submit(
        "session-concurrency-burst",
        _fencing_assignments("campaign/concurrency-burst/worker", count=128),
        max_workers=8,
    )

    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=15)
    assert live.execution.exception() is None
    assert all(node["state"] == "completed" for node in store.nodes[live.id].values())


async def test_fenced_executor_stops_cleanly_after_reducer_committed() -> None:
    """A late claim against an already-reduced schedule is a clean no-op.

    The winning driver committed the reducer (and every node) durably; the
    stale executor that restart recovery left behind must terminate without
    raising out of the execution task.
    """
    store = FencingStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    live = await service.submit(
        "session-fenced",
        _fencing_assignments("campaign/fenced/worker"),
        max_workers=1,
        start_immediately=False,
    )
    # The winning driver finishes the schedule durably behind this executor.
    for node in store.nodes[live.id].values():
        node["state"] = "completed"

    service.start_execution(live, max_workers=1)
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=5)
    assert live.execution.exception() is None
    # The losing driver never mutated the store.
    assert store.attempts == {}


async def test_stale_lease_without_fence_loss_still_raises() -> None:
    """A StaleLeaseError that durable state cannot explain stays a failure."""

    class SpuriousStore(FencingStore):
        def claim(
            self,
            schedule_id,
            node_id,
            owner,
            *,
            lease_expires_at,
            attempt_id=None,
            target_admission=None,
        ):
            del schedule_id, node_id, owner, lease_expires_at, attempt_id, target_admission
            raise StaleLeaseError("scheduler node is not ready")

    store = SpuriousStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    live = await service.submit(
        "session-spurious",
        _fencing_assignments("campaign/spurious/worker"),
        max_workers=1,
        start_immediately=False,
    )
    service.start_execution(live, max_workers=1)
    assert live.execution is not None
    with pytest.raises(StaleLeaseError):
        await asyncio.wait_for(live.execution, timeout=5)


async def test_restart_resume_fences_out_the_pre_restart_executor() -> None:
    """A recovered executor wins the lease; the replaced driver stops cleanly.

    Two service instances share one store (the daemon-restart window). The
    recovered executor expires the inherited lease and finishes the schedule;
    the pre-restart executor's late commit is fenced and must not raise.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider:
        async def stream(self, request):
            del request
            started.set()
            await release.wait()
            yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()

    class RestartFencingStore(FencingStore):
        def __init__(self) -> None:
            super().__init__()
            self.stopped_attempt_ids: set[str] = set()

        def heartbeat(self, attempt_id, generation, *, lease_expires_at):
            if attempt_id in self.stopped_attempt_ids:
                raise StaleLeaseError("pre-restart executor no longer heartbeats")
            return super().heartbeat(
                attempt_id,
                generation,
                lease_expires_at=lease_expires_at,
            )

    store = RestartFencingStore()
    creator = DurableAgentService(
        BlockingProvider(), Tools(), store, model="test", lease_seconds=0.2
    )
    live = await creator.submit(
        "session-restart",
        _fencing_assignments("campaign/restart/worker"),
        max_workers=1,
    )
    assert live.execution is not None
    await asyncio.wait_for(started.wait(), timeout=5)
    pre_restart_attempt = next(
        attempt for attempt in store.attempts.values() if attempt.state == "running"
    )
    # A daemon restart stops the old process's async and thread heartbeat
    # keepers even though this fixture retains its blocked provider so the
    # eventual late commit can prove generation fencing.
    store.stopped_attempt_ids.add(pre_restart_attempt.id)

    recovered = DurableAgentService(Provider(), Tools(), store, model="test", lease_seconds=0.2)
    reopened = await recovered.reopen(live.id)
    assert reopened.execution is not None

    await asyncio.wait_for(reopened.execution, timeout=10)
    # The recovered driver claimed the fenced-out worker with a newer generation.
    worker_node = store.nodes[live.id]["worker:campaign/restart/worker-0"]
    assert worker_node["state"] == "completed"
    assert worker_node["generation"] == 2

    release.set()
    await asyncio.wait_for(live.execution, timeout=10)
    assert live.execution.exception() is None
    # Exactly one completed result per node: no double-driven commits landed.
    worker_results = [
        attempt for attempt in store.attempts.values() if attempt.state == "completed"
    ]
    node_ids = [attempt.node_id for attempt in worker_results]
    assert len(node_ids) == len(set(node_ids))


async def test_live_foreign_driver_fences_out_duplicate_executor() -> None:
    """A duplicate executor started mid-flight winds down without failing.

    Supervision retry can start a second executor while the original driver
    still owns live worker leases: no terminal evidence exists yet (schedule
    running, reducer pending, nothing completed), so the duplicate's
    StaleLeaseError previously escaped and failed the campaign. The winner's
    unexpired running attempts are durable proof of ownership; the duplicate
    must complete cleanly and leave those leases untouched.
    """
    store = FencingStore()
    creator = DurableAgentService(Provider(), Tools(), store, model="test")
    live = await creator.submit(
        "session-duplicate",
        _fencing_assignments("campaign/duplicate/worker"),
        max_workers=1,
        start_immediately=False,
    )
    # The original driver is mid-flight: it owns an unexpired running lease on
    # the first worker node while the rest of the graph is still untouched, so
    # no terminal or generation evidence exists for the duplicate to observe.
    contested = f"worker:{live.assignments[0].id}"
    store.nodes[live.id][contested]["state"] = "ready"
    winner = store.claim(
        live.id,
        contested,
        contested,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
    )

    duplicate = DurableAgentService(Provider(), Tools(), store, model="test")
    reopened = await duplicate.reopen(live.id, resume_execution=False)
    duplicate.start_execution(reopened, max_workers=1)
    assert reopened.execution is not None
    await asyncio.wait_for(reopened.execution, timeout=5)
    assert reopened.execution.exception() is None
    # The duplicate never displaced the winner's claim or completed work.
    assert store.attempts[winner.id].state == "running"
    completed = [
        attempt.node_id for attempt in store.attempts.values() if attempt.state == "completed"
    ]
    assert completed == ["start"]


async def test_dead_driver_lease_is_reclaimed_and_schedule_completes() -> None:
    """A stale-heartbeat foreign attempt must not fence the new executor out.

    A killed/paused daemon leaves a running attempt whose lease has not
    expired yet but whose heartbeat went stale long ago. The executor that
    inherits the schedule must wait out the residue lease, reclaim the node
    through expiry recovery, and re-drive at the next generation — winding
    down here is what left campaign/087355f4's recon node to be cancelled
    with no driver at all.
    """
    store = FencingStore()
    creator = DurableAgentService(Provider(), Tools(), store, model="test", lease_seconds=0.2)
    live = await creator.submit(
        "session-dead-driver",
        _fencing_assignments("campaign/dead-driver/worker"),
        max_workers=1,
        start_immediately=False,
    )
    contested = f"worker:{live.assignments[0].id}"
    store.nodes[live.id][contested]["state"] = "ready"
    residue = store.claim(
        live.id,
        contested,
        contested,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=0.4),
    )
    # The dead driver stopped heartbeating long before its lease lapses.
    store.attempts[residue.id].heartbeat_at = datetime.now(UTC) - timedelta(seconds=60)

    recovered = DurableAgentService(Provider(), Tools(), store, model="test", lease_seconds=0.2)
    reopened = await recovered.reopen(live.id, resume_execution=False)
    recovered.start_execution(reopened, max_workers=1)
    assert reopened.execution is not None
    await asyncio.wait_for(reopened.execution, timeout=10)
    assert reopened.execution.exception() is None
    # The residue lease was reclaimed and the node completed at generation 2.
    node = store.nodes[live.id][contested]
    assert node["state"] == "completed"
    assert node["generation"] == 2
    assert store.nodes[live.id]["reducer"]["state"] == "completed"


async def test_recovered_executor_resumes_retry_transition_interrupted_mid_commit() -> None:
    """A driver killed between settle_attempt and retry_failed must resume.

    The interrupted transition leaves the durable node row failed while the
    persisted checkpoint already shows the node READY for its retry. The
    recovered executor must requeue the row and finish the schedule; reading
    that shape as foreign completion (lost_schedule_fence) wound the only
    driver down and stranded the running schedule.
    """

    class KillSwitchStore(FencingStore):
        def __init__(self) -> None:
            super().__init__()
            self.armed = True

        def retry_failed(self, schedule_id, node_id, *, checkpoint, expected_event_count):
            if self.armed:
                self.armed = False
                raise RuntimeError("simulated daemon kill mid retry transition")
            super().retry_failed(
                schedule_id,
                node_id,
                checkpoint=checkpoint,
                expected_event_count=expected_event_count,
            )

    store = KillSwitchStore()
    creator = DurableAgentService(RetryProvider(), Tools(), store, model="test", lease_seconds=0.2)
    live = await creator.submit(
        "session-interrupted-retry",
        _fencing_assignments("campaign/interrupted-retry/worker"),
        max_workers=1,
    )
    assert live.execution is not None
    with pytest.raises(RuntimeError, match="simulated daemon kill"):
        await asyncio.wait_for(live.execution, timeout=5)
    interrupted = f"worker:{live.assignments[0].id}"
    assert store.nodes[live.id][interrupted]["state"] == "failed"

    recovered = DurableAgentService(Provider(), Tools(), store, model="test", lease_seconds=0.2)
    reopened = await recovered.reopen(live.id)
    assert reopened.execution is not None
    await asyncio.wait_for(reopened.execution, timeout=10)
    assert reopened.execution.exception() is None
    node = store.nodes[live.id][interrupted]
    assert node["state"] == "completed"
    assert node["generation"] == 2
    assert store.nodes[live.id]["reducer"]["state"] == "completed"
    assert not any(
        event_type == "executor_exit" and payload.get("reason") == "lost_schedule_fence"
        for _, event_type, payload in store.lifecycle_events
    )


async def test_early_finisher_outcome_settles_before_long_sibling() -> None:
    """An early finisher's commit must not wait out a long sibling's lease.

    The batch barrier held a finished worker's outcome until every sibling
    completed; a sibling running past the lease TTL let the finisher's lease
    lapse, its commit was fenced, and the fence then read the sibling's own
    unsettled attempt as a foreign driver — campaign/c90ce076's wave-0 was
    abandoned as lost_schedule_fence with recon healthy and tools finished.
    Settlement must land at the tick the worker finishes.
    """

    class LeaseExpiryStore(FencingStore):
        def commit_reducer(self, attempt_id, generation, **kwargs):
            lease = self.attempts[attempt_id]
            if lease.lease_expires_at <= datetime.now(UTC):
                raise StaleLeaseError("attempt lease expired before commit")
            return super().commit_reducer(attempt_id, generation, **kwargs)

    class MixedPaceProvider:
        async def stream(self, request):
            if any("slow" in message.content for message in request.messages):
                await asyncio.sleep(1.2)
            yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()

    def assignment(name: str) -> WorkerAssignment:
        return WorkerAssignment(
            id=f"campaign/early-finisher/{name}",
            objective=name,
            profile_id="recon",
            target_profile_id="target",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )

    store = LeaseExpiryStore()
    service = DurableAgentService(
        MixedPaceProvider(), Tools(), store, model="test", lease_seconds=0.9
    )
    live = await service.submit(
        "session-early-finisher",
        (assignment("fast"), assignment("slow")),
        max_workers=2,
    )
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=10)
    assert live.execution.exception() is None
    fast = store.nodes[live.id]["worker:campaign/early-finisher/fast"]
    assert fast["state"] == "completed"
    assert fast["generation"] == 1
    assert store.nodes[live.id]["worker:campaign/early-finisher/slow"]["state"] == "completed"
    assert store.nodes[live.id]["reducer"]["state"] == "completed"
    assert not any(
        event_type == "executor_exit" and payload.get("reason") == "lost_schedule_fence"
        for _, event_type, payload in store.lifecycle_events
    )


async def test_pause_resume_with_dead_driver_lease_redrives_schedule() -> None:
    """The pause/resume interleaving from campaign/087355f4's journal.

    The acceptance driver pauses and resumes the campaign around a daemon
    restart; the pre-restart worker attempt is still running with an
    unexpired lease and a dead heartbeat. The executor started after resume
    must recover the lease and finish the schedule rather than fencing
    itself out.
    """
    store = FencingStore()
    creator = DurableAgentService(Provider(), Tools(), store, model="test", lease_seconds=0.2)
    live = await creator.submit(
        "session-pause-resume",
        _fencing_assignments("campaign/pause-resume/worker"),
        max_workers=1,
        start_immediately=False,
    )
    contested = f"worker:{live.assignments[0].id}"
    store.nodes[live.id][contested]["state"] = "ready"
    residue = store.claim(
        live.id,
        contested,
        contested,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=0.4),
    )
    store.attempts[residue.id].heartbeat_at = datetime.now(UTC) - timedelta(seconds=60)
    store.pause_schedule(live.id, reason="operator-campaign-pause")
    assert store.attempts[residue.id].state == "expired"
    assert store.nodes[live.id][contested]["state"] == "ready"
    store.resume_schedule(live.id)

    recovered = DurableAgentService(Provider(), Tools(), store, model="test", lease_seconds=0.2)
    reopened = await recovered.reopen(live.id, resume_execution=False)
    recovered.start_execution(reopened, max_workers=1)
    assert reopened.execution is not None
    await asyncio.wait_for(reopened.execution, timeout=10)
    assert reopened.execution.exception() is None
    node = store.nodes[live.id][contested]
    assert node["state"] == "completed"
    assert node["generation"] == 2
    assert store.nodes[live.id]["reducer"]["state"] == "completed"


async def test_live_driver_wait_follows_lease_expiry_not_heartbeat_freshness() -> None:
    """The fatal-verdict gate holds for the whole lease, heartbeat or not.

    campaign/b55ce27f: recon's heartbeats stalled mid-flight while its lease
    stayed valid. A freshness-keyed gate released the wave-0 verdict 40s
    before lease expiry and killed the campaign while the worker was still
    live. The lease is the contract: any running attempt with an unexpired
    lease must hold the gate; heartbeat staleness only decides executor
    fence-out.
    """
    store = FencingStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test")
    schedule_id = store.create_schedule(
        "session-live-gate", {}, {}, ["worker:a"], ready_nodes=("worker:a",)
    )
    lease = store.claim(
        schedule_id,
        "worker:a",
        "worker:a",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    # Fresh heartbeat: a live driver holds the gate.
    assert await service.live_driver_wait(schedule_id) is not None
    # Stale heartbeat but unexpired lease: the gate still holds.
    lease.heartbeat_at = datetime.now(UTC) - timedelta(hours=1)
    assert await service.live_driver_wait(schedule_id) is not None
    # Lease lapsed: recovery owns the row and the gate releases.
    lease.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await service.live_driver_wait(schedule_id) is None
    # A settled attempt never blocks, whatever its timestamps say.
    lease.lease_expires_at = datetime.now(UTC) + timedelta(seconds=60)
    lease.state = "completed"
    assert await service.live_driver_wait(schedule_id) is None


async def test_lease_keepers_survive_transient_heartbeat_errors() -> None:
    """A transient store error must not stop lease heartbeats mid-flight.

    campaign/b55ce27f's recon attempt stopped heartbeating 40s before its
    lease expired while the worker and daemon were still alive: a single
    transient store error (the shared-connection transaction collision) killed
    the keepers — the async keeper let the exception escape its never-awaited
    task, and the thread keeper returned on any error. Keepers must stop only
    on confirmed cancellation or lease loss and ride out transient errors.
    The thread keeper is isolated here by thread name: the async keeper and
    the runtime's boundary heartbeat hook share the store call path, while
    only the thread keeper runs on a dedicated ``lease-keeper-*`` thread.
    """

    class FlakyHeartbeatStore(Store):
        def __init__(self):
            super().__init__()
            self.keeper_calls = 0

        def heartbeat(self, attempt_id, generation, *, lease_expires_at):
            if threading.current_thread().name.startswith("lease-keeper-"):
                self.keeper_calls += 1
                raise RuntimeError("transient store collision")
            return super().heartbeat(attempt_id, generation, lease_expires_at=lease_expires_at)

    class SlowProvider:
        async def stream(self, request):
            del request
            await asyncio.sleep(1.2)
            yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()

    store = FlakyHeartbeatStore()
    service = DurableAgentService(SlowProvider(), Tools(), store, model="test", lease_seconds=0.3)
    live = await service.submit(
        "session-flaky-heartbeat",
        _fencing_assignments("campaign/flaky-heartbeat/worker"),
        max_workers=1,
    )
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=10)
    assert live.execution.exception() is None
    # Every thread-keeper beat fails. A keeper that dies on the first error
    # beats exactly once per worker (2 total); a surviving keeper retries
    # every interval (0.25s) for the worker's whole 1.2s lifetime.
    assert store.keeper_calls > 2


async def test_executor_recovers_mid_wave_worker_whose_keepers_went_silent() -> None:
    """A worker that goes silent mid-wave is expired, cancelled, and re-driven.

    campaign/31449ad2: tools/recon stopped heartbeating with the daemon
    alive; their leases lapsed with no recovery in flight and the schedule
    never re-drove them — supervision then failed the campaign on the
    missing recon. The executor must expire the dead worker's lease during
    the wave, cancel the hung task, and re-claim the node at the next
    generation instead of waiting out the batch forever.
    """

    class SilentHeartbeatStore(FencingStore):
        """Heartbeats land but never renew the first worker attempt's lease."""

        def __init__(self):
            super().__init__()
            self.silent_attempt = None

        def claim(
            self,
            schedule_id,
            node_id,
            owner,
            *,
            lease_expires_at,
            attempt_id=None,
            target_admission=None,
        ):
            lease = super().claim(
                schedule_id,
                node_id,
                owner,
                lease_expires_at=lease_expires_at,
                attempt_id=attempt_id,
                target_admission=target_admission,
            )
            if self.silent_attempt is None and node_id.startswith("worker:"):
                self.silent_attempt = lease.id
            return lease

        def heartbeat(self, attempt_id, generation, *, lease_expires_at):
            if attempt_id == self.silent_attempt:
                # Durable silence: the beat is acknowledged but the lease is
                # never extended, exactly as if the keepers had died.
                return self.attempts[attempt_id]
            return super().heartbeat(attempt_id, generation, lease_expires_at=lease_expires_at)

    hang = asyncio.Event()  # never set: the first worker wedges mid-run
    streams = 0

    class WedgedOnceProvider:
        async def stream(self, request):
            nonlocal streams
            streams += 1
            if streams == 1:
                await hang.wait()
            yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()

    store = SilentHeartbeatStore()
    service = DurableAgentService(
        WedgedOnceProvider(), Tools(), store, model="test", lease_seconds=0.9
    )
    live = await service.submit(
        "session-midwave-recovery",
        _fencing_assignments("campaign/midwave-recovery/worker"),
        max_workers=1,
    )
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=10)
    assert live.execution.exception() is None
    wedged = f"worker:{live.assignments[0].id}"
    node = store.nodes[live.id][wedged]
    assert node["state"] == "completed"
    assert node["generation"] == 2
    assert store.attempts[store.silent_attempt].state == "expired"
    assert store.nodes[live.id]["reducer"]["state"] == "completed"


async def test_executor_retries_a_crashed_worker_instead_of_dying() -> None:
    """One worker's transient exception must not kill the whole executor.

    campaign/31449ad2's tools probe died on a transient store error; the
    exception escaped the batch gather, the executor task died with it, and
    the sibling recon worker was orphaned — its completed outcome discarded,
    both attempts stranded as running zombies. A worker exception must
    become a failed outcome so the node is retried within its admitted
    budget and the wave completes.
    """

    class FlakyCheckpointStore(FencingStore):
        def __init__(self):
            super().__init__()
            self.partial_calls = 0

        def commit_partial(self, attempt_id, generation, *, result, applied_message_ids=()):
            self.partial_calls += 1
            if self.partial_calls == 1:
                raise RuntimeError("transient store collision")
            return "sha256:partial"

    store = FlakyCheckpointStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test", lease_seconds=0.9)
    live = await service.submit(
        "session-crash-retry",
        _fencing_assignments("campaign/crash-retry/worker"),
        max_workers=1,
    )
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=10)
    assert live.execution.exception() is None
    first = f"worker:{live.assignments[0].id}"
    assert store.nodes[live.id][first]["state"] == "completed"
    assert store.nodes[live.id][first]["generation"] == 2
    assert store.nodes[live.id]["reducer"]["state"] == "completed"
    # The crash and the executor's clean exit are journaled for live triage.
    kinds = {event_type for _, event_type, _ in store.lifecycle_events}
    assert "worker_crashed" in kinds
    assert ("executor_exit", "completed") in {
        (event_type, payload.get("reason")) for _, event_type, payload in store.lifecycle_events
    }


async def test_postgres_disk_full_crash_is_infrastructure_and_retryable() -> None:
    from psycopg.errors import DiskFull

    class DiskFullStore(FencingStore):
        failed = False

        def commit_partial(self, attempt_id, generation, *, result, applied_message_ids=()):
            if not self.failed:
                self.failed = True
                raise DiskFull("injected local PostgreSQL disk full")
            return "sha256:partial"

    class Service(DurableAgentService):
        crashed = None

        async def _crashed_worker_outcome(self, *args):
            outcome = await super()._crashed_worker_outcome(*args)
            self.crashed = outcome[2]
            return outcome

    store = DiskFullStore()
    service = Service(Provider(), Tools(), store, model="test", lease_seconds=0.9)
    live = await service.submit(
        "session-disk-full",
        _fencing_assignments("campaign/disk-full/worker"),
        max_workers=1,
    )
    await asyncio.wait_for(live.execution, timeout=10)
    assert service.crashed.infrastructure_failure_category == "storage_exhausted"
    assert service._retry_failure_kind(service.crashed) == RetryFailureKind.WORKER_INTERRUPTED
    assert store.nodes[live.id][f"worker:{live.assignments[0].id}"]["state"] == "completed"
    event = next(payload for _, kind, payload in store.lifecycle_events if kind == "worker_crashed")
    assert event["failure_class"] == "infrastructure"
    assert event["infrastructure_failure_category"] == "storage_exhausted"


async def test_live_driver_wait_holds_while_expired_attempt_has_retry_budget() -> None:
    """The verdict gate waits out recovery headroom after a lease lapses.

    campaign/31449ad2: recon's lease expired cleanly and supervision failed
    the campaign half a second later, before the executor could reclaim the
    node. While the node's latest attempt has lapsed and admitted attempt
    budget remains, the gate must keep waiting; it releases once the budget
    is exhausted or the recovery window (one lease TTL) has passed.
    """
    store = FencingStore()
    service = DurableAgentService(Provider(), Tools(), store, model="test", lease_seconds=10)
    live = await service.submit(
        "session-headroom",
        _fencing_assignments("campaign/headroom/worker"),
        max_workers=1,
        start_immediately=False,
    )
    node_id = f"worker:{live.assignments[0].id}"
    store.nodes[live.id][node_id]["state"] = "ready"
    lease = store.claim(
        live.id,
        node_id,
        node_id,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    # Unexpired lease: the gate holds.
    assert await service.live_driver_wait(live.id) is not None
    # Lease lapsed a moment ago and one of two admitted attempts is spent:
    # recovery headroom holds the gate.
    lease.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await service.live_driver_wait(live.id) is not None
    # Typed worker retry does not mistake two attempts for budget exhaustion.
    store.expire(lease.id, lease.generation)
    second = store.claim(
        live.id,
        node_id,
        node_id,
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert second.generation == 2
    other = f"worker:{live.assignments[1].id}"
    store.nodes[live.id][other]["state"] = "ready"
    stale = store.claim(
        live.id,
        other,
        other,
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=5),
    )
    # The other node still has headroom inside the recovery window: hold.
    assert await service.live_driver_wait(live.id) is not None
    # Past the recovery window (one lease TTL) for both dead drivers: release.
    stale.lease_expires_at = datetime.now(UTC) - timedelta(seconds=20)
    second.lease_expires_at = datetime.now(UTC) - timedelta(seconds=20)
    assert await service.live_driver_wait(live.id) is None


def test_unacknowledged_delivery_rejection_churn_does_not_reset_retry_stalls() -> None:
    acknowledged = {
        "acknowledged": True,
        "is_error": False,
        "status_code": 200,
        "semantic_request_sha256": "6" * 64,
        "semantic_response_sha256": "1" * 64,
    }

    def storm_result(rejections: int) -> WorkerResult:
        evidence = [dict(acknowledged)]
        for index in range(rejections):
            evidence.append(
                {
                    "acknowledged": False,
                    "is_error": True,
                    "status_code": 400,
                    "semantic_request_sha256": hashlib.sha256(
                        f"rejection-{index}".encode()
                    ).hexdigest(),
                    "semantic_response_sha256": "0" * 64,
                }
            )
        return WorkerResult(
            task_id="x",
            status="failed",
            retry_class="fresh_worker",
            retry_failure_kind=RetryFailureKind.TRANSPORT_TRANSIENT,
            checkpoint={
                "effect_observed": True,
                "target_evidence": evidence,
                "finalization_progress": [1, 42],
            },
        )

    first = DurableAgentService._retry_progress_marker(storm_result(115))  # noqa: SLF001
    assert first is not None
    # Every replacement adds one fresh unacknowledged HTTP-400 digest; the
    # marker must stay stable so the consecutive-stall bound accumulates.
    for rejections in (116, 117, 118):
        assert DurableAgentService._retry_progress_marker(storm_result(rejections)) == first  # noqa: SLF001

    # A newly acknowledged exchange is real progress and still resets the bound.
    progressed = storm_result(118)
    progressed.checkpoint["target_evidence"].append(  # type: ignore[union-attr]
        {
            "acknowledged": True,
            "is_error": False,
            "status_code": 200,
            "semantic_request_sha256": "2" * 64,
            "semantic_response_sha256": "3" * 64,
        }
    )
    assert DurableAgentService._retry_progress_marker(progressed) != first  # noqa: SLF001


def test_transport_retry_backoff_is_bounded_doubled_and_jittered() -> None:
    floor = lambda a, _b: a  # noqa: E731
    ceiling = lambda _a, b: b  # noqa: E731

    # Doubling from the base interval, jittered ±25%.
    assert _transport_retry_backoff_seconds(1, 15, 120, jitter=ceiling) == 15 * 1.25
    assert _transport_retry_backoff_seconds(2, 15, 120, jitter=floor) == 30 * 0.75
    assert _transport_retry_backoff_seconds(3, 15, 120, jitter=ceiling) == 60 * 1.25
    # The ceiling caps the doubling; jitter stays symmetric around it.
    assert _transport_retry_backoff_seconds(10, 15, 120, jitter=ceiling) == 120 * 1.25
    assert _transport_retry_backoff_seconds(10, 15, 120, jitter=floor) == 120 * 0.75

    rng = random.Random(7)
    for generation in range(1, 12):
        value = _transport_retry_backoff_seconds(generation, 15, 120, jitter=rng.uniform)
        bounded = min(15 * 2 ** (generation - 1), 120)
        assert 0.75 * bounded <= value <= 1.25 * bounded


async def test_transport_transient_retry_is_paced_before_the_next_generation(
    monkeypatch,
) -> None:
    """A delivery-500 failure requeues after a bounded jittered pause, not
    back-to-back: exactly one pacing sleep lands between the failed generation
    and its replacement, inside the ±25% envelope of the configured base."""

    class DeliveryFailureThenSuccessTools(Tools):
        def __init__(self) -> None:
            self.calls = 0

        def describe(self):
            return (ToolSpec(name="target_request", description="request", input_schema={}),)

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            self.calls += 1
            failed = self.calls == 1
            return ToolResult(
                content=(
                    json.dumps({"error": "Internal Server Error", "statusCode": 500})
                    if failed
                    else '{"response":"delivered"}'
                ),
                is_error=failed,
                metadata={
                    "target_session_id": "api:paced-retry",
                    "retryable": failed,
                    "infrastructure_failure_category": "delivery_failure" if failed else None,
                    "current_target_exchanges": [
                        {
                            "interaction": True,
                            "cleanup": False,
                            "status_code": 500 if failed else 200,
                        }
                    ],
                },
            )

    class OneTargetCallPerAttemptProvider(Provider):
        def __init__(self) -> None:
            self.turns = 0

        async def stream(self, request):
            self.turns += 1
            if request.tools:
                yield ToolCall(
                    call_id=f"paced-call-{self.turns}",
                    name="target_request",
                    arguments={"input": "authorized test"},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield TextDelta(text='{"finding":"target replied after the paced retry"}')
            yield ModelCompleted()

    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(seconds: float) -> None:
        delays.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)

    assignment = WorkerAssignment(
        id="campaign/x/paced-transport-retry",
        objective="Execute one authorized target attempt",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("case:paced-transport-retry",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_seconds=60),
        max_target_exchanges=1,
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    store = Store()
    tools = DeliveryFailureThenSuccessTools()
    service = DurableAgentService(
        OneTargetCallPerAttemptProvider(),
        tools,
        store,
        model="test",
        transport_retry_backoff_seconds=15,
        transport_retry_backoff_max_seconds=120,
    )

    live = await service.submit(
        "session-paced-retry", (assignment,), max_workers=1, minimum_workers=1
    )
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=1)

    attempts = [
        attempt
        for attempt in store.attempts.values()
        if attempt.node_id == f"worker:{assignment.id}"
    ]
    assert [attempt.state for attempt in attempts] == ["failed", "completed"]
    assert tools.calls == 2
    # First retry: one base interval (15s) with ±25% jitter, before the
    # replacement generation fired. Other service sleeps (lease keepers) fall
    # outside this envelope.
    paced = [delay for delay in delays if 15 * 0.75 <= delay <= 15 * 1.25]
    assert len(paced) == 1


async def test_transport_transient_retry_pacing_is_disabled_at_zero() -> None:
    """Backoff base 0 keeps the legacy immediate requeue (pacing off)."""

    class FailingTools(Tools):
        def __init__(self) -> None:
            self.calls = 0

        def describe(self):
            return (ToolSpec(name="target_request", description="request", input_schema={}),)

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            self.calls += 1
            return ToolResult(
                content=json.dumps({"error": "Internal Server Error", "statusCode": 500}),
                is_error=True,
                metadata={
                    "target_session_id": "api:unpaced-retry",
                    "retryable": True,
                    "infrastructure_failure_category": "delivery_failure",
                    "current_target_exchanges": [
                        {"interaction": True, "cleanup": False, "status_code": 500}
                    ],
                },
            )

    class OneTargetCallPerAttemptProvider(Provider):
        def __init__(self) -> None:
            self.turns = 0

        async def stream(self, request):
            self.turns += 1
            if request.tools:
                yield ToolCall(
                    call_id=f"unpaced-call-{self.turns}",
                    name="target_request",
                    arguments={"input": "authorized test"},
                )
                yield ModelCompleted(finish_reason="tool_calls")
                return
            yield TextDelta(text='{"finding":"exhausted"}')
            yield ModelCompleted()

    assignment = WorkerAssignment(
        id="campaign/x/unpaced-transport-retry",
        objective="Execute one authorized target attempt",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("case:unpaced-transport-retry",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_seconds=60),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    store = Store()
    tools = FailingTools()
    service = DurableAgentService(
        OneTargetCallPerAttemptProvider(),
        tools,
        store,
        model="test",
        transport_retry_backoff_seconds=0,
    )

    started = time.monotonic()
    live = await service.submit(
        "session-unpaced-retry", (assignment,), max_workers=1, minimum_workers=1
    )
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=1)
    elapsed = time.monotonic() - started

    # The stall-limited retries fired back-to-back: no pacing was applied.
    assert tools.calls > 1
    assert elapsed < 5


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_graph", [False, True])
async def test_shared_physical_probe_session_waits_for_owned_cleanup(legacy_graph) -> None:
    events: list[str] = []
    active_owner: str | None = None

    class ProbeProvider(Provider):
        def __init__(self):
            self.calls: dict[str, int] = {}

        async def stream(self, request):
            count = self.calls.get(request.budget_id, 0)
            self.calls[request.budget_id] = count + 1
            if count == 0:
                yield ToolCall(
                    call_id=f"call-{request.budget_id}",
                    name="target_request",
                    arguments={"input": "harmless probe"},
                )
                yield ModelCompleted(finish_reason="tool_calls")
            else:
                yield TextDelta(text='{"finding":"capability observed"}')
                yield ModelCompleted()

    class SharedSessionTools(Tools):
        def describe(self):
            return (SimpleNamespace(name="target_request", description="target", input_schema={}),)

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            nonlocal active_owner
            # Bank-style setup resumes one physical session for every local scope.
            assert active_owner is None or active_owner == context.agent_id
            active_owner = context.agent_id
            events.append(f"send:{context.agent_id}")
            await asyncio.sleep(0)
            return ToolResult(
                content='{"response":"acknowledged"}',
                metadata={"target_session_id": "shared-physical-session"},
            )

    async def cleanup(scope_id):
        nonlocal active_owner
        if active_owner == scope_id:
            events.append(f"cleanup:{scope_id}")
            active_owner = None

    class ProbeService(DurableAgentService):
        def _graph_spec(self, *args, **kwargs):
            spec = super()._graph_spec(*args, **kwargs)
            if legacy_graph:
                # Persisted pre-fix probe graphs omitted physical session edges.
                spec = spec.model_copy(
                    update={
                        "edges": tuple(
                            edge
                            for edge in spec.edges
                            if not edge.id.startswith("target-session-owner:")
                        )
                    }
                )
            return spec

    assignments = tuple(
        WorkerAssignment(
            id=f"campaign/probes/capability-probe/{surface}",
            objective="Probe one capability",
            profile_id="recon",
            target_profile_id="bank",
            technique_ids=(f"capability/{surface}",),
            guidance_ids=(),
            tool_ids=("target_request",),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(4, 9_000, 60),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
            target_session_capacity=1,
        )
        for surface in ("memory", "tools")
    )
    service = ProbeService(
        ProbeProvider(), SharedSessionTools(), Store(), model="test", campaign_cleanup=cleanup
    )
    live = await service.submit("session-probes", assignments, max_workers=2, minimum_workers=2)
    assert live.execution is not None
    await asyncio.wait_for(live.execution, timeout=3)
    tasks = await service.list_schedule_tasks(live.id)
    assert {task.status for task in tasks} == {"completed"}, tasks
    assert events == [
        f"send:{assignments[0].id}",
        f"cleanup:{assignments[0].id}",
        f"send:{assignments[1].id}",
        f"cleanup:{assignments[1].id}",
    ]
    assert {task.status for task in await service.list_schedule_tasks(live.id)} == {"completed"}
    assert active_owner is None


@pytest.mark.asyncio
async def test_expanded_transcript_includes_all_retained_messages_and_failed_attempts() -> None:
    class TranscriptStore(Store):
        calls = []

        def get_assignment_transcript_rows(self, session_id, assignment_id):
            self.calls.append((session_id, assignment_id))
            return [
                {
                    "generation": 1,
                    "state": "failed",
                    "error": "session already owned",
                    "payload": {
                        "worker_checkpoint": {
                            "messages": [
                                {"role": "system", "content": "private system prompt"},
                                {"role": "user", "content": "private assignment context"},
                                *(
                                    {"role": "assistant", "content": f"retained message {i}"}
                                    for i in range(12)
                                ),
                                {"role": "tool", "content": "target reply"},
                            ]
                        }
                    },
                },
                {
                    "generation": 2,
                    "state": "completed",
                    "error": None,
                    "payload": {"content": "final worker result"},
                },
            ]

    store = TranscriptStore()
    service = DurableAgentService(Provider(), Tools(), store)
    output = await service.get_assignment_transcript("session-1", "assignment-1")
    assert store.calls == [("session-1", "assignment-1")]
    assert "assistant: retained message 0" in output
    assert "assistant: retained message 11" in output
    assert "tool: target reply" in output
    assert "Failure: session already owned" in output
    assert "Result: final worker result" in output
    assert not any("private" in line for line in output)


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_delivery_retry_cleans_owned_session_before_replacement(cleanup_fails):
    events = []

    class DeliveryProvider(Provider):
        async def stream(self, request):
            if request.tools and not any(
                message.role == "tool" and "observed-success" in str(message.content)
                for message in request.messages
            ):
                yield ToolCall(
                    call_id=str(uuid4()), name="target_request", arguments={"input": "probe"}
                )
            else:
                yield TextDelta(text='{"finding":"checked"}')
            yield ModelCompleted()

    class DeliveryTools(Tools):
        def describe(self):
            return (SimpleNamespace(name="target_request", description="request", input_schema={}),)

        def effect(self, name):
            return "target_network"

        async def invoke(self, name, context, arguments):
            events.append("request")
            if events.count("request") == 1:
                return ToolResult(
                    content="HTTP 400: Game already ended",
                    is_error=True,
                    metadata={
                        "target_delivery_rejected": True,
                        "failure_code": "target_delivery_rejected",
                        "retryable": True,
                        "infrastructure_failure_category": "delivery_failure",
                        "target_exchanges": [
                            {"interaction": True, "cleanup": False, "status_code": 400}
                        ],
                    },
                )
            assert "cleanup" in events
            return ToolResult(
                content="observed-success",
                metadata={
                    "target_exchanges": [
                        {"interaction": True, "cleanup": False, "status_code": 200}
                    ],
                },
            )

    async def cleanup(assignment_id):
        events.append("cleanup")
        if cleanup_fails and events.count("cleanup") == 1:
            raise RuntimeError("owned session not reconciled")

    assignment = WorkerAssignment(
        id="campaign/session-retry/worker",
        objective="Run one reviewed request",
        profile_id="injection",
        target_profile_id="target",
        technique_ids=("test",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
    )
    service = DurableAgentService(
        DeliveryProvider(), DeliveryTools(), Store(), model="test", campaign_cleanup=cleanup
    )
    live = await service.submit("session", (assignment,), max_workers=1, minimum_workers=1)
    await asyncio.wait_for(live.execution, timeout=3)
    task = (await service.list_schedule_tasks(live.id))[0]
    assert task.status == ("failed" if cleanup_fails else "completed"), (task, events)
    assert events[:2] == ["request", "cleanup"]
    assert events.count("request") == (1 if cleanup_fails else 2)


async def test_restart_recovery_pauses_all_target_schedules_until_explicit_resume() -> None:
    store = Store()
    creator = DurableAgentService(Provider(), Tools(), store, model="test")
    assignments = await creator._plan("Assess target", 2)
    # More than the inventory page size: startup must fence every schedule.
    schedule_ids = []
    for index in range(51):
        live = await creator.submit(
            f"session-{index}",
            assignments,
            start_immediately=False,
            minimum_workers=1,
            schedule_id=str(uuid4()),
        )
        store.schedules[live.id]["status"] = "running"
        schedule_ids.append(live.id)
    recovered = DurableAgentService(Provider(), Tools(), store, model="test")
    assert await recovered.recover(resume_execution=False) == 51
    await asyncio.sleep(0)
    assert all(store.schedules[key]["status"] == "paused" for key in schedule_ids)
    assert all(live.execution is None for live in recovered._live.values())
    assert store.attempts == {}
    assert await recovered.recover() == 0
    assert store.attempts == {}

    # Explicit resume starts only the requested schedule; others stay dormant.
    resumed = await recovered.resume_schedule(schedule_ids[0])
    assert resumed.execution is not None
    await asyncio.wait_for(resumed.execution, timeout=2)
    assert all(store.schedules[key]["status"] == "paused" for key in schedule_ids[1:])
    assert all(attempt.schedule_id == schedule_ids[0] for attempt in store.attempts.values())
