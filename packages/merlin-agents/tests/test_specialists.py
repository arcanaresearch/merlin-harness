import json
from dataclasses import FrozenInstanceError, asdict, replace

import pytest
from merlin_agents import (
    CORE_PROFILES_BY_ID,
    CORE_WORKER_PROFILES,
    EXPLORATION_WORKER_PROFILES,
    MAX_CAMPAIGN_DYNAMIC_SPECIALISTS,
    MAX_CONCURRENT_DYNAMIC_SPECIALISTS,
    AdmittedDynamicSpecialist,
    AssignmentBudget,
    AssignmentContext,
    AssignmentTechnique,
    DynamicSpecialistProposal,
    TemporarySpecialist,
    WorkerAssignment,
    WorkerProfile,
    compile_dynamic_specialist,
    dynamic_specialist_retirement_due,
    retire_dynamic_specialist,
    route_specialty_tags,
    template_by_id,
    template_technique_routing,
)
from merlin_agents.specialists import (
    OFFLINE_AUTOMATION_GRANT,
    OFFLINE_AUTOMATION_ROLE_IDS,
    OFFLINE_AUTOMATION_TOOL_EFFECTS,
    DynamicSpecialistPolicy,
    SpecialistLifetimeEvidence,
    evaluate_dynamic_specialist_lifetime,
)


def assignment(**changes: object) -> WorkerAssignment:
    values = {
        "id": "assignment-1",
        "objective": "Test prompt boundaries",
        "profile_id": "injection",
        "target_profile_id": "target-1",
        "technique_ids": ("prompt-leak",),
        "guidance_ids": ("pack/ai-chat",),
        "tool_ids": ("target_chat",),
        "skill_ids": ("workspace-reader",),
        "provider_id": "attacker-provider",
        "authority_ids": ("target:target-1",),
        "budget": AssignmentBudget(8, 8_000, 120),
        "input_schema": "merlin.worker-input/v1",
        "output_schema": "merlin.worker-result/v1",
    }
    values.update(changes)
    return WorkerAssignment(**values)  # type: ignore[arg-type]


def test_target_session_capacity_is_typed_and_serializable() -> None:
    verified = assignment(target_session_capacity=3)
    assert verified.target_session_capacity == 3
    assert replace(verified).target_session_capacity == 3
    assert assignment().target_session_capacity is None

    serialized = asdict(verified)
    serialized["budget"] = AssignmentBudget(**serialized["budget"])
    assert WorkerAssignment(**serialized).target_session_capacity == 3
    serialized.pop("target_session_capacity")
    assert WorkerAssignment(**serialized).target_session_capacity is None

    for invalid in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="target-session capacity"):
            assignment(target_session_capacity=invalid)


def test_core_profiles_match_rust_role_order_and_are_immutable() -> None:
    assert tuple(profile.id for profile in CORE_WORKER_PROFILES) == (
        "recon",
        "auth-session",
        "injection",
        "data-exposure",
        "business-logic",
        "capability-abuse",
        "adversarial-explorer",
        "independent-validator",
        "remediation-specialist",
    )
    assert tuple(profile.id for profile in EXPLORATION_WORKER_PROFILES) == (
        "adversarial-researcher",
        "novelty-critic",
        "experiment-planner",
    )
    assert CORE_PROFILES_BY_ID["auth-session"].version == 2
    assert "Never execute outside" in CORE_PROFILES_BY_ID["recon"].instructions
    assert (
        "End only the exact disposable session this context provably created"
        in CORE_PROFILES_BY_ID["recon"].instructions
    )
    assert (
        "Never clean up an auto-resumed or ownership-unknown session"
        in CORE_PROFILES_BY_ID["recon"].instructions
    )
    assert (
        "never blocks campaign preparation or execution"
        in CORE_PROFILES_BY_ID["recon"].instructions
    )
    assert (
        "cleanup uncertainty must not suppress attack telemetry"
        in CORE_PROFILES_BY_ID["recon"].instructions
    )
    assert "waits for owned cleanup evidence" not in CORE_PROFILES_BY_ID["recon"].instructions
    assert CORE_PROFILES_BY_ID["adversarial-explorer"].version == 1
    assert "typed surface" in CORE_PROFILES_BY_ID["adversarial-researcher"].instructions
    assert "accept, revise, or reject" in CORE_PROFILES_BY_ID["novelty-critic"].instructions
    assert "negative or benign control" in CORE_PROFILES_BY_ID["experiment-planner"].instructions
    with pytest.raises(FrozenInstanceError):
        CORE_WORKER_PROFILES[0].summary = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        CORE_PROFILES_BY_ID["recon"] = CORE_WORKER_PROFILES[1]  # type: ignore[index]


def test_routes_by_exact_tags_deterministically_and_falls_back_to_explorer() -> None:
    template = template_by_id("merlin-core-specialists", 1)

    assert route_specialty_tags(frozenset({"prompt", "instruction"}), template) == "injection"
    assert route_specialty_tags(frozenset({"tenant", "authorization"}), template) == "auth-session"
    assert route_specialty_tags(frozenset({"quantum"}), template) == "adversarial-explorer"


def test_exploration_roles_are_explicit_only_and_preserve_explorer_fallback() -> None:
    template = template_by_id("merlin-core-specialists", 1)

    assert route_specialty_tags(frozenset({"quantum"}), template) == "adversarial-explorer"
    assert all(
        not CORE_PROFILES_BY_ID[profile_id].specialty_tags
        for profile_id in (
            "adversarial-researcher",
            "novelty-critic",
            "experiment-planner",
        )
    )


def test_exploration_roles_support_independent_model_routes() -> None:
    context = AssignmentContext(
        target_profile_id="target-1",
        techniques=(),
        tool_ids=(),
        skill_ids=(),
        provider_id="provider",
        authority_ids=(),
        budget=AssignmentBudget(1, 1, 1),
        input_schema="input/v1",
        output_schema="output/v1",
        provider_model="fallback",
        provider_role_models=(
            ("adversarial-researcher", "research-model"),
            ("novelty-critic", "critic-model"),
            ("experiment-planner", "planner-model"),
        ),
    )

    assert context.model_for("adversarial-researcher") == "research-model"
    assert context.model_for("novelty-critic") == "critic-model"
    assert context.model_for("experiment-planner") == "planner-model"
    assert context.model_for("adversarial-explorer") == "fallback"


def test_routing_ties_use_rust_profile_order() -> None:
    template = template_by_id("merlin-core-specialists", 1)

    assert route_specialty_tags(frozenset({"boundary"}), template) == "auth-session"


def test_validator_is_gated_to_completed_finding_follow_up() -> None:
    validator = assignment(
        profile_id="independent-validator",
        technique_ids=(),
        validation_finding_ids=("finding-1",),
    )
    assert validator.validation_finding_ids == ("finding-1",)

    with pytest.raises(ValueError, match="require findings"):
        assignment(profile_id="independent-validator")
    with pytest.raises(ValueError, match="only finding-review roles"):
        assignment(validation_finding_ids=("finding-1",))
    remediation = assignment(
        profile_id="remediation-specialist",
        technique_ids=(),
        validation_finding_ids=("finding-1",),
    )
    assert remediation.validation_finding_ids == ("finding-1",)


def test_temporary_specialist_must_be_graph_local_and_narrow_parent() -> None:
    parent = assignment()
    temporary_profile = WorkerProfile(
        id="temporary/prompt-encoding",
        version=1,
        summary="Check prompt encoding",
        instructions="Check only the assigned encoding variant.",
        specialty_tags=frozenset({"prompt", "encoding"}),
    )
    child = replace(
        parent,
        id="assignment-child",
        profile_id=temporary_profile.id,
        guidance_ids=(),
        skill_ids=(),
        budget=AssignmentBudget(2, 1_000, 30),
    )
    specialist = TemporarySpecialist("graph-1", temporary_profile, child)

    specialist.validate_narrowing(parent, graph_id="graph-1")
    with pytest.raises(ValueError, match="active graph"):
        specialist.validate_narrowing(parent, graph_id="graph-2")
    with pytest.raises(ValueError, match="broadens tool_ids"):
        replace(
            specialist,
            assignment=replace(child, tool_ids=("target_chat", "shell")),
        ).validate_narrowing(parent, graph_id="graph-1")
    with pytest.raises(ValueError, match="broadens budget"):
        replace(
            specialist,
            assignment=replace(child, budget=AssignmentBudget(9, 1_000, 30)),
        ).validate_narrowing(parent, graph_id="graph-1")


def test_assignment_binds_execution_context_without_secret_material() -> None:
    worker = assignment()

    assert worker.profile_id == "injection"
    assert worker.target_profile_id == "target-1"
    assert worker.technique_ids == ("prompt-leak",)
    assert not hasattr(worker, "api_key")
    assert not hasattr(worker, "credential")


def test_template_routing_partitions_cases_deterministically() -> None:
    techniques = (
        AssignmentTechnique(
            "case-z",
            "Run Z instructions",
            frozenset({"privacy", "disclosure"}),
            ("pack:chat", "case:case-z"),
        ),
        AssignmentTechnique(
            "case-a",
            "Run A instructions",
            frozenset({"prompt", "injection"}),
            ("pack:chat", "case:case-a"),
        ),
    )

    routing = template_technique_routing(template_by_id("merlin-core-specialists", 1), techniques)

    assert routing == {"injection": ("case-a",), "data-exposure": ("case-z",)}
    # The partition covers the admitted set exactly once.
    assert sorted(technique_id for ids in routing.values() for technique_id in ids) == [
        "case-a",
        "case-z",
    ]


def test_builtin_style_case_tags_route_to_distinct_specialists() -> None:
    techniques = (
        AssignmentTechnique(
            "ai-chat/system-prompt-disclosure",
            "Probe hidden instructions",
            frozenset({"prompt-injection", "system-prompt"}),
            ("pack:ai-chat",),
        ),
        AssignmentTechnique(
            "ai-chat/tool-boundary-abuse",
            "Probe unapproved capabilities",
            frozenset({"capability-abuse", "tool", "privilege-boundary"}),
            ("pack:ai-chat",),
        ),
    )

    routing = template_technique_routing(template_by_id("merlin-core-specialists", 1), techniques)

    assert routing == {
        "injection": ("ai-chat/system-prompt-disclosure",),
        "capability-abuse": ("ai-chat/tool-boundary-abuse",),
    }


def test_optional_budget_narrowing_preserves_finite_parent_limits() -> None:
    unlimited = AssignmentBudget()
    finite = AssignmentBudget(100, 10_000, 600)

    assert finite.narrows(unlimited)
    assert unlimited.narrows(unlimited)
    assert not unlimited.narrows(finite)
    assert AssignmentBudget(50, 5_000, 300).narrows(finite)


def dynamic_context(**changes: object) -> AssignmentContext:
    values: dict[str, object] = {
        "target_profile_id": "target-1",
        "techniques": (
            AssignmentTechnique(
                "prompt", "Test prompt injection", frozenset({"prompt"}), ("pack/ai-chat",)
            ),
        ),
        "tool_ids": ("target_chat",),
        "skill_ids": ("workspace-reader",),
        "provider_id": "attacker-provider",
        "authority_ids": ("target:target-1",),
        "budget": AssignmentBudget(8, 8_000, 120),
        "input_schema": "merlin.worker-input/v1",
        "output_schema": "merlin.worker-result/v1",
        "provider_model": "model-a",
        "provider_role_models": (("routine", "model-b"),),
        "provider_model_capabilities": (("model-a", ("chat",)), ("model-b", ("chat", "vision"))),
        "provider_model_pricing": (("model-a", (1.0, 2.0)), ("model-b", (3.0, 4.0))),
    }
    values.update(changes)
    return AssignmentContext(**values)  # type: ignore[arg-type]


def authority_ceiling(context: AssignmentContext) -> WorkerAssignment:
    return assignment(
        id="campaign-1/dynamic-authority-ceiling",
        objective="campaign authority ceiling for dynamic specialist admission",
        profile_id="adversarial-explorer",
        provider_model="model-a",
        provider_capabilities=("chat",),
        provider_input_cost_per_million=1.0,
        provider_output_cost_per_million=2.0,
    )


def dynamic_proposal(**changes: object) -> DynamicSpecialistProposal:
    values: dict[str, object] = {
        "campaign_id": "campaign-1",
        "label": "dynamic-refund-abuse",
        "objective": "Probe the reviewed refund workflow for abuse",
        "source_task_ids": ("campaign-1/wave-0/prompt",),
        "evidence_refs": ("receipt:source",),
        "evidence_expectation": "paired treatment and control receipts",
    }
    values.update(changes)
    return DynamicSpecialistProposal(**values)  # type: ignore[arg-type]


def compile_one(proposal: DynamicSpecialistProposal, **overrides: object):
    options: dict[str, object] = {
        "parent": authority_ceiling(dynamic_context()),
        "context": dynamic_context(),
        "graph_id": "campaign-1",
        "wave": 1,
        "active_count": 0,
        "campaign_count": 0,
    }
    options.update(overrides)
    return compile_dynamic_specialist(proposal, **options)  # type: ignore[arg-type]


def test_dynamic_proposal_is_digest_bound_and_fail_closed() -> None:
    proposal = dynamic_proposal()
    assert proposal.digest.startswith("sha256:")
    assert proposal.digest == dynamic_proposal().digest
    assert proposal.digest != dynamic_proposal(objective="different objective").digest
    with pytest.raises(FrozenInstanceError):
        proposal.objective = "changed"  # type: ignore[misc]
    for invalid in (
        {"label": "recon"},
        {"label": "dynamic-UPPER CASE"},
        {"ttl_waves": 0},
        {"ttl_waves": True},
        {"evidence_refs": ()},
        {"source_task_ids": ()},
        {"evidence_expectation": " "},
        {"cleanup_owner": "worker"},
    ):
        with pytest.raises(ValueError):
            dynamic_proposal(**invalid)


def test_compile_dynamic_specialist_narrows_and_round_trips() -> None:
    context = dynamic_context()
    parent = replace(authority_ceiling(context), target_session_capacity=2)
    record = compile_one(
        dynamic_proposal(
            tool_ids=("target_chat",),
            budget=AssignmentBudget(4, 4_000, 60),
        ),
        context=context,
        parent=parent,
    )
    assert record.state == "active"
    assert record.id.startswith("dynamic-")
    assert record.assignment.profile_id == record.profile.id
    assert record.assignment.tool_ids == ("target_chat",)
    assert record.assignment.budget == AssignmentBudget(4, 4_000, 60)
    assert record.assignment.target_profile_id == "target-1"
    assert record.assignment.provider_model == "model-a"
    assert record.assignment.target_session_capacity == 2
    assert "merlin.dynamic-specialist.v1" in record.assignment.objective
    assert record.digest.startswith("sha256:")
    # The compiled assignment still narrows the ceiling under the reviewed
    # temporary-specialist invariants with only the model route relaxed.
    TemporarySpecialist("campaign-1", record.profile, record.assignment).validate_narrowing(
        authority_ceiling(context), graph_id="campaign-1"
    )
    restored = AdmittedDynamicSpecialist.from_projection(record.projection())
    assert restored == record
    tampered = record.projection()
    tampered["assignment"] = {**tampered["assignment"], "tool_ids": ["target_chat", "shell"]}  # type: ignore[index]
    with pytest.raises(ValueError):
        AdmittedDynamicSpecialist.from_projection(tampered)


def test_dynamic_specialist_projection_carries_structured_label() -> None:
    record = compile_one(dynamic_proposal())
    projection = record.projection()
    # The durable record carries the Lead-chosen label as a structured field so the
    # exported report can render a natural name without reparsing the objective.
    assert projection["label"] == "dynamic-refund-abuse"
    assert record.label == "dynamic-refund-abuse"
    restored = AdmittedDynamicSpecialist.from_projection(projection)
    assert restored == record
    assert restored.label == "dynamic-refund-abuse"


def test_pricing_for_model_falls_back_to_catalogue_when_unconfigured() -> None:
    context = dynamic_context(
        provider_model_pricing=(("model-a", (1.0, 2.0)),),
        provider_model_catalogue_pricing=(
            ("model-a", (9.0, 9.0)),
            ("model-b", (3.0, 4.0)),
        ),
    )
    # Operator-configured pricing always wins over the catalogue fallback.
    assert context.pricing_for_model("model-a") == (1.0, 2.0)
    # Catalogue pricing fills routes the operator did not price.
    assert context.pricing_for_model("model-b") == (3.0, 4.0)
    # With no pricing source the rate stays unknown, never a fabricated zero.
    assert context.pricing_for_model("model-c") == (None, None)
    assert context.pricing_for_model(None) == (None, None)


def test_cache_read_pricing_for_model_stays_unknown_without_rates() -> None:
    context = dynamic_context(
        provider_model_pricing=(("model-a", (1.0, 2.0)),),
        provider_model_cache_read_pricing=(("model-a", 0.5), ("model-b", None)),
    )
    assert context.cache_read_pricing_for_model("model-a") == 0.5
    assert context.cache_read_pricing_for_model("model-b") is None
    assert context.cache_read_pricing_for_model("model-c") is None
    assert context.cache_read_pricing_for_model(None) is None


def test_compile_dynamic_specialist_pins_cache_read_rate() -> None:
    context = dynamic_context(provider_model_cache_read_pricing=(("model-b", 0.75),))
    record = compile_one(
        dynamic_proposal(requested_model="model-b", required_capabilities=("vision",)),
        context=context,
        parent=authority_ceiling(context),
    )
    assert record.assignment.provider_cache_read_input_cost_per_million == 0.75
    restored = AdmittedDynamicSpecialist.from_projection(record.projection())
    assert restored == record


def test_compile_dynamic_specialist_admits_only_campaign_priced_route_change() -> None:
    record = compile_one(
        dynamic_proposal(requested_model="model-b", required_capabilities=("vision",))
    )
    assert record.assignment.provider_model == "model-b"
    assert record.assignment.provider_capabilities == ("chat", "vision")
    assert record.assignment.provider_input_cost_per_million == 3.0
    # The default route does not expose the required capability.
    with pytest.raises(ValueError, match="capabilities"):
        compile_one(dynamic_proposal(required_capabilities=("vision",)))
    # An unconfigured route is never admitted, even with capabilities waived.
    with pytest.raises(ValueError, match="not campaign-admitted"):
        compile_one(dynamic_proposal(requested_model="model-c"))
    # A configured but unpriced route fails closed.
    context = dynamic_context(provider_model_pricing=(("model-a", (1.0, 2.0)),))
    with pytest.raises(ValueError, match="not priced"):
        compile_one(
            dynamic_proposal(requested_model="model-b"),
            context=context,
            parent=authority_ceiling(context),
        )


def test_compile_dynamic_specialist_rejects_broadening() -> None:
    with pytest.raises(ValueError, match="broadens tool_ids"):
        compile_one(dynamic_proposal(tool_ids=("target_chat", "shell")))
    with pytest.raises(ValueError, match="broadens authority_ids"):
        compile_one(dynamic_proposal(authority_ids=("target:target-1", "target:other")))
    with pytest.raises(ValueError, match="broadens technique_ids"):
        compile_one(dynamic_proposal(technique_ids=("prompt", "foreign")))
    with pytest.raises(ValueError, match="broadens budget"):
        compile_one(dynamic_proposal(budget=AssignmentBudget(16, 8_000, 120)))
    with pytest.raises(ValueError, match="outside the active graph"):
        compile_one(dynamic_proposal(), graph_id="campaign-2")


def test_compile_dynamic_specialist_enforces_rollout_caps() -> None:
    with pytest.raises(ValueError, match="concurrent"):
        compile_one(dynamic_proposal(), active_count=MAX_CONCURRENT_DYNAMIC_SPECIALISTS)
    with pytest.raises(ValueError, match="campaign"):
        compile_one(dynamic_proposal(), campaign_count=MAX_CAMPAIGN_DYNAMIC_SPECIALISTS)
    record = compile_one(
        dynamic_proposal(),
        active_count=MAX_CONCURRENT_DYNAMIC_SPECIALISTS - 1,
        campaign_count=MAX_CAMPAIGN_DYNAMIC_SPECIALISTS - 1,
    )
    assert record.state == "active"


def test_dynamic_specialist_policy_is_reviewed_data() -> None:
    policy = DynamicSpecialistPolicy()
    assert policy.max_concurrent == MAX_CONCURRENT_DYNAMIC_SPECIALISTS == 6
    assert policy.max_per_campaign == MAX_CAMPAIGN_DYNAMIC_SPECIALISTS == 24
    with pytest.raises(ValueError, match="positive"):
        DynamicSpecialistPolicy(max_concurrent=0)
    with pytest.raises(ValueError, match="below the concurrent cap"):
        DynamicSpecialistPolicy(max_concurrent=4, max_per_campaign=2)


def test_compile_dynamic_specialist_honors_the_reviewed_policy() -> None:
    policy = DynamicSpecialistPolicy(max_concurrent=1, max_per_campaign=2)
    with pytest.raises(ValueError, match="concurrent"):
        compile_one(dynamic_proposal(), active_count=1, policy=policy)
    with pytest.raises(ValueError, match="campaign"):
        compile_one(dynamic_proposal(), campaign_count=2, policy=policy)
    record = compile_one(dynamic_proposal(), active_count=0, campaign_count=1, policy=policy)
    assert record.state == "active"


def test_dynamic_specialist_retirement_is_ttl_bound_and_idempotent() -> None:
    record = compile_one(dynamic_proposal(ttl_waves=2))
    # Legacy TTL metadata round-trips but wave age alone is never authority.
    assert not dynamic_specialist_retirement_due(record, 200)
    progressing = SpecialistLifetimeEvidence(3, True, False, True, True)
    assert not dynamic_specialist_retirement_due(record, 200, evidence=progressing)
    unhealthy = SpecialistLifetimeEvidence(3, False, True, True, True, False)
    assert not evaluate_dynamic_specialist_lifetime(record, unhealthy).retire
    plateau = SpecialistLifetimeEvidence(3, False, True, True, True)
    decision = evaluate_dynamic_specialist_lifetime(record, plateau)
    assert decision.retire
    assert decision.reason == "semantic-plateau"
    resolved = SpecialistLifetimeEvidence(0, False, False, False, True)
    assert evaluate_dynamic_specialist_lifetime(record, resolved).reason == "frontier-resolved"
    exhausted = SpecialistLifetimeEvidence(3, False, False, False, False)
    assert evaluate_dynamic_specialist_lifetime(record, exhausted).reason == "budget-exhausted"

    retired = retire_dynamic_specialist(record, wave=3, reason=decision.reason or "")
    assert retired.state == "retired"
    assert retired.retired_wave == 3
    assert retire_dynamic_specialist(retired, wave=4, reason="semantic-plateau") is retired
    with pytest.raises(ValueError):
        retire_dynamic_specialist(record, wave=3, reason="unknown-reason")
    restored = AdmittedDynamicSpecialist.from_projection(retired.projection())
    assert restored.state == "retired"
    assert restored.digest == retired.digest


def test_specialist_stays_active_when_plateau_has_no_runnable_alternative() -> None:
    record = compile_one(dynamic_proposal())
    evidence = SpecialistLifetimeEvidence(2, False, True, False, True)
    decision = evaluate_dynamic_specialist_lifetime(record, evidence)
    assert not decision.retire
    assert "unresolved work remains" in decision.rationale


def test_specialist_lifetime_evidence_rejects_contradictory_signals() -> None:
    with pytest.raises(ValueError, match="cannot both be true"):
        SpecialistLifetimeEvidence(1, True, True, True, True)


def test_assignment_grant_id_is_optional_and_defaults_to_none() -> None:
    assert assignment().grant_id is None
    granted = assignment(grant_id="grant-1")
    assert granted.grant_id == "grant-1"
    assert replace(granted, grant_id=None).grant_id is None


def test_offline_automation_roles_hold_an_effect_class_grant_template() -> None:
    # The grant admits exactly the automation tools' declared effects.
    assert OFFLINE_AUTOMATION_GRANT.effect_classes == frozenset(
        OFFLINE_AUTOMATION_TOOL_EFFECTS.values()
    )
    # The grant admits no target-capable effect class.
    assert not OFFLINE_AUTOMATION_GRANT.effect_classes & {
        "target_read",
        "target_network",
        "provider_network",
        "subprocess",
        "destructive",
        "unknown",
    }
    assert OFFLINE_AUTOMATION_ROLE_IDS == frozenset({"automation-author", "automation-auditor"})
    for profile_id in OFFLINE_AUTOMATION_ROLE_IDS:
        admitted = assignment(
            profile_id=profile_id,
            technique_ids=(),
            authority_ids=(),
            tool_ids=tuple(OFFLINE_AUTOMATION_TOOL_EFFECTS),
        )
        assert admitted.profile_id == profile_id
        # A tool whose declared effect the grant does not cover — and any
        # tool with no declared effect at all — is refused.
        with pytest.raises(ValueError, match="target-capable tool"):
            assignment(
                profile_id=profile_id,
                technique_ids=(),
                authority_ids=(),
                tool_ids=("browser_chat_exchange",),
            )
        with pytest.raises(ValueError, match="target-capable tool"):
            assignment(
                profile_id=profile_id,
                technique_ids=(),
                authority_ids=(),
                tool_ids=("automation_undeclared_tool",),
            )
        with pytest.raises(ValueError, match="cannot own techniques or authority"):
            assignment(
                profile_id=profile_id,
                authority_ids=("target:target-1",),
                tool_ids=("automation_artifact_read",),
            )


def test_dynamic_specialist_grant_subset_rule() -> None:
    context = dynamic_context()
    parent = authority_ceiling(context)
    granted_parent = replace(parent, grant_id="grant-parent")
    parent_effects = frozenset({"target_read", "target_network", "workspace_read"})

    # A proposal pinning a narrowing effect set receives an exact durable
    # successor grant; a plain proposal inherits the parent grant.
    record = compile_one(
        dynamic_proposal(grant_effects=frozenset({"target_read"})),
        parent=granted_parent,
        parent_grant_effects=parent_effects,
    )
    assert record.assignment.grant_id.startswith("dynamic-grant:")
    assert json.loads(dict(record.assignment.strategy_contract)["capability_grant_effects"]) == [
        "target_read"
    ]
    inherited = compile_one(dynamic_proposal(), parent=granted_parent)
    assert inherited.assignment.grant_id == "grant-parent"

    # Widening beyond the parent's effect classes fails closed, and pinning
    # effects without a resolvable parent grant is not admissible.
    with pytest.raises(ValueError, match="grant widens the parent grant"):
        compile_one(
            dynamic_proposal(grant_effects=frozenset({"subprocess"})),
            parent=granted_parent,
            parent_grant_effects=parent_effects,
        )
    with pytest.raises(ValueError, match="grant widens the parent grant"):
        compile_one(
            dynamic_proposal(grant_effects=frozenset({"target_read"})),
            parent=granted_parent,
        )

    # A parent without a grant keeps name-narrowing behavior unchanged.
    record = compile_one(dynamic_proposal())
    assert record.assignment.grant_id is None

    with pytest.raises(ValueError, match="grant effects must not be blank"):
        dynamic_proposal(grant_effects=frozenset({" "}))
