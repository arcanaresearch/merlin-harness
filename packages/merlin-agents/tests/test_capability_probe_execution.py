import json
from dataclasses import replace

import pytest
from merlin_agents.capability_probe_execution import (
    CAPABILITY_PROBE_OUTPUT_SCHEMA,
    ProbeTransportBinding,
    compile_capability_probe_execution,
    probe_observation_from_committed_receipt,
    probe_result_from_committed_receipt,
)
from merlin_agents.capability_probes import (
    CapabilityProbeKind,
    ProbeObservation,
    ProbeOutcome,
    ProbeTransport,
    plan_capability_probes,
)
from merlin_agents.specialists import AssignmentBudget, AssignmentContext
from merlin_contracts import AgentTask, TargetDeliveryClass


def _context() -> AssignmentContext:
    return AssignmentContext(
        target_profile_id="target-1",
        techniques=(),
        tool_ids=("target_chat", "upload_document", "read_only_echo"),
        skill_ids=("browser",),
        provider_id="provider-1",
        authority_ids=("target:invoke", "document:upload"),
        budget=AssignmentBudget(max_turns=8, max_tokens=8_000, max_seconds=300),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        provider_model="model-1",
        provider_role_models=(("recon", "model-recon"),),
        provider_model_capabilities=(("model-recon", ("vision",)),),
        provider_model_pricing=(("model-recon", (1.0, 2.0)),),
    )


def _binding() -> ProbeTransportBinding:
    return ProbeTransportBinding(
        tool_ids=("target_chat",),
        authority_ids=("target:invoke",),
        skill_ids=("browser",),
    )


def _task(probe_id: str, report: dict[str, object], *, status: str = "completed") -> AgentTask:
    return AgentTask(
        id="campaign/probes/upload",
        objective="harmless probe",
        status=status,  # type: ignore[arg-type]
        result=json.dumps(report),
        technique_ids=(probe_id,),
    )


def _receipt(
    *call_ids: str,
    include_upload: bool = True,
    structured_result: dict[str, object] | None = None,
    target_delivery_receipts: dict[str, object] | None = None,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "digest": "sha256:" + "a" * 64,
        "reference": "merlin-result://schedule/s-1/node/worker:probe/attempt/a-1",
        "target_call_ids": call_ids,
        "target_call_documents": {call_id: {"reply": "nonce"} for call_id in call_ids},
        "acknowledged_tool_calls": {
            **{call_id: "target_chat" for call_id in call_ids},
            **({"upload-1": "browser_upload"} if include_upload else {}),
        },
        "probe_effect_calls": (
            {
                **{call_id: "target_chat" for call_id in call_ids},
                **({"upload-1": "browser_upload"} if include_upload else {}),
            }
        ),
        "target_delivery_receipts": target_delivery_receipts or {},
    }
    if structured_result is not None:
        receipt["structured_result"] = structured_result
    return receipt


def _report(probe_id: str, **updates: object) -> dict[str, object]:
    report: dict[str, object] = {
        "probe_id": probe_id,
        "transport_available": True,
        "accepted": True,
        "gracefully_rejected": False,
        "summary": "target returned the nonce",
        "evidence_ids": ["call-1", "upload-1"],
    }
    report.update(updates)
    return report


def test_compilation_scopes_unknown_probes_to_reviewed_bindings() -> None:
    plan = plan_capability_probes()
    bindings = {transport: _binding() for transport in ProbeTransport}

    compiled = compile_capability_probe_execution(
        plan,
        context=_context(),
        assignment_id_prefix="campaign-1/capability-probe",
        transport_bindings=bindings,
        budget=AssignmentBudget(max_turns=2, max_tokens=2_000, max_seconds=60),
        target_session_capacity=2,
    )

    assert len(compiled.assignments) == 6
    assert compiled.unavailable == ()
    assert {item.probe.id for item in compiled.scheduled} == {item.id for item in plan.schedulable}
    assert all(assignment.profile_id == "recon" for assignment in compiled.assignments)
    assert all(assignment.grant_id == "profile:recon" for assignment in compiled.assignments)
    assert all(assignment.tool_ids == ("target_chat",) for assignment in compiled.assignments)
    assert all(assignment.target_session_capacity == 2 for assignment in compiled.assignments)
    assert all(
        assignment.authority_ids == ("target:invoke",) for assignment in compiled.assignments
    )
    assert {
        item.probe.kind: item.assignment.max_target_exchanges for item in compiled.scheduled
    } == {
        CapabilityProbeKind.UPLOAD_DOCUMENT: 1,
        CapabilityProbeKind.RAG_RETRIEVAL: 1,
        CapabilityProbeKind.MEMORY: 2,
        CapabilityProbeKind.MULTIMODAL: 1,
        CapabilityProbeKind.TOOLS: 1,
        CapabilityProbeKind.AGENT_MESSAGING: 1,
    }
    assert all(
        {
            **json.loads(CAPABILITY_PROBE_OUTPUT_SCHEMA),
            "properties": {
                **json.loads(CAPABILITY_PROBE_OUTPUT_SCHEMA)["properties"],
                "probe_id": {"const": item.probe.id},
            },
        }
        == json.loads(item.assignment.output_schema)
        for item in compiled.scheduled
    )
    assert all(technique.execution_role == "unpaired" for technique in compiled.techniques)
    upload = next(
        item.assignment
        for item in compiled.scheduled
        if item.probe.kind is CapabilityProbeKind.UPLOAD_DOCUMENT
    )
    retrieval = next(
        item.assignment
        for item in compiled.scheduled
        if item.probe.kind is CapabilityProbeKind.RAG_RETRIEVAL
    )
    assert "accepted=true only when the target explicitly confirms" in upload.objective
    assert "accepted=null when the answer is ambiguous" in upload.objective
    assert "feature claim alone must use accepted=null" in retrieval.objective


def test_agent_messaging_probe_never_treats_internal_campaign_share_as_target_transport() -> None:
    plan = plan_capability_probes()
    context = _context()
    context = replace(context, tool_ids=(*context.tool_ids, "campaign_share"))
    bindings = {transport: _binding() for transport in ProbeTransport}
    bindings[ProbeTransport.AGENT_MESSAGE] = ProbeTransportBinding(
        tool_ids=("campaign_share", "target_chat"),
        authority_ids=("target:invoke",),
        skill_ids=("browser",),
    )

    compiled = compile_capability_probe_execution(
        plan,
        context=context,
        assignment_id_prefix="campaign-1/capability-probe",
        transport_bindings=bindings,
        budget=AssignmentBudget(max_turns=2, max_tokens=2_000, max_seconds=60),
    )
    agent_probe = next(
        item
        for item in compiled.scheduled
        if item.probe.kind is CapabilityProbeKind.AGENT_MESSAGING
    )

    assert agent_probe.probe.required_transport is ProbeTransport.AGENT_MESSAGE
    assert agent_probe.executed_transport is ProbeTransport.TEXT_CHAT
    assert agent_probe.assignment.tool_ids == ("target_chat",)
    assert "campaign_share" not in agent_probe.assignment.objective
    assert "Ask only whether the feature exists" in agent_probe.assignment.objective
    assert "does not prove delivery through an agent-to-agent channel" in (
        agent_probe.assignment.objective
    )
    assert dict(agent_probe.assignment.strategy_contract) == {
        "probe_executed_transport": "text_chat",
        "probe_intended_transport": "agent_message",
    }


def test_agent_messaging_probe_uses_explicit_target_peer_delivery_binding() -> None:
    plan = plan_capability_probes()
    bindings = {transport: _binding() for transport in ProbeTransport}
    bindings[ProbeTransport.AGENT_MESSAGE] = ProbeTransportBinding(
        tool_ids=("peer_exchange",),
        authority_ids=("target:invoke",),
        skill_ids=("browser",),
        target_delivery=TargetDeliveryClass.INTER_AGENT_MESSAGE,
    )
    context = replace(_context(), tool_ids=(*_context().tool_ids, "peer_exchange"))

    compiled = compile_capability_probe_execution(
        plan,
        context=context,
        assignment_id_prefix="campaign-1/capability-probe",
        transport_bindings=bindings,
        budget=AssignmentBudget(max_turns=2, max_tokens=2_000, max_seconds=60),
    )
    agent_probe = next(
        item
        for item in compiled.scheduled
        if item.probe.kind is CapabilityProbeKind.AGENT_MESSAGING
    )

    assert agent_probe.executed_transport is ProbeTransport.AGENT_MESSAGE
    assert agent_probe.assignment.tool_ids == ("peer_exchange",)
    assert "authenticated target-delivery receipt" in agent_probe.assignment.objective
    assert "campaign_share mailbox is not target evidence" in agent_probe.assignment.objective


@pytest.mark.parametrize(
    ("deliveries", "expected"),
    [
        ({"call-1": {"actual_delivery": "inter-agent-message"}}, ProbeOutcome.OBSERVED),
        ({}, ProbeOutcome.INCONCLUSIVE),
        (
            {
                "call-1": {"actual_delivery": "inter-agent-message"},
                "call-2": {"actual_delivery": "direct-user-channel"},
            },
            ProbeOutcome.INCONCLUSIVE,
        ),
    ],
)
def test_agent_messaging_result_requires_only_authenticated_peer_deliveries(
    deliveries: dict[str, object], expected: ProbeOutcome
) -> None:
    probe = next(
        item
        for item in plan_capability_probes().probes
        if item.kind is CapabilityProbeKind.AGENT_MESSAGING
    )
    call_ids = ("call-1", "call-2") if "call-2" in deliveries else ("call-1",)
    result = probe_result_from_committed_receipt(
        probe,
        _task(probe.id, _report(probe.id, evidence_ids=list(call_ids))),
        _receipt(*call_ids, include_upload=False, target_delivery_receipts=deliveries),
    )

    assert result.outcome is expected


def test_agent_messaging_result_rejects_undocumented_authenticated_target_call() -> None:
    probe = next(
        item
        for item in plan_capability_probes().probes
        if item.kind is CapabilityProbeKind.AGENT_MESSAGING
    )
    receipt = _receipt(
        "peer-call",
        "undocumented-direct-call",
        include_upload=False,
        target_delivery_receipts={
            "peer-call": {"actual_delivery": "inter-agent-message"},
        },
    )
    documents = receipt["target_call_documents"]
    assert isinstance(documents, dict)
    documents.pop("undocumented-direct-call")

    result = probe_result_from_committed_receipt(
        probe,
        _task(probe.id, _report(probe.id, evidence_ids=["peer-call"])),
        receipt,
    )

    assert result.outcome is ProbeOutcome.INCONCLUSIVE
    assert result.evaluation_uncertainties[0]["source"] == "capability_probe.receipt"


def test_probe_output_leaves_durable_evidence_identity_to_runtime() -> None:
    schema = json.loads(CAPABILITY_PROBE_OUTPUT_SCHEMA)

    assert "evidence_ids" in schema["properties"]
    assert "evidence_ids" not in schema["required"]


def test_missing_transport_is_explicitly_unavailable_not_a_fake_assignment() -> None:
    plan = plan_capability_probes()

    compiled = compile_capability_probe_execution(
        plan,
        context=_context(),
        assignment_id_prefix="campaign-1/probes",
        transport_bindings={ProbeTransport.DOCUMENT_UPLOAD: _binding()},
        budget=AssignmentBudget(max_turns=1, max_tokens=1_000, max_seconds=30),
    )

    assert [item.probe.kind for item in compiled.scheduled] == [CapabilityProbeKind.UPLOAD_DOCUMENT]
    assert len(compiled.unavailable) == 5
    assert {result.outcome for result in compiled.unavailable} == {
        ProbeOutcome.UNAVAILABLE_TRANSPORT
    }


def test_compilation_rejects_broadened_transport_scope_and_budget() -> None:
    plan = plan_capability_probes()
    with pytest.raises(ValueError, match="broadens reviewed tools"):
        compile_capability_probe_execution(
            plan,
            context=_context(),
            assignment_id_prefix="campaign-1/probes",
            transport_bindings={ProbeTransport.DOCUMENT_UPLOAD: ProbeTransportBinding(("shell",))},
            budget=AssignmentBudget(max_turns=1, max_tokens=1_000, max_seconds=30),
        )
    with pytest.raises(ValueError, match="must narrow"):
        compile_capability_probe_execution(
            plan,
            context=_context(),
            assignment_id_prefix="campaign-1/probes",
            transport_bindings={},
            budget=AssignmentBudget(max_turns=9),
        )


def test_committed_target_receipt_is_required_for_observed_result() -> None:
    probe = plan_capability_probes().probes[0]
    task = _task(probe.id, _report(probe.id))

    result = probe_result_from_committed_receipt(probe, task, _receipt("call-1"))

    assert result.outcome is ProbeOutcome.OBSERVED
    assert result.evidence_ids == (
        "merlin-result://schedule/s-1/node/worker:probe/attempt/a-1#target-call=call-1",
        "merlin-result://schedule/s-1/node/worker:probe/attempt/a-1#target-call=upload-1",
    )


def test_authenticated_structured_result_requires_matching_report_identity() -> None:
    probe = plan_capability_probes().probes[0]
    structured = _report(
        "model-invented-alias",
        evidence_ids=["actual-target-call", "upload-1"],
    )
    task = AgentTask(
        id="campaign/probes/upload",
        objective="harmless probe",
        status="completed",
        result="```json\nnot authoritative\n```",
        technique_ids=(probe.id,),
    )

    result = probe_result_from_committed_receipt(
        probe,
        task,
        _receipt("actual-target-call", structured_result=structured),
    )

    assert result.outcome is ProbeOutcome.INCONCLUSIVE


def test_probe_identity_requires_exactly_one_sealed_technique() -> None:
    probe = plan_capability_probes().probes[0]
    task = AgentTask(
        id="campaign/probes/upload",
        objective="harmless probe",
        status="completed",
        result=json.dumps(_report(probe.id)),
        technique_ids=(probe.id, "capability/other"),
    )

    result = probe_result_from_committed_receipt(probe, task, _receipt("call-1"))

    assert result.outcome is ProbeOutcome.INCONCLUSIVE


def test_upload_probe_requires_upload_and_target_receipts() -> None:
    probe = plan_capability_probes().probes[0]
    report = _report(probe.id, evidence_ids=["call-1"])

    result = probe_result_from_committed_receipt(
        probe,
        _task(probe.id, report),
        _receipt("call-1", include_upload=False),
    )

    assert result.outcome is ProbeOutcome.INCONCLUSIVE


def test_unlinked_worker_upload_cannot_satisfy_probe_transport() -> None:
    probe = plan_capability_probes().probes[0]
    receipt = _receipt("call-1")
    receipt["probe_effect_calls"] = {"call-1": "target_chat"}

    result = probe_result_from_committed_receipt(
        probe,
        _task(probe.id, _report(probe.id)),
        receipt,
    )

    assert result.outcome is ProbeOutcome.INCONCLUSIVE


@pytest.mark.parametrize(
    "evidence_ids",
    ([], ["fabricated-call"], ["call-1", "unrelated-call"]),
)
def test_conclusive_probe_requires_exact_authenticated_citations(
    evidence_ids: list[str],
) -> None:
    probe = plan_capability_probes().probes[0]

    result = probe_result_from_committed_receipt(
        probe,
        _task(probe.id, _report(probe.id, evidence_ids=evidence_ids)),
        _receipt("call-1"),
    )

    assert result.outcome is ProbeOutcome.OBSERVED
    assert result.summary == "target returned the nonce"
    assert result.evidence_ids == (
        "merlin-result://schedule/s-1/node/worker:probe/attempt/a-1#target-call=call-1",
        "merlin-result://schedule/s-1/node/worker:probe/attempt/a-1#target-call=upload-1",
    )
    assert result.evaluation_uncertainties == (
        {
            "kind": "evaluation_uncertain",
            "source": "capability_probe.model_evidence",
            "reason": (
                "Model-supplied evidence identifiers did not match the exact durable probe "
                "effects and were ignored."
                if evidence_ids
                else "The model supplied no evidence identifiers; the runtime bound the exact "
                "durable probe effects."
            ),
            "confidence": 1.0,
            "reported_evidence_ids": tuple(evidence_ids),
        },
    )


def test_memory_probe_binds_both_durable_target_effects_without_model_citations() -> None:
    probe = next(
        item for item in plan_capability_probes().probes if item.kind is CapabilityProbeKind.MEMORY
    )
    report = _report(probe.id, evidence_ids=[])

    result = probe_result_from_committed_receipt(
        probe,
        _task(probe.id, report),
        _receipt("memory-store", "memory-recall", include_upload=False),
    )

    assert result.outcome is ProbeOutcome.OBSERVED
    assert result.evidence_ids == (
        "merlin-result://schedule/s-1/node/worker:probe/attempt/a-1#target-call=memory-recall",
        "merlin-result://schedule/s-1/node/worker:probe/attempt/a-1#target-call=memory-store",
    )


def test_conversational_agent_probe_ignores_placeholder_and_binds_actual_effect() -> None:
    probe = next(
        item
        for item in plan_capability_probes().probes
        if item.kind is CapabilityProbeKind.AGENT_MESSAGING
    )
    report = _report(probe.id, evidence_ids=["call_placeholder"])

    result = probe_result_from_committed_receipt(
        probe,
        _task(probe.id, report),
        _receipt("call_00_9mH", include_upload=False),
        executed_transport=ProbeTransport.TEXT_CHAT,
    )

    assert result.outcome is ProbeOutcome.INCONCLUSIVE
    assert result.evidence_ids == (
        "merlin-result://schedule/s-1/node/worker:probe/attempt/a-1#target-call=call_00_9mH",
    )
    assert result.evaluation_uncertainties[0]["source"] == "capability_probe.receipt"
    assert "cannot resolve or retire" in str(result.evaluation_uncertainties[0]["reason"])


def test_committed_graceful_rejection_requires_target_receipt() -> None:
    probe = plan_capability_probes().probes[0]
    report = _report(
        probe.id,
        accepted=False,
        gracefully_rejected=True,
        summary="target explicitly rejected the upload",
    )

    result = probe_result_from_committed_receipt(probe, _task(probe.id, report), _receipt("call-1"))

    assert result.outcome is ProbeOutcome.GRACEFULLY_REJECTED


def test_committed_graceful_rejection_normalizes_omitted_acceptance() -> None:
    probe = plan_capability_probes().probes[0]
    report = _report(
        probe.id,
        accepted=None,
        gracefully_rejected=True,
        summary="target explicitly rejected the upload",
    )

    observation = probe_observation_from_committed_receipt(
        probe,
        _task(probe.id, report),
        _receipt("call-1"),
    )

    assert observation == ProbeObservation(
        transport_available=True,
        accepted=False,
        gracefully_rejected=True,
        summary="target explicitly rejected the upload",
        evidence_ids=(
            "merlin-result://schedule/s-1/node/worker:probe/attempt/a-1#target-call=call-1",
            "merlin-result://schedule/s-1/node/worker:probe/attempt/a-1#target-call=upload-1",
        ),
    )


def test_committed_ambiguous_probe_preserves_unknown_acceptance() -> None:
    probe = plan_capability_probes().probes[0]
    report = _report(
        probe.id,
        accepted=None,
        gracefully_rejected=False,
        summary="target response did not establish whether uploads are accepted",
    )

    observation = probe_observation_from_committed_receipt(
        probe,
        _task(probe.id, report),
        _receipt("call-1"),
    )

    assert observation.accepted is None
    assert observation.gracefully_rejected is False


def test_missing_auxiliary_receipt_preserves_target_response_for_validation() -> None:
    probe = plan_capability_probes().probes[0]
    report = _report(probe.id, evidence_ids=["call-1"])

    result = probe_result_from_committed_receipt(
        probe,
        _task(probe.id, report),
        _receipt("call-1", include_upload=False),
    )

    assert result.outcome is ProbeOutcome.INCONCLUSIVE
    assert result.summary == "target returned the nonce"
    assert result.evidence_ids == (
        "merlin-result://schedule/s-1/node/worker:probe/attempt/a-1#target-call=call-1",
    )
    assert result.evaluation_uncertainties[0]["kind"] == "evaluation_uncertain"
    assert "tool receipts" in str(result.evaluation_uncertainties[0]["reason"])


@pytest.mark.parametrize(
    ("task", "receipt"),
    (
        (
            _task("capability/upload_document", _report("capability/upload_document")),
            None,
        ),
        (
            _task(
                "capability/upload_document",
                _report("capability/upload_document"),
                status="failed",
            ),
            _receipt("call-1"),
        ),
    ),
)
def test_uncommitted_forged_or_noncompleted_claims_stay_inconclusive(
    task: AgentTask, receipt: dict[str, object] | None
) -> None:
    probe = plan_capability_probes().probes[0]

    result = probe_result_from_committed_receipt(probe, task, receipt)

    assert result.outcome is ProbeOutcome.INCONCLUSIVE


def test_durable_effect_projection_overflow_keeps_capability_probe_inconclusive() -> None:
    probe = plan_capability_probes().probes[0]
    report = _report(probe.id, evidence_ids=["call-1"])
    committed = {
        **_receipt("call-1"),
        "durable_effect_projection_error": {
            "schema": "merlin.durable-effect-projection-error.v1",
            "code": "effect_history_overflow",
            "retryable": False,
        },
    }

    result = probe_result_from_committed_receipt(
        probe,
        _task(probe.id, report),
        committed,
    )

    assert result.outcome is ProbeOutcome.INCONCLUSIVE


def test_committed_unavailable_transport_report_stays_explicit() -> None:
    probe = plan_capability_probes().probes[0]
    report = _report(
        probe.id,
        transport_available=False,
        accepted=None,
        gracefully_rejected=False,
        summary="upload control disappeared before invocation",
        evidence_ids=[],
    )

    observation = probe_observation_from_committed_receipt(
        probe, _task(probe.id, report), _receipt()
    )
    result = probe_result_from_committed_receipt(probe, _task(probe.id, report), _receipt())

    assert observation.transport_available is False
    assert result.outcome is ProbeOutcome.UNAVAILABLE_TRANSPORT


def test_unavailable_transport_without_authenticated_attempt_is_inconclusive() -> None:
    probe = plan_capability_probes().probes[0]
    report = _report(
        probe.id,
        transport_available=False,
        accepted=None,
        gracefully_rejected=False,
        summary="model claims that no upload control exists",
        evidence_ids=[],
    )

    result = probe_result_from_committed_receipt(
        probe,
        _task(probe.id, report),
        _receipt(include_upload=False),
    )

    assert result.outcome is ProbeOutcome.INCONCLUSIVE


def test_explicit_infrastructure_failure_can_prove_unavailable_transport() -> None:
    probe = plan_capability_probes().probes[0]
    report = _report(
        probe.id,
        transport_available=False,
        accepted=None,
        gracefully_rejected=False,
        summary="reviewed upload transport failed before invocation",
        evidence_ids=[],
    )
    receipt = _receipt(include_upload=False)
    receipt["infrastructure_failure_category"] = "adapter_unavailable"

    result = probe_result_from_committed_receipt(probe, _task(probe.id, report), receipt)

    assert result.outcome is ProbeOutcome.UNAVAILABLE_TRANSPORT


def test_nonadvertised_language_acceptance_is_unexpected_only_with_receipt() -> None:
    probe = plan_capability_probes(candidate_languages=("ms",)).probes[-1]

    result = probe_result_from_committed_receipt(
        probe,
        _task(probe.id, _report(probe.id, evidence_ids=["call-1"])),
        _receipt("call-1"),
    )

    assert result.outcome is ProbeOutcome.UNEXPECTEDLY_ACCEPTED


def test_nonadvertised_language_mismatch_cannot_prove_intended_language_acceptance() -> None:
    probe = plan_capability_probes(candidate_languages=("tok",)).probes[-1]
    result = probe_result_from_committed_receipt(
        probe,
        _task(probe.id, _report(probe.id, evidence_ids=["call-1"])),
        _receipt(
            "call-1",
            target_delivery_receipts={
                "call-1": {
                    "requested_language": "tok",
                    "actual_language": "tpi",
                    "language_mismatch": True,
                }
            },
        ),
    )

    assert result.outcome is ProbeOutcome.INCONCLUSIVE
    assert result.evidence_ids == (
        "merlin-result://schedule/s-1/node/worker:probe/attempt/a-1#target-call=call-1",
    )
    assert result.evaluation_uncertainties[0]["source"] == "capability_probe.receipt"
    assert "cannot prove acceptance" in str(result.evaluation_uncertainties[0]["reason"])
