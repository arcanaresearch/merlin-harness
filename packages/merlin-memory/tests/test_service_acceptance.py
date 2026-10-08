from datetime import UTC, datetime
from pathlib import Path

import pytest
from merlin_memory import (
    Applicability,
    BundleProvenance,
    GuidanceMemory,
    ImportPolicy,
    MemoryBundle,
    MemoryRecord,
    PortableMemoryService,
    ReviewedSkill,
)
from pydantic import ValidationError

NOW = datetime(2026, 8, 27, tzinfo=UTC)


class ExportRepository:
    def __init__(self, rows):
        self.rows = rows

    def list_records(self, status=None):
        return [row for row in self.rows if status is None or row["status"] == status]


def row(record: MemoryRecord, status: str) -> dict[str, object]:
    return {
        "bundle_id": "bundle/source",
        "record_id": record.id,
        "kind": record.payload.kind,
        "status": status,
        "payload": record.payload.model_dump(mode="json"),
        "provenance": record.provenance.model_dump(mode="json"),
        "applicability": record.applicability.model_dump(mode="json"),
        "redactions": list(record.redactions),
        "expires_at": record.expires_at,
        "digest": record.digest,
        "rejection_reason": None,
    }


@pytest.mark.asyncio
async def test_export_is_reviewed_only_and_excludes_private_authority(tmp_path: Path) -> None:
    provenance = BundleProvenance(
        source="merlin://reviewed/source",
        exported_by="operator/exporter",
        reviewed_by=("operator/reviewer",),
        created_at=NOW,
    )
    accepted = MemoryRecord(
        id="skill/accepted",
        payload=ReviewedSkill(name="accepted", instructions="Use exact evidence."),
        provenance=provenance,
        applicability=Applicability(scope_ids=("scope/reviewed",)),
    )
    private = MemoryRecord(
        id="guidance/private",
        payload=GuidanceMemory(
            guidance_id="private", content="private transcript prose source-api-secret"
        ),
        provenance=provenance,
    )
    policy = ImportPolicy(
        allowed_kinds=frozenset({"reviewed_skill", "non_secret_config"}),
        allowed_scope_ids=frozenset({"scope/reviewed"}),
    )
    service = PortableMemoryService(
        ExportRepository((row(accepted, "accepted"), row(private, "rejected"))),
        policy,
        non_secret_config=lambda: {"provider": {"model": "reviewed-model"}},
        operator_id="operator/exporter",
        workspace_root=tmp_path,
    )
    output = tmp_path / "memory.json"

    await service.export_file(output)

    raw = output.read_bytes()
    bundle = MemoryBundle.model_validate_json(raw)
    assert [record.id for record in bundle.records[:-1]] == [accepted.id]
    assert b"private transcript prose" not in raw
    assert b"source-api-secret" not in raw
    with pytest.raises(ValidationError, match="not portable"):
        await PortableMemoryService(
            ExportRepository((row(accepted, "accepted"),)),
            policy,
            non_secret_config=lambda: {"permission_grant": "allow-always"},
            workspace_root=tmp_path,
        ).export_file(tmp_path / "unsafe.json")
