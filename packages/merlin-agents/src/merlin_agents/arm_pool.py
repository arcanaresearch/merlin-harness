"""Admission and ranking for the durable six-dimensional exploration pool."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from math import log, sqrt
from typing import Protocol

from .discovery import (
    DEFAULT_RANKING_POLICY,
    ArmIdentity,
    ArmObservation,
    AttemptStats,
    LifecycleState,
    RankingPolicy,
    ScoreComponent,
    UcbRanking,
    aggregate_observations,
    check_novelty,
    rank_tasks_by_ucb,
    schedule_novelty_shadow,
)


class ArmHistorySource(Protocol):
    """Boundary implemented by a target/scenario-scoped durable store."""

    def list_arm_observations(
        self, target_id: str, scenario_fingerprint: str
    ) -> Sequence[ArmObservation | Mapping[str, object]]: ...


@dataclass(frozen=True, slots=True)
class NoveltyAdmission:
    arm: Mapping[str, object]
    admitted: bool
    verdict: str
    similarity: float
    nearest_objective: str | None
    reason: str

    def projection(self) -> dict[str, object]:
        return {**asdict(self), "arm": dict(self.arm)}


@dataclass(frozen=True, slots=True)
class RankedArm:
    arm: Mapping[str, object]
    ranking: UcbRanking

    def projection(self) -> dict[str, object]:
        return {"arm": dict(self.arm), "ranking": self.ranking.projection()}


@dataclass(frozen=True, slots=True)
class ProposalIdentity:
    """Pre-execution identity without inventing observed surface, state, or language."""

    semantic_id: str
    technique_id: str
    controller_id: str
    mutation_id: str
    requested_language: str

    def __post_init__(self) -> None:
        if empty := [name for name, value in asdict(self).items() if not value.strip()]:
            raise ValueError("proposal identity fields must not be empty: " + ", ".join(empty))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ProposalIdentity:
        fields = (
            "semantic_id",
            "technique_id",
            "controller_id",
            "mutation_id",
            "requested_language",
        )
        if missing := [name for name in fields if not isinstance(value.get(name), str)]:
            raise ValueError("proposal identity is missing string fields: " + ", ".join(missing))
        return cls(**{name: str(value[name]).strip() for name in fields})

    @property
    def key(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class RankedFrontierArm:
    """Comparable ranking evidence for an executed arm or unresolved proposal."""

    arm: Mapping[str, object]
    key: str
    score: float
    components: tuple[ScoreComponent, ...]
    lifecycle: LifecycleState
    lifecycle_reason: str
    materialized: bool

    def projection(self) -> dict[str, object]:
        return {
            "arm": dict(self.arm),
            "key": self.key,
            "score": self.score,
            "components": tuple(asdict(item) for item in self.components),
            "lifecycle": self.lifecycle,
            "lifecycle_reason": self.lifecycle_reason,
            "materialized": self.materialized,
        }


def novel_arms(
    candidates: Sequence[Mapping[str, object]], existing_objectives: Sequence[str]
) -> tuple[NoveltyAdmission, ...]:
    """Measure objective novelty without turning text similarity into admission policy.

    Objective similarity is useful ranking evidence, but it cannot establish
    that two independently identified attack cells are equivalent. Canonical
    arm identity and durable ownership handle duplicate execution separately.
    """
    corpus = [value for value in existing_objectives if value.strip()]
    decisions: list[NoveltyAdmission] = []
    for candidate in candidates:
        objective = candidate.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            decisions.append(
                NoveltyAdmission(candidate, False, "invalid", 1.0, None, "objective is missing")
            )
            continue
        novelty = check_novelty(objective, corpus)
        schedule_novelty_shadow(objective, corpus)
        nearest = corpus[novelty.nearest_index] if novelty.nearest_index >= 0 else None
        decisions.append(
            NoveltyAdmission(
                candidate,
                True,
                novelty.verdict,
                novelty.max_similarity,
                nearest,
                "objective similarity recorded as ranking evidence; admission preserved",
            )
        )
        corpus.append(objective)
    return tuple(decisions)


def rank_pool(
    arms: Sequence[Mapping[str, object]],
    observations: Sequence[ArmObservation | Mapping[str, object]],
    *,
    policy: RankingPolicy = DEFAULT_RANKING_POLICY,
) -> tuple[RankedArm, ...]:
    """Rank admitted arms and remove evidence-retired arms from dispatch."""
    stats = aggregate_observations(observations)
    candidates: list[AttemptStats] = []
    measurements: dict[str, Mapping[str, float]] = {}
    states: dict[str, LifecycleState] = {}
    by_key: dict[str, Mapping[str, object]] = {}
    for arm in arms:
        identity = ArmIdentity.from_mapping(arm)
        key = identity.key
        if key in by_key:
            raise ValueError("arm pool contains duplicate canonical identity")
        by_key[key] = arm
        candidates.append(stats.get(key, AttemptStats(identity, 0, 0, 0.0, 0)))
        measured: dict[str, float] = {
            name: _measurement(arm, name)
            for name in (
                "novelty",
                "generalization_opportunity",
                "boundary_opportunity",
            )
        }
        rank_components = arm.get("rank_components")
        for name in (
            "confidence",
            "evaluation_uncertainty",
            "context_changed",
            "language_changed",
            "mutation_changed",
            "state_changed",
            "surface_changed",
        ):
            if name in arm or (isinstance(rank_components, Mapping) and name in rank_components):
                measured[name] = _measurement(arm, name)
        measurements[key] = measured
        state = arm.get("status", "trial")
        if state not in {"trial", "active", "retired"}:
            raise ValueError("arm status must be trial, active, or retired")
        states[key] = state  # type: ignore[assignment]
    ranked = rank_tasks_by_ucb(
        candidates, arm_measurements=measurements, arm_states=states, policy=policy
    )
    return tuple(
        RankedArm(by_key[item.key], item) for item in ranked if item.lifecycle != "retired"
    )


def rank_frontier(
    arms: Sequence[Mapping[str, object]],
    observations: Sequence[ArmObservation | Mapping[str, object]],
    *,
    policy: RankingPolicy = DEFAULT_RANKING_POLICY,
) -> tuple[RankedFrontierArm, ...]:
    """Rank the complete unresolved execution and pre-execution candidate frontier.

    Durable arms use their observed six-dimensional identity and quality history.
    Newly admitted proposals use their requested technique/controller/mutation/language
    identity. They receive a cold-start exploration score plus measured opportunity
    signals; the function never fabricates an actual language, target state, or
    quality result merely to make a proposal fit the post-execution schema.
    """
    materialized: list[Mapping[str, object]] = []
    proposals: list[Mapping[str, object]] = []
    for arm in arms:
        if _has_execution_identity(arm):
            materialized.append(arm)
        else:
            proposals.append(arm)

    ranked: list[RankedFrontierArm] = []
    for item in rank_pool(materialized, observations, policy=policy):
        ranking = item.ranking
        ranked.append(
            RankedFrontierArm(
                arm=item.arm,
                key="execution:" + ranking.key,
                score=ranking.score,
                components=ranking.components,
                lifecycle=ranking.lifecycle,
                lifecycle_reason=ranking.lifecycle_reason,
                materialized=True,
            )
        )

    eligible_attempts = sum(item.attempts for item in aggregate_observations(observations).values())
    seen_proposals: set[str] = set()
    for arm in proposals:
        identity = ProposalIdentity.from_mapping(arm)
        key = "proposal:" + identity.key
        if key in seen_proposals:
            raise ValueError("arm frontier contains duplicate proposal identity")
        seen_proposals.add(key)
        state = arm.get("status", "trial")
        if state not in {"trial", "active", "retired"}:
            raise ValueError("arm status must be trial, active, or retired")
        if state == "retired":
            continue
        novelty = _measurement(arm, "novelty")
        generalization = _measurement(arm, "generalization_opportunity")
        boundary = _measurement(arm, "boundary_opportunity")
        uncertainty = policy.exploration_constant * sqrt(log(eligible_attempts + 2))
        components = (
            ScoreComponent("quality_signal", 0.0, 0.0, "no attributable execution yet"),
            ScoreComponent(
                "uncertainty",
                uncertainty,
                uncertainty,
                "cold-start exploration bonus before an eligible target attempt",
            ),
            ScoreComponent(
                "novelty",
                novelty,
                policy.novelty_weight * novelty,
                "instruction novelty relative to durable objectives",
            ),
            ScoreComponent(
                "generalization",
                generalization,
                policy.generalization_weight * generalization,
                "measured opportunity from receipt-derived neighbouring wins",
            ),
            ScoreComponent(
                "boundary",
                boundary,
                policy.boundary_weight * boundary,
                "measured opportunity at an observed target-state boundary",
            ),
            ScoreComponent("latency", 0.0, 0.0, "not measured before execution"),
            ScoreComponent("cost", 0.0, 0.0, "not measured before execution"),
        )
        ranked.append(
            RankedFrontierArm(
                arm=arm,
                key=key,
                score=sum(component.contribution for component in components),
                components=components,
                lifecycle=state,  # type: ignore[arg-type]
                lifecycle_reason="unresolved proposal remains eligible for execution",
                materialized=False,
            )
        )
    return tuple(sorted(ranked, key=lambda item: (-item.score, item.key)))


def load_ranked_pool(
    source: ArmHistorySource,
    *,
    target_id: str,
    scenario_fingerprint: str,
    arms: Sequence[Mapping[str, object]],
    policy: RankingPolicy = DEFAULT_RANKING_POLICY,
) -> tuple[RankedArm, ...]:
    """Consume the durable target/scenario history through its narrow interface."""
    return rank_pool(
        arms,
        source.list_arm_observations(target_id, scenario_fingerprint),
        policy=policy,
    )


def _measurement(arm: Mapping[str, object], name: str) -> float:
    value = arm.get(name)
    if value is None:
        components = arm.get("rank_components")
        component_name = {
            "generalization_opportunity": "generalization",
            "boundary_opportunity": "boundary",
        }.get(name, name)
        component = components.get(component_name) if isinstance(components, Mapping) else None
        value = component.get("measured_value", 0.0) if isinstance(component, Mapping) else 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"arm {name} must be numeric")
    measured = float(value)
    if not 0.0 <= measured <= 1.0:
        raise ValueError(f"arm {name} must be between zero and one")
    return measured


def _has_execution_identity(arm: Mapping[str, object]) -> bool:
    fields = (
        "surface_id",
        "technique_id",
        "controller_id",
        "mutation_id",
        "actual_language",
        "state_id",
    )
    return all(isinstance(arm.get(name), str) and bool(str(arm[name]).strip()) for name in fields)
