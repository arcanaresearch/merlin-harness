import json
from pathlib import Path

import pytest
from merlin_contracts import (
    EvidenceWitness,
    Finding,
    FindingDraft,
    PartialComplianceSignal,
    ReportRequest,
)
from merlin_findings import FindingError, FindingService


class MemoryStore:
    def __init__(self) -> None:
        self.items: dict[str, tuple[str, Finding]] = {}

    async def create(self, session_id: str, draft: FindingDraft) -> Finding:
        finding = Finding(id=f"f{len(self.items) + 1}", **draft.model_dump())
        self.items[finding.id] = (session_id, finding)
        return finding

    async def list(self, session_id: str, *, status=None, limit=100):
        values = [finding for owner, finding in self.items.values() if owner == session_id]
        return [finding for finding in values if status is None or finding.status == status][:limit]

    async def set_disposition(self, finding_id: str, disposition: str) -> Finding:
        session_id, finding = self.items[finding_id]
        updated = finding.model_copy(update={"status": disposition})
        self.items[finding_id] = (session_id, updated)
        return updated

    async def set_validation(self, finding_id, validation, summary, witnesses, partial_signal=None):
        session_id, finding = self.items[finding_id]
        updated = finding.model_copy(
            update={
                "validation": validation,
                "validation_summary": summary,
                "witnesses": list(witnesses),
                "partial_signal": partial_signal,
                "evidence_references": list(
                    dict.fromkeys(
                        (*finding.evidence_references, *(item.reference for item in witnesses))
                    )
                ),
            }
        )
        self.items[finding_id] = (session_id, updated)
        return updated

    async def set_remediation(self, finding_id, remediation):
        session_id, finding = self.items[finding_id]
        updated = finding.model_copy(update={"remediation": remediation})
        self.items[finding_id] = (session_id, updated)
        return updated


def draft(title: str) -> FindingDraft:
    return FindingDraft(
        title=title,
        severity="high",
        summary="A security control can be bypassed.",
        reproduction="Send the captured request.",
        remediation="Enforce the control.",
        creator="worker-auth",
        evidence_references=["evidence://run-1/turn-4"],
    )


@pytest.mark.asyncio
async def test_kernel_lifecycle_and_accepted_only_report_parity(tmp_path: Path) -> None:
    service = FindingService(MemoryStore(), workspace_root=tmp_path)
    accepted = await service.create_draft("session", draft("Accepted issue"))
    await service.accept_finding("session", accepted.id)
    await service.create_draft("session", draft("Draft issue"))
    rejected = await service.create_draft("session", draft("Rejected issue"))
    await service.reject_finding("session", rejected.id)

    json_result = await service.generate_report(
        ReportRequest(session_id="session", format="json", output=Path("reports/report.json"))
    )
    markdown_result = await service.generate_report(
        ReportRequest(session_id="session", format="markdown", output=Path("reports/report.md"))
    )
    payload = json.loads(json_result.output.read_text())
    markdown = markdown_result.output.read_text()

    assert json_result.finding_count == markdown_result.finding_count == 1
    assert [item["title"] for item in payload["findings"]] == ["Accepted issue"]
    for value in payload["findings"][0].values():
        if isinstance(value, str):
            assert value in markdown
    assert "evidence://run-1/turn-4" in markdown
    assert "Draft issue" not in markdown and "Rejected issue" not in markdown


@pytest.mark.asyncio
async def test_markdown_report_neutralizes_hostile_finding_content(tmp_path: Path) -> None:
    service = FindingService(MemoryStore(), workspace_root=tmp_path)
    hostile = FindingDraft(
        title="# [Injected](javascript:alert(1)) <img src=x>",
        severity="high",
        summary="**not emphasis** <script>alert(2)</script>",
        reproduction="- forged list\n1. forged sequence",
        remediation="> forged quote with an ordinary-hyphen",
        creator="worker-[link](javascript:alert(3))",
        evidence_references=["[evidence](javascript:alert(4))"],
    )
    finding = await service.create_draft("session", hostile)
    await service.accept_finding("session", finding.id)

    result = await service.generate_report(
        ReportRequest(session_id="session", format="markdown", output=Path("reports/safe.md"))
    )
    rendered = result.output.read_text()

    assert "<script>" not in rendered
    assert "<img" not in rendered
    assert "](javascript:" not in rendered
    assert r"## \# \[Injected\]\(javascript:alert\(1\)\) &lt;img src=x&gt;" in rendered
    assert r"\*\*not emphasis\*\* &lt;script&gt;alert\(2\)&lt;/script&gt;" in rendered
    assert r"\- forged list<br>1\. forged sequence" in rendered
    assert "&gt; forged quote with an ordinary-hyphen" in rendered


@pytest.mark.asyncio
async def test_report_output_cannot_escape_workspace(tmp_path: Path) -> None:
    service = FindingService(MemoryStore(), workspace_root=tmp_path)
    with pytest.raises(FindingError, match="within the workspace"):
        await service.generate_report(
            ReportRequest(session_id="session", format="json", output=Path("../escape.json"))
        )


@pytest.mark.asyncio
async def test_customer_fallback_report_omits_operational_reproduction(tmp_path: Path) -> None:
    service = FindingService(MemoryStore(), workspace_root=tmp_path)
    finding = await service.create_draft("session", draft("Customer issue"))
    await service.accept_finding("session", finding.id)

    result = await service.generate_report(
        ReportRequest(
            session_id="session",
            format="markdown",
            audience="customer",
            output=Path("reports/customer.md"),
        )
    )
    content = result.output.read_text()
    assert "Send the captured request." not in content
    assert "worker-auth" not in content
    assert "evidence://run-1/turn-4" not in content
    assert "Reproduction details are available in the internal report." in content


@pytest.mark.asyncio
async def test_durable_campaign_projection_is_preferred_over_findings_fallback(
    tmp_path: Path,
) -> None:
    calls = []

    def export_campaign(session_id, format, audience):
        calls.append((session_id, format, audience))
        return "<html><body>durable campaign report</body></html>", 17

    service = FindingService(
        MemoryStore(), workspace_root=tmp_path, campaign_report_exporter=export_campaign
    )
    result = await service.generate_report(ReportRequest(session_id="session"))

    assert calls == [("session", "html", "internal")]
    assert "durable campaign report" in result.output.read_text()
    assert result.finding_count == 17


@pytest.mark.asyncio
async def test_configured_durable_report_export_never_silently_flattens_findings(
    tmp_path: Path,
) -> None:
    service = FindingService(
        MemoryStore(),
        workspace_root=tmp_path,
        campaign_report_exporter=lambda *_args: None,
    )
    with pytest.raises(FindingError, match="no durable campaign report"):
        await service.generate_report(ReportRequest(session_id="session"))


@pytest.mark.asyncio
async def test_durable_campaign_revisions_can_be_compared(tmp_path: Path) -> None:
    service = FindingService(
        MemoryStore(),
        workspace_root=tmp_path,
        campaign_report_comparator=lambda older, newer: (f"{older} -> {newer}",),
    )

    assert await service.compare_reports("revision-1", "revision-2") == (
        "revision-1 -> revision-2",
    )


@pytest.mark.asyncio
async def test_validation_is_attributed_and_terminal(tmp_path: Path) -> None:
    service = FindingService(
        MemoryStore(),
        workspace_root=tmp_path,
        validator_evidence_verifier=lambda _item, _session, _target: True,
    )
    finding = await service.create_draft("session", draft("Validated issue"))
    witness = EvidenceWitness(
        reference="merlin-exchange://validator/replay-1?digest=sha256:" + "a" * 64,
        digest="sha256:" + "a" * 64,
        source="independent-validator:schedule:attempt:digest",
    )

    validated = await service.validate_finding(
        "session", finding.id, summary="Reproduced independently.", witnesses=[witness]
    )

    assert validated.validation == "validated"
    assert validated.witnesses == [witness]
    recommended = await service.recommend_remediation(
        "session",
        finding.id,
        remediation="[recommended]\n\nBind authorization to authoritative records.",
    )
    assert recommended.remediation.startswith("[recommended]")
    with pytest.raises(Exception, match="cannot transition"):
        await service.refute_finding("session", finding.id, summary="late contradiction")


@pytest.mark.asyncio
async def test_inconclusive_validation_preserves_unknown_without_witness(tmp_path: Path) -> None:
    service = FindingService(MemoryStore(), workspace_root=tmp_path)
    finding = await service.create_draft("session", draft("Incomplete reproduction"))

    updated = await service.mark_inconclusive(
        "session",
        finding.id,
        summary="Target session ended before reproduction completed.",
    )

    assert updated.validation == "inconclusive"
    assert updated.validation_summary == "Target session ended before reproduction completed."
    with pytest.raises(Exception, match="require a validated finding"):
        await service.recommend_remediation(
            "session", finding.id, remediation="Do not publish this as verified."
        )


@pytest.mark.asyncio
async def test_validation_requires_owned_independent_evidence(tmp_path: Path) -> None:
    service = FindingService(MemoryStore(), workspace_root=tmp_path)
    finding = await service.create_draft("session", draft("Evidence ownership"))

    with pytest.raises(FindingError, match="require an evidence witness"):
        await service.validate_finding("session", finding.id, summary="unsupported")
    with pytest.raises(FindingError, match="attributable validator-owned evidence"):
        await service.validate_finding(
            "session",
            finding.id,
            summary="reviewed source evidence only",
            witnesses=[
                EvidenceWitness(
                    reference="evidence://run-1/turn-4",
                    digest="sha256:" + "b" * 64,
                    source="independent-validator:schedule:attempt:digest",
                )
            ],
        )


@pytest.mark.asyncio
async def test_validator_verifier_receives_finding_session_and_target(tmp_path: Path) -> None:
    calls: list[tuple[str, str | None]] = []

    def verify(_witness, session_id, target_id):
        calls.append((session_id, target_id))
        return session_id == "session" and target_id == "target-a"

    service = FindingService(
        MemoryStore(), workspace_root=tmp_path, validator_evidence_verifier=verify
    )
    finding = await service.create_draft(
        "session", draft("Bound evidence").model_copy(update={"target_id": "target-a"})
    )
    witness = EvidenceWitness(
        reference="merlin-exchange://validator/replay-1?digest=sha256:" + "a" * 64,
        digest="sha256:" + "a" * 64,
        source="independent-validator:schedule:attempt:digest",
    )

    updated = await service.validate_finding(
        "session", finding.id, summary="Reproduced.", witnesses=[witness]
    )

    assert updated.validation == "validated"
    assert calls == [("session", "target-a"), ("session", "target-a")]


@pytest.mark.asyncio
async def test_refutation_cannot_reuse_unverified_preseeded_validator_reference(
    tmp_path: Path,
) -> None:
    witness = EvidenceWitness(
        reference="merlin-exchange://validator/forged?digest=sha256:" + "b" * 64,
        digest="sha256:" + "b" * 64,
        source="independent-validator:schedule:attempt:digest",
    )
    seeded = draft("Forged refutation").model_copy(
        update={"evidence_references": ["evidence://run-1/turn-4", witness.reference]}
    )
    service = FindingService(
        MemoryStore(),
        workspace_root=tmp_path,
        validator_evidence_verifier=lambda _item, _session, _target: False,
    )
    finding = await service.create_draft("session", seeded)

    with pytest.raises(FindingError, match="attributable validator-owned evidence"):
        await service.refute_finding(
            "session", finding.id, summary="Claim refuted.", witnesses=[witness]
        )


@pytest.mark.asyncio
async def test_well_formed_but_unverified_validator_uri_is_rejected(tmp_path: Path) -> None:
    service = FindingService(MemoryStore(), workspace_root=tmp_path)
    finding = await service.create_draft("session", draft("Forged validator evidence"))
    digest = "sha256:" + "f" * 64

    with pytest.raises(FindingError, match="unattributable external evidence"):
        await service.validate_finding(
            "session",
            finding.id,
            summary="Forged reproduction.",
            witnesses=[
                EvidenceWitness(
                    reference=f"merlin-result://validator?digest={digest}",
                    digest=digest,
                    source="independent-validator:fake:attempt:digest",
                )
            ],
        )
    with pytest.raises(FindingError, match="unattributable external evidence"):
        await service.validate_finding(
            "session",
            finding.id,
            summary="wrong evidence",
            witnesses=[
                EvidenceWitness(
                    reference="evidence://other",
                    digest="sha256:" + "c" * 64,
                    source="independent-validator",
                )
            ],
        )
    with pytest.raises(FindingError, match="independent reproduction"):
        await service.validate_finding(
            "session",
            finding.id,
            summary="self-attested",
            witnesses=[
                EvidenceWitness(
                    reference="evidence://run-1/turn-4",
                    digest="sha256:" + "c" * 64,
                    source="worker-auth",
                )
            ],
        )


def _paired_service(tmp_path: Path, *, verifier_result: bool = True) -> FindingService:
    return FindingService(
        MemoryStore(),
        workspace_root=tmp_path,
        validator_evidence_verifier=lambda _witness, _session, _target: True,
        comparison_receipt_requirement=lambda finding: (
            "cat/direct/benign-greeting"
            if finding.technique_id == "cat/direct/override-baseline"
            else None
        ),
        comparison_receipt_verifier=lambda _witness, _session, _pair: verifier_result,
    )


def _comparison_witness(pair_id: str, digest: str) -> EvidenceWitness:
    return EvidenceWitness(
        reference=(f"merlin-comparison://baseline/agenda-1?pair_id={pair_id}&digest={digest}"),
        digest=digest,
        source="comparison-receipt:baseline:schedule-1",
    )


@pytest.mark.asyncio
async def test_paired_finding_verdict_requires_sealed_comparison_receipt(
    tmp_path: Path,
) -> None:
    service = _paired_service(tmp_path)
    paired = draft("Paired treatment").model_copy(
        update={"technique_id": "cat/direct/override-baseline"}
    )
    finding = await service.create_draft("session", paired)
    validator_witness = EvidenceWitness(
        reference="merlin-exchange://validator/replay-1?digest=sha256:" + "a" * 64,
        digest="sha256:" + "a" * 64,
        source="independent-validator:schedule:attempt:digest",
    )

    with pytest.raises(FindingError, match="comparison receipt"):
        await service.validate_finding(
            "session", finding.id, summary="Reproduced.", witnesses=[validator_witness]
        )
    with pytest.raises(FindingError, match="comparison receipt"):
        await service.refute_finding(
            "session", finding.id, summary="Refuted.", witnesses=[validator_witness]
        )
    assert (await service.list_findings("session"))[0].validation == "unvalidated"


@pytest.mark.asyncio
async def test_paired_finding_verdict_passes_with_sealed_comparison_receipt(
    tmp_path: Path,
) -> None:
    service = _paired_service(tmp_path)
    paired = draft("Paired treatment").model_copy(
        update={"technique_id": "cat/direct/override-baseline"}
    )
    finding = await service.create_draft("session", paired)
    receipt_digest = "sha256:" + "d" * 64
    witnesses = [
        EvidenceWitness(
            reference="merlin-exchange://validator/replay-1?digest=sha256:" + "a" * 64,
            digest="sha256:" + "a" * 64,
            source="independent-validator:schedule:attempt:digest",
        ),
        _comparison_witness("cat/direct/benign-greeting", receipt_digest),
    ]

    updated = await service.validate_finding(
        "session", finding.id, summary="Reproduced.", witnesses=witnesses
    )

    assert updated.validation == "validated"


@pytest.mark.asyncio
async def test_paired_finding_rejects_receipt_for_wrong_pair_or_unverified_seal(
    tmp_path: Path,
) -> None:
    service = _paired_service(tmp_path)
    paired = draft("Paired treatment").model_copy(
        update={"technique_id": "cat/direct/override-baseline"}
    )
    finding = await service.create_draft("session", paired)
    wrong_pair = [
        _comparison_witness("cat/indirect/benign-doc-summary", "sha256:" + "d" * 64),
    ]
    with pytest.raises(FindingError, match="comparison receipt"):
        await service.validate_finding(
            "session", finding.id, summary="Reproduced.", witnesses=wrong_pair
        )

    unverified = _paired_service(tmp_path, verifier_result=False)
    receipt = _comparison_witness("cat/direct/benign-greeting", "sha256:" + "d" * 64)
    seeded = paired.model_copy(
        update={"evidence_references": ["evidence://run-1/turn-4", receipt.reference]}
    )
    forged = await unverified.create_draft("session", seeded)
    with pytest.raises(FindingError, match="comparison receipt"):
        await unverified.validate_finding(
            "session",
            forged.id,
            summary="Reproduced.",
            witnesses=[receipt],
        )


@pytest.mark.asyncio
async def test_unpaired_finding_verdict_does_not_require_comparison_receipt(
    tmp_path: Path,
) -> None:
    service = _paired_service(tmp_path)
    finding = await service.create_draft("session", draft("Unpaired technique"))

    updated = await service.validate_finding(
        "session",
        finding.id,
        summary="Reproduced.",
        witnesses=[
            EvidenceWitness(
                reference="merlin-exchange://validator/replay-1?digest=sha256:" + "a" * 64,
                digest="sha256:" + "a" * 64,
                source="independent-validator:schedule:attempt:digest",
            )
        ],
    )

    assert updated.validation == "validated"


@pytest.mark.asyncio
async def test_paired_finding_inconclusive_does_not_require_comparison_receipt(
    tmp_path: Path,
) -> None:
    service = _paired_service(tmp_path)
    paired = draft("Paired treatment").model_copy(
        update={"technique_id": "cat/direct/override-baseline"}
    )
    finding = await service.create_draft("session", paired)

    updated = await service.mark_inconclusive(
        "session", finding.id, summary="Reproduction was blocked."
    )

    assert updated.validation == "inconclusive"


def partial_signal() -> PartialComplianceSignal:
    return PartialComplianceSignal(
        observed="Bot disclosed its system-prompt structure and first instruction line.",
        refused="Bot refused to reveal the full system prompt verbatim.",
        evidence_refs=("evidence://run-1/turn-4",),
        graded_by="independent-validator:review",
    )


@pytest.mark.asyncio
async def test_partial_compliance_is_terminal_but_never_validated(tmp_path: Path) -> None:
    service = FindingService(MemoryStore(), workspace_root=tmp_path)
    finding = await service.create_draft("session", draft("Partial leak"))

    updated = await service.mark_partial(
        "session",
        finding.id,
        summary="Target engaged and leaked structure, then refused the objective.",
        signal=partial_signal(),
    )

    assert updated.validation == "partial"
    assert updated.partial_signal is not None
    assert "refused" in updated.partial_signal.refused
    with pytest.raises(Exception, match="require a validated finding"):
        await service.recommend_remediation(
            "session", finding.id, remediation="Do not publish this as verified."
        )
    with pytest.raises(Exception, match="cannot transition"):
        await service.mark_inconclusive("session", finding.id, summary="late change")


@pytest.mark.asyncio
async def test_partial_compliance_requires_signal_and_summary(tmp_path: Path) -> None:
    service = FindingService(MemoryStore(), workspace_root=tmp_path)
    finding = await service.create_draft("session", draft("Partial leak"))

    with pytest.raises(FindingError, match="partial compliance signal"):
        await service.mark_partial("session", finding.id, summary="x", signal=None)
    with pytest.raises(FindingError, match="must not be empty"):
        await service.mark_partial("session", finding.id, summary="  ", signal=partial_signal())
    assert (await service.list_findings("session"))[0].validation == "unvalidated"


@pytest.mark.asyncio
async def test_partial_compliance_rejected_when_store_cannot_persist(tmp_path: Path) -> None:
    class LegacyStore(MemoryStore):
        async def set_validation(self, finding_id, validation, summary, witnesses):
            return await MemoryStore.set_validation(
                self, finding_id, validation, summary, witnesses
            )

    service = FindingService(LegacyStore(), workspace_root=tmp_path)
    finding = await service.create_draft("session", draft("Partial leak"))

    with pytest.raises(FindingError, match="cannot persist partial compliance"):
        await service.mark_partial(
            "session", finding.id, summary="Engaged then refused.", signal=partial_signal()
        )
    assert (await service.list_findings("session"))[0].validation == "unvalidated"


@pytest.mark.asyncio
async def test_partial_compliance_renders_in_report_and_redacts_for_customers(
    tmp_path: Path,
) -> None:
    service = FindingService(MemoryStore(), workspace_root=tmp_path)
    finding = await service.create_draft("session", draft("Partial leak"))
    await service.mark_partial(
        "session", finding.id, summary="Engaged then refused.", signal=partial_signal()
    )
    await service.accept_finding("session", finding.id)

    internal = await service.generate_report(
        ReportRequest(session_id="session", format="markdown", output=Path("reports/internal.md"))
    )
    content = internal.output.read_text()
    assert "Partial compliance (not a verdict)" in content
    assert "disclosed its system-prompt structure" in content
    assert "refused to reveal the full system prompt" in content

    customer = await service.generate_report(
        ReportRequest(
            session_id="session",
            format="markdown",
            audience="customer",
            output=Path("reports/customer.md"),
        )
    )
    customer_content = customer.output.read_text()
    assert "disclosed its system-prompt structure" not in customer_content
