from __future__ import annotations

from merlin_agents.completion_frontier import (
    CompletionCandidateIdentity,
    CompletionScope,
    FrontierState,
    VerifiedCandidateOutcome,
)
from merlin_agents.completion_frontier import project_completion_frontier as project_frontier

SCOPE = CompletionScope(
    campaign_id="campaign/one",
    target_id="target/one",
    target_generation="generation/one",
    scenario_fingerprint="sha256:scenario",
)


def _candidate(technique: str, **axes: str) -> dict[str, str]:
    return {
        "campaign_id": SCOPE.campaign_id,
        "target_id": SCOPE.target_id,
        "target_generation": SCOPE.target_generation,
        "scenario_fingerprint": SCOPE.scenario_fingerprint,
        "technique_id": technique,
        "controller_id": axes.get("controller_id", "direct"),
        "payload_family": axes.get("payload_family", "baseline"),
        "requested_language": axes.get("requested_language", "en"),
        "mutation_id": axes.get("mutation_id", "baseline"),
    }


def _verified_target(identity: dict[str, str], outcome: str) -> VerifiedCandidateOutcome:
    return VerifiedCandidateOutcome(
        identity=CompletionCandidateIdentity(**identity),
        outcome=outcome,
        authority_kind="target",
        source_evidence_refs=("target-call:one",),
        verifier_record_id="canonical-outcome-ledger:one",
    )


def test_projection_unions_sources_and_retires_only_authoritative_exact_outcomes() -> None:
    exact = _candidate("catalogue/override", mutation_id="rot13")
    other_language = _candidate(
        "catalogue/override", requested_language="lzh", mutation_id="parseltongue"
    )

    frontier = project_frontier(
        scope=SCOPE,
        manifest_catalogue_cells=(exact, other_language, _candidate("catalogue/retrieval")),
        arm_arms=({**exact, "status": "retired"},),
        candidate_outcomes=(_verified_target(exact, "refuted"),),
        arm_observations=(
            {
                "identity": {
                    "surface_id": "browser:chat",
                    "technique_id": "catalogue/retrieval",
                    "controller_id": "direct",
                    "mutation_id": "baseline",
                    "actual_language": "en",
                    "state_id": "fresh",
                },
                "outcome": "timeout",
                "infrastructure_failure": True,
            },
        ),
        hypotheses=(
            {
                "id": "hypothesis/untested",
                "campaign_id": SCOPE.campaign_id,
                "target_id": SCOPE.target_id,
                "target_generation": SCOPE.target_generation,
                "scenario_fingerprint": SCOPE.scenario_fingerprint,
                "state": "untested",
            },
            {
                "id": "hypothesis/retry",
                "campaign_id": SCOPE.campaign_id,
                "target_id": SCOPE.target_id,
                "target_generation": SCOPE.target_generation,
                "scenario_fingerprint": SCOPE.scenario_fingerprint,
                "state": "blocked_by_infrastructure",
            },
        ),
        legacy_arm_inputs_are_scope_bound=True,
    )

    retired = frontier.with_state(FrontierState.RETIRED)
    runnable = frontier.with_state(FrontierState.RUNNABLE)
    unresolved = frontier.with_state(FrontierState.UNRESOLVED)
    assert [item.identity["mutation_id"] for item in retired] == ["rot13"]
    assert any(item.identity.get("requested_language") == "lzh" for item in runnable)
    assert any(item.identity.get("technique_id") == "catalogue/retrieval" for item in runnable)
    assert [item.identity["hypothesis_id"] for item in unresolved] == ["hypothesis/untested"]
    assert any(item.identity.get("hypothesis_id") == "hypothesis/retry" for item in runnable)
    exact_entry = retired[0]
    assert exact_entry.sources == ("arm_pool", "candidate_outcome", "manifest_catalogue")
    assert frontier.may_complete is False


def test_recon_applicability_is_advisory_and_cannot_remove_catalogue_work() -> None:
    candidate = _candidate("catalogue/unknown-surface")
    baseline = project_frontier(scope=SCOPE, manifest_catalogue_cells=(candidate,))
    contradicted = project_frontier(
        scope=SCOPE,
        manifest_catalogue_cells=(candidate,),
        recon_applicability=(
            {
                "technique_id": candidate["technique_id"],
                "applicable": False,
                "reason": "not advertised",
            },
        ),
    )

    assert contradicted.entries == baseline.entries
    assert contradicted.advisory_recon_records == 1
    assert contradicted.entries[0].state is FrontierState.RUNNABLE


def test_every_durable_source_rejects_explicit_foreign_scope() -> None:
    foreign = {**_candidate("catalogue/foreign"), "target_id": "target/other"}
    foreign_observation = {
        "identity": {
            **foreign,
            "surface_id": "browser:chat",
            "actual_language": foreign["requested_language"],
            "state_id": "fresh",
        },
        "outcome": "timeout",
        "infrastructure_failure": True,
    }
    frontier = project_frontier(
        scope=SCOPE,
        manifest_catalogue_cells=(foreign,),
        arm_arms=(foreign,),
        arm_observations=(foreign_observation,),
        legacy_arm_inputs_are_scope_bound=True,
    )

    assert frontier.entries == ()
    assert frontier.ignored_out_of_scope_records == 3


def test_unscoped_legacy_arm_input_requires_explicit_store_scope_attestation() -> None:
    legacy = {
        "surface_id": "browser:chat",
        "technique_id": "catalogue/legacy-arm",
        "controller_id": "direct",
        "mutation_id": "baseline",
        "actual_language": "en",
        "state_id": "fresh",
    }

    rejected = project_frontier(scope=SCOPE, arm_arms=(legacy,))
    accepted = project_frontier(
        scope=SCOPE,
        arm_arms=(legacy,),
        legacy_arm_inputs_are_scope_bound=True,
    )

    assert rejected.entries == ()
    assert rejected.ignored_out_of_scope_records == 1
    assert accepted.entries[0].state is FrontierState.RUNNABLE


def test_foreign_target_generation_hypothesis_cannot_block_current_campaign() -> None:
    frontier = project_frontier(
        scope=SCOPE,
        hypotheses=(
            {
                "id": "hypothesis/stale-generation",
                "campaign_id": SCOPE.campaign_id,
                "target_id": SCOPE.target_id,
                "target_generation": "generation/old",
                "scenario_fingerprint": SCOPE.scenario_fingerprint,
                "state": "in_progress",
            },
        ),
    )

    assert frontier.entries == ()
    assert frontier.ignored_out_of_scope_records == 1


def test_typed_infrastructure_remains_runnable_and_budget_is_not_retirement() -> None:
    retry = _candidate("catalogue/retry")
    capped = _candidate("catalogue/capped")
    hard_guard = _candidate("catalogue/scope")
    frontier = project_frontier(
        scope=SCOPE,
        retryable_failures=(
            {**retry, "failure_kind": "provider_truncated", "status": "failed"},
            {**capped, "failure_kind": "budget", "status": "failed"},
            {
                **hard_guard,
                "failure_kind": "scope",
                "status": "failed",
                "retryable": True,
            },
        ),
    )

    by_technique = {item.identity["technique_id"]: item for item in frontier.entries}
    assert by_technique["catalogue/retry"].state is FrontierState.RUNNABLE
    assert by_technique["catalogue/capped"].state is FrontierState.BUDGET_BLOCKED
    assert by_technique["catalogue/scope"].state is FrontierState.UNRESOLVED
    assert frontier.with_state(FrontierState.RETIRED) == ()


def test_evaluator_disagreement_stays_runnable_and_global_budget_blocks_open_work() -> None:
    candidate = _candidate("catalogue/encoded", mutation_id="rot13")
    frontier = project_frontier(
        scope=SCOPE,
        manifest_catalogue_cells=(candidate,),
        candidate_outcomes=(
            {
                **candidate,
                "outcome": "evaluation_uncertain",
                "reason": "language detector disagreed with validator",
            },
        ),
        campaign_budget_exhausted=True,
    )

    assert frontier.entries[0].state is FrontierState.BUDGET_BLOCKED
    assert frontier.entries[0].outcome == "evaluation_uncertain"
    assert frontier.entries[0].reasons == ("language detector disagreed with validator",)
    assert frontier.may_complete is False


def test_verified_canonical_outcome_retires_without_trusting_a_result_label() -> None:
    candidate = _candidate("catalogue/typed-outcome")
    frontier = project_frontier(
        scope=SCOPE,
        manifest_catalogue_cells=(candidate,),
        candidate_outcomes=(
            {**candidate, "outcome": "confirmed"},
            _verified_target(candidate, "confirmed"),
        ),
    )

    assert frontier.entries[0].state is FrontierState.RETIRED
    assert frontier.may_complete is True


def test_self_consistent_but_unverified_evidence_references_cannot_retire() -> None:
    candidate = _candidate("catalogue/fabricated")
    fabricated = {
        **candidate,
        "outcome": "confirmed",
        "evidence_refs": ["not-a-durable-receipt"],
        "outcome_authority": {
            "kind": "target",
            "outcome": "confirmed",
            "evidence_refs": ["not-a-durable-receipt"],
        },
    }

    frontier = project_frontier(
        scope=SCOPE,
        manifest_catalogue_cells=(candidate,),
        candidate_outcomes=(fabricated,),
    )

    assert frontier.entries[0].state is FrontierState.RUNNABLE
    assert frontier.may_complete is False


def test_unverified_terminal_label_cannot_overwrite_verified_outcome() -> None:
    candidate = _candidate("catalogue/no-overwrite")
    frontier = project_frontier(
        scope=SCOPE,
        candidate_outcomes=(
            _verified_target(candidate, "confirmed"),
            {**candidate, "outcome": "refuted", "reason": "unverified evaluator label"},
        ),
    )

    assert frontier.entries[0].state is FrontierState.RETIRED
    assert frontier.entries[0].outcome == "confirmed"


def test_verified_outcome_rejects_blank_or_duplicate_evidence_references() -> None:
    identity = CompletionCandidateIdentity(**_candidate("catalogue/bad-evidence"))
    for source_refs in (("",), ("receipt:one", "receipt:one")):
        try:
            VerifiedCandidateOutcome(
                identity=identity,
                outcome="confirmed",
                authority_kind="target",
                source_evidence_refs=source_refs,
                verifier_record_id="canonical-outcome-ledger:bad",
            )
        except ValueError as error:
            assert str(error) == (
                "verified candidate outcome requires source evidence and verifier id"
            )
        else:
            raise AssertionError("blank or duplicate evidence references must be rejected")


def test_partial_or_foreign_scoped_evidence_cannot_change_current_frontier() -> None:
    partial = {"technique_id": "catalogue/partial", "outcome": "confirmed"}
    foreign = _verified_target(
        {**_candidate("catalogue/foreign"), "campaign_id": "campaign/other"}, "confirmed"
    )
    frontier = project_frontier(
        scope=SCOPE,
        candidate_outcomes=(partial, foreign),
        retryable_failures=(
            {"technique_id": "catalogue/partial", "failure_kind": "provider_transient"},
        ),
        hypotheses=(
            {"id": "hypothesis/unscoped", "state": "supported"},
            {
                "id": "hypothesis/foreign",
                "campaign_id": "campaign/other",
                "target_id": SCOPE.target_id,
                "target_generation": SCOPE.target_generation,
                "scenario_fingerprint": SCOPE.scenario_fingerprint,
                "state": "supported",
                "state_evidence_refs": ["receipt:one"],
            },
        ),
    )

    assert frontier.entries == ()
    assert frontier.ignored_out_of_scope_records == 5
    assert frontier.may_complete is False


def test_validator_retirement_requires_a_verified_source_and_witness_reference_union() -> None:
    candidate = _candidate("catalogue/validator")
    witness = {
        "digest": "sha256:" + "a" * 64,
        "reference": "merlin-exchange://validator?campaign=one",
        "source": "validator-model",
    }
    invalid = {
        **candidate,
        "outcome": "confirmed",
        "evidence_refs": ["target-call:one", witness["digest"]],
        "outcome_authority": {
            "kind": "validator",
            "outcome": "confirmed",
            "evidence_refs": ["target-call:one", witness["digest"]],
            "source_evidence_refs": ["target-call:one"],
            "validator_evidence_refs": [witness["digest"]],
            "validator_witnesses": [witness],
        },
    }
    valid = VerifiedCandidateOutcome(
        identity=CompletionCandidateIdentity(**candidate),
        outcome="confirmed",
        authority_kind="validator",
        source_evidence_refs=("target-call:one",),
        validator_evidence_refs=(witness["digest"],),
        validator_witness_digests=(witness["digest"],),
        verifier_record_id="canonical-outcome-ledger:validator-one",
    )

    unresolved = project_frontier(scope=SCOPE, candidate_outcomes=(invalid,))
    retired = project_frontier(scope=SCOPE, candidate_outcomes=(invalid, valid))

    assert unresolved.entries[0].state is FrontierState.RUNNABLE
    assert retired.entries[0].state is FrontierState.RETIRED


def test_conflicting_evidence_bound_outcomes_are_evaluation_uncertain() -> None:
    candidate = _candidate("catalogue/conflict")
    frontier = project_frontier(
        scope=SCOPE,
        candidate_outcomes=(
            _verified_target(candidate, "confirmed"),
            VerifiedCandidateOutcome(
                identity=CompletionCandidateIdentity(**candidate),
                outcome="refuted",
                authority_kind="target",
                source_evidence_refs=("target-call:two",),
                verifier_record_id="canonical-outcome-ledger:two",
            ),
        ),
    )

    assert frontier.entries[0].state is FrontierState.UNRESOLVED
    assert frontier.entries[0].outcome == "evaluation_uncertain"
    assert frontier.entries[0].reasons == ("conflicting authoritative candidate outcomes",)


def test_hard_failure_precedence_is_order_independent() -> None:
    candidate = _candidate("catalogue/hard-precedence")
    transient = {**candidate, "failure_kind": "provider_transient", "retryable": True}
    hard = {**candidate, "failure_kind": "state_integrity", "retryable": True}

    for failures in ((transient, hard), (hard, transient)):
        frontier = project_frontier(scope=SCOPE, retryable_failures=failures)
        assert frontier.entries[0].state is FrontierState.UNRESOLVED


def test_hypothesis_terminal_state_requires_durable_state_evidence() -> None:
    base = {
        "id": "hypothesis/terminal",
        "campaign_id": SCOPE.campaign_id,
        "target_id": SCOPE.target_id,
        "target_generation": SCOPE.target_generation,
        "scenario_fingerprint": SCOPE.scenario_fingerprint,
        "state": "falsified",
    }

    unsupported = project_frontier(scope=SCOPE, hypotheses=(base,))
    supported = project_frontier(
        scope=SCOPE,
        hypotheses=({**base, "state_evidence_refs": ["receipt:one"]},),
    )

    assert unsupported.entries[0].state is FrontierState.UNRESOLVED
    assert supported.entries[0].state is FrontierState.RETIRED


def test_empty_frontier_is_not_completion_evidence() -> None:
    assert project_frontier(scope=SCOPE).may_complete is False


def test_retired_hypotheses_alone_cannot_authorize_campaign_completion() -> None:
    frontier = project_frontier(
        scope=SCOPE,
        hypotheses=(
            {
                "id": "hypothesis/only",
                "campaign_id": SCOPE.campaign_id,
                "target_id": SCOPE.target_id,
                "target_generation": SCOPE.target_generation,
                "scenario_fingerprint": SCOPE.scenario_fingerprint,
                "state": "falsified",
                "state_evidence_refs": ["receipt:one"],
            },
        ),
    )

    assert frontier.entries[0].state is FrontierState.RETIRED
    assert frontier.may_complete is False


def _hypothesis(hypothesis_id: str, state: str = "untested", **extra: object) -> dict[str, object]:
    return {
        "id": hypothesis_id,
        "campaign_id": SCOPE.campaign_id,
        "target_id": SCOPE.target_id,
        "target_generation": SCOPE.target_generation,
        "scenario_fingerprint": SCOPE.scenario_fingerprint,
        "state": state,
        **extra,
    }


def test_hypothesis_budget_marks_overflow_as_budget_blocked_limitations() -> None:
    hypotheses = tuple(
        _hypothesis(f"hypothesis/{index:03d}", creating_evidence_refs=("receipt:one",))
        for index in range(101)
    )

    frontier = project_frontier(
        scope=SCOPE,
        manifest_catalogue_cells=(_candidate("catalogue/override"),),
        hypotheses=hypotheses,
        hypothesis_budget=100,
    )

    blocked = tuple(
        item
        for item in frontier.entries
        if item.kind == "hypothesis" and item.state is FrontierState.BUDGET_BLOCKED
    )
    assert len(blocked) == 1
    assert blocked[0].identity["hypothesis_id"] == "hypothesis/000"
    assert any(reason.startswith("hypothesis_budget_capped") for reason in blocked[0].reasons)
    unresolved_hypotheses = tuple(
        item for item in frontier.with_state(FrontierState.UNRESOLVED) if item.kind == "hypothesis"
    )
    assert len(unresolved_hypotheses) == 100
    assert frontier.may_complete is False

    uncapped = project_frontier(
        scope=SCOPE,
        manifest_catalogue_cells=(_candidate("catalogue/override"),),
        hypotheses=hypotheses,
    )
    assert not any(
        item.kind == "hypothesis" and item.state is FrontierState.BUDGET_BLOCKED
        for item in uncapped.entries
    )


def test_hypothesis_budget_frees_retired_hypotheses_and_prefers_evidence() -> None:
    retired = tuple(
        _hypothesis(
            f"hypothesis/done-{index:03d}",
            state="falsified",
            state_evidence_refs=("receipt:one",),
        )
        for index in range(99)
    )
    parented = _hypothesis(
        "hypothesis/open-parented",
        parent_hypothesis_id="hypothesis/done-000",
        creating_evidence_refs=("receipt:two",),
    )
    plain = _hypothesis("hypothesis/open-plain")

    frontier = project_frontier(
        scope=SCOPE,
        manifest_catalogue_cells=(_candidate("catalogue/override"),),
        hypotheses=(*retired, plain, parented),
        hypothesis_budget=100,
    )

    blocked = tuple(
        item
        for item in frontier.entries
        if item.kind == "hypothesis" and item.state is FrontierState.BUDGET_BLOCKED
    )
    assert [item.identity["hypothesis_id"] for item in blocked] == ["hypothesis/open-plain"]
    unresolved_hypotheses = tuple(
        item for item in frontier.with_state(FrontierState.UNRESOLVED) if item.kind == "hypothesis"
    )
    assert [item.identity["hypothesis_id"] for item in unresolved_hypotheses] == [
        "hypothesis/open-parented"
    ]
    assert len(frontier.with_state(FrontierState.RETIRED)) == 99


def test_hypothesis_budget_prefers_attempted_over_untested_hypotheses() -> None:
    in_progress = _hypothesis("hypothesis/attempted", state="in_progress")
    untested = _hypothesis("hypothesis/untested", creating_evidence_refs=("receipt:one",))

    frontier = project_frontier(
        scope=SCOPE,
        manifest_catalogue_cells=(_candidate("catalogue/override"),),
        hypotheses=(untested, in_progress),
        hypothesis_budget=1,
    )

    blocked = tuple(
        item
        for item in frontier.entries
        if item.kind == "hypothesis" and item.state is FrontierState.BUDGET_BLOCKED
    )
    assert [item.identity["hypothesis_id"] for item in blocked] == ["hypothesis/untested"]
    runnable = tuple(
        item for item in frontier.with_state(FrontierState.RUNNABLE) if item.kind == "hypothesis"
    )
    assert [item.identity["hypothesis_id"] for item in runnable] == ["hypothesis/attempted"]
