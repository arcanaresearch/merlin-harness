"""Contextual-specialization arm generator for campaign exploration.

Pure and injectable: this module never imports a live provider. Callers pass a
``generator`` callable that turns a prompt string into a list of candidate dicts,
which keeps the generator testable and provider-agnostic while the novelty and
deduplication policy stays in one place.

Given an arm that already worked in one context plus a new target context
(chatbot, session, tenant, language, authentication, ...), the generator asks an
LLM to specialize that arm for the new context and admits only novel variants.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence

from jsonschema import ValidationError, validate

SPECIALIZATION_OUTPUT_SCHEMA: dict[str, object] = {
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
_MAX_FIELD_CHARS = 200

Generator = Callable[[str], Sequence[Mapping[str, object]]]


def build_specialization_prompt(
    *,
    arm: Mapping[str, object],
    target_context: str,
    existing_objectives: Sequence[str],
    maximum: int,
) -> str:
    """Assemble a bounded prompt that asks for context-specialized arms.

    The prompt names every existing objective under an explicit "do not repeat"
    list, grounds the request in the source arm that already worked, and caps the
    requested output at ``maximum`` arms.
    """
    if maximum < 1:
        raise ValueError("specialization maximum must be positive")
    objectives = [
        _truncate(objective, _MAX_OBJECTIVE_CHARS)
        for objective in existing_objectives[:_MAX_CONTEXT_ITEMS]
        if objective and str(objective).strip()
    ]
    source_arm: dict[str, object] = {
        "technique_id": _truncate(_text(arm.get("technique_id")), _MAX_FIELD_CHARS),
        "objective": _truncate(_text(arm.get("objective")), _MAX_OBJECTIVE_CHARS),
        "semantic_id": _truncate(_text(arm.get("semantic_id")), _MAX_FIELD_CHARS),
        "strategy_id": _truncate(_text(arm.get("strategy_id")), _MAX_FIELD_CHARS),
    }
    rationale = _text(arm.get("rationale"))
    if rationale:
        source_arm["rationale"] = _truncate(rationale, _MAX_OBJECTIVE_CHARS)
    lines = [
        "You are specializing a proven assessment arm for a new target context.",
        f"Propose at most {maximum} specialized arms that adapt the source arm to that context.",
        "",
        f"Target context: {_truncate(str(target_context).strip(), _MAX_OBJECTIVE_CHARS)}",
        "",
        "Source arm that already worked in a different context:",
        json.dumps(source_arm, ensure_ascii=False),
        "",
        "Existing objectives — do NOT repeat or closely paraphrase any of these:",
    ]
    lines.extend(f"- {objective}" for objective in objectives)
    lines.extend(
        [
            "",
            "Output one JSON object per specialized arm, each matching this schema:",
            json.dumps(SPECIALIZATION_OUTPUT_SCHEMA, ensure_ascii=False),
        ]
    )
    return "\n".join(lines)


def generate_specializations(
    *,
    arm: Mapping[str, object],
    target_context: str,
    existing_objectives: Sequence[str],
    generator: Generator,
    maximum: int,
) -> tuple[dict[str, object], ...]:
    """Generate, validate, and identity-dedupe specialized arms.

    Each candidate dict is validated against :data:`SPECIALIZATION_OUTPUT_SCHEMA`;
    invalid dicts are dropped. Text similarity is left to ARM ranking and does
    not suppress a distinct execution identity. Arms are normalized to
    ``technique_id``, ``semantic_id``, ``strategy_id``, ``objective``,
    ``source="contextual"`` and, when present, ``rationale``. At most ``maximum``
    arms are returned.
    """
    if maximum < 1:
        raise ValueError("specialization maximum must be positive")
    objectives = [
        str(objective).strip() for objective in existing_objectives if str(objective or "").strip()
    ]
    prompt = build_specialization_prompt(
        arm=arm,
        target_context=target_context,
        existing_objectives=objectives,
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
        specialized: dict[str, object] = {
            "technique_id": candidate["technique_id"],
            "semantic_id": candidate["semantic_id"],
            "strategy_id": candidate["strategy_id"],
            "objective": candidate["objective"],
            "source": "contextual",
        }
        if candidate["rationale"]:
            specialized["rationale"] = candidate["rationale"]
        admitted.append(specialized)
        seen_keys.add(key)
        if len(admitted) >= maximum:
            break
    return tuple(admitted)


def _validated(raw: object) -> dict[str, str] | None:
    if not isinstance(raw, Mapping):
        return None
    instance = dict(raw)
    try:
        validate(instance=instance, schema=SPECIALIZATION_OUTPUT_SCHEMA)
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


def _text(value: object) -> str:
    return str(value or "").strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"
