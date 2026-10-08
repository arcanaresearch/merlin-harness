from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from typing import Any

from merlin_findings import CampaignReportProjection, EvidenceCitation


class PostgresReportRevisionStore:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def next_revision(self, campaign_id: str) -> int:
        row = self.connection.execute(
            """SELECT COALESCE(MAX(revision), 0) + 1 AS revision
               FROM merlin_storage.report_revisions WHERE campaign_id = %s""",
            (campaign_id,),
        ).fetchone()
        return int(row["revision"])

    def save(self, projection: CampaignReportProjection) -> CampaignReportProjection | None:
        document = {
            "revision_id": projection.revision_id,
            "campaign_id": projection.campaign_id,
            "revision": projection.revision,
            "stop_reason": projection.stop_reason,
            "cleanup_status": projection.cleanup_status,
            "findings": list(projection.findings),
            "tasks": list(projection.tasks),
            "evidence": [asdict(citation) for citation in projection.evidence],
            "usage": dict(projection.usage),
            "created_at": projection.created_at.isoformat(),
            "digest": projection.digest,
            "audience": projection.audience,
            "report_state": projection.report_state,
            "metadata": dict(projection.metadata),
            "coverage": dict(projection.coverage),
            "techniques": list(projection.techniques),
            "exchanges": list(projection.exchanges),
            "lineage": list(projection.lineage),
            "hypotheses": list(projection.hypotheses),
            "unsearched_frontier": list(projection.unsearched_frontier),
            "limitations": list(projection.limitations),
            "completion_digest": projection.completion_digest,
            "completion_document": (
                dict(projection.completion_document)
                if projection.completion_document is not None
                else None
            ),
            "source_seal_state": projection.source_seal_state,
            "digest_schema": projection.digest_schema,
        }
        row = self.connection.execute(
            """INSERT INTO merlin_storage.report_revisions
               (revision_id, campaign_id, revision, digest, projection, created_at)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s)
               ON CONFLICT DO NOTHING RETURNING revision_id""",
            (
                projection.revision_id,
                projection.campaign_id,
                projection.revision,
                projection.digest,
                json.dumps(document, default=str),
                projection.created_at,
            ),
        ).fetchone()
        if row is not None:
            return projection
        if projection.completion_digest is not None:
            existing = self.find_by_completion_digest(
                projection.campaign_id, projection.completion_digest
            )
            if existing is not None:
                return existing
        raise ValueError("report revision identity conflicted with another immutable revision")

    def find_by_completion_digest(
        self, campaign_id: str, completion_digest: str
    ) -> CampaignReportProjection | None:
        row = self.connection.execute(
            """SELECT revision_id, campaign_id, revision, digest, projection, created_at
               FROM merlin_storage.report_revisions
               WHERE campaign_id = %s
                 AND projection->>'completion_digest' = %s
               ORDER BY revision LIMIT 1""",
            (campaign_id, completion_digest),
        ).fetchone()
        return _checked_projection(row) if row else None

    def get(self, revision_id: str) -> CampaignReportProjection | None:
        row = self.connection.execute(
            """SELECT revision_id, campaign_id, revision, digest, projection, created_at
               FROM merlin_storage.report_revisions WHERE revision_id = %s""",
            (revision_id,),
        ).fetchone()
        return _checked_projection(row) if row else None

    def list(self, campaign_id: str) -> list[CampaignReportProjection]:
        rows = self.connection.execute(
            """SELECT revision_id, campaign_id, revision, digest, projection, created_at
               FROM merlin_storage.report_revisions
               WHERE campaign_id = %s ORDER BY revision""",
            (campaign_id,),
        ).fetchall()
        return [_checked_projection(row) for row in rows]

    def latest_for_session(self, session_id: str) -> CampaignReportProjection | None:
        row = self.connection.execute(
            """WITH ranked AS (
                   SELECT report.revision_id, report.campaign_id, report.revision,
                          report.digest, report.projection, report.created_at,
                          row_number() OVER (
                              PARTITION BY report.campaign_id
                              ORDER BY
                                  (report.projection->>'source_seal_state' = 'sealed') DESC,
                                  report.created_at DESC,
                                  report.revision_id DESC
                          ) AS campaign_rank
                   FROM merlin_storage.report_revisions AS report
                   JOIN merlin_storage.assessment_campaigns AS campaign
                     ON campaign.id = report.campaign_id
                   WHERE campaign.session_id = %s
               )
               SELECT revision_id, campaign_id, revision, digest, projection, created_at
               FROM ranked
               WHERE campaign_rank = 1
               ORDER BY created_at DESC, revision_id DESC
               LIMIT 1""",
            (session_id,),
        ).fetchone()
        return _checked_projection(row) if row else None


def _projection(document: Mapping[str, Any]) -> CampaignReportProjection:
    return CampaignReportProjection(
        revision_id=str(document["revision_id"]),
        campaign_id=str(document["campaign_id"]),
        revision=int(document["revision"]),
        stop_reason=str(document["stop_reason"]),  # type: ignore[arg-type]
        cleanup_status=str(document["cleanup_status"]),
        findings=tuple(document["findings"]),
        tasks=tuple(document["tasks"]),
        evidence=tuple(EvidenceCitation(**item) for item in document["evidence"]),
        usage=dict(document["usage"]),
        created_at=datetime.fromisoformat(str(document["created_at"])),
        digest=str(document["digest"]),
        audience=str(document.get("audience", "internal")),  # type: ignore[arg-type]
        report_state=str(document.get("report_state", "incomplete")),  # type: ignore[arg-type]
        metadata=dict(document.get("metadata", {})),
        coverage=dict(document.get("coverage", {})),
        techniques=tuple(document.get("techniques", ())),
        exchanges=tuple(document.get("exchanges", ())),
        lineage=tuple(document.get("lineage", ())),
        hypotheses=tuple(document.get("hypotheses", ())),
        unsearched_frontier=tuple(document.get("unsearched_frontier", ())),
        limitations=tuple(str(item) for item in document.get("limitations", ())),
        completion_digest=(
            str(document["completion_digest"])
            if document.get("completion_digest") is not None
            else None
        ),
        completion_document=(
            dict(document["completion_document"])
            if document.get("completion_document") is not None
            else None
        ),
        source_seal_state=str(  # type: ignore[arg-type]
            document.get("source_seal_state", "legacy_unsealed")
        ),
        digest_schema=str(document.get("digest_schema", "v1")),  # type: ignore[arg-type]
    )


def _checked_projection(row: Mapping[str, Any]) -> CampaignReportProjection:
    projection = _projection(row["projection"])
    if (
        projection.revision_id != str(row["revision_id"])
        or projection.campaign_id != str(row["campaign_id"])
        or projection.revision != int(row["revision"])
        or projection.digest != str(row["digest"])
        or projection.created_at != row["created_at"]
    ):
        raise ValueError("indexed report identity conflicts with its immutable projection")
    return projection
