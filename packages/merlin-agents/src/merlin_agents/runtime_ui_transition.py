from __future__ import annotations

import hashlib
import json
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
_UNCHANGED_TRANSITION_FIELDS = {
    "schema",
    "material",
    "adapter_stale",
    "before_digest",
    "after_digest",
    "transition_digest",
    "changed_dimensions",
    "url_change",
    "title_change",
    "frame_change",
    "observation_kind_change",
    "added_controls",
    "removed_controls",
    "controls_truncated",
    "reviewed_state_changes",
    "state_changes_truncated",
    "reviewed_control_state_changes",
    "added_visible_text",
    "evidence_refs",
    "reobservation_recommended",
}


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


def _exact_unchanged_proof(
    raw: Mapping[str, object],
    *,
    before_digest: str,
    after_digest: str,
) -> bool:
    """Accept only the bounded, contradiction-free receipt emitted by BrowserTool."""

    if set(raw) != _UNCHANGED_TRANSITION_FIELDS:
        return False
    empty_list_fields = (
        "changed_dimensions",
        "added_controls",
        "removed_controls",
        "reviewed_state_changes",
        "reviewed_control_state_changes",
        "added_visible_text",
    )
    if any(type(raw.get(key)) is not list or raw.get(key) for key in empty_list_fields):
        return False
    if any(
        raw.get(key) is not None
        for key in ("url_change", "title_change", "frame_change", "observation_kind_change")
    ):
        return False
    if (
        raw.get("controls_truncated") is not False
        or raw.get("state_changes_truncated") is not False
    ):
        return False
    evidence_refs = raw.get("evidence_refs")
    if (
        type(evidence_refs) is not list
        or not 1 <= len(evidence_refs) <= 8
        or any(
            not isinstance(item, str) or not item or len(item) > 256
            for item in evidence_refs
        )
    ):
        return False
    encoded = json.dumps(
        [before_digest, after_digest], sort_keys=True, separators=(",", ":")
    ).encode()
    expected_transition_digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return raw.get("transition_digest") == expected_transition_digest


def normalize_target_ui_transition(
    metadata: Mapping[str, object],
) -> dict[str, object] | None:
    raw = metadata.get("target_ui_transition")
    if not isinstance(raw, Mapping) or raw.get("schema") != _TRANSITION_SCHEMA:
        return None
    material = raw.get("material")
    reobservation_recommended = raw.get("reobservation_recommended")
    adapter_stale = raw.get("adapter_stale")
    material_barrier = bool(
        material is True
        and reobservation_recommended is True
        and type(adapter_stale) is bool
    )
    unchanged_proof = bool(
        material is False
        and reobservation_recommended is False
        and adapter_stale is False
    )
    if not material_barrier and not unchanged_proof:
        return None
    before, after = raw.get("before_digest"), raw.get("after_digest")
    if not all(isinstance(item, str) and _DIGEST.fullmatch(item) for item in (before, after)):
        return None
    if material_barrier and before == after or unchanged_proof and before != after:
        return None
    if unchanged_proof and not _exact_unchanged_proof(
        raw,
        before_digest=before,
        after_digest=after,
    ):
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

    added_controls = controls("added_controls")
    removed_controls = controls("removed_controls")
    if unchanged_proof and (
        changed_dimensions
        or added_controls
        or removed_controls
        or reviewed_state_changes
        or raw_title_change is not None
        or raw.get("controls_truncated") is True
        or raw.get("state_changes_truncated") is True
    ):
        return None
    if unchanged_proof:
        return {
            "schema": _TRANSITION_SCHEMA,
            "material": False,
            "adapter_stale": False,
            "before_digest": before,
            "after_digest": after,
            "transition_digest": raw["transition_digest"],
            "changed_dimensions": [],
            "url_change": None,
            "title_change": None,
            "frame_change": None,
            "observation_kind_change": None,
            "added_controls": [],
            "removed_controls": [],
            "controls_truncated": False,
            "reviewed_state_changes": [],
            "state_changes_truncated": False,
            "reviewed_control_state_changes": [],
            "added_visible_text": [],
            "evidence_refs": strings("evidence_refs", 8, 256),
            "reobservation_recommended": False,
        }

    normalized: dict[str, object] = {
        "schema": _TRANSITION_SCHEMA,
        "material": material,
        "adapter_stale": adapter_stale,
        "before_digest": before,
        "after_digest": after,
        "changed_dimensions": changed_dimensions,
        "added_controls": added_controls,
        "removed_controls": removed_controls,
        "controls_truncated": raw.get("controls_truncated") is True,
        "evidence_refs": strings("evidence_refs", 8, 256),
        "reobservation_recommended": reobservation_recommended,
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
