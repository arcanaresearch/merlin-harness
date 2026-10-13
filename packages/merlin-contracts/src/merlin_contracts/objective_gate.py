"""Transport-neutral evaluation of sealed objective prerequisite predicates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def evaluate_objective_gate_predicate(
    gate: Mapping[str, object],
    transition: Mapping[str, object],
) -> bool | None:
    """Evaluate one sealed gate against one validated authoritative transition.

    ``None`` means the transition cannot authoritatively settle the predicate.
    Callers must keep that result retryable or blocked according to the sealed
    gate policy; they must never treat it as success.
    """

    predicate = gate.get("success_predicate")
    if not isinstance(predicate, Mapping):
        return None
    if (
        transition.get("schema") != "merlin.target-state-transition.v2"
        or transition.get("authority") != "target"
        or transition.get("identity_match") is not True
        or transition.get("conflict") is True
        or _nonempty_sequence(transition.get("validation_errors"))
    ):
        return None

    name = predicate.get("observation_name")
    if not isinstance(name, str) or not name.strip():
        return None
    quarantined = transition.get("quarantined_observations", ())
    if isinstance(quarantined, Sequence) and not isinstance(quarantined, (str, bytes)):
        if name in quarantined:
            return None
    observations = transition.get("observations")
    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)):
        return None
    selected = [
        item for item in observations if isinstance(item, Mapping) and item.get("name") == name
    ]
    if len(selected) != 1 or _nonempty_sequence(selected[0].get("validation_errors")):
        return None

    observation = selected[0]
    operator = predicate.get("operator")
    expected = predicate.get("expected_values", ())
    if not isinstance(expected, Sequence) or isinstance(expected, (str, bytes)):
        return None
    before = observation.get("before_value")
    after = observation.get("after_value")
    if operator == "terminal_success":
        return observation.get("status") == "success"
    if operator in {"increases", "decreases"}:
        if not _ordered_pair(before, after):
            return None
        return after > before if operator == "increases" else after < before
    if after is None or not expected:
        return None
    if operator == "equals":
        return _typed_equal(after, expected[0])
    if operator == "one_of":
        return any(_typed_equal(after, value) for value in expected)
    if operator in {"at_least", "at_most"}:
        threshold = expected[0]
        if not _ordered_pair(threshold, after):
            return None
        return after >= threshold if operator == "at_least" else after <= threshold
    return None


def _nonempty_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and bool(value)


def _ordered_pair(left: object, right: object) -> bool:
    return all(
        isinstance(value, (int, float)) and not isinstance(value, bool) for value in (left, right)
    )


def _typed_equal(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right
