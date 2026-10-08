"""Preflight campaign cost estimation and budget gating.

Before a reviewed campaign executes, estimate its token/USD spend from
historical ``schedule_usage`` averages grouped by assignment kind and refuse
to run above the configured caps unless the operator explicitly overrides.
Estimation is advisory telemetry: it never narrows the admitted agenda, it
only gates execution against operator-configured budget caps.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# Fallback per-assignment token estimate when no historical usage exists:
# a base context plus a per-target-call increment, floored so single-call
# assignments still carry a realistic minimum.
FALLBACK_BASE_TOKENS = 6_000
FALLBACK_TOKENS_PER_TARGET_CALL = 2_000
FALLBACK_MIN_TOKENS = 8_000
# Conservative bound multiplier applied to the point estimate before the
# execution gate compares it against the configured caps.
CONSERVATIVE_MULTIPLIER = 2.0
# Assumed input/output split when pricing is pinned but history carries no
# token composition; agentic loops are input-dominated.
DEFAULT_INPUT_TOKEN_SHARE = 0.8
# History bucket that aggregates every worker wave regardless of wave kind.
WORKER_HISTORY_KIND = "worker"
LEAD_HISTORY_KIND = "lead"


@dataclass(frozen=True, slots=True)
class AssignmentEstimateInput:
    """One admitted agenda assignment as the estimator sees it."""

    kind: str
    role: str
    expected_target_calls: int

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.role.strip():
            raise ValueError("assignment estimate kind and role must not be empty")
        if isinstance(self.expected_target_calls, bool) or self.expected_target_calls < 1:
            raise ValueError("expected target calls must be a positive integer")


@dataclass(frozen=True, slots=True)
class PreflightEstimate:
    """Token/USD projection for one prepared campaign."""

    assignment_estimates: tuple[Mapping[str, object], ...]
    lead_assessments: int
    lead_estimated_tokens: int
    estimated_tokens: int
    estimated_cost_usd: float | None
    conservative_tokens: int
    conservative_cost_usd: float | None
    conservative_multiplier: float
    history: str

    def as_mapping(self) -> Mapping[str, object]:
        return {
            "assignments": self.assignment_estimates,
            "lead_assessments": self.lead_assessments,
            "lead_estimated_tokens": self.lead_estimated_tokens,
            "estimated_tokens": self.estimated_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "conservative_tokens": self.conservative_tokens,
            "conservative_cost_usd": self.conservative_cost_usd,
            "conservative_multiplier": self.conservative_multiplier,
            "history": self.history,
        }


def fallback_assignment_tokens(expected_target_calls: int) -> int:
    """Per-assignment token estimate when no usage history exists."""
    calls = max(1, expected_target_calls)
    return max(
        FALLBACK_MIN_TOKENS,
        FALLBACK_BASE_TOKENS + calls * FALLBACK_TOKENS_PER_TARGET_CALL,
    )


def _numeric(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def _average_tokens_per_call(records: Sequence[Mapping[str, float]]) -> float | None:
    """Average tokens per target call across historical usage records."""
    per_call: list[float] = []
    for record in records:
        total = _numeric(record.get("total_tokens"))
        if total is None or total <= 0:
            continue
        turns = _numeric(record.get("turns")) or 0.0
        per_call.append(total / max(turns, 1.0))
    if not per_call:
        return None
    return sum(per_call) / len(per_call)


def _input_share(records: Sequence[Mapping[str, float]]) -> float:
    shares: list[float] = []
    for record in records:
        total = _numeric(record.get("total_tokens"))
        input_tokens = _numeric(record.get("input_tokens"))
        if total and input_tokens is not None and input_tokens <= total:
            shares.append(input_tokens / total)
    if not shares:
        return DEFAULT_INPUT_TOKEN_SHARE
    return sum(shares) / len(shares)


def _cost_for_tokens(
    tokens: float,
    input_share: float,
    pricing: tuple[float | None, float | None] | None,
) -> float | None:
    if pricing is None:
        return None
    input_rate, output_rate = pricing
    if input_rate is None and output_rate is None:
        return None
    incoming = tokens * input_share
    outgoing = tokens - incoming
    return (incoming * (input_rate or 0.0) + outgoing * (output_rate or 0.0)) / 1_000_000


def estimate_preflight(
    assignments: Sequence[AssignmentEstimateInput],
    *,
    lead_assessments: int,
    history: Mapping[str, Sequence[Mapping[str, float]]],
    pricing: Mapping[str, tuple[float | None, float | None]],
) -> PreflightEstimate:
    """Estimate total and conservative (2x) token/USD spend for a campaign."""
    if lead_assessments < 0:
        raise ValueError("lead assessments must be non-negative")
    history_used = False
    details: list[Mapping[str, object]] = []
    total_tokens = 0.0
    total_cost = 0.0
    cost_known = True
    for assignment in assignments:
        records = tuple(history.get(assignment.kind, ())) or tuple(
            history.get(WORKER_HISTORY_KIND, ())
        )
        average = _average_tokens_per_call(records)
        if average is None:
            tokens = float(fallback_assignment_tokens(assignment.expected_target_calls))
            source = "fallback"
        else:
            history_used = True
            tokens = average * assignment.expected_target_calls
            source = "history"
        cost = _cost_for_tokens(tokens, _input_share(records), pricing.get(assignment.role))
        if cost is None:
            cost_known = False
        else:
            total_cost += cost
        total_tokens += tokens
        details.append(
            {
                "kind": assignment.kind,
                "role": assignment.role,
                "expected_target_calls": assignment.expected_target_calls,
                "estimated_tokens": int(round(tokens)),
                "estimated_cost_usd": cost,
                "source": source,
            }
        )
    lead_records = tuple(history.get(LEAD_HISTORY_KIND, ()))
    lead_average = _average_tokens_per_call(lead_records)
    if lead_average is None:
        lead_tokens = float(fallback_assignment_tokens(1))
    else:
        history_used = True
        lead_tokens = lead_average
    lead_tokens *= lead_assessments
    lead_cost = _cost_for_tokens(
        lead_tokens, _input_share(lead_records), pricing.get("campaign-lead")
    )
    if lead_cost is None:
        cost_known = False
    else:
        total_cost += lead_cost
    total_tokens += lead_tokens
    estimated_tokens = int(round(total_tokens))
    conservative_tokens = int(round(total_tokens * CONSERVATIVE_MULTIPLIER))
    return PreflightEstimate(
        assignment_estimates=tuple(details),
        lead_assessments=lead_assessments,
        lead_estimated_tokens=int(round(lead_tokens)),
        estimated_tokens=estimated_tokens,
        estimated_cost_usd=round(total_cost, 6) if cost_known else None,
        conservative_tokens=conservative_tokens,
        conservative_cost_usd=(
            round(total_cost * CONSERVATIVE_MULTIPLIER, 6) if cost_known else None
        ),
        conservative_multiplier=CONSERVATIVE_MULTIPLIER,
        history="applied" if history_used else "unavailable",
    )


def exceeded_caps(
    estimate: Mapping[str, object],
    *,
    max_tokens: int | None = None,
    max_cost_usd: float | None = None,
) -> tuple[str, ...]:
    """Caps the conservative estimate exceeds; empty means within budget."""
    exceeded: list[str] = []
    conservative_tokens = _numeric(estimate.get("conservative_tokens"))
    if max_tokens is not None and conservative_tokens is not None:
        if conservative_tokens > max_tokens:
            exceeded.append("tokens")
    conservative_cost = _numeric(estimate.get("conservative_cost_usd"))
    if max_cost_usd is not None and conservative_cost is not None:
        if conservative_cost > max_cost_usd:
            exceeded.append("cost_usd")
    return tuple(exceeded)
