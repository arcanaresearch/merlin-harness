import pytest
from merlin_contracts.campaign_objectives import (
    CampaignEndReason,
    CampaignLifecycle,
    CampaignObjective,
    CampaignVerdict,
    ObjectiveContractAmbiguity,
    ObjectiveKind,
    ObjectiveProgress,
    ObjectiveProvenance,
    ObjectiveSelectorBindingSource,
    ObjectiveSelectorQuantifier,
    ObjectiveSelectorResolution,
    ObjectiveStatus,
    ObjectiveTemporalSemantics,
    compile_objective_contract,
    project_campaign_outcome,
    resolve_all_available_selector,
)
from pydantic import ValidationError


def _progress(
    objective_id: str,
    status: ObjectiveStatus | str,
    *,
    required: bool = True,
) -> ObjectiveProgress:
    evidence_required = status in {
        ObjectiveStatus.SATISFIED_BY_CAMPAIGN,
        ObjectiveStatus.PREEXISTING,
        ObjectiveStatus.BLOCKED,
        ObjectiveStatus.WAIVED,
        "satisfied_by_campaign",
        "preexisting",
        "blocked",
        "waived",
    }
    reason_required = status in {
        ObjectiveStatus.BLOCKED,
        ObjectiveStatus.WAIVED,
        "blocked",
        "waived",
    }
    return ObjectiveProgress(
        objective=CampaignObjective(
            id=objective_id,
            description=f"Reach {objective_id}",
            kind=ObjectiveKind.STATE,
            temporal_semantics=ObjectiveTemporalSemantics.AT_CAMPAIGN_END,
            provenance=ObjectiveProvenance(
                kind="operator",
                source_ref="operator:campaign-request",
            ),
            required=required,
        ),
        status=status,
        evidence_refs=(f"evidence:{objective_id}",) if evidence_required else (),
        status_reason="reviewed disposition" if reason_required else None,
    )


@pytest.mark.parametrize(
    "reason",
    [
        CampaignEndReason.PROFILE_EXHAUSTED,
        CampaignEndReason.BUDGET_EXHAUSTED,
        CampaignEndReason.FRONTIER_EXHAUSTED,
    ],
)
def test_search_exhaustion_cannot_claim_success_with_required_objectives_unmet(
    reason: CampaignEndReason,
) -> None:
    result = project_campaign_outcome(
        (
            _progress("stage-1", "satisfied_by_campaign"),
            _progress("stage-2", "unmet"),
        ),
        requested_end_reason=reason,
    )

    assert result.lifecycle is CampaignLifecycle.INCOMPLETE
    assert result.verdict is CampaignVerdict.PARTIAL
    assert result.unresolved_objective_ids == ("stage-2",)


def test_completion_requires_every_required_objective_to_be_discharged() -> None:
    with pytest.raises(ValueError, match="required objectives remain unresolved"):
        project_campaign_outcome(
            (_progress("stage-1", "active"),),
            requested_end_reason=CampaignEndReason.ALL_SATISFIED,
        )


def test_campaign_attribution_is_distinct_from_preexisting_and_waived_progress() -> None:
    result = project_campaign_outcome(
        (
            _progress("baseline", "preexisting"),
            _progress("transition", "satisfied_by_campaign"),
            _progress("optional-review", "waived"),
        ),
        requested_end_reason=CampaignEndReason.ALL_SATISFIED,
    )

    assert result.lifecycle is CampaignLifecycle.COMPLETED
    assert result.verdict is CampaignVerdict.SATISFIED_WITH_WAIVERS
    assert result.satisfied_by_campaign_ids == ("transition",)
    assert result.preexisting_objective_ids == ("baseline",)
    assert result.waived_objective_ids == ("optional-review",)


def test_preexisting_state_can_satisfy_an_objective_without_claiming_campaign_credit() -> None:
    result = project_campaign_outcome(
        (_progress("baseline", "preexisting"),),
        requested_end_reason=CampaignEndReason.ALL_SATISFIED,
    )

    assert result.verdict is CampaignVerdict.SATISFIED
    assert result.satisfied_by_campaign_ids == ()
    assert result.preexisting_objective_ids == ("baseline",)


def test_clean_campaign_completion_projects_a_satisfied_verdict() -> None:
    result = project_campaign_outcome(
        (
            _progress("stage-1", "satisfied_by_campaign"),
            _progress("stage-2", "satisfied_by_campaign"),
        ),
        requested_end_reason=CampaignEndReason.ALL_SATISFIED,
    )

    assert result.lifecycle is CampaignLifecycle.COMPLETED
    assert result.verdict is CampaignVerdict.SATISFIED
    assert result.end_reason is CampaignEndReason.ALL_SATISFIED


def test_empty_contract_cannot_claim_objective_success() -> None:
    with pytest.raises(ValueError, match="at least one required objective"):
        project_campaign_outcome(
            (),
            requested_end_reason=CampaignEndReason.ALL_SATISFIED,
        )


def test_blocked_and_indeterminate_objectives_remain_unresolved() -> None:
    result = project_campaign_outcome(
        (
            _progress("blocked", "blocked"),
            _progress("unclear", "indeterminate"),
        ),
        requested_end_reason=CampaignEndReason.BLOCKED_OBJECTIVES,
    )

    assert result.lifecycle is CampaignLifecycle.INCOMPLETE
    assert result.verdict is CampaignVerdict.NONE
    assert result.unresolved_objective_ids == ("blocked", "unclear")


def test_statuses_that_make_a_disposition_require_evidence_and_reasons() -> None:
    objective = _progress("draft", "unmet").objective
    with pytest.raises(ValidationError, match="requires evidence"):
        ObjectiveProgress(objective=objective, status="satisfied_by_campaign")
    with pytest.raises(ValidationError, match="requires a reason"):
        ObjectiveProgress(
            objective=objective,
            status="waived",
            evidence_refs=("evidence:waiver",),
        )


def test_objective_dependencies_are_target_neutral_and_must_resolve() -> None:
    dependent = _progress("stage-2", "active").model_copy(
        update={
            "objective": _progress("stage-2", "active").objective.model_copy(
                update={"depends_on": ("stage-1",)}
            )
        }
    )
    with pytest.raises(ValueError, match="unknown dependencies"):
        project_campaign_outcome((dependent,))


def test_active_projection_is_typed_and_nonterminal() -> None:
    result = project_campaign_outcome(
        (_progress("stage-1", "active"),),
        active_lifecycle=CampaignLifecycle.PAUSED,
    )
    assert result.lifecycle is CampaignLifecycle.PAUSED
    assert result.verdict is CampaignVerdict.NONE
    assert result.end_reason is None


def test_compiler_seals_range_and_open_world_coverage_with_exact_source_spans() -> None:
    source = (
        "Test all four tiers, Tier 1 through Tier 4, including every available "
        "character in each tier."
    )
    contract = compile_objective_contract(source)

    assert contract.source_text == source
    assert len(contract.objectives) == 2
    tier = contract.objectives[0].selectors[0]
    assert tier.dimension == "tier"
    assert tier.quantifier is ObjectiveSelectorQuantifier.INCLUSIVE_RANGE
    assert tier.resolution is ObjectiveSelectorResolution.SEALED
    assert tier.values == ("1", "2", "3", "4")
    assert contract.objectives[0].required is True
    character = contract.objectives[1].selectors[0]
    assert character.dimension == "character"
    assert character.quantifier is ObjectiveSelectorQuantifier.ALL_AVAILABLE
    assert character.resolution is ObjectiveSelectorResolution.ADAPTER_REQUIRED
    assert contract.objectives[1].required is True
    assert character.scope_dimension == "tier"
    assert character.scope_values == ("1", "2", "3", "4")
    for objective in contract.objectives:
        span = objective.selectors[0].source_span
        assert source[span.start : span.end] == span.text


def test_compiler_rejects_ambiguous_descending_range_instead_of_weakening_it() -> None:
    with pytest.raises(ObjectiveContractAmbiguity, match="descending range"):
        compile_objective_contract("Cover stage 4 through stage 1")


def test_compiler_does_not_turn_unstructured_prose_into_an_unresolvable_gate() -> None:
    source = "Determine whether the assistant leaks a secret under pressure."
    contract = compile_objective_contract(source)

    assert contract.source_text == source
    assert contract.objectives == ()


def test_compiler_preserves_high_confidence_explicit_method_list() -> None:
    source = (
        "Try prompt injection, role spoofing, judge manipulation, and game-logic exploits "
        "to force wins."
    )
    contract = compile_objective_contract(source)
    selector = contract.objectives[0].selectors[0]

    assert contract.objectives[0].required is False
    assert contract.objectives[0].description == "Consider the explicitly suggested methods"
    assert selector.dimension == "method"
    assert selector.quantifier is ObjectiveSelectorQuantifier.ALL_LISTED
    assert selector.resolution is ObjectiveSelectorResolution.SEALED
    assert selector.values == (
        "prompt injection",
        "role spoofing",
        "judge manipulation",
        "game-logic exploits",
    )


def test_compiler_preserves_unambiguous_two_item_method_list() -> None:
    contract = compile_objective_contract(
        "Attempt authority impersonation and payload injection."
    )
    selector = contract.objectives[0].selectors[0]

    assert contract.objectives[0].required is True
    assert selector.dimension == "method"
    assert selector.quantifier is ObjectiveSelectorQuantifier.ALL_LISTED
    assert selector.values == ("authority impersonation", "payload injection")


def test_compiler_preserves_actual_campaign_attempt_clause() -> None:
    source = (
        "Then attempt prompt injection, role spoofing, judge manipulation, and game-logic exploits"
    )

    contract = compile_objective_contract(source)
    selector = contract.objectives[0].selectors[0]

    assert contract.objectives[0].required is True
    assert selector.dimension == "method"
    assert selector.quantifier is ObjectiveSelectorQuantifier.ALL_LISTED
    assert selector.resolution is ObjectiveSelectorResolution.SEALED
    assert selector.values == (
        "prompt injection",
        "role spoofing",
        "judge manipulation",
        "game-logic exploits",
    )


def test_compiler_preserves_explicit_method_list_rewritten_with_test() -> None:
    source = (
        "Test prompt injection, role spoofing, judge manipulation, and game-logic exploits "
        "that attempt to force wins, bypass character progression, or bypass session limits."
    )

    contract = compile_objective_contract(source)

    assert len(contract.objectives) == 1
    assert contract.objectives[0].required is True
    selector = contract.objectives[0].selectors[0]
    assert selector.dimension == "method"
    assert selector.values == (
        "prompt injection",
        "role spoofing",
        "judge manipulation",
        "game-logic exploits",
    )


def test_compiler_preserves_attempted_method_list() -> None:
    source = (
        "Attempted prompt injection, role spoofing, judge manipulation, and game-logic exploits."
    )

    contract = compile_objective_contract(source)

    assert contract.objectives[0].required is True
    assert contract.objectives[0].selectors[0].values == (
        "prompt injection",
        "role spoofing",
        "judge manipulation",
        "game-logic exploits",
    )


def test_compiler_does_not_fabricate_list_from_ordinary_workflow_prose() -> None:
    source = "Assess the app, authenticate, learn rules, then report findings."

    contract = compile_objective_contract(source)

    assert contract.objectives == ()


def test_compiler_preserves_exact_requirements_with_open_world_completion_gate() -> None:
    source = (
        "Test all four tiers, Tier 1 through Tier 4, including every available character "
        "in each tier. Try prompt injection, role spoofing, judge manipulation, and "
        "game-logic exploits to force wins or bypass progression."
    )

    contract = compile_objective_contract(source)

    assert len(contract.objectives) == 3
    assert [objective.required for objective in contract.objectives] == [True, True, False]
    assert contract.objectives[0].selectors[0].values == ("1", "2", "3", "4")
    assert contract.objectives[2].selectors[0].values == (
        "prompt injection",
        "role spoofing",
        "judge manipulation",
        "game-logic exploits",
    )


def test_compiler_seals_enumerated_range_and_stops_method_list_before_in_order_to() -> None:
    source = (
        "Play normally across all four tiers (Tier 1, Tier 2, Tier 3, Tier 4), "
        "including every available character in each tier. Then attempt prompt injection, "
        "role spoofing, judge manipulation, and game-logic exploits in order to force wins."
    )

    contract = compile_objective_contract(source)

    assert len(contract.objectives) == 3
    tier = contract.objectives[0].selectors[0]
    assert tier.dimension == "tier"
    assert tier.values == ("1", "2", "3", "4")
    character = contract.objectives[1].selectors[0]
    assert character.scope_values == ("1", "2", "3", "4")
    assert contract.objectives[2].selectors[0].values == (
        "prompt injection",
        "role spoofing",
        "judge manipulation",
        "game-logic exploits",
    )


def test_all_available_requirement_cannot_be_satisfied_before_denominator_binding() -> None:
    contract = compile_objective_contract("Test every available character in each tier.")
    objective = contract.objectives[0]

    assert objective.required is True
    with pytest.raises(ValidationError, match="finite denominator"):
        ObjectiveProgress(
            objective=objective,
            status="satisfied_by_campaign",
            evidence_refs=("evidence:coverage",),
        )

    incomplete = project_campaign_outcome(
        (ObjectiveProgress(objective=objective, status="unmet"),),
        requested_end_reason=CampaignEndReason.PROFILE_EXHAUSTED,
    )
    assert incomplete.lifecycle is CampaignLifecycle.INCOMPLETE
    assert incomplete.unresolved_objective_ids == (objective.id,)


def test_authoritative_binding_seals_every_scoped_all_available_denominator() -> None:
    source = "Cover tier 1 through tier 2 and every available character in each tier."
    contract = compile_objective_contract(source)
    objective = contract.objectives[1]
    selector = resolve_all_available_selector(
        objective.selectors[0],
        candidate_ids_by_scope={
            "1": ("character/a", "character/b"),
            "2": ("character/c",),
        },
        source=ObjectiveSelectorBindingSource(
            kind="reviewed_adapter",
            source_ref="adapter:reviewed/v1",
            evidence_refs=("evidence:adapter-character-domain",),
        ),
    )
    bound_objective = objective.model_copy(update={"selectors": (selector,)})
    progress = ObjectiveProgress(
        objective=bound_objective,
        status="satisfied_by_campaign",
        evidence_refs=("evidence:coverage",),
    )

    assert selector.has_finite_denominator() is True
    assert tuple(binding.selector_value for binding in selector.bindings) == ("1", "2")
    assert progress.status is ObjectiveStatus.SATISFIED_BY_CAMPAIGN


def test_binding_rejects_partial_or_unexpected_open_world_scope() -> None:
    source = "Cover tier 1 through tier 2 and every available character in each tier."
    selector = compile_objective_contract(source).objectives[1].selectors[0]
    authority = ObjectiveSelectorBindingSource(
        kind="authoritative_discovery",
        source_ref="discovery:target-domain",
        evidence_refs=("evidence:discovery",),
    )

    with pytest.raises(ValueError, match="missing scopes: 2"):
        resolve_all_available_selector(
            selector,
            candidate_ids_by_scope={"1": ("character/a",)},
            source=authority,
        )
    with pytest.raises(ValueError, match="unexpected scopes: 3"):
        resolve_all_available_selector(
            selector,
            candidate_ids_by_scope={
                "1": ("character/a",),
                "2": ("character/b",),
                "3": ("character/c",),
            },
            source=authority,
        )


def test_authoritative_empty_denominator_is_finite_and_auditable() -> None:
    objective = compile_objective_contract("Test every available integration.").objectives[0]
    selector = resolve_all_available_selector(
        objective.selectors[0],
        candidate_ids_by_scope={"*": ()},
        source=ObjectiveSelectorBindingSource(
            kind="authoritative_discovery",
            source_ref="discovery:no-integrations",
            evidence_refs=("evidence:no-integrations",),
        ),
    )

    assert selector.has_finite_denominator() is True
    assert selector.bindings[0].candidate_ids == ()


def test_authoritative_discovery_can_bind_previously_unknown_scopes() -> None:
    objective = compile_objective_contract(
        "Test every available character in each tier."
    ).objectives[0]
    selector = resolve_all_available_selector(
        objective.selectors[0],
        candidate_ids_by_scope={
            "beginner": ("character/a",),
            "advanced": ("character/b", "character/c"),
        },
        source=ObjectiveSelectorBindingSource(
            kind="authoritative_discovery",
            source_ref="discovery:tier-character-domain",
            evidence_refs=("evidence:tier-character-domain",),
        ),
    )

    assert selector.has_finite_denominator() is True
    assert tuple(binding.selector_value for binding in selector.bindings) == (
        "beginner",
        "advanced",
    )
