from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from typing import Any


@dataclass(frozen=True, slots=True)
class Migration:
    owner: str
    version: int
    sql: str
    checksum: str


DEFAULT_OWNER = "merlin-storage-postgres"
LEGACY_OWNER = "redpen-storage-postgres"


def load_migrations(owner: str = DEFAULT_OWNER) -> tuple[Migration, ...]:
    directory = files("merlin_storage_postgres").joinpath("sql")
    migrations = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.name.endswith(".sql"):
            continue
        version = int(path.name.split("_", 1)[0])
        sql = path.read_text(encoding="utf-8")
        migrations.append(Migration(owner, version, sql, sha256(sql.encode()).hexdigest()))
    return tuple(migrations)


class MigrationRunner:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def apply(self, migrations: Iterable[Migration] | None = None) -> None:
        selected = tuple(load_migrations() if migrations is None else migrations)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS public.plugin_migrations (
                    owner text NOT NULL,
                    version integer NOT NULL,
                    checksum text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (owner, version)
                )
                """
            )
            cursor.execute("SELECT owner, version, checksum FROM public.plugin_migrations")
            applied = {(row["owner"], row["version"]): row["checksum"] for row in cursor.fetchall()}
            for migration in selected:
                key = (migration.owner, migration.version)
                legacy_key = (LEGACY_OWNER, migration.version)
                if (
                    key not in applied
                    and migration.owner == DEFAULT_OWNER
                    and legacy_key in applied
                ):
                    cursor.execute(
                        "UPDATE public.plugin_migrations SET owner = %s, checksum = %s "
                        "WHERE owner = %s AND version = %s",
                        (migration.owner, migration.checksum, LEGACY_OWNER, migration.version),
                    )
                    applied[key] = migration.checksum
                if key in applied:
                    if applied[key] != migration.checksum:
                        raise RuntimeError(
                            f"migration checksum changed: {migration.owner} {migration.version}"
                        )
                    continue
                cursor.execute(migration.sql)
                cursor.execute(
                    "INSERT INTO public.plugin_migrations "
                    "(owner, version, checksum) VALUES (%s, %s, %s)",
                    (migration.owner, migration.version, migration.checksum),
                )
