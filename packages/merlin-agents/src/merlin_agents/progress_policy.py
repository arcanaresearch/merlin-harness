"""Pure campaign progress and stall decisions from durable observations.

The policy intentionally has no orchestration counters.  Waves, supervisor
retries, hypotheses, and extensions are implementation details rather than
evidence that a campaign is progressing or finished.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProviderHealth(StrEnum):
    """Observed health of the provider/transport needed by unresolved work."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ProgressAction(StrEnum):
    """Operational proposal; never a security verdict."""

    CONTINUE = "continue"
    REPLAN = "replan"
    WAIT_FOR_INFRASTRUCTURE = "wait_for_infrastructure"
    COMPLETE = "complete"
    STOP_AT_BUDGET = "stop_at_budget"


@dataclass(frozen=True, slots=True)
class BudgetEvidence:
    """Explicit durable budget limits observed as exhausted by the caller."""

    exhausted_dimensions: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if any(not dimension.strip() for dimension in self.exhausted_dimensions):
            raise ValueError("budget dimensions must not be blank")

    @property
    def exhausted(self) -> bool:
        return bool(self.exhausted_dimensions)


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    """Durable campaign facts at one observation boundary.

    ``semantic_fingerprint`` represents the meaningful target/evidence state
    after removing volatile identifiers and timestamps.  A missing fingerprint
    means semantic progress cannot yet be judged; it is not a stall.
    """

    durable_target_calls: int
    durable_evidence: frozenset[str]
    unresolved_frontier: frozenset[str]
    durably_resolved: frozenset[str]
    semantic_fingerprint: str | None
    frontier_exhausted: bool = False
    provider_health: ProviderHealth = ProviderHealth.HEALTHY
    infrastructure_failures: frozenset[str] = frozenset()
    budget: BudgetEvidence = BudgetEvidence()

    def __post_init__(self) -> None:
        if self.durable_target_calls < 0:
            raise ValueError("durable target calls cannot be negative")
        if self.semantic_fingerprint is not None and not self.semantic_fingerprint.strip():
            raise ValueError("semantic fingerprint must be non-blank when supplied")
        for label, values in (
            ("evidence identifiers", self.durable_evidence),
            ("frontier identifiers", self.unresolved_frontier),
            ("resolved frontier identifiers", self.durably_resolved),
            ("infrastructure failures", self.infrastructure_failures),
        ):
            if any(not value.strip() for value in values):
                raise ValueError(f"{label} must not be blank")
        if overlap := self.unresolved_frontier & self.durably_resolved:
            raise ValueError(
                "frontier items cannot be unresolved and durably resolved: "
                + ", ".join(sorted(overlap))
            )
        if self.frontier_exhausted and self.unresolved_frontier:
            raise ValueError("an exhausted frontier cannot contain unresolved items")


@dataclass(frozen=True, slots=True)
class ProgressDecision:
    """A transparent next-step proposal derived from two durable snapshots."""

    action: ProgressAction
    made_progress: bool
    semantic_plateau: bool
    terminal: bool
    reasons: tuple[str, ...]
    limitation: str | None = None


def assess_campaign_progress(
    previous: ProgressSnapshot | None,
    current: ProgressSnapshot,
) -> ProgressDecision:
    """Classify progress without treating infrastructure trouble as an outcome."""

    _validate_monotonic_durability(previous, current)
    evidence_added = bool(
        previous is not None and current.durable_evidence - previous.durable_evidence
    )
    frontier_added = bool(
        previous is not None and current.unresolved_frontier - previous.unresolved_frontier
    )
    resolution_added = bool(
        previous is not None and current.durably_resolved - previous.durably_resolved
    )
    calls_added = bool(
        previous is not None and current.durable_target_calls > previous.durable_target_calls
    )
    semantics_changed = (
        previous is not None
        and previous.semantic_fingerprint is not None
        and current.semantic_fingerprint is not None
        and previous.semantic_fingerprint != current.semantic_fingerprint
    )
    made_progress = evidence_added or frontier_added or resolution_added or semantics_changed
    semantic_plateau = bool(
        calls_added
        and previous is not None
        and previous.semantic_fingerprint is not None
        and current.semantic_fingerprint == previous.semantic_fingerprint
        and not evidence_added
        and not frontier_added
        and not resolution_added
    )
    unhealthy = current.provider_health is not ProviderHealth.HEALTHY

    budget_stops_unfinished = current.budget.exhausted and (
        not current.frontier_exhausted
        or bool(current.unresolved_frontier)
        or bool(current.infrastructure_failures)
        or unhealthy
    )
    if budget_stops_unfinished:
        dimensions = ", ".join(sorted(current.budget.exhausted_dimensions))
        unresolved = len(current.unresolved_frontier)
        infrastructure = len(current.infrastructure_failures)
        limitation_parts = [f"{unresolved} unresolved frontier item(s)"]
        if not current.frontier_exhausted:
            limitation_parts.append("frontier exhaustion was not established")
        if unhealthy:
            limitation_parts.append(f"provider health was {current.provider_health.value}")
        if infrastructure:
            limitation_parts.append(f"{infrastructure} infrastructure issue(s) remained unresolved")
        return ProgressDecision(
            ProgressAction.STOP_AT_BUDGET,
            made_progress,
            semantic_plateau,
            True,
            (
                f"explicit {dimensions} budget exhausted",
                *((f"provider health is {current.provider_health.value}",) if unhealthy else ()),
            ),
            "Stopped at an explicit budget with " + "; ".join(limitation_parts) + ".",
        )

    if unhealthy or current.infrastructure_failures:
        reasons = []
        if unhealthy:
            reasons.append(f"provider health is {current.provider_health.value}")
        reasons.extend(sorted(current.infrastructure_failures))
        return ProgressDecision(
            ProgressAction.WAIT_FOR_INFRASTRUCTURE,
            made_progress,
            semantic_plateau,
            False,
            tuple(reasons),
        )

    if current.frontier_exhausted:
        return ProgressDecision(
            ProgressAction.COMPLETE,
            made_progress,
            semantic_plateau,
            True,
            ("the durable full-frontier projection is exhausted",),
        )

    if previous is None:
        return ProgressDecision(
            ProgressAction.CONTINUE,
            made_progress,
            False,
            False,
            ("baseline durable campaign observation recorded",),
        )

    if made_progress:
        reasons = []
        if evidence_added:
            reasons.append("new durable evidence was recorded")
        if frontier_added:
            reasons.append("new unresolved frontier work was discovered")
        if resolution_added:
            reasons.append("frontier work gained an attributable durable resolution")
        if semantics_changed:
            reasons.append("the durable semantic state changed")
        return ProgressDecision(
            ProgressAction.CONTINUE,
            True,
            False,
            False,
            tuple(reasons),
        )

    if semantic_plateau:
        return ProgressDecision(
            ProgressAction.REPLAN,
            False,
            True,
            False,
            ("new target calls produced no durable evidence, frontier change, or semantic change",),
        )

    reason = (
        "semantic progress is not yet observable"
        if calls_added
        else "no new durable observation has arrived"
    )
    return ProgressDecision(
        ProgressAction.CONTINUE,
        False,
        False,
        False,
        (reason,),
    )


def _validate_monotonic_durability(
    previous: ProgressSnapshot | None,
    current: ProgressSnapshot,
) -> None:
    if previous is None:
        return
    if current.durable_target_calls < previous.durable_target_calls:
        raise ValueError("durable target call count cannot regress")
    if not previous.durable_evidence.issubset(current.durable_evidence):
        raise ValueError("durable evidence cannot disappear")
    if not previous.durably_resolved.issubset(current.durably_resolved):
        raise ValueError("durable frontier resolutions cannot disappear")
    removed = previous.unresolved_frontier - current.unresolved_frontier
    if unexplained := removed - current.durably_resolved:
        raise ValueError(
            "unresolved frontier items require attributable durable resolution before removal: "
            + ", ".join(sorted(unexplained))
        )
