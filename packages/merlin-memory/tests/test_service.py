import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from merlin_memory import (
    BundleProvenance,
    ImportPolicy,
    MemoryBundle,
    MemoryRecord,
    NonSecretConfig,
    PortableMemoryService,
    ReviewedSkill,
    build_bundle,
    encode_bundle,
)
from pydantic import ValidationError

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def provenance() -> BundleProvenance:
    return BundleProvenance(
        source="merlin://repo/source",
        exported_by="operator/source",
        reviewed_by=("operator/reviewer",),
        created_at=NOW,
    )


def record(id: str) -> MemoryRecord:
    return MemoryRecord(
        id=id,
        payload=ReviewedSkill(name=id, instructions=f"Reviewed {id}"),
        provenance=provenance(),
    )


def row(value: MemoryRecord, status: str, bundle_id: str = "bundle/source") -> dict:
    return {
        "record_id": value.id,
        "bundle_id": bundle_id,
        "kind": value.payload.kind,
        "digest": value.digest,
        "payload": value.payload.model_dump(mode="json"),
        "provenance": value.provenance.model_dump(mode="json"),
        "applicability": value.applicability.model_dump(mode="json"),
        "redactions": list(value.redactions),
        "expires_at": value.expires_at,
        "status": status,
        "reviewed_at": None,
        "reviewed_by": None,
        "rejection_reason": None,
    }


class Repository:
    def __init__(self) -> None:
        self.rows = [
            row(record("skill/accepted"), "accepted"),
            row(record("skill/q"), "quarantined"),
        ]
        self.imported = []

    def list_records(self, status=None):
        return [item for item in self.rows if status is None or item["status"] == status]

    def quarantine(self, bundle, *, operator_id):
        self.imported.append((bundle, operator_id))


async def test_service_export_contains_only_accepted_records_and_allowlisted_config(
    tmp_path: Path,
) -> None:
    repository = Repository()
    service = PortableMemoryService(
        repository,  # type: ignore[arg-type]
        ImportPolicy(allowed_kinds=frozenset({"reviewed_skill", "non_secret_config"})),
        non_secret_config=lambda: {
            "provider": {"base_url": "https://provider.example/v1", "model": "model"},
            "permission_mode": "prompt",
        },
        workspace_root=tmp_path,
    )

    result = await service.export_file("memory/export.json")
    raw = await asyncio.to_thread(Path(result["path"]).read_bytes)
    exported = MemoryBundle.model_validate_json(raw)

    assert [item.id for item in exported.records if item.payload.kind == "reviewed_skill"] == [
        "skill/accepted"
    ]
    assert all(item.id != "skill/q" for item in exported.records)
    config = next(
        item.payload for item in exported.records if item.payload.kind == "non_secret_config"
    )
    assert config.values["provider"]["model"] == "model"


async def test_service_import_is_quarantined_and_paths_are_workspace_bounded(
    tmp_path: Path,
) -> None:
    repository = Repository()
    service = PortableMemoryService(
        repository,  # type: ignore[arg-type]
        ImportPolicy(allowed_kinds=frozenset({"reviewed_skill"})),
        workspace_root=tmp_path,
    )
    imported = build_bundle("bundle/new", provenance(), (record("skill/new"),))
    path = tmp_path / "incoming.json"
    path.write_bytes(encode_bundle(imported))

    result = await service.import_file(path)

    assert result == {"bundle_id": "bundle/new", "status": "quarantined", "count": 1}
    assert repository.imported[0][0] == imported
    with pytest.raises(ValueError, match="outside the workspace"):
        await service.export_file(tmp_path.parent / "escape.json")


async def test_list_memory_exposes_scope_conflicts_and_optional_expiry() -> None:
    repository = Repository()
    service = PortableMemoryService(
        repository,  # type: ignore[arg-type]
        ImportPolicy(allowed_kinds=frozenset({"reviewed_skill"})),
    )

    rows = await service.list_memory()

    assert rows[0]["applicability"]["scope_ids"] == []
    assert rows[0]["expires_at"] is None
    assert rows[0]["conflicts"] == ("skill/q",)
    assert rows[1]["conflicts"] == ("skill/accepted",)


def test_non_secret_config_explicitly_excludes_grants_and_authority() -> None:
    with pytest.raises(ValidationError, match="not portable"):
        NonSecretConfig(values={"grants": ["target:all"]})
    with pytest.raises(ValidationError, match="not portable"):
        NonSecretConfig(values={"authority": {"scope": "all"}})
