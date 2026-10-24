from __future__ import annotations

from pathlib import Path

import pytest
from merlin_storage_postgres.daemon_run_store import DaemonRunRepository
from merlin_storage_postgres.database import Database, DatabaseConfig
from merlin_storage_postgres.migrations import MigrationRunner


@pytest.mark.asyncio
async def test_daemon_wiring_reconciles_unclean_run_and_finishes_cleanup(tmp_path: Path) -> None:
    import asyncio
    from tempfile import TemporaryDirectory
    from types import SimpleNamespace

    from merlin_tui.local_control import _Channel, serve

    with TemporaryDirectory(prefix="merlin-runs-", dir="/tmp") as directory:
        path = Path(directory) / "daemon.sock"
        with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
            with database.connect() as observer:
                MigrationRunner(observer).apply()
                repository = DaemonRunRepository(observer)
                repository.start("killed-predecessor", pid=123)
                connections = []
                cleanup_calls = []
                failed_id = None

                async def close() -> None:
                    cleanup_calls.append("close")
                    if len(cleanup_calls) == 1:
                        raise RuntimeError("injected cleanup failure")

                def factory(**kwargs):
                    connection = database.connect()
                    connections.append(connection)
                    return SimpleNamespace(
                        daemon_run_repository=DaemonRunRepository(connection),
                        close=close,
                    )

                for first in (True, False):
                    task = asyncio.create_task(serve(path, backend_factory=factory))
                    try:
                        async with asyncio.timeout(3):
                            while not await asyncio.to_thread(path.exists):
                                if task.done():
                                    await task
                                await asyncio.sleep(0.001)
                        runs = repository.list_recent()
                        current = runs[0]
                        assert current.status == "running"
                        assert repository.get("killed-predecessor").status == "unclean"
                        if not first:
                            assert repository.get(failed_id).status == "unclean"
                        reader, writer = await asyncio.open_unix_connection(path)
                        channel = _Channel(reader, writer)
                        await channel.send({"method": "shutdown_clean"})
                        assert (await channel.receive())["value"] == "stopping"
                        writer.close()
                        await writer.wait_closed()
                        if first:
                            with pytest.raises(RuntimeError, match="injected cleanup failure"):
                                await task
                            failed_id = current.id
                            assert repository.get(failed_id).status == "stopping"
                        else:
                            await task
                            stopped = repository.get(current.id)
                            assert stopped.status == "stopped"
                            assert stopped.stop_kind == "clean"
                            assert stopped.details["cleanup_error"] is None
                        assert connections[-1].closed
                    finally:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)


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
