"""Deterministic validation and comparison for reviewed target state transitions."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence

_ROLES = {
    "state_identity",
    "progress_ordinal",
    "score",
    "budget_remaining",
    "terminal_status",
    "custom",
}
_VALUE_TYPES = {"integer", "number", "string", "boolean"}
_TRUSTED_CLASSES = {"host_observed", "independent_authority"}
_STATUSES = {"success", "failure", "progress", "regression", "unchanged", "unclear"}


def build_state_observation(
    *,
    name: str,
    role: str,
    phase: str,
    step_id: str,
    evaluation_root: str,
    pointer: str,
    binding_context: Mapping[str, object],
    selected_value: object = None,
    selected_available: bool = True,
    preferred_direction: str | None = None,
    success_values: Sequence[object] = (),
    failure_values: Sequence[object] = (),
    evidence_refs: Sequence[str] = (),
    value_type: str | None = None,
    minimum_value: int | float | None = None,
    maximum_value: int | float | None = None,
    monotonicity: str | None = None,
    trust_class: str | None = None,
) -> dict[str, object]:
    """Build the canonical, content-addressed observation envelope used by every transport."""

    binding = {
        **dict(binding_context),
        "step_id": step_id,
        "evaluation_root": evaluation_root,
        "pointer": pointer,
        "phase": phase,
    }
    selected_digest = _digest_json(selected_value) if selected_available else None
    observation: dict[str, object] = {
        "schema": "merlin.target-state-observation.v1",
        "authority": "target",
        "name": name,
        "role": role,
        "phase": phase,
        "step_id": step_id,
        "evaluation_root": evaluation_root,
        "pointer": pointer,
        "binding_digest": _digest_json(binding),
        "selected_digest": selected_digest,
        **(
            {"selected_value": selected_value}
            if selected_available and _json_scalar(selected_value)
            else {}
        ),
        **({"preferred_direction": preferred_direction} if preferred_direction is not None else {}),
        **({"success_values": list(success_values)} if success_values else {}),
        **({"failure_values": list(failure_values)} if failure_values else {}),
        **({"evidence_refs": list(evidence_refs)} if evidence_refs else {}),
        **({"value_type": value_type} if value_type is not None else {}),
        **({"minimum_value": minimum_value} if minimum_value is not None else {}),
        **({"maximum_value": maximum_value} if maximum_value is not None else {}),
        **({"monotonicity": monotonicity} if monotonicity is not None else {}),
        **({"trust_class": trust_class} if trust_class is not None else {}),
    }
    return {**observation, "observation_digest": _digest_json(observation)}


def evaluate_state_transition(
    observations: Sequence[Mapping[str, object]],
    corroboration_rules: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Return a stable transition disposition from typed, attributable observations."""

    ordered_observations = sorted(observations, key=lambda item: _digest_json(dict(item)))
    identity_match, identity_errors = _identity_relationship(ordered_observations)
    grouped: dict[str, dict[str, list[Mapping[str, object]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for item in ordered_observations:
        name = item.get("name")
        phase = item.get("phase")
        if item.get("role") == "state_identity" or not isinstance(name, str):
            continue
        if phase in {"baseline", "result"}:
            grouped[name][str(phase)].append(item)

    compared = [
        _compare_observation(name, phases.get("baseline", ()), phases.get("result", ()))
        for name, phases in sorted(grouped.items())
    ]
    corroboration, corroborated = _evaluate_corroboration(compared, corroboration_rules)
    eligible_statuses: set[str] = set()
    quarantined: list[str] = []
    validation_errors = list(identity_errors)
    for item in compared:
        name = str(item["name"])
        errors = item.get("validation_errors", ())
        if isinstance(errors, list):
            validation_errors.extend(str(error) for error in errors)
        trust_class = item.get("trust_class")
        trusted = trust_class in _TRUSTED_CLASSES or name in corroborated
        status = str(item.get("status") or "unclear")
        if errors or not trusted:
            quarantined.append(name)
            continue
        if status in _STATUSES:
            eligible_statuses.add(status)

    conflict = {"success", "failure"} <= eligible_statuses or {
        "progress",
        "regression",
    } <= eligible_statuses
    outcome = _outcome(eligible_statuses, conflict=conflict)
    if identity_match is not True and compared:
        outcome = "unclear"
    unique_errors = sorted(set(validation_errors))
    disposition_rule = (
        "identity_unproven"
        if identity_match is not True and compared
        else "trusted_observations_conflict"
        if conflict
        else "validated_corroborated_observations"
        if outcome != "unclear"
        else "no_valid_corroborated_observation"
    )
    return {
        "schema": "merlin.target-state-transition.v2",
        "authority": "target",
        "identity_match": identity_match,
        "outcome": outcome,
        "observations": compared,
        "disposition_rule": disposition_rule,
        "validation_errors": unique_errors,
        "quarantined_observations": sorted(set(quarantined)),
        "corroboration": corroboration,
        **({"conflict": True} if conflict else {}),
    }


def _identity_relationship(
    observations: Sequence[Mapping[str, object]],
) -> tuple[bool | None, list[str]]:
    phases = {
        phase: [
            item
            for item in observations
            if item.get("role") == "state_identity" and item.get("phase") == phase
        ]
        for phase in ("baseline", "result")
    }
    errors: list[str] = []
    for phase, values in phases.items():
        for value in values:
            errors.extend(_observation_envelope_errors("physical_state_identity", phase, value))
        if len(values) > 1 and len({item.get("selected_digest") for item in values}) > 1:
            errors.append(f"physical_state_identity:{phase}:cross_source_mismatch")
    baseline = phases["baseline"][0] if phases["baseline"] else None
    result = phases["result"][-1] if phases["result"] else None
    baseline_digest = baseline.get("selected_digest") if baseline is not None else None
    result_digest = result.get("selected_digest") if result is not None else None
    if not _sha256_digest(baseline_digest):
        errors.append("physical_state_identity:baseline:invalid_digest")
        return None, errors
    if result is None:
        return (True, errors) if not errors else (None, errors)
    if not _sha256_digest(result_digest):
        errors.append("physical_state_identity:result:invalid_digest")
        return None, errors
    return (baseline_digest == result_digest, errors)


def _compare_observation(
    name: str,
    baselines: Sequence[Mapping[str, object]],
    results: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    errors: list[str] = []
    if len(baselines) != 1:
        errors.append(f"{name}:baseline:expected_one_source")
    if len(results) != 1:
        errors.append(f"{name}:result:expected_one_source")
    if len({item.get("selected_digest") for item in baselines}) > 1:
        errors.append(f"{name}:baseline:cross_source_mismatch")
    if len({item.get("selected_digest") for item in results}) > 1:
        errors.append(f"{name}:result:cross_source_mismatch")
    baseline = baselines[0] if baselines else None
    result = results[-1] if results else None
    exemplar = result or baseline or {}
    definition_keys = (
        "role",
        "value_type",
        "minimum_value",
        "maximum_value",
        "monotonicity",
        "preferred_direction",
        "trust_class",
        "success_values",
        "failure_values",
    )
    if baseline is not None and result is not None:
        for key in definition_keys:
            if baseline.get(key) != result.get(key):
                errors.append(f"{name}:{key}:cross_source_mismatch")

    for phase, items in (("baseline", baselines), ("result", results)):
        for item in items:
            errors.extend(_observation_envelope_errors(name, phase, item))

    role = exemplar.get("role")
    value_type = exemplar.get("value_type")
    trust_class = exemplar.get("trust_class")
    before_digest = baseline.get("selected_digest") if baseline is not None else None
    after_digest = result.get("selected_digest") if result is not None else None
    before_value = baseline.get("selected_value") if baseline is not None else None
    after_value = result.get("selected_value") if result is not None else None
    if role not in _ROLES or role == "state_identity":
        errors.append(f"{name}:invalid_role")
    if value_type not in _VALUE_TYPES:
        errors.append(f"{name}:missing_or_invalid_value_type")
    if trust_class not in {"target_reported", *_TRUSTED_CLASSES}:
        errors.append(f"{name}:missing_or_invalid_trust_class")
    for phase, digest, value in (
        ("baseline", before_digest, before_value),
        ("result", after_digest, after_value),
    ):
        if not _sha256_digest(digest):
            errors.append(f"{name}:{phase}:invalid_digest")
        elif not _value_matches_type(value, value_type):
            errors.append(f"{name}:{phase}:type_mismatch")
        elif digest != _digest_json(value):
            errors.append(f"{name}:{phase}:digest_mismatch")
        errors.extend(_bound_errors(name, phase, value, exemplar))

    monotonicity = exemplar.get("monotonicity")
    preferred_direction = exemplar.get("preferred_direction")
    if monotonicity not in {None, "nondecreasing", "nonincreasing"}:
        errors.append(f"{name}:invalid_monotonicity")
    if monotonicity is not None and _ordered_pair(before_value, after_value):
        if monotonicity == "nondecreasing" and after_value < before_value:
            errors.append(f"{name}:monotonicity_violation")
        elif monotonicity == "nonincreasing" and after_value > before_value:
            errors.append(f"{name}:monotonicity_violation")
    if preferred_direction in {"increase", "decrease"} and value_type not in {
        "integer",
        "number",
    }:
        errors.append(f"{name}:ordered_non_numeric_value")
    if (
        preferred_direction == "increase"
        and monotonicity == "nonincreasing"
        or preferred_direction == "decrease"
        and monotonicity == "nondecreasing"
    ):
        errors.append(f"{name}:direction_monotonicity_conflict")

    success_values = tuple(exemplar.get("success_values", ()))
    failure_values = tuple(exemplar.get("failure_values", ()))
    for expected in (*success_values, *failure_values):
        if not _value_matches_type(expected, value_type):
            errors.append(f"{name}:terminal_value_type_mismatch")
    if any(
        _typed_equal(success, failure) for success in success_values for failure in failure_values
    ):
        errors.append(f"{name}:terminal_values_overlap")
    if errors:
        status = "unclear"
    elif any(_typed_equal(after_value, expected) for expected in success_values):
        status = "success"
    elif any(_typed_equal(after_value, expected) for expected in failure_values):
        status = "failure"
    elif _typed_equal(before_value, after_value):
        status = "unchanged"
    elif preferred_direction in {"increase", "decrease"} and _ordered_pair(
        before_value, after_value
    ):
        moved_preferred = (
            after_value > before_value
            if preferred_direction == "increase"
            else after_value < before_value
        )
        status = "progress" if moved_preferred else "regression"
    else:
        status = "unclear"
    return {
        "name": name,
        "role": role if isinstance(role, str) else "custom",
        "status": status,
        "trust_class": trust_class,
        "before_digest": before_digest,
        "after_digest": after_digest,
        **({"before_value": before_value} if _sha256_digest(before_digest) else {}),
        **({"after_value": after_value} if _sha256_digest(after_digest) else {}),
        **({"preferred_direction": preferred_direction} if preferred_direction is not None else {}),
        **({"monotonicity": monotonicity} if monotonicity is not None else {}),
        **({"minimum_value": exemplar.get("minimum_value")} if "minimum_value" in exemplar else {}),
        **({"maximum_value": exemplar.get("maximum_value")} if "maximum_value" in exemplar else {}),
        **(
            {"baseline_binding_digest": baseline.get("binding_digest")}
            if baseline is not None
            else {}
        ),
        **({"result_binding_digest": result.get("binding_digest")} if result is not None else {}),
        "validation_errors": sorted(set(errors)),
    }


def _evaluate_corroboration(
    compared: Sequence[Mapping[str, object]],
    rules: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], set[str]]:
    by_name = {str(item.get("name")): item for item in compared}
    receipts: list[dict[str, object]] = []
    corroborated: set[str] = set()
    for rule in sorted(rules, key=lambda item: str(item.get("name") or "")):
        name = str(rule.get("name") or "")
        names_value = rule.get("observation_names")
        names = (
            tuple(str(item) for item in names_value)
            if isinstance(names_value, Sequence) and not isinstance(names_value, (str, bytes))
            else ()
        )
        minimum = rule.get("minimum_agreement")
        fingerprints = [
            (
                str(by_name[item].get("status") or "unclear"),
                by_name[item].get("before_digest"),
                by_name[item].get("after_digest"),
            )
            for item in names
            if item in by_name
            and not by_name[item].get("validation_errors")
            and by_name[item].get("status") not in {None, "unclear"}
        ]
        counts = Counter(fingerprints)
        (agreed_fingerprint, agreement) = (
            counts.most_common(1)[0] if counts else (("unclear", None, None), 0)
        )
        agreed_status = agreed_fingerprint[0]
        agreeing_names = [
            item
            for item in names
            if item in by_name
            and (
                by_name[item].get("status"),
                by_name[item].get("before_digest"),
                by_name[item].get("after_digest"),
            )
            == agreed_fingerprint
        ]
        independent = any(
            by_name[item].get("trust_class") in _TRUSTED_CLASSES for item in agreeing_names
        )
        satisfied = (
            isinstance(minimum, int)
            and not isinstance(minimum, bool)
            and agreement >= minimum
            and independent
            and agreed_status != "unclear"
        )
        if satisfied:
            corroborated.update(agreeing_names)
        receipts.append(
            {
                "name": name,
                "observation_names": list(names),
                "minimum_agreement": minimum,
                "agreed_status": agreed_status,
                "agreement": agreement,
                "independent_source_present": independent,
                "satisfied": satisfied,
            }
        )
    return receipts, corroborated


def _observation_envelope_errors(
    name: str,
    phase: str,
    observation: Mapping[str, object],
) -> list[str]:
    errors: list[str] = []
    if observation.get("schema") != "merlin.target-state-observation.v1":
        errors.append(f"{name}:{phase}:invalid_schema")
    if observation.get("authority") != "target":
        errors.append(f"{name}:{phase}:invalid_authority")
    if not _sha256_digest(observation.get("binding_digest")):
        errors.append(f"{name}:{phase}:invalid_binding_digest")
    selected_digest = observation.get("selected_digest")
    if _sha256_digest(selected_digest) and "selected_value" in observation:
        if selected_digest != _digest_json(observation.get("selected_value")):
            errors.append(f"{name}:{phase}:digest_mismatch")
    observed_digest = observation.get("observation_digest")
    digest_source = dict(observation)
    digest_source.pop("observation_digest", None)
    if observed_digest != _digest_json(digest_source):
        errors.append(f"{name}:{phase}:observation_digest_mismatch")
    return errors


def _bound_errors(
    name: str, phase: str, value: object, definition: Mapping[str, object]
) -> list[str]:
    errors: list[str] = []
    minimum = definition.get("minimum_value")
    maximum = definition.get("maximum_value")
    if minimum is not None and not _finite_number(minimum):
        errors.append(f"{name}:invalid_minimum")
    if maximum is not None and not _finite_number(maximum):
        errors.append(f"{name}:invalid_maximum")
    if _finite_number(minimum) and _finite_number(maximum) and minimum > maximum:
        errors.append(f"{name}:invalid_bounds")
    if _finite_number(value):
        if _finite_number(minimum) and value < minimum:
            errors.append(f"{name}:{phase}:below_minimum")
        if _finite_number(maximum) and value > maximum:
            errors.append(f"{name}:{phase}:above_maximum")
    return errors


def _outcome(statuses: set[str], *, conflict: bool) -> str:
    if conflict:
        return "unclear"
    for status, outcome in (
        ("success", "confirmed"),
        ("failure", "refuted"),
        ("progress", "progress"),
        ("regression", "regression"),
    ):
        if status in statuses:
            return outcome
    return "unclear"


def _value_matches_type(value: object, value_type: object) -> bool:
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "string":
        return isinstance(value, str)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "number":
        return _finite_number(value)
    return False


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _json_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _ordered_pair(before: object, after: object) -> bool:
    return _finite_number(before) and _finite_number(after)


def _typed_equal(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def _sha256_digest(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        return False
    try:
        int(value[7:], 16)
    except ValueError:
        return False
    return True


def _digest_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
