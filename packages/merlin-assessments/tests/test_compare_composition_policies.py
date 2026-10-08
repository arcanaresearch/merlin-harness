"""Specialist composition policy comparison smoke test.

Drives ``scripts/compare_composition_policies.py`` against the deterministic
fixture target: both composition policies must complete a seeded campaign and
the emitted summary must carry all eight metric categories per policy.  This
is an informational comparison harness, not a certification gate or a
statistical reliability study.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from merlin_assessments import FixtureTargetServer

ROOT = Path(__file__).resolve().parents[3]
DRIVER_PATH = ROOT / "scripts" / "compare_composition_policies.py"

spec = importlib.util.spec_from_file_location("compare_composition_policies", DRIVER_PATH)
assert spec is not None and spec.loader is not None
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


async def test_composition_policy_comparison_completes_both_runs(tmp_path: Path) -> None:
    async with FixtureTargetServer() as target:
        results = {}
        for policy in driver.POLICIES:
            results[policy] = await driver.run_policy_lane(tmp_path / policy, target, policy)
    summary = driver._summary(results)  # noqa: SLF001

    assert set(summary["policies"]) == set(driver.POLICIES)
    assert summary["metric_categories"] == list(driver.METRIC_CATEGORIES)
    for policy in driver.POLICIES:
        per_policy = summary["policies"][policy]
        assert set(per_policy) == set(driver.METRIC_CATEGORIES)
        # The fixture's deterministic oracle is an observation, not an
        # authoritative validator. Both policies preserve the matched evidence
        # as uncertain until a validator or target-owned success signal confirms it.
        assert per_policy["findings"]["validated_count"] == 0
        assert per_policy["findings"]["gate_verdict"] == "ambiguous"
        assert per_policy["findings"]["win_marker_observed"] is True
        assert per_policy["reproduction"]["gate_receipt_steps"] == 2
        reliability = per_policy["reliability"]
        assert reliability["sample_size"] == 1
        assert reliability["execution_completed"] is True
        assert reliability["attempt_count"] > 0
        assert reliability["completed_attempt_count"] == reliability["attempt_count"]
        assert reliability["failed_attempt_count"] == 0
        assert reliability["acknowledged_effect_fraction"] == 1.0
        assert per_policy["tokens"]["inference_count"] == 2
    opinionated = results["opinionated"]["composition"]
    autonomous = results["autonomous"]["composition"]
    assert opinionated["admitted_specialists"] == ["dynamic-injection"]
    assert autonomous["admitted_specialists"] == []
    assert "structural lanes only" in autonomous["note"]


async def test_composition_policy_comparison_writes_summary(tmp_path: Path) -> None:
    summary = await driver._run(driver.POLICIES, tmp_path / "run")

    assert (tmp_path / "run" / "summary.json").is_file()
    assert (tmp_path / "run" / "summary.md").is_file()
    assert set(summary["policies"]) == set(driver.POLICIES)
    assert all(
        set(summary["policies"][policy]) == set(driver.METRIC_CATEGORIES)
        for policy in driver.POLICIES
    )
    assert summary["gate"] is None
    assert len(summary["limitations"]) == 4
    assert "one sample" in summary["limitations"][1]
