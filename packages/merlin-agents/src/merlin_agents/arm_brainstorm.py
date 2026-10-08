"""Free-form brainstorm arm generator for campaign exploration.

Pure and injectable: this module never imports a live provider. Callers pass a
``generator`` callable that turns a prompt string into a list of candidate dicts,
which keeps the generator testable and provider-agnostic while the novelty and
deduplication policy stays in one place.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict

from jsonschema import ValidationError, validate

BRAINSTORM_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "technique_id": {"type": "string", "minLength": 1},
        "objective": {"type": "string", "minLength": 1},
        "semantic_id": {"type": "string", "minLength": 1},
        "strategy_id": {"type": "string", "minLength": 1},
        "rationale": {"type": "string"},
    },
    "required": ["technique_id", "objective", "semantic_id", "strategy_id"],
}

_MAX_CONTEXT_ITEMS = 20
_MAX_OBJECTIVE_CHARS = 500

Generator = Callable[[str], Sequence[Mapping[str, object]]]


def build_brainstorm_prompt(
    *,
    seeds: Sequence[object],
    receipts: Sequence[object],
    existing_objectives: Sequence[str],
    maximum: int,
) -> str:
    """Assemble a bounded prompt that asks for novel arms.

    The prompt names every existing objective under an explicit "do not repeat"
    list so the model has something concrete to exclude, grounds novelty in the
    seed taxonomy and committed receipts, and caps the requested output at
    ``maximum`` arms.
    """
    if maximum < 1:
        raise ValueError("brainstorm maximum must be positive")
    objectives = [
        _truncate(objective, _MAX_OBJECTIVE_CHARS)
        for objective in existing_objectives[:_MAX_CONTEXT_ITEMS]
        if objective and str(objective).strip()
    ]
    lines = [
        "You are proposing novel assessment arms for a reviewed target.",
        f"Propose at most {maximum} arms that are meaningfully different from one another.",
        "",
        "Existing objectives — do NOT repeat or closely paraphrase any of these:",
    ]
    lines.extend(f"- {objective}" for objective in objectives)
    lines.extend(
        [
            "",
            "Seed technique taxonomy (use as inspiration, not as a required answer):",
            json.dumps(_bounded(seeds), ensure_ascii=False),
            "",
            "Committed target receipts (ground novelty in observed evidence):",
            json.dumps(_bounded(receipts), ensure_ascii=False),
            "",
            "Output one JSON object per arm, each matching this schema:",
            json.dumps(BRAINSTORM_OUTPUT_SCHEMA, ensure_ascii=False),
        ]
    )
    return "\n".join(lines)


def generate_arms(
    *,
    seeds: Sequence[object],
    receipts: Sequence[object],
    existing_arms: Sequence[Mapping[str, object]],
    generator: Generator,
    maximum: int,
) -> tuple[dict[str, object], ...]:
    """Generate, validate, and identity-dedupe brainstorm arms.

    Each candidate dict is validated against :data:`BRAINSTORM_OUTPUT_SCHEMA`;
    invalid dicts are dropped. Text similarity is left for downstream ranking;
    it cannot suppress a distinct semantic/strategy identity. Arms are normalized to
    ``technique_id``, ``semantic_id``, ``strategy_id``, ``objective``,
    ``source="brainstorm"`` and, when present, ``rationale``. At most ``maximum``
    arms are returned.
    """
    if maximum < 1:
        raise ValueError("brainstorm maximum must be positive")
    existing_objectives = [
        str(arm.get("objective") or "").strip()
        for arm in existing_arms
        if str(arm.get("objective") or "").strip()
    ]
    prompt = build_brainstorm_prompt(
        seeds=seeds,
        receipts=receipts,
        existing_objectives=existing_objectives,
        maximum=maximum,
    )
    admitted: list[dict[str, object]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for raw in generator(prompt):
        candidate = _validated(raw)
        if candidate is None:
            continue
        key = (candidate["semantic_id"], candidate["technique_id"], candidate["strategy_id"])
        if key in seen_keys:
            continue
        arm: dict[str, object] = {
            "technique_id": candidate["technique_id"],
            "semantic_id": candidate["semantic_id"],
            "strategy_id": candidate["strategy_id"],
            "objective": candidate["objective"],
            "source": "brainstorm",
        }
        if candidate["rationale"]:
            arm["rationale"] = candidate["rationale"]
        admitted.append(arm)
        seen_keys.add(key)
        if len(admitted) >= maximum:
            break
    return tuple(admitted)


def _validated(raw: object) -> dict[str, str] | None:
    if not isinstance(raw, Mapping):
        return None
    instance = dict(raw)
    try:
        validate(instance=instance, schema=BRAINSTORM_OUTPUT_SCHEMA)
    except ValidationError:
        return None
    semantic_id = str(instance["semantic_id"]).strip()
    strategy_id = str(instance["strategy_id"]).strip()
    objective = str(instance["objective"]).strip()
    technique_id = str(instance["technique_id"]).strip()
    if not (semantic_id and strategy_id and objective and technique_id):
        return None
    rationale = str(instance["rationale"]).strip() if instance.get("rationale") else ""
    return {
        "technique_id": technique_id,
        "objective": objective,
        "semantic_id": semantic_id,
        "strategy_id": strategy_id,
        "rationale": rationale,
    }


def _render(item: object) -> object:
    if isinstance(item, Mapping):
        return dict(item)
    if callable(getattr(item, "projection", None)):
        return item.projection()
    try:
        return asdict(item)  # type: ignore[arg-type]
    except TypeError:
        return vars(item) if hasattr(item, "__dict__") else str(item)


def _bounded(items: Sequence[object]) -> list[object]:
    return [_render(item) for item in items[:_MAX_CONTEXT_ITEMS]]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"
