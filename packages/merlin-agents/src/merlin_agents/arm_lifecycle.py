"""Deterministic trial → active | retired lifecycle for the durable ARM pool.

Pure arithmetic over recorded cross-campaign outcomes — no model judgment.
Mirrors the reference scanner's ``evaluateTrialStrategies``: once a trial arm
accumulates enough eligible attempts against a target/scenario, its graded
score decides the transition. A positive score promotes the arm to ``active``;
a trial that ends with no positive signal retires as ``no_signal_after_trial``.
Infrastructure failures never count as attempts here because
``aggregate_observations`` already withholds them from ``AttemptStats``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from .discovery import AttemptStats, LifecycleState

DEFAULT_TRIAL_MIN_ATTEMPTS = 5

PROMOTE_REASON = "signal_after_trial"
RETIRE_REASON_NO_SIGNAL = "no_signal_after_trial"

CONFIRMED_WEIGHT = 100
PARTIAL_WEIGHT = 8

LifecycleAction = Literal["promote", "retire"]


@dataclass(frozen=True, slots=True)
class ArmTrialEvaluation:
    """One graded trial verdict for an arm's recorded outcome history."""

    action: LifecycleAction
    reason: str
    status: LifecycleState
    attempts: int
    confirmed: int
    partials: float
    failures: float
    score: float

    def projection(self) -> dict[str, object]:
        return asdict(self)


def trial_signal_score(*, confirmed: int, partials: float, failures: float) -> float:
    """Grade one arm: ``100 * confirmed + 8 * partial − failures``."""
    if confirmed < 0 or partials < 0 or failures < 0:
        raise ValueError("trial outcome counts cannot be negative")
    return float(CONFIRMED_WEIGHT * confirmed) + PARTIAL_WEIGHT * partials - failures


def trial_inputs_from_stats(stats: AttemptStats) -> dict[str, float | int]:
    """Project aggregated attempt stats onto the trial scoring vocabulary.

    ``confirmed`` counts wins; ``partials`` is the summed graded partial
    signal; ``failures`` are the remaining quality-eligible attempts that
    produced neither a win nor a partial (refuted, blocked, unclear).
    """
    confirmed = stats.wins
    partials = stats.partial_signal
    failures = max(0.0, stats.attempts - confirmed - partials)
    return {"confirmed": confirmed, "partials": partials, "failures": failures}


def evaluate_trial_lifecycle(
    status: LifecycleState | str,
    *,
    attempts: int,
    confirmed: int,
    partials: float,
    failures: float,
    min_attempts: int = DEFAULT_TRIAL_MIN_ATTEMPTS,
) -> ArmTrialEvaluation | None:
    """Decide a trial arm's transition from its recorded outcomes, or ``None``.

    ``None`` means no transition: the arm is not in ``trial`` (active and
    retired states are owned by ranking/reactivation paths) or the trial has
    not yet reached ``min_attempts`` eligible attempts across campaigns.
    """
    if min_attempts < 1:
        raise ValueError("trial minimum attempts must be positive")
    if attempts < 0 or confirmed < 0 or partials < 0 or failures < 0:
        raise ValueError("trial outcome counts cannot be negative")
    if status not in {"trial", "active", "retired"}:
        raise ValueError("arm status must be trial, active, or retired")
    if status != "trial" or attempts < min_attempts:
        return None
    score = trial_signal_score(confirmed=confirmed, partials=partials, failures=failures)
    if score > 0:
        return ArmTrialEvaluation(
            action="promote",
            reason=PROMOTE_REASON,
            status="active",
            attempts=attempts,
            confirmed=confirmed,
            partials=partials,
            failures=failures,
            score=score,
        )
    return ArmTrialEvaluation(
        action="retire",
        reason=RETIRE_REASON_NO_SIGNAL,
        status="retired",
        attempts=attempts,
        confirmed=confirmed,
        partials=partials,
        failures=failures,
        score=score,
    )


def evaluate_trial_from_stats(
    status: LifecycleState | str,
    stats: AttemptStats,
    *,
    min_attempts: int = DEFAULT_TRIAL_MIN_ATTEMPTS,
) -> ArmTrialEvaluation | None:
    """Evaluate one arm's trial directly from its aggregated attempt stats."""
    inputs = trial_inputs_from_stats(stats)
    return evaluate_trial_lifecycle(
        status,
        attempts=stats.attempts,
        confirmed=int(inputs["confirmed"]),
        partials=float(inputs["partials"]),
        failures=float(inputs["failures"]),
        min_attempts=min_attempts,
    )


def lifecycle_counts(arms: Sequence[Mapping[str, object]]) -> dict[str, int]:
    """Count pool arms by persisted lifecycle status."""
    counts = {"trial": 0, "active": 0, "retired": 0}
    for arm in arms:
        status = str(arm.get("status") or "trial")
        if status in counts:
            counts[status] += 1
    return counts
