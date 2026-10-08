from __future__ import annotations

from typing import Any

from merlin_memory import VerifiedInteractionLesson
from psycopg.types.json import Jsonb


class InteractionLessonRepository:
    """Immutable persistence and exact-target retrieval for verified UI recipes."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def save(self, lesson: VerifiedInteractionLesson) -> VerifiedInteractionLesson:
        document = lesson.model_dump(mode="json")
        with self.connection.transaction():
            existing = self.connection.execute(
                "SELECT document FROM merlin_storage.interaction_lessons WHERE id = %s",
                (lesson.id,),
            ).fetchone()
            if existing is not None:
                restored = VerifiedInteractionLesson.model_validate(existing["document"])
                if restored.digest != lesson.digest:
                    raise ValueError("interaction lesson id collision")
                return restored
            self.connection.execute(
                """INSERT INTO merlin_storage.interaction_lessons
                       (id, target_id, interface_key, document, digest, verified_by,
                        verified_at, source_campaign_id, source_task_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    lesson.id,
                    lesson.target_id,
                    lesson.interface_key,
                    Jsonb(document),
                    lesson.digest,
                    lesson.verified_by,
                    lesson.verified_at,
                    lesson.source_campaign_id,
                    lesson.source_task_id,
                ),
            )
        return lesson

    def for_target(
        self, target_id: str, *, interface_key: str | None = None, limit: int = 20
    ) -> tuple[VerifiedInteractionLesson, ...]:
        if not target_id.strip():
            raise ValueError("target id is required")
        if limit < 1 or limit > 100:
            raise ValueError("interaction lesson limit must be between 1 and 100")
        query = """SELECT document FROM merlin_storage.interaction_lessons
                   WHERE target_id = %s"""
        parameters: list[object] = [target_id]
        if interface_key is not None:
            query += " AND interface_key = %s"
            parameters.append(interface_key)
        query += " ORDER BY verified_at DESC, id LIMIT %s"
        parameters.append(limit)
        rows = self.connection.execute(query, tuple(parameters)).fetchall()
        return tuple(VerifiedInteractionLesson.model_validate(row["document"]) for row in rows)
