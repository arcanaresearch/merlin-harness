from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from merlin_contracts import (
    EvidenceWitness,
    Finding,
    FindingDraft,
    FindingStatus,
    FindingValidation,
    PartialComplianceSignal,
    ReportAudience,
    ReportFormat,
    ReportRequest,
    ReportResult,
)

from .reporting import export_report


class FindingStore(Protocol):
    async def create(self, session_id: str, draft: FindingDraft) -> Finding: ...
    async def list(
        self, session_id: str, *, status: FindingStatus | None = None, limit: int = 100
    ) -> Sequence[Finding]: ...
    async def set_disposition(self, finding_id: str, disposition: str) -> Finding: ...
    async def set_validation(
        self,
        finding_id: str,
        validation: FindingValidation,
        summary: str,
        witnesses: Sequence[EvidenceWitness],
        partial_signal: PartialComplianceSignal | None = None,
    ) -> Finding: ...
    async def set_remediation(self, finding_id: str, remediation: str) -> Finding: ...


class FindingError(ValueError):
    pass


class FindingNotFoundError(FindingError):
    pass


class FindingTransitionError(FindingError):
    pass


class FindingService:
    """Kernel-facing lifecycle and accepted-only report service."""

    def __init__(
        self,
        store: FindingStore,
        *,
        workspace_root: Path,
        campaign_report_exporter: (
            Callable[
                [str, ReportFormat, ReportAudience],
                str | bytes | tuple[str | bytes, int] | None,
            ]
            | None
        ) = None,
        campaign_report_comparator: Callable[[str, str], Sequence[str]] | None = None,
        validator_evidence_verifier: Callable[[EvidenceWitness, str, str], bool] | None = None,
        comparison_receipt_requirement: Callable[[Finding], str | None] | None = None,
        comparison_receipt_verifier: Callable[[EvidenceWitness, str, str], bool] | None = None,
    ) -> None:
        self._store = store
        self._workspace_root = workspace_root.resolve()
        self._campaign_report_exporter = campaign_report_exporter
        self._campaign_report_comparator = campaign_report_comparator
        self._validator_evidence_verifier = validator_evidence_verifier
        self._comparison_receipt_requirement = comparison_receipt_requirement
        self._comparison_receipt_verifier = comparison_receipt_verifier

    async def create_draft(self, session_id: str, draft: FindingDraft) -> Finding:
        return await self._store.create(session_id, draft)

    async def list_findings(self, session_id: str) -> tuple[Finding, ...]:
        return tuple(await self._store.list(session_id))

    async def list_findings_for_completion(self, session_id: str) -> tuple[Finding, ...]:
        """Read an unbounded finding set; fail closed if the store lacks authority."""
        loader = getattr(self._store, "list_all", None)
        if loader is None:
            raise RuntimeError("finding store lacks an unbounded completion query")
        value = loader(session_id)
        if asyncio.iscoroutine(value):
            value = await value
        return tuple(value)

    async def accept_finding(self, session_id: str, finding_id: str) -> Finding:
        return await self._disposition(session_id, finding_id, "accepted")

    async def reject_finding(self, session_id: str, finding_id: str) -> Finding:
        return await self._disposition(session_id, finding_id, "rejected")

    async def validate_finding(
        self,
        session_id: str,
        finding_id: str,
        *,
        summary: str,
        witnesses: Sequence[EvidenceWitness] = (),
    ) -> Finding:
        return await self._validation(session_id, finding_id, "validated", summary, witnesses)

    async def refute_finding(
        self,
        session_id: str,
        finding_id: str,
        *,
        summary: str,
        witnesses: Sequence[EvidenceWitness] = (),
    ) -> Finding:
        return await self._validation(session_id, finding_id, "refuted", summary, witnesses)

    async def mark_inconclusive(
        self,
        session_id: str,
        finding_id: str,
        *,
        summary: str,
        witnesses: Sequence[EvidenceWitness] = (),
    ) -> Finding:
        return await self._validation(session_id, finding_id, "inconclusive", summary, witnesses)

    async def mark_partial(
        self,
        session_id: str,
        finding_id: str,
        *,
        summary: str,
        signal: PartialComplianceSignal,
        witnesses: Sequence[EvidenceWitness] = (),
    ) -> Finding:
        """Record graded partial compliance: observed progress plus refusal.

        Partial is its own terminal outcome, not a verdict — it never satisfies
        the validated/refuted witness gates and never unlocks remediation.
        """
        return await self._validation(
            session_id, finding_id, "partial", summary, witnesses, signal=signal
        )

    async def recommend_remediation(
        self, session_id: str, finding_id: str, *, remediation: str
    ) -> Finding:
        if not remediation.strip():
            raise FindingError("remediation must not be empty")
        findings = await self._store.list(session_id)
        finding = next((item for item in findings if item.id == finding_id), None)
        if finding is None:
            raise FindingNotFoundError(f"unknown finding {finding_id!r} in this session")
        if finding.validation != "validated":
            raise FindingTransitionError("remediation recommendations require a validated finding")
        return await self._store.set_remediation(finding_id, remediation)

    async def generate_report(self, request: ReportRequest) -> ReportResult:
        findings = tuple(await self._store.list(request.session_id, status="accepted"))
        content = None
        if self._campaign_report_exporter is not None:
            content = await asyncio.to_thread(
                self._campaign_report_exporter,
                request.session_id,
                request.format,
                request.audience,
            )
        finding_count = len(findings)
        if isinstance(content, tuple):
            content, finding_count = content
        if self._campaign_report_exporter is not None and content is None:
            raise FindingError("no durable campaign report exists for this session")
        if content is None:
            content = export_report(findings, request.format, request.audience)
        output = self._resolve_output(request.output)
        await asyncio.to_thread(_atomic_write, output, content)
        return ReportResult(format=request.format, output=output, finding_count=finding_count)

    async def compare_reports(self, older_id: str, newer_id: str) -> tuple[str, ...]:
        if self._campaign_report_comparator is None:
            raise FindingError("durable report comparison is not configured")
        return tuple(await asyncio.to_thread(self._campaign_report_comparator, older_id, newer_id))

    async def _disposition(self, session_id: str, finding_id: str, disposition: str) -> Finding:
        findings = await self._store.list(session_id)
        finding = next((item for item in findings if item.id == finding_id), None)
        if finding is None:
            raise FindingNotFoundError(f"unknown finding {finding_id!r} in this session")
        if finding.status != "draft":
            raise FindingTransitionError(
                f"finding {finding_id!r} cannot transition from {finding.status}"
            )
        try:
            return await self._store.set_disposition(finding_id, disposition)
        except ValueError as exc:
            raise FindingTransitionError(str(exc)) from exc

    async def _validation(
        self,
        session_id: str,
        finding_id: str,
        validation: FindingValidation,
        summary: str,
        witnesses: Sequence[EvidenceWitness],
        signal: PartialComplianceSignal | None = None,
    ) -> Finding:
        if not summary.strip():
            raise FindingError("validation summary must not be empty")
        if validation == "partial" and signal is None:
            raise FindingError("partial findings require a partial compliance signal")
        if validation != "partial" and signal is not None:
            raise FindingError("a partial compliance signal is only valid for partial findings")
        findings = await self._store.list(session_id)
        finding = next((item for item in findings if item.id == finding_id), None)
        if finding is None:
            raise FindingNotFoundError(f"unknown finding {finding_id!r} in this session")
        if finding.validation != "unvalidated":
            raise FindingTransitionError(
                f"finding {finding_id!r} cannot transition from {finding.validation}"
            )
        if validation in {"validated", "refuted"}:
            if not witnesses:
                raise FindingError(f"{validation} findings require an evidence witness")
            evidence = set(finding.evidence_references)
            external = [witness for witness in witnesses if witness.reference not in evidence]
            if any(
                not _attributable_validator_evidence(
                    witness,
                    self._validator_evidence_verifier,
                    session_id=session_id,
                    target_id=finding.target_id,
                )
                and not _sealed_comparison_receipt(
                    witness,
                    self._comparison_receipt_verifier,
                    session_id=session_id,
                )
                for witness in external
            ):
                raise FindingError("validation witness refers to unattributable external evidence")
            campaign_witnesses = [
                witness
                for witness in witnesses
                if witness.source.startswith("independent-validator:")
            ]
            if campaign_witnesses and not all(
                _attributable_validator_evidence(
                    witness,
                    self._validator_evidence_verifier,
                    session_id=session_id,
                    target_id=finding.target_id,
                )
                for witness in campaign_witnesses
            ):
                raise FindingError(
                    "independent reproduction requires attributable validator-owned evidence"
                )
            required_pair = (
                self._comparison_receipt_requirement(finding)
                if self._comparison_receipt_requirement is not None
                else None
            )
            if required_pair is not None and not any(
                _sealed_comparison_receipt(
                    witness,
                    self._comparison_receipt_verifier,
                    session_id=session_id,
                    pair_id=required_pair,
                )
                for witness in witnesses
            ):
                raise FindingError(
                    "paired treatment findings require the sealed treatment/control "
                    "comparison receipt for an independent verdict"
                )
        if validation == "validated" and not any(
            witness.source != finding.creator for witness in witnesses
        ):
            raise FindingError("validated findings require an independent reproduction witness")
        try:
            if validation == "partial":
                try:
                    return await self._store.set_validation(
                        finding_id,
                        validation,
                        summary,
                        tuple(witnesses),
                        partial_signal=signal,
                    )
                except TypeError as exc:
                    raise FindingError(
                        "finding store cannot persist partial compliance outcomes"
                    ) from exc
            return await self._store.set_validation(
                finding_id, validation, summary, tuple(witnesses)
            )
        except ValueError as exc:
            raise FindingTransitionError(str(exc)) from exc

    def _resolve_output(self, requested: Path) -> Path:
        output = requested if requested.is_absolute() else self._workspace_root / requested
        resolved = output.resolve()
        if not resolved.is_relative_to(self._workspace_root):
            raise FindingError("report output must stay within the workspace")
        return resolved


def _atomic_write(output: Path, content: str | bytes) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        mode = "wb" if isinstance(content, bytes) else "w"
        kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8"}
        with os.fdopen(descriptor, mode, **kwargs) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _attributable_validator_evidence(
    witness: EvidenceWitness,
    verifier: Callable[[EvidenceWitness, str, str], bool] | None,
    *,
    session_id: str,
    target_id: str,
) -> bool:
    if not witness.source.startswith("independent-validator:"):
        return False
    parsed = urlsplit(witness.reference)
    if parsed.scheme not in {"merlin-result", "merlin-effect", "merlin-exchange"}:
        return False
    return (
        parse_qs(parsed.query).get("digest") == [witness.digest]
        and verifier is not None
        and verifier(witness, session_id, target_id)
    )


def _sealed_comparison_receipt(
    witness: EvidenceWitness,
    verifier: Callable[[EvidenceWitness, str, str], bool] | None,
    *,
    session_id: str,
    pair_id: str | None = None,
) -> bool:
    parsed = urlsplit(witness.reference)
    if parsed.scheme != "merlin-comparison":
        return False
    values = parse_qs(parsed.query)
    pairs = values.get("pair_id")
    if pairs is None or len(pairs) != 1 or (pair_id is not None and pairs[0] != pair_id):
        return False
    return (
        values.get("digest") == [witness.digest]
        and verifier is not None
        and verifier(witness, session_id, pairs[0])
    )
