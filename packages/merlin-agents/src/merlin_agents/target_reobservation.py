"""Authenticated target-surface transition obligations for campaign scheduling."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from .runtime_ui_transition import (
    normalize_target_ui_transition as _normalize_runtime_target_ui_transition,
)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
_PROJECTIONS = ("target_evidence_receipts", "target_ui_transition_receipts")


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _text(value: object, *, limit: int, required: bool = False) -> str | None:
    if not isinstance(value, str) or len(value) > limit or required and not value:
        return None
    return value


def normalize_target_ui_transition(value: object) -> tuple[dict[str, object], str] | None:
    """Reuse the runtime projection and bind its stable transition evidence digest."""

    if not isinstance(value, Mapping):
        return None
    projected = _normalize_runtime_target_ui_transition({"target_ui_transition": value})
    if projected is None:
        return None
    transition_digest = _digest([projected["before_digest"], projected["after_digest"]])
    claimed = value.get("transition_digest")
    if claimed is not None and claimed != transition_digest:
        return None
    projected = dict(projected)
    projected["transition_digest"] = transition_digest
    return projected, transition_digest


def normalize_target_surface_generation_receipt(
    value: object,
) -> Mapping[str, object] | None:
    """Validate one daemon-authored passive surface-generation receipt."""

    if not isinstance(value, Mapping):
        return None
    required_text = {
        "campaign_id": 256,
        "session_id": 256,
        "target_id": 256,
        "generation_id": 71,
        "event_set_digest": 71,
        "assignment_id": 512,
        "attempt_id": 256,
        "call_id": 256,
        "context_id": 256,
        "tab_ref": 256,
        "observed_at": 64,
        "request_evidence_ref": 512,
        "response_evidence_ref": 512,
        "surface_observation_digest": 71,
        "current_surface_digest": 71,
        "compiled_adapter_digest": 71,
        "evidence_ref": 128,
        "receipt_sha256": 64,
    }
    if (
        set(value) != {"schema", *required_text}
        or value.get("schema") != "merlin.target-surface-generation.v1"
        or any(
            _text(value.get(field), limit=limit, required=True) is None
            for field, limit in required_text.items()
        )
    ):
        return None
    for field in (
        "generation_id",
        "event_set_digest",
        "surface_observation_digest",
        "current_surface_digest",
        "compiled_adapter_digest",
    ):
        if _DIGEST.fullmatch(str(value[field])) is None:
            return None
    if (
        _HEX_DIGEST.fullmatch(str(value["receipt_sha256"])) is None
        or value["evidence_ref"] != "target-surface-generation:sha256:" + value["receipt_sha256"]
    ):
        return None
    try:
        observed_at = datetime.fromisoformat(str(value["observed_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    if observed_at.tzinfo is None:
        return None
    projected_body = {
        "schema": "merlin.target-surface-generation.v1",
        **{
            field: value[field]
            for field in required_text
            if field not in {"evidence_ref", "receipt_sha256"}
        },
    }
    expected_surface = _digest(
        {
            "schema": "merlin.target-surface-generation-surface.v1",
            "session_id": projected_body["session_id"],
            "context_id": projected_body["context_id"],
            "target_id": projected_body["target_id"],
            "tab_ref": projected_body["tab_ref"],
            "observed_at": projected_body["observed_at"],
            "request_evidence_ref": projected_body["request_evidence_ref"],
            "response_evidence_ref": projected_body["response_evidence_ref"],
            "surface_observation_digest": projected_body["surface_observation_digest"],
            "compiled_adapter_digest": projected_body["compiled_adapter_digest"],
        }
    )
    if projected_body["current_surface_digest"] != expected_surface:
        return None
    receipt_sha256 = _digest(projected_body).removeprefix("sha256:")
    if value["receipt_sha256"] != receipt_sha256:
        return None
    return {
        **projected_body,
        "evidence_ref": value["evidence_ref"],
        "receipt_sha256": receipt_sha256,
    }


@dataclass(frozen=True, slots=True)
class TargetReobservationObligation:
    campaign_id: str
    session_id: str
    target_id: str
    event_digest: str
    transition_digest: str
    source_schedule_id: str
    source_wave: int
    source_task_id: str
    target_call_id: str
    receipt_sha256: str
    receipt_projection: str
    transition: Mapping[str, object]

    @property
    def obligation_id(self) -> str:
        return "target-reobservation:" + self.event_digest.removeprefix("sha256:")

    def observed_payload(self, campaign_id: str, target_id: str) -> dict[str, object]:
        return {
            "kind": "target_surface_transition_observed",
            "schema": "merlin.target-surface-transition-observed.v1",
            "campaign_id": campaign_id,
            "session_id": self.session_id,
            "target_id": target_id,
            "obligation_id": self.obligation_id,
            "event_digest": self.event_digest,
            "transition_digest": self.transition_digest,
            "source_schedule_id": self.source_schedule_id,
            "source_wave": self.source_wave,
            "source_task_id": self.source_task_id,
            "target_call_id": self.target_call_id,
            "target_receipt_sha256": self.receipt_sha256,
            "receipt_projection": self.receipt_projection,
            "target_ui_transition": dict(self.transition),
        }


def extract_target_reobservation_obligations(
    *,
    campaign_id: str,
    session_id: str,
    target_id: str,
    source_schedule_id: str,
    source_wave: int,
    task_ids: Sequence[str],
    evidence: Mapping[str, Mapping[str, object]],
) -> tuple[TargetReobservationObligation, ...]:
    """Extract signed success and non-credit error surface-transition receipts."""

    found: dict[str, TargetReobservationObligation] = {}
    for task_id in sorted(set(task_ids)):
        committed = evidence.get(task_id)
        if not isinstance(committed, Mapping):
            continue
        for projection in _PROJECTIONS:
            receipts = committed.get(projection)
            if not isinstance(receipts, Mapping):
                continue
            for raw_call_id, receipt in receipts.items():
                call_id = str(raw_call_id)
                if not isinstance(receipt, Mapping):
                    continue
                receipt_sha256 = receipt.get("receipt_sha256")
                body = {key: item for key, item in receipt.items() if key != "receipt_sha256"}
                provider_call_id = receipt.get("provider_call_id")
                if (
                    receipt.get("call_id") != call_id
                    or receipt.get("reference") != f"target-call:{call_id}"
                    or receipt.get("assignment_id") != task_id
                    or receipt.get("campaign_id") != campaign_id
                    or receipt.get("session_id") != session_id
                    or receipt.get("target_id") != target_id
                    or not isinstance(receipt.get("tool_name"), str)
                    or not receipt["tool_name"]
                    or not isinstance(receipt_sha256, str)
                    or _HEX_DIGEST.fullmatch(receipt_sha256) is None
                    or _digest(body).removeprefix("sha256:") != receipt_sha256
                    or provider_call_id is not None
                    and (
                        not isinstance(provider_call_id, str)
                        or not provider_call_id
                        or provider_call_id == call_id
                    )
                ):
                    continue
                if projection == "target_evidence_receipts":
                    if receipt.get("is_error") is not False:
                        continue
                else:
                    attempted = receipt.get("target_effect_attempted")
                    generic_browser_transition = bool(
                        receipt.get("generic_browser_effect") is True
                        and isinstance(receipt.get("compiled_adapter_digest"), str)
                        and _DIGEST.fullmatch(str(receipt["compiled_adapter_digest"])) is not None
                        and receipt.get("generic_browser_effect_class")
                        in {"target_network", "target_read"}
                    )
                    strict_zero_effect = attempted is False and all(
                        type(receipt.get(name)) is int and receipt.get(name) == 0
                        for name in (
                            "interaction_count",
                            "limit_interaction_count",
                            "target_call_budget_charge",
                        )
                    )
                    generic_effect_valid = bool(
                        generic_browser_transition
                        and type(receipt.get("is_error")) is bool
                        and attempted is True
                        and receipt.get("interaction_count") == 0
                        and receipt.get("limit_interaction_count") == 0
                        and receipt.get("target_call_budget_charge") == 1
                    )
                    legacy_error_valid = bool(
                        not generic_browser_transition
                        and receipt.get("is_error") is True
                        and (attempted is True or strict_zero_effect)
                    )
                    if not (generic_effect_valid or legacy_error_valid):
                        continue
                normalized = normalize_target_ui_transition(receipt.get("target_ui_transition"))
                if normalized is None:
                    continue
                transition, transition_digest = normalized
                if (
                    projection == "target_ui_transition_receipts"
                    and receipt.get("target_effect_attempted") is False
                    and transition["adapter_stale"] is not True
                ):
                    continue
                event_digest = _digest(
                    [campaign_id, task_id, call_id, receipt_sha256, transition_digest]
                )
                found.setdefault(
                    event_digest,
                    TargetReobservationObligation(
                        campaign_id=campaign_id,
                        session_id=session_id,
                        target_id=target_id,
                        event_digest=event_digest,
                        transition_digest=transition_digest,
                        source_schedule_id=source_schedule_id,
                        source_wave=source_wave,
                        source_task_id=task_id,
                        target_call_id=call_id,
                        receipt_sha256=receipt_sha256,
                        receipt_projection=projection,
                        transition=transition,
                    ),
                )
    return tuple(found[key] for key in sorted(found))


def _obligation(payload: Mapping[str, object]) -> TargetReobservationObligation | None:
    normalized = normalize_target_ui_transition(payload.get("target_ui_transition"))
    source_wave = payload.get("source_wave")
    values = (
        payload.get("campaign_id"),
        payload.get("session_id"),
        payload.get("target_id"),
        payload.get("event_digest"),
        payload.get("transition_digest"),
        payload.get("source_schedule_id"),
        payload.get("source_task_id"),
        payload.get("target_call_id"),
        payload.get("target_receipt_sha256"),
        payload.get("receipt_projection"),
    )
    if (
        payload.get("kind") != "target_surface_transition_observed"
        or payload.get("schema") != "merlin.target-surface-transition-observed.v1"
        or normalized is None
        or type(source_wave) is not int
        or any(not isinstance(item, str) or not item for item in values)
        or _DIGEST.fullmatch(str(values[3])) is None
        or normalized[1] != values[4]
        or _HEX_DIGEST.fullmatch(str(values[8])) is None
        or values[9] not in _PROJECTIONS
        or values[3] != _digest([values[0], values[6], values[7], values[8], values[4]])
        or payload.get("obligation_id")
        != "target-reobservation:" + str(values[3]).removeprefix("sha256:")
    ):
        return None
    return TargetReobservationObligation(
        campaign_id=str(values[0]),
        session_id=str(values[1]),
        target_id=str(values[2]),
        event_digest=str(values[3]),
        transition_digest=str(values[4]),
        source_schedule_id=str(values[5]),
        source_wave=source_wave,
        source_task_id=str(values[6]),
        target_call_id=str(values[7]),
        receipt_sha256=str(values[8]),
        receipt_projection=str(values[9]),
        transition=normalized[0],
    )


@dataclass(frozen=True, slots=True)
class TargetReobservationGeneration:
    campaign_id: str
    session_id: str
    target_id: str
    generation_id: str
    event_digests: tuple[str, ...]
    observation_cutoff_sequence: int
    source_wave: int

    @property
    def obligation_id(self) -> str:
        return "target-reobservation:" + self.generation_id.removeprefix("sha256:")


@dataclass(frozen=True, slots=True)
class TargetReobservationOverflow:
    campaign_id: str
    session_id: str
    target_id: str
    generation_id: str
    event_set_digest: str
    event_count: int
    source_wave: int


def target_reobservation_generation(
    pending: Sequence[TargetReobservationObligation],
    observation_sequences: Mapping[str, int],
) -> TargetReobservationGeneration | None:
    if not pending or len(pending) > 256:
        return None
    campaign_ids = {item.campaign_id for item in pending}
    session_ids = {item.session_id for item in pending}
    target_ids = {item.target_id for item in pending}
    if len(campaign_ids) != 1 or len(session_ids) != 1 or len(target_ids) != 1:
        return None
    ordered = tuple(
        sorted(
            (item.event_digest for item in pending),
            key=lambda event: (observation_sequences.get(event, 0), event),
        )
    )
    if any(event not in observation_sequences for event in ordered):
        return None
    cutoff = max(observation_sequences[event] for event in ordered)
    campaign_id = next(iter(campaign_ids))
    session_id = next(iter(session_ids))
    target_id = next(iter(target_ids))
    generation_id = _digest(
        {
            "schema": "merlin.target-reobservation-generation.v1",
            "campaign_id": campaign_id,
            "session_id": session_id,
            "target_id": target_id,
            "observation_cutoff_sequence": cutoff,
            "event_digests": ordered,
        }
    )
    return TargetReobservationGeneration(
        campaign_id,
        session_id,
        target_id,
        generation_id,
        ordered,
        cutoff,
        max(item.source_wave for item in pending),
    )


def target_reobservation_schedule_id(campaign_id: str, generation_id: str, attempt: int) -> str:
    """Return the only scheduler identity authorized for a generation attempt."""

    if (
        not isinstance(campaign_id, str)
        or not campaign_id
        or not isinstance(generation_id, str)
        or _DIGEST.fullmatch(generation_id) is None
        or type(attempt) is not int
        or attempt < 1
    ):
        raise ValueError("target re-observation schedule identity is invalid")
    return str(
        uuid5(
            NAMESPACE_URL,
            f"merlin.target-reobservation.v1\n{campaign_id}\n{generation_id}\n{attempt}",
        )
    )


@dataclass(frozen=True, slots=True)
class TargetReobservationProjection:
    pending: tuple[TargetReobservationObligation, ...]
    observation_sequences: Mapping[str, int]
    generations: Mapping[str, TargetReobservationGeneration]
    starts: Mapping[str, tuple[str, ...]]
    blocked: frozenset[str]
    retry_authorizations: Mapping[str, Mapping[int, str]]
    overflow: TargetReobservationOverflow | None
    refreshed: Mapping[str, Mapping[str, object]]
    reviewed: Mapping[str, str]

    def active_schedule(self, generation_id: str) -> str | None:
        return next(
            (
                schedule_id
                for schedule_id in reversed(self.starts.get(generation_id, ()))
                if schedule_id not in self.blocked
            ),
            None,
        )

    def eligible(self, generation_id: str) -> bool:
        starts = self.starts.get(generation_id, ())
        return self.active_schedule(generation_id) is None and (
            not starts or len(starts) in self.retry_authorizations.get(generation_id, {})
        )

    @property
    def current_generation(self) -> TargetReobservationGeneration | None:
        unresolved = next(
            (
                generation
                for generation_id, generation in self.generations.items()
                if generation_id not in self.refreshed
            ),
            None,
        )
        if unresolved is not None:
            return unresolved
        return target_reobservation_generation(self.pending, self.observation_sequences)

    @property
    def latest_refreshed(self) -> Mapping[str, object] | None:
        return next(reversed(self.refreshed.values()), None)

    @property
    def review_pending(self) -> tuple[Mapping[str, object], ...]:
        return tuple(
            payload
            for generation_id, payload in self.refreshed.items()
            if generation_id not in self.reviewed
        )

    def lead_review_bindings(self) -> tuple[Mapping[str, str], ...]:
        return tuple(
            {
                "generation_id": str(payload["generation_id"]),
                "settlement_id": str(payload["settlement_id"]),
            }
            for payload in self.review_pending
        )

    def lead_projection(self, *, latest_wave: int | None = None) -> Mapping[str, object]:
        """Expose bounded state identities without control labels or action authority."""

        current = self.current_generation
        pending = tuple(
            {
                "event_digest": item.event_digest,
                "transition_digest": item.transition_digest,
                "adapter_stale": item.transition["adapter_stale"],
                "after_digest": item.transition["after_digest"],
                "changed_dimensions": item.transition["changed_dimensions"],
                "evidence_refs": item.transition["evidence_refs"],
            }
            for item in self.pending[-16:]
        )
        generation = (
            {
                "generation_id": current.generation_id,
                "event_count": len(current.event_digests),
                "event_set_digest": _digest({"event_digests": current.event_digests}),
                "status": (
                    "reobserving"
                    if self.active_schedule(current.generation_id)
                    else "ready"
                    if self.eligible(current.generation_id)
                    else "blocked_waiting_operator"
                ),
            }
            if current is not None
            else {
                "generation_id": self.overflow.generation_id,
                "event_count": self.overflow.event_count,
                "event_set_digest": self.overflow.event_set_digest,
                "status": "blocked_event_overflow",
            }
            if self.overflow is not None
            else None
        )
        unreviewed = tuple(
            payload
            for generation_id, payload in self.refreshed.items()
            if generation_id not in self.reviewed
        )
        reviewable = (
            unreviewed[:16]
            if unreviewed
            else tuple(
                payload
                for payload in self.refreshed.values()
                if latest_wave is not None and latest_wave <= int(payload["source_wave"])
            )[-16:]
        )
        refreshed = tuple(
            {
                key: payload[key]
                for key in (
                    "generation_id",
                    "settlement_id",
                    "event_count",
                    "event_set_digest",
                    "status",
                    "current_surface_digest",
                    "surface_evidence_digest",
                    "surface_evidence_refs",
                    "recon_handoff_id",
                    "compiled_adapter_digest",
                    "reviewed_adapter_digest",
                    "result_reference",
                    "result_digest",
                )
                if key in payload
            }
            for payload in reviewable
        )
        return {"pending": pending, "generation": generation, "refreshed": refreshed}

    def recovery_state(self, *, latest_wave: int | None) -> Mapping[str, object] | None:
        """Return the exact passive barrier that durable recovery may execute."""

        generation = self.current_generation
        if generation is not None:
            active_schedule = self.active_schedule(generation.generation_id)
            starts = self.starts.get(generation.generation_id, ())
            recovery_schedule = active_schedule or (starts[-1] if starts else None)
            event_set_digest = _digest({"event_digests": generation.event_digests})
            return {
                "schema": "merlin.target-reobservation-recovery-state.v1",
                "phase": "reobserve",
                "generation_id": generation.generation_id,
                "event_set_digest": event_set_digest,
                "source_wave": generation.source_wave,
                "schedule_id": recovery_schedule,
                "settlement_id": None,
                "lead_decision_id": None,
            }
        if self.overflow is not None:
            return {
                "schema": "merlin.target-reobservation-recovery-state.v1",
                "phase": "reobserve",
                "generation_id": self.overflow.generation_id,
                "event_set_digest": self.overflow.event_set_digest,
                "source_wave": self.overflow.source_wave,
                "schedule_id": None,
                "settlement_id": None,
                "lead_decision_id": None,
            }
        for generation_id, settlement in self.refreshed.items():
            reviewed = self.reviewed.get(generation_id)
            source_wave = int(settlement["source_wave"])
            if reviewed is not None and latest_wave is not None and latest_wave > source_wave:
                continue
            return {
                "schema": "merlin.target-reobservation-recovery-state.v1",
                "phase": "review",
                "generation_id": generation_id,
                "event_set_digest": settlement["event_set_digest"],
                "source_wave": source_wave,
                "schedule_id": settlement["schedule_id"],
                "settlement_id": settlement["settlement_id"],
                "lead_decision_id": reviewed,
            }
        return None


def _generation_from_start(
    payload: Mapping[str, object],
    observed: Mapping[str, TargetReobservationObligation],
    observation_sequences: Mapping[str, int],
) -> TargetReobservationGeneration | None:
    events = payload.get("event_digests")
    cutoff = payload.get("observation_cutoff_sequence")
    source_wave = payload.get("source_wave")
    attempt = payload.get("attempt")
    schedule_id = payload.get("schedule_id")
    if (
        payload.get("schema") != "merlin.target-reobservation-started.v1"
        or payload.get("wave_kind") != "objective_prerequisite"
        or not isinstance(events, (list, tuple))
        or not events
        or len(events) > 256
        or len(set(events)) != len(events)
        or any(not isinstance(item, str) or _DIGEST.fullmatch(item) is None for item in events)
        or type(cutoff) is not int
        or cutoff < 1
        or type(source_wave) is not int
        or type(attempt) is not int
        or attempt < 1
        or not isinstance(schedule_id, str)
    ):
        return None
    obligations = tuple(observed.get(str(event)) for event in events)
    if any(item is None for item in obligations):
        return None
    values = tuple(item for item in obligations if item is not None)
    if any(observation_sequences[item.event_digest] > cutoff for item in values):
        return None
    generation = target_reobservation_generation(values, observation_sequences)
    if (
        generation is None
        or generation.observation_cutoff_sequence != cutoff
        or generation.source_wave != source_wave
        or tuple(events) != generation.event_digests
        or payload.get("campaign_id") != generation.campaign_id
        or payload.get("session_id") != generation.session_id
        or payload.get("target_id") != generation.target_id
        or payload.get("generation_id") != generation.generation_id
        or payload.get("obligation_id") != generation.obligation_id
        or payload.get("event_set_digest") != _digest({"event_digests": generation.event_digests})
        or schedule_id
        != target_reobservation_schedule_id(
            generation.campaign_id, generation.generation_id, attempt
        )
    ):
        return None
    return generation


def project_target_reobservations(
    rows: Sequence[Mapping[str, object]],
    *,
    campaign_id: str | None = None,
    session_id: str | None = None,
    target_id: str | None = None,
) -> TargetReobservationProjection:
    observed: dict[str, TargetReobservationObligation] = {}
    observation_sequences: dict[str, int] = {}
    generations: dict[str, TargetReobservationGeneration] = {}
    starts: dict[str, list[str]] = {}
    blocked: set[str] = set()
    authorizations: dict[str, dict[int, str]] = {}
    authorization_commands: dict[str, set[str]] = {}
    refreshed: dict[str, Mapping[str, object]] = {}
    reviewed: dict[str, str] = {}
    settled_events: set[str] = set()
    for position, row in enumerate(rows, 1):
        payload = row.get("payload") if isinstance(row, Mapping) else None
        if not isinstance(payload, Mapping):
            continue
        sequence = row.get("sequence") if isinstance(row, Mapping) else None
        sequence = sequence if type(sequence) is int and sequence > 0 else position
        kind = payload.get("kind")
        event_digest = payload.get("event_digest")
        if kind == "target_surface_transition_observed" and isinstance(event_digest, str):
            obligation = _obligation(payload)
            if (
                obligation is not None
                and (campaign_id is None or obligation.campaign_id == campaign_id)
                and (session_id is None or obligation.session_id == session_id)
                and (target_id is None or obligation.target_id == target_id)
            ):
                observed.setdefault(event_digest, obligation)
                observation_sequences.setdefault(event_digest, sequence)
        elif kind == "target_reobservation_started":
            generation = _generation_from_start(payload, observed, observation_sequences)
            schedule_id = payload.get("schedule_id")
            attempt = payload.get("attempt")
            if (
                generation is not None
                and isinstance(schedule_id, str)
                and schedule_id
                and type(attempt) is int
            ):
                generation_id = generation.generation_id
                accepted_starts = starts.get(generation_id, [])
                if attempt == 1:
                    pending_prefix = tuple(
                        item
                        for event, item in observed.items()
                        if event not in settled_events and observation_sequences[event] < sequence
                    )
                    expected = target_reobservation_generation(
                        pending_prefix, observation_sequences
                    )
                    authorized = (
                        not accepted_starts
                        and generation_id not in generations
                        and expected == generation
                    )
                else:
                    authorized = bool(
                        generations.get(generation_id) == generation
                        and attempt == len(accepted_starts) + 1
                        and accepted_starts
                        and accepted_starts[-1] in blocked
                        and attempt - 1 in authorizations.get(generation_id, {})
                    )
                if authorized:
                    generations.setdefault(generation_id, generation)
                    starts.setdefault(generation_id, []).append(schedule_id)
        elif kind == "target_reobservation_blocked":
            generation_id = payload.get("generation_id")
            schedule_id = payload.get("schedule_id")
            generation = generations.get(str(generation_id))
            attempt = payload.get("attempt")
            expected_starts = starts.get(str(generation_id), ())
            if (
                generation is not None
                and payload.get("schema") == "merlin.target-reobservation-blocked.v1"
                and payload.get("campaign_id") == generation.campaign_id
                and payload.get("session_id") == generation.session_id
                and payload.get("target_id") == generation.target_id
                and payload.get("obligation_id") == generation.obligation_id
                and tuple(payload.get("event_digests") or ()) == generation.event_digests
                and payload.get("event_count") == len(generation.event_digests)
                and payload.get("event_set_digest")
                == _digest({"event_digests": generation.event_digests})
                and payload.get("observation_cutoff_sequence")
                == generation.observation_cutoff_sequence
                and payload.get("source_wave") == generation.source_wave
                and type(attempt) is int
                and 1 <= attempt <= len(expected_starts)
                and schedule_id == expected_starts[attempt - 1]
            ):
                blocked.add(schedule_id)
        elif kind == "target_reobservation_retry_authorized":
            generation_id = payload.get("generation_id")
            command_id = payload.get("command_id")
            generation = generations.get(str(generation_id))
            authorized_after_attempt = payload.get("authorized_after_attempt")
            generation_starts = starts.get(str(generation_id), ())
            if (
                generation is not None
                and payload.get("schema") == "merlin.target-reobservation-retry-authorized.v1"
                and payload.get("campaign_id") == generation.campaign_id
                and payload.get("session_id") == generation.session_id
                and payload.get("target_id") == generation.target_id
                and payload.get("obligation_id") == generation.obligation_id
                and payload.get("event_set_digest")
                == _digest({"event_digests": generation.event_digests})
                and type(authorized_after_attempt) is int
                and authorized_after_attempt == len(generation_starts)
                and authorized_after_attempt > 0
                and generation_starts[-1] in blocked
                and isinstance(command_id, str)
                and command_id
                and command_id not in authorization_commands.setdefault(str(generation_id), set())
                and authorized_after_attempt
                not in authorizations.setdefault(str(generation_id), {})
            ):
                authorizations[str(generation_id)][authorized_after_attempt] = command_id
                authorization_commands[str(generation_id)].add(command_id)
        elif kind == "target_reobservation_settled" and payload.get("status") == "refreshed":
            generation_id = payload.get("generation_id")
            generation = generations.get(str(generation_id))
            schedule_id = payload.get("schedule_id")
            event_digests = payload.get("event_digests")
            evidence_refs = payload.get("surface_evidence_refs")
            digests = (
                payload.get("current_surface_digest"),
                payload.get("surface_evidence_digest"),
                payload.get("compiled_adapter_digest"),
                payload.get("reviewed_adapter_digest"),
                payload.get("result_digest"),
            )
            settlement_id = payload.get("settlement_id")
            attempt = payload.get("attempt")
            generation_starts = starts.get(str(generation_id), ())
            active_schedule = next(
                (item for item in reversed(generation_starts) if item not in blocked),
                None,
            )
            expected_event_set = (
                _digest({"event_digests": generation.event_digests})
                if generation is not None
                else None
            )
            expected_surface_evidence = (
                _digest({"evidence_refs": sorted(str(item) for item in evidence_refs)})
                if isinstance(evidence_refs, (list, tuple))
                else None
            )
            if (
                generation is not None
                and payload.get("schema") == "merlin.target-reobservation-settlement.v1"
                and payload.get("campaign_id") == generation.campaign_id
                and payload.get("session_id") == generation.session_id
                and payload.get("target_id") == generation.target_id
                and payload.get("obligation_id") == generation.obligation_id
                and payload.get("event_set_digest") == expected_event_set
                and payload.get("event_count") == len(generation.event_digests)
                and payload.get("observation_cutoff_sequence")
                == generation.observation_cutoff_sequence
                and payload.get("source_wave") == generation.source_wave
                and isinstance(schedule_id, str)
                and schedule_id == active_schedule
                and type(attempt) is int
                and attempt == len(generation_starts)
                and tuple(event_digests or ()) == generation.event_digests
                and settlement_id
                == "target-reobservation-settled/"
                + generation.generation_id.removeprefix("sha256:")
                and all(isinstance(item, str) and _DIGEST.fullmatch(item) for item in digests)
                and isinstance(evidence_refs, (list, tuple))
                and 1 <= len(evidence_refs) <= 8
                and all(_text(item, limit=256, required=True) is not None for item in evidence_refs)
                and payload.get("surface_evidence_digest") == expected_surface_evidence
                and isinstance(payload.get("recon_handoff_id"), str)
                and bool(payload["recon_handoff_id"])
                and isinstance(payload.get("result_reference"), str)
                and bool(payload["result_reference"])
            ):
                refreshed[generation.generation_id] = dict(payload)
                settled_events.update(generation.event_digests)
        decision_id = row.get("decision_id") if isinstance(row, Mapping) else None
        bindings = payload.get("target_reobservation_generations")
        if (
            isinstance(decision_id, str)
            and re.fullmatch(r"lead-decision:[0-9a-f]{64}", decision_id) is not None
            and isinstance(bindings, (list, tuple))
            and len(bindings) <= 16
        ):
            for binding in bindings:
                if not isinstance(binding, Mapping) or set(binding) != {
                    "generation_id",
                    "settlement_id",
                }:
                    continue
                generation_id = binding.get("generation_id")
                settlement_id = binding.get("settlement_id")
                settlement = refreshed.get(str(generation_id))
                if (
                    isinstance(generation_id, str)
                    and isinstance(settlement_id, str)
                    and settlement is not None
                    and settlement.get("settlement_id") == settlement_id
                ):
                    reviewed[generation_id] = decision_id
    pending = tuple(
        sorted(
            (item for key, item in observed.items() if key not in settled_events),
            key=lambda item: (observation_sequences[item.event_digest], item.event_digest),
        )
    )
    overflow: TargetReobservationOverflow | None = None
    if len(pending) > 256 and not any(
        generation_id not in refreshed for generation_id in generations
    ):
        campaign_ids = {item.campaign_id for item in pending}
        session_ids = {item.session_id for item in pending}
        target_ids = {item.target_id for item in pending}
        if len(campaign_ids) == len(session_ids) == len(target_ids) == 1:
            event_digests = tuple(item.event_digest for item in pending)
            event_set_digest = _digest({"event_digests": event_digests})
            overflow = TargetReobservationOverflow(
                campaign_id=next(iter(campaign_ids)),
                session_id=next(iter(session_ids)),
                target_id=next(iter(target_ids)),
                generation_id=_digest(
                    {
                        "schema": "merlin.target-reobservation-overflow.v1",
                        "campaign_id": next(iter(campaign_ids)),
                        "session_id": next(iter(session_ids)),
                        "target_id": next(iter(target_ids)),
                        "event_set_digest": event_set_digest,
                        "event_count": len(event_digests),
                    }
                ),
                event_set_digest=event_set_digest,
                event_count=len(event_digests),
                source_wave=max(item.source_wave for item in pending),
            )
    return TargetReobservationProjection(
        pending,
        observation_sequences,
        generations,
        {key: tuple(value) for key, value in starts.items()},
        frozenset(blocked),
        {key: dict(value) for key, value in authorizations.items()},
        overflow,
        refreshed,
        reviewed,
    )


def target_reobservation_schedule_ids(
    rows: Sequence[Mapping[str, object]],
    *,
    campaign_id: str,
    session_id: str,
    target_id: str,
) -> tuple[str, ...]:
    """Return only schedules whose full journal authority matches the campaign."""

    projection = project_target_reobservations(
        rows,
        campaign_id=campaign_id,
        session_id=session_id,
        target_id=target_id,
    )
    return tuple(
        sorted(
            {
                schedule_id
                for generation_id, starts in projection.starts.items()
                if (generation := projection.generations.get(generation_id)) is not None
                and generation.campaign_id == campaign_id
                and generation.session_id == session_id
                and generation.target_id == target_id
                for schedule_id in starts
            }
        )
    )
