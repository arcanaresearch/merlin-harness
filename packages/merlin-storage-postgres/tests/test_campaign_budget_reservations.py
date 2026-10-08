from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest
from merlin_storage_postgres import (
    CampaignStore,
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    SessionRepository,
)
from psycopg.rows import dict_row


def test_campaign_budget_reservation_is_atomic_idempotent_and_restart_safe(tmp_path) -> None:
    data_dir = tmp_path / "postgres"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("budget reservation")
            CampaignStore(connection).prepare(
                "campaign/budget",
                str(session.id),
                {
                    "manifest": {
                        "budget": {
                            "max_turns": 2,
                            "max_tokens": 100,
                            "max_seconds": 120,
                            "max_cost_usd": 1.0,
                        }
                    }
                },
                "sha256:budget",
            )
            connection.commit()
        url = database.url
        barrier = Barrier(2)

        def reserve(contender: tuple[str, str]):
            identity, actor_kind = contender
            with psycopg.connect(url, row_factory=dict_row) as connection:
                barrier.wait()
                return CampaignStore(connection).reserve_budget(
                    "campaign/budget",
                    identity,
                    actor_kind=actor_kind,
                    tokens=70,
                    cost_usd=0.7,
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(reserve, (("lead-request", "lead"), ("worker-request", "worker"))))

        assert sorted(row["state"] for row in rows) == ["active", "rejected"]
        assert {row["actor_kind"] for row in rows} == {"lead", "worker"}
        admitted = next(row for row in rows if row["state"] == "active")
        with database.connect() as connection:
            store = CampaignStore(connection)
            assert store.remaining_budget_seconds("campaign/budget") == 120
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"nodes": ["worker"]},
                {"completed": []},
                ["worker"],
                ready_nodes=["worker"],
            )
            store.begin_execution("campaign/budget", "sha256:budget", schedule_id)
            remaining = store.remaining_budget_seconds("campaign/budget")
            assert remaining is not None and 0 < remaining <= 120
            settled = store.settle_budget(
                "campaign/budget", admitted["reservation_id"], tokens=50, cost_usd=0.5
            )
            assert settled["state"] == "settled"
            assert (
                store.settle_budget(
                    "campaign/budget",
                    admitted["reservation_id"],
                    tokens=50,
                    cost_usd=0.5,
                )["state"]
                == "settled"
            )
            partial = store.reserve_budget(
                "campaign/budget",
                "request-partial",
                actor_kind="worker",
                tokens=80,
                cost_usd=0.8,
                allow_partial_tokens=True,
            )
            assert partial["state"] == "active"
            assert partial["reserved_tokens"] == 50
            assert float(partial["reserved_cost_usd"]) == pytest.approx(0.5)
            assert store.release_budget("campaign/budget", "request-partial")["state"] == (
                "released"
            )
            replacement = store.reserve_budget(
                "campaign/budget",
                "request-3",
                actor_kind="lead",
                tokens=50,
                cost_usd=0.5,
            )
            assert replacement["state"] == "active"

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            store = CampaignStore(connection)
            assert (
                store.reserve_budget(
                    "campaign/budget",
                    "request-3",
                    actor_kind="lead",
                    tokens=50,
                    cost_usd=0.5,
                )["state"]
                == "active"
            )
            assert store.release_budget("campaign/budget", "request-3")["state"] == "released"
            assert (
                store.reserve_budget(
                    "campaign/budget",
                    "request-3",
                    actor_kind="lead",
                    tokens=50,
                    cost_usd=0.5,
                )["state"]
                == "active"
            )
            assert store.release_active_budget("campaign/budget") == 1
            assert (
                store.reserve_budget(
                    "campaign/budget",
                    "request-3",
                    actor_kind="worker",
                    tokens=50,
                    cost_usd=0.5,
                )["state"]
                == "active"
            )


def test_turns_never_gate_a_reservation(tmp_path) -> None:
    """A turn-count budget must not reject a reservation.

    Turns are model inference round-trips, not a user-meaningful resource cap;
    tokens, cost, and seconds are the real limits. This locks in the removal of
    turn gating so it cannot silently regress.
    """
    data_dir = tmp_path / "postgres"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("turns never gate")
            CampaignStore(connection).prepare(
                "campaign/turns-never-gate",
                str(session.id),
                {
                    "manifest": {
                        "budget": {
                            "max_turns": 1,
                            "max_tokens": 1_000_000,
                            "max_seconds": 120,
                            "max_cost_usd": 100.0,
                        }
                    }
                },
                "sha256:turns-never-gate",
            )
            connection.commit()
        with database.connect() as connection:
            store = CampaignStore(connection)
            reservation = store.reserve_budget(
                "campaign/turns-never-gate",
                "recon/provider/1",
                actor_kind="worker",
                tokens=50,
                turns=100,
                cost_usd=0.5,
            )
            assert reservation["state"] == "active"


def test_turn_only_reservation_settles_reported_token_and_cost_usage(tmp_path) -> None:
    """A turn-only reservation (no token/cost cap) must settle real usage.

    Reconnaissance workers are turn-budgeted but not token/cost-budgeted, so their
    reservation is created with reserved_tokens=0 and reserved_cost_usd=0. Settling
    their actual provider usage must not be rejected as exceeding the reservation.
    """
    data_dir = tmp_path / "postgres"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("turn-only budget")
            CampaignStore(connection).prepare(
                "campaign/turn-only",
                str(session.id),
                {"manifest": {"budget": {"max_turns": 2}}},
                "sha256:turn-only",
            )
            connection.commit()
        with database.connect() as connection:
            store = CampaignStore(connection)
            reservation = store.reserve_budget(
                "campaign/turn-only",
                "recon/provider/1",
                actor_kind="worker",
                tokens=0,
                turns=1,
                cost_usd=0.0,
                allow_partial_tokens=True,
            )
            assert reservation["state"] == "active"
            assert reservation["reserved_tokens"] == 0
            settled = store.settle_budget(
                "campaign/turn-only",
                "recon/provider/1",
                tokens=1542,
                turns=1,
                cost_usd=0.0031,
            )
            assert settled["state"] == "settled"
            assert settled["settled_tokens"] == 1542
            assert float(settled["settled_cost_usd"]) == pytest.approx(0.0031)
