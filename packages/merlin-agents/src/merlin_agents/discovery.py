"""Evidence-backed primitives for continuous exploration.

The canonical bandit unit is surface x technique x controller x mutation x
actual-language x target-state. Durable infrastructure failures stay observable
but never enter attack-quality rewards or retirement.
"""

from __future__ import annotations

import asyncio
import json
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from math import log, sqrt
from typing import Literal

from .jev import JevClient
from .telemetry import emit_judgment

NoveltyVerdict = Literal["novel", "similar", "duplicate"]
LifecycleState = Literal["trial", "active", "retired"]
RetirementAction = Literal["promote", "reactivate", "downrank", "keep"]

_NOVEL_THRESHOLD = 0.25
_DUPLICATE_THRESHOLD = 0.55
_INFRASTRUCTURE_OUTCOMES = frozenset(
    {
        "infrastructure_failure",
        "provider_failure",
        "transport_failure",
        "timeout",
        "rate_limited",
        "cancelled",
    }
)
_QUALITY_OUTCOMES = frozenset(
    {
        "finding",
        "validated_finding",
        "win",
        "near_miss",
        "partial",
        "negative_result",
        "refuted",
        "clean_negative",
    }
)


def normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def char_ngrams(text: str, n: int) -> set[str]:
    chars = list(text)
    return (
        {"".join(chars[i : i + n]) for i in range(len(chars) - n + 1)} if len(chars) >= n else set()
    )


def jaccard_3gram(a: str, b: str) -> float:
    a_grams, b_grams = char_ngrams(normalize_text(a), 3), char_ngrams(normalize_text(b), 3)
    if not a_grams and not b_grams:
        return 1.0
    if not a_grams or not b_grams:
        return 0.0
    intersection = len(a_grams & b_grams)
    return intersection / (len(a_grams) + len(b_grams) - intersection)


@dataclass(frozen=True, slots=True)
class NoveltyResult:
    verdict: NoveltyVerdict
    max_similarity: float
    nearest_index: int


def check_novelty(candidate: str, corpus: Sequence[str]) -> NoveltyResult:
    if not corpus:
        return NoveltyResult("novel", 0.0, -1)
    similarities = tuple(jaccard_3gram(candidate, entry) for entry in corpus)
    maximum = max(similarities)
    verdict: NoveltyVerdict = (
        "duplicate"
        if maximum >= _DUPLICATE_THRESHOLD
        else "similar"
        if maximum >= _NOVEL_THRESHOLD
        else "novel"
    )
    return NoveltyResult(verdict, maximum, similarities.index(maximum))


_JEV_SHADOW_NEIGHBOR_LIMIT = 5
_JEV_SHADOW_SAME_THRESHOLD = 0.5
_JEV_SHADOW_QUESTION = (
    "Is this attack objective semantically the same attack as any of these "
    "listed objectives (same mechanism, surface, and goal)?"
)
_JEV_SHADOW_TRUE = "the candidate duplicates a listed attack: same mechanism, surface, and goal"
_JEV_SHADOW_FALSE = "the candidate is a distinct attack from every listed objective"

_shadow_client_checked = False
_shadow_client: JevClient | None = None


def _jev_shadow_client() -> JevClient | None:
    """Lazily build the shared Jev client; ``None`` when unavailable/disabled."""
    global _shadow_client, _shadow_client_checked
    if _shadow_client_checked:
        return _shadow_client
    _shadow_client_checked = True
    try:
        from merlin_config import load_config
    except ImportError:
        return None
    try:
        config = load_config()
    except Exception:  # noqa: BLE001 - advisory gate; never raise
        return None
    judgments = getattr(config, "judgments", None)
    if judgments is None or not getattr(judgments, "jev_decisions_enabled", False):
        return None
    _shadow_client = JevClient.from_config(judgments)
    return _shadow_client


def _shadow_neighbors(candidate: str, corpus: Sequence[str]) -> tuple[str, ...]:
    """Top-k corpus entries by Jaccard similarity, capped and zero-scored ones dropped."""
    scored = sorted(
        ((jaccard_3gram(candidate, entry), entry) for entry in corpus),
        key=lambda item: item[0],
        reverse=True,
    )
    return tuple(entry for score, entry in scored[:_JEV_SHADOW_NEIGHBOR_LIMIT] if score > 0.0)


async def jev_shadow_novelty(
    candidate: str,
    corpus: Sequence[str],
    *,
    client: JevClient,
    campaign_id: str = "",
    task_id: str = "",
    technique_ids: Sequence[str] = (),
) -> None:
    """Telemetry-only Jev shadow of the Jaccard novelty verdict.

    Asks one ``noul`` question against the top-k Jaccard neighbors and emits
    the Jev verdict paired with the Jaccard verdict (graded_by ``jev-shadow``)
    for later calibration.  The result never feeds back into admission.
    """
    neighbors = _shadow_neighbors(candidate, corpus)
    if not neighbors:
        return
    jaccard = check_novelty(candidate, corpus)
    state = (
        "Candidate attack objective:\n"
        + candidate
        + "\n\nExisting objectives:\n"
        + "\n".join(f"{index}. {entry}" for index, entry in enumerate(neighbors, start=1))
    )
    input_before, output_before = client.input_tokens, client.output_tokens
    started = time.monotonic()
    probability = await client.noul(
        state,
        _JEV_SHADOW_QUESTION,
        true_meaning=_JEV_SHADOW_TRUE,
        false_meaning=_JEV_SHADOW_FALSE,
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    if probability is None:
        return
    jev_verdict = "same" if probability >= _JEV_SHADOW_SAME_THRESHOLD else "different"
    confidence = probability if jev_verdict == "same" else 1.0 - probability
    emit_judgment(
        campaign_id=campaign_id or "arm-novelty-shadow",
        task_id=task_id or candidate[:256],
        technique_ids=technique_ids,
        verdict=f"jev_{jev_verdict}_vs_jaccard_{jaccard.verdict}",
        confidence=confidence,
        graded_by="jev-shadow",
        latency_ms=latency_ms,
        input_tokens=client.input_tokens - input_before,
        output_tokens=client.output_tokens - output_before,
    )


def schedule_novelty_shadow(
    candidate: str,
    corpus: Sequence[str],
    *,
    client: JevClient | None = None,
    campaign_id: str = "",
    task_id: str = "",
    technique_ids: Sequence[str] = (),
) -> None:
    """Fire-and-forget shadow judgment; a no-op without a client or running loop."""
    try:
        if not corpus:
            return
        resolved = client if client is not None else _jev_shadow_client()
        if resolved is None:
            return
        loop = asyncio.get_running_loop()
        loop.create_task(
            jev_shadow_novelty(
                candidate,
                tuple(corpus),
                client=resolved,
                campaign_id=campaign_id,
                task_id=task_id,
                technique_ids=technique_ids,
            )
        )
    except Exception:  # noqa: BLE001 - shadow telemetry must never raise
        return


@dataclass(frozen=True, slots=True)
class ArmIdentity:
    """One independently learnable execution context."""

    surface_id: str
    technique_id: str
    controller_id: str
    mutation_id: str
    actual_language: str
    state_id: str

    def __post_init__(self) -> None:
        if empty := [name for name, value in asdict(self).items() if not value.strip()]:
            raise ValueError("arm identity fields must not be empty: " + ", ".join(empty))
        if self.actual_language.casefold() in {"unknown", "requested"}:
            raise ValueError("arm identity requires an observed language or explicit und")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ArmIdentity:
        fields = (
            "surface_id",
            "technique_id",
            "controller_id",
            "mutation_id",
            "actual_language",
            "state_id",
        )
        if missing := [name for name in fields if not isinstance(value.get(name), str)]:
            raise ValueError("arm identity is missing string fields: " + ", ".join(missing))
        return cls(**{name: str(value[name]).strip() for name in fields})

    @property
    def key(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def projection(self) -> dict[str, str]:
        return asdict(self)


def arm_key(identity: ArmIdentity) -> str:
    """Return the sole canonical six-dimensional arm key."""
    return identity.key


@dataclass(frozen=True, slots=True)
class ArmObservation:
    identity: ArmIdentity
    outcome: str
    partial_signal: float = 0.0
    infrastructure_failure: bool = False
    generalized_win: bool = False
    boundary_signal: bool = False
    latency_ms: int | None = None
    cost_microusd: int | None = None
    transform_credit_eligible: bool = True
    evaluation_uncertainties: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        if not self.outcome.strip():
            raise ValueError("arm observation outcome cannot be empty")
        if not 0.0 <= self.partial_signal <= 1.0:
            raise ValueError("partial signal must be between zero and one")
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError("latency cannot be negative")
        if self.cost_microusd is not None and self.cost_microusd < 0:
            raise ValueError("cost cannot be negative")
        if not isinstance(self.evaluation_uncertainties, tuple) or any(
            not isinstance(item, Mapping) for item in self.evaluation_uncertainties
        ):
            raise ValueError("arm observation evaluation uncertainties must be mappings")
        if self.generalized_win and self.outcome not in {"finding", "validated_finding", "win"}:
            raise ValueError("win generalization requires a winning source observation")
        if self.is_infrastructure_failure and (
            self.partial_signal > 0 or self.generalized_win or self.boundary_signal
        ):
            raise ValueError("infrastructure failures cannot carry attack-quality signal")
        # A transform-attribution disagreement does not erase what the target did.
        # ``aggregate_observations`` withholds that evidence from the transform-specific
        # arm while the raw observation remains available to base-technique learning.

    @property
    def is_infrastructure_failure(self) -> bool:
        return self.infrastructure_failure or self.outcome in _INFRASTRUCTURE_OUTCOMES

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ArmObservation:
        identity = value.get("identity")
        outcome = value.get("outcome")
        if not isinstance(identity, Mapping) or not isinstance(outcome, str):
            raise ValueError("arm observation requires canonical identity and string outcome")
        for name in (
            "infrastructure_failure",
            "generalized_win",
            "boundary_signal",
            "transform_credit_eligible",
        ):
            if name in value and type(value[name]) is not bool:
                raise ValueError(f"arm observation {name} must be a boolean")
        partial = value.get("partial_signal", 0.0)
        if isinstance(partial, bool) or not isinstance(partial, (int, float)):
            raise ValueError("arm observation partial_signal must be numeric")
        raw_uncertainties = value.get("evaluation_uncertainties", ())
        if not isinstance(raw_uncertainties, Sequence) or isinstance(
            raw_uncertainties, (str, bytes)
        ):
            raise ValueError("arm observation evaluation uncertainties must be a sequence")
        if any(not isinstance(item, Mapping) for item in raw_uncertainties):
            raise ValueError("arm observation evaluation uncertainties must be mappings")
        transform_credit_eligible = value.get("transform_credit_eligible")
        if transform_credit_eligible is None:
            transform_credit_eligible = not any(
                item.get("withhold_transform_credit") is True for item in raw_uncertainties
            )
        return cls(
            identity=ArmIdentity.from_mapping(identity),
            outcome=outcome,
            partial_signal=float(partial),
            infrastructure_failure=bool(value.get("infrastructure_failure", False)),
            generalized_win=bool(value.get("generalized_win", False)),
            boundary_signal=bool(value.get("boundary_signal", False)),
            latency_ms=_optional_int(value.get("latency_ms"), "latency_ms"),
            cost_microusd=_optional_int(value.get("cost_microusd"), "cost_microusd"),
            transform_credit_eligible=bool(transform_credit_eligible),
            evaluation_uncertainties=tuple(dict(item) for item in raw_uncertainties),
        )


@dataclass(frozen=True, slots=True)
class AttemptStats:
    identity: ArmIdentity
    attempts: int
    wins: int
    partial_signal: float
    clean_negatives: int
    infrastructure_failures: int = 0
    generalized_wins: int = 0
    boundary_signals: int = 0
    total_latency_ms: int = 0
    total_cost_microusd: int = 0
    latency_observations: int = 0
    cost_observations: int = 0
    evaluation_uncertainties: int = 0

    def __post_init__(self) -> None:
        integers = (
            self.attempts,
            self.wins,
            self.clean_negatives,
            self.infrastructure_failures,
            self.generalized_wins,
            self.boundary_signals,
            self.total_latency_ms,
            self.total_cost_microusd,
            self.latency_observations,
            self.cost_observations,
            self.evaluation_uncertainties,
        )
        if any(value < 0 for value in integers) or self.partial_signal < 0:
            raise ValueError("arm statistics cannot be negative")
        if self.wins + self.clean_negatives > self.attempts:
            raise ValueError("terminal quality outcomes cannot exceed eligible attempts")
        if self.partial_signal > self.attempts:
            raise ValueError("partial signal cannot exceed eligible attempts")

    @property
    def key(self) -> str:
        return self.identity.key

    @property
    def signal(self) -> float:
        return float(self.wins) + self.partial_signal


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    name: str
    measured_value: float
    contribution: float
    reason: str


@dataclass(frozen=True, slots=True)
class UcbRanking:
    identity: ArmIdentity
    score: float
    components: tuple[ScoreComponent, ...]
    lifecycle: LifecycleState
    lifecycle_reason: str

    @property
    def key(self) -> str:
        return self.identity.key

    @property
    def is_weak(self) -> bool:
        return self.lifecycle == "retired"

    def projection(self) -> dict[str, object]:
        return {
            "key": self.key,
            "identity": self.identity.projection(),
            "score": self.score,
            "components": tuple(asdict(item) for item in self.components),
            "lifecycle": self.lifecycle,
            "lifecycle_reason": self.lifecycle_reason,
        }


@dataclass(frozen=True, slots=True)
class RankingPolicy:
    exploration_constant: float = 1.5
    partial_weight: float = 0.35
    novelty_weight: float = 0.45
    generalization_weight: float = 0.25
    boundary_weight: float = 0.30
    latency_weight: float = 0.10
    cost_weight: float = 0.10
    latency_reference_ms: int = 30_000
    cost_reference_microusd: int = 100_000
    trial_retire_attempts: int = 5
    active_retire_attempts: int = 20

    def __post_init__(self) -> None:
        weights = (
            self.exploration_constant,
            self.partial_weight,
            self.novelty_weight,
            self.generalization_weight,
            self.boundary_weight,
            self.latency_weight,
            self.cost_weight,
        )
        if any(value < 0 for value in weights):
            raise ValueError("ranking policy weights cannot be negative")
        if self.latency_reference_ms <= 0 or self.cost_reference_microusd <= 0:
            raise ValueError("ranking policy cost and latency references must be positive")
        if self.trial_retire_attempts < 1 or self.active_retire_attempts < 1:
            raise ValueError("ranking policy retirement thresholds must be positive")


DEFAULT_RANKING_POLICY = RankingPolicy()


@dataclass(frozen=True, slots=True)
class RetirementDecision:
    action: RetirementAction
    reason: str


def classify_lifecycle(
    state: LifecycleState,
    *,
    attempts: int,
    signal: float,
    trial_signal_attempts: int = 5,
    active_retire_attempts: int = 20,
    confidence: float = 0.0,
    uncertainty: float = 0.0,
    novelty: float = 0.0,
    context_changed: bool = False,
) -> RetirementDecision:
    if signal < 0 or attempts < 0:
        raise ValueError("signal and attempts must be non-negative")
    if state not in {"trial", "active", "retired"}:
        raise ValueError("lifecycle state must be trial, active, or retired")
    if any(value < 0 or value > 1 for value in (confidence, uncertainty, novelty)):
        raise ValueError("confidence, uncertainty, and novelty must be between zero and one")
    if state == "retired":
        reason = "legacy zero-signal retirement reopened as a reversible trial"
        if context_changed:
            reason += "; language, mutation, surface, or target state changed"
        elif uncertainty > 0 or novelty > 0:
            reason += "; uncertainty or novelty leaves useful exploration value"
        return RetirementDecision("reactivate", reason)
    if state == "trial":
        if signal > 0:
            return RetirementDecision("promote", "quality signal observed within trial budget")
        if attempts >= trial_signal_attempts:
            return RetirementDecision(
                "downrank",
                "zero signal lowers priority reversibly; confidence, uncertainty, novelty, "
                "and changed execution context remain ranking evidence",
            )
        return RetirementDecision("keep", "trial quality budget remains")
    if signal > 0:
        return RetirementDecision("keep", "active arm retains quality signal")
    if attempts >= active_retire_attempts:
        return RetirementDecision(
            "downrank",
            "zero signal lowers priority reversibly; later evidence or a changed execution "
            "context can restore rank",
        )
    return RetirementDecision("keep", "active retirement budget remains")


def aggregate_observations(
    observations: Sequence[ArmObservation | Mapping[str, object]],
) -> dict[str, AttemptStats]:
    buckets: dict[str, dict[str, object]] = {}
    for raw in observations:
        item = raw if isinstance(raw, ArmObservation) else ArmObservation.from_mapping(raw)
        bucket = buckets.setdefault(
            item.identity.key,
            {
                "identity": item.identity,
                "attempts": 0,
                "wins": 0,
                "partial_signal": 0.0,
                "clean_negatives": 0,
                "infrastructure_failures": 0,
                "generalized_wins": 0,
                "boundary_signals": 0,
                "total_latency_ms": 0,
                "total_cost_microusd": 0,
                "latency_observations": 0,
                "cost_observations": 0,
                "evaluation_uncertainties": 0,
            },
        )
        if item.evaluation_uncertainties:
            bucket["evaluation_uncertainties"] = int(bucket["evaluation_uncertainties"]) + 1
        if item.is_infrastructure_failure:
            bucket["infrastructure_failures"] = int(bucket["infrastructure_failures"]) + 1
            continue
        quality_eligible = item.transform_credit_eligible and (
            item.outcome in _QUALITY_OUTCOMES or item.partial_signal > 0
        )
        if not quality_eligible:
            continue
        bucket["attempts"] = int(bucket["attempts"]) + 1
        if item.outcome in {"finding", "validated_finding", "win"}:
            bucket["wins"] = int(bucket["wins"]) + 1
        elif item.outcome in {"negative_result", "refuted", "clean_negative"}:
            bucket["clean_negatives"] = int(bucket["clean_negatives"]) + 1
        bucket["partial_signal"] = float(bucket["partial_signal"]) + item.partial_signal
        bucket["generalized_wins"] = int(bucket["generalized_wins"]) + int(item.generalized_win)
        bucket["boundary_signals"] = int(bucket["boundary_signals"]) + int(item.boundary_signal)
        bucket["total_latency_ms"] = int(bucket["total_latency_ms"]) + (item.latency_ms or 0)
        bucket["total_cost_microusd"] = int(bucket["total_cost_microusd"]) + (
            item.cost_microusd or 0
        )
        bucket["latency_observations"] = int(bucket["latency_observations"]) + int(
            item.latency_ms is not None
        )
        bucket["cost_observations"] = int(bucket["cost_observations"]) + int(
            item.cost_microusd is not None
        )
    return {key: AttemptStats(**bucket) for key, bucket in buckets.items()}  # type: ignore[arg-type]


def ucb_score(stats: AttemptStats, total_runs: int, exploration_constant: float = 1.5) -> float:
    exploitation = (stats.wins + 0.35 * stats.partial_signal) / max(stats.attempts, 1)
    return exploitation + exploration_constant * sqrt(
        log(max(total_runs, 0) + 2) / (stats.attempts + 1)
    )


def rank_tasks_by_ucb(
    stats: Sequence[AttemptStats],
    *,
    arm_measurements: Mapping[str, Mapping[str, float]] | None = None,
    arm_states: Mapping[str, LifecycleState] | None = None,
    policy: RankingPolicy = DEFAULT_RANKING_POLICY,
) -> tuple[UcbRanking, ...]:
    measurements, states = arm_measurements or {}, arm_states or {}
    total_runs = sum(item.attempts for item in stats)
    rankings: list[UcbRanking] = []
    for item in stats:
        state = states.get(item.key, "trial")
        values = measurements.get(item.key, {})
        novelty, gen_op, boundary_op = (
            _bounded(values.get(name, 0.0))
            for name in ("novelty", "generalization_opportunity", "boundary_opportunity")
        )
        evidence_confidence = _bounded(
            values.get(
                "confidence",
                item.clean_negatives / max(item.attempts, 1),
            )
        )
        evaluation_uncertainty = _bounded(
            values.get(
                "evaluation_uncertainty",
                item.evaluation_uncertainties
                / max(item.attempts + item.evaluation_uncertainties, 1),
            )
        )
        context_changed = any(
            _bounded(values.get(name, 0.0)) > 0
            for name in (
                "context_changed",
                "language_changed",
                "mutation_changed",
                "state_changed",
                "surface_changed",
            )
        )
        decision = classify_lifecycle(
            state,
            attempts=item.attempts,
            signal=item.signal,
            trial_signal_attempts=policy.trial_retire_attempts,
            active_retire_attempts=policy.active_retire_attempts,
            confidence=evidence_confidence,
            uncertainty=evaluation_uncertainty,
            novelty=novelty,
            context_changed=context_changed,
        )
        lifecycle: LifecycleState = (
            "trial"
            if decision.action == "reactivate"
            else "active"
            if decision.action == "promote"
            else state
        )
        quality = (item.wins + policy.partial_weight * item.partial_signal) / max(item.attempts, 1)
        exploration_bonus = policy.exploration_constant * sqrt(
            log(total_runs + 2) / (item.attempts + 1)
        )
        generalization = policy.generalization_weight * (
            gen_op + item.generalized_wins / max(item.attempts, 1)
        )
        boundary = policy.boundary_weight * (
            boundary_op + item.boundary_signals / max(item.attempts, 1)
        )
        latency = min(
            1.0,
            item.total_latency_ms / max(item.latency_observations, 1) / policy.latency_reference_ms,
        )
        cost = min(
            1.0,
            item.total_cost_microusd
            / max(item.cost_observations, 1)
            / policy.cost_reference_microusd,
        )
        downrank = (
            evidence_confidence
            * (1.0 - evaluation_uncertainty)
            * (1.0 - novelty)
            * (0.0 if context_changed else 1.0)
            if decision.action == "downrank"
            else 0.0
        )
        components = (
            ScoreComponent(
                "quality_signal",
                quality,
                quality,
                "wins plus weighted near-miss signal per eligible attempt",
            ),
            ScoreComponent(
                "portfolio_exploration_bonus",
                exploration_bonus,
                exploration_bonus,
                "increases as peer evidence accumulates and decreases as this candidate "
                "receives eligible attempts",
            ),
            ScoreComponent(
                "epistemic_uncertainty",
                evaluation_uncertainty,
                0.0,
                "evaluator uncertainty is reported separately and affects only reversible "
                "downranking",
            ),
            ScoreComponent(
                "novelty",
                novelty,
                policy.novelty_weight * novelty,
                "instruction novelty relative to durable objectives",
            ),
            ScoreComponent(
                "generalization",
                gen_op,
                generalization,
                "opportunity plus validated win-derived yield",
            ),
            ScoreComponent(
                "boundary", boundary_op, boundary, "opportunity plus observed state-boundary signal"
            ),
            ScoreComponent(
                "latency",
                latency,
                -policy.latency_weight * latency,
                "mean eligible latency against policy reference",
            ),
            ScoreComponent(
                "cost",
                cost,
                -policy.cost_weight * cost,
                "mean eligible cost against policy reference",
            ),
            ScoreComponent(
                "reversible_downrank",
                downrank,
                -policy.novelty_weight * downrank,
                (
                    "zero-signal evidence lowers priority without retirement; evaluator "
                    "uncertainty, novelty, or changed language/mutation/state reduces the penalty"
                ),
            ),
        )
        rankings.append(
            UcbRanking(
                item.identity,
                sum(part.contribution for part in components),
                components,
                lifecycle,
                decision.reason,
            )
        )
    attempts_by_key = {item.key: item.attempts for item in stats}
    return tuple(
        sorted(
            rankings,
            key=lambda item: (
                0 if attempts_by_key[item.key] == 0 else 1,
                -item.score,
                item.key,
            ),
        )
    )


def _bounded(value: float) -> float:
    if not 0.0 <= value <= 1.0:
        raise ValueError("arm ranking measurements must be between zero and one")
    return value


def _optional_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"arm observation {name} must be an integer or null")
    return value
