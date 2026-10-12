"""Same-target catalog-cell history: skip cells already terminally executed.

History rows are keyed on (target_id, adapter_generation, technique_id,
controller_id, payload_family, language_id, mutation_id) and carry the prior
outcome plus the campaign that produced it. The adapter generation is the stable delivery-channel
identity of the reviewed target contract (endpoints, methods, interface, and
which step interacts — never LLM-authored prose), so reworded reconnaissance
keeps the generation and a genuinely different endpoint or method mints new
keys and every cell re-runs — that is the entire invalidation story.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from hashlib import sha256
from typing import Any

# A terminal outcome means the cell was executed to a conclusion on this
# target+generation and must not be re-run. Ordinary ``blocked`` is explicitly
# retryable: it includes infrastructure failures and unresolved capability
# discovery. Content/capability blocking never retires a security-test cell.
TERMINAL_CELL_OUTCOMES = frozenset({"confirmed", "refuted"})
CELL_OUTCOMES = frozenset(
    {"confirmed", "refuted", "blocked", "blocked_terminal", "partial", "unclear"}
)

# ``partial`` is a graded non-verdict outcome: durable receipts show the target
# engaged with the technique objective and the attempt made progress, but no
# confirmed or refuted verdict was reached. It is terminal for the campaign
# that recorded it (the cell completed with a graded signal) yet never skipped
# in later campaigns — it re-runs, deprioritized behind untried and unclear
# cells, so cross-campaign learning ranks it instead of starving on it.
PARTIAL_CELL_OUTCOME = "partial"

# Legacy journals may still contain this outcome; new plans do not emit it.
LANGUAGE_DEFERRAL_OUTCOME = "deferred"

# Language evidence guides ordering; it never removes an authorized cell.
LANGUAGE_SIGNAL_OUTCOMES = frozenset({"confirmed", "partial", "unclear"})

CellParts = tuple[str, str, str, str, str]
"""Technique, controller, transform, language, and mutation variant identity."""


def technique_cell_parts(technique: Any) -> CellParts:
    """Project one assignment technique onto its stable history-cell identity."""

    return concrete_cell_parts(
        technique_id=technique.id,
        controller_id=technique.strategy_id,
        payload_family=technique.transform_id,
        language_id=getattr(technique, "language", "und"),
        mutation_seed=getattr(technique, "mutation_seed", None),
    )


def concrete_cell_parts(
    *,
    technique_id: object,
    controller_id: object,
    payload_family: object,
    language_id: object,
    mutation_seed: object,
) -> CellParts:
    """Build one exact history identity without materializing a rich candidate."""

    normalized_seed = str(mutation_seed or "").strip()
    mutation_id = (
        "none" if not normalized_seed else "sha256:" + sha256(normalized_seed.encode()).hexdigest()
    )
    return (
        str(technique_id),
        str(controller_id),
        str(payload_family),
        str(language_id),
        mutation_id,
    )


def language_reference_cell_parts(family: Any, reference: Any) -> CellParts:
    """Project a compact language reference onto its exact durable history key."""

    if str(reference.family_id) != str(family.id):
        raise ValueError("language candidate does not belong to its history family")
    return concrete_cell_parts(
        technique_id=reference.id,
        controller_id=family.strategy_id,
        payload_family=reference.variant_id,
        language_id=reference.requested_language,
        mutation_seed=getattr(family, "mutation_seed", None),
    )


def normalize_outcome(outcome: object) -> str:
    """Reduce raw attempt outcomes onto the history vocabulary."""

    value = str(outcome)
    return value if value in CELL_OUTCOMES else "unclear"


def _requires_authenticated_peer_delivery(technique_id: object) -> bool:
    """Identify catalogue cells whose result depends on target-side peer delivery."""

    return "/cat/agent-to-agent/" in str(technique_id)


def plan_skip(
    techniques: Sequence[Any],
    history: Mapping[CellParts, Mapping[str, object]],
    *,
    default_language: str = "en",
    signal_languages: Iterable[str] = (),
) -> tuple[tuple[Any, ...], tuple[dict[str, object], ...]]:
    """Split catalog cells into runnable techniques and journaled skips.

    A cell with a conclusive prior outcome is skipped; a miss, ``blocked``, or
    ``unclear`` prior outcome re-runs. A ``partial`` prior outcome (graded
    engagement without a verdict) also re-runs — it is never skipped — but the
    cell is deprioritized behind never-attempted and ``unclear`` cells in the
    runnable order, so learning ranks it rather than suppressing it. A2A cells
    additionally require durable
    proof that their target effects used only an authenticated peer channel;
    legacy/direct/mixed delivery remains retryable. Each fully identified
    variant is independent: a separate control cell, mutation, or language
    variant never revokes a conclusive result. Selective validation that
    genuinely requires another exchange must be represented as its own cell.

    Baseline and observed-language cells rank first. Missing engagement
    evidence, a clean refusal, or infrastructure failure never excludes a
    different language cell from the runnable plan.
    """

    skipped_by_cell: dict[CellParts, dict[str, object]] = {}
    for technique in techniques:
        if str(technique.id).startswith("hypothesis:"):
            continue
        cell_parts = technique_cell_parts(technique)
        row = history.get(cell_parts)
        if row is None:
            continue
        outcome = normalize_outcome(row.get("outcome"))
        if outcome not in TERMINAL_CELL_OUTCOMES:
            continue
        if (
            _requires_authenticated_peer_delivery(technique.id)
            and row.get("peer_delivery_authenticated") is not True
        ):
            # Historical A2A rows predate target-side delivery attribution.  A
            # direct conversational spoof can remain useful evidence, but it
            # cannot conclusively resolve an inter-agent-message surface.
            continue
        _, controller_id, payload_family, language_id, mutation_id = cell_parts
        skipped_by_cell[cell_parts] = {
            "technique_id": str(technique.id),
            "controller_id": controller_id,
            "payload_family": payload_family,
            "language_id": language_id,
            "mutation_id": mutation_id,
            "outcome": outcome,
            "source_campaign_id": str(row.get("source_campaign_id", "")),
        }
    signalled = {
        str(language).strip().casefold() for language in signal_languages if str(language).strip()
    }
    for cell_parts, row in history.items():
        if normalize_outcome(row.get("outcome")) in LANGUAGE_SIGNAL_OUTCOMES:
            language = str(cell_parts[3]).strip().casefold()
            if language:
                signalled.add(language)
    baseline = {"und", default_language.strip().casefold() or "en"}

    def _priority(technique: Any) -> tuple[int, int]:
        row = history.get(technique_cell_parts(technique))
        partial = int(
            row is not None and normalize_outcome(row.get("outcome")) == PARTIAL_CELL_OUTCOME
        )
        language = str(getattr(technique, "language", "und")).strip().casefold()
        return partial, int(language not in baseline and language not in signalled)

    runnable = tuple(
        sorted(
            (item for item in techniques if technique_cell_parts(item) not in skipped_by_cell),
            key=_priority,
        )
    )
    skipped = tuple(
        skipped_by_cell[technique_cell_parts(item)]
        for item in techniques
        if technique_cell_parts(item) in skipped_by_cell
    )
    return runnable, skipped


def entries_for_completion(
    target_id: str,
    adapter_generation: str,
    techniques: Sequence[Any],
    cells: Sequence[Sequence[object]],
    source_campaign_id: str,
) -> tuple[dict[str, object], ...]:
    """Build history rows from one campaign's sealed completion receipt cells.

    ``cells`` are ``(task_id, technique_id, outcome)`` triples from the sealed
    completion document, optionally with a fourth mapping element carrying the
    deterministic ``partial_signal`` payload for ``partial`` outcomes (what
    progress was observed and which evidence graded it). ``unclear`` is an
    inconclusive attempt, not a disagreement: a cell that reached one terminal
    outcome keeps it even when a sibling attempt in another wave returned
    ``unclear``. A ``partial`` outcome — graded engagement without a verdict —
    outranks ``unclear`` but never displaces a terminal outcome. Only genuinely
    conflicting terminal outcomes (e.g. ``refuted`` and ``confirmed``) reduce
    to ``unclear`` — history never manufactures certainty. Techniques the
    campaign skipped on imported history are absent from the completion cells
    and keep their original rows.
    """

    known: dict[str, list[Any]] = {}
    for technique in techniques:
        if str(technique.id).startswith("hypothesis:"):
            continue
        known.setdefault(str(technique.id), []).append(technique)
    outcomes: dict[str, set[str]] = {}
    signals: dict[str, list[Mapping[str, object]]] = {}
    for cell in cells:
        if len(cell) < 3:
            continue
        technique_id = str(cell[1])
        if technique_id not in known:
            continue
        outcomes.setdefault(technique_id, set()).add(normalize_outcome(cell[2]))
        if len(cell) > 3 and isinstance(cell[3], Mapping):
            signals.setdefault(technique_id, []).append(cell[3])
    entries: list[dict[str, object]] = []
    for technique_id in sorted(outcomes):
        variants = known[technique_id]
        terminal = outcomes[technique_id] & TERMINAL_CELL_OUTCOMES
        outcome = next(iter(terminal)) if len(terminal) == 1 else "unclear"
        if not terminal and PARTIAL_CELL_OUTCOME in outcomes[technique_id]:
            # Graded engagement is stronger than a bare inconclusive attempt:
            # the cell completed with recorded progress, not with nothing.
            outcome = PARTIAL_CELL_OUTCOME
        # Legacy completion cells carry only the concrete technique id. If an
        # old manifest reused that id for multiple variants, do not guess which
        # variant earned a conclusive outcome: keep every variant retryable.
        if len({technique_cell_parts(item) for item in variants}) > 1:
            outcome = "unclear"
        for cell_technique in variants:
            (
                _,
                controller_id,
                payload_family,
                language_id,
                mutation_id,
            ) = technique_cell_parts(cell_technique)
            entry: dict[str, object] = {
                "target_id": target_id,
                "adapter_generation": adapter_generation,
                "technique_id": technique_id,
                "controller_id": controller_id,
                "payload_family": payload_family,
                "language_id": language_id,
                "mutation_id": mutation_id,
                "outcome": outcome,
                "source_campaign_id": source_campaign_id,
            }
            if outcome == PARTIAL_CELL_OUTCOME:
                entry["partial_signal"] = _partial_signal_payload(signals.get(technique_id, ()))
            entries.append(entry)
    return tuple(entries)


def _partial_signal_payload(signals: Iterable[Mapping[str, object]]) -> dict[str, object]:
    """Merge per-cell deterministic partial signals into one history payload."""

    merged = [dict(signal) for signal in signals]
    evidence_refs = tuple(
        sorted(
            {
                str(ref)
                for signal in merged
                for ref in signal.get("evidence_refs", ())
                if isinstance(ref, str) and ref.strip()
            }
        )
    )
    engaged_attempts = sum(
        int(signal.get("engaged_attempts", 1) or 1)
        for signal in merged
        if str(signal.get("graded_by", "")).strip()
    )
    graded_by = next(
        (str(signal["graded_by"]) for signal in merged if signal.get("graded_by")),
        "completion.cells",
    )
    return {
        "progress": (
            "target engaged with the technique objective and the attempt made "
            "recorded progress, but no confirmed or refuted verdict was reached"
        ),
        "engaged_attempts": engaged_attempts,
        "evidence_refs": evidence_refs,
        "graded_by": graded_by,
    }
