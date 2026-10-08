"""Evidence-first execution proposals for adaptive attack strategies.

The scheduler store owns immutable observations and effect lineage.  This module
owns the product decision derived from those observations: continue execution,
ask a validator to review the evidence, retry/mutate unresolved work, or stop for
an explicit operational reason.  None of these proposals is a target-security
verdict.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

StrategyAction = Literal["continue", "validate", "retry", "stop"]


@dataclass(frozen=True, slots=True)
class EvaluationUncertainty:
    """Structured advisory evidence that must not become an admission gate."""

    source: str
    reason: str
    confidence: float

    def dump(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StrategyProposal:
    """A non-authoritative next action derived from immutable observations."""

    strategy_id: str
    action: StrategyAction
    reason: str
    next_stage: str | None = None
    observations: int = 0
    evaluation_uncertain: tuple[EvaluationUncertainty, ...] = ()

    @property
    def target_execution_terminal(self) -> bool:
        """Whether this proposal requires no additional target call in this attempt."""

        return self.action != "continue"

    def dump(self) -> dict[str, object]:
        return {
            **asdict(self),
            "evaluation_uncertain": tuple(item.dump() for item in self.evaluation_uncertain),
        }


def propose_strategy_action(
    strategy_id: str,
    state: Mapping[str, object] | None,
    contract: Mapping[str, object] | Sequence[tuple[object, object]] = (),
) -> StrategyProposal:
    """Derive an execution proposal without converting evidence into a verdict.

    ``state`` is a store projection of immutable target observations.  Content
    evaluator disagreement is retained as uncertainty.  Only cancellation and
    explicit budget caps produce a hard stop proposal.
    """

    normalized_contract = _mapping(contract)
    normalized_state = {} if state is None else dict(state)
    steps = _steps(normalized_state)
    uncertainties = _uncertainties(normalized_state)
    terminal = _optional_text(normalized_state.get("terminal_reason"))

    if terminal in {"cancelled", "capped"}:
        return StrategyProposal(
            strategy_id=strategy_id,
            action="stop",
            reason=(
                "operator cancellation"
                if terminal == "cancelled"
                else "explicit execution budget reached"
            ),
            observations=len(steps),
            evaluation_uncertain=uncertainties,
        )
    if terminal in {"ambiguous", "blocked"}:
        return StrategyProposal(
            strategy_id=strategy_id,
            action="retry",
            reason="execution evidence is unresolved; retry or mutate without retiring it",
            observations=len(steps),
            evaluation_uncertain=_dedupe(
                (*uncertainties, _uncertain(f"legacy terminal marker was {terminal}"))
            ),
        )
    if terminal in {"awaiting-judge", "positive", "invalid-premise", "plateau"}:
        return StrategyProposal(
            strategy_id=strategy_id,
            action="validate",
            reason="target observations require validator judgement",
            observations=len(steps),
            evaluation_uncertain=_dedupe(
                (*uncertainties, _uncertain(f"evaluator observation was {terminal}"))
            ),
        )

    if strategy_id == "crescendo":
        return _crescendo_proposal(steps, normalized_state, normalized_contract, uncertainties)
    if strategy_id == "goat":
        return _goat_proposal(steps, normalized_contract, uncertainties)
    if strategy_id == "hydra-select":
        return _hydra_proposal(steps, normalized_state, normalized_contract, uncertainties)
    raise ValueError(f"unsupported adaptive strategy: {strategy_id}")


def _crescendo_proposal(
    steps: tuple[Mapping[str, object], ...],
    state: Mapping[str, object],
    contract: Mapping[str, object],
    uncertainties: tuple[EvaluationUncertainty, ...],
) -> StrategyProposal:
    last_decision = _optional_text(steps[-1].get("decision")) if steps else None
    if last_decision in {"success", "invalid-premise"}:
        return StrategyProposal(
            "crescendo",
            "validate",
            "content evaluator signal requires validator judgement",
            observations=len(steps),
            evaluation_uncertain=_dedupe(
                (*uncertainties, _uncertain(f"content evaluator signalled {last_decision}"))
            ),
        )
    max_turns = _positive_int(contract.get("max_target_turns"))
    if max_turns is not None and len(steps) >= max_turns:
        return StrategyProposal(
            "crescendo",
            "validate",
            "review the accumulated evidence before allocating another target turn",
            observations=len(steps),
            evaluation_uncertain=uncertainties,
        )
    plateau_turns = _positive_int(contract.get("plateau_turns"))
    trailing_no_progress = 0
    for step in reversed(steps):
        if step.get("decision") != "no-progress":
            break
        trailing_no_progress += 1
    if plateau_turns is not None and trailing_no_progress >= plateau_turns:
        return StrategyProposal(
            "crescendo",
            "validate",
            "progress stalled; review evidence and mutate before spending more target turns",
            observations=len(steps),
            evaluation_uncertain=uncertainties,
        )
    next_stage = (
        "backtrack"
        if last_decision == "refusal"
        else (_optional_text(state.get("next_stage")) or ("probe" if not steps else "escalate"))
    )
    return StrategyProposal(
        "crescendo",
        "continue",
        "another authorized target observation can advance the strategy",
        next_stage=next_stage,
        observations=len(steps),
        evaluation_uncertain=uncertainties,
    )


def _goat_proposal(
    steps: tuple[Mapping[str, object], ...],
    contract: Mapping[str, object],
    uncertainties: tuple[EvaluationUncertainty, ...],
) -> StrategyProposal:
    target_turns = _positive_int(contract.get("target_turns"))
    if target_turns is not None and len(steps) >= target_turns:
        return StrategyProposal(
            "goat",
            "validate",
            "the planned target observations are ready for validator judgement",
            observations=len(steps),
            evaluation_uncertain=uncertainties,
        )
    return StrategyProposal(
        "goat",
        "continue",
        "another authorized target observation remains in the strategy plan",
        next_stage="target-turn",
        observations=len(steps),
        evaluation_uncertain=uncertainties,
    )


def _hydra_proposal(
    steps: tuple[Mapping[str, object], ...],
    state: Mapping[str, object],
    contract: Mapping[str, object],
    uncertainties: tuple[EvaluationUncertainty, ...],
) -> StrategyProposal:
    if state.get("blocking_effect_id") is not None:
        return StrategyProposal(
            "hydra-select",
            "retry",
            "an unresolved effect must not be resent; retry with a fresh candidate lineage",
            observations=len(steps),
            evaluation_uncertain=_dedupe(
                (*uncertainties, _uncertain("target effect acknowledgement is unresolved"))
            ),
        )
    plan = state.get("plan")
    selected = plan.get("selected_candidate_ids") if isinstance(plan, Mapping) else None
    selected_count = (
        len(selected)
        if isinstance(selected, Sequence) and not isinstance(selected, (str, bytes))
        else _positive_int(contract.get("selected_count"))
    )
    objective_observed = any(step.get("objective_matched") is True for step in steps)
    if _enabled(contract.get("stop_on_positive")) and objective_observed:
        return StrategyProposal(
            "hydra-select",
            "validate",
            "content evaluator signal requires validator judgement",
            observations=len(steps),
            evaluation_uncertain=_dedupe(
                (*uncertainties, _uncertain("content evaluator signalled an objective match"))
            ),
        )
    if selected_count is not None and len(steps) >= selected_count:
        return StrategyProposal(
            "hydra-select",
            "validate",
            "the selected candidate observations are ready for validator judgement",
            observations=len(steps),
            evaluation_uncertain=uncertainties,
        )
    if plan is None:
        return StrategyProposal(
            "hydra-select",
            "continue",
            "generate and persist a diverse candidate plan",
            next_stage="propose",
            observations=len(steps),
            evaluation_uncertain=uncertainties,
        )
    return StrategyProposal(
        "hydra-select",
        "continue",
        "execute the next selected candidate",
        next_stage="selected-candidate",
        observations=len(steps),
        evaluation_uncertain=uncertainties,
    )


def _mapping(
    value: Mapping[str, object] | Sequence[tuple[object, object]],
) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        return {str(key): item for key, item in value}
    except (TypeError, ValueError):
        return {}


def _steps(state: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    value = state.get("steps", ())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _uncertainties(state: Mapping[str, object]) -> tuple[EvaluationUncertainty, ...]:
    values: list[EvaluationUncertainty] = []
    for key in ("evaluation_uncertain", "evaluation_uncertainties"):
        raw = state.get(key, ())
        if raw is True:
            values.append(_uncertain("stored evaluator observation is uncertain"))
        elif isinstance(raw, str):
            values.append(_uncertain(raw))
        elif isinstance(raw, Sequence):
            for item in raw:
                if isinstance(item, Mapping):
                    reason = item.get("reason")
                    if isinstance(reason, str) and reason.strip():
                        confidence = item.get("confidence", 0.0)
                        values.append(
                            EvaluationUncertainty(
                                source=str(item.get("source") or "strategy_controller"),
                                reason=reason.strip(),
                                confidence=(
                                    float(confidence)
                                    if isinstance(confidence, (int, float))
                                    and not isinstance(confidence, bool)
                                    else 0.0
                                ),
                            )
                        )
                elif isinstance(item, str) and item.strip():
                    values.append(_uncertain(item.strip()))
    return _dedupe(values)


def _dedupe(
    values: Sequence[EvaluationUncertainty],
) -> tuple[EvaluationUncertainty, ...]:
    return tuple(dict.fromkeys(values))


def _uncertain(reason: str, *, confidence: float = 0.0) -> EvaluationUncertainty:
    return EvaluationUncertainty("strategy_controller", reason, confidence)


def _positive_int(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _enabled(value: object) -> bool:
    if value is True:
        return True
    return isinstance(value, str) and value.strip().casefold() == "true"


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
