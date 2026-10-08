import hashlib
import json
from types import SimpleNamespace

import pytest
from merlin_tui.bootstrap import (
    _adaptive_controller_provenance,
    _exploration_report_metrics,
    build_backend,
)


def test_adaptive_controller_provenance_distinguishes_execution_from_selection() -> None:
    manifest = SimpleNamespace(profile=SimpleNamespace(value="exploration"))
    rows = _adaptive_controller_provenance(
        manifest,
        [
            {
                "payload": {
                    "kind": "exploration_review_decision",
                    "hypothesis_id": "hypothesis:one",
                    "admitted": True,
                }
            }
        ],
        [
            {"strategy_id": "goat", "outcome": "blocked"},
            {
                "strategy_id": "direct",
                "hypothesis_id": "hypothesis:one",
                "outcome": "confirmed",
            },
        ],
        pair_comparisons=[{"verdict": "negative"}],
        crescendo_receipts=[{"verdict": "positive", "steps": ({}, {})}],
        goat_receipts=[],
        hydra_select_receipts=[
            {
                "evidence_verdict": "negative",
                "candidates": ({}, {}, {}),
                "selected_candidate_ids": ("one", "two"),
                "steps": ({},),
            }
        ],
    )

    by_controller = {row["controller"]: row for row in rows}
    assert by_controller["PAIR"]["status"] == "ran"
    assert by_controller["GOAT"]["status"] == "selected; no terminal receipt"
    assert by_controller["Hydra"] == {
        "controller": "Hydra",
        "status": "ran",
        "mode": "candidate generation, bounded selection, and sequential probes",
        "activity": "3 proposed; 2 selected; 1 executed.",
        "outcome": "negative",
        "receipt_count": 1,
    }
    assert by_controller["Crescendo"]["activity"] == ("2 transition receipts across 1 execution.")
    assert by_controller["Exploration"]["status"] == "ran"
    assert by_controller["Exploration"]["outcome"] == "confirmed=1"


def test_adaptive_controller_provenance_does_not_claim_unselected_controller_ran() -> None:
    rows = _adaptive_controller_provenance(
        SimpleNamespace(profile=SimpleNamespace(value="recommended")),
        [],
        [],
        pair_comparisons=[],
        crescendo_receipts=[],
        goat_receipts=[],
        hydra_select_receipts=[],
    )

    assert {row["status"] for row in rows} == {"not evidenced", "not selected"}


def test_adaptive_controller_provenance_reports_degraded_and_unavailable_truthfully() -> None:
    rows = _adaptive_controller_provenance(
        SimpleNamespace(profile=SimpleNamespace(value="recommended")),
        [],
        [
            {
                "strategy_id": "goat",
                "outcome": "attempted",
                "strategy_execution": {
                    "requested_controller": "goat",
                    "actual_controller": "direct",
                    "execution_mode": "degraded",
                    "reason": "provider route lacked native turn callbacks",
                },
            },
            {
                "strategy_id": "hydra-select",
                "outcome": "unclear",
                "strategy_execution": {
                    "requested_controller": "hydra-select",
                    "actual_controller": None,
                    "execution_mode": "unavailable",
                    "reason": "reviewed strategy contract was absent",
                },
            },
        ],
        pair_comparisons=[],
        crescendo_receipts=[],
        goat_receipts=[],
        hydra_select_receipts=[],
    )

    by_controller = {row["controller"]: row for row in rows}
    assert by_controller["GOAT"]["status"] == "ran degraded"
    assert by_controller["GOAT"]["mode"] == "degraded: requested goat; actual direct"
    assert by_controller["GOAT"]["activity"] == ("provider route lacked native turn callbacks")
    assert by_controller["Hydra"]["status"] == "unavailable; unresolved"
    assert by_controller["Hydra"]["mode"] == (
        "unavailable: requested hydra-select; no controller executed"
    )
    assert by_controller["Hydra"]["outcome"] == "not available"


def test_adaptive_controller_provenance_reports_native_and_degraded_as_mixed() -> None:
    rows = _adaptive_controller_provenance(
        SimpleNamespace(profile=SimpleNamespace(value="recommended")),
        [],
        [
            {
                "strategy_id": "goat",
                "strategy_execution": {
                    "requested_controller": "goat",
                    "actual_controller": "direct",
                    "execution_mode": "degraded",
                    "reason": "provider route lacked native turn callbacks",
                },
            }
        ],
        pair_comparisons=[],
        crescendo_receipts=[],
        goat_receipts=[
            {
                "strategy_state": "completed",
                "evidence_verdict": "negative",
            }
        ],
        hydra_select_receipts=[],
    )

    goat = next(row for row in rows if row["controller"] == "GOAT")
    assert goat["status"] == "ran native and degraded"
    assert goat["mode"] == "mixed: 1 native receipt; 1 degraded execution"
    assert "Degraded (1): provider route lacked native turn callbacks" in goat["activity"]
    assert goat["outcome"] == "negative"


def test_adaptive_controller_provenance_keeps_unavailable_counts_in_mixed_modes() -> None:
    unavailable = {
        "strategy_id": "goat",
        "strategy_execution": {
            "requested_controller": "goat",
            "actual_controller": None,
            "execution_mode": "unavailable",
            "reason": "reviewed contract absent",
        },
    }
    degraded = {
        "strategy_id": "goat",
        "strategy_execution": {
            "requested_controller": "goat",
            "actual_controller": "direct",
            "execution_mode": "degraded",
            "reason": "native callbacks absent",
        },
    }
    manifest = SimpleNamespace(profile=SimpleNamespace(value="recommended"))

    native_and_unavailable = _adaptive_controller_provenance(
        manifest,
        [],
        [unavailable],
        pair_comparisons=[],
        crescendo_receipts=[],
        goat_receipts=[{"evidence_verdict": "negative", "steps": ()}],
        hydra_select_receipts=[],
    )
    mixed_without_native = _adaptive_controller_provenance(
        manifest,
        [],
        [degraded, unavailable],
        pair_comparisons=[],
        crescendo_receipts=[],
        goat_receipts=[],
        hydra_select_receipts=[],
    )
    all_modes = _adaptive_controller_provenance(
        manifest,
        [],
        [degraded, unavailable],
        pair_comparisons=[],
        crescendo_receipts=[],
        goat_receipts=[{"evidence_verdict": "negative", "steps": ()}],
        hydra_select_receipts=[],
    )

    first = next(row for row in native_and_unavailable if row["controller"] == "GOAT")
    second = next(row for row in mixed_without_native if row["controller"] == "GOAT")
    third = next(row for row in all_modes if row["controller"] == "GOAT")
    assert first["status"] == "mixed; unresolved unavailable execution"
    assert first["mode"] == "mixed: 1 native receipt; 1 unavailable execution"
    assert "Unavailable (1): reviewed contract absent" in first["activity"]
    assert second["status"] == "mixed; unresolved unavailable execution"
    assert second["mode"] == "mixed: 1 degraded execution; 1 unavailable execution"
    assert "Degraded (1): native callbacks absent" in second["activity"]
    assert "Unavailable (1): reviewed contract absent" in second["activity"]
    assert third["mode"] == (
        "mixed: 1 native receipt; 1 degraded execution; 1 unavailable execution"
    )
    assert "Native:" in third["activity"]


def test_exploration_report_metrics_are_derived_from_sealed_inputs() -> None:
    hypothesis = {
        "surface": "POST /chat",
        "assertion": "session ownership is not enforced",
        "oracle": "a foreign session cannot be read",
        "novelty_basis": "recon separated identity and session state",
    }
    hypothesis_id = (
        "hypothesis:"
        + hashlib.sha256(
            json.dumps(hypothesis, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
    )
    manifest = SimpleNamespace(
        profile=SimpleNamespace(value="exploration"),
        catalogue=(
            SimpleNamespace(
                id="learning:exploration-source",
                content=json.dumps(
                    {
                        "source": "history_guided",
                        "comparable_campaigns": 2,
                        "comparable_attempts": 7,
                        "current_negatives_skipped": 3,
                        "seed_reasons": [{"technique_id": "baseline", "reason": "control"}],
                    }
                ),
            ),
        ),
    )
    events = [
        {"payload": {"followups": [{"hypothesis": hypothesis}]}},
        {
            "payload": {
                "kind": "exploration_review_decision",
                "hypothesis_id": hypothesis_id,
                "verdict": "accept",
                "admitted": True,
            }
        },
    ]
    techniques = [
        {
            "id": hypothesis_id,
            "hypothesis_id": hypothesis_id,
            "outcome": "confirmed",
            "independent_reproduction": "validated",
        }
    ]

    metrics = _exploration_report_metrics(manifest, events, techniques)

    assert metrics is not None
    assert metrics["source"] == "history_guided"
    assert metrics["seed_count"] == 1
    assert metrics["hypotheses_admitted"] == 1
    assert metrics["hypotheses_attempted"] == 1
    assert metrics["outcomes_confirmed"] == 1
    assert metrics["history_savings"] == 3
    assert metrics["scope"] == "target_scoped"
    assert metrics["target_scoped_lessons"] == (hypothesis_id,)
    assert metrics["promotion_candidates"] == ()


def test_untrusted_caller_claim_cannot_create_cross_target_promotion() -> None:
    hypothesis = {
        "surface": "POST /chat",
        "assertion": "session ownership is not enforced",
        "oracle": "a foreign session cannot be read",
        "novelty_basis": "recon separated identity and session state",
    }
    hypothesis_id = (
        "hypothesis:"
        + hashlib.sha256(
            json.dumps(hypothesis, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
    )
    manifest = SimpleNamespace(profile=SimpleNamespace(value="exploration"), catalogue=())
    events = [{"payload": {"followups": [{"hypothesis": hypothesis}]}}]
    techniques = [
        {
            "id": hypothesis_id,
            "hypothesis_id": hypothesis_id,
            "outcome": "confirmed",
            "independent_reproduction": "validated",
        }
    ]

    metrics = _exploration_report_metrics(manifest, events, techniques)

    assert metrics is not None
    assert metrics["scope"] == "target_scoped"
    assert metrics["target_scoped_lessons"] == (hypothesis_id,)
    assert metrics["promotion_evidence_status"] == "not_evaluated_no_trusted_receipt"
    assert metrics["trusted_evaluator_receipts"] == ()
    assert metrics["promotion_candidates"] == ()
    with pytest.raises(TypeError, match="held_out_case_ids"):
        _exploration_report_metrics(  # type: ignore[call-arg]
            manifest, events, techniques, held_out_case_ids=frozenset({hypothesis_id})
        )


def test_non_exploration_profile_has_no_exploration_metrics() -> None:
    manifest = SimpleNamespace(profile=SimpleNamespace(value="recommended"), catalogue=())

    assert _exploration_report_metrics(manifest, [], []) is None


def test_build_backend_does_not_accept_untrusted_held_out_claims() -> None:
    import inspect

    assert "exploration_held_out_reports" not in inspect.signature(build_backend).parameters


def test_production_metrics_count_rejections_duplicates_and_grounding_failures() -> None:
    manifest = SimpleNamespace(profile=SimpleNamespace(value="exploration"), catalogue=())
    events = [
        {
            "payload": {
                "kind": "exploration_review_decision",
                "hypothesis_id": "hypothesis:rejected",
                "verdict": "reject",
                "admitted": False,
            }
        },
        {
            "payload": {
                "kind": "exploration_selection",
                "selection": {
                    "selected": (
                        {
                            "candidate_id": "hypothesis:selected",
                            "reasons": ({"factor": "redundancy", "measured_value": 2},),
                        },
                    ),
                    "deferred": (),
                    "unmeasured": (
                        {"candidate_id": "hypothesis:ungrounded", "reason": "missing evidence"},
                    ),
                },
            }
        },
    ]

    metrics = _exploration_report_metrics(manifest, events, [])

    assert metrics is not None
    assert metrics["hypotheses_rejected"] == 1
    assert metrics["hypotheses_duplicate"] == 2
    assert metrics["hypotheses_grounding_failed"] == 1
