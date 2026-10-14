"""Deterministic, read-only projection of durable target-state observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from .effects import ToolEffectRecord, ToolEffectState

_VISIBLE_ROLES = {
    "progress_ordinal": 0,
    "score": 1,
    "budget_remaining": 2,
    "terminal_status": 3,
    "custom": 4,
}
_VALUE_TYPES = {"integer", "number", "string", "boolean"}


def project_target_progress(
    effects: Sequence[ToolEffectRecord],
    *,
    campaign_id: str,
    target_id: str,
    now: datetime | None = None,
    history_truncated: bool = False,
) -> dict[str, object]:
    """Reduce immutable target effects into one compact, evidence-backed snapshot."""

    clock = now or datetime.now(UTC)
    ordered = sorted(
        (
            effect
            for effect in effects
            if effect.campaign_id == campaign_id
            and effect.target_id == target_id
            and effect.state in {ToolEffectState.ACKNOWLEDGED, ToolEffectState.RECONCILED}
        ),
        key=lambda effect: (
            effect.updated_at or effect.created_at or datetime.min.replace(tzinfo=UTC),
            effect.idempotency_key,
        ),
    )
    observations: list[tuple[datetime | None, str | None, str, Mapping[str, object]]] = []
    transitions: list[tuple[datetime | None, str, Mapping[str, object]]] = []
    invalid_count = 0
    target_exchange_count = 0
    for effect in ordered:
        metadata = effect.result_metadata
        exchange_values = metadata.get("current_target_exchanges", metadata.get("target_exchanges"))
        exchanges = (
            tuple(item for item in exchange_values if isinstance(item, Mapping))
            if isinstance(exchange_values, (list, tuple))
            else ()
        )
        target_exchange_count += sum(item.get("interaction") is True for item in exchanges)
        candidates = [
            item
            for item in _mapping_sequence(metadata.get("state_observations"))
        ]
        for exchange in exchanges:
            candidates.extend(_mapping_sequence(exchange.get("state_observations")))
        identity = next(
            (
                str(item["selected_digest"])
                for item in reversed(candidates)
                if item.get("role") == "state_identity"
                and isinstance(item.get("selected_digest"), str)
            ),
            None,
        )
        seen: set[str] = set()
        for item in candidates:
            digest = item.get("observation_digest")
            dedupe_key = str(digest or _digest_json(dict(item)))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            if not _valid_observation(item):
                invalid_count += 1
                continue
            observations.append(
                (effect.updated_at or effect.created_at, identity, effect.call_id, item)
            )
        transition = metadata.get("target_state_transition")
        if isinstance(transition, Mapping):
            transitions.append((effect.updated_at or effect.created_at, effect.call_id, transition))

    active_identity = next(
        (identity for _at, identity, _call, _item in reversed(observations) if identity), None
    )
    selected: dict[str, tuple[datetime | None, str, Mapping[str, object]]] = {}
    conflicts: list[dict[str, object]] = []
    for observed_at, identity, call_id, item in observations:
        if item.get("role") == "state_identity":
            continue
        if active_identity is not None and identity not in {None, active_identity}:
            continue
        name = str(item["name"])
        prior = selected.get(name)
        if (
            prior is not None
            and prior[1] == call_id
            and prior[2].get("phase") == item.get("phase")
            and prior[2].get("selected_digest") != item.get("selected_digest")
        ):
            conflicts.append(
                {
                    "name": name,
                    "previous_digest": prior[2].get("selected_digest"),
                    "latest_digest": item.get("selected_digest"),
                }
            )
        selected[name] = (observed_at, call_id, item)

    fields = tuple(
        _field_projection(name, value)
        for name, value in sorted(
            selected.items(),
            key=lambda pair: (
                _VISIBLE_ROLES.get(str(pair[1][2].get("role")), 99),
                pair[0],
            ),
        )
    )
    latest_at = max(
        (observed_at for observed_at, _call, _item in selected.values() if observed_at is not None),
        default=None,
    )
    age_seconds = max(0.0, (clock - latest_at).total_seconds()) if latest_at is not None else None
    freshness = (
        "unknown"
        if latest_at is None
        else "current"
        if age_seconds is not None and age_seconds <= 300
        else "stale"
    )
    limitations: list[str] = []
    if not fields:
        limitations.append("Target state has not been discovered from reviewed observations yet.")
    if observations and active_identity is None:
        limitations.append("Physical target-state identity is not established.")
    if invalid_count:
        limitations.append(f"{invalid_count} invalid state observation(s) were excluded.")
    if history_truncated:
        limitations.append("Older target evidence is outside this live projection window.")
    latest_transition = _latest_transition(transitions)
    body: dict[str, object] = {
        "schema": "merlin.target-progress.v1",
        "campaign_id": campaign_id,
        "target_id": target_id,
        "status": "observed" if fields else "unknown",
        "freshness": freshness,
        "observed_at": latest_at.isoformat() if latest_at is not None else None,
        "age_seconds": age_seconds,
        "target_exchange_count": target_exchange_count,
        "history_truncated": history_truncated,
        "current_state": fields,
        "latest_transition": latest_transition,
        "conflicts": tuple(conflicts[-8:]),
        "limitations": tuple(limitations),
    }
    body["snapshot_revision"] = _digest_json(
        {key: value for key, value in body.items() if key not in {"age_seconds", "freshness"}}
    )
    return body


def _field_projection(
    name: str, value: tuple[datetime | None, str, Mapping[str, object]]
) -> dict[str, object]:
    observed_at, call_id, item = value
    return {
        "name": name,
        "label": _humanize(name),
        "role": item["role"],
        "value": item["selected_value"],
        "value_type": item["value_type"],
        "trust_class": item.get("trust_class", "target_reported"),
        "source_call_id": call_id,
        "observation_digest": item["observation_digest"],
        "observed_at": observed_at.isoformat() if observed_at is not None else None,
        **({"minimum_value": item["minimum_value"]} if "minimum_value" in item else {}),
        **({"maximum_value": item["maximum_value"]} if "maximum_value" in item else {}),
    }


def _latest_transition(
    transitions: Sequence[tuple[datetime | None, str, Mapping[str, object]]],
) -> dict[str, object] | None:
    if not transitions:
        return None
    observed_at, call_id, transition = transitions[-1]
    return {
        "outcome": str(transition.get("outcome") or "unclear"),
        "identity_match": transition.get("identity_match"),
        "source_call_id": call_id,
        "observed_at": observed_at.isoformat() if observed_at is not None else None,
        "observations": tuple(
            {
                key: item.get(key)
                for key in ("name", "role", "status", "before_value", "after_value")
                if key in item
            }
            for item in _mapping_sequence(transition.get("observations"))
        ),
    }


def _valid_observation(item: Mapping[str, object]) -> bool:
    if item.get("schema") != "merlin.target-state-observation.v1":
        return False
    if item.get("authority") != "target" or item.get("role") not in {
        "state_identity",
        *_VISIBLE_ROLES,
    }:
        return False
    if not isinstance(item.get("name"), str) or not item.get("name"):
        return False
    if item.get("phase") not in {"baseline", "result"}:
        return False
    if item.get("value_type") not in _VALUE_TYPES or "selected_value" not in item:
        return False
    if not _matches_value_type(item["selected_value"], str(item["value_type"])):
        return False
    if item.get("selected_digest") != _digest_json(item["selected_value"]):
        return False
    unsigned = dict(item)
    observed_digest = unsigned.pop("observation_digest", None)
    return observed_digest == _digest_json(unsigned)


def _matches_value_type(value: object, value_type: str) -> bool:
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return value_type == "string" and isinstance(value, str)


def _mapping_sequence(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _humanize(name: str) -> str:
    return " ".join(part for part in name.replace("-", "_").split("_") if part).title() or name


def _digest_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
