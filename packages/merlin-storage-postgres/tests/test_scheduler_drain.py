import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from merlin_agents import AssignmentBudget, DurableAgentService, WorkerAssignment
from merlin_contracts import ModelCompleted, TextDelta
from merlin_storage_postgres import (
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    SessionRepository,
    StaleLeaseError,
)


def test_drain_preserves_owned_attempts_fences_future_claims_and_survives_reopen(tmp_path):
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("operator drain")
            store = DurableSchedulerStore(connection)
            nodes = ["worker:running", "worker:queued", "cleanup:running", "cleanup:queued"]
            schedule = store.create_schedule(
                session.id,
                {"nodes": nodes},
                {},
                nodes,
                ready_nodes=nodes,
            )
            lease = store.claim(
                schedule,
                "worker:running",
                "worker",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            assert store.drain_schedule(schedule, reason="operator_stopped") == ("worker:queued",)
            assert store.drain_schedule(schedule, reason="operator_stopped") == ()
            states = {row["node_id"]: row["state"] for row in store.list_nodes(schedule)}
            assert states["worker:running"] == "leased"
            assert states["worker:queued"] == "cancelled"
            assert states["cleanup:queued"] == "ready"
            # An ordinary attempt remains owned and can continue heartbeating.
            store.heartbeat(
                lease.id,
                lease.generation,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            reopened = DurableSchedulerStore(connection)
            with pytest.raises(StaleLeaseError, match="finishing current work"):
                reopened.claim(
                    schedule,
                    "worker:queued",
                    "another-worker",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )
            cleanup = reopened.claim(
                schedule,
                "cleanup:queued",
                "cleanup-worker",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            assert cleanup.node_id == "cleanup:queued"
            assert sum(row["type"] == "schedule_draining" for row in store.events(schedule)) == 1
            store.verify(schedule)


@pytest.mark.asyncio
async def test_live_executor_finishes_running_worker_and_cleanup_without_starting_queued(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    class Provider:
        async def stream(self, request):
            calls.append(request)
            started.set()
            await release.wait()
            yield TextDelta(text='{"observation":"retained evidence"}')
            yield ModelCompleted()

    class Tools:
        def describe(self):
            return ()

    assignments = tuple(
        WorkerAssignment(
            id=f"task-{index}",
            objective="Inspect saved artifact",
            profile_id="recon",
            target_profile_id="fixture",
            technique_ids=(),
            guidance_ids=(),
            tool_ids=(),
            skill_ids=(),
            provider_id="fixture",
            authority_ids=(),
            budget=AssignmentBudget(max_seconds=30),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        for index in range(3)
    )
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("live drain")
            store = DurableSchedulerStore(connection)
            service = DurableAgentService(Provider(), Tools(), store, model="fixture")
            live = await service.submit(
                session.id,
                assignments,
                max_workers=1,
                minimum_workers=1,
            )
            await asyncio.wait_for(started.wait(), 5)
            await service.drain_schedule(live.id, reason="operator_stopped")
            assert live.execution is not None and not live.execution.done()
            release.set()
            await asyncio.wait_for(asyncio.shield(live.execution), 10)
            assert len(calls) == 1
            states = {row["node_id"]: row["state"] for row in store.list_nodes(live.id)}
            assert (
                sum(
                    state == "cancelled"
                    for key, state in states.items()
                    if key.startswith("worker:")
                )
                == 2
            )
            assert all(
                state == "completed" for key, state in states.items() if key.startswith("cleanup:")
            )
            store.verify(live.id)
