from __future__ import annotations

import argparse
import importlib.util
import stat
from pathlib import Path

import pytest
from merlin_contracts import EvaluationMetrics
from merlin_promotion import load_frozen_evaluation_suite


def _script_module():
    path = Path(__file__).parents[3] / "scripts" / "acceptance_provider_route.py"
    spec = importlib.util.spec_from_file_location("acceptance_provider_route", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_acceptance_runner_writes_loadable_owner_only_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _script_module()
    fixture = tmp_path / "case.json"
    fixture.write_text("{}", encoding="utf-8")
    destination = tmp_path / "suite.json"
    observed = EvaluationMetrics(
        task_success_rate=1,
        false_positive_rate=0,
        evidence_attribution_rate=1,
        authority_violations=0,
        mean_cost=0,
        p95_latency_ms=25,
        cancellation_recovery_rate=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
    )

    class Provider:
        async def close(self) -> None:
            return None

    async def evaluate(candidate, case, binding):
        del candidate, case, binding
        return observed

    monkeypatch.setattr(module, "_provider", lambda arguments: Provider())
    monkeypatch.setattr(module, "ProviderCaseEvaluator", lambda provider: evaluate)
    arguments = argparse.Namespace(
        fixtures=[fixture],
        model="reviewed-model",
        transport="default",
        provider_name="reviewed-provider",
        route="reviewed-route",
        role_scopes=["routine"],
        case_timeout_seconds=60,
        max_events=20_000,
        write_suite=destination,
        suite_id="reviewed-suite",
        dataset_provenance="operator-heldout-v1",
        maximum_token_ratio=1.25,
        maximum_latency_ratio=2,
    )

    await module._run(arguments)

    suite = load_frozen_evaluation_suite(destination)
    assert suite.id == "reviewed-suite"
    assert suite.baseline_metrics == observed
    assert suite.policy.maximum_token_ratio == 1.25
    assert suite.policy.maximum_latency_ratio == 2
    assert suite.budget["max_events"] == 20_000
    assert suite.route_role_scopes == ("routine",)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        module._write_new_owner_file(destination, "must not replace\n")


@pytest.mark.asyncio
async def test_acceptance_runner_never_freezes_failing_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _script_module()
    fixture = tmp_path / "case.json"
    fixture.write_text("{}", encoding="utf-8")
    destination = tmp_path / "must-not-exist.json"
    failed = EvaluationMetrics(
        task_success_rate=0,
        false_positive_rate=0,
        evidence_attribution_rate=1,
        authority_violations=0,
        mean_cost=0,
        p95_latency_ms=25,
        cancellation_recovery_rate=1,
    )

    class Provider:
        async def close(self) -> None:
            return None

    async def evaluate(candidate, case, binding):
        del candidate, case, binding
        return failed

    monkeypatch.setattr(module, "_provider", lambda arguments: Provider())
    monkeypatch.setattr(module, "ProviderCaseEvaluator", lambda provider: evaluate)
    arguments = argparse.Namespace(
        fixtures=[fixture],
        model="failing-model",
        transport="default",
        provider_name="reviewed-provider",
        route="reviewed-route",
        role_scopes=None,
        case_timeout_seconds=60,
        max_events=20_000,
        write_suite=destination,
        suite_id="invalid-suite",
        dataset_provenance="operator-heldout-v1",
        maximum_token_ratio=1.25,
        maximum_latency_ratio=2,
    )

    with pytest.raises(SystemExit):
        await module._run(arguments)
    assert not destination.exists()
