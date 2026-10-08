"""Unit tests for preflight campaign cost estimation and budget gating."""

from __future__ import annotations

import pytest
from merlin_agents.preflight import (
    CONSERVATIVE_MULTIPLIER,
    AssignmentEstimateInput,
    estimate_preflight,
    exceeded_caps,
    fallback_assignment_tokens,
)


def test_fallback_assignment_tokens_formula() -> None:
    assert fallback_assignment_tokens(1) == 8_000
    assert fallback_assignment_tokens(4) == 14_000
    assert fallback_assignment_tokens(10) == 26_000


def test_estimate_without_history_uses_fallback_per_assignment() -> None:
    estimate = estimate_preflight(
        (
            AssignmentEstimateInput(kind="direct", role="injection", expected_target_calls=1),
            AssignmentEstimateInput(kind="goat", role="injection", expected_target_calls=5),
        ),
        lead_assessments=2,
        history={},
        pricing={},
    )
    assert estimate.history == "unavailable"
    worker_tokens = 8_000 + 16_000
    lead_tokens = 2 * 8_000
    assert estimate.lead_estimated_tokens == lead_tokens
    assert estimate.estimated_tokens == worker_tokens + lead_tokens
    assert estimate.conservative_tokens == int(
        round((worker_tokens + lead_tokens) * CONSERVATIVE_MULTIPLIER)
    )
    assert all(item["source"] == "fallback" for item in estimate.assignment_estimates)
    # No pricing pinned anywhere: cost is unknown rather than fabricated.
    assert estimate.estimated_cost_usd is None
    assert estimate.conservative_cost_usd is None


def test_estimate_with_history_averages_tokens_per_call() -> None:
    history = {
        "worker": (
            {"total_tokens": 20_000, "turns": 4, "input_tokens": 16_000},
            {"total_tokens": 40_000, "turns": 8, "input_tokens": 30_000},
        ),
        "lead": ({"total_tokens": 50_000, "turns": 1},),
    }
    estimate = estimate_preflight(
        (
            AssignmentEstimateInput(kind="direct", role="injection", expected_target_calls=3),
            AssignmentEstimateInput(kind="direct", role="recon", expected_target_calls=1),
        ),
        lead_assessments=1,
        history=history,
        pricing={"injection": (1.0, 2.0), "recon": (1.0, 2.0), "campaign-lead": (1.0, 2.0)},
    )
    assert estimate.history == "applied"
    # Historical average: 5,000 tokens per target call for both records.
    assert estimate.estimated_tokens == 3 * 5_000 + 1 * 5_000 + 50_000
    assert estimate.conservative_tokens == estimate.estimated_tokens * 2
    assert all(item["source"] == "history" for item in estimate.assignment_estimates)
    assert estimate.estimated_cost_usd is not None
    assert estimate.conservative_cost_usd == pytest.approx(
        estimate.estimated_cost_usd * CONSERVATIVE_MULTIPLIER
    )
    # Injection: 15,000 tokens with historical input share 0.775 at $1/$2 per M.
    injection = estimate.assignment_estimates[0]
    assert injection["estimated_cost_usd"] == pytest.approx(
        (15_000 * 0.775 * 1.0 + 15_000 * 0.225 * 2.0) / 1_000_000
    )


def test_kind_specific_history_preferred_over_worker_aggregate() -> None:
    history = {
        "goat": ({"total_tokens": 90_000, "turns": 9},),
        "worker": ({"total_tokens": 5_000, "turns": 1},),
    }
    estimate = estimate_preflight(
        (AssignmentEstimateInput(kind="goat", role="injection", expected_target_calls=2),),
        lead_assessments=0,
        history=history,
        pricing={},
    )
    # goat bucket: 10,000 tokens per call; worker aggregate would give 5,000.
    assert estimate.estimated_tokens == 20_000


def test_zero_token_history_records_are_ignored() -> None:
    history = {"worker": ({"total_tokens": 0, "turns": 0},)}
    estimate = estimate_preflight(
        (AssignmentEstimateInput(kind="direct", role="injection", expected_target_calls=1),),
        lead_assessments=0,
        history=history,
        pricing={},
    )
    assert estimate.history == "unavailable"
    assert estimate.estimated_tokens == 8_000


def test_assignment_input_validation() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        AssignmentEstimateInput(kind="direct", role="injection", expected_target_calls=0)
    with pytest.raises(ValueError, match="must not be empty"):
        AssignmentEstimateInput(kind="", role="injection", expected_target_calls=1)


def test_exceeded_caps_compares_conservative_bound() -> None:
    estimate = {"conservative_tokens": 100_000, "conservative_cost_usd": 2.5}
    assert exceeded_caps(estimate, max_tokens=200_000, max_cost_usd=5.0) == ()
    assert exceeded_caps(estimate, max_tokens=50_000) == ("tokens",)
    assert exceeded_caps(estimate, max_cost_usd=1.0) == ("cost_usd",)
    assert exceeded_caps(estimate, max_tokens=50_000, max_cost_usd=1.0) == (
        "tokens",
        "cost_usd",
    )
    # Unset caps and unknown cost never gate.
    assert exceeded_caps(estimate) == ()
    assert (
        exceeded_caps({"conservative_tokens": 10, "conservative_cost_usd": None}, max_cost_usd=0.01)
        == ()
    )
