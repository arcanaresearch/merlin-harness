from __future__ import annotations

import hashlib
import json

import pytest
from merlin_assessments.transition_oracle import evaluate_state_transition


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _identity(phase: str, value: str) -> dict[str, object]:
    observation = {
        "schema": "merlin.target-state-observation.v1",
        "authority": "target",
        "name": "physical_state_identity",
        "role": "state_identity",
        "phase": phase,
        "binding_digest": _digest({"identity": phase}),
        "selected_digest": _digest(value),
        "selected_value": value,
    }
    return {**observation, "observation_digest": _digest(observation)}


def _observation(
    name: str,
    phase: str,
    value: object,
    *,
    role: str = "score",
    trust_class: str | None = "host_observed",
    value_type: str = "integer",
    preferred_direction: str | None = "increase",
    **definition: object,
) -> dict[str, object]:
    observation = {
        "schema": "merlin.target-state-observation.v1",
        "name": name,
        "role": role,
        "phase": phase,
        "authority": "target",
        "binding_digest": _digest({"name": name, "phase": phase}),
        "selected_digest": _digest(value),
        "selected_value": value,
        "value_type": value_type,
        "trust_class": trust_class,
        "preferred_direction": preferred_direction,
        **definition,
    }
    return {**observation, "observation_digest": _digest(observation)}


def test_host_observed_typed_bounded_monotonic_progress_is_authoritative() -> None:
    observations = [
        _identity("baseline", "run-1"),
        _identity("result", "run-1"),
        _observation(
            "score",
            "baseline",
            10,
            minimum_value=0,
            maximum_value=100,
            monotonicity="nondecreasing",
        ),
        _observation(
            "score",
            "result",
            25,
            minimum_value=0,
            maximum_value=100,
            monotonicity="nondecreasing",
        ),
    ]

    first = evaluate_state_transition(observations)
    second = evaluate_state_transition(tuple(reversed(observations)))

    assert first == second
    assert first["identity_match"] is True
    assert first["outcome"] == "progress"
    assert first["disposition_rule"] == "validated_corroborated_observations"
    assert first["validation_errors"] == []
    assert first["quarantined_observations"] == []


def test_target_reported_progress_requires_independent_corroboration() -> None:
    observations = [
        _identity("baseline", "run-1"),
        _identity("result", "run-1"),
        _observation("target_score", "baseline", 10, trust_class="target_reported"),
        _observation("target_score", "result", 25, trust_class="target_reported"),
    ]

    transition = evaluate_state_transition(observations)

    assert transition["outcome"] == "unclear"
    assert transition["quarantined_observations"] == ["target_score"]


def test_target_reported_progress_can_be_corroborated_by_host_observation() -> None:
    observations = [
        _identity("baseline", "run-1"),
        _identity("result", "run-1"),
        _observation("target_score", "baseline", 10, trust_class="target_reported"),
        _observation("target_score", "result", 25, trust_class="target_reported"),
        _observation("host_score", "baseline", 10),
        _observation("host_score", "result", 25),
    ]
    rules = [
        {
            "name": "score_agreement",
            "observation_names": ["target_score", "host_score"],
            "minimum_agreement": 2,
        }
    ]

    transition = evaluate_state_transition(observations, rules)

    assert transition["outcome"] == "progress"
    assert transition["quarantined_observations"] == []
    assert transition["corroboration"] == [
        {
            "name": "score_agreement",
            "observation_names": ["target_score", "host_score"],
            "minimum_agreement": 2,
            "agreed_status": "progress",
            "agreement": 2,
            "independent_source_present": True,
            "satisfied": True,
        }
    ]


def test_same_direction_with_different_values_does_not_corroborate() -> None:
    observations = [
        _identity("baseline", "run-1"),
        _identity("result", "run-1"),
        _observation("target_score", "baseline", 10, trust_class="target_reported"),
        _observation("target_score", "result", 25, trust_class="target_reported"),
        _observation("host_score", "baseline", 100),
        _observation("host_score", "result", 125),
    ]

    transition = evaluate_state_transition(
        observations,
        [
            {
                "name": "score_agreement",
                "observation_names": ["target_score", "host_score"],
                "minimum_agreement": 2,
            }
        ],
    )

    assert transition["outcome"] == "progress"
    assert transition["quarantined_observations"] == ["target_score"]
    assert transition["corroboration"][0]["satisfied"] is False


def test_forged_observation_envelope_is_quarantined() -> None:
    forged = _observation("score", "result", 25)
    forged["selected_value"] = 100
    transition = evaluate_state_transition(
        [
            _identity("baseline", "run-1"),
            _identity("result", "run-1"),
            _observation("score", "baseline", 10),
            forged,
        ]
    )

    assert transition["outcome"] == "unclear"
    assert "score:result:digest_mismatch" in transition["validation_errors"]
    assert "score:result:observation_digest_mismatch" in transition["validation_errors"]


@pytest.mark.parametrize("conflict_kind", ["terminal", "direction"])
def test_conflicting_trusted_observations_are_unclear(conflict_kind: str) -> None:
    observations = [_identity("baseline", "run-1"), _identity("result", "run-1")]
    if conflict_kind == "terminal":
        observations.extend(
            [
                _observation(
                    "success_state",
                    "baseline",
                    "active",
                    role="terminal_status",
                    value_type="string",
                    preferred_direction=None,
                    success_values=["won"],
                ),
                _observation(
                    "success_state",
                    "result",
                    "won",
                    role="terminal_status",
                    value_type="string",
                    preferred_direction=None,
                    success_values=["won"],
                ),
                _observation(
                    "failure_state",
                    "baseline",
                    "active",
                    role="terminal_status",
                    value_type="string",
                    preferred_direction=None,
                    failure_values=["lost"],
                ),
                _observation(
                    "failure_state",
                    "result",
                    "lost",
                    role="terminal_status",
                    value_type="string",
                    preferred_direction=None,
                    failure_values=["lost"],
                ),
            ]
        )
    else:
        observations.extend(
            [
                _observation("progress_score", "baseline", 10),
                _observation("progress_score", "result", 20),
                _observation("regression_score", "baseline", 20),
                _observation("regression_score", "result", 10),
            ]
        )

    transition = evaluate_state_transition(observations)

    assert transition["outcome"] == "unclear"
    assert transition["conflict"] is True
    assert transition["disposition_rule"] == "trusted_observations_conflict"


@pytest.mark.parametrize(
    ("baseline", "result", "definition", "error"),
    [
        (10, "25", {}, "score:result:type_mismatch"),
        (10, 125, {"maximum_value": 100}, "score:result:above_maximum"),
        (
            25,
            10,
            {"monotonicity": "nondecreasing"},
            "score:monotonicity_violation",
        ),
    ],
)
def test_hostile_typed_bounds_and_monotonic_claims_are_quarantined(
    baseline: object,
    result: object,
    definition: dict[str, object],
    error: str,
) -> None:
    observations = [
        _identity("baseline", "run-1"),
        _identity("result", "run-1"),
        _observation("score", "baseline", baseline, **definition),
        _observation("score", "result", result, **definition),
    ]

    transition = evaluate_state_transition(observations)

    assert transition["outcome"] == "unclear"
    assert error in transition["validation_errors"]
    assert transition["quarantined_observations"] == ["score"]


def test_conflicting_duplicate_source_and_identity_are_never_authoritative() -> None:
    observations = [
        _identity("baseline", "run-1"),
        _identity("result", "run-2"),
        _observation("score", "baseline", 10),
        _observation("score", "baseline", 11),
        _observation("score", "result", 25),
    ]

    transition = evaluate_state_transition(observations)

    assert transition == evaluate_state_transition(tuple(reversed(observations)))
    assert transition["identity_match"] is False
    assert transition["outcome"] == "unclear"
    assert "score:baseline:cross_source_mismatch" in transition["validation_errors"]
    assert "score" in transition["quarantined_observations"]


def test_legacy_missing_trust_and_type_remains_uncertain() -> None:
    transition = evaluate_state_transition(
        [
            _identity("baseline", "run-1"),
            _identity("result", "run-1"),
            _observation(
                "score",
                "baseline",
                10,
                trust_class=None,
                value_type="legacy",
            ),
            _observation(
                "score",
                "result",
                25,
                trust_class=None,
                value_type="legacy",
            ),
        ]
    )

    assert transition["outcome"] == "unclear"
    assert "score:missing_or_invalid_trust_class" in transition["validation_errors"]
    assert "score:missing_or_invalid_value_type" in transition["validation_errors"]
