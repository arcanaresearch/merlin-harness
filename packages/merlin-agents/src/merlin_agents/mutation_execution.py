"""Curated production mutation portfolio and unified reachability records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .language_execution import language_reachability
from .parseltongue import parseltongue_reachability

MutationAudience = Literal[
    "mainstream", "low-resource", "classical", "mixed", "encoded", "adversarial"
]


@dataclass(frozen=True, slots=True)
class MutationStrategy:
    strategy_id: str
    audience: MutationAudience
    transform_id: str
    requested_language: str
    production_reachable: bool
    locally_executable: bool
    execution_path: str | None
    reachability_reason: str


def production_mutation_schedule() -> tuple[MutationStrategy, ...]:
    """Return a small diversity portfolio, not a misleading 54x222 product."""
    requested = (
        ("language:native:en", "mainstream", "native", "en"),
        ("language:translated:es", "mainstream", "translated", "es"),
        ("register:amharic", "low-resource", "register", "am"),
        ("register:wenyanwen", "classical", "register", "lzh"),
        ("language:mixed:en-es", "mixed", "mixed", "mul"),
    )
    strategies: list[MutationStrategy] = []
    for strategy_id, audience, transform_id, language in requested:
        reach = language_reachability(transform_id)
        strategies.append(
            MutationStrategy(
                strategy_id=strategy_id,
                audience=audience,  # type: ignore[arg-type]
                transform_id=transform_id,
                requested_language=language,
                production_reachable=reach.production_reachable,
                locally_executable=reach.locally_executable,
                execution_path=reach.execution_path,
                reachability_reason=reach.reason,
            )
        )
    for transform_id, audience in (
        ("encoding/base64", "encoded"),
        ("concealment/homoglyph", "adversarial"),
    ):
        reach = parseltongue_reachability(transform_id, category=transform_id.split("/", 1)[0])
        strategies.append(
            MutationStrategy(
                strategy_id=f"parseltongue:{transform_id}",
                audience=audience,  # type: ignore[arg-type]
                transform_id=transform_id,
                requested_language="und",
                production_reachable=reach.executable,
                locally_executable=reach.executable,
                execution_path=reach.executor_id,
                reachability_reason=reach.reason,
            )
        )
    return tuple(strategies)
