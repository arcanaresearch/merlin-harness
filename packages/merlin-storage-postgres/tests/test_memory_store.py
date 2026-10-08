from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from merlin_memory import (
    Applicability,
    BundleProvenance,
    MemoryRecord,
    ReviewedSkill,
    SealedLesson,
    build_bundle,
)
from merlin_storage_postgres import (
    Database,
    DatabaseConfig,
    MigrationRunner,
    PortableMemoryRepository,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def provenance() -> BundleProvenance:
    return BundleProvenance(
        source="merlin://repository/acme",
        exported_by="operator/exporter",
        reviewed_by=("operator/reviewer",),
        created_at=NOW,
    )


def memory_record(
    id: str,
    *,
    repositories: tuple[str, ...] = (),
    targets: tuple[str, ...] = (),
    providers: tuple[str, ...] = (),
    operators: tuple[str, ...] = (),
    scopes: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
    expires_at: datetime | None = None,
) -> MemoryRecord:
    return MemoryRecord(
        id=id,
        payload=ReviewedSkill(name=id, instructions=f"Use reviewed memory {id}."),
        provenance=provenance(),
        applicability=Applicability(
            repository_ids=repositories,
            target_ids=targets,
            provider_ids=providers,
            operator_ids=operators,
            scope_ids=scopes,
            tags=tags,
        ),
        expires_at=expires_at,
    )


def test_pg0_portable_memory_review_scope_turn_audit_and_delete(tmp_path: Path) -> None:
    exact = memory_record(
        "skill/exact",
        repositories=("repo/acme",),
        targets=("target/chat",),
        providers=("provider/attacker",),
        operators=("operator/alice",),
        scopes=("scope/security",),
        tags=("ai-chat",),
        expires_at=NOW + timedelta(days=30),
    )
    other_target = memory_record("skill/other", targets=("target/other",))
    rejected = MemoryRecord(
        id="lesson/rejected",
        payload=SealedLesson(
            statement="An unsupported lesson.", evidence_references=("evidence/1",)
        ),
        provenance=provenance(),
    )
    bundle = build_bundle("bundle/import-1", provenance(), (exact, other_target, rejected))

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            memory = PortableMemoryRepository(connection)
            memory.quarantine(bundle, operator_id="operator/importer")

            # A partial/invalid review is atomic: the valid row remains quarantined.
            with pytest.raises(ValueError, match="invalid or incomplete"):
                memory.accept(
                    bundle.bundle_id,
                    (exact.id, "missing"),
                    operator_id="operator/alice",
                )
            memory.accept(
                bundle.bundle_id, (exact.id, other_target.id), operator_id="operator/alice"
            )
            memory.reject(
                bundle.bundle_id,
                (rejected.id,),
                operator_id="operator/alice",
                reason="not supported by reviewed evidence",
            )

            active = memory.active(
                repository_id="repo/acme",
                target_id="target/chat",
                provider_id="provider/attacker",
                operator_id="operator/alice",
                scope_ids=("scope/security",),
                tags=("ai-chat",),
                at=NOW,
            )
            assert [record.id for record in active] == [exact.id]
            assert (
                memory.active(
                    repository_id="repo/wrong",
                    target_id="target/chat",
                    provider_id="provider/attacker",
                    operator_id="operator/alice",
                    scope_ids=("scope/security",),
                    tags=("ai-chat",),
                    at=NOW,
                )
                == ()
            )
            with pytest.raises(ValueError, match="scope are required"):
                memory.active(
                    repository_id="",
                    target_id="target/chat",
                    provider_id="provider/attacker",
                    operator_id="operator/alice",
                    at=NOW,
                )
            assert (
                memory.active(
                    repository_id="repo/acme",
                    target_id="target/chat",
                    provider_id="provider/attacker",
                    operator_id="operator/alice",
                    scope_ids=("scope/security",),
                    tags=("ai-chat",),
                    at=NOW + timedelta(days=31),
                )
                == ()
            )

            used = memory.record_turn_use(
                "session-1",
                "turn-1",
                (exact.id,),
                repository_id="repo/acme",
                target_id="target/chat",
                provider_id="provider/attacker",
                operator_id="operator/alice",
                scope_ids=("scope/security",),
                tags=("ai-chat",),
                at=NOW,
            )
            assert [record.id for record in used] == [exact.id]
            assert memory.turn_uses("session-1", "turn-1") == (exact.id,)
            with pytest.raises(ValueError, match="inactive or inapplicable"):
                memory.record_turn_use(
                    "session-1",
                    "turn-2",
                    (rejected.id,),
                    repository_id="repo/acme",
                    target_id="target/chat",
                    provider_id="provider/attacker",
                    operator_id="operator/alice",
                    at=NOW,
                )

            memory.delete(
                bundle.bundle_id,
                (exact.id,),
                operator_id="operator/alice",
                reason="operator removed imported memory",
            )
            assert memory.turn_uses("session-1", "turn-1") == (exact.id,)
            audit = connection.execute(
                """SELECT event FROM merlin_storage.portable_memory_audit
                   WHERE record_id = %s ORDER BY id""",
                (exact.id,),
            ).fetchall()
            assert [row["event"] for row in audit] == [
                "quarantined",
                "accepted",
                "used_in_turn",
                "deleted",
            ]


def test_pg0_promoted_memory_scope_expiry_and_use_survive_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres"
    exact = memory_record(
        "skill/restart-exact",
        repositories=("repo/acme",),
        targets=("target/chat",),
        providers=("provider/attacker",),
        operators=("operator/alice",),
        scopes=("scope/security",),
        tags=("ai-chat",),
        expires_at=NOW + timedelta(days=30),
    )
    expired = memory_record(
        "skill/restart-expired",
        targets=("target/chat",),
        expires_at=NOW - timedelta(seconds=1),
    )
    other_target = memory_record("skill/restart-other", targets=("target/other",))
    bundle = build_bundle("bundle/restart-memory", provenance(), (exact, expired, other_target))

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            memory = PortableMemoryRepository(connection)
            memory.quarantine(bundle, operator_id="operator/importer")
            memory.accept(
                bundle.bundle_id,
                (exact.id, expired.id, other_target.id),
                operator_id="operator/alice",
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            memory = PortableMemoryRepository(connection)
            active = memory.active(
                repository_id="repo/acme",
                target_id="target/chat",
                provider_id="provider/attacker",
                operator_id="operator/alice",
                scope_ids=("scope/security",),
                tags=("ai-chat",),
                at=NOW,
            )
            assert [record.id for record in active] == [exact.id]
            memory.record_turn_use(
                "session-restart",
                "turn-restart",
                (exact.id,),
                repository_id="repo/acme",
                target_id="target/chat",
                provider_id="provider/attacker",
                operator_id="operator/alice",
                scope_ids=("scope/security",),
                tags=("ai-chat",),
                at=NOW,
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            memory = PortableMemoryRepository(connection)
            assert memory.turn_uses("session-restart", "turn-restart") == (exact.id,)
            events = connection.execute(
                """SELECT event FROM merlin_storage.portable_memory_audit
                   WHERE record_id = %s ORDER BY id""",
                (exact.id,),
            ).fetchall()
            assert [row["event"] for row in events] == [
                "quarantined",
                "accepted",
                "used_in_turn",
            ]
