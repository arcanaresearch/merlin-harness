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

# Non-baseline language cells are deferred until the language shows target
# signal. ``confirmed``, ``partial``, and ``unclear`` mean the target produced
# something other than a clean refusal (``refuted``) or an infrastructure
# failure (``blocked``/``blocked_terminal``), so the language is worth its
# budget.
LANGUAGE_SIGNAL_OUTCOMES = frozenset({"confirmed", "partial", "unclear"})
LANGUAGE_DEFERRAL_OUTCOME = "deferred"

# The ten most-spoken languages (front of _SECURITY_LANGUAGE_FRONTIER in the
# TUI bootstrap). LLM-backed targets engage in them with near-certainty, and
# the profile cell cap bounds the spend, so these run without per-target
# engagement evidence. The long tail stays probe/evidence-gated.
UNCONDITIONAL_LANGUAGES = frozenset({"en", "zh", "hi", "es", "ar", "fr", "bn", "pt", "ru", "ur"})

CellParts = tuple[str, str, str, str, str]
"""Technique, controller, transform, language, and mutation variant identity."""


def technique_cell_parts(technique: Any) -> CellParts:
    """Project one assignment technique onto its stable history-cell identity."""

    mutation_seed = str(getattr(technique, "mutation_seed", None) or "").strip()
    mutation_id = (
        "none" if not mutation_seed else "sha256:" + sha256(mutation_seed.encode()).hexdigest()
    )
    return (
        str(technique.id),
        str(technique.strategy_id),
        str(technique.transform_id),
        str(getattr(technique, "language", "und")),
        mutation_id,
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

    Language admission is evidence-gated for the long tail: baseline-language
    cells (the campaign default language or unmarked ``und``) always run first,
    and the ten most-spoken languages (``UNCONDITIONAL_LANGUAGES``) run
    unconditionally — LLM-backed targets engage in them with near-certainty
    and the profile cell cap bounds the spend. Any other non-baseline language
    cell is admitted only when that language already
    has signal for this target — a prior attempt whose outcome was anything
    other than a clean refusal or an infrastructure failure, or an explicit
    ``signal_languages`` entry (e.g. from a capability probe that observed
    the target engaging in that language). Ungated cells are deferred, not
    dropped: each deferral is journaled with its reason so the completion
    document lists it as deferred coverage.
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
    for technique in techniques:
        if str(technique.id).startswith("hypothesis:"):
            continue
        cell_parts = technique_cell_parts(technique)
        if cell_parts in skipped_by_cell:
            continue
        _, controller_id, payload_family, language_id, mutation_id = cell_parts
        language = language_id.strip().casefold()
        if not language or language in baseline or language in signalled:
            continue
        if language in UNCONDITIONAL_LANGUAGES:
            continue
        skipped_by_cell[cell_parts] = {
            "technique_id": str(technique.id),
            "controller_id": controller_id,
            "payload_family": payload_family,
            "language_id": language_id,
            "mutation_id": mutation_id,
            "outcome": LANGUAGE_DEFERRAL_OUTCOME,
            "source_campaign_id": "",
            "reason": (
                f"language cell deferred: no recorded evidence that this target engages "
                f"in {language_id!r} (no capability-probe confirmation and no prior "
                "attempt with an outcome other than a clean refusal or infrastructure "
                "failure); baseline-language cells run first"
            ),
        }

    def _priority(technique: Any) -> int:
        row = history.get(technique_cell_parts(technique))
        if row is None:
            return 0
        # Partial priors rank behind untried and unclear/blocked cells; every
        # other non-terminal outcome keeps its original catalogue order.
        return 1 if normalize_outcome(row.get("outcome")) == PARTIAL_CELL_OUTCOME else 0

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
