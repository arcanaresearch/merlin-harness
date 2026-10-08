"""Fail-closed quarantine and explicit acceptance for imported bundles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError

from .bundle import MAX_RECORDS, MemoryBundle, MemoryRecord


class QuarantineError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ImportPolicy:
    allowed_kinds: frozenset[str]
    allowed_scope_ids: frozenset[str] = frozenset()
    max_bytes: int = 1_048_576
    max_records: int = MAX_RECORDS


class MemoryQuarantine:
    def __init__(self, *, existing_ids: frozenset[str] = frozenset()) -> None:
        self._existing_ids = set(existing_ids)
        self._pending: dict[str, MemoryBundle] = {}

    def import_bundle(
        self,
        raw: bytes,
        policy: ImportPolicy,
        *,
        now: datetime | None = None,
    ) -> MemoryBundle:
        if len(raw) > policy.max_bytes:
            raise QuarantineError("bundle exceeds import size limit")
        try:
            bundle = MemoryBundle.model_validate_json(raw)
        except ValidationError as error:
            raise QuarantineError(f"invalid or tampered bundle: {error}") from error
        if len(bundle.records) > policy.max_records:
            raise QuarantineError("bundle exceeds record limit")
        if bundle.bundle_id in self._pending:
            raise QuarantineError("bundle is already quarantined")
        collisions = sorted(
            record.id for record in bundle.records if record.id in self._existing_ids
        )
        if collisions:
            raise QuarantineError(f"record id collision: {', '.join(collisions)}")
        current = now or datetime.now(UTC)
        for record in bundle.records:
            if record.payload.kind not in policy.allowed_kinds:
                raise QuarantineError(f"record kind is outside import scope: {record.payload.kind}")
            if record.expires_at is not None and record.expires_at <= current:
                raise QuarantineError(f"record has expired: {record.id}")
            scopes = set(record.applicability.scope_ids)
            if scopes and not scopes <= policy.allowed_scope_ids:
                raise QuarantineError(f"record applicability is outside import scope: {record.id}")
        self._pending[bundle.bundle_id] = bundle
        return bundle

    def pending(self) -> tuple[MemoryBundle, ...]:
        return tuple(self._pending[key] for key in sorted(self._pending))

    def accept(self, bundle_id: str, record_ids: frozenset[str]) -> tuple[MemoryRecord, ...]:
        """Explicitly accept a reviewed subset; nothing imports implicitly."""
        try:
            bundle = self._pending[bundle_id]
        except KeyError as error:
            raise QuarantineError(f"unknown quarantined bundle: {bundle_id}") from error
        available = {record.id: record for record in bundle.records}
        if not record_ids or not record_ids <= available.keys():
            raise QuarantineError("acceptance must name a non-empty subset of quarantined records")
        accepted = tuple(available[id] for id in sorted(record_ids))
        self._existing_ids.update(record_ids)
        del self._pending[bundle_id]
        return accepted
