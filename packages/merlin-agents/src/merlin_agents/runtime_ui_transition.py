from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from itertools import islice

_TRANSITION_SCHEMA = "merlin.target-ui-transition.v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_STATE_ROLES = {
    "state_identity",
    "progress_ordinal",
    "score",
    "budget_remaining",
    "terminal_status",
    "custom",
}
_STATE_BOUNDARIES = {"stage", "success", "failure"}


def _items(value: object, limit: int) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(islice(value, limit))


def _control(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    fields = {
        key: value.get(key)
        for key in ("role", "name", "frame_ref", "input_type", "surface_scope")
    }
    limits = {
        "role": 64,
        "name": 256,
        "frame_ref": 256,
        "input_type": 64,
        "surface_scope": 32,
    }
    if any(
        item is not None and (not isinstance(item, str) or len(item) > limits[key])
        for key, item in fields.items()
    ) or not all(fields[key] for key in ("role", "name")) or fields[
        "surface_scope"
    ] not in {
        None,
        "document",
        "transcript",
        "status",
        "dialog",
        "modal",
        "top_layer",
    }:
        return None
    result = {key: item for key, item in fields.items() if item is not None}
    result.update(
        {
            key: True
            for key in ("disabled", "read_only", "occluded")
            if value.get(key) is True
        }
    )
    return result


def _state_scalar(value: object) -> bool | int | float | str | None:
    if type(value) is bool:
        return value
    if type(value) is int and -(10**128) < value < 10**128:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, str) and len(value) <= 512:
        return value
    return None


def _state_change(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    name = value.get("name")
    role = value.get("role")
    boundary = value.get("boundary")
    before = _state_scalar(value.get("before"))
    after = _state_scalar(value.get("after"))
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 128
        or not isinstance(role, str)
        or role not in _STATE_ROLES
        or (
            boundary is not None
            and (not isinstance(boundary, str) or boundary not in _STATE_BOUNDARIES)
        )
        or before is None
        or after is None
        or type(before) is type(after)
        and before == after
    ):
        return None
    return {
        "name": name,
        "role": role,
        "before": before,
        "after": after,
        **({"boundary": boundary} if boundary is not None else {}),
    }


def normalize_target_ui_transition(
    metadata: Mapping[str, object],
) -> dict[str, object] | None:
    raw = metadata.get("target_ui_transition")
    if not isinstance(raw, Mapping) or (
        raw.get("schema") != _TRANSITION_SCHEMA
        or raw.get("material") is not True
        or raw.get("reobservation_recommended") is not True
        or type(raw.get("adapter_stale")) is not bool
    ):
        return None
    before, after = raw.get("before_digest"), raw.get("after_digest")
    if not all(isinstance(item, str) and _DIGEST.fullmatch(item) for item in (before, after)):
        return None
    if before == after:
        return None

    def strings(key: str, count: int, length: int) -> list[str]:
        return [item[:length] for item in _items(raw.get(key), count) if isinstance(item, str)]

    def controls(key: str) -> list[dict[str, object]]:
        return [item for value in _items(raw.get(key), 20) if (item := _control(value))]

    raw_state_changes = _items(raw.get("reviewed_state_changes"), 33)
    if len(raw_state_changes) > 32 and raw.get("state_changes_truncated") is not True:
        return None
    reviewed_state_changes = [_state_change(value) for value in raw_state_changes[:32]]
    if any(value is None for value in reviewed_state_changes):
        return None

    raw_title_change = raw.get("title_change")
    title_change: dict[str, str] | None = None
    if raw_title_change is not None:
        if not isinstance(raw_title_change, Mapping) or any(
            not isinstance(raw_title_change.get(key), str)
            for key in ("before", "after")
        ):
            return None
        title_change = {
            key: str(raw_title_change[key])[:512]
            for key in ("before", "after")
        }

    changed_dimensions = strings("changed_dimensions", 12, 64)
    if "reviewed_state" in changed_dimensions and not any(
        isinstance(value, Mapping) and value.get("boundary") in _STATE_BOUNDARIES
        for value in reviewed_state_changes
    ):
        return None

    normalized: dict[str, object] = {
        "schema": _TRANSITION_SCHEMA,
        "material": True,
        "adapter_stale": raw["adapter_stale"],
        "before_digest": before,
        "after_digest": after,
        "changed_dimensions": changed_dimensions,
        "added_controls": controls("added_controls"),
        "removed_controls": controls("removed_controls"),
        "controls_truncated": raw.get("controls_truncated") is True,
        "evidence_refs": strings("evidence_refs", 8, 256),
        "reobservation_recommended": True,
    }
    if reviewed_state_changes:
        normalized["reviewed_state_changes"] = [
            value for value in reviewed_state_changes if value is not None
        ]
    if raw.get("state_changes_truncated") is True:
        normalized["state_changes_truncated"] = True
    if title_change is not None:
        normalized["title_change"] = title_change
    return normalized


def _proved_effect_free(metadata: Mapping[str, object], interactions: int, limit: int) -> bool:
    return bool(
        metadata.get("target_effect_attempted") is False
        and type(metadata.get("target_call_budget_charge")) is int
        and metadata.get("target_call_budget_charge") == 0
        and interactions == 0
        and limit == 0
    )


def strict_pre_effect_transition(
    metadata: Mapping[str, object],
    *,
    is_error: bool,
    interaction_count: int,
    limit_interaction_count: int,
) -> dict[str, object] | None:
    transition = normalize_target_ui_transition(metadata)
    valid = (
        is_error
        and transition is not None
        and transition["adapter_stale"] is True
        and _proved_effect_free(metadata, interaction_count, limit_interaction_count)
    )
    return transition if valid else None
