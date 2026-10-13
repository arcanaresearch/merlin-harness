"""Frozen campaign reader from the last pre-state-scope release.

The two queries and row projection below are copied from ``CampaignStore`` at
commit ``1b4f8bb`` (migrations through 083).  Keep this fixture independent of
the installed ``merlin_storage_postgres`` package: the rollback test must prove
what the prior reader sees, rather than configuring the current reader to act
old.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

FROZEN_SOURCE_COMMIT: Final = "1b4f8bb"
FROZEN_MIGRATION_MAX: Final = 83


class FrozenPriorReaderV083:
    """Read resumable campaigns exactly as the pre-state-scope store did."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def get(self, campaign_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM merlin_storage.assessment_campaigns WHERE id = %s",
            (campaign_id,),
        ).fetchone()
        return None if row is None else self._row(row)

    def list_executing(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT * FROM merlin_storage.assessment_campaigns
               WHERE status = 'executing' ORDER BY updated_at, id"""
        ).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "id": str(row["id"]),
            "session_id": str(row["session_id"]),
            "schedule_id": None if row.get("schedule_id") is None else str(row["schedule_id"]),
        }
