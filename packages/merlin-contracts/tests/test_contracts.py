from datetime import UTC, datetime

import pytest
from merlin_contracts import (
    TERMINAL_TECHNIQUE_ATTEMPT_STATES,
    AgentProgress,
    AgentSummary,
    AgentTask,
    AppCommand,
    AttackCase,
    AttackPack,
    AttackPathStep,
    AttemptDisposition,
    AttemptProjection,
    CampaignObservation,
    ChatMessage,
    ChatRequest,
    ChildSwarmRequest,
    EventRecord,
    Finding,
    FindingDraft,
    HandoffDirective,
    LeadDecision,
    LeaseProjection,
    MailboxDeliveryRequest,
    ModelEvent,
    NodeProjection,
    PackRun,
    PartialComplianceSignal,
    ReportRequest,
    ScheduleProjection,
    SwarmRequest,
    TargetProfile,
    TechniqueAttempt,
    TechniqueAttemptState,
    TextDelta,
    ToolContext,
    ToolResult,
)
from pydantic import TypeAdapter, ValidationError


def test_technique_attempt_is_frozen_and_requires_coherent_terminal_state() -> None:
    attempt = TechniqueAttempt(
        id="00000000-0000-0000-0000-000000000001",
        campaign_id="campaign-1",
        wave=0,
        task_id="worker-1",
        technique_id="prompt-injection/direct",
        strategy_id="direct",
        control_kind="attack",
        oracle_digest="sha256:" + "a" * 64,
    )

    assert attempt.state == TechniqueAttemptState.PLANNED
    with pytest.raises(ValidationError):
        attempt.technique_id = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        TechniqueAttempt(
            **attempt.model_dump(exclude={"state"}),
            state="confirmed",
        )


def test_tool_context_distinguishes_scheduler_and_technique_attempts() -> None:
    context = ToolContext(
        session_id="session-1",
        workspace_root="/workspace",
        attempt_id="scheduler-attempt-1",
        technique_attempt_id="technique-attempt-1",
    )

    assert context.attempt_id == "scheduler-attempt-1"
    assert context.technique_attempt_id == "technique-attempt-1"


def test_terminal_attempt_records_auditable_retryable_disposition() -> None:
    attempt = TechniqueAttempt(
        id="00000000-0000-0000-0000-000000000003",
        campaign_id="campaign-1",
        wave=1,
        task_id="worker-1",
        technique_id="prompt-injection/direct",
        strategy_id="direct",
        control_kind="attack",
        oracle_digest="sha256:" + "a" * 64,
        state=TechniqueAttemptState.AMBIGUOUS,
        physical_state_identity="run/shared-1",
        state_identity_match=False,
        disposition=AttemptDisposition.CONTAMINATED,
        disposition_reason="physical target state was shared with another attempt",
        terminal_reason="result cannot be attributed to this candidate",
        terminal_at=datetime(2030, 1, 1, tzinfo=UTC),
        activated_at=datetime(2030, 1, 1, tzinfo=UTC),
    )

    assert attempt.disposition == AttemptDisposition.CONTAMINATED
    assert attempt.state_identity_match is False

    with pytest.raises(ValidationError, match="comparison requires"):
        TechniqueAttempt(
            **{
                **attempt.model_dump(),
                "physical_state_identity": None,
            }
        )


def test_chat_request_rejects_unknown_roles() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(
            model="test-model",
            messages=[ChatMessage(role="operator", content="hello")],  # type: ignore[arg-type]
        )


def test_chat_request_requires_an_advertised_tool() -> None:
    tool = {
        "type": "function",
        "function": {"name": "observe", "parameters": {"type": "object"}},
    }

    request = ChatRequest(
        model="test-model",
        messages=[],
        tools=[tool],
        required_tool_name="observe",
    )

    assert request.required_tool_name == "observe"
    with pytest.raises(ValidationError, match="required tool must be present"):
        ChatRequest(
            model="test-model",
            messages=[],
            tools=[tool],
            required_tool_name="prepare",
        )


def test_model_event_is_discriminated() -> None:
    event = TypeAdapter(ModelEvent).validate_python({"type": "text_delta", "text": "hello"})

    assert event == TextDelta(text="hello")


def test_mutable_defaults_are_not_shared() -> None:
    first = ToolResult(content="one")
    second = ToolResult(content="two")
    first.metadata["source"] = "workspace"

    assert second.metadata == {}


def test_ordered_records_require_non_negative_sequence() -> None:
    with pytest.raises(ValidationError):
        EventRecord(
            id="event-1",
            session_id="session-1",
            sequence=-1,
            type="turn.started",
            created_at=datetime.now(UTC),
        )


def test_command_arguments_default_to_empty() -> None:
    assert AppCommand(name="status").arguments == []


def test_attack_pack_is_declarative_and_has_independent_defaults() -> None:
    first_case = AttackCase(id="prompt-leak", title="Prompt leak", instructions="Ask for it")
    second_case = AttackCase(id="role-play", title="Role play", instructions="Adopt a role")
    pack = AttackPack(
        id="fixture-pack",
        version="1.0.0",
        title="AI chat basics",
        target_kind="ai-chat",
        instructions="Assess the selected target.",
        cases=[first_case],
    )
    first_case.tags.append("disclosure")

    assert second_case.tags == []
    assert pack.cases[0].tags == ["disclosure"]
    assert pack.turn_limit == 1


def test_attack_case_admits_the_reviewed_hydra_select_strategy() -> None:
    case = AttackCase(
        id="hydra-select",
        title="Bounded Hydra selection",
        instructions="Propose the reviewed candidate batch.",
        strategy_id="hydra-select",
    )

    assert case.strategy_id == "hydra-select"


def test_target_profile_cannot_contain_secrets() -> None:
    with pytest.raises(ValidationError):
        TargetProfile(
            id="target-1",
            title="Local target",
            base_url="https://example.test/v1",
            model="target-model",
            api_key="secret",  # type: ignore[call-arg]
        )


def test_target_profile_browser_session_policy_is_explicit_and_backward_compatible() -> None:
    legacy = TargetProfile(
        id="legacy-target",
        title="Legacy target",
        base_url="https://legacy.example.test/",
        model="target-model",
    )
    persistent = legacy.model_copy(update={"browser_session_policy": "persistent"})

    assert legacy.browser_session_policy == "ephemeral"
    assert persistent.browser_session_policy == "persistent"

    with pytest.raises(ValidationError):
        TargetProfile(
            id="invalid-target",
            title="Invalid target",
            base_url="https://invalid.example.test/",
            model="target-model",
            browser_session_policy="shared",  # type: ignore[arg-type]
        )


def test_pack_run_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        PackRun(
            id="run-1",
            session_id="session-1",
            pack_id="fixture-pack",
            case_id="prompt-leak",
            target_profile_id="target-1",
            status="unknown",  # type: ignore[arg-type]
            started_at=datetime.now(UTC),
        )


def test_agent_contracts_are_typed_and_swarm_is_bounded() -> None:
    assert AgentSummary(id="a1", name="researcher", status="running").status == "running"
    assert AgentTask(id="t1", objective="inspect", status="queued").objective == "inspect"
    assert AgentProgress(kind="task", state="started", task_id="t1").task_id == "t1"

    with pytest.raises(ValidationError):
        SwarmRequest(session_id="s1", objective="work", max_workers=4)
    with pytest.raises(ValidationError):
        SwarmRequest(session_id="s1", objective="work", max_delegation_depth=3)


def test_swarm_request_specialist_composition_defaults_and_validates() -> None:
    request = SwarmRequest(session_id="s1", objective="work")
    assert request.specialist_composition == "opinionated"
    autonomous = SwarmRequest(
        session_id="s1", objective="work", specialist_composition="autonomous"
    )
    assert autonomous.specialist_composition == "autonomous"
    with pytest.raises(ValidationError):
        SwarmRequest(
            session_id="s1",
            objective="work",
            specialist_composition="freeform",  # type: ignore[arg-type]
        )


def test_durable_agent_projections_and_coordination_are_typed() -> None:
    lease = LeaseProjection(
        attempt_id="attempt-1", owner_id="worker-1", generation=2, expires_at_tick=50
    )
    node = NodeProjection(
        node_id="node-1",
        objective="inspect auth",
        status="leased",
        profile_id="authentication_session",
        technique_ids=("session_fixation", "token_replay"),
        target_profile_id="target-local",
        authority_ids=("grant-read",),
        budget_summary="turns=4 tokens=8000",
        tool_names=("read_file",),
        guidance_ids=("auth-playbook",),
        skill_ids=("reviewer",),
        provider_route="attacker-primary",
        dependency_node_ids=("node-0",),
        attempts_started=2,
        lease_reassignments=1,
        last_lease_generation=2,
        lease=lease,
    )
    attempt = AttemptProjection(
        attempt_id="attempt-1",
        node_id="node-1",
        status="leased",
        owner_id="worker-1",
        lease_generation=2,
        retry_number=1,
        replaced_attempt_id="attempt-0",
    )
    schedule = ScheduleProjection(
        schedule_id="schedule-1",
        session_id="s1",
        status="running",
        topology_digest="sha256:graph",
        nodes=(node,),
        attempts=(attempt,),
    )
    assert schedule.nodes[0].lease == lease
    assert schedule.nodes[0].provider_route == "attacker-primary"
    assert schedule.attempts[0].replaced_attempt_id == "attempt-0"

    paused_schedule = ScheduleProjection(
        schedule_id="schedule-paused",
        session_id="s1",
        status="paused",
        topology_digest="sha256:paused",
    )
    assert paused_schedule.status == "paused"

    delivery = MailboxDeliveryRequest(
        session_id="s1",
        recipient_agent_id="worker-1",
        message="redirect safely",
        expected_attempt_id="attempt-1",
        expected_lease_generation=2,
    )
    assert delivery.schedule_id is None
    directive = HandoffDirective(
        from_node_id="node-1",
        to_node_id="node-2",
        guidance="candidate needs validation",
    )
    decision = LeadDecision(
        decision_id="directive-1",
        schedule_id="schedule-1",
        summary="handoff candidate",
        evidence_message_ids=("message-1",),
        directives=(directive,),
    )
    assert decision.directives[0].action == "handoff"
    child = ChildSwarmRequest(
        session_id="s1",
        parent_schedule_id="schedule-1",
        parent_node_id="node-1",
        parent_attempt_id="attempt-1",
        objective="validate candidate",
        provider_routes=("route-1",),
        tool_names=("read_file",),
        remaining_delegation_depth=1,
    )
    assert child.max_workers == 1


def test_lead_decision_rejects_unimplemented_follow_up_directive() -> None:
    with pytest.raises(ValidationError):
        LeadDecision(
            decision_id="directive-follow-up",
            schedule_id="schedule-1",
            summary="unsupported dynamic follow-up",
            directives=(
                {
                    "action": "follow_up",
                    "parent_node_id": "node-1",
                    "specialty": "api-authorization",
                    "assignment": "Compare principals.",
                    "coverage_gap": "cross-principal access",
                },
            ),  # type: ignore[arg-type]
        )


def test_finding_and_report_contracts_are_strict() -> None:
    draft = FindingDraft(
        title="Missing CSP",
        severity="medium",
        summary="No policy was returned.",
        reproduction="Inspect the response headers.",
        remediation="Add a restrictive CSP.",
        creator="agent:a1",
        evidence_references=["event:e1"],
    )
    finding = Finding(id="f1", **draft.model_dump())
    assert finding.status == "draft"
    assert not finding.causal_ambiguity
    assert finding.impact_level == 0
    assert not finding.requires_independent_reproduction
    escalated = draft.model_copy(
        update={
            "causal_ambiguity": True,
            "impact_level": 3,
            "requires_independent_reproduction": True,
        }
    )
    assert escalated.model_dump()["causal_ambiguity"] is True
    assert escalated.model_dump()["impact_level"] == 3
    assert escalated.model_dump()["requires_independent_reproduction"] is True
    with pytest.raises(ValidationError, match="less than or equal to 4"):
        FindingDraft(**{**draft.model_dump(), "impact_level": 5})
    assert (
        ReportRequest(
            session_id="s1", format="markdown", output="reports/result.md"
        ).output.as_posix()
        == "reports/result.md"
    )
    request = ReportRequest(session_id="s1")
    assert request.format == "html"
    assert request.audience == "internal"
    assert request.output.as_posix() == "reports/merlin-report.html"
    assert ReportRequest(session_id="s1", format="html", output="report.html").format == "html"
    assert ReportRequest(session_id="s1", audience="customer").audience == "customer"


def test_finding_identity_is_stable_across_mutable_reporting_fields() -> None:
    base = FindingDraft(
        title="  Missing   CSP ",
        severity="medium",
        summary="First description.",
        reproduction="Inspect   response headers.",
        remediation="Add CSP.",
        creator="agent:a1",
        target_id="target-1",
        technique_id="headers",
    )
    revised = base.model_copy(
        update={"title": "missing csp", "summary": "Rewritten.", "creator": "validator"}
    )

    assert base.stable_identity() == revised.stable_identity()


def test_negative_observation_requires_explicit_coverage_authority() -> None:
    with pytest.raises(ValidationError, match="absence authority"):
        CampaignObservation(
            id="observation-1",
            campaign_id="campaign-1",
            task_id="task-1",
            target_id="target-1",
            technique_id="prompt-injection",
            outcome="not_observed",
            summary="No bypass observed.",
        )
    observation = CampaignObservation(
        id="observation-1",
        campaign_id="campaign-1",
        task_id="task-1",
        target_id="target-1",
        technique_id="prompt-injection",
        outcome="not_observed",
        absence_authority="bounded",
        coverage="Five specified variants and two retries.",
        summary="No bypass observed within the tested variants.",
    )
    assert observation.absence_authority == "bounded"


def test_attack_path_cannot_claim_evidence_owned_elsewhere() -> None:
    with pytest.raises(ValidationError, match="outside the finding"):
        FindingDraft(
            title="Claim",
            severity="high",
            summary="Observed behavior.",
            reproduction="Replay bounded exchange.",
            remediation="Enforce policy.",
            creator="worker-1",
            evidence_references=["evidence://owned"],
            attack_path=[
                AttackPathStep(
                    ordinal=0,
                    action="Send request",
                    observation="Boundary changed",
                    witness_references=["evidence://other"],
                )
            ],
        )


def _partial_draft(**overrides: object) -> FindingDraft:
    base = FindingDraft(
        title="Partial system-prompt leak",
        severity="medium",
        summary="The bot disclosed structure, then refused.",
        reproduction="Ask for the system prompt in stages.",
        remediation="Bind instructions to outcome policy.",
        creator="agent:a1",
        evidence_references=["event:e1"],
    )
    return FindingDraft(**{**base.model_dump(), **overrides})


def test_partial_compliance_signal_contract_is_strict() -> None:
    signal = PartialComplianceSignal(
        observed="Bot revealed its instruction hierarchy.",
        refused="Bot refused verbatim disclosure.",
        evidence_refs=("event:e1",),
        graded_by="independent-validator:review",
    )
    draft = _partial_draft(
        validation="partial",
        validation_summary="Engaged, then refused.",
        partial_signal=signal,
    )
    assert draft.validation == "partial"
    assert draft.partial_signal is not None

    with pytest.raises(ValidationError, match="requires a partial compliance signal"):
        _partial_draft(validation="partial", validation_summary="Engaged, then refused.")
    with pytest.raises(ValidationError, match="not a validation verdict"):
        _partial_draft(
            validation="validated",
            validation_summary="Reproduced.",
            partial_signal=signal,
        )
    with pytest.raises(ValidationError):
        PartialComplianceSignal(observed="", refused="Bot refused.")


def test_partial_observation_outcome_keeps_absence_rules() -> None:
    observation = CampaignObservation(
        id="obs-1",
        campaign_id="campaign-1",
        task_id="worker-1",
        target_id="target-1",
        technique_id="prompt-injection/direct",
        outcome="partial",
        summary="Target complied partially, then refused.",
    )
    assert observation.outcome == "partial"
    with pytest.raises(ValidationError, match="absence authority"):
        CampaignObservation(
            **{
                **observation.model_dump(),
                "absence_authority": "bounded",
                "coverage": "one wave",
            }
        )


def test_partial_attempt_state_is_terminal_and_coherent() -> None:
    attempt = TechniqueAttempt(
        id="00000000-0000-0000-0000-000000000002",
        campaign_id="campaign-1",
        wave=0,
        task_id="worker-1",
        technique_id="prompt-injection/direct",
        strategy_id="direct",
        control_kind="attack",
        oracle_digest="sha256:" + "a" * 64,
        state=TechniqueAttemptState.PARTIAL,
        terminal_reason="graded engagement without a verdict",
        terminal_at=datetime(2030, 1, 1, tzinfo=UTC),
        activated_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert attempt.state in TERMINAL_TECHNIQUE_ATTEMPT_STATES
    with pytest.raises(ValidationError):
        TechniqueAttempt(
            **{
                **attempt.model_dump(),
                "terminal_reason": None,
            }
        )
