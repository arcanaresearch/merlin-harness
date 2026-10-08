"""Observation-derived semantic neighbourhood relationships between pack cases.

Relationships are derived only from committed target receipts: a shared-surface
link records that two distinct cases were observed on the same endpoint or
parameter, and a shared-failure-cause link records that two distinct cases
produced the same observed failure signature. Every admitted relationship
carries a receipt-derived material-difference explanation so a neighbour is
never confused with a duplicate of an already-planned or executed case.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

RelationshipKind = Literal["shared-surface", "shared-failure-cause"]
RELATIONSHIP_KINDS: tuple[RelationshipKind, ...] = ("shared-surface", "shared-failure-cause")


@dataclass(frozen=True, slots=True)
class ObservedCaseReceipt:
    """Committed target-receipt projection binding one case to observed target behavior."""

    case_id: str
    endpoint: str
    parameters: tuple[str, ...] = ()
    failure_cause: str | None = None
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("observed receipt case id cannot be empty")
        if not self.endpoint.strip():
            raise ValueError("observed receipt endpoint cannot be empty")
        if len(set(self.parameters)) != len(self.parameters) or any(
            not item.strip() for item in self.parameters
        ):
            raise ValueError("observed receipt parameters must be unique non-empty strings")
        if self.failure_cause is not None and not self.failure_cause.strip():
            raise ValueError("observed receipt failure cause cannot be blank")
        if len(set(self.evidence_refs)) != len(self.evidence_refs) or any(
            not item.strip() for item in self.evidence_refs
        ):
            raise ValueError("observed receipt evidence refs must be unique non-empty strings")

    def projection(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CaseRelationship:
    """Digest-bound receipt-derived link between two distinct cases."""

    id: str
    kind: RelationshipKind
    case_ids: tuple[str, str]
    basis: str
    material_difference: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in RELATIONSHIP_KINDS:
            raise ValueError(
                "case relationship kind must be shared-surface or shared-failure-cause"
            )
        if len(self.case_ids) != 2 or not all(item.strip() for item in self.case_ids):
            raise ValueError("case relationship requires exactly two non-empty case ids")
        if self.case_ids[0] >= self.case_ids[1]:
            raise ValueError("case relationship case ids must be distinct and sorted")
        if not self.basis.strip():
            raise ValueError("case relationship basis cannot be empty")
        if not self.material_difference.strip():
            raise ValueError("case relationship requires a material-difference explanation")
        if len(set(self.evidence_refs)) != len(self.evidence_refs) or any(
            not item.strip() for item in self.evidence_refs
        ):
            raise ValueError("case relationship evidence refs must be unique non-empty strings")
        if self.id != _relationship_id(self.kind, self.case_ids, self.basis):
            raise ValueError("case relationship id does not match its canonical digest")

    def projection(self) -> dict[str, object]:
        return asdict(self)


def derive_case_relationships(
    receipts: tuple[ObservedCaseReceipt, ...],
) -> tuple[CaseRelationship, ...]:
    """Derive deterministic shared-surface and shared-failure-cause links from receipts."""
    case_ids = [receipt.case_id for receipt in receipts]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("observed receipts must carry exactly one receipt per case")
    relationships: list[CaseRelationship] = []
    ordered = sorted(receipts, key=lambda receipt: receipt.case_id)
    for left_index, left in enumerate(ordered):
        for right in ordered[left_index + 1 :]:
            pair = (left.case_id, right.case_id)
            evidence = tuple(sorted(set(left.evidence_refs) | set(right.evidence_refs)))
            surface_basis = _shared_surface_basis(left, right)
            if surface_basis is not None:
                relationships.append(
                    CaseRelationship(
                        id=_relationship_id("shared-surface", pair, surface_basis),
                        kind="shared-surface",
                        case_ids=pair,
                        basis=surface_basis,
                        material_difference=_surface_difference(left, right, surface_basis),
                        evidence_refs=evidence,
                    )
                )
            if left.failure_cause is not None and left.failure_cause == right.failure_cause:
                cause_basis = f"failure-cause:{left.failure_cause}"
                relationships.append(
                    CaseRelationship(
                        id=_relationship_id("shared-failure-cause", pair, cause_basis),
                        kind="shared-failure-cause",
                        case_ids=pair,
                        basis=cause_basis,
                        material_difference=_failure_cause_difference(left, right),
                        evidence_refs=evidence,
                    )
                )
    return tuple(sorted(relationships, key=lambda item: item.id))


def _shared_surface_basis(left: ObservedCaseReceipt, right: ObservedCaseReceipt) -> str | None:
    if left.endpoint.strip().casefold() == right.endpoint.strip().casefold():
        return f"endpoint:{left.endpoint.strip().casefold()}"
    shared = sorted(
        {item.strip().casefold() for item in left.parameters}
        & {item.strip().casefold() for item in right.parameters}
    )
    if shared:
        return f"parameter:{shared[0]}"
    return None


def _surface_difference(left: ObservedCaseReceipt, right: ObservedCaseReceipt, basis: str) -> str:
    left_cause = left.failure_cause or "no recorded failure cause"
    right_cause = right.failure_cause or "no recorded failure cause"
    if left.failure_cause != right.failure_cause:
        difference = f"they recorded different failure causes ({left_cause!r} vs {right_cause!r})"
    else:
        difference = (
            f"they remain distinct reviewed cases ({left.case_id!r} vs {right.case_id!r}) "
            "with separate receipt evidence"
        )
    return (
        f"Both cases were observed on the same target surface ({basis}), but {difference}; "
        "the neighbour is not a duplicate of the observed case."
    )


def _failure_cause_difference(left: ObservedCaseReceipt, right: ObservedCaseReceipt) -> str:
    return (
        f"Both cases recorded the failure cause {left.failure_cause!r}, but on distinct "
        f"surfaces ({left.endpoint.strip().casefold()!r} vs "
        f"{right.endpoint.strip().casefold()!r}); the neighbour is not a duplicate of the "
        "observed case."
    )


def _relationship_id(kind: RelationshipKind, case_ids: tuple[str, str], basis: str) -> str:
    encoded = json.dumps(
        {"kind": kind, "case_ids": case_ids, "basis": basis},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def receipt_from_mapping(value: Mapping[str, Any]) -> ObservedCaseReceipt:
    """Reconstruct one committed receipt projection using the exact durable schema."""
    expected = {"case_id", "endpoint", "parameters", "failure_cause", "evidence_refs"}
    supplied = set(value)
    if missing := expected - supplied:
        raise ValueError(
            "observed receipt is missing required fields: " + ", ".join(sorted(missing))
        )
    if extra := supplied - expected:
        raise ValueError("observed receipt has unexpected fields: " + ", ".join(sorted(extra)))
    for name in ("parameters", "evidence_refs"):
        items = value[name]
        if not isinstance(items, (list, tuple)) or any(not isinstance(item, str) for item in items):
            raise ValueError(f"observed receipt {name} must be an array of strings")
    failure_cause = value["failure_cause"]
    if failure_cause is not None and not isinstance(failure_cause, str):
        raise ValueError("observed receipt failure_cause must be a string or null")
    return ObservedCaseReceipt(
        case_id=str(value["case_id"]),
        endpoint=str(value["endpoint"]),
        parameters=tuple(value["parameters"]),
        failure_cause=failure_cause,
        evidence_refs=tuple(value["evidence_refs"]),
    )
