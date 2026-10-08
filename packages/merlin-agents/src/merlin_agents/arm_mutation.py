"""Mutation arm generator for campaign exploration.

Pure and injectable: this module never imports a live provider. Callers pass a
``generator`` callable that turns a prompt string into a list of candidate dicts,
which keeps the generator testable and provider-agnostic while the novelty and
deduplication policy stays in one place.

Unlike :mod:`merlin_agents.arm_brainstorm`, mutation starts from one existing
arm and asks the model for perturbed variants that stay recognizable but are
meaningfully different along a caller-supplied direction.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence

from jsonschema import ValidationError, validate

MUTATION_OUTPUT_SCHEMA: dict[str, object] = {
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
_MAX_DIRECTION_CHARS = 200

Generator = Callable[[str], Sequence[Mapping[str, object]]]


def build_mutation_prompt(
    *,
    arm: Mapping[str, object],
    existing_objectives: Sequence[str],
    direction: str,
    maximum: int,
) -> str:
    """Assemble a bounded prompt that asks for mutated variants of one arm.

    The prompt names the source arm by technique id and objective, carries the
    caller's mutation direction, and lists every existing objective under an
    explicit "do not repeat" heading so the model has something concrete to
    exclude. Requested output is capped at ``maximum`` variants.
    """
    if maximum < 1:
        raise ValueError("mutation maximum must be positive")
    source_objective = _truncate(str(arm.get("objective") or "").strip(), _MAX_OBJECTIVE_CHARS)
    source_technique_id = str(arm.get("technique_id") or "").strip()
    objectives = [
        _truncate(objective, _MAX_OBJECTIVE_CHARS)
        for objective in existing_objectives[:_MAX_CONTEXT_ITEMS]
        if objective and str(objective).strip()
    ]
    direction_hint = _truncate(str(direction).strip(), _MAX_DIRECTION_CHARS)
    lines = [
        "You are mutating an existing assessment arm into a small family of variants.",
        f"Produce between 1 and {maximum} variants of the arm below.",
        "Each variant must stay recognizable as a mutation of the source arm but be",
        "meaningfully different from it and from every existing objective.",
        "",
        f"Mutation direction: {direction_hint}",
        "",
        f"Source arm technique_id: {source_technique_id}",
        f"Source arm objective: {source_objective}",
    ]
    if objectives:
        lines.extend(
            [
                "",
                "Existing objectives — do NOT repeat or closely paraphrase any of these:",
            ]
        )
        lines.extend(f"- {objective}" for objective in objectives)
    lines.extend(
        [
            "",
            "Output one JSON object per variant, each matching this schema:",
            json.dumps(MUTATION_OUTPUT_SCHEMA, ensure_ascii=False),
        ]
    )
    return "\n".join(lines)


def generate_mutations(
    *,
    arm: Mapping[str, object],
    existing_objectives: Sequence[str],
    direction: str,
    generator: Generator,
    maximum: int,
) -> tuple[dict[str, object], ...]:
    """Generate, validate, and identity-dedupe mutated arms.

    Each candidate dict is validated against :data:`MUTATION_OUTPUT_SCHEMA`;
    invalid dicts are dropped. Objective similarity is downstream rank evidence,
    not a content gate. Variants are normalized to
    ``technique_id``, ``semantic_id``, ``strategy_id``, ``objective``,
    ``source="mutation"`` and, when present, ``rationale``. At most ``maximum``
    variants are returned.
    """
    if maximum < 1:
        raise ValueError("mutation maximum must be positive")
    corpus = [
        str(objective or "").strip()
        for objective in existing_objectives
        if str(objective or "").strip()
    ]
    prompt = build_mutation_prompt(
        arm=arm,
        existing_objectives=corpus,
        direction=direction,
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
        mutated: dict[str, object] = {
            "technique_id": candidate["technique_id"],
            "semantic_id": candidate["semantic_id"],
            "strategy_id": candidate["strategy_id"],
            "objective": candidate["objective"],
            "source": "mutation",
        }
        if candidate["rationale"]:
            mutated["rationale"] = candidate["rationale"]
        admitted.append(mutated)
        seen_keys.add(key)
        if len(admitted) >= maximum:
            break
    return tuple(admitted)


def _validated(raw: object) -> dict[str, str] | None:
    if not isinstance(raw, Mapping):
        return None
    instance = dict(raw)
    try:
        validate(instance=instance, schema=MUTATION_OUTPUT_SCHEMA)
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


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"
