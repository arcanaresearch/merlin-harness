from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from merlin_memory import MemoryBundle, MemoryRecord

Disposition = Literal["accepted", "rejected"]


class PortableMemoryRepository:
    """Transactional quarantine, review, retrieval, and turn-use audit boundary."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def quarantine(self, bundle: MemoryBundle, *, operator_id: str) -> MemoryBundle:
        _operator(operator_id)
        document = bundle.model_dump(mode="json", by_alias=True)
        with self.connection.transaction():
            collisions = self.connection.execute(
                "SELECT record_id FROM merlin_storage.portable_memory_records "
                "WHERE record_id = ANY(%s)",
                ([record.id for record in bundle.records],),
            ).fetchall()
            if collisions:
                raise ValueError("portable memory record id collision")
            self.connection.execute(
                """INSERT INTO merlin_storage.portable_memory_bundles
                   (bundle_id, schema_version, digest, provenance, document)
                   VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)""",
                (
                    bundle.bundle_id,
                    bundle.schema_version,
                    bundle.digest,
                    json.dumps(bundle.provenance.model_dump(mode="json")),
                    json.dumps(document),
                ),
            )
            for record in bundle.records:
                self.connection.execute(
                    """INSERT INTO merlin_storage.portable_memory_records
                       (record_id, bundle_id, kind, digest, payload, provenance,
                        applicability, redactions, expires_at)
                       VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                               %s::jsonb, %s)""",
                    (
                        record.id,
                        bundle.bundle_id,
                        record.payload.kind,
                        record.digest,
                        record.payload.model_dump_json(),
                        record.provenance.model_dump_json(),
                        record.applicability.model_dump_json(),
                        json.dumps(record.redactions),
                        record.expires_at,
                    ),
                )
                self._audit(
                    bundle.bundle_id,
                    record.id,
                    "quarantined",
                    operator_id,
                    None,
                    record.provenance.model_dump(mode="json"),
                )
        return bundle

    def list_records(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in {
            "quarantined",
            "accepted",
            "rejected",
            "deleted",
        }:
            raise ValueError("unknown portable memory status")
        query = """SELECT record_id, bundle_id, kind, digest, provenance, applicability,
                          redactions, expires_at, status, reviewed_at, reviewed_by,
                          rejection_reason, payload
                   FROM merlin_storage.portable_memory_records"""
        parameters: tuple[object, ...] = ()
        if status is not None:
            query += " WHERE status = %s"
            parameters = (status,)
        query += " ORDER BY bundle_id, record_id"
        return self.connection.execute(query, parameters).fetchall()

    def accept(
        self, bundle_id: str, record_ids: Sequence[str], *, operator_id: str
    ) -> tuple[MemoryRecord, ...]:
        return self._disposition(bundle_id, record_ids, "accepted", operator_id, None)

    def reject(
        self,
        bundle_id: str,
        record_ids: Sequence[str],
        *,
        operator_id: str,
        reason: str,
    ) -> tuple[MemoryRecord, ...]:
        if not reason.strip():
            raise ValueError("rejection reason is required")
        return self._disposition(bundle_id, record_ids, "rejected", operator_id, reason)

    def delete(
        self,
        bundle_id: str,
        record_ids: Sequence[str],
        *,
        operator_id: str,
        reason: str,
    ) -> None:
        _operator(operator_id)
        ids = _ids(record_ids)
        if not reason.strip():
            raise ValueError("deletion reason is required")
        with self.connection.transaction():
            rows = self._locked(bundle_id, ids, allowed=("quarantined", "accepted", "rejected"))
            self.connection.execute(
                """UPDATE merlin_storage.portable_memory_records
                   SET status = 'deleted', deleted_at = now(), reviewed_by = %s
                   WHERE bundle_id = %s AND record_id = ANY(%s)""",
                (operator_id, bundle_id, list(ids)),
            )
            for row in rows:
                self._audit(
                    bundle_id,
                    row["record_id"],
                    "deleted",
                    operator_id,
                    reason,
                    row["provenance"],
                )
            self._refresh_bundle(bundle_id, operator_id)

    def active(
        self,
        *,
        repository_id: str,
        target_id: str,
        provider_id: str,
        operator_id: str,
        scope_ids: Sequence[str] = (),
        tags: Sequence[str] = (),
        at: datetime | None = None,
    ) -> tuple[MemoryRecord, ...]:
        _scope(repository_id, target_id, provider_id, operator_id)
        current = at or datetime.now(UTC)
        rows = self.connection.execute(
            _ACTIVE_SQL,
            (
                current,
                repository_id,
                target_id,
                provider_id,
                operator_id,
                list(scope_ids),
                list(tags),
                list(tags),
            ),
        ).fetchall()
        return tuple(_record(row) for row in rows)

    def record_turn_use(
        self,
        session_id: str,
        turn_id: str,
        record_ids: Sequence[str],
        *,
        repository_id: str,
        target_id: str,
        provider_id: str,
        operator_id: str,
        scope_ids: Sequence[str] = (),
        tags: Sequence[str] = (),
        at: datetime | None = None,
    ) -> tuple[MemoryRecord, ...]:
        _operator(operator_id)
        ids = _ids(record_ids)
        with self.connection.transaction():
            active = self.active(
                repository_id=repository_id,
                target_id=target_id,
                provider_id=provider_id,
                operator_id=operator_id,
                scope_ids=scope_ids,
                tags=tags,
                at=at,
            )
            selected = {record.id: record for record in active if record.id in ids}
            if selected.keys() != ids:
                raise ValueError("turn references inactive or inapplicable portable memory")
            for record_id in sorted(ids):
                record = selected[record_id]
                bundle_id = self.connection.execute(
                    "SELECT bundle_id FROM merlin_storage.portable_memory_records "
                    "WHERE record_id = %s",
                    (record_id,),
                ).fetchone()["bundle_id"]
                applicability = record.applicability.model_dump(mode="json")
                inserted = self.connection.execute(
                    """INSERT INTO merlin_storage.portable_memory_turn_uses
                       (session_id, turn_id, record_id, bundle_id, operator_id, applicability)
                       VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                       ON CONFLICT DO NOTHING RETURNING record_id""",
                    (
                        session_id,
                        turn_id,
                        record_id,
                        bundle_id,
                        operator_id,
                        json.dumps(applicability),
                    ),
                ).fetchone()
                if inserted is not None:
                    self._audit(
                        bundle_id,
                        record_id,
                        "used_in_turn",
                        operator_id,
                        f"{session_id}:{turn_id}",
                        record.provenance.model_dump(mode="json"),
                    )
        return tuple(selected[id] for id in sorted(ids))

    def turn_uses(self, session_id: str, turn_id: str) -> tuple[str, ...]:
        rows = self.connection.execute(
            """SELECT record_id FROM merlin_storage.portable_memory_turn_uses
               WHERE session_id = %s AND turn_id = %s ORDER BY record_id""",
            (session_id, turn_id),
        ).fetchall()
        return tuple(row["record_id"] for row in rows)

    def _disposition(
        self,
        bundle_id: str,
        record_ids: Sequence[str],
        disposition: Disposition,
        operator_id: str,
        reason: str | None,
    ) -> tuple[MemoryRecord, ...]:
        _operator(operator_id)
        ids = _ids(record_ids)
        with self.connection.transaction():
            rows = self._locked(bundle_id, ids, allowed=("quarantined",))
            self.connection.execute(
                """UPDATE merlin_storage.portable_memory_records
                   SET status = %s, reviewed_at = now(), reviewed_by = %s,
                       rejection_reason = %s
                   WHERE bundle_id = %s AND record_id = ANY(%s)""",
                (disposition, operator_id, reason, bundle_id, list(ids)),
            )
            for row in rows:
                self._audit(
                    bundle_id,
                    row["record_id"],
                    disposition,
                    operator_id,
                    reason,
                    row["provenance"],
                )
            self._refresh_bundle(bundle_id, operator_id)
        return tuple(_record(row) for row in rows)

    def _locked(
        self, bundle_id: str, ids: frozenset[str], *, allowed: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT * FROM merlin_storage.portable_memory_records
               WHERE bundle_id = %s AND record_id = ANY(%s) FOR UPDATE""",
            (bundle_id, list(ids)),
        ).fetchall()
        if {row["record_id"] for row in rows} != ids or any(
            row["status"] not in allowed for row in rows
        ):
            raise ValueError("portable memory transition is invalid or incomplete")
        return rows

    def _refresh_bundle(self, bundle_id: str, operator_id: str) -> None:
        statuses = {
            row["status"]
            for row in self.connection.execute(
                "SELECT status FROM merlin_storage.portable_memory_records WHERE bundle_id = %s",
                (bundle_id,),
            ).fetchall()
        }
        status = "quarantined"
        if "quarantined" not in statuses:
            status = next(iter(statuses)) if len(statuses) == 1 else "reviewed"
        self.connection.execute(
            """UPDATE merlin_storage.portable_memory_bundles
               SET status = %s, reviewed_at = now(), reviewed_by = %s,
                   deleted_at = CASE WHEN %s = 'deleted' THEN now() ELSE deleted_at END
               WHERE bundle_id = %s""",
            (status, operator_id, status, bundle_id),
        )

    def _audit(
        self,
        bundle_id: str,
        record_id: str,
        event: str,
        operator_id: str,
        reason: str | None,
        provenance: dict[str, Any],
    ) -> None:
        self.connection.execute(
            """INSERT INTO merlin_storage.portable_memory_audit
               (bundle_id, record_id, event, operator_id, reason, provenance)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb)""",
            (bundle_id, record_id, event, operator_id, reason, json.dumps(provenance)),
        )


_ACTIVE_SQL = """
SELECT * FROM merlin_storage.portable_memory_records
WHERE status = 'accepted' AND (expires_at IS NULL OR expires_at > %s)
  AND (jsonb_array_length(applicability->'repository_ids') = 0
       OR applicability->'repository_ids' ? %s)
  AND (jsonb_array_length(applicability->'target_ids') = 0
       OR applicability->'target_ids' ? %s)
  AND (jsonb_array_length(applicability->'provider_ids') = 0
       OR applicability->'provider_ids' ? %s)
  AND (jsonb_array_length(applicability->'operator_ids') = 0
       OR applicability->'operator_ids' ? %s)
  AND (jsonb_array_length(applicability->'scope_ids') = 0
       OR applicability->'scope_ids' ?| %s)
  AND (%s::text[] = '{}'::text[] OR jsonb_array_length(applicability->'tags') = 0
       OR applicability->'tags' ?| %s)
ORDER BY record_id
"""


def _record(row: dict[str, Any]) -> MemoryRecord:
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


def _ids(record_ids: Sequence[str]) -> frozenset[str]:
    ids = frozenset(record_ids)
    if not ids or any(not id.strip() for id in ids):
        raise ValueError("record ids must be a non-empty set")
    return ids


def _operator(operator_id: str) -> None:
    if not operator_id.strip():
        raise PermissionError("an explicit operator is required")


def _scope(repository_id: str, target_id: str, provider_id: str, operator_id: str) -> None:
    if any(not value.strip() for value in (repository_id, target_id, provider_id, operator_id)):
        raise ValueError("repository, target, provider, and operator scope are required")
