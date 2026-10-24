from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from psycopg.types.json import Jsonb

DaemonRunStatus = Literal["running", "stopping", "stopped", "unclean"]
DaemonStopKind = Literal["clean", "fatal_exception", "signal", "missing_heartbeat", "unknown"]


@dataclass(frozen=True, slots=True)
class DaemonRun:
    id: str
    pid: int
    started_at: datetime
    heartbeat_at: datetime
    stop_requested_at: datetime | None
    stopped_at: datetime | None
    status: DaemonRunStatus
    stop_kind: DaemonStopKind | None
    stop_reason: str | None
    details: Mapping[str, object]


class DaemonRunRepository:
    """Durable lifecycle store for daemon process incarnations."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def start(
        self,
        run_id: str,
        *,
        pid: int,
        details: Mapping[str, object] | None = None,
    ) -> DaemonRun:
        identifier = run_id.strip()
        if not identifier:
            raise ValueError("daemon run id must not be blank")
        if pid <= 0:
            raise ValueError("daemon pid must be positive")
        supplied_details = dict(details or {})
        with self.connection.transaction():
            self.connection.execute(
                """UPDATE merlin_storage.daemon_runs
                   SET status = 'unclean', stopped_at = now(),
                       stop_kind = 'missing_heartbeat',
                       stop_reason = coalesce(stop_reason, 'superseded by a new daemon run')
                   WHERE id <> %s AND status IN ('running', 'stopping')""",
                (identifier,),
            )
            inserted = self.connection.execute(
                """INSERT INTO merlin_storage.daemon_runs (id, pid, details)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (id) DO NOTHING RETURNING id""",
                (identifier, pid, Jsonb(supplied_details)),
            ).fetchone()
            stored = self.get(identifier)
            if stored is None:
                raise RuntimeError("daemon run insert did not persist")
            if inserted is None and (
                stored.pid != pid
                or (details is not None and dict(stored.details) != supplied_details)
            ):
                raise ValueError("daemon run identity conflicts with an existing record")
            return stored

    def heartbeat(self, run_id: str) -> DaemonRun:
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.daemon_runs
                   SET heartbeat_at = now()
                   WHERE id = %s AND status IN ('running', 'stopping')
                   RETURNING id""",
                (run_id,),
            ).fetchone()
            if row is None:
                return self._missing_or_invalid(run_id, "heartbeat")
            return self._require(run_id)

    def request_stop(
        self,
        run_id: str,
        *,
        stop_kind: DaemonStopKind,
        reason: str,
        details: Mapping[str, object] | None = None,
    ) -> DaemonRun:
        self._validate_stop_kind(stop_kind)
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("daemon stop reason must not be blank")
        supplied_details = None if details is None else Jsonb(dict(details))
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.daemon_runs
                   SET status = 'stopping', stop_requested_at = coalesce(stop_requested_at, now()),
                       stop_kind = %s, stop_reason = %s,
                       details = coalesce(%s, details), heartbeat_at = now()
                   WHERE id = %s AND status = 'running'
                   RETURNING id""",
                (stop_kind, normalized_reason, supplied_details, run_id),
            ).fetchone()
            stored = self._require(run_id)
            if row is None:
                if (
                    stored.status == "stopping"
                    and stored.stop_kind == stop_kind
                    and stored.stop_reason == normalized_reason
                    and (details is None or dict(stored.details) == dict(details))
                ):
                    return stored
                raise ValueError(f"daemon run cannot request stop from {stored.status}")
            return stored

    def finish(
        self,
        run_id: str,
        *,
        stop_kind: DaemonStopKind,
        reason: str,
        details: Mapping[str, object] | None = None,
    ) -> DaemonRun:
        self._validate_stop_kind(stop_kind)
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("daemon stop reason must not be blank")
        supplied_details = None if details is None else Jsonb(dict(details))
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.daemon_runs
                   SET status = 'stopped',
                       stop_requested_at = coalesce(stop_requested_at, now()),
                       stopped_at = now(), heartbeat_at = now(),
                       stop_kind = %s, stop_reason = %s,
                       details = coalesce(%s, details)
                   WHERE id = %s AND status IN ('running', 'stopping')
                   RETURNING id""",
                (stop_kind, normalized_reason, supplied_details, run_id),
            ).fetchone()
            stored = self._require(run_id)
            if row is None:
                if (
                    stored.status == "stopped"
                    and stored.stop_kind == stop_kind
                    and stored.stop_reason == normalized_reason
                    and (details is None or dict(stored.details) == dict(details))
                ):
                    return stored
                raise ValueError(f"daemon run cannot finish from {stored.status}")
            return stored

    def get(self, run_id: str) -> DaemonRun | None:
        row = self.connection.execute(
            "SELECT * FROM merlin_storage.daemon_runs WHERE id = %s",
            (run_id,),
        ).fetchone()
        return None if row is None else self._row(row)

    def list_recent(self, *, limit: int = 20) -> tuple[DaemonRun, ...]:
        if limit <= 0:
            raise ValueError("daemon run limit must be positive")
        rows = self.connection.execute(
            """SELECT * FROM merlin_storage.daemon_runs
               ORDER BY started_at DESC, id DESC LIMIT %s""",
            (limit,),
        ).fetchall()
        return tuple(self._row(row) for row in rows)

    def _require(self, run_id: str) -> DaemonRun:
        stored = self.get(run_id)
        if stored is None:
            raise KeyError(run_id)
        return stored

    def _missing_or_invalid(self, run_id: str, operation: str) -> DaemonRun:
        stored = self.get(run_id)
        if stored is None:
            raise KeyError(run_id)
        raise ValueError(f"daemon run cannot {operation} from {stored.status}")

    @staticmethod
    def _validate_stop_kind(stop_kind: str) -> None:
        if stop_kind not in {"clean", "fatal_exception", "signal", "missing_heartbeat", "unknown"}:
            raise ValueError(f"invalid daemon stop kind: {stop_kind}")

    @staticmethod
    def _row(row: Mapping[str, Any]) -> DaemonRun:
        return DaemonRun(
            id=str(row["id"]),
            pid=int(row["pid"]),
            started_at=row["started_at"],
            heartbeat_at=row["heartbeat_at"],
            stop_requested_at=row["stop_requested_at"],
            stopped_at=row["stopped_at"],
            status=row["status"],
            stop_kind=row["stop_kind"],
            stop_reason=row["stop_reason"],
            details=dict(row["details"]),
        )
