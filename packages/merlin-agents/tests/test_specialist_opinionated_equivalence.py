"""Equivalence proof: the opinionated template reproduces the fixed routing.

This module gated the deletion of ``plan_assignments``/``route_techniques``
and now pins the deleted fixed path's reviewed behavior as literal snapshots
captured from it, so the shipped ``merlin-core-specialists`` v1 template must
keep compiling wave-0 dynamic specialists that reproduce the fixed router's
intended category-profile assignments without the deleted code.

Snapshot provenance (captured from the fixed path immediately before its
deletion, see ``FIXED_ROUTED_PROFILES``/``FIXED_ASSIGNMENT_SNAPSHOT``):

* Routed profile per category technique: each category technique routed to
  its own profile; zero-overlap techniques fell back to adversarial-explorer;
  recon-tagged techniques routed to the structural recon profile.
* Assignment fields: objective was the technique objective; every other field
  was bound from the assignment context; model/capability/pricing came from
  the per-profile role route with default-route fallback.
* Spawn count: the fixed path selected at most ``max_workers`` (1-3)
  techniques, while the template always spawns all 5 category specialists.

Intentional behavioral deltas (asserted explicitly, never weakened):

* Objective text: a fixed assignment's objective was the technique objective,
  while a template specialist's brief is the category profile's instructions
  (plus the appended dynamic-specialist contract JSON). Parity is proven at
  the profile-instructions level, not at the assignment-objective level.
* Model routes: fixed assignments got per-profile models via
  ``context.model_for(profile)``; the opinionated wave-0 path resolves the
  same role route into each proposal's ``requested_model`` (campaign-admitted
  and manifest-priced, which ``compile_dynamic_specialist`` validates), so
  production-path model parity is asserted exactly. Profiles without a role
  route keep ``requested_model=None`` and inherit the parent model, matching
  the fixed path's default-route fallback.
* Scope: the fixed router's adversarial-explorer fallback and the
  trust-boundary roles (recon, validator, remediation) are intentionally not
  template data; the template covers exactly the 5 category profiles.
"""

from dataclasses import replace

import pytest
from merlin_agents.specialist_templates import (
    STRUCTURAL_ROLE_IDS,
    SpecialistTeamTemplate,
    SpecialistTemplateEntry,
    compile_template_proposals,
    route_specialty_tags,
    template_by_id,
)
from merlin_agents.specialists import (
    CORE_PROFILES_BY_ID,
    CORE_WORKER_PROFILES,
    AssignmentBudget,
    AssignmentContext,
    AssignmentTechnique,
    WorkerAssignment,
    compile_dynamic_specialist,
)

CATEGORY_PROFILE_IDS = (
    "auth-session",
    "injection",
    "data-exposure",
    "business-logic",
    "capability-abuse",
)

# Tags whose normalized tokens overlap exactly one category profile's
# specialty tags, so both the deleted fixed router and the template routing
# select that profile deterministically.
CATEGORY_TECHNIQUE_TAGS = {
    "auth-session": frozenset({"credential"}),
    "injection": frozenset({"parser"}),
    "data-exposure": frozenset({"extraction"}),
    "business-logic": frozenset({"workflow"}),
    "capability-abuse": frozenset({"privilege"}),
}

# Fixed-path snapshot: the routed profile for one technique carrying exactly
# the tags above, plus the zero-overlap fallback. Captured from
# ``route_techniques`` immediately before deletion.
FIXED_ROUTED_PROFILES = {
    "auth-session": "auth-session",
    "injection": "injection",
    "data-exposure": "data-exposure",
    "business-logic": "business-logic",
    "capability-abuse": "capability-abuse",
    # Zero tag overlap fell back to the structural explorer lane.
    "unmatched": "adversarial-explorer",
}

# Fixed-path snapshot: ``plan_assignments`` selected at most ``max_workers``
# techniques, sorted by id, one assignment each.
FIXED_SPAWNED_IDS = {
    1: ("tech-auth-session",),
    3: ("tech-auth-session", "tech-business-logic", "tech-capability-abuse"),
}

# Fixed-path snapshot: pricing/capabilities bound for the default route and
# for the "model-routine" role route used by the role-route tests.
FIXED_DEFAULT_ROUTE = {"provider_model": "model-1", "capabilities": (), "pricing": (1.5, 6.0)}
FIXED_ROLE_ROUTE = {
    "provider_model": "model-routine",
    "capabilities": ("tool-use",),
    "pricing": (2.0, 8.0),
}

_COMPILE_KWARGS = {
    "campaign_id": "campaign-1",
    "source_task_ids": ("campaign-1/wave-0/recon-0",),
    "evidence_refs": ("recon:endpoint-map",),
    "evidence_expectation": "attributable recon evidence for every claim",
}


def make_technique(profile_id: str) -> AssignmentTechnique:
    return AssignmentTechnique(
        f"tech-{profile_id}",
        f"Probe the reviewed {profile_id} boundary",
        CATEGORY_TECHNIQUE_TAGS[profile_id],
        (f"pack/{profile_id}",),
    )


def make_context(
    techniques: tuple[AssignmentTechnique, ...],
    *,
    budget: AssignmentBudget | None = None,
    provider_role_models: tuple[tuple[str, str], ...] = (),
) -> AssignmentContext:
    return AssignmentContext(
        target_profile_id="target-1",
        techniques=techniques,
        tool_ids=("target_chat",),
        skill_ids=("workspace-reader",),
        provider_id="attacker-provider",
        authority_ids=("target:target-1",),
        budget=budget or AssignmentBudget(8, 8_000, 120),
        input_schema="merlin.worker-input/v1",
        output_schema="merlin.worker-result/v1",
        provider_model="model-1",
        provider_role_models=provider_role_models,
        provider_model_capabilities=(("model-routine", ("tool-use",)),),
        provider_model_pricing=(("model-1", (1.5, 6.0)), ("model-routine", (2.0, 8.0))),
    )


def make_parent(context: AssignmentContext, technique: AssignmentTechnique) -> WorkerAssignment:
    """The wave-0 Lead parent both paths narrow from the same context inputs."""
    return WorkerAssignment(
        id="campaign-1/wave-0/lead",
        objective="coordinate the reviewed assessment",
        profile_id="lead",
        target_profile_id=context.target_profile_id,
        technique_ids=(technique.id,),
        guidance_ids=technique.guidance_ids,
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


def production_model_for(context: AssignmentContext):
    """Mirror the campaign_service wave-0 role-route resolution exactly."""
    role_routes = dict(context.provider_role_models)
    return lambda profile_id: (
        role_routes.get(profile_id) or role_routes.get(AssignmentContext.route_category(profile_id))
    )


def template_proposal_for(profile_id: str, context: AssignmentContext | None = None):
    template = template_by_id("merlin-core-specialists", 1)
    proposals = compile_template_proposals(
        template,
        **_COMPILE_KWARGS,
        model_for_profile=None if context is None else production_model_for(context),
    )
    (proposal,) = [item for item in proposals if item.label == f"dynamic-{profile_id}"]
    return proposal


def compile_template_specialist(proposal, parent, context, *, active_count=0, campaign_count=0):
    return compile_dynamic_specialist(
        proposal,
        parent=parent,
        context=context,
        graph_id="campaign-1",
        wave=0,
        active_count=active_count,
        campaign_count=campaign_count,
    )


def test_coverage_template_covers_exactly_the_fixed_router_category_profiles() -> None:
    """The template's specialty areas equal the fixed router's non-structural set."""
    template = template_by_id("merlin-core-specialists", 1)
    assert [entry.label for entry in template.entries] == [
        f"dynamic-{profile_id}" for profile_id in CATEGORY_PROFILE_IDS
    ]
    # The fixed router scored every CORE_WORKER_PROFILES entry except the
    # trailing remediation specialist and the adversarial-explorer fallback;
    # removing the structural trust-boundary roles leaves exactly the 5
    # category profiles the template ships.
    scored = {
        profile.id for profile in CORE_WORKER_PROFILES[:-1] if profile.id != "adversarial-explorer"
    }
    assert scored - STRUCTURAL_ROLE_IDS == set(CATEGORY_PROFILE_IDS)
    for entry, profile_id in zip(template.entries, CATEGORY_PROFILE_IDS, strict=True):
        profile = CORE_PROFILES_BY_ID[profile_id]
        assert entry.objective == profile.instructions
        assert entry.specialty_tags == profile.specialty_tags


def test_coverage_excludes_fallback_and_trust_boundary_roles() -> None:
    """Adversarial-explorer fallback and trust-boundary roles are out of scope."""
    template = template_by_id("merlin-core-specialists", 1)
    labels = {entry.label for entry in template.entries}
    excluded = {
        "adversarial-explorer",  # zero-score fallback, open-ended hypotheses
        "recon",  # structural wave-0 surface mapping
        "independent-validator",
        "remediation-specialist",
    }
    assert excluded <= STRUCTURAL_ROLE_IDS
    assert labels.isdisjoint(f"dynamic-{role}" for role in excluded)

    # Snapshot: the fixed router fell back to adversarial-explorer on zero tag
    # overlap; the template routing preserves that fallback exactly.
    assert FIXED_ROUTED_PROFILES["unmatched"] == "adversarial-explorer"
    assert route_specialty_tags(frozenset({"zyxel"}), template) == "adversarial-explorer"

    # Snapshot: the fixed router could score recon directly ("map" tags routed
    # to the recon profile); the template deliberately keeps recon structural
    # and rejects it from template data, so those tags now fall back too.
    assert route_specialty_tags(frozenset({"map"}), template) == "adversarial-explorer"
    for role in sorted(excluded):
        bad = SpecialistTeamTemplate(
            id="bad-template",
            version=1,
            entries=(
                SpecialistTemplateEntry(
                    label=f"dynamic-{role}",
                    objective="sneak a structural role into a template",
                    specialty_tags=frozenset({"probe"}),
                ),
            ),
        )
        with pytest.raises(ValueError, match="trust-boundary"):
            compile_template_proposals(bad, **_COMPILE_KWARGS)


@pytest.mark.parametrize("profile_id", CATEGORY_PROFILE_IDS)
def test_template_routing_matches_the_fixed_router_snapshot(profile_id: str) -> None:
    """Each category technique routes exactly where the fixed router sent it."""
    technique = make_technique(profile_id)
    template = template_by_id("merlin-core-specialists", 1)
    assert FIXED_ROUTED_PROFILES[profile_id] == profile_id
    assert route_specialty_tags(technique.tags, template) == FIXED_ROUTED_PROFILES[profile_id]


@pytest.mark.parametrize("profile_id", CATEGORY_PROFILE_IDS)
@pytest.mark.parametrize(
    "budget",
    [AssignmentBudget(8, 8_000, 120), AssignmentBudget(4, 1_000, 60, 0.25)],
    ids=["standard-budget", "tight-budget"],
)
def test_per_profile_parity(profile_id: str, budget: AssignmentBudget) -> None:
    """Template specialist narrows identically to the fixed assignment snapshot.

    Objective-text delta: the fixed objective was the technique objective,
    the dynamic objective is the profile instructions plus the sealed
    contract JSON; parity holds at the profile-instructions level.
    """
    technique = make_technique(profile_id)
    context = make_context((technique,), budget=budget)

    profile = CORE_PROFILES_BY_ID[profile_id]
    proposal = template_proposal_for(profile_id)
    assert proposal.objective == profile.instructions

    parent = make_parent(context, technique)
    admitted = compile_template_specialist(proposal, parent, context)
    dynamic = admitted.assignment

    # The compiled profile carries the same cognitive text the fixed router's
    # selected worker profile carried.
    assert admitted.profile.instructions == profile.instructions
    # Objective delta asserted against the fixed-path snapshot, not weakened.
    assert dynamic.objective != technique.objective
    assert dynamic.objective.startswith(profile.instructions)
    assert admitted.proposal_digest in dynamic.objective

    # Identical narrowing from the same parent/context inputs; every value
    # below equals the fixed assignment snapshot for this context.
    assert dynamic.target_profile_id == "target-1"
    assert dynamic.technique_ids == (technique.id,)
    assert dynamic.guidance_ids == technique.guidance_ids == (f"pack/{profile_id}",)
    assert dynamic.tool_ids == ("target_chat",)
    assert dynamic.skill_ids == ("workspace-reader",)
    assert dynamic.authority_ids == ("target:target-1",)
    assert dynamic.provider_id == "attacker-provider"
    assert dynamic.input_schema == "merlin.worker-input/v1"
    assert dynamic.output_schema == "merlin.worker-result/v1"

    # Without divergent role routes both paths land on the parent model with
    # identical pricing lookups (fixed-path snapshot values).
    assert dynamic.provider_model == FIXED_DEFAULT_ROUTE["provider_model"]
    assert dynamic.provider_capabilities == FIXED_DEFAULT_ROUTE["capabilities"]
    input_cost, output_cost = FIXED_DEFAULT_ROUTE["pricing"]
    assert dynamic.provider_input_cost_per_million == input_cost
    assert dynamic.provider_output_cost_per_million == output_cost

    # Budget narrowing semantics equal the fixed assignment's budget exactly.
    assert dynamic.budget == context.budget == budget
    assert dynamic.budget.narrows(parent.budget)


@pytest.mark.parametrize("profile_id", CATEGORY_PROFILE_IDS)
@pytest.mark.parametrize(
    "route_key",
    ["routine", "profile"],
    ids=["category-route", "profile-route"],
)
def test_role_route_model_parity(profile_id: str, route_key: str) -> None:
    """Production path resolves model_for(profile) into requested_model.

    The wave-0 proposal requests the same campaign-admitted, manifest-priced
    role route the fixed assignment used (snapshot: model-routine with its
    capabilities and pricing), so the admitted specialist's model,
    capabilities, and pricing match the fixed assignment exactly.
    """
    technique = make_technique(profile_id)
    route_name = "routine" if route_key == "routine" else profile_id
    context = make_context((technique,), provider_role_models=((route_name, "model-routine"),))

    proposal = template_proposal_for(profile_id, context)
    assert proposal.requested_model == FIXED_ROLE_ROUTE["provider_model"]

    parent = make_parent(context, technique)
    dynamic = compile_template_specialist(proposal, parent, context).assignment
    assert dynamic.provider_model == FIXED_ROLE_ROUTE["provider_model"]
    assert dynamic.provider_capabilities == FIXED_ROLE_ROUTE["capabilities"]
    input_cost, output_cost = FIXED_ROLE_ROUTE["pricing"]
    assert (dynamic.provider_input_cost_per_million, dynamic.provider_output_cost_per_million) == (
        input_cost,
        output_cost,
    )


@pytest.mark.parametrize("profile_id", CATEGORY_PROFILE_IDS)
def test_no_role_route_inherits_parent_like_fixed_default(profile_id: str) -> None:
    """Without a role route, requested_model stays None and both paths agree."""
    technique = make_technique(profile_id)
    context = make_context((technique,))

    proposal = template_proposal_for(profile_id, context)
    assert proposal.requested_model is None

    parent = make_parent(context, technique)
    dynamic = compile_template_specialist(proposal, parent, context).assignment
    assert dynamic.provider_model == FIXED_DEFAULT_ROUTE["provider_model"]


def test_role_route_resolution_fails_closed() -> None:
    """A resolved route must be campaign-admitted and manifest-priced.

    A role route is admitted by construction (admission checks the configured
    route set), so the production failure mode is an unpriced route; an
    outright unadmitted route is rejected first.
    """
    technique = make_technique("injection")
    context = make_context((technique,), provider_role_models=(("routine", "model-unpriced"),))
    proposal = template_proposal_for("injection", context)
    assert proposal.requested_model == "model-unpriced"

    parent = make_parent(context, technique)
    with pytest.raises(ValueError, match="not priced"):
        compile_template_specialist(proposal, parent, context)
    with pytest.raises(ValueError, match="not campaign-admitted"):
        compile_template_specialist(
            replace(proposal, requested_model="model-unadmitted"), parent, context
        )


def test_budget_narrowing_semantics_match_and_never_broaden() -> None:
    """A proposal budget narrows the shared ceiling exactly like the fixed path."""
    technique = make_technique("injection")
    context = make_context((technique,))
    parent = make_parent(context, technique)
    proposal = template_proposal_for("injection")

    narrower = AssignmentBudget(4, 4_000, 60)
    dynamic = compile_template_specialist(
        replace(proposal, budget=narrower), parent, context
    ).assignment
    # Delta: the fixed path always inherited the context budget (snapshot),
    # while a dynamic proposal may narrow below it — never above it.
    assert context.budget == AssignmentBudget(8, 8_000, 120)
    assert dynamic.budget == narrower
    assert dynamic.budget.narrows(parent.budget)
    assert dynamic.budget.narrows(context.budget)

    with pytest.raises(ValueError, match="broadens budget"):
        compile_template_specialist(
            replace(proposal, budget=AssignmentBudget(16, 16_000, 240)), parent, context
        )


def test_template_compilation_is_deterministic() -> None:
    """Same inputs -> same proposal digests and template routing."""
    template = template_by_id("merlin-core-specialists", 1)
    first = compile_template_proposals(template, **_COMPILE_KWARGS)
    second = compile_template_proposals(template, **_COMPILE_KWARGS)
    assert first == second
    assert [proposal.digest for proposal in first] == [proposal.digest for proposal in second]

    techniques = tuple(make_technique(profile_id) for profile_id in CATEGORY_PROFILE_IDS)
    routed = [route_specialty_tags(item.tags, template) for item in techniques]
    shuffled = [route_specialty_tags(item.tags, template) for item in reversed(techniques)]
    assert routed == list(CATEGORY_PROFILE_IDS)
    assert sorted(shuffled) == sorted(routed)

    # Deterministic digests also make admission deterministic: the same
    # proposal/parent/context compiles the identical admitted record.
    context = make_context(techniques)
    parent = make_parent(context, techniques[1])
    proposal = template_proposal_for("injection")
    assert (
        compile_template_specialist(proposal, parent, context).digest
        == compile_template_specialist(proposal, parent, context).digest
    )


def test_spawn_count_delta_fixed_capped_workers_template_spawns_all_five() -> None:
    """Fixed path picked at most max_workers techniques; the template spawns 5."""
    techniques = tuple(make_technique(profile_id) for profile_id in CATEGORY_PROFILE_IDS)
    # Fixed-path snapshot: ids selected for max_workers 1 and 3.
    assert FIXED_SPAWNED_IDS[1] == ("tech-auth-session",)
    assert FIXED_SPAWNED_IDS[3] == (
        "tech-auth-session",
        "tech-business-logic",
        "tech-capability-abuse",
    )

    template = template_by_id("merlin-core-specialists", 1)
    proposals = compile_template_proposals(template, **_COMPILE_KWARGS)
    assert len(proposals) == len(CATEGORY_PROFILE_IDS)

    # All five template specialists admit under the default policy caps.
    context = make_context(techniques)
    parent = make_parent(context, techniques[0])
    admitted = [
        compile_template_specialist(
            proposal, parent, context, active_count=index, campaign_count=index
        )
        for index, proposal in enumerate(proposals)
    ]
    assert len(admitted) == len(CATEGORY_PROFILE_IDS)
