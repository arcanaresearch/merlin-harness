from __future__ import annotations

from pathlib import Path

import pytest
from merlin_storage_postgres.daemon_run_store import DaemonRunRepository
from merlin_storage_postgres.database import Database, DatabaseConfig
from merlin_storage_postgres.migrations import MigrationRunner


def test_daemon_run_lifecycle_is_idempotent(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            repository = DaemonRunRepository(connection)

            started = repository.start("daemon/run-1", pid=321, details={"socket": "local"})
            assert started.status == "running"
            assert repository.start("daemon/run-1", pid=321, details={"socket": "local"}) == started
            assert repository.start("daemon/run-1", pid=321) == started

            heartbeat = repository.heartbeat("daemon/run-1")
            assert heartbeat.heartbeat_at >= started.heartbeat_at

            stopping = repository.request_stop(
                "daemon/run-1",
                stop_kind="clean",
                reason="operator requested shutdown",
            )
            assert stopping.status == "stopping"
            assert stopping.stop_requested_at is not None
            assert (
                repository.request_stop(
                    "daemon/run-1",
                    stop_kind="clean",
                    reason="operator requested shutdown",
                )
                == stopping
            )

            stopped = repository.finish(
                "daemon/run-1",
                stop_kind="clean",
                reason="operator requested shutdown",
                details={"cleanup": "completed"},
            )
            assert stopped.status == "stopped"
            assert stopped.stopped_at is not None
            assert stopped.details == {"cleanup": "completed"}
            assert (
                repository.finish(
                    "daemon/run-1",
                    stop_kind="clean",
                    reason="operator requested shutdown",
                    details={"cleanup": "completed"},
                )
                == stopped
            )


def test_new_daemon_marks_incomplete_predecessor_unclean(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            repository = DaemonRunRepository(connection)

            repository.start("daemon/old", pid=111)
            current = repository.start("daemon/current", pid=222)
            old = repository.get("daemon/old")

            assert current.status == "running"
            assert old is not None
            assert old.status == "unclean"
            assert old.stop_kind == "missing_heartbeat"
            assert old.stopped_at is not None
            assert repository.start("daemon/current", pid=222) == current


def test_daemon_run_rejects_identity_and_transition_conflicts(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            repository = DaemonRunRepository(connection)
            repository.start("daemon/run-1", pid=321)

            with pytest.raises(ValueError, match="identity conflicts"):
                repository.start("daemon/run-1", pid=999)
            with pytest.raises(ValueError, match="invalid daemon stop kind"):
                repository.finish(
                    "daemon/run-1",
                    stop_kind="invented",  # type: ignore[arg-type]
                    reason="invalid",
                )

            repository.finish("daemon/run-1", stop_kind="signal", reason="SIGTERM")
            with pytest.raises(ValueError, match="cannot heartbeat from stopped"):
                repository.heartbeat("daemon/run-1")
            with pytest.raises(ValueError, match="cannot request stop from stopped"):
                repository.request_stop(
                    "daemon/run-1",
                    stop_kind="clean",
                    reason="too late",
                )
