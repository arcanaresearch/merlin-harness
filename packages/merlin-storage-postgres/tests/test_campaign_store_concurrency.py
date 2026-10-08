from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from merlin_storage_postgres import Database, DatabaseConfig, MigrationRunner, SessionRepository
from merlin_storage_postgres.campaign_store import CampaignStore
from merlin_storage_postgres.scheduler_store import DurableSchedulerStore


class _Transaction:
    def __init__(self, connection: _OverlapRejectingConnection) -> None:
        self.connection = connection

    def __enter__(self) -> None:
        with self.connection.guard:
            if self.connection.transaction_active:
                raise RuntimeError("concurrent transaction entered")
            self.connection.transaction_active = True
        self.connection.first_transaction_started.set()
        time.sleep(0.02)

    def __exit__(self, *_args: Any) -> None:
        with self.connection.guard:
            self.connection.transaction_active = False


class _Cursor:
    def fetchone(self) -> dict[str, Any]:
        return {
            "id": "campaign/test",
            "session_id": "session/test",
            "schedule_id": None,
            "status": "prepared",
            "event_count": 0,
            "last_event_digest": "0" * 64,
        }


class _OverlapRejectingConnection:
    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.transaction_active = False
        self.first_transaction_started = threading.Event()

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def execute(self, *_args: Any, **_kwargs: Any) -> _Cursor:
        return _Cursor()


def test_campaign_store_serializes_transactions_across_worker_threads() -> None:
    connection = _OverlapRejectingConnection()
    first_store = CampaignStore(connection)
    replacement_store = CampaignStore(connection)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            first_store.prepare,
            "campaign/test",
            "session/test",
            {"objective": "test"},
            "sha256:test",
        )
        assert connection.first_transaction_started.wait(timeout=1)
        second = executor.submit(
            replacement_store.append_lead,
            "campaign/test",
            0,
            {"status": "monitoring"},
        )

        assert first.result(timeout=1)["status"] == "prepared"
        assert second.result(timeout=1)["id"] == "campaign/test"


def test_campaign_and_scheduler_stores_share_the_connection_operation_lock() -> None:
    connection = _OverlapRejectingConnection()
    campaigns = CampaignStore(connection)
    scheduler = DurableSchedulerStore(connection)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            campaigns.prepare,
            "campaign/test",
            "session/test",
            {"objective": "test"},
            "sha256:test",
        )
        assert connection.first_transaction_started.wait(timeout=1)
        second = executor.submit(
            scheduler.create_schedule,
            "session/test",
            {"nodes": ["worker"]},
            {},
            ["worker"],
            ready_nodes=["worker"],
            schedule_id="schedule/test",
        )

        assert first.result(timeout=1)["status"] == "prepared"
        assert second.result(timeout=1) == "schedule/test"


def test_database_connection_prevents_cross_repository_schedule_rollback(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("serialized connection")
            scheduler = DurableSchedulerStore(connection)
            transaction_started = threading.Event()
            schedule_id = "197b4754-1510-4e9e-876e-bc555ee36a7f"

            def rollback_unrelated_transaction() -> None:
                try:
                    with connection.transaction():
                        transaction_started.set()
                        time.sleep(0.1)
                        raise RuntimeError("force outer rollback")
                except RuntimeError:
                    pass

            with ThreadPoolExecutor(max_workers=2) as executor:
                outer = executor.submit(rollback_unrelated_transaction)
                assert transaction_started.wait(timeout=1)
                created = executor.submit(
                    scheduler.create_schedule,
                    str(session.id),
                    {"nodes": ["worker"]},
                    {},
                    ["worker"],
                    ready_nodes=["worker"],
                    schedule_id=schedule_id,
                )
                outer.result(timeout=2)
                assert created.result(timeout=2) == schedule_id

            assert scheduler.load_schedule(schedule_id) is not None
