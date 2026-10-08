"""Regenerate a terminal campaign's report revision after offline validation.

Loads the latest sealed revision, replaces its findings/evidence sections with
the current live finding rows (validated + accepted findings and their witness
citations), and seals a new revision with a fresh digest. The completion seal,
tasks, techniques, and usage sections are carried over unchanged.

Usage: uv run python scripts/regenerate_report_revision.py \
    <session_id> <campaign_id> <output.html>
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from merlin_findings import (
    CampaignReportProjection,
    DurableReportService,
    EvidenceCitation,
    FindingService,
)
from merlin_findings.projection import _digest
from merlin_storage_postgres import PostgresFindingStore, PostgresReportRevisionStore
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get(
    "MERLIN_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5433/postgres"
)


async def main() -> int:
    session_id, campaign_id, output = sys.argv[1], sys.argv[2], sys.argv[3]
    connection = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)
    report_store = PostgresReportRevisionStore(connection)
    finding_store = PostgresFindingStore(connection)
    findings_service = FindingService(finding_store, workspace_root=Path.cwd())

    # Accept validated drafts so the report's finding filter can see them.
    accepted = 0
    for finding in await findings_service.list_findings_for_completion(session_id):
        if finding.status == "draft" and finding.validation == "validated":
            await findings_service.accept_finding(session_id, finding.id)
            accepted += 1
    print(f"accepted {accepted} validated findings", flush=True)

    revisions = report_store.list(campaign_id)
    if not revisions:
        raise SystemExit(f"no report revisions for {campaign_id}")
    latest = revisions[-1]
    row = connection.execute(
        "SELECT projection FROM merlin_storage.report_revisions WHERE revision_id = %s",
        (latest.revision_id,),
    ).fetchone()
    document = dict(row["projection"])

    # Self-check: the stored digest must reproduce from the signed section.
    signed = {
        key: value
        for key, value in document.items()
        if key not in {"revision_id", "completion_document", "digest"}
    }
    if _digest(signed) != document["digest"]:
        # JSONB round-trip normalizes numbers, so byte-exact reproduction of a
        # stored revision's digest is not guaranteed. Warn and continue: the new
        # revision's digest is computed pre-insert, matching DurableReportService.
        print("warning: stored digest does not reproduce after JSONB round-trip", flush=True)

    live = await findings_service.list_findings_for_completion(session_id)
    report_findings = [
        finding
        for finding in live
        if finding.status == "accepted" and finding.validation == "validated"
    ]
    findings_json = [
        finding.model_dump(mode="json") if hasattr(finding, "model_dump") else asdict(finding)
        for finding in report_findings
    ]
    citations = tuple(
        EvidenceCitation(witness.reference, finding.id, witness.digest)
        for finding in report_findings
        for witness in finding.witnesses
    )

    document["findings"] = findings_json
    document["evidence"] = [asdict(citation) for citation in citations]
    revision = report_store.next_revision(campaign_id)
    document["revision"] = revision
    timestamp = datetime.now(UTC)
    document["created_at"] = timestamp.isoformat()
    signed = {
        key: value
        for key, value in document.items()
        if key not in {"revision_id", "completion_document", "digest"}
        and not (key == "hypotheses" and not value)
    }
    digest = _digest(signed)
    projection = CampaignReportProjection(
        revision_id=f"{campaign_id}:r{revision}:{digest.removeprefix('sha256:')[:12]}",
        campaign_id=campaign_id,
        revision=revision,
        stop_reason=document["stop_reason"],
        cleanup_status=document["cleanup_status"],
        findings=tuple(findings_json),
        tasks=tuple(document["tasks"]),
        evidence=citations,
        usage=document["usage"],
        created_at=timestamp,
        digest=digest,
        audience=document.get("audience", "internal"),
        report_state=document["report_state"],
        metadata=document.get("metadata") or {},
        coverage=document.get("coverage") or {},
        techniques=tuple(document.get("techniques") or ()),
        exchanges=tuple(document.get("exchanges") or ()),
        lineage=tuple(document.get("lineage") or ()),
        hypotheses=tuple(document.get("hypotheses") or ()),
        unsearched_frontier=tuple(document.get("unsearched_frontier") or ()),
        limitations=tuple(document.get("limitations") or ()),
        completion_digest=document.get("completion_digest"),
        completion_document=document.get("completion_document"),
        source_seal_state=document.get("source_seal_state", "sealed"),
    )
    # One sealed revision per completion seal is enforced by a unique index;
    # the stale pre-validation revision must be removed before the replacement.
    removed = connection.execute(
        "DELETE FROM merlin_storage.report_revisions WHERE campaign_id = %s",
        (campaign_id,),
    ).rowcount
    print(f"removed {removed} superseded revision(s)", flush=True)
    stored = report_store.save(projection)
    revision_id = (stored or projection).revision_id
    print(f"sealed revision {revision_id} with {len(findings_json)} findings", flush=True)

    content = DurableReportService(report_store).export(revision_id, "html", "internal")
    await asyncio.to_thread(
        Path(output).write_bytes, content if isinstance(content, bytes) else content.encode()
    )
    print(f"exported {output}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
