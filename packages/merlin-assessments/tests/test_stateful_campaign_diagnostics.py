from __future__ import annotations

import json
from pathlib import Path

from merlin_assessments import StatefulFixtureBudget, diagnose_stateful_campaign

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "stateful_failed_campaign_snapshot.json"
CORPUS_PATH = Path(__file__).parent / "fixtures" / "stateful_target_regressions.json"


def _snapshot() -> dict[str, object]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _budget(snapshot: dict[str, object]) -> StatefulFixtureBudget:
    value = snapshot["fixture_budget"]
    assert isinstance(value, dict)
    return StatefulFixtureBudget(
        max_model_turns=int(value["max_model_turns"]),
        max_target_calls=int(value["max_target_calls"]),
        max_total_tokens=int(value["max_total_tokens"]),
        max_latency_ms=int(value["max_latency_ms"]),
        max_elapsed_ms=int(value["max_elapsed_ms"]),
        max_cost_usd_micros=int(value["max_cost_usd_micros"]),
        minimum_unique_valid_attempts=int(value["minimum_unique_valid_attempts"]),
        minimum_accepted_attempts_per_minute=float(value["minimum_accepted_attempts_per_minute"]),
    )


def test_sanitized_corpus_preserves_durable_failure_shape() -> None:
    snapshot = _snapshot()
    provenance = snapshot["provenance"]
    export = snapshot["durable_export"]
    assert isinstance(provenance, dict)
    assert isinstance(export, dict)
    assert provenance["kind"] == "sanitized_durable_export"
    assert set(provenance["removed_fields"]) == {
        "target",
        "prompt",
        "request_body",
        "response_body",
        "credentials",
    }
    assert len(export["technique_attempts"]) == 16
    assert len(export["target_exchanges"]) == 18
    assert {row["physical_state_digest"] for row in export["technique_attempts"]} == {
        "sha256:physical-1",
        "sha256:physical-2",
    }


def test_diagnostics_count_contamination_disagreement_and_operational_cost() -> None:
    snapshot = _snapshot()
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    diagnostics = diagnose_stateful_campaign(
        snapshot, budget=_budget(snapshot), import_manifest=corpus
    )
    metrics = diagnostics.metrics

    assert metrics.attempts == 16
    assert metrics.unique_candidates == 16
    assert metrics.physical_state_scopes == 2
    assert metrics.target_calls == 18
    assert metrics.quota_consumption == 14
    assert metrics.accepted_attempts == 14
    assert metrics.contaminated_attempts == 14
    assert metrics.transitions == 14
    assert metrics.contaminated_transitions == 12
    assert metrics.transition_disagreements == 1
    assert metrics.cross_layer_disagreements == 1
    assert metrics.terminal_transitions == 0
    assert metrics.quota_rejections == 4
    assert (metrics.batches, metrics.completed_batches, metrics.queued_batches) == (2, 1, 1)
    assert (
        metrics.outcomes_confirmed,
        metrics.outcomes_refuted,
        metrics.outcomes_blocked,
        metrics.outcomes_unclear,
    ) == (0, 10, 2, 4)
    assert metrics.model_turns == 5
    assert (metrics.input_tokens, metrics.output_tokens, metrics.total_tokens) == (
        1_087_603,
        100,
        1_087_703,
    )
    assert metrics.latency_ms == 600_000
    assert metrics.cost_usd_micros == 1_000_000
    assert metrics.elapsed_ms == 120_000
    assert metrics.unique_valid_attempts == 2
    assert metrics.accepted_attempts_per_minute == 7.0
    assert diagnostics.import_provenance_verified is True

    disagreement = [item for item in diagnostics.outcome_layers if item.disagreement]
    assert len(disagreement) == 1
    assert disagreement[0].candidate_id == "candidate/sanitized-01"
    assert (
        disagreement[0].worker_claim,
        disagreement[0].authoritative_transition,
        disagreement[0].frontier_outcome,
        disagreement[0].finding_status,
    ) == ("confirmed", "confirmed", "refuted", "refuted")


def test_preserved_regression_labels_match_durable_diagnostics() -> None:
    snapshot = _snapshot()
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    shared_state = next(case for case in corpus["cases"] if case["kind"] == "shared_state")
    labels = shared_state["diagnostic_labels"]
    diagnostics = diagnose_stateful_campaign(snapshot)
    metrics = diagnostics.metrics

    assert labels == {
        "unique_candidates": metrics.unique_candidates,
        "physical_state_scopes": metrics.physical_state_scopes,
        "target_calls": metrics.target_calls,
        "quota_consumption": metrics.quota_consumption,
        "cross_layer_disagreements": metrics.cross_layer_disagreements,
        "terminal_producing_layer": diagnostics.terminal_producing_layer,
        "terminal_reason": diagnostics.terminal_reason,
        "unfinished_frontier_cause": diagnostics.unfinished_frontier_cause,
    }


def test_diagnostics_expose_terminal_layer_and_fixture_budget_failures() -> None:
    snapshot = _snapshot()
    diagnostics = diagnose_stateful_campaign(snapshot, budget=_budget(snapshot))

    assert diagnostics.terminal_layers == (
        ("campaign", "running"),
        ("wave", "running"),
        ("scheduler", "completed"),
    )
    assert diagnostics.terminal_layer_disagreement is True
    assert diagnostics.terminal_producing_layer == "scheduler"
    assert diagnostics.terminal_reason == "scheduler_completed"
    assert diagnostics.unfinished_frontier_cause == "queued_batch_after_scheduler_completion"
    assert diagnostics.budget_failures == (
        "total_tokens",
        "unique_valid_attempts",
        "accepted_attempts_per_minute",
    )
    assert diagnostics.evidence_quality == "sanitized_durable_export"


def test_malformed_or_hand_authored_mapping_does_not_gain_durable_evidence_status() -> None:
    diagnostics = diagnose_stateful_campaign(
        {
            "durable_export": {
                "campaigns": [{"status": "completed", "elapsed_ms": -1}],
                "technique_attempts": "not rows",
                "model_calls": [{"input_tokens": True, "latency_ms": -20}],
            }
        }
    )

    assert diagnostics.evidence_quality == "unknown"
    assert diagnostics.metrics.attempts == 0
    assert diagnostics.metrics.total_tokens == 0
    assert diagnostics.metrics.latency_ms == 0
    assert diagnostics.metrics.accepted_attempts_per_minute == 0.0

    snapshot = _snapshot()
    snapshot["durable_export"]["campaigns"][0]["status"] = "completed"
    assert diagnose_stateful_campaign(snapshot).evidence_quality == "unknown"


def test_external_import_receipt_rejects_snapshot_and_manifest_tampering() -> None:
    snapshot = _snapshot()
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert diagnose_stateful_campaign(snapshot, import_manifest=corpus).import_provenance_verified

    corpus["source_import"]["campaign_id_digest"] = "sha256:tampered"
    assert not diagnose_stateful_campaign(
        snapshot, import_manifest=corpus
    ).import_provenance_verified


def test_every_budget_dimension_rejects_a_violation() -> None:
    snapshot = _snapshot()
    baseline = _budget(snapshot)
    dimensions = {
        "model_turns": {"max_model_turns": 4},
        "target_calls": {"max_target_calls": 17},
        "total_tokens": {"max_total_tokens": 1_087_702},
        "latency_ms": {"max_latency_ms": 599_999},
        "elapsed_ms": {"max_elapsed_ms": 119_999},
        "cost_usd_micros": {"max_cost_usd_micros": 999_999},
        "unique_valid_attempts": {"minimum_unique_valid_attempts": 3},
        "accepted_attempts_per_minute": {"minimum_accepted_attempts_per_minute": 7.001},
    }
    baseline_values = {field: getattr(baseline, field) for field in baseline.__dataclass_fields__}
    for expected, override in dimensions.items():
        budget = StatefulFixtureBudget(**(baseline_values | override))
        assert expected in diagnose_stateful_campaign(snapshot, budget=budget).budget_failures


def test_every_regression_fixture_rejects_call_and_useful_throughput_violations() -> None:
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    for case in corpus["cases"]:
        budget = StatefulFixtureBudget(**case["fixture_budget"])
        violating_snapshot = {
            "durable_export": {
                "campaigns": [{"elapsed_ms": budget.max_elapsed_ms}],
                "target_exchanges": [
                    {"id": f"call-{index}"} for index in range(budget.max_target_calls + 1)
                ],
                "technique_attempts": [],
            }
        }
        failures = diagnose_stateful_campaign(violating_snapshot, budget=budget).budget_failures
        assert "target_calls" in failures, case["id"]
        assert "unique_valid_attempts" in failures, case["id"]
