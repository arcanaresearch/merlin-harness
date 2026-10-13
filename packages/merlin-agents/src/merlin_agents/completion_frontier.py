"""Pure projection of the evidence-backed campaign completion frontier.

This module deliberately has no scheduler or campaign-service dependency.  It
answers one question: which exact attack candidates and hypotheses remain open
after all durable sources are overlaid?  Persistence and reconnaissance may
describe the frontier, but neither is allowed to decide attack applicability.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from enum import StrEnum
from typing import Any

from merlin_contracts import (
    candidate_evidence_digest,
    candidate_id_for_identity,
    project_candidate_outcome,
    validate_candidate_outcome_projection,
)


class FrontierState(StrEnum):
    RUNNABLE = "runnable"
    UNRESOLVED = "unresolved"
    RETIRED = "retired"
    BUDGET_BLOCKED = "budget_blocked"


@dataclass(frozen=True, slots=True)
class CompletionScope:
    campaign_id: str
    target_id: str
    target_generation: str
    scenario_fingerprint: str

    def __post_init__(self) -> None:
        if missing := [name for name, value in asdict(self).items() if not value.strip()]:
            raise ValueError("completion scope fields must not be empty: " + ", ".join(missing))


@dataclass(frozen=True, slots=True)
class CompletionCandidateIdentity:
    """The daemon-sealed identity of one independently executable candidate."""

    campaign_id: str
    target_id: str
    target_generation: str
    scenario_fingerprint: str
    technique_id: str
    controller_id: str
    payload_family: str
    requested_language: str
    mutation_id: str

    def __post_init__(self) -> None:
        if missing := [name for name, value in asdict(self).items() if not value.strip()]:
            raise ValueError("candidate identity fields must not be empty: " + ", ".join(missing))

    @property
    def key(self) -> tuple[str, ...]:
        return tuple(asdict(self).values())

    def projection(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class VerifiedCandidateOutcome:
    """Trusted output of the durable canonical-outcome evidence verifier."""

    identity: CompletionCandidateIdentity
    outcome: str
    authority_kind: str
    source_evidence_refs: tuple[str, ...]
    verifier_record_id: str
    validator_evidence_refs: tuple[str, ...] = ()
    validator_witness_digests: tuple[str, ...] = ()
    candidate_outcome_projection: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, CompletionCandidateIdentity):
            raise ValueError("verified candidate outcome requires canonical identity")
        if self.outcome not in _TERMINAL_CANDIDATE_OUTCOMES:
            raise ValueError("verified candidate outcome must be confirmed or refuted")
        if self.authority_kind not in {"target", "validator"}:
            raise ValueError("verified candidate outcome authority must be target or validator")
        if (
            not self.source_evidence_refs
            or any(
                not isinstance(item, str) or not item.strip() for item in self.source_evidence_refs
            )
            or len(set(self.source_evidence_refs)) != len(self.source_evidence_refs)
            or not isinstance(self.verifier_record_id, str)
            or not self.verifier_record_id.strip()
        ):
            raise ValueError("verified candidate outcome requires source evidence and verifier id")
        if (
            any(
                not isinstance(item, str) or not item.strip()
                for item in self.validator_evidence_refs
            )
            or any(
                not isinstance(item, str) or not item.strip()
                for item in self.validator_witness_digests
            )
            or len(set(self.validator_evidence_refs)) != len(self.validator_evidence_refs)
            or len(set(self.validator_witness_digests)) != len(self.validator_witness_digests)
        ):
            raise ValueError("verified candidate outcome evidence references must be unique text")
        if self.authority_kind == "target" and (
            self.validator_evidence_refs or self.validator_witness_digests
        ):
            raise ValueError("target authority cannot carry validator evidence")
        if self.authority_kind == "validator" and (
            not self.validator_evidence_refs
            or set(self.source_evidence_refs) & set(self.validator_evidence_refs)
            or set(self.validator_evidence_refs) != set(self.validator_witness_digests)
        ):
            raise ValueError("validator authority requires an exact disjoint witness/ref union")
        evidence_refs = tuple(sorted((*self.source_evidence_refs, *self.validator_evidence_refs)))
        persisted = (
            None
            if self.candidate_outcome_projection is None
            else validate_candidate_outcome_projection(self.candidate_outcome_projection)
        )
        expected = project_candidate_outcome(
            candidate_id=candidate_id_for_identity(self.identity.projection()),
            attempt_disposition="valid",
            outcome_authority=self.authority_kind,
            outcome=self.outcome,
            evidence_digest=(
                candidate_evidence_digest(evidence_refs)
                if persisted is None
                else persisted["evidence_digest"]
            ),
        )
        if persisted is None:
            object.__setattr__(self, "candidate_outcome_projection", expected)
        elif persisted != expected:
            raise ValueError("frontier outcome conflicts with canonical attempt projection")

    def projection(self) -> dict[str, object]:
        return {
            "candidate_identity": self.identity.projection(),
            "outcome": self.outcome,
            "authority_kind": self.authority_kind,
            "source_evidence_refs": self.source_evidence_refs,
            "validator_evidence_refs": self.validator_evidence_refs,
            "validator_witness_digests": self.validator_witness_digests,
            "verifier_record_id": self.verifier_record_id,
            "candidate_outcome_projection": dict(self.candidate_outcome_projection or {}),
        }


@dataclass(frozen=True, slots=True)
class CompletionFrontierEntry:
    key: tuple[str, ...]
    kind: str
    state: FrontierState
    identity: Mapping[str, str]
    sources: tuple[str, ...]
    reasons: tuple[str, ...] = ()
    outcome: str | None = None


@dataclass(frozen=True, slots=True)
class CompletionFrontier:
    entries: tuple[CompletionFrontierEntry, ...]
    advisory_recon_records: int = 0
    ignored_out_of_scope_records: int = 0

    def with_state(self, state: FrontierState) -> tuple[CompletionFrontierEntry, ...]:
        return tuple(item for item in self.entries if item.state is state)

    @property
    def may_complete(self) -> bool:
        """Completion is possible only when no open work or explicit budget block remains."""
        return any(item.kind == "candidate" for item in self.entries) and all(
            item.state is FrontierState.RETIRED for item in self.entries
        )


_TERMINAL_CANDIDATE_OUTCOMES = frozenset({"confirmed", "refuted"})
_RETRYABLE_FAILURE_KINDS = frozenset(
    {
        "adapter_timeout",
        "cleanup_unresolved",
        "configuration",
        "delivery_failure",
        "missing_response_pointer",
        "missing_target_effect",
        "pointer_repair",
        "provider_transient",
        "provider_truncated",
        "result_schema_invalid",
        "target_adapter_unhealthy",
        "transport_transient",
        "unhealthy_target_session",
        "unknown",
        "worker_interrupted",
    }
)
_TERMINAL_HYPOTHESIS_STATES = frozenset({"falsified", "supported", "superseded"})
_HARD_FAILURE_KINDS = frozenset({"authorization", "scope", "state_integrity"})


@dataclass(slots=True)
class _EntryAccumulator:
    kind: str
    identity: dict[str, str]
    sources: set[str]
    reasons: set[str]
    runnable: bool = False
    retired: bool = False
    budget_blocked: bool = False
    hard_blocked: bool = False
    evaluation_uncertain: bool = False
    authoritative_outcomes: set[str] | None = None
    outcome: str | None = None

    def project(
        self, key: tuple[str, ...], *, campaign_budget_exhausted: bool
    ) -> CompletionFrontierEntry:
        state = (
            FrontierState.UNRESOLVED
            if self.evaluation_uncertain or self.hard_blocked
            else FrontierState.RETIRED
            if self.retired
            else FrontierState.BUDGET_BLOCKED
            if self.budget_blocked or campaign_budget_exhausted
            else FrontierState.RUNNABLE
            if self.runnable
            else FrontierState.UNRESOLVED
        )
        return CompletionFrontierEntry(
            key=key,
            kind=self.kind,
            state=state,
            identity=dict(self.identity),
            sources=tuple(sorted(self.sources)),
            reasons=tuple(sorted(self.reasons)),
            outcome="evaluation_uncertain" if self.evaluation_uncertain else self.outcome,
        )


def project_completion_frontier(
    *,
    scope: CompletionScope,
    manifest_catalogue_cells: Sequence[object] = (),
    candidate_outcomes: Sequence[object] = (),
    arm_arms: Sequence[object] = (),
    arm_observations: Sequence[object] = (),
    hypotheses: Sequence[object] = (),
    retryable_failures: Sequence[object] = (),
    recon_applicability: Sequence[object] = (),
    campaign_budget_exhausted: bool = False,
    hypothesis_budget: int | None = None,
    legacy_arm_inputs_are_scope_bound: bool = False,
) -> CompletionFrontier:
    """Union durable frontier sources without turning reconnaissance into admission.

    Catalogue cells, ARM arms and exact outcomes share the same sealed candidate
    identity.  Infrastructure failures remain open.  Hypotheses use their own
    immutable identity and do not inflate the attack-technique namespace.
    ``recon_applicability`` is accepted only so callers can expose how many
    advisory records existed; its content never changes membership or state.
    ``hypothesis_budget`` bounds how many open hypotheses may remain executable
    work; overflow hypotheses become explicit budget-blocked limitations instead
    of unresolved coverage.
    """

    candidates: dict[tuple[str, ...], _EntryAccumulator] = {}
    hypothesis_entries: dict[tuple[str, ...], _EntryAccumulator] = {}
    hypothesis_rank: dict[tuple[str, ...], tuple[int, int, int, str]] = {}
    ignored_out_of_scope = 0

    for raw in manifest_catalogue_cells:
        row = _record(raw)
        identity = _scoped_candidate(row, scope=scope, allow_missing_scope=False)
        if identity is None:
            ignored_out_of_scope += 1
            continue
        entry = _candidate_entry(candidates, identity, "manifest_catalogue")
        entry.runnable = True

    for raw in arm_arms:
        row = _record(raw)
        identity = _scoped_candidate(
            row,
            scope=scope,
            allow_missing_scope=legacy_arm_inputs_are_scope_bound,
        )
        if identity is None:
            ignored_out_of_scope += 1
            continue
        entry = _candidate_entry(candidates, identity, "arm_pool")
        # ARM lifecycle labels are ranking observations, not retirement authority.
        entry.runnable = True

    for raw in candidate_outcomes:
        verified = isinstance(raw, VerifiedCandidateOutcome)
        row = _record(raw)
        identity_row = (
            _nested_record(row, "candidate_identity")
            or _nested_record(_nested_record(row, "strategy_state"), "candidate_identity")
            or row
        )
        identity = _exact_scoped_candidate(identity_row, scope=scope)
        if identity is None:
            ignored_out_of_scope += 1
            continue
        entry = _candidate_entry(candidates, identity, "candidate_outcome")
        outcome = _text(row, "outcome", "state").casefold()
        reason = _text(row, "reason", "summary")
        if reason:
            entry.reasons.add(reason)
        authoritative_outcome = raw.outcome if verified else None
        if authoritative_outcome is not None:
            if entry.authoritative_outcomes is None:
                entry.authoritative_outcomes = set()
            entry.authoritative_outcomes.add(authoritative_outcome)
            if len(entry.authoritative_outcomes) == 1:
                entry.retired = True
                entry.outcome = authoritative_outcome
            else:
                entry.retired = False
                entry.evaluation_uncertain = True
                entry.reasons.add("conflicting authoritative candidate outcomes")
        else:
            # Labels, ambiguity and evaluator disagreement cannot retire work.
            entry.runnable = True
            if not entry.retired and outcome not in _TERMINAL_CANDIDATE_OUTCOMES:
                entry.outcome = outcome or entry.outcome

    for raw in arm_observations:
        row = _record(raw)
        identity_row = _nested_record(row, "identity") or row
        identity = _scoped_candidate(
            identity_row,
            scope=scope,
            allow_missing_scope=legacy_arm_inputs_are_scope_bound,
        )
        if identity is None:
            ignored_out_of_scope += 1
            continue
        entry = _candidate_entry(candidates, identity, "arm_observation")
        outcome = _text(row, "outcome").casefold()
        infrastructure = bool(row.get("infrastructure_failure")) or outcome in {
            "cancelled",
            "infrastructure_failure",
            "provider_failure",
            "rate_limited",
            "timeout",
            "transport_failure",
        }
        if infrastructure:
            entry.runnable = True
            entry.reasons.add(outcome or "infrastructure_failure")
        elif outcome in _TERMINAL_CANDIDATE_OUTCOMES:
            # ARM observations are learning evidence, not authoritative retirement.
            entry.runnable = True
        else:
            entry.runnable = True

    for raw in retryable_failures:
        row = _record(raw)
        identity_row = (
            _nested_record(row, "candidate_identity")
            or _nested_record(_nested_record(row, "strategy_state"), "candidate_identity")
            or row
        )
        identity = _exact_scoped_candidate(identity_row, scope=scope)
        if identity is None:
            ignored_out_of_scope += 1
            continue
        entry = _candidate_entry(candidates, identity, "typed_failure")
        failure_kind = _text(row, "failure_kind", "infrastructure_failure_category").casefold()
        if not failure_kind:
            raise ValueError("typed failure requires failure_kind")
        entry.reasons.add(failure_kind)
        if failure_kind in _HARD_FAILURE_KINDS:
            # A boolean retry hint cannot weaken authorization or state integrity.
            entry.hard_blocked = True
        elif failure_kind == "budget":
            entry.budget_blocked = True
        elif failure_kind in _RETRYABLE_FAILURE_KINDS or row.get("retryable") is True:
            entry.runnable = True
        else:
            entry.runnable = False

    for raw in hypotheses:
        row = _record(raw)
        hypothesis_id = _text(row, "id", "hypothesis_id")
        if not hypothesis_id:
            raise ValueError("hypothesis requires immutable id")
        campaign_id = _text(row, "campaign_id")
        target_id = _text(row, "target_id")
        target_generation = _text(row, "target_generation")
        scenario = _text(row, "scenario_fingerprint")
        if (
            campaign_id != scope.campaign_id
            or target_id != scope.target_id
            or target_generation != scope.target_generation
            or scenario != scope.scenario_fingerprint
        ):
            ignored_out_of_scope += 1
            continue
        key = (
            "hypothesis",
            campaign_id,
            target_id,
            target_generation,
            scenario,
            hypothesis_id,
        )
        entry = hypothesis_entries.setdefault(
            key,
            _EntryAccumulator(
                kind="hypothesis",
                identity={
                    "campaign_id": campaign_id,
                    "target_id": target_id,
                    "target_generation": target_generation,
                    "scenario_fingerprint": scenario,
                    "hypothesis_id": hypothesis_id,
                },
                sources=set(),
                reasons=set(),
            ),
        )
        entry.sources.add("hypothesis")
        state = _text(row, "state").casefold() or "untested"
        reason = _text(row, "state_reason", "reason")
        if reason:
            entry.reasons.add(reason)
        state_evidence_refs = _ref_set(row.get("state_evidence_refs"))
        creating_evidence_refs = _ref_set(
            row.get("creating_evidence_refs") or row.get("evidence_refs")
        )
        hypothesis_rank[key] = (
            int(state != "untested"),
            int(bool(_text(row, "parent_hypothesis_id")) or bool(creating_evidence_refs)),
            len(creating_evidence_refs),
            hypothesis_id,
        )
        if state in _TERMINAL_HYPOTHESIS_STATES and state_evidence_refs:
            entry.retired = True
            entry.outcome = state
        elif state in {"in_progress", "blocked_by_infrastructure"}:
            entry.runnable = True
        # An untested hypothesis is an unresolved idea until compiled into an
        # exact executable candidate; it is not silently treated as completed.

    if hypothesis_budget is not None and len(hypothesis_entries) > hypothesis_budget:
        retired_count = sum(1 for entry in hypothesis_entries.values() if entry.retired)
        slots = max(0, hypothesis_budget - retired_count)
        ranked_open = sorted(
            ((key, entry) for key, entry in hypothesis_entries.items() if not entry.retired),
            key=lambda pair: hypothesis_rank[pair[0]],
            reverse=True,
        )
        for _key, entry in ranked_open[slots:]:
            entry.runnable = False
            entry.budget_blocked = True
            entry.reasons.add(
                "hypothesis_budget_capped: untested beyond the campaign hypothesis budget"
            )

    all_entries = {**candidates, **hypothesis_entries}
    projected = tuple(
        all_entries[key].project(key, campaign_budget_exhausted=campaign_budget_exhausted)
        for key in sorted(all_entries)
    )
    return CompletionFrontier(
        entries=projected,
        advisory_recon_records=len(recon_applicability),
        ignored_out_of_scope_records=ignored_out_of_scope,
    )


def _candidate_entry(
    entries: dict[tuple[str, ...], _EntryAccumulator],
    identity: CompletionCandidateIdentity,
    source: str,
) -> _EntryAccumulator:
    entry = entries.setdefault(
        identity.key,
        _EntryAccumulator(
            kind="candidate",
            identity=identity.projection(),
            sources=set(),
            reasons=set(),
        ),
    )
    entry.sources.add(source)
    return entry


def _candidate_identity(
    row: Mapping[str, Any],
    *,
    scope: CompletionScope,
    require_exact: bool = False,
) -> CompletionCandidateIdentity:
    nested = _nested_record(row, "candidate_identity")
    value = nested or row
    exact_fields = tuple(CompletionCandidateIdentity.__dataclass_fields__)
    if require_exact and any(not _text(value, field) for field in exact_fields):
        raise ValueError("exact candidate record is missing sealed identity fields")
    technique_id = _text(value, "technique_id", "semantic_id", "id")
    if not technique_id:
        raise ValueError("frontier candidate requires technique_id")
    return CompletionCandidateIdentity(
        campaign_id=_text(value, "campaign_id") or scope.campaign_id,
        target_id=_text(value, "target_id") or scope.target_id,
        target_generation=_text(value, "target_generation") or scope.target_generation,
        scenario_fingerprint=(_text(value, "scenario_fingerprint") or scope.scenario_fingerprint),
        technique_id=technique_id,
        controller_id=_text(value, "controller_id", "strategy_id") or "direct",
        payload_family=_text(value, "payload_family", "transform_id") or "baseline",
        requested_language=(
            _text(value, "requested_language", "language_id", "language", "actual_language")
            or "und"
        ).casefold(),
        mutation_id=_text(value, "mutation_id", "transform_id") or "baseline",
    )


def _exact_scoped_candidate(
    row: Mapping[str, Any], *, scope: CompletionScope
) -> CompletionCandidateIdentity | None:
    fields = tuple(CompletionCandidateIdentity.__dataclass_fields__)
    if any(not _text(row, field) for field in fields):
        return None
    identity = _candidate_identity(row, scope=scope, require_exact=True)
    if identity.key[:4] != (
        scope.campaign_id,
        scope.target_id,
        scope.target_generation,
        scope.scenario_fingerprint,
    ):
        return None
    return identity


def _scoped_candidate(
    row: Mapping[str, Any], *, scope: CompletionScope, allow_missing_scope: bool
) -> CompletionCandidateIdentity | None:
    expected = {
        "campaign_id": scope.campaign_id,
        "target_id": scope.target_id,
        "target_generation": scope.target_generation,
        "scenario_fingerprint": scope.scenario_fingerprint,
    }
    for field, value in expected.items():
        observed = _text(row, field)
        if observed and observed != value:
            return None
        if not observed and not allow_missing_scope:
            return None
    return _candidate_identity(row, scope=scope)


def _ref_set(value: object) -> set[str]:
    if not isinstance(value, (tuple, list, set, frozenset)):
        return set()
    return {str(item) for item in value if isinstance(item, str) and item.strip()}


def _record(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    projection = getattr(value, "projection", None)
    if callable(projection):
        result = projection()
        if isinstance(result, Mapping):
            return result
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        result = model_dump(mode="json")
        if isinstance(result, Mapping):
            return result
    if is_dataclass(value) and not isinstance(value, type):
        result = asdict(value)
        if isinstance(result, Mapping):
            return result
    raise TypeError("frontier inputs must be mappings or projectable records")


def _nested_record(value: Mapping[str, Any] | None, key: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    nested = value.get(key)
    return nested if isinstance(nested, Mapping) else {}


def _text(value: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        item = value.get(key)
        if item is not None and str(item).strip():
            return str(item).strip()
    return ""
