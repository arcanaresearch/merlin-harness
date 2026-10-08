import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from merlin_contracts import PackOutcome, PackRun, PackRunStatus, PackTurn, TargetProfile

from .repositories import PackRunRepository, TargetProfileRepository


class PostgresPackStore:
    """Async boundary for target profiles and durable pack-run history."""

    def __init__(
        self,
        connection: Any,
        *,
        targets: TargetProfileRepository | None = None,
        runs: PackRunRepository | None = None,
    ) -> None:
        self._targets = targets or TargetProfileRepository(connection)
        self._runs = runs or PackRunRepository(connection)
        self._lock = asyncio.Lock()

    async def _call(self, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(function, *args, **kwargs)

    async def save_target(self, profile: TargetProfile) -> TargetProfile:
        return await self._call(self._targets.save, profile)

    async def get_target(self, profile_id: str) -> TargetProfile | None:
        return await self._call(self._targets.get, profile_id)

    async def list_targets(self) -> Sequence[TargetProfile]:
        return await self._call(self._targets.list)

    async def remove_target(self, profile_id: str) -> bool:
        return await self._call(self._targets.remove, profile_id)

    async def create_run(self, run: PackRun) -> PackRun:
        return await self._call(self._runs.create, run)

    async def get_run(self, run_id: str) -> PackRun | None:
        return await self._call(self._runs.get, run_id)

    async def list_runs(self, session_id: str, *, limit: int = 50) -> Sequence[PackRun]:
        return await self._call(self._runs.list, session_id, limit=limit)

    async def set_run_status(
        self,
        run_id: str,
        status: PackRunStatus,
        *,
        completed_at: datetime | None = None,
        outcome: PackOutcome | None = None,
    ) -> PackRun:
        return await self._call(
            self._runs.set_status,
            run_id,
            status,
            completed_at=completed_at,
            outcome=outcome,
        )

    async def append_turn(self, turn: PackTurn) -> PackTurn:
        return await self._call(self._runs.append_turn, turn)

    async def list_turns(self, run_id: str) -> Sequence[PackTurn]:
        return await self._call(self._runs.list_turns, run_id)
