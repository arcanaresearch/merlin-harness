import pytest
from merlin_agents.discovery import ArmIdentity, ArmObservation
from merlin_agents.exploration import (
    EXPLORATION_POLICY,
    ExplorationCandidate,
    ExplorationCandidateMeasurements,
    ExplorationCriticDecision,
    ExplorationExperimentPlan,
    ExplorationSeed,
    ResearcherPolicyContext,
    allocate_independent_researchers,
    evaluate_held_out_exploration,
    exploration_source_summary,
    select_exploration_candidates,
    select_exploration_seeds,
)
from merlin_agents.specialists import AssignmentTechnique
from merlin_contracts import AttemptDisposition, OutcomeAuthority


def _technique(identifier: str, *tags: str) -> AssignmentTechnique:
    return AssignmentTechnique(identifier, identifier, frozenset(tags), ())


def _execution_arm(identifier: str, objective: str | None = None) -> dict[str, object]:
    return {
        **ArmIdentity(
            "browser:chat", identifier, "direct", "baseline", "en", "new-session"
        ).projection(),
        "objective": objective or identifier,
        "novelty": 0.5,
        "generalization_opportunity": 0.0,
        "boundary_opportunity": 0.0,
        "status": "trial",
    }


def test_exploration_policy_reserves_at_least_half_of_attack_work() -> None:
    assert EXPLORATION_POLICY.hypothesis_target(0) == 0
    assert EXPLORATION_POLICY.hypothesis_target(4) == 4
    assert EXPLORATION_POLICY.hypothesis_target(99) == 99
    assert EXPLORATION_POLICY.hypothesis_target(99, available_slots=7) == 7
    assert EXPLORATION_POLICY.hypothesis_target(0, available_slots=0) == 0
    with pytest.raises(ValueError, match="available hypothesis slots"):
        EXPLORATION_POLICY.hypothesis_target(1, available_slots=-1)
    with pytest.raises(ValueError, match="available hypothesis slots"):
        EXPLORATION_POLICY.hypothesis_target(1, available_slots=True)


def test_seed_selection_is_small_deterministic_and_reasoned() -> None:
    arms = tuple(_execution_arm(identifier) for identifier in ("state", "direct", "control"))
    seeds = select_exploration_seeds(arms, ())
    assert {seed.technique_id for seed in seeds} == {"state", "direct", "control"}
    assert all(seed.reason.startswith("bandit:") for seed in seeds)


def test_source_summary_distinguishes_cold_start_and_history() -> None:
    cold = exploration_source_summary((), (), (ExplorationSeed("control", "control"),))
    assert cold.source == "cold_start"
    assert cold.limitation
    history = exploration_source_summary(
        (
            {"campaign_id": "one", "outcome": "finding"},
            {"campaign_id": "one", "outcome": "negative_result"},
        ),
        ({"decision": "historical_exact_match"},),
        (),
    )
    assert history.source == "history_guided"
    assert history.comparable_campaigns == 1
    assert history.prior_findings == 1
    assert history.current_negatives_skipped == 1


def test_held_out_evaluation_scores_discovery_lift_after_execution() -> None:
    evaluation = evaluate_held_out_exploration(
        frozenset({"case:a", "case:b", "case:c"}),
        baseline_finding_ids=frozenset({"case:a", "not-held-out"}),
        exploration_finding_ids=frozenset({"case:a", "case:b"}),
    )

    assert evaluation.held_out_cases == 3
    assert evaluation.baseline_discoveries == 1
    assert evaluation.exploration_discoveries == 2
    assert evaluation.exploration_only_discoveries == 1
    assert evaluation.missed_cases == 1
    assert evaluation.discovery_lift == 1


def _candidate(identifier: str, **overrides: object) -> ExplorationCandidate:
    values = {
        "target_evidence_count": 1,
        "discriminating_observations": 1,
        "impact_level": 1,
        "novelty_dimensions": 1,
        "history_yield_count": 0,
        "estimated_target_turns": 2,
        "effect_risk_level": 0,
        "redundancy_matches": 0,
        "evidence_refs": (f"evidence:{identifier}",),
    }
    values.update(overrides)
    measurements = ExplorationCandidateMeasurements(**values)  # type: ignore[arg-type]
    return ExplorationCandidate(identifier, measurements)


def test_frontier_selection_ranks_recorded_factors_and_exposes_breakdown() -> None:
    selection = select_exploration_candidates(
        (
            _candidate("weak", redundancy_matches=2, estimated_target_turns=6),
            _candidate(
                "strong",
                target_evidence_count=3,
                discriminating_observations=3,
                impact_level=3,
                novelty_dimensions=2,
            ),
        ),
        maximum=1,
    )

    assert [item.candidate_id for item in selection.selected] == ["strong"]
    assert [item.candidate_id for item in selection.deferred] == ["weak"]
    reasons = {item.factor: item for item in selection.selected[0].reasons}
    assert set(reasons) == {
        "target_evidence",
        "discriminative_power",
        "impact",
        "novelty",
        "history_yield",
        "cost",
        "risk",
        "redundancy",
    }
    assert reasons["target_evidence"].contribution == 15
    assert reasons["cost"].contribution == -2


def test_frontier_ranking_uses_stable_id_only_after_measured_ties() -> None:
    selection = select_exploration_candidates(
        (_candidate("z-last"), _candidate("a-first")), maximum=2
    )

    assert [item.candidate_id for item in selection.selected] == ["a-first", "z-last"]


def test_researcher_policy_defaults_to_one_and_is_bounded_by_slots() -> None:
    default = allocate_independent_researchers(ResearcherPolicyContext(0, 0, 8, 12, 3))
    bounded = allocate_independent_researchers(ResearcherPolicyContext(3, 1, 1, 8, 2))

    assert default.researcher_count == 1
    assert bounded.researcher_count == 2
    assert "capped" in bounded.reasons[-1]


def test_researcher_policy_adds_independence_only_for_recorded_triggers() -> None:
    ambiguous = allocate_independent_researchers(ResearcherPolicyContext(2, 0, 8, 12, 3))
    high_value = allocate_independent_researchers(ResearcherPolicyContext(0, 1, 8, 12, 3))
    near_saturation = allocate_independent_researchers(ResearcherPolicyContext(0, 0, 2, 8, 3))

    assert ambiguous.researcher_count == 2
    assert high_value.researcher_count == 2
    assert near_saturation.researcher_count == 2


def _critic(**changes: object) -> ExplorationCriticDecision:
    values = {
        "id": "critic:1",
        "campaign_id": "campaign/1",
        "hypothesis_id": "hypothesis:1",
        "verdict": "accept",
        "evidence_refs": ("evidence:recon",),
        "closest_known_technique_id": "catalogue:session-confusion",
        "meaningful_difference_claim": "The target binds authorization to a second object.",
        "alternative_explanation": "The observed difference may be normal cache isolation.",
        "falsification_test": "Repeat with a server-issued identifier and a fresh browser context.",
        "rationale": "The proposal is distinct enough to test, but is not yet a finding.",
        "confidence": "medium",
        "limitations": ("The behavior has not yet been reproduced.",),
    }
    values.update(changes)
    return ExplorationCriticDecision(**values)  # type: ignore[arg-type]


def _plan(**changes: object) -> ExplorationExperimentPlan:
    values = {
        "id": "experiment:1",
        "campaign_id": "campaign/1",
        "hypothesis_id": "hypothesis:1",
        "critic_decision_id": "critic:1",
        "treatment": "Replay the reviewed request with the alternate object identifier.",
        "control": "Replay with the original server-issued identifier.",
        "expected_treatment_observation": "The alternate object's data is returned.",
        "expected_control_observation": "Only the original object's data is returned.",
        "oracle": "Only the treatment crosses the reviewed identity boundary.",
        "safety_constraints": ("Read-only request", "One alternate identifier"),
        "cleanup": "Release both reviewed browser contexts.",
    }
    values.update(changes)
    return ExplorationExperimentPlan(**values)  # type: ignore[arg-type]


def test_critic_verdict_is_typed_and_requires_semantic_evidence_fields() -> None:
    assert _critic(verdict="reject").projection()["verdict"] == "reject"
    assert _critic(verdict="revise").verdict == "revise"
    assert _critic(verdict="abstain").verdict == "abstain"
    with pytest.raises(ValueError, match="accept, revise, reject, or abstain"):
        _critic(verdict="novel")
    with pytest.raises(ValueError, match="evidence references"):
        _critic(evidence_refs=())
    with pytest.raises(ValueError, match="confidence"):
        _critic(confidence="certain")
    with pytest.raises(ValueError, match="limitations"):
        _critic(limitations=())


def test_experiment_contract_always_requires_treatment_control_oracle_safety_cleanup() -> None:
    decision = _critic()
    plan = _plan()
    plan.validate_against(decision)
    for field in (
        "treatment",
        "control",
        "expected_treatment_observation",
        "expected_control_observation",
        "oracle",
        "cleanup",
    ):
        with pytest.raises(ValueError, match="must not be empty"):
            _plan(**{field: ""})
    with pytest.raises(ValueError, match="safety constraints"):
        _plan(safety_constraints=())


def test_counterfactual_and_confounders_are_conditional_on_critic_judgment() -> None:
    assert _plan().validate_against(_critic()) == ()
    ambiguous = _critic(causal_ambiguity=True)
    uncertainties = _plan().validate_against(ambiguous)
    assert [item["kind"] for item in uncertainties] == [
        "evaluation_uncertain",
        "evaluation_uncertain",
    ]
    assert "counterfactual" in str(uncertainties[0]["reason"])
    assert "confounders" in str(uncertainties[1]["reason"])
    assert (
        len(
            _plan(counterfactual="The control should remain unchanged.").validate_against(ambiguous)
        )
        == 1
    )
    assert (
        _plan(
            counterfactual="The control should remain unchanged.",
            confounders=("Cache state",),
        ).validate_against(ambiguous)
        == ()
    )


@pytest.mark.parametrize("verdict", ("revise", "reject", "abstain"))
def test_semantic_verdict_is_advisory_evidence_not_an_admission_veto(verdict: str) -> None:
    uncertainties = _plan().validate_against(_critic(verdict=verdict))

    assert uncertainties == (
        {
            "kind": "evaluation_uncertain",
            "source": "exploration_reviewer",
            "reason": f"independent reviewer returned {verdict}",
            "confidence": "medium",
            "retryable": verdict in {"revise", "abstain"},
        },
    )


def test_experiment_plan_rejects_only_mismatched_durable_identity() -> None:
    with pytest.raises(ValueError, match="does not bind"):
        _plan().validate_against(_critic(id="critic:other"))


def _neighbour_relationship(
    kind: str, anchor: str, neighbour: str, *, material_difference: str = "differs"
) -> dict[str, object]:
    import hashlib
    import json

    case_ids = tuple(sorted((anchor, neighbour)))
    basis = f"{kind}:basis"
    digest = hashlib.sha256(
        json.dumps(
            {"kind": kind, "case_ids": case_ids, "basis": basis},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "id": f"sha256:{digest}",
        "kind": kind,
        "case_ids": case_ids,
        "basis": basis,
        "material_difference": material_difference,
        "evidence_refs": (f"receipt:{anchor}",),
    }


def test_observation_neighbours_are_proposed_from_receipt_relationships() -> None:
    from merlin_agents.exploration import propose_observation_neighbours

    techniques = (
        _technique("pack/observed", "injection"),
        _technique("pack/neighbour", "session"),
        _technique("pack/unrelated", "other"),
    )
    relationships = (
        _neighbour_relationship("shared-surface", "pack/observed", "pack/neighbour"),
        _neighbour_relationship("shared-failure-cause", "pack/observed", "pack/missing"),
    )

    proposals = propose_observation_neighbours(
        techniques, relationships, attempted_ids=frozenset({"pack/observed"})
    )

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.technique_id == "pack/neighbour"
    assert proposal.anchor_technique_id == "pack/observed"
    assert proposal.relationship == "shared-surface"
    assert proposal.material_difference == "differs"
    assert proposal.evidence_refs == ("receipt:pack/observed",)


def test_observation_neighbours_skip_unanchored_and_attempted_cases() -> None:
    from merlin_agents.exploration import propose_observation_neighbours

    techniques = (_technique("pack/a"), _technique("pack/b"), _technique("pack/c"))
    relationships = (
        _neighbour_relationship("shared-surface", "pack/a", "pack/b"),
        _neighbour_relationship("shared-surface", "pack/b", "pack/c"),
    )

    assert propose_observation_neighbours(techniques, relationships) == ()
    attempted = propose_observation_neighbours(
        techniques, relationships, attempted_ids=frozenset({"pack/a", "pack/b"})
    )
    assert [item.technique_id for item in attempted] == ["pack/c"]


def test_neighbour_proposals_always_require_a_material_difference() -> None:
    from merlin_agents.exploration import NeighbourProposal, propose_observation_neighbours

    with pytest.raises(ValueError, match="material-difference"):
        NeighbourProposal("pack/b", "pack/a", "shared-surface", "  ")
    with pytest.raises(ValueError, match="material-difference"):
        propose_observation_neighbours(
            (_technique("pack/a"), _technique("pack/b")),
            (
                _neighbour_relationship(
                    "shared-surface", "pack/a", "pack/b", material_difference=" "
                ),
            ),
            attempted_ids=frozenset({"pack/a"}),
        )


def test_seed_selection_admits_observation_neighbours() -> None:
    neighbour = _execution_arm("pack/neighbour", "Probe a distinct observed boundary")
    seeds = select_exploration_seeds((neighbour,), ())
    assert seeds[0].technique_id == "pack/neighbour"


def test_admission_rejects_neighbour_candidates_without_material_difference() -> None:
    candidate = ExplorationCandidate(
        "pack/neighbour",
        _candidate("pack/neighbour").measurements,
        relationship="shared-surface",
    )
    with pytest.raises(ValueError, match="material-difference"):
        select_exploration_candidates((candidate,), maximum=1)

    admitted = ExplorationCandidate(
        "pack/neighbour",
        _candidate("pack/neighbour").measurements,
        relationship="shared-failure-cause",
        material_difference="Shares the observed failure signature but targets a distinct surface.",
    )
    selection = select_exploration_candidates((admitted,), maximum=1)
    assert [item.candidate_id for item in selection.selected] == ["pack/neighbour"]
    with pytest.raises(ValueError, match="shared-surface or"):
        ExplorationCandidate(
            "pack/other",
            _candidate("pack/other").measurements,
            relationship="tag-overlap",
        )


def test_seed_selection_retires_dead_arms_with_history() -> None:
    direct, state = _execution_arm("direct"), _execution_arm("state")
    direct_id = ArmIdentity.from_mapping(direct)
    history = tuple(
        ArmObservation(
            direct_id,
            "negative_result",
            attempt_disposition=AttemptDisposition.VALID,
            outcome_authority=OutcomeAuthority.TARGET,
        )
        for _ in range(5)
    )
    seeds = select_exploration_seeds((direct, state), history)
    assert [seed.technique_id for seed in seeds] == ["state", "direct"]
    assert "zero signal lowers priority reversibly" in seeds[1].reason

    reactivated = select_exploration_seeds(({**direct, "status": "retired"},), history)
    assert [seed.technique_id for seed in reactivated] == ["direct"]
    assert "reopened as a reversible trial" in reactivated[0].reason


def test_seed_selection_keeps_distinct_identity_despite_duplicate_wording() -> None:
    control = _execution_arm("control", "Issue a benign instruction.")
    duplicate = _execution_arm("direct", "Issue a benign instruction.")
    seeds = select_exploration_seeds((control, duplicate), ())
    assert {seed.technique_id for seed in seeds} == {"control", "direct"}
