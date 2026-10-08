"""Deterministic policy and preparation projections for exploration campaigns."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from math import ceil
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .specialists import AssignmentTechnique


@dataclass(frozen=True, slots=True)
class ExplorationPolicy:
    """Sealed production policy for target-specific vulnerability research."""

    recon_depth: str = "enhanced"
    # Optional reviewed floor. The shipped policy does not manufacture work
    # when the admitted frontier is empty.
    minimum_hypotheses: int | None = None
    # ``None`` means the production default has no invented hypothesis-count
    # ceiling. A reviewed campaign may still provide an explicit cap as part
    # of its real budget, and callers can pass currently available slots.
    maximum_hypotheses: int | None = None
    hypothesis_attack_fraction: float = 0.5
    # Adaptive search ends on durable progress/stall and budget evidence.  A
    # fixed wave count makes a productive campaign stop merely because it has
    # crossed an orchestration boundary.
    maximum_adaptive_waves: int | None = None
    # Compatibility projection for old manifests. Semantic plateau detection
    # now comes from durable progress evidence, never a wave count.
    quiet_waves_required: int | None = None
    required_roles: tuple[str, ...] = (
        "adversarial-researcher",
        "novelty-critic",
        "experiment-planner",
        "independent-validator",
    )

    def hypothesis_target(
        self,
        taxonomy_seed_count: int,
        *,
        available_slots: int | None = None,
    ) -> int:
        """Derive portfolio capacity from breadth and real available slots."""
        if taxonomy_seed_count < 0:
            raise ValueError("taxonomy seed count cannot be negative")
        if available_slots is not None and (
            isinstance(available_slots, bool)
            or not isinstance(available_slots, int)
            or available_slots < 0
        ):
            raise ValueError("available hypothesis slots must be a nonnegative integer")
        if not 0 < self.hypothesis_attack_fraction < 1:
            raise ValueError("hypothesis attack fraction must be between zero and one")
        proportional = ceil(
            taxonomy_seed_count
            * self.hypothesis_attack_fraction
            / (1.0 - self.hypothesis_attack_fraction)
        )
        target = proportional
        if self.minimum_hypotheses is not None:
            if (
                isinstance(self.minimum_hypotheses, bool)
                or not isinstance(self.minimum_hypotheses, int)
                or self.minimum_hypotheses < 0
            ):
                raise ValueError("explicit minimum hypotheses must be a nonnegative integer")
            target = max(self.minimum_hypotheses, target)
        if self.maximum_hypotheses is not None:
            if (
                isinstance(self.maximum_hypotheses, bool)
                or not isinstance(self.maximum_hypotheses, int)
                or self.maximum_hypotheses < 0
            ):
                raise ValueError("explicit maximum hypotheses must be a nonnegative integer")
            target = min(target, self.maximum_hypotheses)
        if available_slots is not None:
            target = min(target, available_slots)
        return target

    def projection(self) -> dict[str, object]:
        return asdict(self)


EXPLORATION_POLICY = ExplorationPolicy()


CriticVerdict = Literal["accept", "revise", "reject", "abstain"]
CriticConfidence = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class ExplorationCriticDecision:
    """An agent-authored semantic judgment recorded without algorithmic substitution."""

    id: str
    campaign_id: str
    hypothesis_id: str
    verdict: CriticVerdict
    evidence_refs: tuple[str, ...]
    closest_known_technique_id: str
    meaningful_difference_claim: str
    alternative_explanation: str
    falsification_test: str
    rationale: str
    confidence: CriticConfidence
    limitations: tuple[str, ...]
    causal_ambiguity: bool = False
    high_risk_promotion: bool = False

    def __post_init__(self) -> None:
        required = {
            "id": self.id,
            "campaign_id": self.campaign_id,
            "hypothesis_id": self.hypothesis_id,
            "closest_known_technique_id": self.closest_known_technique_id,
            "meaningful_difference_claim": self.meaningful_difference_claim,
            "alternative_explanation": self.alternative_explanation,
            "falsification_test": self.falsification_test,
            "rationale": self.rationale,
        }
        if empty := [name for name, value in required.items() if not value.strip()]:
            raise ValueError(f"critic decision fields must not be empty: {', '.join(empty)}")
        if self.verdict not in {"accept", "revise", "reject", "abstain"}:
            raise ValueError("critic verdict must be accept, revise, reject, or abstain")
        if self.confidence not in {"low", "medium", "high"}:
            raise ValueError("critic confidence must be low, medium, or high")
        if not self.evidence_refs or any(not ref.strip() for ref in self.evidence_refs):
            raise ValueError("critic decision requires non-empty evidence references")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("critic evidence references must be unique")
        if len(self.evidence_refs) > 16 or any(len(ref) > 1_000 for ref in self.evidence_refs):
            raise ValueError("critic decision supports at most 16 bounded evidence references")
        if not self.limitations or any(not item.strip() for item in self.limitations):
            raise ValueError("critic decision requires explicit limitations")
        if len(self.limitations) > 8 or any(len(item) > 1_000 for item in self.limitations):
            raise ValueError("critic decision supports at most 8 bounded limitations")
        if len(set(self.limitations)) != len(self.limitations):
            raise ValueError("critic limitations must be unique")
        if type(self.causal_ambiguity) is not bool or type(self.high_risk_promotion) is not bool:
            raise ValueError("critic decision flags must be booleans")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ExplorationCriticDecision:
        """Reconstruct an untrusted projection using the exact durable schema."""
        expected = {
            "id",
            "campaign_id",
            "hypothesis_id",
            "verdict",
            "evidence_refs",
            "closest_known_technique_id",
            "meaningful_difference_claim",
            "alternative_explanation",
            "falsification_test",
            "rationale",
            "confidence",
            "limitations",
            "causal_ambiguity",
            "high_risk_promotion",
        }
        supplied = set(value)
        if missing := expected - supplied:
            raise ValueError(
                "critic decision is missing required fields: " + ", ".join(sorted(missing))
            )
        if extra := supplied - expected:
            raise ValueError("critic decision has unexpected fields: " + ", ".join(sorted(extra)))
        string_fields = expected - {
            "evidence_refs",
            "limitations",
            "causal_ambiguity",
            "high_risk_promotion",
        }
        if any(not isinstance(value[name], str) for name in string_fields):
            raise ValueError("critic decision scalar fields must be strings")
        for name in ("evidence_refs", "limitations"):
            items = value[name]
            if not isinstance(items, (list, tuple)) or any(
                not isinstance(item, str) for item in items
            ):
                raise ValueError(f"critic decision {name} must be an array of strings")
        return cls(
            id=value["id"],
            campaign_id=value["campaign_id"],
            hypothesis_id=value["hypothesis_id"],
            verdict=value["verdict"],
            evidence_refs=tuple(value["evidence_refs"]),
            closest_known_technique_id=value["closest_known_technique_id"],
            meaningful_difference_claim=value["meaningful_difference_claim"],
            alternative_explanation=value["alternative_explanation"],
            falsification_test=value["falsification_test"],
            rationale=value["rationale"],
            confidence=value["confidence"],
            limitations=tuple(value["limitations"]),
            causal_ambiguity=value["causal_ambiguity"],
            high_risk_promotion=value["high_risk_promotion"],
        )  # type: ignore[arg-type]

    def projection(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExplorationExperimentPlan:
    """A bounded experiment contract derived from an agent-authored critic decision."""

    id: str
    campaign_id: str
    hypothesis_id: str
    critic_decision_id: str
    treatment: str
    control: str
    expected_treatment_observation: str
    expected_control_observation: str
    oracle: str
    safety_constraints: tuple[str, ...]
    cleanup: str
    counterfactual: str | None = None
    confounders: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required = {
            "id": self.id,
            "campaign_id": self.campaign_id,
            "hypothesis_id": self.hypothesis_id,
            "critic_decision_id": self.critic_decision_id,
            "treatment": self.treatment,
            "control": self.control,
            "expected_treatment_observation": self.expected_treatment_observation,
            "expected_control_observation": self.expected_control_observation,
            "oracle": self.oracle,
            "cleanup": self.cleanup,
        }
        if empty := [name for name, value in required.items() if not value.strip()]:
            raise ValueError(f"experiment plan fields must not be empty: {', '.join(empty)}")
        if not self.safety_constraints or any(not item.strip() for item in self.safety_constraints):
            raise ValueError("experiment plan requires explicit safety constraints")

    def validate_against(
        self, decision: ExplorationCriticDecision
    ) -> tuple[Mapping[str, object], ...]:
        """Validate durable identity and report semantic review uncertainty.

        The critic is an evaluator, not an execution admission authority. Its
        verdict and causal-review requests therefore remain observations on an
        otherwise executable plan. Only a plan bound to a different durable
        campaign, hypothesis, or critic decision is structurally invalid.
        """
        if (
            self.campaign_id != decision.campaign_id
            or self.hypothesis_id != decision.hypothesis_id
            or self.critic_decision_id != decision.id
        ):
            raise ValueError("experiment plan does not bind the critic decision")

        uncertainties: list[Mapping[str, object]] = []
        if decision.verdict != "accept":
            uncertainties.append(
                {
                    "kind": "evaluation_uncertain",
                    "source": "exploration_reviewer",
                    "reason": f"independent reviewer returned {decision.verdict}",
                    "confidence": decision.confidence,
                    "retryable": decision.verdict in {"revise", "abstain"},
                }
            )
        if decision.causal_ambiguity or decision.high_risk_promotion:
            if self.counterfactual is None or not self.counterfactual.strip():
                uncertainties.append(
                    {
                        "kind": "evaluation_uncertain",
                        "source": "exploration_reviewer",
                        "reason": (
                            "review requested causal qualification but the plan does not "
                            "declare a counterfactual"
                        ),
                        "confidence": decision.confidence,
                        "retryable": True,
                    }
                )
            if not self.confounders or any(not item.strip() for item in self.confounders):
                uncertainties.append(
                    {
                        "kind": "evaluation_uncertain",
                        "source": "exploration_reviewer",
                        "reason": (
                            "review requested causal qualification but the plan does not "
                            "declare confounders"
                        ),
                        "confidence": decision.confidence,
                        "retryable": True,
                    }
                )
        return tuple(uncertainties)

    def projection(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExplorationSeed:
    technique_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ExplorationSourceSummary:
    source: str
    comparable_campaigns: int
    comparable_attempts: int
    prior_findings: int
    current_negatives_skipped: int
    drifted_or_incomplete: int
    seed_reasons: tuple[ExplorationSeed, ...]
    limitation: str | None

    def projection(self) -> dict[str, object]:
        return {
            **asdict(self),
            "seed_reasons": tuple(asdict(item) for item in self.seed_reasons),
        }


@dataclass(frozen=True, slots=True)
class HeldOutExplorationEvaluation:
    """Comparable discovery lift without exposing held-out cases to campaign planning."""

    held_out_cases: int
    baseline_discoveries: int
    exploration_discoveries: int
    exploration_only_discoveries: int
    missed_cases: int

    @property
    def discovery_lift(self) -> int:
        return self.exploration_discoveries - self.baseline_discoveries


@dataclass(frozen=True, slots=True)
class ExplorationCandidateMeasurements:
    """Recorded facts used to prioritize one typed frontier hypothesis.

    Values are counts or bounded policy classifications, never model confidence estimates.
    """

    target_evidence_count: int
    discriminating_observations: int
    impact_level: int
    novelty_dimensions: int
    history_yield_count: int
    estimated_target_turns: int
    effect_risk_level: int
    redundancy_matches: int
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        counts = (
            self.target_evidence_count,
            self.discriminating_observations,
            self.novelty_dimensions,
            self.history_yield_count,
            self.estimated_target_turns,
            self.redundancy_matches,
        )
        if any(value < 0 for value in counts):
            raise ValueError("exploration candidate measurements cannot be negative")
        if not 0 <= self.impact_level <= 4:
            raise ValueError("impact level must be between 0 and 4")
        if not 0 <= self.effect_risk_level <= 4:
            raise ValueError("effect risk level must be between 0 and 4")
        if len(set(self.evidence_refs)) != len(self.evidence_refs) or any(
            not item.strip() for item in self.evidence_refs
        ):
            raise ValueError("evidence refs must be unique non-empty strings")


@dataclass(frozen=True, slots=True)
class ExplorationCandidate:
    candidate_id: str
    measurements: ExplorationCandidateMeasurements
    relationship: str | None = None
    material_difference: str = ""

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ValueError("exploration candidate id cannot be empty")
        if self.relationship is not None and self.relationship not in NEIGHBOUR_RELATIONSHIPS:
            raise ValueError(
                "exploration candidate relationship must be shared-surface or shared-failure-cause"
            )


NEIGHBOUR_RELATIONSHIPS = ("shared-surface", "shared-failure-cause")


@dataclass(frozen=True, slots=True)
class NeighbourProposal:
    """One observation-derived neighbour proposed from committed target receipts."""

    technique_id: str
    anchor_technique_id: str
    relationship: str
    material_difference: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.technique_id.strip() or not self.anchor_technique_id.strip():
            raise ValueError("neighbour proposal technique ids cannot be empty")
        if self.technique_id == self.anchor_technique_id:
            raise ValueError("neighbour proposal cannot anchor on itself")
        if self.relationship not in NEIGHBOUR_RELATIONSHIPS:
            raise ValueError(
                "neighbour proposal relationship must be shared-surface or shared-failure-cause"
            )
        if not self.material_difference.strip():
            raise ValueError("neighbour proposal requires a material-difference explanation")
        if len(set(self.evidence_refs)) != len(self.evidence_refs) or any(
            not item.strip() for item in self.evidence_refs
        ):
            raise ValueError("neighbour proposal evidence refs must be unique non-empty strings")


def propose_observation_neighbours(
    techniques: tuple[AssignmentTechnique, ...],
    relationships: tuple[Mapping[str, object], ...],
    *,
    attempted_ids: frozenset[str] | tuple[str, ...] = (),
    maximum: int = 6,
) -> tuple[NeighbourProposal, ...]:
    """Propose neighbour cases from receipt-derived relationship records.

    The anchor is the side of a relationship with committed target receipts (an
    already-planned or executed case); the neighbour is proposed only when it is
    not itself planned or executed, so follow-ups come from what the target
    actually did rather than static catalogue metadata alone.
    """
    if maximum < 1:
        raise ValueError("neighbour proposal maximum must be positive")
    by_id = {item.id: item for item in techniques}
    attempted = frozenset(attempted_ids)
    proposals: dict[str, NeighbourProposal] = {}
    ordered = sorted(
        relationships,
        key=lambda item: (str(item.get("kind", "")), str(item.get("id", ""))),
    )
    for record in ordered:
        if len(proposals) >= maximum:
            break
        relationship = str(record.get("kind", ""))
        raw_ids = record.get("case_ids", ())
        if not isinstance(raw_ids, (list, tuple)) or len(raw_ids) != 2:
            raise ValueError("neighbour relationship record requires exactly two case ids")
        case_ids = tuple(sorted(str(item) for item in raw_ids))
        anchors = [case_id for case_id in case_ids if case_id in attempted]
        neighbours = [case_id for case_id in case_ids if case_id not in attempted]
        if len(anchors) != 1 or len(neighbours) != 1:
            continue
        neighbour_id = neighbours[0]
        if neighbour_id not in by_id or neighbour_id in proposals:
            continue
        evidence = record.get("evidence_refs", ())
        if not isinstance(evidence, (list, tuple)):
            raise ValueError("neighbour relationship evidence refs must be an array")
        proposals[neighbour_id] = NeighbourProposal(
            technique_id=neighbour_id,
            anchor_technique_id=anchors[0],
            relationship=relationship,
            material_difference=str(record.get("material_difference", "")),
            evidence_refs=tuple(sorted(str(item) for item in evidence)),
        )
    return tuple(proposals[technique_id] for technique_id in sorted(proposals))


@dataclass(frozen=True, slots=True)
class ExplorationFactorReason:
    factor: str
    measured_value: int
    contribution: int
    basis: str


@dataclass(frozen=True, slots=True)
class RankedExplorationCandidate:
    candidate_id: str
    score: int
    reasons: tuple[ExplorationFactorReason, ...]
    evidence_refs: tuple[str, ...]

    def projection(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "score": self.score,
            "reasons": tuple(asdict(item) for item in self.reasons),
            "evidence_refs": self.evidence_refs,
        }


@dataclass(frozen=True, slots=True)
class ExplorationSelection:
    selected: tuple[RankedExplorationCandidate, ...]
    deferred: tuple[RankedExplorationCandidate, ...]

    def projection(self) -> dict[str, object]:
        return {
            "selected": tuple(item.projection() for item in self.selected),
            "deferred": tuple(item.projection() for item in self.deferred),
        }


def rank_exploration_candidates(
    candidates: tuple[ExplorationCandidate, ...],
) -> tuple[RankedExplorationCandidate, ...]:
    """Rank typed hypotheses from recorded factors with an inspectable score breakdown."""
    if len({item.candidate_id for item in candidates}) != len(candidates):
        raise ValueError("exploration candidate ids must be unique")
    for candidate in candidates:
        if candidate.relationship is not None and not candidate.material_difference.strip():
            raise ValueError(
                "observation-derived neighbour candidates require a material-difference "
                "explanation against the closest planned or executed case"
            )
    ranked = []
    for candidate in candidates:
        measured = candidate.measurements
        factors = (
            ExplorationFactorReason(
                "target_evidence",
                measured.target_evidence_count,
                min(4, measured.target_evidence_count) * 5,
                "attributable current-target evidence records (capped at 4)",
            ),
            ExplorationFactorReason(
                "discriminative_power",
                measured.discriminating_observations,
                min(4, measured.discriminating_observations) * 5,
                "observations that distinguish competing explanations (capped at 4)",
            ),
            ExplorationFactorReason(
                "impact",
                measured.impact_level,
                measured.impact_level * 4,
                "bounded effect classification: 0 none through 4 critical",
            ),
            ExplorationFactorReason(
                "novelty",
                measured.novelty_dimensions,
                min(4, measured.novelty_dimensions) * 3,
                "target dimensions absent from attempted hypotheses (capped at 4)",
            ),
            ExplorationFactorReason(
                "history_yield",
                measured.history_yield_count,
                min(4, measured.history_yield_count) * 2,
                "durable prior findings on explicitly related surfaces (capped at 4)",
            ),
            ExplorationFactorReason(
                "cost",
                measured.estimated_target_turns,
                -min(6, (measured.estimated_target_turns + 1) // 2) * 2,
                "reviewed target-turn estimate, penalized in two-turn bands",
            ),
            ExplorationFactorReason(
                "risk",
                measured.effect_risk_level,
                -measured.effect_risk_level * 4,
                "bounded target-effect risk: 0 read-only through 4 destructive",
            ),
            ExplorationFactorReason(
                "redundancy",
                measured.redundancy_matches,
                -min(4, measured.redundancy_matches) * 5,
                "materially overlapping attempted hypotheses (capped at 4)",
            ),
        )
        ranked.append(
            RankedExplorationCandidate(
                candidate_id=candidate.candidate_id,
                score=sum(item.contribution for item in factors),
                reasons=factors,
                evidence_refs=tuple(sorted(measured.evidence_refs)),
            )
        )
    return tuple(
        sorted(
            ranked,
            key=lambda item: (
                -item.score,
                -item.reasons[1].measured_value,
                -item.reasons[0].measured_value,
                item.candidate_id,
            ),
        )
    )


def select_exploration_candidates(
    candidates: tuple[ExplorationCandidate, ...], *, maximum: int
) -> ExplorationSelection:
    """Select a bounded frontier while retaining explanations for deferred candidates."""
    if maximum < 1:
        raise ValueError("exploration candidate maximum must be positive")
    ranked = rank_exploration_candidates(candidates)
    return ExplorationSelection(selected=ranked[:maximum], deferred=ranked[maximum:])


@dataclass(frozen=True, slots=True)
class ResearcherPolicyContext:
    ambiguous_observations: int
    high_value_surfaces: int
    remaining_hypotheses: int
    required_hypotheses: int
    available_independent_slots: int

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.ambiguous_observations,
                self.high_value_surfaces,
                self.remaining_hypotheses,
                self.required_hypotheses,
                self.available_independent_slots,
            )
        ):
            raise ValueError("researcher policy measurements cannot be negative")


@dataclass(frozen=True, slots=True)
class ResearcherAllocation:
    researcher_count: int
    reasons: tuple[str, ...]

    def projection(self) -> dict[str, object]:
        return asdict(self)


def allocate_independent_researchers(context: ResearcherPolicyContext) -> ResearcherAllocation:
    """Allocate at most three researchers only at measured decision boundaries."""
    if context.available_independent_slots == 0:
        return ResearcherAllocation(0, ("no independent researcher slots are available",))
    requested = 1
    reasons = ["one bounded researcher is the default"]
    if context.ambiguous_observations >= 2:
        requested += 1
        reasons.append("multiple recorded observations support competing explanations")
    if context.high_value_surfaces >= 1:
        requested += 1
        reasons.append("a reviewed high-value surface warrants independent challenge")
    near_saturation_threshold = max(1, context.required_hypotheses // 4)
    if (
        context.required_hypotheses > 0
        and 0 < context.remaining_hypotheses <= near_saturation_threshold
    ):
        requested += 1
        reasons.append("the measured frontier is near saturation and needs an independent pass")
    allocated = min(3, context.available_independent_slots, requested)
    if allocated < requested:
        reasons.append("allocation was capped by policy or available independent slots")
    return ResearcherAllocation(allocated, tuple(reasons))


def evaluate_held_out_exploration(
    held_out_case_ids: frozenset[str],
    *,
    baseline_finding_ids: frozenset[str],
    exploration_finding_ids: frozenset[str],
) -> HeldOutExplorationEvaluation:
    """Score sealed outputs after execution; held-out IDs are never planner inputs."""
    baseline = held_out_case_ids & baseline_finding_ids
    exploration = held_out_case_ids & exploration_finding_ids
    return HeldOutExplorationEvaluation(
        held_out_cases=len(held_out_case_ids),
        baseline_discoveries=len(baseline),
        exploration_discoveries=len(exploration),
        exploration_only_discoveries=len(exploration - baseline),
        missed_cases=len(held_out_case_ids - exploration),
    )


@dataclass(frozen=True, slots=True)
class ExplorationArmSelection:
    """Selected arms plus complete novelty and ranking audit evidence."""

    selected: tuple[Mapping[str, object], ...]
    rankings: tuple[Mapping[str, object], ...]
    novelty_decisions: tuple[Mapping[str, object], ...]
    deferred_keys: tuple[str, ...]

    def projection(self) -> dict[str, object]:
        return asdict(self)


def select_exploration_arms(
    arms: tuple[Mapping[str, object], ...],
    history: tuple[Mapping[str, object], ...],
    *,
    existing_objectives: tuple[str, ...] = (),
    maximum: int = 6,
) -> ExplorationArmSelection:
    """Record novelty, rank full-context arms, and exclude evidence-retired arms.

    This is the live-selector integration surface. The caller must materialize
    actual delivery language and current target state before invoking it; this
    function intentionally has no requested-language or state fallback.
    """
    from .arm_pool import novel_arms, rank_pool

    if maximum < 1:
        raise ValueError("exploration arm maximum must be positive")
    novelty = novel_arms(arms, existing_objectives)
    admitted = tuple(
        {**item.arm, "novelty": 1.0 - item.similarity} for item in novelty if item.admitted
    )
    ranked = rank_pool(admitted, history)
    selected = ranked[:maximum]
    return ExplorationArmSelection(
        selected=tuple(item.arm for item in selected),
        rankings=tuple(item.ranking.projection() for item in ranked),
        novelty_decisions=tuple(item.projection() for item in novelty),
        deferred_keys=tuple(item.ranking.key for item in ranked[maximum:]),
    )


def select_exploration_frontier(
    arms: tuple[Mapping[str, object], ...],
    history: tuple[Mapping[str, object], ...],
    *,
    existing_objectives: tuple[str, ...] = (),
    maximum: int = 6,
) -> ExplorationArmSelection:
    """Rank existing execution arms and unresolved proposals as one frontier.

    Unlike ``select_exploration_arms``, this integration surface accepts newly
    admitted proposals before their first delivery receipt. Requested language
    remains a proposal dimension; it is never relabelled as observed language.
    Slicing happens only after every unresolved candidate has a comparable
    ranking record.
    """
    from .arm_pool import novel_arms, rank_frontier

    if maximum < 1:
        raise ValueError("exploration arm maximum must be positive")
    novelty = novel_arms(arms, existing_objectives)
    admitted = tuple(
        {
            **item.arm,
            "novelty": (
                item.arm["novelty"]
                if isinstance(item.arm.get("novelty"), (int, float))
                and not isinstance(item.arm.get("novelty"), bool)
                else 1.0 - item.similarity
            ),
        }
        for item in novelty
        if item.admitted
    )
    ranked = rank_frontier(admitted, history)
    selected = ranked[:maximum]
    return ExplorationArmSelection(
        selected=tuple(item.arm for item in selected),
        rankings=tuple(item.projection() for item in ranked),
        novelty_decisions=tuple(item.projection() for item in novelty),
        deferred_keys=tuple(item.key for item in ranked[maximum:]),
    )


def select_exploration_seeds(
    arms: tuple[Mapping[str, object], ...],
    history: tuple[Mapping[str, object], ...],
    *,
    existing_objectives: tuple[str, ...] = (),
    maximum: int = 6,
) -> tuple[ExplorationSeed, ...]:
    """Project canonical ranked arms into campaign seed references."""
    selection = select_exploration_arms(
        arms, history, existing_objectives=existing_objectives, maximum=maximum
    )
    rankings = {str(item["key"]): item for item in selection.rankings}
    seeds: list[ExplorationSeed] = []
    from .discovery import ArmIdentity

    for arm in selection.selected:
        identity = ArmIdentity.from_mapping(arm)
        ranking = rankings[identity.key]
        components = ranking["components"]
        strongest = max(components, key=lambda item: abs(float(item["contribution"])))
        seeds.append(
            ExplorationSeed(
                identity.technique_id,
                f"bandit:{strongest['name']}={float(strongest['contribution']):.4f}; "
                f"score={float(ranking['score']):.4f}; {ranking['lifecycle_reason']}",
            )
        )
    return tuple(seeds)


def exploration_source_summary(
    attempts: tuple[Mapping[str, object], ...],
    decisions: tuple[Mapping[str, object], ...],
    seeds: tuple[ExplorationSeed, ...],
) -> ExplorationSourceSummary:
    campaign_ids = {
        str(item.get("campaign_id")) for item in attempts if item.get("campaign_id") is not None
    }
    comparable_attempts = len(attempts)
    source = "history_guided" if comparable_attempts else "cold_start"
    return ExplorationSourceSummary(
        source=source,
        comparable_campaigns=len(campaign_ids),
        comparable_attempts=comparable_attempts,
        prior_findings=sum(item.get("outcome") == "finding" for item in attempts),
        current_negatives_skipped=sum(
            str(item.get("decision", "")) == "historical_exact_match" for item in decisions
        ),
        drifted_or_incomplete=sum(
            str(item.get("decision", "")).startswith("revalidate_") for item in decisions
        ),
        seed_reasons=seeds,
        limitation=(
            "No comparable campaign evidence; exploration relies on current reconnaissance and "
            "may be less efficient."
            if source == "cold_start"
            else None
        ),
    )
