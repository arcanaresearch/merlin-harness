from datetime import UTC, datetime
from pathlib import Path

import pytest
from merlin_memory import InteractionStep, VerifiedInteractionLesson
from merlin_storage_postgres import (
    Database,
    DatabaseConfig,
    InteractionLessonRepository,
    MigrationRunner,
)


def lesson(
    id: str, target_id: str, *, outcome: str = "Assistant response appeared"
) -> VerifiedInteractionLesson:
    return VerifiedInteractionLesson(
        id=id,
        target_id=target_id,
        interface_key="support-chat",
        steps=(
            InteractionStep(action="fill", locator="#message", value="{{message}}"),
            InteractionStep(action="press", value="Enter"),
            InteractionStep(action="assert_visible", locator=".assistant:last-child"),
        ),
        outcome=outcome,
        evidence_references=("evidence://verified-send",),
        verified_by="validator:browser-outcome",
        verified_at=datetime(2026, 8, 28, tzinfo=UTC),
        source_campaign_id="campaign-1",
        source_task_id="recon-1",
    )


def test_verified_interactions_persist_and_are_exact_target_scoped(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            repository = InteractionLessonRepository(connection)
            wanted = repository.save(lesson("lesson/wanted", "target/plunder/refund-l1"))
            repository.save(lesson("lesson/other", "target/plunder/refund-l2"))

            assert repository.save(wanted) == wanted
            assert repository.for_target("target/plunder/refund-l1") == (wanted,)
            assert (
                repository.for_target("target/plunder/refund-l1", interface_key="different-chat")
                == ()
            )


def test_interaction_id_replay_cannot_change_immutable_content(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            repository = InteractionLessonRepository(connection)
            repository.save(lesson("lesson/stable", "target/a"))
            with pytest.raises(ValueError, match="id collision"):
                repository.save(
                    lesson("lesson/stable", "target/a", outcome="A different claimed result")
                )
