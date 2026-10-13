"""Canonical candidate outcome projection shared by execution and reporting."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Literal, TypedDict

CandidateOutcome = Literal["confirmed", "refuted", "progress", "regression", "unclear"]
CandidateTerminality = Literal["terminal", "retryable"]

_DISPOSITIONS = frozenset(
    {"valid", "contaminated", "protocol_invalid", "infrastructure_failed", "uncertain"}
)
_AUTHORITIES = frozenset({"target", "validator", "advisory", "none"})
_OUTCOMES = frozenset({"confirmed", "refuted", "progress", "regression", "unclear"})
_TERMINAL_OUTCOMES = frozenset({"confirmed", "refuted"})
_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_PROJECTION_FIELDS = frozenset(
    {
        "schema",
        "candidate_id",
        "valid",
        "attempt_disposition",
        "outcome_authority",
        "outcome",
        "evidence_digest",
        "terminality",
    }
)


class CandidateOutcomeProjection(TypedDict):
    schema: Literal["merlin.candidate-outcome.v1"]
    candidate_id: str
    valid: bool
    attempt_disposition: str
    outcome_authority: str
    outcome: CandidateOutcome
    evidence_digest: str
    terminality: CandidateTerminality


def project_candidate_outcome(
    *,
    candidate_id: str,
    attempt_disposition: object,
    outcome_authority: object,
    outcome: object,
    evidence_digest: str,
) -> CandidateOutcomeProjection:
    """Normalize every downstream view to one conservative outcome relation."""

    normalized_candidate = candidate_id.strip()
    if not normalized_candidate:
        raise ValueError("candidate outcome requires a non-empty candidate id")
    if not isinstance(evidence_digest, str) or not _SHA256.fullmatch(evidence_digest):
        raise ValueError("candidate outcome requires a full SHA-256 evidence digest")
    normalized_digest = (
        evidence_digest if evidence_digest.startswith("sha256:") else f"sha256:{evidence_digest}"
    )
    disposition = str(attempt_disposition or "uncertain").strip().casefold()
    if disposition not in _DISPOSITIONS:
        disposition = "uncertain"
    authority = str(outcome_authority or "none").strip().casefold()
    if authority not in _AUTHORITIES:
        authority = "none"
    normalized_outcome = str(outcome or "unclear").strip().casefold()
    if normalized_outcome not in _OUTCOMES:
        normalized_outcome = "unclear"
    valid = disposition == "valid" and authority in {"target", "validator"}
    if not valid:
        authority = "none"
        normalized_outcome = "unclear"
    terminality: CandidateTerminality = (
        "terminal" if valid and normalized_outcome in _TERMINAL_OUTCOMES else "retryable"
    )
    return {
        "schema": "merlin.candidate-outcome.v1",
        "candidate_id": normalized_candidate,
        "valid": valid,
        "attempt_disposition": disposition,
        "outcome_authority": authority,
        "outcome": normalized_outcome,  # type: ignore[typeddict-item]
        "evidence_digest": normalized_digest,
        "terminality": terminality,
    }


def candidate_id_for_identity(identity: Mapping[str, object]) -> str:
    """Return the stable opaque id used by every projection of one exact candidate."""

    if not identity or any(not str(key).strip() for key in identity):
        raise ValueError("candidate identity requires named fields")
    if any(not isinstance(value, str) or not value.strip() for value in identity.values()):
        raise ValueError("candidate identity fields must be non-empty strings")
    encoded = json.dumps(
        dict(identity), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def candidate_evidence_digest(evidence_refs: Sequence[str]) -> str:
    """Digest the exact evidence set used to settle one attempt."""

    refs = tuple(sorted(set(evidence_refs)))
    if not refs or any(not isinstance(value, str) or not value.strip() for value in refs):
        raise ValueError("candidate outcome requires non-empty evidence references")
    encoded = json.dumps(refs, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def validate_candidate_outcome_projection(
    value: Mapping[str, object],
    *,
    expected_candidate_id: str | None = None,
) -> CandidateOutcomeProjection:
    """Validate a persisted projection and reject drift in any canonical field."""

    missing = _PROJECTION_FIELDS - value.keys()
    if missing:
        raise ValueError(
            "candidate outcome projection is missing canonical fields: "
            + ", ".join(sorted(missing))
        )
    if value.get("schema") != "merlin.candidate-outcome.v1":
        raise ValueError("candidate outcome projection schema is unsupported")
    candidate_id = str(value.get("candidate_id") or "").strip()
    if expected_candidate_id is not None and candidate_id != expected_candidate_id:
        raise ValueError("candidate outcome projection candidate id conflicts with settlement")
    normalized = project_candidate_outcome(
        candidate_id=candidate_id,
        attempt_disposition=value.get("attempt_disposition"),
        outcome_authority=value.get("outcome_authority"),
        outcome=value.get("outcome"),
        evidence_digest=str(value.get("evidence_digest") or ""),
    )
    observed = {field: value.get(field) for field in _PROJECTION_FIELDS}
    if observed != normalized:
        raise ValueError("candidate outcome projection conflicts with canonical normalization")
    return normalized


def assert_candidate_outcome_match(
    authoritative: Mapping[str, object],
    projection: Mapping[str, object],
) -> CandidateOutcomeProjection:
    """Require a downstream projection to preserve every canonical ledger field."""

    expected = validate_candidate_outcome_projection(authoritative)
    observed = validate_candidate_outcome_projection(
        projection, expected_candidate_id=expected["candidate_id"]
    )
    if observed != expected:
        raise ValueError("candidate outcome projection disagrees with canonical attempt ledger")
    return observed


def model_authority_disagreement(
    model_outcome: object,
    authoritative: Mapping[str, object],
) -> dict[str, object] | None:
    """Record a model/oracle conflict without weakening target authority."""

    model = str(model_outcome or "").strip().casefold()
    authoritative_outcome = str(authoritative.get("outcome") or "unclear").strip().casefold()
    if (
        model not in _TERMINAL_OUTCOMES
        or authoritative_outcome not in _TERMINAL_OUTCOMES
        or model == authoritative_outcome
    ):
        return None
    return {
        "schema": "merlin.model-authority-disagreement.v1",
        "candidate_id": authoritative.get("candidate_id"),
        "model_outcome": model,
        "authoritative_outcome": authoritative_outcome,
        "evidence_digest": authoritative.get("evidence_digest"),
        "escalation": "independent_validation",
        "retryable": True,
    }


def summarize_candidate_projections(
    projections: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    """Count executions separately from unique candidates and repeated attempts."""

    by_candidate: dict[str, list[Mapping[str, object]]] = {}
    for projection in projections:
        if projection.get("schema") != "merlin.candidate-outcome.v1":
            raise ValueError("candidate outcome summary requires canonical projections")
        candidate_id = str(projection.get("candidate_id") or "").strip()
        if not candidate_id:
            raise ValueError("candidate outcome summary requires candidate ids")
        by_candidate.setdefault(candidate_id, []).append(projection)
    terminal_unique = 0
    conflicting_unique = 0
    for attempts in by_candidate.values():
        terminal_outcomes = {
            str(item.get("outcome")) for item in attempts if item.get("terminality") == "terminal"
        }
        if len(terminal_outcomes) == 1:
            terminal_unique += 1
        elif len(terminal_outcomes) > 1:
            conflicting_unique += 1
    return {
        "attempt_count": len(projections),
        "unique_candidate_count": len(by_candidate),
        "repeat_attempt_count": len(projections) - len(by_candidate),
        "terminal_unique_candidate_count": terminal_unique,
        "conflicting_unique_candidate_count": conflicting_unique,
    }
