from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from merlin_agents import PortableMemoryQuery, PortableMemoryRecord

from .bundle import (
    BundleProvenance,
    MemoryRecord,
    NonSecretConfig,
    build_bundle,
    encode_bundle,
)
from .quarantine import ImportPolicy, MemoryQuarantine


class MemoryRepository(Protocol):
    def list_records(self, status: str | None = None) -> list[dict[str, Any]]: ...
    def quarantine(self, bundle: Any, *, operator_id: str) -> Any: ...
    def accept(self, bundle_id: str, record_ids: Sequence[str], *, operator_id: str) -> Any: ...
    def reject(
        self,
        bundle_id: str,
        record_ids: Sequence[str],
        *,
        operator_id: str,
        reason: str,
    ) -> Any: ...
    def delete(
        self,
        bundle_id: str,
        record_ids: Sequence[str],
        *,
        operator_id: str,
        reason: str,
    ) -> None: ...
    def active(self, **scope: Any) -> tuple[MemoryRecord, ...]: ...
    def record_turn_use(self, *args: Any, **kwargs: Any) -> tuple[MemoryRecord, ...]: ...


class PortableMemoryService:
    """Production async service over portable-memory persistence."""

    def __init__(
        self,
        repository: MemoryRepository,
        import_policy: ImportPolicy,
        *,
        non_secret_config: Callable[[], Mapping[str, Any]] = dict,
        operator_id: str = "operator/local",
        workspace_root: Path | str = ".",
    ) -> None:
        if not operator_id.strip():
            raise ValueError("portable memory requires an operator id")
        self._repository = repository
        self._policy = import_policy
        self._non_secret_config = non_secret_config
        self._operator_id = operator_id
        self._workspace_root = Path(workspace_root).resolve()
        self._lock = asyncio.Lock()

    async def list_memory(self, status: str | None = None) -> Sequence[Mapping[str, Any]]:
        rows = await self._call(self._repository.list_records, status)
        return [
            {
                "bundle_id": row["bundle_id"],
                "record_id": row["record_id"],
                "kind": row["kind"],
                "status": row["status"],
                "source": row["provenance"]["source"],
                "reviewed_by": row["provenance"]["reviewed_by"],
                "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
                "applicability": row["applicability"],
                "redactions": row["redactions"],
                "digest": row["digest"],
                "rejection_reason": row["rejection_reason"],
                "conflicts": _conflicts(row, rows),
            }
            for row in rows
        ]

    async def import_file(self, path: Path | str) -> Mapping[str, Any]:
        resolved = self._path(path)
        if resolved.stat().st_size > self._policy.max_bytes:
            raise ValueError("portable memory bundle exceeds import size limit")
        raw = await asyncio.to_thread(resolved.read_bytes)
        rows = await self._call(self._repository.list_records, None)
        quarantine = MemoryQuarantine(existing_ids=frozenset(str(row["record_id"]) for row in rows))
        bundle = quarantine.import_bundle(raw, self._policy)
        await self._call(self._repository.quarantine, bundle, operator_id=self._operator_id)
        return {
            "bundle_id": bundle.bundle_id,
            "status": "quarantined",
            "count": len(bundle.records),
        }

    async def export_file(self, path: Path | str) -> Mapping[str, Any]:
        rows = await self._call(self._repository.list_records, "accepted")
        records = tuple(_stored_record(row) for row in rows)
        now = datetime.now(UTC)
        provenance = BundleProvenance(
            source="merlin://portable-memory/accepted",
            exported_by=self._operator_id,
            reviewed_by=(self._operator_id,),
            created_at=now,
        )
        config = NonSecretConfig(values=dict(self._non_secret_config()))
        config_record = MemoryRecord(id=f"config/{uuid4()}", payload=config, provenance=provenance)
        bundle = build_bundle(f"bundle/{uuid4()}", provenance, records + (config_record,))
        resolved = self._path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(resolved.write_bytes, encode_bundle(bundle))
        return {"path": str(resolved), "bundle_id": bundle.bundle_id, "count": len(bundle.records)}

    async def accept(self, bundle_id: str, record_ids: Sequence[str]) -> Mapping[str, Any]:
        records = await self._call(
            self._repository.accept,
            bundle_id,
            record_ids,
            operator_id=self._operator_id,
        )
        return {"bundle_id": bundle_id, "accepted": [record.id for record in records]}

    async def reject(
        self, bundle_id: str, record_ids: Sequence[str], reason: str
    ) -> Mapping[str, Any]:
        records = await self._call(
            self._repository.reject,
            bundle_id,
            record_ids,
            operator_id=self._operator_id,
            reason=reason,
        )
        return {"bundle_id": bundle_id, "rejected": [record.id for record in records]}

    async def delete(
        self, bundle_id: str, record_ids: Sequence[str], reason: str
    ) -> Mapping[str, Any]:
        await self._call(
            self._repository.delete,
            bundle_id,
            record_ids,
            operator_id=self._operator_id,
            reason=reason,
        )
        return {"bundle_id": bundle_id, "deleted": sorted(set(record_ids))}

    async def retrieve(self, query: PortableMemoryQuery) -> tuple[PortableMemoryRecord, ...]:
        records = await self._call(
            self._repository.active,
            repository_id=query.repository_id,
            target_id=query.target_id,
            provider_id=query.provider_id,
            operator_id=query.operator_id,
            scope_ids=query.scope_ids,
            tags=query.tags,
        )
        return tuple(
            PortableMemoryRecord(
                id=record.id,
                kind=record.payload.kind,
                content=_content(record),
                provenance_source=record.provenance.source,
                reviewed_by=record.provenance.reviewed_by,
                digest=record.digest,
            )
            for record in records
        )

    async def record_turn_use(
        self,
        query: PortableMemoryQuery,
        turn_id: str,
        record_ids: tuple[str, ...],
    ) -> None:
        await self._call(
            self._repository.record_turn_use,
            query.session_id,
            turn_id,
            record_ids,
            repository_id=query.repository_id,
            target_id=query.target_id,
            provider_id=query.provider_id,
            operator_id=query.operator_id,
            scope_ids=query.scope_ids,
            tags=query.tags,
        )

    async def _call(self, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(function, *args, **kwargs)

    def _path(self, path: Path | str) -> Path:
        candidate = Path(path)
        resolved = (
            (self._workspace_root / candidate).resolve()
            if not candidate.is_absolute()
            else candidate.resolve()
        )
        if not resolved.is_relative_to(self._workspace_root):
            raise ValueError("portable memory path is outside the workspace")
        return resolved


def _stored_record(row: Mapping[str, Any]) -> MemoryRecord:
    return MemoryRecord.model_validate(
        {
            "id": row["record_id"],
            "payload": row["payload"],
            "provenance": row["provenance"],
            "applicability": row["applicability"],
            "redactions": row["redactions"],
            "expires_at": row["expires_at"],
            "digest": row["digest"],
        }
    )


def _content(record: MemoryRecord) -> str:
    payload = record.payload
    for attribute in ("instructions", "content", "statement", "observation"):
        value = getattr(payload, attribute, None)
        if isinstance(value, str):
            return value
    return json.dumps(payload.model_dump(mode="json"), sort_keys=True)


def _conflicts(row: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    if row["status"] == "deleted":
        return ()
    return tuple(
        sorted(
            str(other["record_id"])
            for other in rows
            if other["record_id"] != row["record_id"]
            and other["status"] in {"accepted", "quarantined"}
            and other["kind"] == row["kind"]
            and _scopes_overlap(row["applicability"], other["applicability"])
        )
    )


def _scopes_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    dimensions = (
        "repository_ids",
        "target_ids",
        "provider_ids",
        "operator_ids",
        "target_kinds",
        "scope_ids",
        "tags",
    )
    for dimension in dimensions:
        left_values = set(left.get(dimension, ()))
        right_values = set(right.get(dimension, ()))
        if left_values and right_values and left_values.isdisjoint(right_values):
            return False
    return True
