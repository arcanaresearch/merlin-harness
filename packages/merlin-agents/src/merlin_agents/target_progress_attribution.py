"""Merge durable campaign state attribution into the target progress view."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

_ROLE_ORDER = {
    "progress_ordinal": 0,
    "score": 1,
    "budget_remaining": 2,
    "terminal_status": 3,
    "custom": 4,
}
_TRUSTED_PROVENANCE = frozenset({"campaign", "campaign_restart", "preexisting"})
_UNDISCOVERED_LIMITATION = "Target state has not been discovered from reviewed observations yet."


def merge_target_progress_attributions(
    base: Mapping[str, object],
    journal: Sequence[Mapping[str, object]],
    *,
    campaign_id: str,
    target_id: str,
) -> dict[str, object]:
    """Add authoritative transition receipts that sit outside the effect window."""

    result = dict(base)
    selected: dict[str, dict[str, object]] = {}
    for state_field in base.get("current_state", ()):
        if isinstance(state_field, Mapping) and isinstance(state_field.get("name"), str):
            selected[str(state_field["name"])] = dict(state_field)

    latest_transition: dict[str, object] | None = None
    latest_attribution_at: datetime | None = None
    for row in journal:
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else row
        if not isinstance(payload, Mapping):
            continue
        if (
            payload.get("kind") != "target_state_attribution"
            or payload.get("schema") != "merlin.target-state-attribution.v1"
            or payload.get("provenance") not in _TRUSTED_PROVENANCE
            or payload.get("campaign_id") not in {None, campaign_id}
            or payload.get("target_id") not in {None, target_id}
        ):
            continue
        changes = payload.get("changes")
        if not isinstance(changes, (list, tuple)):
            continue
        observed_at = next(
            (
                parsed
                for candidate in (
                    payload.get("observed_at"),
                    payload.get("result_observed_at"),
                    row.get("created_at"),
                    row.get("occurred_at"),
                )
                if (parsed := _timestamp(candidate)) is not None
            ),
            None,
        )
        accepted_changes: list[dict[str, object]] = []
        for change in changes:
            if not isinstance(change, Mapping):
                continue
            name = change.get("name")
            if not isinstance(name, str) or not name or "after_value" not in change:
                continue
            value = change["after_value"]
            value_type = _value_type(value)
            if value_type is None:
                continue
            role = str(change.get("role") or "custom")
            if role not in _ROLE_ORDER:
                role = "custom"
            selected[name] = {
                "name": name,
                "label": _humanize(name),
                "role": role,
                "value": value,
                "value_type": value_type,
                "trust_class": (
                    "reviewed_reconnaissance"
                    if payload.get("source_kind") == "reconnaissance"
                    else "target_reported"
                ),
                "source_call_id": payload.get("target_call_id") or payload.get("attribution_id"),
                "observation_digest": payload.get("transition_digest")
                or payload.get("attribution_id"),
                "observed_at": observed_at.isoformat() if observed_at is not None else None,
            }
            accepted_changes.append(
                {
                    key: change.get(key)
                    for key in ("name", "role", "status", "before_value", "after_value")
                    if key in change
                }
            )
        if accepted_changes:
            latest_transition = {
                "outcome": str(payload.get("transition_outcome") or "unclear"),
                "identity_match": None,
                "source_call_id": payload.get("target_call_id") or payload.get("attribution_id"),
                "observed_at": observed_at.isoformat() if observed_at is not None else None,
                "observations": tuple(accepted_changes),
            }
            if observed_at is not None:
                latest_attribution_at = observed_at

    if latest_transition is None:
        return result

    fields = tuple(
        sorted(
            selected.values(),
            key=lambda item: (
                _ROLE_ORDER.get(str(item.get("role")), 99),
                str(item.get("name") or ""),
            ),
        )
    )
    base_observed_at = _timestamp(base.get("observed_at"))
    observed_at = max(
        (value for value in (base_observed_at, latest_attribution_at) if value is not None),
        default=None,
    )
    age_seconds = (
        max(0.0, (datetime.now(UTC) - observed_at).total_seconds())
        if observed_at is not None
        else None
    )
    result.update(
        {
            "schema": "merlin.target-progress.v1",
            "campaign_id": campaign_id,
            "target_id": target_id,
            "status": "observed",
            "freshness": (
                "unknown"
                if observed_at is None
                else "current"
                if age_seconds is not None and age_seconds <= 300
                else "stale"
            ),
            "observed_at": observed_at.isoformat() if observed_at is not None else None,
            "age_seconds": age_seconds,
            "target_exchange_count": int(base.get("target_exchange_count") or 0),
            "history_truncated": base.get("history_truncated") is True,
            "current_state": fields,
            "latest_transition": latest_transition,
            "conflicts": tuple(base.get("conflicts", ())),
            "limitations": tuple(
                str(item)
                for item in base.get("limitations", ())
                if str(item) != _UNDISCOVERED_LIMITATION
            ),
        }
    )
    result["snapshot_revision"] = _digest(
        {
            key: value
            for key, value in result.items()
            if key not in {"age_seconds", "freshness", "snapshot_revision"}
        }
    )
    return result


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _value_type(value: object) -> str | None:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return None


def _humanize(name: str) -> str:
    return " ".join(part for part in name.replace("-", "_").split("_") if part).title() or name


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
