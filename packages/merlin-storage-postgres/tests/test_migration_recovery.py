from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
from merlin_storage_postgres import Database, DatabaseConfig, Migration, MigrationRunner
from psycopg.errors import SyntaxError as PostgresSyntaxError


def _migration(version: int, sql: str) -> Migration:
    return Migration("acceptance-upgrade", version, sql, sha256(sql.encode()).hexdigest())


def test_failed_upgrade_rolls_back_without_losing_prior_state(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres"
    baseline = _migration(
        1,
        "CREATE TABLE public.acceptance_state (value text NOT NULL); "
        "INSERT INTO public.acceptance_state VALUES ('preserved')",
    )
    additive = _migration(2, "ALTER TABLE public.acceptance_state ADD COLUMN revision integer")
    broken = _migration(3, "ALTER TABLE public.acceptance_state ADD COLUMN")

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            runner = MigrationRunner(connection)
            runner.apply((baseline,))
            with pytest.raises(PostgresSyntaxError):
                runner.apply((baseline, additive, broken))

            assert (
                connection.execute("SELECT value FROM public.acceptance_state").fetchone()["value"]
                == "preserved"
            )
            columns = connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'acceptance_state'"
            ).fetchall()
            assert {row["column_name"] for row in columns} == {"value"}
            applied = connection.execute(
                "SELECT version FROM public.plugin_migrations "
                "WHERE owner = 'acceptance-upgrade' ORDER BY version"
            ).fetchall()
            assert [row["version"] for row in applied] == [1]

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            runner = MigrationRunner(connection)
            repaired = _migration(
                3, "UPDATE public.acceptance_state SET revision = 2 WHERE value = 'preserved'"
            )
            runner.apply((baseline, additive, repaired))
            assert connection.execute(
                "SELECT value, revision FROM public.acceptance_state"
            ).fetchone() == {"value": "preserved", "revision": 2}

            changed_baseline = _migration(1, "SELECT 1")
            with pytest.raises(RuntimeError, match="migration checksum changed"):
                runner.apply((changed_baseline, additive, repaired))
            assert connection.execute(
                "SELECT value, revision FROM public.acceptance_state"
            ).fetchone() == {"value": "preserved", "revision": 2}
