"""Generative arm discovery orchestrator for live campaign exploration.

Pure and injectable: this module never imports a live provider. The caller
supplies a synchronous ``generator`` callable (matching the arm generator
modules' contract) and an optional ``record_arm`` sink so admitted arms reach
the durable pool. It composes the committed generators — primary brainstorm
plus, when a signal is present, failure-derived and win-generalization — then
records cross-generator novelty as ranking evidence before identity admission.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from merlin_contracts import ModelJsonError

from .adaptive_catalog import catalog_mutation_candidates
from .arm_brainstorm import generate_arms as generate_brainstorm_arms
from .arm_failure_derived import generate_failure_arms
from .arm_win_generalization import generate_win_arm
from .discovery import check_novelty, schedule_novelty_shadow

Generator = Callable[[str], Sequence[Mapping[str, object]]]


@dataclass(frozen=True, slots=True)
class ArmDiscoveryResult:
    """Admitted arms plus the resulting pool they joined."""

    admitted: tuple[Mapping[str, object], ...]
    pool: tuple[Mapping[str, object], ...]
    generation_failures: tuple[Mapping[str, str], ...] = ()

    def projection(self) -> dict[str, object]:
        return {
            "admitted": tuple(dict(arm) for arm in self.admitted),
            "pool": tuple(dict(arm) for arm in self.pool),
            "generation_failures": tuple(dict(item) for item in self.generation_failures),
        }


def discover_arms(
    *,
    target_id: str,
    scenario_fingerprint: str,
    seeds: Sequence[object] = (),
    receipts: Sequence[object] = (),
    existing_arms: Sequence[Mapping[str, object]] = (),
    generator: Generator,
    maximum: int,
    failure: Mapping[str, object] | None = None,
    win: Mapping[str, object] | None = None,
    record_arm: Callable[[Mapping[str, object]], None] | None = None,
) -> ArmDiscoveryResult:
    """Generate distinct arm identities and admit them through the supplied durable sink.

    Brainstorm is always run and is the primary producer. ``failure`` and
    ``win`` are optional extracted signals; when present they additionally feed
    the failure-derived and win-generalization generators. Every candidate from
    every generator is de-duplicated against one another and against the
    existing pool before enrichment and admission. Text similarity remains an
    explainable ranking observation and never suppresses a distinct identity.
    """
    if maximum < 1:
        raise ValueError("arm discovery maximum must be positive")
    if not target_id.strip() or not scenario_fingerprint.strip():
        raise ValueError("arm discovery requires a target and scenario fingerprint")
    existing_objectives = [
        str(arm.get("objective") or "").strip()
        for arm in existing_arms
        if str(arm.get("objective") or "").strip()
    ]
    generation_failures: list[Mapping[str, str]] = []
    failure_candidates: list[Mapping[str, object]] = []
    if failure is not None:
        try:
            failure_candidates.extend(
                generate_failure_arms(
                    transcript=str(failure.get("transcript") or ""),
                    failure_reason=str(failure.get("failure_reason") or ""),
                    technique=str(failure.get("technique") or ""),
                    existing_objectives=existing_objectives,
                    generator=generator,
                    maximum=maximum,
                )
            )
        except ModelJsonError as error:
            generation_failures.append(
                {"generator": "failure-derived", "status": "retryable", "reason": str(error)}
            )
    win_candidates: list[Mapping[str, object]] = []
    if win is not None:
        try:
            win_arm = generate_win_arm(
                transcript=str(win.get("transcript") or ""),
                strategy=str(win.get("strategy") or ""),
                technique=str(win.get("technique") or ""),
                existing_objectives=existing_objectives,
                generator=generator,
            )
        except ModelJsonError as error:
            generation_failures.append(
                {"generator": "win-generalization", "status": "retryable", "reason": str(error)}
            )
            win_arm = None
        if win_arm is not None:
            win_candidates.append(win_arm)
    reserved_signals = min(maximum - 1, len(failure_candidates) + len(win_candidates))
    # The reviewed local catalogue is the reliable floor when a model-backed
    # generator is malformed or unavailable. Rank its complete candidate grid
    # and allow it to fill the whole discovery capacity; brainstorm and
    # receipt-derived signals still interleave ahead of final admission.
    catalog_maximum = max(1, maximum - reserved_signals)
    catalog_candidates = catalog_mutation_candidates(
        seeds,
        existing_arms,
        maximum=catalog_maximum,
    )
    try:
        brainstorm_candidates = generate_brainstorm_arms(
            seeds=seeds,
            receipts=receipts,
            existing_arms=existing_arms,
            generator=generator,
            maximum=max(1, maximum - reserved_signals - len(catalog_candidates)),
        )
    except ModelJsonError as error:
        generation_failures.append(
            {"generator": "brainstorm", "status": "retryable", "reason": str(error)}
        )
        brainstorm_candidates = ()
    groups = (
        catalog_candidates,
        brainstorm_candidates,
        failure_candidates,
        win_candidates,
    )
    candidates = [
        group[index]
        for index in range(max(map(len, groups), default=0))
        for group in groups
        if index < len(group)
    ]

    admitted: list[Mapping[str, object]] = []
    seen_keys: set[tuple[str, str, str, str, str]] = {_proposal_key(arm) for arm in existing_arms}
    novelty_corpus = list(existing_objectives)
    for candidate in candidates:
        objective = str(candidate.get("objective") or "").strip()
        if not objective:
            continue
        key = _proposal_key(candidate)
        if key in seen_keys:
            continue
        novelty = check_novelty(objective, novelty_corpus)
        schedule_novelty_shadow(objective, novelty_corpus)
        arm = _enrich_arm(
            {
                **candidate,
                "novelty": 1.0 - novelty.max_similarity,
                "novelty_observation": {
                    "verdict": novelty.verdict,
                    "max_similarity": novelty.max_similarity,
                    "admission_effect": "none",
                },
            },
            target_id,
            scenario_fingerprint,
        )
        admitted.append(arm)
        seen_keys.add(key)
        novelty_corpus.append(objective)
        if record_arm is not None:
            record_arm(arm)
        if len(admitted) >= maximum:
            break

    return ArmDiscoveryResult(
        admitted=tuple(admitted),
        pool=(*existing_arms, *admitted),
        generation_failures=tuple(generation_failures),
    )


def _enrich_arm(
    arm: Mapping[str, object], target_id: str, scenario_fingerprint: str
) -> dict[str, object]:
    semantic_id, technique_id, controller_id, mutation_id, requested_language = _proposal_key(arm)
    strategy_id = _strategy(arm)
    objective = str(arm.get("objective") or "").strip()
    proposal_identity = json.dumps(
        {
            "semantic_id": semantic_id,
            "technique_id": technique_id,
            "controller_id": controller_id,
            "mutation_id": mutation_id,
            "requested_language": requested_language,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    arm_id = (
        "arm:"
        + hashlib.sha256(
            f"{target_id}\n{scenario_fingerprint}\n{proposal_identity}".encode()
        ).hexdigest()[:24]
    )
    source = str(arm.get("source") or "brainstorm")
    enriched: dict[str, object] = {
        "id": arm_id,
        "target_id": target_id,
        "scenario_fingerprint": scenario_fingerprint,
        "semantic_id": semantic_id,
        "strategy_id": strategy_id,
        "controller_id": controller_id,
        "mutation_id": mutation_id,
        "requested_language": requested_language,
        "technique_id": technique_id,
        "objective": objective,
        "source": source,
        "novelty_digest": "sha256:" + hashlib.sha256(objective.encode()).hexdigest(),
        # Win generalizations arrive with an attributable confirmed win, so
        # they enter the pool already active (harvestWin promote:true); every
        # other source starts as a trial.
        "status": "active" if source == "win-generalization" else "trial",
    }
    rationale = str(arm.get("rationale") or "").strip()
    if rationale:
        enriched["rationale"] = rationale
    axis_coords = arm.get("axis_coords")
    if isinstance(axis_coords, Mapping) and axis_coords:
        enriched["axis_coords"] = dict(axis_coords)
    execution_reachability = arm.get("execution_reachability")
    if isinstance(execution_reachability, Mapping) and execution_reachability:
        enriched["execution_reachability"] = dict(execution_reachability)
    execution_bounds = arm.get("execution_bounds")
    if isinstance(execution_bounds, Mapping) and execution_bounds:
        enriched["execution_bounds"] = dict(execution_bounds)
    novelty = arm.get("novelty")
    if isinstance(novelty, (int, float)) and not isinstance(novelty, bool):
        enriched["novelty"] = float(novelty)
    novelty_observation = arm.get("novelty_observation")
    if isinstance(novelty_observation, Mapping):
        enriched["novelty_observation"] = dict(novelty_observation)
    for name in ("generalization_opportunity", "boundary_opportunity"):
        value = arm.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            enriched[name] = float(value)
    rank_components = arm.get("rank_components")
    if isinstance(rank_components, Mapping):
        enriched["rank_components"] = dict(rank_components)
    uncertainties = arm.get("evaluation_uncertainties")
    if isinstance(uncertainties, Sequence) and not isinstance(uncertainties, (str, bytes)):
        structured = tuple(dict(item) for item in uncertainties if isinstance(item, Mapping))
        if structured:
            enriched["evaluation_uncertainties"] = structured
    return enriched


def _semantic(arm: Mapping[str, object]) -> str:
    return str(arm.get("semantic_id") or "").strip()


def _strategy(arm: Mapping[str, object]) -> str:
    return str(arm.get("strategy_id") or "direct").strip()


def _proposal_key(arm: Mapping[str, object]) -> tuple[str, str, str, str, str]:
    """Deduplicate proposals across technique, controller, mutation, and language."""
    execution = arm.get("execution_reachability")
    execution = execution if isinstance(execution, Mapping) else {}
    axis = arm.get("axis_coords")
    axis = axis if isinstance(axis, Mapping) else {}
    axis_identity = (
        json.dumps(dict(axis), sort_keys=True, separators=(",", ":")) if axis else "baseline"
    )
    mutation = str(
        arm.get("mutation_id") or execution.get("requested_transform_id") or axis_identity
    )
    language = str(arm.get("requested_language") or execution.get("requested_language") or "und")
    return (
        _semantic(arm),
        str(arm.get("technique_id") or "").strip(),
        str(arm.get("controller_id") or _strategy(arm)),
        mutation,
        language,
    )
