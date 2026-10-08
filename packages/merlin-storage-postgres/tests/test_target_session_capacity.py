from dataclasses import asdict
from pathlib import Path

from merlin_agents import AssignmentBudget, WorkerAssignment
from merlin_storage_postgres import (
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    SessionRepository,
)


def _assignment(capacity: int | None) -> WorkerAssignment:
    return WorkerAssignment(
        id="assignment:capacity",
        objective="Exercise a reviewed target session",
        profile_id="injection",
        target_profile_id="target-1",
        technique_ids=("technique-1",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider-1",
        authority_ids=("target:target-1",),
        budget=AssignmentBudget(max_turns=2),
        input_schema="merlin.worker-input/v1",
        output_schema="merlin.worker-result/v1",
        target_session_capacity=capacity,
    )


def test_target_session_capacity_survives_schedule_restart_and_legacy_omission(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-capacity-restart"
    assignment = asdict(_assignment(3))
    legacy_assignment = dict(assignment)
    legacy_assignment.pop("target_session_capacity")

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("capacity persistence")
            schedule_id = DurableSchedulerStore(connection).create_schedule(
                session.id,
                {"assignments": [assignment, legacy_assignment]},
                {},
                ["worker:capacity"],
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            MigrationRunner(connection).apply()
            schedule = DurableSchedulerStore(connection).load_schedule(schedule_id)
            assert schedule is not None
            stored = schedule["graph"]["assignments"]
            assert stored[0]["target_session_capacity"] == 3
            assert stored[1].get("target_session_capacity") is None
