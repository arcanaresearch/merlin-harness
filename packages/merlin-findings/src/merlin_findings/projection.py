# Embedded standalone HTML intentionally keeps its template readable as one document.
# ruff: noqa: E501

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from merlin_contracts import (
    CampaignLifecycle,
    CampaignObjective,
    CampaignOutcomeProjection,
    CampaignTerminalReason,
    CampaignVerdict,
    assert_candidate_outcome_match,
    candidate_id_for_identity,
    read_campaign_terminal_reason,
    summarize_candidate_projections,
    validate_candidate_outcome_projection,
)


@dataclass(frozen=True, slots=True)
class EvidenceCitation:
    reference: str
    finding_id: str
    witness: str

    def __post_init__(self) -> None:
        if not self.reference.strip() or not self.finding_id.strip() or not self.witness.strip():
            raise ValueError("evidence citations require reference, finding, and witness")


@dataclass(frozen=True, slots=True)
class CampaignLesson:
    id: str
    statement: str
    evidence_references: tuple[str, ...]


def _canonical_candidate_metadata(
    metadata: Mapping[str, Any] | None,
    techniques: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate the attempt ledger and derive its immutable report counters."""

    normalized = dict(metadata or {})
    raw_projections = normalized.get("candidate_outcome_projections")
    if raw_projections is None:
        return normalized
    if not isinstance(raw_projections, Sequence) or isinstance(raw_projections, (str, bytes)):
        raise ValueError("report candidate outcome projections must be a sequence")
    if any(not isinstance(item, Mapping) for item in raw_projections):
        raise ValueError("report candidate outcome projections must all be mappings")
    canonical_items: list[dict[str, Any]] = []
    for item in raw_projections:
        identity = item.get("candidate_identity")
        projection = validate_candidate_outcome_projection(
            item,
            expected_candidate_id=(
                candidate_id_for_identity(identity) if isinstance(identity, Mapping) else None
            ),
        )
        canonical_items.append({**dict(item), **projection})
    canonical = tuple(canonical_items)
    by_candidate = {str(item["candidate_id"]): item for item in canonical}
    for row in techniques:
        if not isinstance(row, Mapping):
            continue
        projected = row.get("candidate_outcome_projection")
        if not isinstance(projected, Mapping):
            continue
        authoritative = by_candidate.get(str(projected.get("candidate_id") or ""))
        if authoritative is None:
            raise ValueError("report technique outcome has no canonical attempt-ledger projection")
        assert_candidate_outcome_match(authoritative, projected)
    summary = summarize_candidate_projections(canonical)
    for key, value in summary.items():
        existing = normalized.get(key)
        if existing is not None and existing != value:
            raise ValueError(f"report {key} conflicts with canonical candidate outcomes")
        normalized[key] = value
    normalized["candidate_outcome_projections"] = canonical
    return normalized


@dataclass(frozen=True, slots=True)
class CampaignReportProjection:
    revision_id: str
    campaign_id: str
    revision: int
    stop_reason: Literal[
        "objective_satisfied",
        "frontier_exhausted",
        "budget_exhausted",
        "target_terminal",
        "operator_stopped",
        "infrastructure_blocked_after_retry_policy",
    ]
    cleanup_status: str
    findings: tuple[Mapping[str, Any], ...]
    tasks: tuple[Mapping[str, Any], ...]
    evidence: tuple[EvidenceCitation, ...]
    usage: Mapping[str, int | float]
    created_at: datetime
    digest: str
    audience: Literal["internal", "customer"] = "internal"
    report_state: Literal["complete", "provisional", "incomplete"] = "complete"
    metadata: Mapping[str, Any] = None  # type: ignore[assignment]
    coverage: Mapping[str, int] = None  # type: ignore[assignment]
    techniques: tuple[Mapping[str, Any], ...] = ()
    exchanges: tuple[Mapping[str, Any], ...] = ()
    lineage: tuple[Mapping[str, Any], ...] = ()
    hypotheses: tuple[Mapping[str, Any], ...] = ()
    unsearched_frontier: tuple[Mapping[str, Any], ...] = ()
    limitations: tuple[str, ...] = ()
    completion_digest: str | None = None
    completion_document: Mapping[str, Any] | None = None
    source_seal_state: Literal["sealed", "legacy_unsealed"] = "legacy_unsealed"
    digest_schema: Literal["v1", "v2"] = "v2"
    # Loaded only while validating or rendering a v2 completion snapshot. These
    # rows are deliberately excluded from the immutable report projection and
    # its digest so the stored completion document remains bounded.
    frontier_snapshot_rows: tuple[Mapping[str, Any], ...] = ()

    @property
    def terminal_reason(self) -> CampaignTerminalReason:
        """Canonical reason, including explicit translation for historical reports."""

        return read_campaign_terminal_reason(self.stop_reason)

    def __post_init__(self) -> None:
        metadata = _canonical_candidate_metadata(self.metadata, self.techniques)
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "coverage", dict(self.coverage or {}))
        if self.completion_document is not None:
            object.__setattr__(self, "completion_document", dict(self.completion_document))
        object.__setattr__(
            self,
            "frontier_snapshot_rows",
            tuple(dict(row) for row in self.frontier_snapshot_rows),
        )


class ReportRevisionRepository(Protocol):
    def next_revision(self, campaign_id: str) -> int: ...
    def save(self, projection: CampaignReportProjection) -> CampaignReportProjection | None: ...
    def get(self, revision_id: str) -> CampaignReportProjection | None: ...
    def list(self, campaign_id: str) -> Sequence[CampaignReportProjection]: ...


class FrontierSnapshotProvider(Protocol):
    """Read an immutable completion frontier without embedding it in the seal."""

    def get_frontier_completion_snapshot(
        self, campaign_id: str, snapshot_id: str
    ) -> Mapping[str, Any] | None: ...

    def frontier_completion_counts(
        self, campaign_id: str, snapshot_id: str
    ) -> Mapping[str, Any] | None: ...

    def list_frontier_completion_page(
        self,
        campaign_id: str,
        snapshot_id: str,
        *,
        limit: int = 100,
        after_position: int = -1,
    ) -> Sequence[Mapping[str, Any]]: ...


FailureInjector = Callable[[str], None]


def _json_value(value: Any) -> Any:
    """Recursively normalize a JSON-bound value (datetime → ISO text).

    Durable stores can hand back raw datetime objects (scheduler/wave timing,
    cleanup-authority receipts); report digests must match the daemon's own
    ``_json_value``-normalized seal, so normalization uses ``isoformat``.
    """
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in value), key=str)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _completion_authority_document(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    document = value.get("document")
    return document if isinstance(document, Mapping) else value


def _reviewed_objective_contract(
    value: object,
) -> tuple[CampaignObjective, ...] | None:
    """Read an optional sealed objective contract without inventing legacy objectives."""

    if value is None:
        return None
    raw = value.get("objectives") if isinstance(value, Mapping) else value
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("campaign objective contract must contain an objectives sequence")
    objectives = tuple(CampaignObjective.model_validate(item) for item in raw)
    ids = tuple(item.id for item in objectives)
    if not objectives or len(ids) != len(set(ids)):
        raise ValueError("campaign objective contract requires unique non-empty objectives")
    return objectives


def _reviewed_campaign_outcome(value: object) -> CampaignOutcomeProjection | None:
    if value is None:
        return None
    outcome = CampaignOutcomeProjection.model_validate(value)
    required_ids = outcome.required_objective_ids
    unresolved_ids = outcome.unresolved_objective_ids
    if len(required_ids) != len(set(required_ids)) or len(unresolved_ids) != len(
        set(unresolved_ids)
    ):
        raise ValueError("campaign outcome objective ids must be unique")
    if not set(unresolved_ids).issubset(required_ids):
        raise ValueError("campaign outcome unresolved objectives must be required objectives")
    discharged = (
        set(outcome.satisfied_by_campaign_ids)
        | set(outcome.preexisting_objective_ids)
        | set(outcome.waived_objective_ids)
    )
    if discharged & set(unresolved_ids):
        raise ValueError("campaign outcome cannot both discharge and leave an objective unresolved")
    successful = outcome.lifecycle is CampaignLifecycle.COMPLETED or outcome.verdict in {
        CampaignVerdict.SATISFIED,
        CampaignVerdict.SATISFIED_WITH_WAIVERS,
    }
    if successful and unresolved_ids:
        raise ValueError("campaign outcome cannot claim success with unresolved objectives")
    if outcome.lifecycle is CampaignLifecycle.COMPLETED and outcome.verdict not in {
        CampaignVerdict.SATISFIED,
        CampaignVerdict.SATISFIED_WITH_WAIVERS,
    }:
        raise ValueError("completed campaign outcome requires a satisfied verdict")
    if outcome.lifecycle in {
        CampaignLifecycle.COMPLETED,
        CampaignLifecycle.INCOMPLETE,
        CampaignLifecycle.STOPPED,
        CampaignLifecycle.FAILED,
    } and outcome.end_reason is None:
        raise ValueError("terminal campaign outcome requires an end reason")
    return outcome


def _objective_report_authority(
    metadata: Mapping[str, Any] | None,
    completion: Mapping[str, Any] | None,
) -> tuple[CampaignOutcomeProjection | None, bool]:
    """Reconcile optional objective authority embedded in report and completion inputs."""

    authority = _completion_authority_document(completion)
    contracts = tuple(
        item
        for item in (
            _reviewed_objective_contract((metadata or {}).get("objective_contract")),
            _reviewed_objective_contract(authority.get("objective_contract")),
        )
        if item is not None
    )
    if len(contracts) == 2 and contracts[0] != contracts[1]:
        raise ValueError("report objective contract conflicts with the completion authority")
    outcomes = tuple(
        item
        for item in (
            _reviewed_campaign_outcome((metadata or {}).get("campaign_outcome")),
            _reviewed_campaign_outcome(authority.get("campaign_outcome")),
        )
        if item is not None
    )
    if len(outcomes) == 2 and outcomes[0] != outcomes[1]:
        raise ValueError("report campaign outcome conflicts with the completion authority")
    outcome = outcomes[0] if outcomes else None
    if contracts and outcome is None:
        raise ValueError("sealed objective contract requires a typed campaign outcome projection")
    if contracts and outcome is not None:
        required_ids = tuple(item.id for item in contracts[0] if item.required)
        if required_ids != outcome.required_objective_ids:
            raise ValueError("campaign outcome does not cover the sealed required objectives")
    return outcome, bool(contracts)


def _normalize_frontier_snapshot_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the canonical row shape used by campaign snapshot sealing."""
    refs = row.get("evidence_refs", ())
    if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
        raise ValueError("v2 completion frontier snapshot evidence refs are invalid")
    normalized = {
        "position": row.get("position"),
        "candidate_id": str(row.get("candidate_id") or "").strip(),
        "state": str(row.get("state") or "").strip(),
        "outcome": str(row.get("outcome") or "").strip(),
        "attempt_id": None
        if row.get("attempt_id") is None
        else str(row.get("attempt_id")).strip() or None,
        "task_id": None if row.get("task_id") is None else str(row.get("task_id")).strip() or None,
        "evidence_refs": [str(value).strip() for value in refs],
        "disposition_reason": None
        if row.get("disposition_reason") is None
        else str(row.get("disposition_reason")).strip() or None,
    }
    if type(normalized["position"]) is not int or normalized["position"] < 0:
        raise ValueError("v2 completion frontier snapshot position is invalid")
    if not normalized["candidate_id"] or not normalized["state"] or not normalized["outcome"]:
        raise ValueError("v2 completion frontier snapshot row is incomplete")
    if any(not value for value in normalized["evidence_refs"]) or len(
        set(normalized["evidence_refs"])
    ) != len(normalized["evidence_refs"]):
        raise ValueError("v2 completion frontier snapshot evidence refs are invalid")
    return normalized


class DurableReportService:
    """Create immutable report revisions from one explicit durable-state projection."""

    def __init__(
        self,
        repository: ReportRevisionRepository,
        *,
        failure_injector: FailureInjector | None = None,
        frontier_snapshot_provider: FrontierSnapshotProvider | None = None,
    ) -> None:
        self._repository = repository
        self._inject = failure_injector or (lambda stage: None)
        self._frontier_snapshots = frontier_snapshot_provider

    def create(
        self,
        campaign_id: str,
        *,
        stop_reason: str,
        cleanup_status: str,
        findings: Sequence[Mapping[str, Any]],
        tasks: Sequence[Mapping[str, Any]],
        evidence: Sequence[EvidenceCitation],
        usage: Mapping[str, int | float],
        audience: Literal["internal", "customer"] = "internal",
        report_state: Literal["complete", "provisional", "incomplete"] | None = None,
        metadata: Mapping[str, Any] | None = None,
        coverage: Mapping[str, int] | None = None,
        techniques: Sequence[Mapping[str, Any]] = (),
        exchanges: Sequence[Mapping[str, Any]] = (),
        lineage: Sequence[Mapping[str, Any]] = (),
        hypotheses: Sequence[Mapping[str, Any]] = (),
        unsearched_frontier: Sequence[Mapping[str, Any]] = (),
        limitations: Sequence[str] = (),
        completion_digest: str | None = None,
        completion_document: Mapping[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> CampaignReportProjection:
        if audience != "internal":
            raise ValueError("durable report sources must be internal; derive customer exports")
        # Normalize once so seal digests, the duplicate-report comparison, and
        # the repository's JSON write all operate on the same JSON-safe payload.
        findings = _json_value(findings)
        tasks = _json_value(tasks)
        usage = _json_value(usage)
        metadata = None if metadata is None else _json_value(metadata)
        coverage = None if coverage is None else _json_value(coverage)
        techniques = _json_value(techniques)
        exchanges = _json_value(exchanges)
        lineage = _json_value(lineage)
        hypotheses = _json_value(hypotheses)
        unsearched_frontier = _json_value(unsearched_frontier)
        metadata = _canonical_candidate_metadata(metadata, techniques)
        if completion_document is not None:
            completion_document = _json_value(completion_document)
        campaign_outcome, _ = _objective_report_authority(metadata, completion_document)
        objective_incomplete = campaign_outcome is not None and (
            campaign_outcome.lifecycle is not CampaignLifecycle.COMPLETED
            or bool(campaign_outcome.unresolved_objective_ids)
        )
        if objective_incomplete and report_state == "complete":
            raise ValueError(
                "complete report cannot claim success while required objectives remain unresolved"
            )
        if objective_incomplete and isinstance(completion_document, Mapping):
            terminal_status = str(completion_document.get("terminal_status") or "").casefold()
            if terminal_status in {"complete", "completed", "success", "succeeded"}:
                raise ValueError(
                    "successful completion seal conflicts with unresolved required objectives"
                )
        source_stop_reason = stop_reason.strip().casefold()
        legacy_limited_completion = source_stop_reason == "completed_with_limitations"
        require_frontier_ready = source_stop_reason in {
            "saturated",
            CampaignTerminalReason.FRONTIER_EXHAUSTED.value,
        }
        try:
            stop_reason = read_campaign_terminal_reason(stop_reason).value
        except ValueError as exc:
            raise ValueError("report requires an exact terminal campaign stop reason") from exc
        finding_ids = {str(item.get("id", "")) for item in findings}
        if len(finding_ids) != len(findings) or "" in finding_ids:
            raise ValueError("report findings require unique non-empty IDs")
        if any(citation.finding_id not in finding_ids for citation in evidence):
            raise ValueError("evidence citation refers to a finding outside the report")
        _validate_report_joins(techniques, exchanges, lineage)
        _validate_coverage(coverage)
        timestamp = created_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("report timestamp must be timezone-aware")
        cleanup_complete = cleanup_status in {"complete", "completed"}
        state = "incomplete" if objective_incomplete else report_state or (
            "provisional"
            if legacy_limited_completion and not unsearched_frontier and cleanup_complete
            else "complete"
            if stop_reason
            in {
                CampaignTerminalReason.OBJECTIVE_SATISFIED.value,
                CampaignTerminalReason.FRONTIER_EXHAUSTED.value,
            }
            and not unsearched_frontier
            and cleanup_complete
            else "incomplete"
        )
        if state == "complete" and (
            stop_reason
            not in {
                CampaignTerminalReason.OBJECTIVE_SATISFIED.value,
                CampaignTerminalReason.FRONTIER_EXHAUSTED.value,
            }
            or unsearched_frontier
            or not cleanup_complete
        ):
            raise ValueError(
                "complete report requires an objective/frontier completion reason, empty "
                "frontier, and completed cleanup"
            )
        if (completion_digest is None) != (completion_document is None):
            raise ValueError("report completion seal requires both document and digest")
        sealed_completion = dict(completion_document) if completion_document is not None else None
        if sealed_completion is not None:
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(completion_digest)):
                raise ValueError("report completion seal requires a full SHA-256 digest")
            if completion_digest != _digest(sealed_completion):
                raise ValueError("report completion seal digest does not match its document")
            completion_schema = sealed_completion.get("schema")
            if sealed_completion.get("kind") != "campaign_completion" or completion_schema not in {
                "merlin.campaign-completion.v1",
                "merlin.campaign-completion.v2",
            }:
                raise ValueError("report requires a supported campaign completion seal")
            sealed_stop_reason = sealed_completion.get("stop_reason")
            if (
                not isinstance(sealed_stop_reason, str)
                or read_campaign_terminal_reason(sealed_stop_reason).value != stop_reason
            ):
                raise ValueError("report stop reason conflicts with the completion seal")
            inner_digest = sealed_completion.get("completion_digest")
            sealed_document = sealed_completion.get("document")
            if not isinstance(sealed_document, Mapping) or inner_digest != _digest(sealed_document):
                raise ValueError("report completion seal contains an invalid authority document")
            if sealed_document.get("schema") != completion_schema:
                raise ValueError(
                    "report completion seal schema conflicts with its authority document"
                )
            required_fields = {
                "schema",
                "campaign_id",
                "manifest_digest",
                "target_id",
                "profile",
                "waves",
                "receipts",
                "findings",
                "cleanup",
                "saturation",
            }
            required_fields.add(
                "cells"
                if completion_schema == "merlin.campaign-completion.v1"
                else "frontier_snapshot"
            )
            if not required_fields.issubset(sealed_document):
                raise ValueError("report completion authority document is incomplete")
            if sealed_document.get("campaign_id") != campaign_id:
                raise ValueError("report completion seal belongs to another campaign")
            if (metadata or {}).get("manifest_digest") != sealed_document.get("manifest_digest"):
                raise ValueError("report manifest conflicts with the completion seal")
            if (metadata or {}).get("profile") != sealed_document.get("profile"):
                raise ValueError("report profile conflicts with the completion seal")
            if completion_schema == "merlin.campaign-completion.v2":
                self._load_frontier_snapshot(campaign_id, sealed_document)
            saturation = sealed_document.get("saturation")
            if require_frontier_ready and (
                not isinstance(saturation, Mapping) or saturation.get("ready") is not True
            ):
                raise ValueError("saturated report requires a ready completion seal")
            cleanup = sealed_document.get("cleanup")
            if not isinstance(cleanup, Mapping):
                raise ValueError("report completion seal lacks cleanup authority")
            expected_cleanup = tuple(cleanup.get("expected", ()))
            if cleanup_status in {"complete", "completed", "not_applicable"} and expected_cleanup:
                states = dict(cleanup.get("states", ()))

                def cleanup_state(node: object) -> object:
                    value = states.get(node)
                    return value[0] if isinstance(value, (tuple, list)) and value else value

                if any(
                    cleanup_state(node) not in {"completed", "cleaned", "released"}
                    for node in expected_cleanup
                ):
                    raise ValueError("report cleanup status conflicts with the completion seal")
        published = self._repository.list(campaign_id)
        sealed_published = tuple(item for item in published if item.source_seal_state == "sealed")
        conflicting_seals = tuple(
            item for item in sealed_published if item.completion_digest != completion_digest
        )
        # A campaign whose prior sealed reports all belong to failed or
        # interrupted terminalizations was retried and legitimately re-sealed a
        # new completion document; the new revision supersedes them. A sealed
        # report from a successful terminalization is never shadowed.
        supersedes_failed_seal = bool(conflicting_seals) and all(
            item.terminal_reason is CampaignTerminalReason.INFRASTRUCTURE_BLOCKED_AFTER_RETRY_POLICY
            for item in conflicting_seals
        )
        if completion_digest is not None and conflicting_seals and not supersedes_failed_seal:
            raise ValueError("campaign already has a different sealed completion report")
        if completion_digest is not None:
            finder = getattr(self._repository, "find_by_completion_digest", None)
            if callable(finder):
                existing = finder(campaign_id, completion_digest)
                if existing is not None:
                    existing = self._required(existing.revision_id)
                    requested = {
                        "stop_reason": stop_reason,
                        "cleanup_status": cleanup_status,
                        "findings": [dict(item) for item in findings],
                        "tasks": [dict(item) for item in tasks],
                        "evidence": [asdict(item) for item in evidence],
                        "usage": dict(usage),
                        "audience": audience,
                        "report_state": state,
                        "metadata": dict(metadata or {}),
                        "coverage": dict(coverage or {}),
                        "techniques": [dict(item) for item in techniques],
                        "exchanges": [dict(item) for item in exchanges],
                        "lineage": [dict(item) for item in lineage],
                        "unsearched_frontier": [dict(item) for item in unsearched_frontier],
                        "limitations": list(limitations),
                    }
                    actual = {key: _source_document(existing)[key] for key in requested}
                    if actual != requested:
                        raise ValueError("completion seal already published a different report")
                    return existing
        elif sealed_published:
            raise ValueError("sealed campaign report cannot be shadowed by an unsealed revision")
        revision = self._repository.next_revision(campaign_id)
        hypothesis_rows = [dict(item) for item in hypotheses]
        expanded: dict[str, Any] = {
            "audience": audience,
            "report_state": state,
            "metadata": dict(metadata or {}),
            "coverage": dict(coverage or {}),
            "techniques": [dict(item) for item in techniques],
            "exchanges": [dict(item) for item in exchanges],
            "lineage": [dict(item) for item in lineage],
            "unsearched_frontier": [dict(item) for item in unsearched_frontier],
            "limitations": list(limitations),
            "completion_digest": completion_digest,
            "source_seal_state": "sealed" if sealed_completion is not None else "legacy_unsealed",
            "digest_schema": "v2",
        }
        # Hypotheses join the signed document only when present so revisions sealed
        # before this field existed keep their original digests on re-export.
        if hypothesis_rows:
            expanded["hypotheses"] = hypothesis_rows
        document = {
            "campaign_id": campaign_id,
            "revision": revision,
            "stop_reason": stop_reason,
            "cleanup_status": cleanup_status,
            "findings": list(findings),
            "tasks": list(tasks),
            "evidence": [asdict(citation) for citation in evidence],
            "usage": dict(usage),
            "created_at": timestamp.isoformat(),
            **expanded,
        }
        digest = _digest(document)
        projection = CampaignReportProjection(
            revision_id=f"{campaign_id}:r{revision}:{digest.removeprefix('sha256:')[:12]}",
            campaign_id=campaign_id,
            revision=revision,
            stop_reason=stop_reason,  # type: ignore[arg-type]
            cleanup_status=cleanup_status,
            findings=tuple(dict(item) for item in findings),
            tasks=tuple(dict(item) for item in tasks),
            evidence=tuple(evidence),
            usage=dict(usage),
            created_at=timestamp,
            digest=digest,
            audience=audience,
            report_state=state,
            metadata=expanded["metadata"],
            coverage=expanded["coverage"],
            techniques=tuple(expanded["techniques"]),
            exchanges=tuple(expanded["exchanges"]),
            lineage=tuple(expanded["lineage"]),
            hypotheses=tuple(hypothesis_rows),
            unsearched_frontier=tuple(expanded["unsearched_frontier"]),
            limitations=tuple(expanded["limitations"]),
            completion_digest=completion_digest,
            completion_document=sealed_completion,
            source_seal_state=expanded["source_seal_state"],
            digest_schema="v2",
        )
        self._inject("before_report_commit")
        stored = self._repository.save(projection)
        self._inject("after_report_commit")
        if isinstance(stored, CampaignReportProjection):
            proposed = _source_document(projection)
            canonical = _source_document(stored)
            for key in ("revision", "created_at"):
                proposed.pop(key, None)
                canonical.pop(key, None)
            if proposed != canonical:
                raise ValueError("completion seal concurrently published a different report")
            return stored
        return projection

    def compare(self, older_id: str, newer_id: str) -> tuple[str, ...]:
        older = self._required(older_id)
        newer = self._required(newer_id)
        if older.campaign_id != newer.campaign_id:
            raise ValueError("report comparison requires the same campaign")
        old = _document(older)
        new = _document(newer)
        mechanical = {
            "$.revision_id",
            "$.revision",
            "$.created_at",
            "$.digest",
            "$.completion_digest",
        }
        return tuple(row for row in _diff(old, new) if row.split(":", 1)[0] not in mechanical)

    def export(
        self,
        revision_id: str,
        format: Literal["html", "markdown", "json", "csv"],
        audience: Literal["internal", "customer"] | None = None,
    ) -> str | bytes:
        projection = self._required(revision_id)
        source_projection: CampaignReportProjection | None = None
        if audience == "customer" and projection.audience != "customer":
            source_projection = projection
            source_revision_digest = projection.digest
            projection = _customer_projection(projection)
            projection = replace(
                projection,
                metadata={**projection.metadata, "source_revision_digest": source_revision_digest},
            )
            projection = replace(projection, digest=_digest(_source_document(projection)))
        self._inject("before_report_export")
        if format == "json":
            return json.dumps(_document(projection), indent=2, sort_keys=True, default=str) + "\n"
        if format == "html":
            return _html_report(projection, source=source_projection)
        if format == "csv":
            return _csv_bundle(projection)
        if format != "markdown":
            raise ValueError("report format must be html, markdown, csv, or json")
        return _markdown_report(projection)

    def lessons_agree(
        self, revision_id: str, lessons: Sequence[CampaignLesson]
    ) -> tuple[bool, tuple[str, ...]]:
        projection = self._required(revision_id)
        report_evidence = {citation.reference for citation in projection.evidence}
        failures = tuple(
            lesson.id
            for lesson in lessons
            if not lesson.statement.strip()
            or not lesson.evidence_references
            or not set(lesson.evidence_references).issubset(report_evidence)
        )
        return not failures, failures

    def _required(self, revision_id: str) -> CampaignReportProjection:
        projection = self._repository.get(revision_id)
        if projection is None:
            raise ValueError(f"unknown report revision {revision_id!r}")
        if projection.audience != "internal":
            raise ValueError("stored report source has an unsafe audience classification")
        if projection.report_state not in {"complete", "provisional", "incomplete"}:
            raise ValueError("stored report source has an invalid report state")
        if projection.source_seal_state not in {"sealed", "legacy_unsealed"}:
            raise ValueError("stored report source has an invalid seal state")
        if projection.digest_schema not in {"v1", "v2"}:
            raise ValueError("stored report source has an invalid digest schema")
        source = (
            _legacy_source_document(projection)
            if projection.digest_schema == "v1"
            else _source_document(projection)
        )
        if projection.digest != _digest(source):
            raise ValueError("stored report projection drifted after publication")
        if projection.source_seal_state == "sealed":
            if projection.completion_document is None or projection.completion_digest != _digest(
                projection.completion_document
            ):
                raise ValueError("stored report completion seal drifted after publication")
        sealed = projection.completion_document
        document = sealed.get("document") if isinstance(sealed, Mapping) else None
        if (
            isinstance(sealed, Mapping)
            and sealed.get("schema") == "merlin.campaign-completion.v2"
            and isinstance(document, Mapping)
        ):
            projection = replace(
                projection,
                frontier_snapshot_rows=self._load_frontier_snapshot(
                    projection.campaign_id, document
                ),
            )
        return projection

    def _load_frontier_snapshot(
        self, campaign_id: str, authority_document: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], ...]:
        header = authority_document.get("frontier_snapshot")
        if not isinstance(header, Mapping):
            raise ValueError("v2 completion seal lacks a frontier snapshot header")
        if not {"id", "reference", "row_digest", "total_count", "counts", "sample"}.issubset(
            header
        ):
            raise ValueError("v2 completion frontier snapshot header is incomplete")
        snapshot_id = str(header.get("id") or "").strip()
        reference = str(header.get("reference") or "").strip()
        expected_reference = f"merlin-frontier-snapshot:{campaign_id}:{snapshot_id}"
        if not snapshot_id or reference != expected_reference:
            raise ValueError("v2 completion seal has an invalid frontier snapshot reference")
        provider = self._frontier_snapshots
        if provider is None:
            raise ValueError("v2 completion seal requires a frontier snapshot provider")
        stored_header = provider.get_frontier_completion_snapshot(campaign_id, snapshot_id)
        stored_counts = provider.frontier_completion_counts(campaign_id, snapshot_id)
        if stored_header is None or stored_counts is None:
            raise ValueError("v2 completion frontier snapshot is unavailable")
        if str(stored_header.get("id") or "") != snapshot_id:
            raise ValueError("v2 completion frontier snapshot identity conflicts with storage")
        for key in ("row_digest", "total_count", "counts"):
            if _json_value(header.get(key)) != _json_value(stored_header.get(key)):
                raise ValueError(f"v2 completion frontier snapshot {key} conflicts with storage")
            if _json_value(header.get(key)) != _json_value(stored_counts.get(key)):
                raise ValueError(f"v2 completion frontier snapshot {key} count projection drifted")

        total_count = header.get("total_count")
        if type(total_count) is not int or total_count < 0:
            raise ValueError("v2 completion frontier snapshot total count is invalid")
        rows: list[Mapping[str, Any]] = []
        after_position = -1
        while len(rows) < total_count:
            page = tuple(
                provider.list_frontier_completion_page(
                    campaign_id,
                    snapshot_id,
                    limit=min(500, total_count - len(rows)),
                    after_position=after_position,
                )
            )
            if not page:
                raise ValueError("v2 completion frontier snapshot ended before its declared count")
            normalized = tuple(_normalize_frontier_snapshot_row(row) for row in page)
            positions = tuple(row["position"] for row in normalized)
            if positions[0] != after_position + 1 or positions != tuple(
                range(positions[0], positions[0] + len(positions))
            ):
                raise ValueError("v2 completion frontier snapshot positions are not contiguous")
            rows.extend(normalized)
            after_position = positions[-1]
        extra = provider.list_frontier_completion_page(
            campaign_id, snapshot_id, limit=1, after_position=after_position
        )
        if extra:
            raise ValueError("v2 completion frontier snapshot exceeds its declared count")
        candidate_ids = tuple(str(row["candidate_id"]) for row in rows)
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("v2 completion frontier snapshot candidate ids are not unique")
        actual_counts = {
            "states": {
                state: sum(row["state"] == state for row in rows)
                for state in ("queued", "claimed", "scheduled", "terminal")
            },
            "outcomes": {
                outcome: sum(row["outcome"] == outcome for row in rows)
                for outcome in (
                    "confirmed",
                    "refuted",
                    "partial",
                    "unclear",
                    "blocked",
                    "retryable",
                    "unattempted",
                )
            },
        }
        if actual_counts != _json_value(header.get("counts")):
            raise ValueError("v2 completion frontier snapshot counts do not match its rows")
        calculated = _digest({"schema_version": 1, "rows": rows}).removeprefix("sha256:")
        declared_digest = str(header.get("row_digest") or "").removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", declared_digest) or calculated != declared_digest:
            raise ValueError("v2 completion frontier snapshot row digest does not match")
        sample = header.get("sample", ())
        if not isinstance(sample, Sequence) or isinstance(sample, (str, bytes)) or len(sample) > 32:
            raise ValueError("v2 completion frontier snapshot sample is invalid")
        normalized_sample = tuple(
            _normalize_frontier_snapshot_row(row) if isinstance(row, Mapping) else None
            for row in sample
        )
        if (
            any(row is None for row in normalized_sample)
            or tuple(rows[: len(sample)]) != normalized_sample
        ):
            raise ValueError("v2 completion frontier snapshot sample conflicts with its rows")
        return tuple(rows)


def _recorded_cost_details(
    projection: CampaignReportProjection,
) -> tuple[str, str, str] | None:
    usd_cost = next(
        (
            projection.usage.get(key)
            for key in ("estimated_cost_usd", "cost_usd")
            if isinstance(projection.usage.get(key), (int, float))
            and not isinstance(projection.usage.get(key), bool)
        ),
        None,
    )
    raw_cost = projection.usage.get("cost")
    undenominated_cost = (
        raw_cost if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool) else None
    )
    if usd_cost is None and undenominated_cost is None:
        return None
    cost_text = (
        f"${float(usd_cost):,.4f}"
        if usd_cost is not None
        else f"{float(undenominated_cost):,.4f} (currency not recorded)"
    )
    provenance = projection.metadata.get("model_cost_provenance")
    pricing_source = str(
        provenance.get("pricing_source") if isinstance(provenance, Mapping) else ""
    ).strip()
    pricing_source_url = str(
        provenance.get("pricing_source_url") if isinstance(provenance, Mapping) else ""
    ).strip()
    if pricing_source.casefold() == "models.dev" and not pricing_source_url:
        pricing_source_url = "https://models.dev"
    return cost_text, pricing_source, pricing_source_url


def _target_progress(value: CampaignReportProjection) -> Mapping[str, Any] | None:
    progress = value.metadata.get("target_progress")
    if not isinstance(progress, Mapping) or progress.get("schema") != "merlin.target-progress.v1":
        return None
    return progress


def _target_progress_fields(progress: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    fields = progress.get("current_state")
    if not isinstance(fields, (tuple, list)):
        return ()
    return tuple(field for field in fields if isinstance(field, Mapping))


def _target_progress_field_text(field: Mapping[str, Any]) -> str:
    label = str(field.get("label") or field.get("name") or "State")
    value = field.get("value")
    maximum = field.get("maximum_value")
    if maximum is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{label}: {value}/{maximum}"
    return f"{label}: {value}"


def _customer_target_progress(progress: Mapping[str, Any]) -> dict[str, Any]:
    transition = progress.get("latest_transition")
    transition_fields = (
        tuple(
            _allowed(item, ("name", "role", "status", "before_value", "after_value"))
            for item in transition.get("observations", ())
            if isinstance(item, Mapping)
        )
        if isinstance(transition, Mapping)
        and isinstance(transition.get("observations"), (tuple, list))
        else ()
    )
    return {
        "schema": "merlin.target-progress.v1",
        "status": str(progress.get("status") or "unknown"),
        "observed_at": progress.get("observed_at"),
        "target_exchange_count": int(progress.get("target_exchange_count") or 0),
        "history_truncated": progress.get("history_truncated") is True,
        "current_state": tuple(
            _allowed(
                field,
                (
                    "name",
                    "label",
                    "role",
                    "value",
                    "value_type",
                    "trust_class",
                    "minimum_value",
                    "maximum_value",
                    "observed_at",
                ),
            )
            for field in _target_progress_fields(progress)
        ),
        "latest_transition": (
            {
                "outcome": str(transition.get("outcome") or "unclear"),
                "observed_at": transition.get("observed_at"),
                "observations": transition_fields,
            }
            if isinstance(transition, Mapping)
            else None
        ),
        "conflict_count": len(progress.get("conflicts", ()))
        if isinstance(progress.get("conflicts"), (tuple, list))
        else 0,
        "limitations": tuple(
            str(item)
            for item in progress.get("limitations", ())
            if isinstance(item, str) and item.strip()
        )
        if isinstance(progress.get("limitations"), (tuple, list))
        else (),
    }


def _markdown_report(projection: CampaignReportProjection) -> str:
    exploration = _exploration_metrics(projection)
    all_observability = _report_observability(projection)
    observability = tuple(
        row for row in all_observability if row.get("category") != "Capability probe"
    )
    audience_label = "Customer report" if projection.audience == "customer" else "Internal report"
    target_progress = _target_progress(projection)
    lines = [
        f"# Campaign report {projection.campaign_id}",
        "",
        f"**Audience: {audience_label}**",
        "",
        "## Contents",
        "",
        "- [Executive summary](#executive-summary)",
        "- [Integrity and provenance](#integrity-and-provenance)",
        *(("- [Target progress](#target-progress)",) if target_progress else ()),
        "- [Coverage](#coverage)",
        "- [Coverage provenance](#coverage-provenance)",
        "- [Language coverage](#language-coverage)",
        "- [Cross-campaign learning](#cross-campaign-learning)",
        "- [Adaptive controllers](#adaptive-controllers)",
        *(("- [Exploration metrics](#exploration-metrics)",) if exploration else ()),
        *(("- [Execution observability](#execution-observability)",) if observability else ()),
        "- [Findings](#findings)",
        "- [Techniques](#techniques)",
        *(("- [Hypotheses evaluated](#hypotheses-evaluated)",) if projection.hypotheses else ()),
        "- [Evidence](#evidence)",
        "- [Target exchanges](#target-exchanges)",
        "- [Lineage](#lineage)",
        "- [Unsearched frontier](#unsearched-frontier)",
        "- [Limitations and cleanup](#limitations-and-cleanup)",
        "",
        "## Executive summary",
        "",
        f"- Revision: {projection.revision_id}",
        f"- Digest: {projection.digest}",
        f"- Stop reason: {projection.stop_reason}",
        f"- State: **{projection.report_state}**",
        f"- Audience: {projection.audience}",
        f"- Cleanup: {projection.cleanup_status}",
        *(
            (
                f"- Recorded estimated model cost: {_markdown_text(cost_details[0])}",
                f"- Pricing source: [{_markdown_text(cost_details[1])}](<{cost_details[2]}>)"
                if cost_details[1] and cost_details[2].startswith(("https://", "http://"))
                else f"- Pricing source: {_markdown_text(cost_details[1])}"
                if cost_details[1]
                else "",
            )
            if (cost_details := _recorded_cost_details(projection)) is not None
            else ()
        ),
        "",
        "## Integrity and provenance",
        "",
        f"- Revision ID: `{_markdown_code(projection.revision_id)}`",
        f"- Projection digest: `{_markdown_code(projection.digest)}`",
        f"- Completion digest: `{_markdown_code(projection.completion_digest or 'not sealed')}`",
        f"- Source seal: {_markdown_text(projection.source_seal_state)}",
    ]
    if target_progress is not None:
        lines.extend(("", "## Target progress", ""))
        fields = _target_progress_fields(target_progress)
        if fields:
            lines.extend(
                f"- {_markdown_text(_target_progress_field_text(field))}" for field in fields
            )
        else:
            lines.append("- Target state was not discovered from reviewed observations.")
        lines.append(
            f"- Target exchanges observed: {int(target_progress.get('target_exchange_count') or 0)}"
        )
        transition = target_progress.get("latest_transition")
        if isinstance(transition, Mapping):
            changes = tuple(
                item
                for item in transition.get("observations", ())
                if isinstance(item, Mapping) and "before_value" in item and "after_value" in item
            )
            if changes:
                change = changes[0]
                lines.append(
                    "- Latest measured change: "
                    + _markdown_text(str(change.get("name") or "state").replace("_", " ").title())
                    + f" {_markdown_text(change['before_value'])} → {_markdown_text(change['after_value'])}"
                    + f" ({_markdown_text(transition.get('outcome', 'unclear'))})"
                )
        if target_progress.get("history_truncated") is True:
            lines.append(
                "- Evidence window: recent evidence only; older observations are retained durably."
            )
    lines.extend(("", "## Coverage", ""))
    coverage = _coverage(projection)
    for key, label in (
        ("planned", "Attack techniques selected"),
        ("attempted", "Techniques executed against the target"),
        ("confirmed", "Confirmed findings"),
        ("negative", "Clear negatives"),
        ("partial", "Partial progress (target engaged, no verdict)"),
        ("unclear", "Inconclusive"),
        ("retryable_infrastructure", "Retryable infrastructure failures"),
        ("physically_unreachable", "Physically unreachable"),
        ("unresolved", "Unresolved"),
    ):
        lines.append(f"- {label}: {coverage.get(key, 0)}")
    provenance = _coverage_provenance(projection)
    lines.extend(("", "## Coverage provenance", ""))
    lines.append(
        f"- Candidate variations executed in this campaign: {provenance['newly_executed']}"
    )
    lines.append(
        f"- Candidate variations reused from conclusive history: {provenance['reused_from_history']}"
    )
    lines.append(f"- Candidate variations unresolved: {provenance['still_unresolved']}")
    lines.append(
        f"- Candidate variations durably unreachable: {provenance['physically_unreachable']}"
    )
    _markdown_rows(lines, "Language coverage", _language_coverage(projection))
    _markdown_rows(
        lines,
        "Cross-campaign learning",
        tuple(projection.metadata.get("cross_campaign_learning", ())),
    )
    _markdown_rows(
        lines,
        "Adaptive controllers",
        tuple(projection.metadata.get("adaptive_controllers", ())),
    )
    if technique_skips := tuple(
        item for item in projection.metadata.get("technique_skips", ()) if isinstance(item, Mapping)
    ):
        _markdown_rows(lines, "Skipped cells (prior campaign history)", technique_skips)
    if exploration:
        _markdown_rows(lines, "Exploration metrics", exploration)
    if observability:
        _markdown_rows(lines, "Execution observability", observability)
    lines.extend(("", "## Findings", ""))
    for finding in projection.findings:
        finding_id = str(finding.get("id"))
        lines.extend((f"### {_markdown_text(finding.get('title', finding_id))}", ""))
        lines.extend((f"- ID: `{_markdown_code(finding_id)}`",))
        lines.extend((f"- Severity: {_markdown_text(finding.get('severity', 'unknown'))}",))
        lines.extend((f"- Validation: {_markdown_text(finding.get('validation', 'unknown'))}", ""))
        lines.extend((_markdown_text(finding.get("summary", "")), ""))
        signal = finding.get("partial_signal")
        if isinstance(signal, Mapping):
            lines.extend(
                (
                    f"- Partial compliance (not a verdict) — observed: "
                    f"{_markdown_text(signal.get('observed', 'not recorded'))}",
                    f"- Refused: {_markdown_text(signal.get('refused', 'not recorded'))}",
                    "",
                )
            )
    lines.extend(("## Techniques", ""))
    for row in _ledger_technique_rows(projection):
        lines.append(
            f"- **{_markdown_text(_human_technique_name(row))}** — "
            f"{_markdown_text(_canonical_outcome_label(row))}: "
            f"{_markdown_text(row.get('rationale') or row.get('reason') or 'No execution result was recorded.')}"
        )
    if hypothesis_rows := _hypothesis_rows(projection):
        lines.extend(("", "## Hypotheses evaluated", ""))
        for row in hypothesis_rows:
            lines.append(f"- **{_markdown_text(row['outcome'])}** — {_markdown_text(row['claim'])}")
    lines.extend(("", "## Evidence", ""))
    for citation in projection.evidence:
        lines.append(
            f"- `{_markdown_code(citation.reference)}` — finding "
            f"{_markdown_text(citation.finding_id)}; witness {_markdown_text(citation.witness)}"
        )
    _markdown_rows(lines, "Target exchanges", projection.exchanges)
    _markdown_rows(lines, "Lineage", projection.lineage)
    _markdown_rows(lines, "Unsearched frontier", projection.unsearched_frontier)
    lines.extend(("", "## Limitations and cleanup", ""))
    lines.append(f"- Cleanup: {projection.cleanup_status}")
    lines.extend(
        f"- {_markdown_text(item)}" for item in _dedupe_limitations(projection.limitations)
    )
    return "\n".join(lines) + "\n"


def _markdown_rows(lines: list[str], title: str, rows: Sequence[Mapping[str, Any]]) -> None:
    lines.extend(("", f"## {title}", ""))
    if not rows:
        lines.append("No records captured.")
    for index, row in enumerate(rows, start=1):
        heading = next(
            (
                str(row[key]).strip()
                for key in ("name", "controller", "metric", "kind", "category", "id")
                if row.get(key) is not None and str(row[key]).strip()
            ),
            f"Record {index}",
        )
        lines.extend((f"### {_markdown_text(heading)}", ""))
        for key, item in row.items():
            if key in {"name", "controller", "metric", "kind", "category", "id"}:
                continue
            if item is None or item == "" or item == () or item == [] or item == {}:
                continue
            lines.append(
                f"- {_markdown_text(key.replace('_', ' ').title())}: "
                f"{_markdown_text(_markdown_human_value(item))}"
            )
        lines.append("")


def _markdown_human_value(value: Any) -> str:
    if isinstance(value, Mapping):
        return "; ".join(
            f"{str(key).replace('_', ' ').title()}: {_markdown_human_value(item)}"
            for key, item in value.items()
        )
    if isinstance(value, (tuple, list, set, frozenset)):
        return "; ".join(_markdown_human_value(item) for item in value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str) and re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)+", value):
        return value.replace("_", " ")
    return "Not recorded" if value is None else str(value)


def _markdown_text(value: Any) -> str:
    return "<br>".join(
        re.sub(r"([\\`*_{}\[\]()#+\-.!>|])", r"\\\1", html.escape(line, quote=False))
        for line in str(value).splitlines() or ("",)
    )


def _markdown_code(value: Any) -> str:
    return html.escape(str(value), quote=False).replace("`", "&#96;").replace("\n", " ")


def _customer_projection(value: CampaignReportProjection) -> CampaignReportProjection:
    customer_campaign_id = (
        "customer-campaign-" + hashlib.sha256(value.campaign_id.encode()).hexdigest()[:12]
    )
    customer_revision_id = f"{customer_campaign_id}:r{value.revision}"
    metadata = {
        key: value.metadata[key]
        for key in (
            "verdict",
            "confidence",
            "profile",
            "adaptive_controllers",
            "specialist_composition",
            "specialist_template_id",
            "specialist_template_version",
            "specialist_instances",
            "specialist_instance_count",
            "execution_task_records",
            "execution_task_count",
            "attempt_count",
            "unique_candidate_count",
            "repeat_attempt_count",
            "terminal_unique_candidate_count",
            "conflicting_unique_candidate_count",
            "unique_technique_count",
        )
        if key in value.metadata
    }
    cost_provenance = value.metadata.get("model_cost_provenance")
    if isinstance(cost_provenance, Mapping):
        metadata["model_cost_provenance"] = {
            key: item
            for key, item in _allowed(
                cost_provenance,
                (
                    "schedule_usage_estimated_cost_usd",
                    "campaign_lead_estimated_cost_usd",
                    "arm_outcome_cost_usd",
                    "arm_outcome_count",
                    "arm_cost_is_attribution_subset",
                    "pricing_source",
                    "pricing_source_url",
                    "pricing_as_of",
                    "cache_read_pricing",
                ),
            ).items()
            if item is not None and item != ""
        }
    target_progress = _target_progress(value)
    if target_progress is not None:
        metadata["target_progress"] = _customer_target_progress(target_progress)
    metadata["title"] = "Merlin customer assessment"
    metadata["dynamic_specialists"] = tuple(
        _allowed(row, ("name", "state", "admitted_wave", "retired_wave"))
        for row in value.metadata.get("dynamic_specialists", ())
        if isinstance(row, Mapping)
    )
    for key in (
        "arm_rankings",
        "rankings",
        "capability_probe_results",
        "capability_probes",
        "execution_reachability",
        "reachability",
    ):
        rows = value.metadata.get(key)
        if isinstance(rows, (tuple, list)):
            metadata[key] = tuple(
                _allowed(
                    row,
                    (
                        "name",
                        "kind",
                        "capability_kind",
                        "status",
                        "outcome",
                        "language",
                        "rank",
                        "score",
                        "reason",
                        "reachability_reason",
                    ),
                )
                for row in rows
                if isinstance(row, Mapping)
            )
    finding_ids = {
        str(row.get("id")): f"customer-finding-{index}"
        for index, row in enumerate(value.findings, start=1)
    }
    source_techniques = _ledger_candidate_rows(value)
    technique_ids = {
        str(row.get("id", row.get("technique_id"))): f"customer-technique-{index}"
        for index, row in enumerate(source_techniques, start=1)
    }
    semantic_aliases: dict[str, str] = {}
    ambiguous_aliases: set[str] = set()
    for original, customer in technique_ids.items():
        alias = original.split("@", 1)[0]
        if alias in semantic_aliases and semantic_aliases[alias] != customer:
            ambiguous_aliases.add(alias)
        else:
            semantic_aliases[alias] = customer
    name_ids: dict[str, str] = {}
    ambiguous_names: set[str] = set()
    semantic_id_aliases: dict[str, str] = {}
    ambiguous_semantic_ids: set[str] = set()
    for row in source_techniques:
        customer = technique_ids[str(row.get("id", row.get("technique_id")))]
        for raw_key, mapping, ambiguous in (
            (row.get("name"), name_ids, ambiguous_names),
            (row.get("semantic_id"), semantic_id_aliases, ambiguous_semantic_ids),
        ):
            if raw_key is None:
                continue
            key = str(raw_key)
            if key in mapping and mapping[key] != customer:
                ambiguous.add(key)
            else:
                mapping[key] = customer

    def _customer_lineage_technique(raw: object) -> str | None:
        key = str(raw)
        if key in technique_ids:
            return technique_ids[key]
        if key in name_ids and key not in ambiguous_names:
            return name_ids[key]
        if key not in ambiguous_aliases and key in semantic_aliases:
            return semantic_aliases[key]
        if key in semantic_id_aliases and key not in ambiguous_semantic_ids:
            return semantic_id_aliases[key]
        return None

    findings = tuple(
        {
            **_allowed(
                row,
                (
                    "title",
                    "summary",
                    "severity",
                    "validation",
                    "remediation_status",
                    "recommendation_status",
                ),
            ),
            "id": finding_ids[str(row.get("id"))],
            **(
                {"remediation": _redact_customer_text(str(row["remediation"]))}
                if row.get("remediation")
                else {}
            ),
        }
        for row in value.findings
    )
    tasks = tuple(
        {"id": f"customer-task-{index}", **_allowed(row, ("status",))}
        for index, row in enumerate(value.tasks, start=1)
    )
    techniques = tuple(
        _allowed(
            row,
            (
                "name",
                "outcome",
                "independent_reproduction",
                "requested_language",
                "actual_language",
                "language_evidence_status",
                "language_evidence_source",
                "planned_language",
                "observed_language",
                "language_verification",
                "language",
                "transform_id",
                "rank",
                "rank_reason",
                "decision_reason",
                "probe_status",
                "probe_outcome",
                "reachability_reason",
                "semantic_id",
                "strategy_id",
                "candidate_id",
                "equivalence_key",
                "controller_id",
                "payload_family",
                "language_id",
                "mutation_id",
                "failure_kind",
                "failure_class",
                "retryable",
                "infrastructure_failure",
                "physically_unreachable",
                "evaluation_uncertain",
                "evaluation_state",
                "lifecycle_state",
                "security_outcome",
                "ledger_source",
                "execution_reachability",
                "hypothesis_id",
                "pair_verdict",
            ),
        )
        | (
            {
                "strategy_execution": _allowed(
                    row["strategy_execution"],
                    (
                        "requested_controller",
                        "actual_controller",
                        "execution_mode",
                    ),
                )
            }
            if isinstance(row.get("strategy_execution"), Mapping)
            else {}
        )
        | {
            "id": technique_ids[str(row.get("id", row.get("technique_id")))],
            "candidate_id": technique_ids[str(row.get("id", row.get("technique_id")))],
            "name": str(
                row.get("name")
                or _human_label(row.get("technique_id") or row.get("id"), "Technique")
            ),
            "rationale": "Internal assessment rationale redacted.",
        }
        for row in source_techniques
    )
    exchange_ids = {
        str(row.get("id")): (
            f"customer-exchange-{ordinal + 1}-"
            + hashlib.sha256(str(row.get("id")).encode()).hexdigest()[:8]
        )
        for ordinal, row in enumerate(value.exchanges)
        if row.get("id") is not None
    }
    exchanges = tuple(
        {
            **_allowed(
                row,
                (
                    "outcome",
                    "response_kind",
                    "request_at",
                    "response_at",
                    "started_at",
                    "completed_at",
                    "observed_at",
                    "created_at",
                ),
            ),
            "id": exchange_ids.get(str(row.get("id")), "customer-exchange-unattributed"),
            "response_kind": row.get("response_kind")
            or (
                "browser_observation"
                if str(row.get("id", "")).startswith("browser:")
                else "assistant"
            ),
            "request": _redact_customer_text(_human_message(row.get("request"), role="user")),
            "response": _redact_customer_text(
                _human_message(row.get("response"), role="assistant")
            ),
        }
        for row in value.exchanges
    )
    missing_exchanges = sorted(
        {
            str(row.get("exchange"))
            for row in value.lineage
            if row.get("exchange") is not None and str(row.get("exchange")) not in exchange_ids
        }
    )
    if missing_exchanges:
        raise ValueError("customer report lineage refers to exchanges outside the report")
    lineage_rows: list[dict[str, Any]] = []
    redacted_lineage_count = 0
    for row in value.lineage:
        raw_technique = row.get("technique")
        resolved_technique = (
            _customer_lineage_technique(raw_technique) if raw_technique is not None else None
        )
        if raw_technique is not None and resolved_technique is None:
            redacted_lineage_count += 1
            continue
        lineage_rows.append(
            {
                **_allowed(row, ("stage", "status")),
                "technique": resolved_technique,
                "attempt": "[redacted]",
                **({"stage_receipt": "[redacted]"} if row.get("stage_receipt") is not None else {}),
                "exchange": (
                    exchange_ids[str(row.get("exchange"))]
                    if row.get("exchange") is not None
                    else None
                ),
                **{
                    key: "[redacted]"
                    for key in (
                        "validator_attempt",
                        "effect_receipt",
                        "request_effect_receipt",
                        "submit_effect_receipt",
                        "target_action_effect_receipt",
                        "response_effect_receipt",
                    )
                    if row.get(key) is not None
                },
                "pack": "[redacted]",
                "entry": "[redacted]",
            }
        )
    lineage = tuple(lineage_rows)
    evidence = tuple(
        EvidenceCitation(
            f"evidence://redacted/{index}",
            finding_ids[citation.finding_id],
            "[redacted]",
        )
        for index, citation in enumerate(value.evidence, start=1)
    )
    customer_limitations = _dedupe_limitations(
        (
            "Independent reproduction was not established."
            if "independent reproduction" in _limitation_text(item).casefold()
            else "Target exchange evidence was not available."
            if "target exchange" in _limitation_text(item).casefold()
            else "Cleanup obligations remain unresolved."
            if "cleanup" in _limitation_text(item).casefold()
            and "unresolved" in _limitation_text(item).casefold()
            else "An internal assessment limitation was redacted."
        )
        for item in value.limitations
    ) or ("No material customer-visible limitations were recorded.",)
    if redacted_lineage_count:
        customer_limitations = (
            *customer_limitations,
            "Some internal technique lineage was redacted.",
        )
    projected = replace(
        value,
        campaign_id=customer_campaign_id,
        revision_id=customer_revision_id,
        audience="customer",
        findings=findings,
        tasks=tasks,
        evidence=evidence,
        usage={
            key: amount
            for key, amount in value.usage.items()
            if key
            in {
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "tokens",
                "turns",
                "estimated_cost_usd",
                "cost_usd",
                "cost",
            }
            and isinstance(amount, (int, float))
            and not isinstance(amount, bool)
        },
        metadata=metadata,
        techniques=techniques,
        exchanges=exchanges,
        lineage=lineage,
        unsearched_frontier=tuple(
            {
                "id": f"customer-frontier-{index}",
                **_allowed(row, ("kind", "surface", "reason")),
            }
            for index, row in enumerate(value.unsearched_frontier, start=1)
        ),
        limitations=customer_limitations,
        hypotheses=tuple(
            {"claim": claim, "outcome": _hypothesis_outcome_label(row.get("state"))}
            for row in value.hypotheses
            if (claim := str(row.get("claim") or "").strip())
        ),
    )
    _validate_report_joins(projected.techniques, projected.exchanges, projected.lineage)
    return projected


def _allowed(row: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: row[key] for key in keys if key in row}


def _hypothesis_outcome_label(state: object) -> str:
    label = str(state or "").strip().casefold()
    return {
        "supported": "Supported by evidence",
        "falsified": "Refuted",
        "blocked_by_infrastructure": "Inconclusive",
    }.get(label, "Evaluated")


def _hypothesis_rows(value: CampaignReportProjection) -> tuple[dict[str, str], ...]:
    """Claim text plus outcome for each recorded hypothesis, ordered by creation time."""
    rows: list[tuple[tuple[int, str], int, str, str]] = []
    for ordinal, row in enumerate(value.hypotheses):
        claim = str(row.get("claim") or "").strip()
        if not claim:
            continue
        outcome = row.get("outcome")
        rows.append(
            (
                _hypothesis_sort_key(row.get("created_at")),
                ordinal,
                claim,
                str(outcome).strip()
                if isinstance(outcome, str) and outcome.strip()
                else _hypothesis_outcome_label(row.get("state")),
            )
        )
    return tuple(
        {"claim": claim, "outcome": outcome} for _sort, _ordinal, claim, outcome in sorted(rows)
    )


def _hypothesis_sort_key(value: object) -> tuple[int, str]:
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str) and value.strip():
        try:
            timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return (1, "")
    else:
        return (1, "")
    if timestamp.tzinfo is None:
        return (1, "")
    return (0, timestamp.astimezone(UTC).isoformat())


def _validate_report_joins(
    techniques: Sequence[Mapping[str, Any]],
    exchanges: Sequence[Mapping[str, Any]],
    lineage: Sequence[Mapping[str, Any]],
) -> None:
    exchange_ids = [str(row.get("id", "")) for row in exchanges]
    if "" in exchange_ids or len(set(exchange_ids)) != len(exchange_ids):
        raise ValueError("report exchanges require unique non-empty IDs")
    technique_ids = [str(row.get("id", row.get("technique_id", ""))) for row in techniques]
    if techniques and ("" in technique_ids or len(set(technique_ids)) != len(technique_ids)):
        raise ValueError("report techniques require unique non-empty IDs")
    technique_set = set(technique_ids)
    exchange_set = set(exchange_ids)
    for row in lineage:
        exchange = row.get("exchange")
        if exchange is not None and str(exchange) not in exchange_set:
            raise ValueError("report lineage refers to an exchange outside the report")
        technique = row.get("technique")
        if technique is not None and technique_ids and str(technique) not in technique_set:
            raise ValueError("report lineage refers to a technique outside the report")


def _validate_coverage(coverage: Mapping[str, int] | None) -> None:
    if not coverage:
        return
    keys = ("planned", "attempted", "confirmed", "negative", "unclear", "blocked", "untested")
    if any(type(coverage.get(key)) is not int or int(coverage[key]) < 0 for key in keys):
        raise ValueError("report coverage requires non-negative integer counts")
    # ``partial`` is an optional graded category: reports written before it
    # existed lumped those cells into ``unclear``.
    partial = coverage.get("partial", 0)
    if type(partial) is not int or partial < 0:
        raise ValueError("report coverage requires non-negative integer counts")
    outcomes = partial + sum(
        int(coverage[key]) for key in ("confirmed", "negative", "unclear", "blocked", "untested")
    )
    if outcomes != int(coverage["planned"]):
        raise ValueError("report coverage outcomes must equal planned coverage")
    observed = partial + sum(int(coverage[key]) for key in ("confirmed", "negative", "unclear"))
    if not observed <= int(coverage["attempted"]) <= int(coverage["planned"]):
        raise ValueError("report attempted coverage is inconsistent with outcomes")


@dataclass(frozen=True, slots=True)
class _CandidateLedgerEntry:
    """One admitted attack candidate with outcome and lifecycle kept separate."""

    candidate_id: str
    semantic_id: str
    display_name: str
    controller_id: str
    payload_family: str
    language_id: str
    mutation_id: str
    source: str
    security_outcome: str | None
    evaluation_state: str
    lifecycle_state: str
    requested_language: str
    actual_language: str | None
    language_evidence_status: str
    language_evidence_source: str | None
    reason: str | None
    original: Mapping[str, Any]

    @property
    def canonical_outcome(self) -> str:
        if self.security_outcome in {"confirmed", "negative"}:
            return self.security_outcome
        if self.lifecycle_state in {"retryable_infrastructure", "physically_unreachable"}:
            return self.lifecycle_state
        if _canonical_outcome(self.original) == "partial":
            return "partial"
        if self.evaluation_state == "uncertain":
            return "inconclusive"
        return "unresolved"

    def row(self) -> dict[str, Any]:
        return {
            **dict(self.original),
            "id": self.candidate_id,
            "candidate_id": self.candidate_id,
            "semantic_id": self.semantic_id,
            "name": self.display_name,
            "controller_id": self.controller_id,
            "strategy_id": self.controller_id,
            "payload_family": self.payload_family,
            "language_id": self.language_id,
            "mutation_id": self.mutation_id,
            "requested_language": self.requested_language,
            "actual_language": self.actual_language,
            "language_evidence_status": self.language_evidence_status,
            "language_evidence_source": self.language_evidence_source,
            "ledger_source": self.source,
            "security_outcome": self.security_outcome,
            "evaluation_state": self.evaluation_state,
            "lifecycle_state": self.lifecycle_state,
            "evaluation_uncertain": self.evaluation_state == "uncertain",
            "outcome": self.canonical_outcome,
            **({"reason": self.reason} if self.reason else {}),
        }


def _identity_axis(row: Mapping[str, Any], key: str, fallback: str) -> str:
    value = str(row.get(key) or "").strip()
    if value:
        return value
    identity = str(row.get("candidate_id") or row.get("id") or "")
    marker = {
        "language_id": "@lang=",
        "mutation_id": "@variant=",
    }.get(key)
    if marker and marker in identity:
        return identity.split(marker, 1)[1].split("@", 1)[0] or fallback
    return fallback


def _candidate_identity(row: Mapping[str, Any], *, index: int) -> tuple[str, ...]:
    semantic_id = _semantic_technique_id(row, index=index)
    controller_id = str(row.get("controller_id") or row.get("strategy_id") or "direct")
    payload_family = str(row.get("payload_family") or row.get("transform_id") or "baseline")
    language_id = _identity_axis(
        row,
        "language_id",
        str(
            row.get("requested_language")
            or row.get("planned_language")
            or row.get("language")
            or "und"
        ),
    )
    mutation_id = _identity_axis(
        row,
        "mutation_id",
        str(row.get("transform_id") or "baseline"),
    )
    return semantic_id, controller_id, payload_family, language_id, mutation_id


def _is_capability_probe(row: Mapping[str, Any]) -> bool:
    identity = " ".join(
        str(row.get(key) or "").casefold() for key in ("id", "candidate_id", "kind", "category")
    )
    return (
        "capability-probe" in identity
        or "capability_probe" in identity
        or identity.startswith("recon:")
    )


def _is_hypothesis(row: Mapping[str, Any]) -> bool:
    identity = " ".join(
        str(row.get(key) or "").strip().casefold()
        for key in ("id", "candidate_id", "technique_id", "kind", "category")
    )
    return any(
        token in identity
        for token in (
            "hypothesis:",
            "hypothesis-",
            "exploratory_hypothesis",
            "target-derived-hypothesis",
        )
    )


def _is_reportable_candidate(row: Mapping[str, Any]) -> bool:
    return not _is_capability_probe(row) and not _is_hypothesis(row)


def _frontier_candidates(row: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    if not _is_reportable_candidate(row):
        return ()
    semantic_id = str(row.get("semantic_id") or row.get("technique_id") or "").strip()
    if not semantic_id:
        # Frontier observations such as "Japanese gradual escalation" are not
        # admitted candidates unless they carry an unambiguous catalogue identity.
        return ()
    raw_variant_ids = row.get("variant_ids", ())
    raw_languages = row.get("languages", ())
    variant_ids = (
        tuple(str(item) for item in raw_variant_ids if str(item).strip())
        if isinstance(raw_variant_ids, (tuple, list))
        else ()
    )
    languages = (
        tuple(str(item) for item in raw_languages if str(item).strip())
        if isinstance(raw_languages, (tuple, list))
        else ()
    )
    if not variant_ids:
        return ({**dict(row), "outcome": "unresolved"},)
    return tuple(
        {
            **dict(row),
            "id": variant_id,
            "candidate_id": variant_id,
            "semantic_id": semantic_id,
            "language": languages[index]
            if len(languages) == len(variant_ids)
            else languages[0]
            if len(languages) == 1
            else "und",
            "outcome": "unresolved",
        }
        for index, variant_id in enumerate(variant_ids)
    )


def _candidate_ledger(value: CampaignReportProjection) -> tuple[_CandidateLedgerEntry, ...]:
    """Overlay execution facts on the complete admitted candidate frontier."""
    source_priority = {
        "legacy_coverage": 0,
        "admitted": 1,
        "frontier": 2,
        "completion": 3,
        "history": 4,
        "execution": 5,
        "validator": 6,
        "canonical": 7,
    }
    source_rows: list[tuple[str, Mapping[str, Any]]] = []
    canonical_projections = value.metadata.get("candidate_outcome_projections", ())
    if isinstance(canonical_projections, (tuple, list)):
        for raw in canonical_projections:
            if not isinstance(raw, Mapping):
                continue
            projection = validate_candidate_outcome_projection(raw)
            identity = raw.get("candidate_identity")
            identity = dict(identity) if isinstance(identity, Mapping) else {}
            source_rows.append(
                (
                    "canonical",
                    {
                        **identity,
                        "id": projection["candidate_id"],
                        "candidate_id": projection["candidate_id"],
                        "outcome": projection["outcome"],
                        "attempt_disposition": projection["attempt_disposition"],
                        "outcome_authority": projection["outcome_authority"],
                        "evidence_digest": projection["evidence_digest"],
                        "terminality": projection["terminality"],
                        "status": "executed",
                        "candidate_outcome_projection": projection,
                    },
                )
            )
    admitted = value.metadata.get("admitted_candidates", ())
    if isinstance(admitted, (tuple, list)):
        source_rows.extend(
            ("admitted", row)
            for row in admitted
            if isinstance(row, Mapping) and _is_reportable_candidate(row)
        )
    frontier_embedded = any(
        isinstance(row, Mapping) and row.get("ledger_source") == "frontier"
        for row in value.techniques
    )
    if not frontier_embedded:
        for row in value.unsearched_frontier:
            if isinstance(row, Mapping):
                source_rows.extend(("frontier", item) for item in _frontier_candidates(row))
    source_rows.extend(
        (
            str(row.get("ledger_source") or "execution")
            if str(row.get("ledger_source") or "execution") in source_priority
            else "execution",
            row,
        )
        for row in value.techniques
        if isinstance(row, Mapping) and _is_reportable_candidate(row)
    )
    candidate_references: dict[str, Mapping[str, Any]] = {}
    semantic_references: dict[str, set[str]] = {}
    for index, (_, row) in enumerate(source_rows):
        reference = str(
            row.get("candidate_id") or row.get("equivalence_key") or row.get("id") or ""
        ).strip()
        if not reference:
            continue
        candidate_references[reference] = row
        semantic_references.setdefault(_semantic_technique_id(row, index=index), set()).add(
            reference
        )

    def joined_candidate_reference(reference: object) -> str | None:
        requested = str(reference or "").strip()
        if not requested or _is_hypothesis({"id": requested}):
            return None
        if requested in candidate_references:
            return requested
        matches = semantic_references.get(requested.split("@", 1)[0], set())
        return next(iter(matches)) if len(matches) == 1 else None

    source_rows.extend(
        (
            "validator",
            {
                "id": joined_candidate_reference(row.get("technique_id")),
                "candidate_id": joined_candidate_reference(row.get("technique_id")),
                "technique_id": str(row.get("technique_id")),
                "outcome": "confirmed",
                "rationale": row.get("summary"),
            },
        )
        for row in value.findings
        if _finding_is_validated(row)
        and joined_candidate_reference(row.get("technique_id")) is not None
    )
    for row in value.findings:
        if not _finding_is_partial(row):
            continue
        candidate_reference = joined_candidate_reference(row.get("technique_id"))
        if candidate_reference is None:
            continue
        signal = row.get("partial_signal")
        source_rows.append(
            (
                "validator",
                {
                    "id": candidate_reference,
                    "candidate_id": candidate_reference,
                    "technique_id": str(row.get("technique_id")),
                    "outcome": "partial",
                    "rationale": row.get("validation_summary") or row.get("summary"),
                    **({"partial_signal": dict(signal)} if isinstance(signal, Mapping) else {}),
                },
            )
        )
    sealed = value.completion_document
    document = sealed.get("document") if isinstance(sealed, Mapping) else None
    if isinstance(document, Mapping):
        for cell in document.get("cells", ()):
            if isinstance(cell, (tuple, list)) and len(cell) >= 3:
                candidate_reference = joined_candidate_reference(cell[1])
                if candidate_reference is not None:
                    source_rows.append(
                        (
                            "completion",
                            {
                                "id": candidate_reference,
                                "candidate_id": candidate_reference,
                                "technique_id": str(cell[1]),
                                "outcome": str(cell[2]),
                                "attempt_id": str(cell[0]),
                                **(
                                    {"partial_signal": dict(cell[3])}
                                    if len(cell) > 3 and isinstance(cell[3], Mapping)
                                    else {}
                                ),
                            },
                        )
                    )
    source_rows.extend(
        (
            "completion",
            {
                "id": row["candidate_id"],
                "candidate_id": row["candidate_id"],
                "technique_id": row["candidate_id"],
                "outcome": row["outcome"],
                "status": row["state"],
                "attempt_id": row.get("attempt_id"),
                "task_id": row.get("task_id"),
                "evidence_references": tuple(row.get("evidence_refs", ())),
                "reason": row.get("disposition_reason"),
                "retryable": row["outcome"] == "retryable",
            },
        )
        for row in value.frontier_snapshot_rows
    )
    skips = value.metadata.get("technique_skips", ())
    if isinstance(skips, (tuple, list)):
        source_rows.extend(
            ("history", row)
            for row in skips
            if isinstance(row, Mapping)
            and _is_reportable_candidate(row)
            # Only conclusive history is coverage evidence ("reused from
            # conclusive history"). Deferred/skip rows stay visible in the
            # dedicated Skipped cells section; admitting them here inflates
            # the ledger with thousands of never-executed cells whose deferral
            # rationale then misclassifies as infrastructure failure.
            and _canonical_outcome(row) in {"confirmed", "negative"}
        )

    raw_language_evidence = value.metadata.get("actual_language_evidence", ())
    raw_language_evidence = (
        raw_language_evidence if isinstance(raw_language_evidence, (tuple, list)) else ()
    )
    evidence_by_attempt = {
        str(row.get("technique_attempt_id")): row
        for row in raw_language_evidence
        if isinstance(row, Mapping) and row.get("technique_attempt_id")
    }
    merged: dict[tuple[str, ...], _CandidateLedgerEntry] = {}
    identity_by_candidate_id: dict[str, tuple[str, ...]] = {}
    for index, (source, row) in enumerate(source_rows):
        identity = _candidate_identity(row, index=index)
        candidate_reference = str(
            row.get("candidate_id") or row.get("equivalence_key") or row.get("id") or ""
        ).strip()
        if candidate_reference and candidate_reference in identity_by_candidate_id:
            identity = identity_by_candidate_id[candidate_reference]
        elif candidate_reference:
            identity_by_candidate_id[candidate_reference] = identity
        attempt_ids = {
            str(item)
            for item in (
                row.get("attempt_id"),
                *(
                    row.get("attempt_ids", ())
                    if isinstance(row.get("attempt_ids"), (tuple, list))
                    else ()
                ),
            )
            if item
        }
        language_evidence = next(
            (evidence_by_attempt[item] for item in attempt_ids if item in evidence_by_attempt), None
        )
        raw = (
            str(row.get("outcome", row.get("status", "")))
            .strip()
            .casefold()
            .replace("-", "_")
            .replace(" ", "_")
        )
        classified = _canonical_outcome(row)
        security_outcome = (
            "confirmed"
            if classified == "confirmed"
            else "negative"
            if classified == "negative"
            else None
        )
        evaluation_uncertain = bool(
            row.get("evaluation_uncertain")
            or row.get("evaluation_uncertainties")
            or raw in {"unclear", "inconclusive", "evaluation_uncertain", "ambiguous", "regression"}
        )
        execution_status = str(row.get("lifecycle_state") or row.get("status") or "").strip()
        execution_evidenced = bool(
            source in {"completion", "validator"}
            or security_outcome
            or evaluation_uncertain
            or execution_status.casefold().replace("-", "_").replace(" ", "_")
            in {"attempted", "completed", "executed", "running", "succeeded"}
            or row.get("attempt_id")
            or row.get("attempt_ids")
            or row.get("task_id")
            or row.get("task_ids")
            or row.get("exchange_id")
            or row.get("target_effect_receipt")
        )
        lifecycle_state = (
            "reused"
            if source == "history" and security_outcome
            else classified
            if classified in {"retryable_infrastructure", "physically_unreachable"}
            else "executed"
            if execution_evidenced
            else "planned"
        )
        requested_language = str(
            row.get("requested_language")
            or row.get("planned_language")
            or row.get("language_id")
            or row.get("language")
            or identity[3]
            or "und"
        ).casefold()
        actual_language = (
            str(
                (language_evidence or {}).get("actual_language")
                or row.get("actual_language")
                or row.get("observed_language")
                or ""
            )
            .strip()
            .casefold()
            or None
        )
        evidence_status = str(
            (language_evidence or {}).get("language_evidence_status")
            or row.get("language_evidence_status")
            or row.get("language_verification")
            or "unavailable"
        ).casefold()
        candidate_id = str(candidate_reference or "|".join(identity))
        entry = _CandidateLedgerEntry(
            candidate_id=candidate_id,
            semantic_id=identity[0],
            display_name=_human_technique_name(row),
            controller_id=identity[1],
            payload_family=identity[2],
            language_id=identity[3],
            mutation_id=identity[4],
            source=source,
            security_outcome=security_outcome,
            evaluation_state="uncertain"
            if evaluation_uncertain
            else "conclusive"
            if security_outcome
            else "not_evaluated",
            lifecycle_state=lifecycle_state,
            requested_language=requested_language,
            actual_language=actual_language,
            language_evidence_status=evidence_status,
            language_evidence_source=str(
                (language_evidence or {}).get("language_evidence_source")
                or row.get("language_evidence_source")
                or ""
            ).strip()
            or None,
            reason=str(row.get("reason") or row.get("rationale") or "").strip() or None,
            original=row,
        )
        prior = merged.get(identity)
        if prior is None or (source_priority[source], _outcome_priority(entry.row())) > (
            source_priority[prior.source],
            _outcome_priority(prior.row()),
        ):
            merged[identity] = entry

    # Legacy report revisions may contain only aggregate coverage. Materialize
    # anonymous admitted cells so every renderer still reads one ledger instead
    # of mixing aggregate and row-level truth.
    planned = value.coverage.get("planned") if value.coverage else None
    if type(planned) is int and planned > len(merged):
        existing_outcomes = [item.canonical_outcome for item in merged.values()]
        desired = (
            ("confirmed", int(value.coverage.get("confirmed", 0))),
            ("negative", int(value.coverage.get("negative", 0))),
            ("partial", int(value.coverage.get("partial", 0))),
            ("inconclusive", int(value.coverage.get("unclear", 0))),
            ("legacy_blocked", int(value.coverage.get("blocked", 0))),
            ("unresolved", int(value.coverage.get("untested", 0))),
        )
        ordinal = 0
        for outcome, count in desired:
            missing = max(0, count - existing_outcomes.count(outcome))
            for _ in range(min(missing, planned - len(merged))):
                ordinal += 1
                identity = (f"legacy-admitted-{ordinal}", "direct", "baseline", "und", "baseline")
                security_outcome = outcome if outcome in {"confirmed", "negative"} else None
                merged[identity] = _CandidateLedgerEntry(
                    candidate_id=identity[0],
                    semantic_id=identity[0],
                    display_name="Unattributed technique",
                    controller_id="direct",
                    payload_family="baseline",
                    language_id="und",
                    mutation_id="baseline",
                    source="legacy_coverage",
                    security_outcome=security_outcome,
                    evaluation_state="uncertain"
                    if outcome == "inconclusive"
                    else "conclusive"
                    if security_outcome
                    else "not_evaluated",
                    lifecycle_state="executed"
                    if outcome in {"partial", "inconclusive"} or security_outcome
                    else "legacy_blocked"
                    if outcome == "legacy_blocked"
                    else "unresolved",
                    requested_language="und",
                    actual_language=None,
                    language_evidence_status="unavailable",
                    language_evidence_source=None,
                    reason="Legacy report recorded aggregate coverage without candidate identity.",
                    original={"outcome": "partial"} if outcome == "partial" else {},
                )
                existing_outcomes.append("unresolved" if outcome == "legacy_blocked" else outcome)
        while len(merged) < planned:
            ordinal += 1
            identity = (f"legacy-admitted-{ordinal}", "direct", "baseline", "und", "baseline")
            merged[identity] = _CandidateLedgerEntry(
                candidate_id=identity[0],
                semantic_id=identity[0],
                display_name="Unattributed technique",
                controller_id="direct",
                payload_family="baseline",
                language_id="und",
                mutation_id="baseline",
                source="legacy_coverage",
                security_outcome=None,
                evaluation_state="not_evaluated",
                lifecycle_state="unresolved",
                requested_language="und",
                actual_language=None,
                language_evidence_status="unavailable",
                language_evidence_source=None,
                reason="Legacy report recorded aggregate coverage without candidate identity.",
                original={},
            )
    return tuple(merged[key] for key in sorted(merged))


def _ledger_candidate_rows(value: CampaignReportProjection) -> tuple[Mapping[str, Any], ...]:
    return tuple(entry.row() for entry in _candidate_ledger(value))


def _ledger_technique_rows(value: CampaignReportProjection) -> tuple[Mapping[str, Any], ...]:
    return _unique_technique_rows(_ledger_candidate_rows(value))


def _coverage(value: CampaignReportProjection) -> dict[str, int]:
    return _coverage_rows(_ledger_technique_rows(value))


def _coverage_provenance(value: CampaignReportProjection) -> dict[str, int]:
    """Separate current execution, conclusive reuse, and unresolved frontier."""
    ledger = _candidate_ledger(value)
    return {
        "newly_executed": sum(
            item.source != "history" and item.lifecycle_state == "executed" for item in ledger
        ),
        "reused_from_history": sum(item.lifecycle_state == "reused" for item in ledger),
        "still_unresolved": sum(
            item.canonical_outcome
            in {"partial", "inconclusive", "retryable_infrastructure", "unresolved"}
            for item in ledger
        ),
        "physically_unreachable": sum(
            item.canonical_outcome == "physically_unreachable" for item in ledger
        ),
    }


def _unique_technique_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Return one aggregate row per semantic attack technique."""
    unique: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        identity = _semantic_technique_id(row, index=index)
        prior = unique.get(identity)
        if prior is None or _outcome_priority(row) > _outcome_priority(prior):
            unique[identity] = row
    return tuple(unique.values())


def _semantic_technique_id(row: Mapping[str, Any], *, index: int = 0) -> str:
    explicit = str(row.get("semantic_id") or row.get("technique_id") or "").strip()
    if explicit:
        return explicit.split("@", 1)[0]
    identity = str(row.get("id") or f"unattributed-{index}").strip()
    return identity.split("@", 1)[0]


def _reason_text(row: Mapping[str, Any]) -> str:
    values = (
        row.get("failure_kind"),
        row.get("failure_class"),
        row.get("reachability_reason"),
        row.get("terminal_reason"),
        row.get("reason"),
        row.get("rationale") if row.get("outcome") in {"blocked", "failed"} else None,
    )
    return " ".join(str(item).strip().casefold() for item in values if str(item or "").strip())


def _canonical_outcome(row: Mapping[str, Any]) -> str:
    """Classify durable evidence without treating scheduler labels as verdicts."""
    raw = str(row.get("outcome", row.get("status", ""))).strip().casefold().replace("-", "_")
    raw = raw.replace(" ", "_")
    if raw in {"confirmed", "validated", "authoritative_win", "finding"}:
        return "confirmed"
    if raw in {"negative", "refuted", "clean_negative", "clear_negative"}:
        return "negative"
    if raw in {"partial", "partial_signal", "partial_progress", "progress"}:
        return "partial"
    if raw in {
        "unclear",
        "inconclusive",
        "evaluation_uncertain",
        "ambiguous",
        "regression",
    }:
        return "inconclusive"
    if raw in {"attempted", "executed"} and (
        row.get("task_id")
        or row.get("task_ids")
        or row.get("attempt_id")
        or row.get("attempt_ids")
        or row.get("exchange_id")
    ):
        # Executed against the target with no verdict recorded: the cell engaged
        # the target, which is partial progress, not an unresolved cell.
        return "partial"
    reason = _reason_text(row)
    retryable_flag = row.get("retryable") is True or row.get("infrastructure_failure") is True
    retryable_terms = (
        "timeout",
        "truncat",
        "missing pointer",
        "missing response",
        "provider",
        "transport",
        "adapter",
        "infrastructure",
        "rate limit",
        "connection",
        "temporary",
        "retryable",
    )
    if retryable_flag or any(term in reason for term in retryable_terms):
        return "retryable_infrastructure"

    unreachable_flag = row.get("physically_unreachable") is True
    unreachable_terms = (
        "physically unreachable",
        "permanently impossible",
        "authoritatively absent",
        "transport cannot represent",
    )
    if unreachable_flag or any(term in reason for term in unreachable_terms):
        return "physically_unreachable"
    return "unresolved"


def _outcome_priority(row: Mapping[str, Any]) -> int:
    return {
        "confirmed": 7,
        "negative": 6,
        "partial": 5,
        "inconclusive": 4,
        "retryable_infrastructure": 3,
        "physically_unreachable": 2,
        "unresolved": 1,
    }[_canonical_outcome(row)]


def _coverage_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {
        key: 0
        for key in (
            "planned",
            "attempted",
            "confirmed",
            "negative",
            "partial",
            "unclear",
            "blocked",
            "untested",
            "retryable_infrastructure",
            "physically_unreachable",
            "unresolved",
        )
    }
    counts["planned"] = len(rows)
    for row in rows:
        if row.get("lifecycle_state") == "legacy_blocked":
            counts["blocked"] += 1
            counts["unresolved"] += 1
            continue
        outcome = _canonical_outcome(row)
        if outcome in {"confirmed", "negative", "partial", "inconclusive"}:
            counts["attempted"] += 1
        if outcome == "confirmed":
            counts["confirmed"] += 1
        elif outcome == "negative":
            counts["negative"] += 1
        elif outcome == "partial":
            counts["partial"] += 1
        elif outcome == "inconclusive":
            counts["unclear"] += 1
        elif outcome in {"retryable_infrastructure", "physically_unreachable"}:
            counts["blocked"] += 1
            counts[outcome] += 1
        else:
            counts["untested"] += 1
            counts["unresolved"] += 1
    return counts


def _language_coverage(value: CampaignReportProjection) -> tuple[dict[str, Any], ...]:
    grouped: dict[tuple[str, str, bool], list[Mapping[str, Any]]] = {}
    for row in _ledger_candidate_rows(value):
        identity = str(row.get("id", row.get("technique_id", "")))
        evidence_status = str(
            row.get("language_evidence_status") or row.get("language_verification") or "unavailable"
        ).casefold()
        actual_language = (
            str(row.get("actual_language") or row.get("observed_language") or "").strip().casefold()
        )
        actual_recorded = bool(actual_language) and evidence_status in {
            "observed",
            "inferred",
            "verified",
            "mismatch",
        }
        requested_language = str(
            row.get("requested_language")
            or row.get("planned_language")
            or row.get("language")
            or ""
        ).strip().casefold() or (
            identity.split("@lang=", 1)[1].split("@", 1)[0] if "@lang=" in identity else "und"
        )
        variant = str(row.get("mutation_id") or row.get("transform_id") or "").strip() or (
            identity.split("@variant=", 1)[1].split("@", 1)[0]
            if "@variant=" in identity
            else "baseline"
        )
        if actual_recorded:
            grouped.setdefault((actual_language, variant, True), []).append(row)
        if requested_language != "und" and (
            not actual_recorded or requested_language != actual_language
        ):
            grouped.setdefault((requested_language, variant, False), []).append(
                {**row, "outcome": "unresolved", "planned_language_only": True}
            )
    results = []
    for (language, variant, actual_recorded), rows in sorted(grouped.items()):
        planned = tuple(
            sorted(
                {
                    (
                        str(
                            row.get("requested_language")
                            or row.get("planned_language")
                            or row.get("language")
                            or ""
                        )
                        .strip()
                        .casefold()
                        or (
                            str(row.get("id", row.get("technique_id", "")))
                            .split("@lang=", 1)[1]
                            .split("@", 1)[0]
                            if "@lang=" in str(row.get("id", row.get("technique_id", "")))
                            else "und"
                        )
                    )
                    for row in rows
                }
            )
        )
        results.append(
            {
                "language": language,
                "variant": variant,
                "planned_languages": planned,
                "actual_language_recorded": actual_recorded,
                "language_evidence_sources": tuple(
                    sorted(
                        {
                            str(row.get("language_evidence_source")).strip()
                            for row in rows
                            if str(row.get("language_evidence_source") or "").strip()
                        }
                    )
                ),
                "language_mismatches": sum(
                    (
                        str(
                            row.get("language_evidence_status")
                            or row.get("language_verification")
                            or ""
                        ).casefold()
                        == "mismatch"
                        or (
                            actual_recorded
                            and str(
                                row.get("actual_language") or row.get("observed_language") or ""
                            )
                            .strip()
                            .casefold()
                            != str(
                                row.get("requested_language")
                                or row.get("planned_language")
                                or row.get("language")
                                or "und"
                            )
                            .strip()
                            .casefold()
                        )
                    )
                    for row in rows
                ),
                "language_unverified": sum(
                    str(
                        row.get("language_evidence_status")
                        or row.get("language_verification")
                        or "unavailable"
                    ).casefold()
                    in {"unverified", "not-recorded", "unavailable", "inconclusive"}
                    for row in rows
                ),
                **_coverage_rows(rows),
            }
        )
    return tuple(results)


def _report_observability(value: CampaignReportProjection) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    metadata_groups = (
        ("ARM ranking", ("arm_rankings", "rankings")),
        ("Capability probe", ("capability_probe_results", "capability_probes")),
        ("Execution reachability", ("execution_reachability", "reachability")),
    )
    for category, keys in metadata_groups:
        source = next(
            (
                value.metadata.get(key)
                for key in keys
                if isinstance(value.metadata.get(key), (list, tuple))
            ),
            (),
        )
        for item in source:
            if isinstance(item, Mapping):
                rows.append({"category": category, **dict(item)})
    for technique in value.techniques:
        details = _allowed(
            technique,
            (
                "rank",
                "rank_reason",
                "decision_reason",
                "probe_status",
                "probe_outcome",
                "reachability_reason",
            ),
        )
        reachability = technique.get("execution_reachability")
        if isinstance(reachability, Mapping):
            details.update(_allowed(reachability, ("status", "outcome", "reason")))
        if details:
            rows.append(
                {
                    "category": "Technique execution",
                    "name": technique.get("name") or technique.get("id") or "Technique",
                    **details,
                }
            )
    return tuple(rows)


def _exploration_metrics(value: CampaignReportProjection) -> tuple[dict[str, Any], ...]:
    """Project only explicitly persisted exploration measurements."""
    raw = value.metadata.get("exploration_metrics", value.metadata.get("exploration"))
    if not isinstance(raw, Mapping):
        return ()
    fields = (
        "source",
        "comparable_campaigns",
        "comparable_attempts",
        "seed_count",
        "seed_reasons",
        "hypotheses_attempted",
        "hypotheses_admitted",
        "hypotheses_rejected",
        "hypotheses_duplicate",
        "hypotheses_grounding_failed",
        "outcomes_confirmed",
        "outcomes_refuted",
        "outcomes_blocked",
        "outcomes_unclear",
        "outcomes_unsearched",
        "history_savings",
        "frontier_remaining",
        "scope",
        "promotion_evidence_status",
        "trusted_evaluator_receipts",
        "target_scoped_lessons",
        "promotion_candidates",
    )
    return tuple(
        {"metric": key, "value": raw[key]} for key in fields if key in raw and raw[key] is not None
    )


def _human_label(value: object, fallback: str = "Test") -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    raw = raw.split("@", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[-_]+", " ", raw).strip().title() or fallback


def _human_technique_name(row: Mapping[str, Any]) -> str:
    """Prefer catalogue identity over synthetic task labels."""
    supplied = str(row.get("name") or "").strip()
    if supplied and not re.fullmatch(r"(?i)(?:customer\s+)?task\s*\d+", supplied):
        return supplied
    identity = row.get("semantic_id") or row.get("technique_id") or row.get("id")
    return _human_label(identity, "Unattributed technique")


def _canonical_outcome_label(row: Mapping[str, Any]) -> str:
    return {
        "confirmed": "Confirmed finding",
        "negative": "Clear negative",
        "partial": "Partial progress",
        "inconclusive": "Inconclusive",
        "retryable_infrastructure": "Retryable infrastructure failure",
        "physically_unreachable": "Physically unreachable",
        "unresolved": "Unresolved",
    }[_canonical_outcome(row)]


def _limitation_text(item: object) -> str:
    if not isinstance(item, Mapping):
        return re.sub(r"\s+", " ", str(item)).strip()
    reason = str(item.get("reason") or item.get("message") or "").strip()
    category = str(
        item.get("failure_class") or item.get("failure_kind") or item.get("kind") or ""
    ).strip()
    technique = str(item.get("technique_name") or item.get("technique_id") or "").strip()
    prefix = ": ".join(part for part in (category.replace("_", " "), technique) if part)
    text = f"{prefix}: {reason}" if prefix and reason else prefix or reason
    count = item.get("count")
    if type(count) is int and count > 1:
        text += f" ({count} affected items)"
    return re.sub(r"\s+", " ", text).strip()


def _dedupe_limitations(items: Sequence[str] | Any) -> tuple[str, ...]:
    unique: dict[str, tuple[str, int, bool]] = {}
    for item in items:
        typed = isinstance(item, Mapping)
        if typed:
            text = _limitation_text(
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"technique_id", "technique_name", "count"}
                }
            )
            count = item.get("count", 1)
            count = count if type(count) is int and count > 0 else 1
        else:
            text = _limitation_text(item)
            count = 1
        if not text:
            continue
        key = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
        prior = unique.get(key)
        if prior is None:
            unique[key] = (text, count, typed)
        elif typed:
            unique[key] = (prior[0], prior[1] + count, True)
    return tuple(
        f"{text} ({count} affected items)" if typed and count > 1 else text
        for text, count, typed in unique.values()
    )


def _human_target(value: CampaignReportProjection) -> str:
    raw = str(
        value.metadata.get("target")
        or value.metadata.get("title")
        or value.metadata.get("objective")
        or ""
    ).strip()
    if not raw:
        return "the assessed target"
    match = re.match(r"https?://([^/?#]+)(/[^?#]*)?", raw)
    if match is None:
        return _human_label(raw, "the assessed target")
    host, path = match.groups()
    meaningful = [
        part for part in (path or "").split("/") if part and part not in {"api", "chat", "resume"}
    ]
    suffix = f" · {_human_label(meaningful[-1])}" if meaningful else ""
    return f"{host}{suffix}"


def _human_message(value: object, *, role: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if not text or text.startswith("[") and "receipt" in text.casefold():
            return ""
        if "redacted browser input" in text.casefold():
            return ""
        # A browser fill redacted at persistence carries no verifiable prompt text;
        # treat it as absent so the exchange is not rendered as a real conversation turn.
        if text == "[redacted]":
            return ""
        return text
    if isinstance(value, Mapping):
        keys = (
            ("message", "userMessage", "prompt", "content", "text", "input", "query")
            if role == "user"
            else ("assistant", "answer", "message", "content", "text", "output", "response")
        )
        for key in keys:
            candidate = value.get(key)
            text = _human_message(candidate, role=role)
            if text:
                return text
        return ""
    if isinstance(value, (tuple, list)):
        parts = [_human_message(item, role=role) for item in value]
        return "\n".join(part for part in parts if part)
    return str(value).strip()


def _redact_customer_text(value: str) -> str:
    """Remove credential-shaped text without hiding the operator-visible prompt."""
    if not value:
        return ""
    redacted = re.sub(
        r"(?i)(https?://)[^\s/:@]+:[^\s/@]+@",
        r"\1[redacted]@",
        value,
    )
    redacted = re.sub(
        r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|secret|password|credential)=)"
        r"[^&#\s]+",
        r"\1[redacted]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b((?:api[_-]?key|access[_-]?token|token|secret|password|credential)"
        r"\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;.!?]+)",
        r"\1[redacted]",
        redacted,
    )
    redacted = re.sub(
        r"(?im)^(\s*(?:cookie|set-cookie)\s*:\s*).+$",
        r"\1[redacted]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(Authorization\s*:\s*)?(?:Basic|Digest)\s+[^\r\n]+",
        lambda match: (match.group(1) or "") + "[redacted authorization]",
        redacted,
    )
    # Session IDs are NOT credentials: they are ephemeral target-chat scopes the
    # reader needs to correlate evidence. Genuine secret patterns stay.
    redacted = re.sub(
        r"(?i)\b((?:aws[_-]?secret[_-]?access[_-]?key)"
        r"\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;.!?]+)",
        r"\1[redacted]",
        redacted,
    )
    for pattern in (
        r"AIza[0-9A-Za-z_-]{35}",
        r"gh(?:p|o|u|s|r)_[A-Za-z0-9]{30,}",
        r"github_pat_[A-Za-z0-9_]{20,}",
        r"xox(?:b|p|a|r|s)-[A-Za-z0-9-]{10,}",
        r"(?:AKIA|ASIA)[A-Z0-9]{16}",
        r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}",
    ):
        redacted = re.sub(pattern, "[redacted]", redacted)
    redacted = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer [redacted]", redacted)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", redacted)


def _message_time(value: object) -> str:
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str) and value.strip():
        try:
            timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return ""
    else:
        return ""
    if timestamp.tzinfo is None:
        return ""
    normalized = timestamp.astimezone(UTC)
    machine = normalized.isoformat().replace("+00:00", "Z")
    label = normalized.strftime("%d %b %Y, %H:%M:%S UTC")
    return f'<time class="message-time" datetime="{machine}">{label}</time>'


def _exchange_sort_key(exchange: Mapping[str, Any]) -> tuple[int, str]:
    raw = (
        exchange.get("request_at")
        or exchange.get("started_at")
        or exchange.get("response_at")
        or exchange.get("completed_at")
        or exchange.get("created_at")
    )
    if isinstance(raw, datetime):
        timestamp = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            timestamp = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return (1, "")
    else:
        return (1, "")
    if timestamp.tzinfo is None:
        return (1, "")
    return (0, timestamp.astimezone(UTC).isoformat())


def _conversation_html(
    exchanges: Sequence[Mapping[str, Any]],
    metadata: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    # Exchanges that share a target session are one continuous conversation with the
    # target; render them as a single chat thread so the separator only appears
    # between distinct sessions, not between messages of the same session. Within a
    # session, consecutive exchanges attributed to the same technique form a chunk
    # introduced by a small caption. Exchanges without a session key keep the
    # per-exchange treatment; exchanges without an attributable technique render
    # without a caption.
    groups: dict[str, list[tuple[tuple[int, str], int, str, str | None]]] = {}
    for ordinal, exchange in enumerate(exchanges):
        user_text = _human_message(exchange.get("request"), role="user")
        assistant_text = _human_message(exchange.get("response"), role="assistant")
        request_at, response_at = exchange.get("request_at"), exchange.get("response_at")
        if request_at and response_at:
            try:
                request_time_value = datetime.fromisoformat(str(request_at).replace("Z", "+00:00"))
                response_time_value = datetime.fromisoformat(
                    str(response_at).replace("Z", "+00:00")
                )
                if response_time_value < request_time_value:
                    assistant_text = ""
            except (TypeError, ValueError):
                pass
        # A chat transcript is useful only when both sides are attributable. Browser
        # input is deliberately redacted at persistence time, so do not present its
        # orphaned response as though Merlin captured a complete conversation.
        if not user_text:
            continue
        missing_response = not assistant_text
        if missing_response:
            assistant_text = "No attributable response was recorded for this submission."
        turns = []
        if user_text:
            request_time = _message_time(
                exchange.get("request_at")
                or exchange.get("started_at")
                or exchange.get("created_at")
            )
            turns.append(
                '<li class="turn user"><span class="speaker">Merlin test prompt</span>'
                f'<div class="bubble">{html.escape(user_text)}</div>{request_time}</li>'
            )
        if assistant_text:
            response_time = _message_time(
                exchange.get("response_at")
                or exchange.get("completed_at")
                or exchange.get("observed_at")
                or exchange.get("created_at")
            )
            speaker = (
                "Response unavailable"
                if missing_response
                else "Browser observation"
                if (
                    exchange.get("response_kind") == "browser_observation"
                    or str(exchange.get("id", "")).startswith("browser:")
                )
                else "Assistant"
            )
            turns.append(
                f'<li class="turn assistant"><span class="speaker">{speaker}</span>'
                f'<div class="bubble">{html.escape(assistant_text)}</div>{response_time}</li>'
            )
        meta = metadata[ordinal] if metadata is not None and ordinal < len(metadata) else {}
        session_id = (
            exchange.get("target_session_id")
            or meta.get("target_session_id")
            or meta.get("browser_context")
        )
        technique = meta.get("technique")
        # Group by technique first: attempts against the same technique belong
        # in one titled chunk even when they ran in separate target sessions.
        # Only unattributed exchanges fall back to per-session grouping.
        if technique:
            group_key = f"technique:{technique}"
        elif isinstance(session_id, str) and session_id.strip():
            group_key = f"session:{session_id}"
        else:
            group_key = f"exchange:{ordinal}"
        groups.setdefault(group_key, []).append(
            (
                _exchange_sort_key(exchange),
                ordinal,
                "".join(turns),
                str(technique) if technique else None,
            )
        )

    def _render_group(group: list[tuple[tuple[int, str], int, str, str | None]]) -> str:
        chunks: list[str] = []
        current_technique: str | None = None
        first = True
        for _sort, _ordinal, turns, technique in sorted(group):
            if first or technique != current_technique:
                current_technique = technique
                first = False
                if technique is not None:
                    chunks.append(
                        '<li class="technique-caption" aria-label="Technique under test">'
                        f"{html.escape(technique)}</li>"
                    )
            chunks.append(turns)
        return '<li class="exchange"><ol class="turns">' + "".join(chunks) + "</ol></li>"

    conversations = [
        _render_group(group)
        for group in sorted(groups.values(), key=lambda g: min(item[0] for item in g))
    ]
    if not conversations:
        return '<p class="empty">No readable conversation was captured.</p>'
    return (
        '<details class="conversation-details"><summary>Show captured conversations</summary>'
        '<ol class="conversations" aria-label="Conversation evidence">'
        + "".join(conversations)
        + "</ol></details>"
    )


def _human_finding_html(row: Mapping[str, Any]) -> str:
    title = str(row.get("title") or "Validated finding")
    summary = str(row.get("summary") or "No plain-language summary was recorded.")
    severity = _human_label(row.get("severity"), "Unrated")
    remediation, recommendation_status = _remediation_view(
        row.get("remediation"),
        row.get("remediation_status") or row.get("recommendation_status"),
    )
    recommendation = remediation or "No remediation recommendation was recorded."
    # The status label is only meaningful when there is advice to label; a
    # "not recorded" placeholder already says so in prose.
    has_advice = bool(remediation) and not remediation.startswith("Not recorded")
    status_line = (
        f'<p class="meta">Recommendation status: {html.escape(recommendation_status)}</p>'
        if has_advice
        else ""
    )
    signal = row.get("partial_signal")
    partial_line = ""
    if isinstance(signal, Mapping):
        observed = html.escape(str(signal.get("observed") or "not recorded"))
        refused = html.escape(str(signal.get("refused") or "not recorded"))
        partial_line = (
            '<p class="meta">Partial compliance (not a verdict) — '
            f"observed: {observed}; refused: {refused}</p>"
        )
    return (
        '<article class="finding">'
        f'<div class="finding-heading"><h3>{html.escape(title)}</h3><span>{html.escape(severity)}</span></div>'
        f'<p>{html.escape(summary)}</p><p class="meta">Validation: '
        f"{html.escape(_friendly_value('validation', row.get('validation', 'unknown')))}</p>"
        f"{partial_line}"
        f"<h4>Recommended action</h4><p>{html.escape(recommendation)}</p>"
        f"{status_line}</article>"
    )


def _technique_method(row: Mapping[str, Any]) -> str:
    """Name the execution method evidenced for one technique result."""
    execution = row.get("strategy_execution")
    execution = execution if isinstance(execution, Mapping) else {}
    execution_mode = str(execution.get("execution_mode") or "").strip().casefold()
    requested = (
        str(execution.get("requested_controller") or row.get("strategy_id") or "direct")
        .strip()
        .casefold()
    )
    actual = str(execution.get("actual_controller") or "").strip().casefold()
    if execution_mode == "unavailable":
        return f"{_human_label(requested, 'Adaptive')} unavailable"
    if execution_mode == "degraded":
        actual_label = "Direct comparison" if actual == "direct" else _human_label(actual)
        return f"{actual_label} (requested {_human_label(requested)})"
    strategy = actual or requested
    if strategy == "goat":
        return "GOAT"
    if strategy == "hydra-select":
        return "Hydra"
    if strategy == "crescendo":
        return "Crescendo"
    if row.get("hypothesis_id"):
        return "Exploration"
    if row.get("pair_verdict") is not None or row.get("pair_receipt") is not None:
        return "PAIR"
    return "Direct comparison"


def _language_label(language: object, variant: object) -> str:
    raw_language = str(language or "").strip().casefold()
    language = {
        "am": "Amharic (am)",
        "ar": "Arabic (ar)",
        "az": "Azerbaijani (az)",
        "bn": "Bengali (bn)",
        "de": "German (de)",
        "en": "English (en)",
        "es": "Spanish (es)",
        "fa": "Persian (fa)",
        "fr": "French (fr)",
        "gu": "Gujarati (gu)",
        "ha": "Hausa (ha)",
        "hi": "Hindi (hi)",
        "id": "Indonesian (id)",
        "ig": "Igbo (ig)",
        "it": "Italian (it)",
        "ja": "Japanese (ja)",
        "jv": "Javanese (jv)",
        "km": "Khmer (km)",
        "kn": "Kannada (kn)",
        "ko": "Korean (ko)",
        "la": "Latin (la)",
        "lzh": "Classical Chinese (lzh)",
        "ml": "Malayalam (ml)",
        "mr": "Marathi (mr)",
        "ms": "Malay (ms)",
        "my": "Burmese (my)",
        "ne": "Nepali (ne)",
        "nl": "Dutch (nl)",
        "pa": "Punjabi (pa)",
        "pl": "Polish (pl)",
        "pt": "Portuguese (pt)",
        "ro": "Romanian (ro)",
        "ru": "Russian (ru)",
        "si": "Sinhala (si)",
        "sw": "Swahili (sw)",
        "ta": "Tamil (ta)",
        "te": "Telugu (te)",
        "th": "Thai (th)",
        "tl": "Filipino (tl)",
        "tok": "Toki Pona (tok)",
        "tr": "Turkish (tr)",
        "uk": "Ukrainian (uk)",
        "ur": "Urdu (ur)",
        "uz": "Uzbek (uz)",
        "vi": "Vietnamese (vi)",
        "yo": "Yoruba (yo)",
        "yue": "Cantonese (yue)",
        "zh": "Chinese (zh)",
        "zh-en": "Chinese + English (zh-en)",
        "und": "Undetermined (und)",
    }.get(
        raw_language,
        "Default / not recorded" if not raw_language else raw_language,
    )
    raw_variant = str(variant or "").strip().casefold()
    variant = "Baseline" if raw_variant in {"", "baseline", "native"} else _human_label(raw_variant)
    return f"{language} · {variant}"


def _finding_is_validated(row: Mapping[str, Any]) -> bool:
    return str(row.get("validation") or "").strip().casefold() in {
        "validated",
        "independently reproduced",
    }


def _finding_is_partial(row: Mapping[str, Any]) -> bool:
    return str(row.get("validation") or "").strip().casefold() == "partial"


def _capability_probe_html(rows: Sequence[Mapping[str, Any]]) -> str:
    """Render reconnaissance as a compact human summary, not attack techniques."""
    if not rows:
        return ""
    surface_labels = {
        "agent messaging": "Agent-to-agent messaging",
        "memory": "Persistent memory",
        "multimodal": "Images or audio",
        "rag retrieval": "Retrieval / RAG",
        "tools": "External tools",
        "upload document": "Document upload",
    }
    accepted_languages = sorted(
        {
            str(row.get("language") or "").strip()
            for row in rows
            if str(row.get("capability_kind") or row.get("kind") or "").casefold()
            == "non advertised language"
            and str(row.get("outcome") or row.get("status") or "").casefold()
            in {"accepted", "supported", "unexpectedly accepted"}
            and str(row.get("language") or "").strip()
        }
    )
    summary_rows: list[tuple[str, str]] = []
    if accepted_languages:
        summary_rows.append(
            (
                "Non-advertised languages",
                "Target replied successfully to harmless checks in "
                + ", ".join(accepted_languages)
                + ".",
            )
        )
    seen_surfaces: set[str] = set()
    for row in rows:
        kind = str(row.get("capability_kind") or row.get("kind") or "").strip().casefold()
        if not kind or kind == "non advertised language" or kind in seen_surfaces:
            continue
        seen_surfaces.add(kind)
        outcome = str(row.get("outcome") or row.get("status") or "").strip().casefold()
        if outcome in {"accepted", "supported", "unexpectedly accepted", "confirmed"}:
            finding = "Confirmed by a harmless discovery check."
        elif outcome in {"rejected", "unsupported", "not available"}:
            finding = "Not observed during this scan."
        else:
            finding = "Not confirmed; this surface remains eligible for future testing."
        summary_rows.append((surface_labels.get(kind, _human_label(kind)), finding))
    table_rows = "".join(
        '<tr><th scope="row">'
        + html.escape(surface)
        + "</th><td>"
        + html.escape(finding)
        + "</td></tr>"
        for surface, finding in summary_rows
    )
    return (
        '<section class="recon-summary"><h3>What the target appears to support</h3>'
        '<p class="scope-note">Before attack testing, Merlin safely checks for interaction '
        "surfaces that may expose additional weaknesses. These checks are reconnaissance, "
        "not attack techniques.</p>"
        '<div class="table-wrap" role="region" aria-label="Reconnaissance summary" tabindex="0">'
        '<table class="result-table"><thead><tr><th>Surface</th><th>What Merlin learned</th>'
        f"</tr></thead><tbody>{table_rows}</tbody></table></div></section>"
    )


def _conversation_metadata(
    value: CampaignReportProjection,
    source: CampaignReportProjection | None,
) -> tuple[dict[str, str | None], ...] | None:
    """Per-exchange conversation grouping data from the stored (internal) document.

    Customer projections redact session identifiers and hash exchange ids, so the
    grouping data is derived from the internal source document by ordinal; customer
    exchanges preserve the source order. Returns None when ordinals do not align.
    """
    base = source if source is not None else value
    if len(base.exchanges) != len(value.exchanges):
        return None
    technique_names: dict[str, str] = {}
    for row in base.techniques:
        label = _human_technique_name(row)
        for key in (
            row.get("id"),
            row.get("technique_id"),
            row.get("name"),
            row.get("semantic_id"),
        ):
            if key is not None:
                technique_names.setdefault(str(key), label)
    exchange_techniques: dict[str, str] = {}
    recorded_exchange_techniques = base.metadata.get("exchange_techniques")
    if isinstance(recorded_exchange_techniques, Mapping):
        caption_labels: dict[str, str] = {}
        for row in base.techniques:
            for key in (row.get("id"), row.get("technique_id"), row.get("name")):
                if key is not None:
                    caption_labels.setdefault(
                        str(key),
                        _human_label(row.get("semantic_id") or row.get("id"), "Technique"),
                    )
        for exchange_id, technique_id in recorded_exchange_techniques.items():
            if exchange_id is not None and technique_id:
                exchange_techniques.setdefault(
                    str(exchange_id),
                    caption_labels.get(str(technique_id), _human_label(technique_id, "Technique")),
                )
    for row in base.lineage:
        exchange = row.get("exchange")
        technique = row.get("technique")
        if exchange is None or technique is None:
            continue
        name = technique_names.get(str(technique))
        if name is not None:
            exchange_techniques.setdefault(str(exchange), name)
    return tuple(
        {
            "target_session_id": (
                str(row.get("target_session_id"))
                if row.get("target_session_id") is not None
                else None
            ),
            # Browser-captured exchanges carry no target session id; the browser
            # context embedded in their id (browser:{context}:...) is the same
            # continuous conversation, so it is the grouping key of last resort.
            "browser_context": (
                f"browser-context:{match.group(1)}"
                if (match := re.fullmatch(r"browser:([0-9a-fA-F]+):.*", str(row.get("id") or "")))
                else None
            ),
            "technique": exchange_techniques.get(str(row.get("id"))),
        }
        for row in base.exchanges
    )


def _html_report(
    value: CampaignReportProjection, source: CampaignReportProjection | None = None
) -> str:
    technique_records = _ledger_technique_rows(value)
    candidate_records = _ledger_candidate_rows(value)
    coverage = _coverage(value)
    planned = coverage.get("planned", 0)
    clear = coverage.get("confirmed", 0) + coverage.get("negative", 0)
    partial = coverage.get("partial", 0)
    inconclusive = coverage.get("unclear", 0)
    retryable = coverage.get("retryable_infrastructure", 0)
    unreachable = coverage.get("physically_unreachable", 0)
    unresolved = coverage.get("unresolved", 0)
    executed = clear + partial + inconclusive
    unfinished = retryable + unreachable + unresolved
    provenance = _coverage_provenance(value)
    validated_findings = tuple(row for row in value.findings if _finding_is_validated(row))
    objective_outcome = value.metadata.get("campaign_outcome")
    objective_incomplete = (
        value.report_state == "incomplete"
        and isinstance(objective_outcome, Mapping)
        and bool(objective_outcome.get("unresolved_objective_ids"))
    )
    if validated_findings:
        verdict_label = "Action required"
        verdict = f"{len(validated_findings)} validated security finding{'s' if len(validated_findings) != 1 else ''} require attention."
        verdict_class = "danger"
    elif objective_incomplete:
        verdict_label = "Scan ended with unresolved results"
        verdict = (
            f"Merlin executed {executed} of {planned} selected techniques against the target: "
            f"{clear} produced a clear result, {partial} made partial progress, and "
            f"{inconclusive} were inconclusive. "
            f"{retryable} hit retryable infrastructure failures, {unreachable} were proven "
            f"physically unreachable, and {unresolved} remain unresolved. "
            "No validated finding was established, "
            "but this does not prove the target is safe."
        )
        verdict_class = "warning"
    elif not unfinished and clear == planned:
        verdict_label = "No issue observed in tested scope"
        verdict = "No validated vulnerability was found in the tests that produced a clear result. This is not a guarantee that the target is safe."
        verdict_class = "success"
    elif (
        value.terminal_reason is CampaignTerminalReason.FRONTIER_EXHAUSTED
        and value.report_state == "provisional"
    ):
        verdict_label = "Scan completed with limitations"
        verdict = (
            f"The campaign reached a terminal state and cleanup finished. Merlin addressed "
            f"{executed} of {planned} selected techniques with a real target result: {clear} "
            f"were clear, {partial} made partial progress, and {inconclusive} were "
            f"inconclusive. {retryable} hit retryable "
            f"infrastructure failures, {unreachable} were proven physically unreachable, "
            f"and {unresolved} remain unresolved. No validated finding was established, but these "
            "unresolved "
            "results do not prove the target is safe."
        )
        verdict_class = "warning"
    elif executed == planned and value.unsearched_frontier and not retryable:
        verdict_label = "Technique testing completed; exploration remains"
        verdict = (
            "All selected campaign tests ran, but required target-derived exploration was "
            "not completed. Treat the report as provisional."
        )
        verdict_class = "warning"
    elif value.report_state == "incomplete":
        verdict_label = "Scan ended with unresolved results"
        verdict = (
            f"Merlin executed {executed} of {planned} selected techniques against the target: "
            f"{clear} produced a clear result, {partial} made partial progress, and "
            f"{inconclusive} were inconclusive. "
            f"{retryable} hit retryable infrastructure failures, {unreachable} were proven "
            f"physically unreachable, and {unresolved} remain unresolved. "
            "No validated finding was established, "
            "but this does not prove the target is safe."
        )
        verdict_class = "warning"
    else:
        verdict_label = "Unable to draw a complete security conclusion"
        verdict = "The scan ended before all required tests finished. Review the coverage and limitations before relying on this assessment."
        verdict_class = "warning"

    findings = "".join(_human_finding_html(row) for row in value.findings) or (
        '<p class="empty">No validated security finding was established. '
        "The coverage below defines exactly what this statement includes.</p>"
    )
    target_progress = _target_progress(value)
    target_progress_html = ""
    if target_progress is not None:
        progress_fields = _target_progress_fields(target_progress)
        progress_cards = "".join(
            '<div class="metric"><strong>'
            + html.escape(str(field.get("value")))
            + (
                " / " + html.escape(str(field.get("maximum_value")))
                if field.get("maximum_value") is not None
                else ""
            )
            + "</strong><span>"
            + html.escape(str(field.get("label") or field.get("name") or "State"))
            + "</span></div>"
            for field in progress_fields
        )
        if not progress_cards:
            progress_cards = (
                '<p class="empty">Target state was not discovered from reviewed observations.</p>'
            )
        transition_note = ""
        transition = target_progress.get("latest_transition")
        if isinstance(transition, Mapping):
            changes = tuple(
                item
                for item in transition.get("observations", ())
                if isinstance(item, Mapping) and "before_value" in item and "after_value" in item
            )
            if changes:
                change = changes[0]
                transition_note = (
                    '<p class="scope-note"><strong>Latest measured change:</strong> '
                    + html.escape(str(change.get("name") or "state").replace("_", " ").title())
                    + " "
                    + html.escape(str(change["before_value"]))
                    + " → "
                    + html.escape(str(change["after_value"]))
                    + " · "
                    + html.escape(str(transition.get("outcome") or "unclear"))
                    + "</p>"
                )
        exchange_count = int(target_progress.get("target_exchange_count") or 0)
        evidence_window = " in recent evidence" if target_progress.get("history_truncated") else ""
        target_progress_html = (
            '<section id="target-progress"><h2>Target progress</h2>'
            '<p class="scope-note">Authoritative state observed from the target during this campaign. '
            "Security findings and target progress are separate measurements.</p>"
            f'<div class="metrics">{progress_cards}</div>{transition_note}'
            f'<p class="scope-note">{exchange_count} target exchange'
            f"{'s' if exchange_count != 1 else ''} observed{evidence_window}.</p></section>"
        )
    coverage_cards = "".join(
        f'<div class="metric"><strong>{count}</strong><span>{label}</span></div>'
        for count, label in (
            (planned, "Selected techniques"),
            (executed, "Techniques executed"),
            (clear, "Clear results"),
            (partial, "Partial progress"),
            (inconclusive, "Inconclusive"),
            (retryable, "Retryable infrastructure failures"),
            (unreachable, "Physically unreachable"),
            (unresolved, "Unresolved"),
        )
    )
    provenance_cards = "".join(
        f'<div class="metric"><strong>{count}</strong><span>{label}</span></div>'
        for count, label in (
            (provenance["newly_executed"], "Candidate variations executed now"),
            (provenance["reused_from_history"], "Candidate variations reused from history"),
            (provenance["still_unresolved"], "Candidate variations unresolved"),
            (provenance["physically_unreachable"], "Candidate variations durably unreachable"),
        )
    )
    technique_rows = (
        "".join(
            '<tr><th scope="row">'
            + html.escape(_human_technique_name(row))
            + "</th><td>"
            + html.escape(_technique_method(row))
            + '</td><td><span class="outcome">'
            + html.escape(_canonical_outcome_label(row))
            + "</span></td></tr>"
            for row in technique_records
        )
        or '<tr><td colspan="3" class="empty">No technique result was recorded.</td></tr>'
    )
    remediation = (
        '<section id="remediation"><h2>Recommended actions</h2><ol class="action-list">'
        + "".join(
            "<li><strong>"
            + html.escape(str(row.get("title") or "Finding"))
            + "</strong><span>"
            + html.escape(
                _remediation_view(row.get("remediation"))[0] or "Recommendation unavailable."
            )
            + "</span></li>"
            for row in value.findings
        )
        + "</ol></section>"
        if value.findings
        else ""
    )
    frontier_reasons: dict[str, int] = {}
    for row in value.unsearched_frontier:
        reason = str(row.get("reason") or "Required work was not completed.")
        frontier_reasons[reason] = frontier_reasons.get(reason, 0) + 1
    frontier = "".join(
        "<li>" + html.escape(reason) + (f" ({count} required items)" if count > 1 else "") + "</li>"
        for reason, count in frontier_reasons.items()
    )
    limitation_items = list(_dedupe_limitations(value.limitations))
    frontier_keys = {
        re.sub(r"[^a-z0-9]+", " ", reason.casefold()).strip() for reason in frontier_reasons
    }
    limitation_items = [
        item
        for item in limitation_items
        if re.sub(r"[^a-z0-9]+", " ", item.casefold()).strip() not in frontier_keys
    ]
    if unfinished and not frontier:
        limitation_items.append(
            f"{unfinished} selected technique{'s' if unfinished != 1 else ''} remain unresolved."
        )
    limitations = (
        "".join(f"<li>{html.escape(item)}</li>" for item in limitation_items)
        or "<li>No material limitation was recorded.</li>"
    )
    composition = _human_label(value.metadata.get("specialist_composition"), "Not specified")
    specialist_rows = "".join(
        "<dt>Specialist</dt><dd>"
        + html.escape(_human_label(item.get("name"), "Specialist"))
        + "</dd>"
        for item in value.metadata.get("dynamic_specialists", ())
        if isinstance(item, Mapping)
    )
    selected_methods = {_technique_method(row) for row in technique_records}
    unselected_methods = tuple(
        _human_label(item.get("controller"), "Adaptive method")
        for item in value.metadata.get("adaptive_controllers", ())
        if isinstance(item, Mapping)
        and str(item.get("status", "")).casefold() in {"not selected", "not evidenced"}
        and _human_label(item.get("controller"), "Adaptive method") not in selected_methods
    )
    unselected_note = (
        '<p class="scope-note"><strong>Adaptive methods not used:</strong> '
        + html.escape(", ".join(dict.fromkeys(unselected_methods)))
        + ". The method column below shows what ran for each technique.</p>"
        if unselected_methods
        else '<p class="scope-note">The method column shows what ran for each technique.</p>'
    )
    language_rows = tuple(
        row
        for row in _language_coverage(value)
        if str(row.get("language") or "").strip().casefold()
        and (
            bool(row.get("actual_language_recorded"))
            or str(row.get("language") or "").strip().casefold() != "und"
        )
    )
    language_chart = (
        "".join(
            '<li class="language-row"><div><strong>'
            + html.escape(_language_label(row.get("language"), row.get("variant")))
            + (
                "</strong><small>Planned / unverified; no actual request-language "
                "observation was durably recorded.</small><strong>"
                if not row.get("actual_language_recorded")
                else "</strong><small>Actual request language from durable evidence.</small>"
                "<strong>"
            )
            + (
                "</strong><small>Actual request differed from planned language: "
                + html.escape(
                    ", ".join(
                        _language_label(item, "baseline").split(" · ", 1)[0]
                        for item in row.get("planned_languages", ())
                    )
                )
                + ". This attempt was not credited to the planned cell.</small><strong>"
                if int(row.get("language_mismatches", 0))
                else ""
            )
            + "</strong><span>"
            + html.escape(
                f"{int(row.get('confirmed', 0)) + int(row.get('negative', 0))} clear · "
                f"{int(row.get('partial', 0))} partial · "
                f"{int(row.get('unclear', 0))} inconclusive · "
                f"{int(row.get('retryable_infrastructure', 0))} infrastructure retry · "
                f"{int(row.get('physically_unreachable', 0))} unreachable · "
                f"{int(row.get('unresolved', 0))} unresolved"
            )
            + '</span></div><div class="coverage-bar" aria-label="'
            + html.escape(
                f"{int(row.get('attempted', 0))} of {int(row.get('planned', 0))} tests ran"
            )
            + '"><span class="clear" style="width:'
            + str(
                round(
                    100
                    * (int(row.get("confirmed", 0)) + int(row.get("negative", 0)))
                    / max(1, int(row.get("planned", 0))),
                    1,
                )
            )
            + '%"></span><span class="partial" style="width:'
            + str(
                round(
                    100 * int(row.get("partial", 0)) / max(1, int(row.get("planned", 0))),
                    1,
                )
            )
            + '%"></span><span class="unclear" style="width:'
            + str(
                round(
                    100 * int(row.get("unclear", 0)) / max(1, int(row.get("planned", 0))),
                    1,
                )
            )
            + '%"></span><span class="blocked" style="width:'
            + str(
                round(
                    100
                    * (int(row.get("blocked", 0)) + int(row.get("untested", 0)))
                    / max(1, int(row.get("planned", 0))),
                    1,
                )
            )
            + '%"></span></div></li>'
            for row in language_rows
        )
        or '<li class="empty">No language coverage was recorded.</li>'
    )
    usage = next(
        (
            value.usage.get(key)
            for key in ("total_tokens", "tokens")
            if isinstance(value.usage.get(key), (int, float))
            and not isinstance(value.usage.get(key), bool)
        ),
        None,
    )
    if usage is None:
        token_parts = tuple(
            value.usage.get(key)
            for key in ("input_tokens", "output_tokens")
            if isinstance(value.usage.get(key), (int, float))
            and not isinstance(value.usage.get(key), bool)
        )
        usage = sum(token_parts) if token_parts else None
    usd_cost = next(
        (
            value.usage.get(key)
            for key in ("estimated_cost_usd", "cost_usd")
            if isinstance(value.usage.get(key), (int, float))
            and not isinstance(value.usage.get(key), bool)
        ),
        None,
    )
    undenominated_cost = value.usage.get("cost")
    if not isinstance(undenominated_cost, (int, float)) or isinstance(undenominated_cost, bool):
        undenominated_cost = None
    usage_text = f"{int(usage):,}" if isinstance(usage, (int, float)) else "Not recorded"
    cost_text = (
        f"${float(usd_cost):,.4f}"
        if isinstance(usd_cost, (int, float))
        else f"{float(undenominated_cost):,.4f} (currency not recorded)"
        if isinstance(undenominated_cost, (int, float))
        else "Not recorded"
    )
    recorded_specialists = value.metadata.get(
        "specialist_instance_count", value.metadata.get("specialist_instances")
    )
    specialist_count: object = (
        recorded_specialists
        if type(recorded_specialists) is int and recorded_specialists >= 0
        else "Not recorded"
    )
    summary_metrics = [
        (len(validated_findings), "Validated findings"),
        (len(technique_records), "Unique attack techniques selected"),
        (len(candidate_records), "Technique variations selected"),
        (specialist_count, "Worker executions"),
        (f"{clear} / {planned}", "Techniques with a clear result"),
        (usage_text, "Model tokens"),
    ]
    if usd_cost is not None or undenominated_cost is not None:
        summary_metrics.append((cost_text, "Recorded estimated model cost"))
    summary_cards = "".join(
        f'<div class="metric"><strong>{html.escape(str(metric))}</strong><span>{html.escape(label)}</span></div>'
        for metric, label in summary_metrics
    )
    specialist_states: dict[str, str] = {}
    for item in value.metadata.get("dynamic_specialists", ()):
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        if name and name not in specialist_states:
            specialist_states[name] = str(item.get("state") or "").strip().casefold()
    if specialist_states:
        specialist_detail = ", ".join(
            f"{name} ({state})" if state and state != "active" else name
            for name, state in specialist_states.items()
        )
        summary_cards += (
            f'<div class="metric"><strong>{len(specialist_states)}</strong>'
            "<span>Distinct specialist types</span>"
            f'<small class="metric-detail">{html.escape(specialist_detail)}</small></div>'
        )
    else:
        specialist_template = str(value.metadata.get("specialist_template_id") or "").strip()
        if specialist_template:
            summary_cards += (
                f'<div class="metric"><strong>{html.escape(specialist_template)}</strong>'
                "<span>Specialist template</span></div>"
            )
    cost_provenance = value.metadata.get("model_cost_provenance")
    pricing_source = str(
        cost_provenance.get("pricing_source") if isinstance(cost_provenance, Mapping) else ""
    ).strip()
    pricing_source_url = str(
        cost_provenance.get("pricing_source_url") if isinstance(cost_provenance, Mapping) else ""
    ).strip()
    if pricing_source.casefold() == "models.dev" and not pricing_source_url:
        pricing_source_url = "https://models.dev"
    cost_note = (
        "A cost value was recorded without a currency denomination, so it is not presented as USD."
        if usd_cost is None and undenominated_cost is not None
        else "Recorded cost sums Campaign Lead and scheduled specialist estimates. ARM cost is "
        "an attribution subset and is not added a second time."
        if isinstance(cost_provenance, Mapping)
        and cost_provenance.get("arm_cost_is_attribution_subset") is True
        else "Cost reflects only model-usage estimates durably recorded for this report."
    )
    cost_source_note = (
        '<dd>Pricing source: <a href="'
        + html.escape(pricing_source_url, quote=True)
        + '">'
        + html.escape(pricing_source)
        + "</a>.</dd>"
        if pricing_source and pricing_source_url.startswith(("https://", "http://"))
        else f"<dd>Pricing source: {html.escape(pricing_source)}.</dd>"
        if pricing_source
        else ""
    )
    cost_detail = (
        '<dl class="detail-card"><dt>Recorded estimated model cost</dt><dd>'
        + html.escape(cost_text)
        + "</dd><dd>"
        + html.escape(cost_note)
        + f"</dd>{cost_source_note}</dl>"
        if usd_cost is not None or undenominated_cost is not None
        else ""
    )
    hypothesis_rows = _hypothesis_rows(value)
    hypotheses_html = (
        '<section id="hypotheses"><h2>Hypotheses evaluated</h2>'
        '<p class="scope-note">Evidence-backed attack claims the campaign lead tested, '
        "with the outcome recorded for each.</p>"
        '<ol class="hypotheses">'
        + "".join(
            '<li class="hypothesis"><span class="outcome">'
            + html.escape(row["outcome"])
            + "</span><p>"
            + html.escape(row["claim"])
            + "</p></li>"
            for row in hypothesis_rows
        )
        + "</ol></section>"
        if hypothesis_rows
        else ""
    )
    observability_rows = _report_observability(value)
    capability_rows = tuple(
        row for row in observability_rows if row.get("category") == "Capability probe"
    )
    other_observability_rows = tuple(
        row for row in observability_rows if row.get("category") != "Capability probe"
    )
    observability = (
        '<section id="observability"><h2>Discovery and execution notes</h2>'
        '<p class="scope-note">This section explains reconnaissance and why particular attack '
        "methods ran. Technical evidence is collapsed by default.</p>"
        + _capability_probe_html(capability_rows)
        + ('<div class="detail-grid">' if other_observability_rows else "")
        + "".join(
            '<dl class="detail-card"><dt>'
            + html.escape(str(row.get("category") or row.get("name") or "Execution evidence"))
            + "</dt><dd>"
            + html.escape(
                _markdown_human_value(
                    {
                        key: item
                        for key, item in row.items()
                        if key not in {"category", "name"}
                        and item is not None
                        and item != ""
                        and item != ()
                        and item != []
                    }
                )
            )
            + "</dd></dl>"
            for row in other_observability_rows
        )
        + ("</div>" if other_observability_rows else "")
        + "</section>"
        if observability_rows
        else ""
    )
    clear_end = round(100 * clear / max(1, planned), 2)
    partial_end = round(100 * (clear + partial) / max(1, planned), 2)
    unclear_end = round(100 * (clear + partial + inconclusive) / max(1, planned), 2)
    retryable_end = round(100 * (clear + partial + inconclusive + retryable) / max(1, planned), 2)
    unreachable_end = round(
        100 * (clear + partial + inconclusive + retryable + unreachable) / max(1, planned), 2
    )
    coverage_chart = (
        '<div class="coverage-overview"><div class="coverage-donut" role="img" '
        f'aria-label="{clear} of {planned} techniques produced a clear result" '
        f'style="--clear-end:{clear_end}%;--partial-end:{partial_end}%;--unclear-end:{unclear_end}%;'
        f'--retryable-end:{retryable_end}%;--unreachable-end:{unreachable_end}%"><span><strong>{clear} / {planned}</strong>'
        '<small>clear results</small></span></div><ul class="coverage-legend">'
        f'<li><i class="clear"></i>Clear result: {clear}</li>'
        f'<li><i class="partial"></i>Partial progress: {partial}</li>'
        f'<li><i class="unclear"></i>Inconclusive: {inconclusive}</li>'
        f'<li><i class="retryable"></i>Retryable infrastructure: {retryable}</li>'
        f'<li><i class="unreachable"></i>Physically unreachable: {unreachable}</li>'
        f'<li><i class="unresolved"></i>Unresolved: {unresolved}</li></ul></div>'
    )
    technique_skip_rows = "".join(
        '<tr><th scope="row">'
        + html.escape(str(item.get("technique_id", "")))
        + "</th><td>"
        + html.escape(_friendly_value("outcome", item.get("outcome", "unknown")))
        + "</td><td>"
        + html.escape(str(item.get("source_campaign_id", "")))
        + "</td></tr>"
        for item in value.metadata.get("technique_skips", ())
        if isinstance(item, Mapping)
    )
    technique_skips_html = (
        '<section id="technique-skips"><h2>Skipped cells (prior campaign history)</h2>'
        '<p class="scope-note">These catalog cells were not re-run: a prior campaign on the '
        "same reviewed target already reached a terminal outcome. The imported outcome and "
        "source campaign are listed so coverage claims stay attributable.</p>"
        '<div class="table-wrap" role="region" aria-label="Skipped cells" tabindex="0">'
        '<table class="result-table"><thead><tr><th>Technique</th><th>Imported outcome</th>'
        "<th>Source campaign</th></tr></thead><tbody>"
        + technique_skip_rows
        + "</tbody></table></div></section>"
        if technique_skip_rows
        else ""
    )
    nav_items = (
        ("summary", "Executive summary"),
        *((("target-progress", "Target progress"),) if target_progress_html else ()),
        ("findings", "Findings"),
        *((("remediation", "Actions"),) if value.findings else ()),
        ("coverage", "Coverage"),
        ("coverage-provenance", "Coverage provenance"),
        *((("language", "Language coverage"),) if language_rows else ()),
        ("techniques", "Techniques & methods"),
        *((("hypotheses", "Hypotheses"),) if hypothesis_rows else ()),
        *((("technique-skips", "Skipped cells"),) if technique_skip_rows else ()),
        *((("observability", "Execution observability"),) if observability_rows else ()),
        ("conversation", "Conversation"),
        ("details", "Details"),
    )
    nav = "".join(f'<a href="#{section}">{label}</a>' for section, label in nav_items)
    target = _human_target(value)
    generated = value.created_at.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")
    audience_label = "Customer report" if value.audience == "customer" else "Internal report"
    rendered = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Merlin assessment report</title><style>
:root{{--ink:#20262d;--muted:#5e6873;--line:#dce1e5;--paper:#fbfbfa;--panel:#f1f3f4;--accent:#a92835;--good:#176b4d;--warn:#885f00;--user:#dce9ff;--assistant:#eceff1}}*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}a{{color:#2458a6}}a:focus-visible,summary:focus-visible{{outline:3px solid #2458a6;outline-offset:3px}}.layout{{display:grid;grid-template-columns:190px minmax(0,820px);gap:48px;max-width:1120px;margin:auto;padding:40px 28px 80px}}nav{{position:sticky;top:24px;align-self:start;border-right:1px solid var(--line);padding-right:20px}}nav strong{{display:block;margin-bottom:10px}}nav a{{display:block;min-height:44px;padding:10px 4px;text-decoration:none}}main{{min-width:0}}header{{padding-bottom:32px;border-bottom:1px solid var(--line)}}.eyebrow{{font-weight:700;color:var(--accent);letter-spacing:.08em;text-transform:uppercase}}h1{{font:600 clamp(34px,4vw,46px)/1.08 ui-serif,Georgia,serif;margin:8px 0}}h2{{font:600 28px/1.2 ui-serif,Georgia,serif;margin:56px 0 18px;padding-top:10px;border-top:1px solid var(--line)}}h3,h4{{margin:0 0 8px}}.target,.meta,.scope-note,.empty{{color:var(--muted)}}.verdict{{border-left:5px solid var(--warn);background:#fff7df;padding:18px 20px;margin-top:22px}}.verdict.success{{border-color:var(--good);background:#eaf6f0}}.verdict.danger{{border-color:var(--accent);background:#fff0f1}}.verdict strong{{display:block;font-size:19px;margin-bottom:4px}}.finding{{background:var(--panel);border-left:4px solid var(--accent);padding:20px;margin:12px 0}}.finding-heading{{display:flex;gap:12px;justify-content:space-between;align-items:start}}.finding-heading span,.outcome{{font-size:13px;font-weight:700;background:white;border:1px solid var(--line);border-radius:999px;padding:3px 8px}}.action-list{{padding-left:22px}}.action-list li{{margin:12px 0}}.action-list span{{display:block;color:var(--muted)}}.metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}.metric{{background:var(--panel);padding:15px;min-height:92px;min-width:0}}.metric strong,.metric span{{display:block}}.metric strong{{font-size:25px;overflow-wrap:anywhere}}.metric-detail{{display:block;margin-top:6px;color:var(--muted);font-size:12px;overflow-wrap:anywhere}}.metric span{{color:var(--muted)}}.scope-note{{max-width:72ch}}.conversations,.turns{{list-style:none;padding:0;margin:0}}.exchange{{border-bottom:1px solid var(--line);padding:18px 0}}.turn{{display:flex;flex-direction:column;align-items:flex-start;margin:12px 0}}.turn.user{{align-items:flex-end}}.speaker{{font-size:12px;font-weight:700;color:var(--muted);margin:0 8px 4px}}.bubble{{max-width:min(75ch,82%);padding:12px 15px;border-radius:16px 16px 16px 4px;background:var(--assistant);white-space:pre-wrap;overflow-wrap:anywhere}}.user .bubble{{background:var(--user);border-radius:16px 16px 4px 16px}}.techniques{{list-style:none;padding:0;display:grid;gap:8px}}.technique{{display:flex;justify-content:space-between;gap:16px;padding:12px 0;border-bottom:1px solid var(--line)}}.detail-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}}.detail-card{{background:var(--panel);padding:16px}}.detail-card dt{{font-weight:700}}.detail-card dd{{margin:3px 0 0;color:var(--muted)}}.mobile-nav{{display:none}}@media(max-width:760px){{.layout{{display:block;padding:24px 18px 60px}}nav{{display:none}}.mobile-nav{{display:block;margin:20px 0}}.mobile-nav summary{{min-height:44px;padding:10px;border:1px solid var(--line);cursor:pointer}}.mobile-nav a{{display:block;min-height:44px;padding:10px}}.metrics,.detail-grid{{grid-template-columns:1fr 1fr}}.bubble{{max-width:92%}}h2{{margin-top:44px}}}}@media(max-width:430px){{.metrics,.detail-grid{{grid-template-columns:1fr}}}}@media print{{nav,.mobile-nav{{display:none}}.layout{{display:block;max-width:none;padding:0}}.bubble{{max-width:80%}}}}
.summary-metrics{{margin-top:18px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}}.language-chart{{list-style:none;padding:0;margin:0;display:grid;gap:14px}}.language-row>div:first-child{{display:flex;justify-content:space-between;gap:20px}}.language-row span{{color:var(--muted)}}.coverage-bar{{height:10px;background:#e1e5e8;border-radius:999px;overflow:hidden;margin-top:7px}}.coverage-bar span{{display:block;height:100%;background:var(--good)}}.table-wrap{{overflow-x:auto}}.result-table{{width:100%;border-collapse:collapse;text-align:left}}.result-table th,.result-table td{{padding:12px 10px;border-bottom:1px solid var(--line);vertical-align:top}}.result-table thead th{{font-size:13px;color:var(--muted);background:var(--panel)}}.result-table tbody th{{font-weight:600}}@media(max-width:760px){{.summary-metrics{{grid-template-columns:1fr 1fr}}.language-row>div:first-child{{display:block}}}}@media(max-width:430px){{.summary-metrics{{grid-template-columns:1fr}}}}
.conversation-details{{border:1px solid var(--line);background:#fff;padding:0 16px}}.conversation-details summary{{cursor:pointer;min-height:44px;padding:12px 0;font-weight:700}}.message-time{{display:block;margin:4px 8px 0;color:var(--muted);font-size:11px}}.technique-caption{{margin:18px 8px 2px;font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}}.hypotheses{{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:minmax(0,1fr);gap:10px}}.hypothesis{{min-width:0;background:var(--panel);padding:14px 16px}}.hypothesis p{{margin:8px 0 0;white-space:pre-wrap;overflow-wrap:anywhere}}
@media(max-width:760px){{main{{display:flex;flex-direction:column}}.mobile-nav{{order:-1;position:sticky;top:0;z-index:10;margin:0 0 20px;background:var(--paper);border-bottom:1px solid var(--line)}}.mobile-nav summary{{position:relative;padding-right:64px;font-weight:700;background:var(--paper)}}.mobile-nav summary::after{{content:"Menu";position:absolute;right:10px;font-size:12px;color:var(--muted);font-weight:600}}.mobile-nav[open] summary::after{{content:"Close"}}.mobile-nav a{{background:var(--paper);border-top:1px solid var(--line);text-decoration:none}}}}
:root{{--retryable:#b96657;--unreachable:#7c6aa6;--unresolved:#c9ced3;--partial:#4f7ba6}}.coverage-overview{{display:flex;align-items:center;gap:32px;margin:22px 0}}.coverage-donut{{position:relative;width:190px;aspect-ratio:1;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--good) 0 var(--clear-end),var(--partial) var(--clear-end) var(--partial-end),#d5a234 var(--partial-end) var(--unclear-end),var(--retryable) var(--unclear-end) var(--retryable-end),var(--unreachable) var(--retryable-end) var(--unreachable-end),var(--unresolved) var(--unreachable-end) 100%)}}.coverage-donut::before{{content:"";position:absolute;width:118px;aspect-ratio:1;border-radius:50%;background:var(--paper)}}.coverage-donut span{{position:relative;text-align:center}}.coverage-donut strong,.coverage-donut small{{display:block}}.coverage-donut strong{{font-size:24px}}.coverage-donut small{{color:var(--muted)}}.coverage-legend{{list-style:none;padding:0;margin:0;display:grid;gap:8px}}.coverage-legend i{{display:inline-block;width:11px;height:11px;margin-right:8px;background:var(--unresolved)}}.coverage-legend i.clear{{background:var(--good)}}.coverage-legend i.partial{{background:var(--partial)}}.coverage-legend i.unclear{{background:#d5a234}}.coverage-legend i.retryable{{background:var(--retryable)}}.coverage-legend i.unreachable{{background:var(--unreachable)}}.coverage-legend i.unresolved{{background:var(--unresolved)}}.coverage-bar{{display:flex}}.coverage-bar span{{background:none}}.coverage-bar .clear{{background:var(--good)}}.coverage-bar .partial{{background:var(--partial)}}.coverage-bar .unclear{{background:#d5a234}}.coverage-bar .blocked{{background:var(--unresolved)}}@media(max-width:430px){{.coverage-overview{{align-items:flex-start;flex-direction:column}}}}
</style></head><body><div class="layout">
<nav aria-label="Report contents"><strong>Contents</strong>{nav}</nav><main>
<header id="summary"><div class="eyebrow">Security assessment · {html.escape(audience_label)}</div><h1>{html.escape(target)}</h1>
<p class="target">Generated {html.escape(generated)}</p><div class="verdict {verdict_class}">
<strong>{html.escape(verdict_label)}</strong><span>{html.escape(verdict)}</span></div>
<div class="metrics summary-metrics">{summary_cards}</div></header>
<details class="mobile-nav"><summary>Report contents</summary>{nav}</details>
{target_progress_html}<section id="findings"><h2>Findings</h2>{findings}</section>{remediation}
<section id="coverage"><h2>Coverage</h2><p class="scope-note">Attack techniques are counted once by catalogue identity. Language, mutation, encoding, and strategy variations are candidates beneath those techniques. Specialist agents and scheduler task records are operational counts, not additional techniques.</p>
{coverage_chart}<div class="metrics">{coverage_cards}</div></section>
<section id="coverage-provenance"><h2>Coverage provenance</h2><p class="scope-note">These are candidate-variation counts, not additional attack techniques. They separate work performed now from conclusive results imported from the same reviewed target generation. Inconclusive, retryable, and unstarted work stays unresolved and remains eligible for future execution; durably unreachable work is reported separately.</p><div class="metrics">{provenance_cards}</div></section>
<section id="language"><h2>Language coverage</h2><p class="scope-note">Rows appear only for tests with an explicitly requested or durably observed language. Default-language behavior is not inferred when evidence is absent.</p><ul class="language-chart">{language_chart}</ul></section>
<section id="techniques"><h2>Techniques and execution methods</h2>{unselected_note}<div class="table-wrap" role="region" aria-label="Technique results" tabindex="0"><table class="result-table"><thead><tr><th>Technique</th><th>Method used</th><th>Result</th></tr></thead><tbody>{technique_rows}</tbody></table></div></section>{hypotheses_html}{technique_skips_html}{observability}
<section id="conversation"><h2>Conversation evidence</h2><p class="scope-note">Merlin test prompts appear on the right and target replies on the left. Internal request envelopes and evidence identifiers are excluded.</p>{_conversation_html(value.exchanges, _conversation_metadata(value, source))}</section>
<section id="details"><h2>Assessment details</h2><div class="detail-grid"><dl class="detail-card"><dt>Specialist composition</dt><dd>{html.escape(composition.casefold())}</dd>{specialist_rows}</dl><dl class="detail-card"><dt>Cleanup</dt><dd>{html.escape(_friendly_value("cleanup", value.cleanup_status))}</dd></dl><dl class="detail-card"><dt>Model tokens</dt><dd>{html.escape(usage_text)}</dd></dl>{cost_detail}<dl class="detail-card"><dt>Result state</dt><dd>{html.escape(_report_state_label(value))}</dd></dl><dl class="detail-card"><dt>Audience</dt><dd>{html.escape(audience_label)}</dd></dl></div><h3>Limitations</h3><ul>{limitations}{frontier}</ul></section>
</main></div></body></html>"""
    if not language_rows:
        empty_language_section = (
            '<section id="language"><h2>Language coverage</h2><p class="scope-note">'
            "Rows appear only for tests with an explicitly requested or durably observed "
            "language. Default-language behavior is not inferred when evidence is absent."
            '</p><ul class="language-chart"><li class="empty">'
            "No language coverage was recorded.</li></ul></section>"
        )
        rendered = rendered.replace(empty_language_section, "")
    return rendered


def _verdict(value: CampaignReportProjection) -> str:
    if value.report_state == "provisional":
        if value.terminal_reason is CampaignTerminalReason.FRONTIER_EXHAUSTED:
            return (
                "All required testing and cleanup completed, but some results were inconclusive. "
                "Review the limitations before relying on the security conclusion."
            )
        return "Scan ended; the security conclusion is provisional because independent validation remains outstanding."
    if value.report_state != "complete":
        coverage = _coverage(value)
        remaining = len(value.unsearched_frontier)
        if coverage.get("attempted", 0) == coverage.get("planned", 0) and remaining:
            return (
                "All selected technique evaluations ran, but the assessment ended with "
                f"{remaining} required follow-up item{'s' if remaining != 1 else ''} "
                "unfinished. No overall security conclusion can be drawn; the remaining "
                "work is listed below."
            )
        return (
            "The scan ended before all required testing was finished. Merlin completed "
            f"{coverage.get('attempted', 0)} of {coverage.get('planned', 0)} required technique "
            "evaluations. No security conclusion can be drawn; remaining work is listed below."
        )
    return f"Assessment completed with {len(value.findings)} validated finding{'s' if len(value.findings) != 1 else ''}."


def _report_title(value: CampaignReportProjection) -> str:
    title = str(value.metadata.get("title") or value.metadata.get("objective") or value.campaign_id)
    target_url = re.search(r"https?://[^\s]+", title)
    if target_url:
        path = target_url.group(0).split("?", 1)[0].rstrip("/").split("/")[3:]
        meaningful = next(
            (part for part in path if part.casefold() not in {"chat", "resume", "api"}),
            "target",
        )
        return f"{meaningful.replace('-', ' ').title()} security assessment"
    if title.startswith("Assessment of "):
        return "Security assessment of " + title.removeprefix("Assessment of ")
    return title


def _report_verdict(value: CampaignReportProjection) -> str:
    stored = str(value.metadata.get("verdict") or "")
    if value.report_state != "complete" or "incomplete" in stored.casefold():
        return _verdict(value)
    return stored or _verdict(value)


def _report_state_label(value: CampaignReportProjection) -> str:
    if value.report_state == "complete":
        return "Merlin scan completed"
    if value.report_state == "provisional":
        return (
            "Merlin scan completed · limitations present"
            if value.terminal_reason is CampaignTerminalReason.FRONTIER_EXHAUSTED
            else "Merlin scan ended · conclusion provisional"
        )
    coverage = _coverage(value)
    addressed = coverage.get("planned", 0) - coverage.get("untested", 0)
    if addressed == coverage.get("planned", 0):
        return "Merlin scan ended · some results remain unresolved"
    return "Merlin scan ended · selected techniques remain unaddressed"


def _report_status_label(value: CampaignReportProjection) -> str:
    if value.report_state == "incomplete":
        return "ended with required tests unfinished"
    return value.stop_reason.replace("_", " ")


def _remediation_view(value: Any, recorded_status: Any = None) -> tuple[str, str]:
    remediation = str(value or "").strip()
    status = {
        "recommended": "Recommended",
        "verified": "Verified",
    }.get(str(recorded_status or "").strip().casefold(), "Unreviewed")
    for marker, implied in (("[recommended]", "Recommended"), ("[verified]", "Verified")):
        if remediation.casefold().startswith(marker):
            remediation = remediation[len(marker) :].strip()
            # Legacy marker prefix implies the status when none was recorded.
            if not str(recorded_status or "").strip():
                status = implied
            break
    return remediation, status


def _coverage_label(key: str) -> str:
    return {
        "planned": "Required",
        "attempted": "Tests run",
        "clear_results": "Tests with a clear result",
        "confirmed": "Confirmed test outcome",
        "negative": "No issue observed",
        "partial": "Partial progress",
        "unclear": "Inconclusive",
        "blocked": "Not completed",
        "untested": "Not started",
    }.get(key, key.replace("_", " ").title())


def _friendly_value(key: str, value: object) -> str:
    rendered = str(value)
    common = {
        "not_recorded": "Not available",
        "catalogue_technique": "Catalogued test",
        "adaptive_strategy": "Adaptive test",
        "campaign_receipt": "Campaign evidence",
        "authoritative adaptive outcome": "Required adaptive outcome",
        "historical_exact_match": "Exact previous result reused",
        "revalidate_finding": "Previous finding scheduled for verification",
        "revalidate_drift": "Retest required because the target changed",
        "revalidate_setup_only": "Previous setup evidence only; test required",
        "new": "First test in this scenario",
        "response_not_observed": "Response not observed",
        "legacy_unsealed": "Legacy report; evidence seal unavailable",
        "sealed": "Verified against sealed campaign evidence",
        "und": "Not specified",
        "negative": "No issue observed",
        "partial": "Partial progress",
        "confirmed": "Confirmed test outcome",
        "unclear": "Inconclusive",
        "admitted technique has no terminal campaign receipt": "Required technique was not completed before the scan ended.",
        "adaptive strategy outcome remains unresolved": "Required adaptive test did not reach a conclusive result.",
        "unvalidated": "Claimed — not independently reproduced",
        "validated": "Independently reproduced",
    }
    if key in {"outcome", "status"} and rendered == "blocked":
        return "Not completed"
    if key in {"outcome", "status"} and rendered == "blocked_terminal":
        return "Not completed (reviewed capability gap)"
    if key == "validation" and rendered == "partial":
        return "Partial compliance — not a verdict"
    if key == "blocked":
        return rendered
    return common.get(rendered, rendered.replace("_", " ") if key == "kind" else rendered)


def _display(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, default=str)
    return "" if value is None else str(value)


def _csv_bundle(value: CampaignReportProjection) -> bytes:
    cost_details = _recorded_cost_details(value)
    target_progress = _target_progress(value)
    techniques = tuple(
        {
            **dict(row),
            "canonical_outcome": _canonical_outcome(row),
            "display_name": _human_technique_name(row),
        }
        for row in _ledger_candidate_rows(value)
    )
    tables: dict[str, Sequence[Mapping[str, Any]]] = {
        "summary.csv": (
            {
                **dict(value.metadata),
                **_coverage(value),
                "campaign_id": value.campaign_id,
                "revision_id": value.revision_id,
                "revision": value.revision,
                "digest": value.digest,
                "completion_digest": value.completion_digest,
                "source_seal_state": value.source_seal_state,
                "audience": value.audience,
                "created_at": value.created_at.isoformat(),
                "state": value.report_state,
                "stop_reason": value.stop_reason,
                "cleanup_status": value.cleanup_status,
                "usage": dict(value.usage),
                **(
                    {
                        "recorded_estimated_model_cost": cost_details[0],
                        "pricing_source": cost_details[1],
                        "pricing_source_url": cost_details[2],
                    }
                    if cost_details is not None
                    else {}
                ),
            },
        ),
        "findings.csv": value.findings,
        "techniques.csv": techniques,
        "evidence.csv": tuple(asdict(row) for row in value.evidence),
        "exchanges.csv": value.exchanges,
        "lineage.csv": value.lineage,
        "language-coverage.csv": _language_coverage(value),
        "cross-campaign-learning.csv": tuple(value.metadata.get("cross_campaign_learning", ())),
        "unsearched-frontier.csv": value.unsearched_frontier,
        "limitations.csv": tuple(
            {"limitation": item} for item in _dedupe_limitations(value.limitations)
        ),
    }
    schemas = {
        "summary.csv": (
            "campaign_id",
            "revision_id",
            "revision",
            "digest",
            "completion_digest",
            "source_seal_state",
            "audience",
            "created_at",
            "state",
            "stop_reason",
            "cleanup_status",
            "planned",
            "attempted",
            "confirmed",
            "negative",
            "partial",
            "unclear",
            "blocked",
            "untested",
            "retryable_infrastructure",
            "physically_unreachable",
            "unresolved",
            "title",
            "objective",
            "verdict",
            "confidence",
            "profile",
            "usage",
            "recorded_estimated_model_cost",
            "pricing_source",
            "pricing_source_url",
        ),
        "findings.csv": ("id", "title", "summary", "severity", "validation"),
        "techniques.csv": (
            "id",
            "name",
            "display_name",
            "outcome",
            "canonical_outcome",
            "rationale",
            "independent_reproduction",
            "language",
            "requested_language",
            "actual_language",
            "language_evidence_status",
            "language_evidence_source",
            "planned_language",
            "observed_language",
            "language_verification",
            "language_confidence",
            "transform_id",
            "semantic_id",
            "strategy_id",
            "candidate_id",
            "controller_id",
            "payload_family",
            "language_id",
            "mutation_id",
            "ledger_source",
            "security_outcome",
            "evaluation_state",
            "lifecycle_state",
            "evaluation_uncertain",
        ),
        "evidence.csv": ("reference", "finding_id", "witness"),
        "exchanges.csv": ("id", "request", "response", "outcome"),
        "lineage.csv": (
            "pack",
            "entry",
            "technique",
            "semantic_technique",
            "stage",
            "attempt",
            "exchange",
            "status",
        ),
        "language-coverage.csv": (
            "language",
            "variant",
            "planned_languages",
            "actual_language_recorded",
            "language_evidence_sources",
            "language_mismatches",
            "language_unverified",
            "planned",
            "attempted",
            "confirmed",
            "negative",
            "partial",
            "unclear",
            "blocked",
            "untested",
            "retryable_infrastructure",
            "physically_unreachable",
            "unresolved",
        ),
        "cross-campaign-learning.csv": (
            "technique_id",
            "decision",
            "attempt_identity",
            "evidence_digest",
        ),
        "unsearched-frontier.csv": ("id", "kind", "surface", "oracle", "reason"),
        "limitations.csv": ("limitation",),
    }
    exploration = _exploration_metrics(value)
    if exploration:
        tables["exploration-metrics.csv"] = exploration
        schemas["exploration-metrics.csv"] = ("metric", "value")
    if target_progress is not None:
        fields = _target_progress_fields(target_progress)
        tables["target-progress.csv"] = tuple(
            {
                "name": field.get("name"),
                "label": field.get("label"),
                "role": field.get("role"),
                "value": field.get("value"),
                "minimum_value": field.get("minimum_value"),
                "maximum_value": field.get("maximum_value"),
                "trust_class": field.get("trust_class"),
                "observed_at": field.get("observed_at"),
                "target_exchange_count": target_progress.get("target_exchange_count"),
            }
            for field in fields
        ) or (
            {
                "name": "target_state",
                "label": "Target state",
                "value": "not discovered",
                "target_exchange_count": target_progress.get("target_exchange_count"),
            },
        )
        schemas["target-progress.csv"] = (
            "name",
            "label",
            "role",
            "value",
            "minimum_value",
            "maximum_value",
            "trust_class",
            "observed_at",
            "target_exchange_count",
        )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, rows in tables.items():
            buffer = io.StringIO(newline="")
            keys = schemas[name]
            writer = csv.DictWriter(buffer, fieldnames=keys, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _csv_cell(row.get(key)) for key in keys})
            member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = 0o100644 << 16
            archive.writestr(member, buffer.getvalue())
    return output.getvalue()


def _csv_cell(value: Any) -> str:
    rendered = _display(value)
    if rendered.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + rendered
    return rendered


def _document(value: CampaignReportProjection) -> dict[str, Any]:
    document = {
        "revision_id": value.revision_id,
        "campaign_id": value.campaign_id,
        "revision": value.revision,
        "stop_reason": value.stop_reason,
        "cleanup_status": value.cleanup_status,
        "findings": list(value.findings),
        "tasks": list(value.tasks),
        "evidence": [asdict(citation) for citation in value.evidence],
        "usage": dict(value.usage),
        "created_at": value.created_at.isoformat(),
        "digest": value.digest,
        "audience": value.audience,
        "report_state": value.report_state,
        "metadata": dict(value.metadata),
        "coverage": dict(value.coverage),
        "language_coverage": list(_language_coverage(value)),
        "techniques": list(value.techniques),
        "exchanges": list(value.exchanges),
        "lineage": list(value.lineage),
        "unsearched_frontier": list(value.unsearched_frontier),
        "limitations": list(value.limitations),
        "completion_digest": value.completion_digest,
        "source_seal_state": value.source_seal_state,
        "digest_schema": value.digest_schema,
    }
    if value.hypotheses:
        document["hypotheses"] = list(value.hypotheses)
    exploration = _exploration_metrics(value)
    if exploration:
        document["exploration_metrics"] = list(exploration)
    return document


def _source_document(value: CampaignReportProjection) -> dict[str, Any]:
    """Rebuild the exact immutable source payload without exporting the raw seal."""
    document = {
        "campaign_id": value.campaign_id,
        "revision": value.revision,
        "stop_reason": value.stop_reason,
        "cleanup_status": value.cleanup_status,
        "findings": list(value.findings),
        "tasks": list(value.tasks),
        "evidence": [asdict(citation) for citation in value.evidence],
        "usage": dict(value.usage),
        "created_at": value.created_at.isoformat(),
        "audience": value.audience,
        "report_state": value.report_state,
        "metadata": dict(value.metadata),
        "coverage": dict(value.coverage),
        "techniques": list(value.techniques),
        "exchanges": list(value.exchanges),
        "lineage": list(value.lineage),
        "unsearched_frontier": list(value.unsearched_frontier),
        "limitations": list(value.limitations),
        "completion_digest": value.completion_digest,
        "source_seal_state": value.source_seal_state,
        "digest_schema": value.digest_schema,
    }
    if value.hypotheses:
        document["hypotheses"] = list(value.hypotheses)
    return document


def _legacy_source_document(value: CampaignReportProjection) -> dict[str, Any]:
    document = _source_document(value)
    for key in ("completion_digest", "source_seal_state", "digest_schema"):
        document.pop(key)
    return document


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _diff(old: object, new: object, path: str = "$") -> list[str]:
    if isinstance(old, dict) and isinstance(new, dict):
        rows = []
        for key in sorted(set(old) | set(new)):
            rows.extend(_diff(old.get(key), new.get(key), f"{path}.{key}"))
        return rows
    if old != new:
        return [f"{path}: {json.dumps(old, default=str)} -> {json.dumps(new, default=str)}"]
    return []
