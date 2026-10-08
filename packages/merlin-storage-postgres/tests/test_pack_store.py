from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

from merlin_contracts import PackOutcome, PackRun, TargetProfile
from merlin_storage_postgres import PostgresPackStore


class FakeTargets:
    def __init__(self) -> None:
        self.profile: TargetProfile | None = None

    def save(self, profile: TargetProfile) -> TargetProfile:
        self.profile = profile
        return profile

    def get(self, profile_id: str) -> TargetProfile | None:
        return self.profile if self.profile and self.profile.id == profile_id else None

    def list(self) -> list[TargetProfile]:
        return [self.profile] if self.profile else []

    def remove(self, profile_id: str) -> bool:
        if self.profile is None or self.profile.id != profile_id:
            return False
        self.profile = None
        return True


class FakeRuns:
    def __init__(self) -> None:
        self.run: PackRun | None = None

    def create(self, run: PackRun) -> PackRun:
        self.run = run
        return run

    def get(self, run_id: str) -> PackRun | None:
        return self.run if self.run and self.run.id == run_id else None

    def list(self, session_id: str, *, limit: int = 50) -> list[PackRun]:
        return [self.run] if self.run and self.run.session_id == session_id else []

    def set_status(self, run_id: str, status: str, **changes: object) -> PackRun:
        assert self.run is not None and self.run.id == run_id
        self.run = self.run.model_copy(update={"status": status, **changes})
        return self.run

    def append_turn(self, turn: object) -> object:
        return turn

    def list_turns(self, run_id: str) -> Sequence[object]:
        return []


def test_async_pack_store_adapts_repositories() -> None:
    async def exercise() -> None:
        targets = FakeTargets()
        runs = FakeRuns()
        store = PostgresPackStore(object(), targets=targets, runs=runs)  # type: ignore[arg-type]
        target = TargetProfile(
            id="target", title="Target", base_url="http://target/v1", model="model"
        )
        run = PackRun(
            id="a59ec775-5780-493d-8178-6d610a97445d",
            session_id="479993a9-79fb-429e-8dad-45ec1b8e85ac",
            pack_id="pack",
            case_id="case",
            target_profile_id=target.id,
            status="running",
            started_at=datetime.now(UTC),
        )

        assert await store.save_target(target) == target
        assert await store.create_run(run) == run
        outcome = PackOutcome(successful=True, summary="Succeeded")
        completed = await store.set_run_status(run.id, "completed", outcome=outcome)
        assert completed.outcome == outcome
        assert list(await store.list_runs(run.session_id)) == [completed]

    asyncio.run(exercise())
