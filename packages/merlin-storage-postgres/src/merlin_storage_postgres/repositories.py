from __future__ import annotations

import json
from collections.abc import Sequence
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from merlin_contracts import (
    ChatMessage,
    CompactionRecord,
    ContextProjection,
    EventRecord,
    MessageLineage,
    MessageRecord,
    PackOutcome,
    PackRun,
    PackRunStatus,
    PackTurn,
    SessionRecord,
    TargetProfile,
)
from psycopg.types.json import Jsonb

from .sanitize import escape_nul


def _record(model: Any, row: dict[str, Any]) -> Any:
    values = dict(row)
    for field in ("id", "session_id", "run_id"):
        if field in values:
            values[field] = str(values[field])
    return model.model_validate(values)


class SessionRepository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def create(self, title: str | None = None, *, session_id: UUID | None = None) -> SessionRecord:
        row = self.connection.execute(
            """INSERT INTO merlin_storage.sessions (id, title) VALUES (%s, %s)
               RETURNING id, title, created_at, updated_at""",
            (session_id or uuid4(), title),
        ).fetchone()
        self.connection.commit()
        return _record(SessionRecord, row)

    def get(self, session_id: UUID) -> SessionRecord | None:
        row = self.connection.execute(
            "SELECT id, title, created_at, updated_at FROM merlin_storage.sessions WHERE id = %s",
            (session_id,),
        ).fetchone()
        return _record(SessionRecord, row) if row else None

    def list(self, *, limit: int = 50, offset: int = 0, query: str = "") -> list[SessionRecord]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("session page requires limit 1-100 and non-negative offset")
        pattern = f"%{query.strip()}%"
        rows = self.connection.execute(
            """SELECT id, title, created_at, updated_at FROM merlin_storage.sessions
               WHERE (%s = '' OR title ILIKE %s OR id::text ILIKE %s)
               ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s""",
            (query.strip(), pattern, pattern, limit, offset),
        ).fetchall()
        return [_record(SessionRecord, row) for row in rows]

    def count(self, *, query: str = "") -> int:
        pattern = f"%{query.strip()}%"
        row = self.connection.execute(
            """SELECT count(*) AS value FROM merlin_storage.sessions
               WHERE (%s = '' OR title ILIKE %s OR id::text ILIKE %s)""",
            (query.strip(), pattern, pattern),
        ).fetchone()
        return int(row["value"])

    def list_resumable(
        self, *, limit: int = 50, offset: int = 0, query: str = ""
    ) -> list[SessionRecord]:
        """List sessions that contain a user-visible conversation."""
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("session page requires limit 1-100 and non-negative offset")
        pattern = f"%{query.strip()}%"
        rows = self.connection.execute(
            """SELECT session.id,
                      COALESCE(NULLIF(session.title, ''), left(first_user.content, 120)) AS title,
                      session.created_at, session.updated_at
               FROM merlin_storage.sessions AS session
               JOIN LATERAL (
                   SELECT message.content
                   FROM merlin_storage.messages AS message
                   WHERE message.session_id = session.id
                     AND message.role = 'user'
                     AND btrim(message.content) <> ''
                   ORDER BY message.sequence
                   LIMIT 1
               ) AS first_user ON true
               WHERE (%s = '' OR session.title ILIKE %s
                      OR first_user.content ILIKE %s OR session.id::text ILIKE %s)
               ORDER BY session.updated_at DESC, session.id DESC LIMIT %s OFFSET %s""",
            (query.strip(), pattern, pattern, pattern, limit, offset),
        ).fetchall()
        return [_record(SessionRecord, row) for row in rows]

    def count_resumable(self, *, query: str = "") -> int:
        pattern = f"%{query.strip()}%"
        row = self.connection.execute(
            """SELECT count(*) AS value
               FROM merlin_storage.sessions AS session
               WHERE EXISTS (
                   SELECT 1 FROM merlin_storage.messages AS message
                   WHERE message.session_id = session.id
                     AND message.role = 'user'
                     AND btrim(message.content) <> ''
               )
               AND (%s = '' OR session.title ILIKE %s
                    OR EXISTS (
                        SELECT 1 FROM merlin_storage.messages AS message
                        WHERE message.session_id = session.id
                          AND message.role = 'user'
                          AND message.content ILIKE %s
                    ) OR session.id::text ILIKE %s)""",
            (query.strip(), pattern, pattern, pattern),
        ).fetchone()
        return int(row["value"])

    def projection_head(self, session_id: UUID | str) -> int | None:
        row = self.connection.execute(
            "SELECT projection_head FROM merlin_storage.sessions WHERE id = %s", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"session not found: {session_id}")
        return row["projection_head"]

    def rewind(self, session_id: UUID | str, sequence: int) -> None:
        if sequence < 0:
            raise ValueError("rewind sequence must be non-negative")
        maximum = self.connection.execute(
            """SELECT COALESCE(max(sequence), 0) AS value
               FROM merlin_storage.messages WHERE session_id = %s""",
            (session_id,),
        ).fetchone()["value"]
        if sequence > maximum:
            raise ValueError(f"rewind sequence {sequence} exceeds transcript head {maximum}")
        self.connection.execute(
            """UPDATE merlin_storage.sessions
               SET projection_head = %s, updated_at = now() WHERE id = %s""",
            (sequence, session_id),
        )
        self.connection.commit()

    def restore(self, session_id: UUID | str) -> None:
        self.connection.execute(
            """UPDATE merlin_storage.sessions
               SET projection_head = NULL, updated_at = now() WHERE id = %s""",
            (session_id,),
        )
        self.connection.commit()

    def branch(self, session_id: UUID | str, sequence: int) -> SessionRecord:
        if sequence < 0:
            raise ValueError("branch sequence must be non-negative")
        source = self.get(session_id)
        if source is None:
            raise KeyError(f"session not found: {session_id}")
        maximum = self.connection.execute(
            """SELECT COALESCE(max(sequence), 0) AS value
               FROM merlin_storage.messages WHERE session_id = %s""",
            (session_id,),
        ).fetchone()["value"]
        if sequence > maximum:
            raise ValueError(f"branch sequence {sequence} exceeds transcript head {maximum}")
        branch_id = uuid4()
        with self.connection.transaction():
            row = self.connection.execute(
                """INSERT INTO merlin_storage.sessions
                       (id, title, parent_session_id, forked_from_sequence)
                   VALUES (%s, %s, %s, %s)
                   RETURNING id, title, created_at, updated_at""",
                (branch_id, source.title, session_id, sequence),
            ).fetchone()
            self.connection.execute(
                """INSERT INTO merlin_storage.messages
                       (id, session_id, role, content, name, tool_call_id, tool_calls,
                        reasoning_content, source_message_id)
                   SELECT gen_random_uuid(), %s, role, content, name, tool_call_id, tool_calls,
                          reasoning_content, source_message_id
                   FROM merlin_storage.messages
                   WHERE session_id = %s AND sequence <= %s ORDER BY sequence""",
                (branch_id, session_id, sequence),
            )
        return _record(SessionRecord, row)


class MessageRepository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def append(
        self,
        session_id: UUID,
        role: str,
        content: str,
        *,
        message_id: UUID | None = None,
        name: str | None = None,
        tool_call_id: str | None = None,
        tool_calls: list[dict[str, object]] | None = None,
        reasoning_content: str | None = None,
    ) -> MessageRecord:
        inserted_id = message_id or uuid4()
        with self.connection.transaction():
            row = self.connection.execute(
                """INSERT INTO merlin_storage.messages
                       (id, session_id, role, content, name, tool_call_id, tool_calls,
                        reasoning_content, source_message_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id, session_id, sequence, role, content, created_at, tool_call_id""",
                (
                    inserted_id,
                    session_id,
                    role,
                    escape_nul(content),
                    escape_nul(name),
                    escape_nul(tool_call_id),
                    Jsonb(escape_nul(tool_calls or [])),
                    escape_nul(reasoning_content),
                    inserted_id,
                ),
            ).fetchone()
            self.connection.execute(
                "UPDATE merlin_storage.sessions SET updated_at = now() WHERE id = %s",
                (session_id,),
            )
        return _record(MessageRecord, row)

    def list(self, session_id: UUID) -> list[MessageRecord]:
        rows = self.connection.execute(
            """SELECT id, session_id, sequence, role, content, created_at, tool_call_id
               FROM merlin_storage.messages
               WHERE session_id = %s ORDER BY sequence""",
            (session_id,),
        ).fetchall()
        return [_record(MessageRecord, row) for row in rows]

    def list_chat(self, session_id: UUID | str) -> list[ChatMessage]:
        head = self.connection.execute(
            "SELECT projection_head FROM merlin_storage.sessions WHERE id = %s", (session_id,)
        ).fetchone()
        projection_head = head["projection_head"] if head else None
        rows = self.connection.execute(
            """SELECT role, content, name, tool_call_id, tool_calls, reasoning_content
               FROM merlin_storage.messages
               WHERE session_id = %s AND sequence <= COALESCE(%s::bigint, sequence)
               ORDER BY sequence""",
            (session_id, projection_head),
        ).fetchall()
        return [ChatMessage.model_validate(row) for row in rows]

    def projection(self, session_id: UUID | str, identity: ChatMessage) -> ContextProjection:
        head_row = self.connection.execute(
            "SELECT projection_head FROM merlin_storage.sessions WHERE id = %s", (session_id,)
        ).fetchone()
        if head_row is None:
            raise KeyError(f"session not found: {session_id}")
        rows = self.connection.execute(
            """SELECT id, source_message_id, sequence, role, content, name, tool_call_id,
                      tool_calls, reasoning_content
               FROM merlin_storage.messages
               WHERE session_id = %s AND sequence <= COALESCE(%s::bigint, sequence)
               ORDER BY sequence""",
            (session_id, head_row["projection_head"]),
        ).fetchall()
        messages = tuple(
            ChatMessage.model_validate(
                {
                    key: row[key]
                    for key in (
                        "role",
                        "content",
                        "name",
                        "tool_call_id",
                        "tool_calls",
                        "reasoning_content",
                    )
                }
            )
            for row in rows
        )
        document = [message.model_dump(mode="json") for message in messages]
        digest = sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ContextProjection(
            session_id=str(session_id),
            identity=identity,
            messages=messages,
            lineage=tuple(
                MessageLineage(
                    sequence=row["sequence"],
                    message_id=str(row["id"]),
                    source_message_id=str(row["source_message_id"]),
                )
                for row in rows
            ),
            transcript_digest=digest,
            head_sequence=rows[-1]["sequence"] if rows else 0,
        )

    def clear(self, session_id: UUID | str) -> None:
        self.connection.execute(
            "DELETE FROM merlin_storage.messages WHERE session_id = %s", (session_id,)
        )
        self.connection.commit()

    def history_page(
        self,
        session_id: UUID | str,
        *,
        limit: int = 50,
        before: int | None = None,
        exclude_current_turn: bool = False,
    ) -> dict[str, Any]:
        """Read bounded original transcript rows using a stable sequence cursor."""
        if not 1 <= limit <= 100 or (before is not None and before < 1):
            raise ValueError("history requires limit 1-100 and a positive before cursor")
        rows = self.connection.execute(
            """WITH visible AS (
                   SELECT m.* FROM merlin_storage.messages m
                   JOIN merlin_storage.sessions s ON s.id = m.session_id
                   WHERE m.session_id = %s AND m.role <> 'system'
                     AND m.sequence <= COALESCE(s.projection_head, m.sequence)
               )
               SELECT sequence, id, role, content, name, reasoning_content, created_at
               FROM visible
               WHERE (%s::bigint IS NULL OR sequence < %s)
                 AND (NOT %s OR sequence < (
                     SELECT max(sequence) FROM visible WHERE role = 'user'
                 ))
               ORDER BY sequence DESC LIMIT %s""",
            (session_id, before, before, exclude_current_turn, limit + 1),
        ).fetchall()
        more = len(rows) > limit
        selected = list(reversed(rows[:limit]))
        return {
            "session_id": str(session_id),
            "messages": [
                {**row, "id": str(row["id"]), "created_at": row["created_at"].isoformat()}
                for row in selected
            ],
            "before": int(selected[0]["sequence"]) if selected else None,
            "has_more": more,
        }

    def provenance(self, session_id: UUID | str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT sequence, role, id, source_message_id
               FROM merlin_storage.messages
               WHERE session_id = %s ORDER BY sequence""",
            (session_id,),
        ).fetchall()
        return [
            {
                "sequence": int(row["sequence"]),
                "role": str(row["role"]),
                "message_id": str(row["id"]),
                "source_message_id": str(row["source_message_id"]),
            }
            for row in rows
        ]


class CompactionRepository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def save(self, record: CompactionRecord) -> CompactionRecord:
        row = self.connection.execute(
            """INSERT INTO merlin_storage.conversation_compactions
                   (id, session_id, source_start, source_end, source_digest, summary,
                    summary_digest, algorithm)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id
               RETURNING id, session_id, source_start, source_end, source_digest, summary,
                         summary_digest, algorithm, created_at""",
            (
                record.id,
                record.session_id,
                record.source_start,
                record.source_end,
                record.source_digest,
                record.summary,
                record.summary_digest,
                record.algorithm,
            ),
        ).fetchone()
        self.connection.commit()
        return CompactionRecord.model_validate({**row, "session_id": str(row["session_id"])})

    def list(self, session_id: UUID | str) -> list[CompactionRecord]:
        rows = self.connection.execute(
            """SELECT id, session_id, source_start, source_end, source_digest, summary,
                      summary_digest, algorithm, created_at
               FROM merlin_storage.conversation_compactions
               WHERE session_id = %s ORDER BY source_end DESC, created_at DESC""",
            (session_id,),
        ).fetchall()
        return [
            CompactionRecord.model_validate({**row, "session_id": str(row["session_id"])})
            for row in rows
        ]

    def list_lineage(self, session_id: UUID | str) -> list[CompactionRecord]:
        """List compactions visible through the active branch projection.

        Ancestor records remain owned by their source session. A branch therefore
        exposes provenance without copying summaries or manufacturing new record
        identities. Rewinds hide records whose source range is beyond the active
        projection head.
        """
        rows = self.connection.execute(
            """WITH RECURSIVE lineage AS (
                   SELECT id, parent_session_id, forked_from_sequence,
                          projection_head AS visible_through, 0 AS depth
                   FROM merlin_storage.sessions WHERE id = %s
                   UNION ALL
                   SELECT parent.id, parent.parent_session_id, parent.forked_from_sequence,
                          CASE
                              WHEN child.forked_from_sequence IS NULL
                                  THEN child.visible_through
                              WHEN child.visible_through IS NULL
                                  THEN child.forked_from_sequence
                              ELSE LEAST(child.visible_through, child.forked_from_sequence)
                          END AS visible_through,
                          child.depth + 1
                   FROM merlin_storage.sessions AS parent
                   JOIN lineage AS child ON parent.id = child.parent_session_id
               )
               SELECT compact.id, compact.session_id, compact.source_start,
                      compact.source_end, compact.source_digest, compact.summary,
                      compact.summary_digest, compact.algorithm, compact.created_at
               FROM lineage
               JOIN merlin_storage.conversation_compactions AS compact
                 ON compact.session_id = lineage.id
               WHERE lineage.visible_through IS NULL
                  OR compact.source_end <= lineage.visible_through
               ORDER BY lineage.depth, compact.source_end DESC, compact.created_at DESC""",
            (session_id,),
        ).fetchall()
        return [
            CompactionRecord.model_validate({**row, "session_id": str(row["session_id"])})
            for row in rows
        ]


class EventRepository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def append(self, session_id: UUID, event_type: str, payload: dict[str, Any]) -> EventRecord:
        row = self.connection.execute(
            """INSERT INTO merlin_storage.events (id, session_id, type, payload)
               VALUES (%s, %s, %s, %s)
               RETURNING id, sequence, session_id, type, payload, created_at""",
            (uuid4(), session_id, event_type, Jsonb(payload)),
        ).fetchone()
        self.connection.commit()
        return _record(EventRecord, row)

    def list(self, session_id: UUID, *, after: int = 0) -> list[EventRecord]:
        rows = self.connection.execute(
            """SELECT id, sequence, session_id, type, payload, created_at
               FROM merlin_storage.events
               WHERE session_id = %s AND sequence > %s ORDER BY sequence""",
            (session_id, after),
        ).fetchall()
        return [_record(EventRecord, row) for row in rows]


class TargetProfileRepository:
    """Persists non-secret AI-target connection settings."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def save(self, profile: TargetProfile) -> TargetProfile:
        row = self.connection.execute(
            """INSERT INTO merlin_storage.target_profiles
                   (id, title, base_url, model, browser_session_policy)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   title = EXCLUDED.title,
                   base_url = EXCLUDED.base_url,
                   model = EXCLUDED.model,
                   browser_session_policy = EXCLUDED.browser_session_policy
               RETURNING id, title, base_url, model, browser_session_policy""",
            (
                profile.id,
                profile.title,
                profile.base_url,
                profile.model,
                profile.browser_session_policy,
            ),
        ).fetchone()
        self.connection.commit()
        return _record(TargetProfile, row)

    def get(self, profile_id: str) -> TargetProfile | None:
        row = self.connection.execute(
            """SELECT id, title, base_url, model, browser_session_policy
               FROM merlin_storage.target_profiles WHERE id = %s""",
            (profile_id,),
        ).fetchone()
        return _record(TargetProfile, row) if row else None

    def list(self) -> list[TargetProfile]:
        rows = self.connection.execute(
            """SELECT id, title, base_url, model, browser_session_policy
               FROM merlin_storage.target_profiles ORDER BY title, id"""
        ).fetchall()
        return [_record(TargetProfile, row) for row in rows]

    def remove(self, profile_id: str) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM merlin_storage.target_profiles WHERE id = %s", (profile_id,)
        )
        self.connection.commit()
        return cursor.rowcount > 0


class PackRunRepository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    @staticmethod
    def _columns() -> str:
        return (
            "id, session_id, pack_id, case_id, target_profile_id, status, "
            "started_at, completed_at, outcome"
        )

    def create(self, run: PackRun) -> PackRun:
        row = self.connection.execute(
            f"""INSERT INTO merlin_storage.pack_runs
                    ({self._columns()})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {self._columns()}""",
            (
                run.id,
                run.session_id,
                run.pack_id,
                run.case_id,
                run.target_profile_id,
                run.status,
                run.started_at,
                run.completed_at,
                Jsonb(run.outcome.model_dump()) if run.outcome else None,
            ),
        ).fetchone()
        self.connection.commit()
        return _record(PackRun, row)

    def get(self, run_id: UUID | str) -> PackRun | None:
        row = self.connection.execute(
            f"SELECT {self._columns()} FROM merlin_storage.pack_runs WHERE id = %s",
            (run_id,),
        ).fetchone()
        return _record(PackRun, row) if row else None

    def list(self, session_id: UUID | str, *, limit: int = 50) -> list[PackRun]:
        rows = self.connection.execute(
            f"""SELECT {self._columns()} FROM merlin_storage.pack_runs
                WHERE session_id = %s ORDER BY started_at DESC, id DESC LIMIT %s""",
            (session_id, limit),
        ).fetchall()
        return [_record(PackRun, row) for row in rows]

    def set_status(
        self,
        run_id: UUID | str,
        status: PackRunStatus,
        *,
        completed_at: Any | None = None,
        outcome: PackOutcome | None = None,
    ) -> PackRun:
        row = self.connection.execute(
            f"""UPDATE merlin_storage.pack_runs
                SET status = %s, completed_at = %s, outcome = %s
                WHERE id = %s RETURNING {self._columns()}""",
            (
                status,
                completed_at,
                Jsonb(outcome.model_dump()) if outcome else None,
                run_id,
            ),
        ).fetchone()
        if row is None:
            self.connection.rollback()
            raise KeyError(f"unknown pack run: {run_id}")
        self.connection.commit()
        return _record(PackRun, row)

    def append_turn(self, turn: PackTurn) -> PackTurn:
        row = self.connection.execute(
            """INSERT INTO merlin_storage.pack_turns
                   (id, run_id, sequence, role, content, created_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING id, run_id, sequence, role, content, created_at""",
            (turn.id, turn.run_id, turn.sequence, turn.role, turn.content, turn.created_at),
        ).fetchone()
        self.connection.commit()
        return _record(PackTurn, row)

    def list_turns(self, run_id: UUID | str) -> Sequence[PackTurn]:
        rows = self.connection.execute(
            """SELECT id, run_id, sequence, role, content, created_at
               FROM merlin_storage.pack_turns
               WHERE run_id = %s ORDER BY sequence""",
            (run_id,),
        ).fetchall()
        return [_record(PackTurn, row) for row in rows]
