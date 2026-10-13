from __future__ import annotations

import os
import signal
import sys

import psycopg
from merlin_storage_postgres import CampaignStore, DurableSchedulerStore
from psycopg.rows import dict_row


def _apply_successor_boundary(
    connection,
    *,
    campaign_id: str,
    session_id: str,
    boundary: int,
) -> None:
    campaigns = CampaignStore(connection)
    scheduler = DurableSchedulerStore(connection)
    batch_id = "batch/0"
    schedule_id = sys.argv[5]

    campaigns.claim_frontier_batch(campaign_id, 0, batch_id, 1)
    if boundary == 1:
        return
    scheduler.create_schedule(
        session_id,
        {"assignments": []},
        {"phase": "created"},
        ["lead", "worker"],
        ready_nodes=["lead"],
        schedule_id=schedule_id,
    )
    if boundary == 2:
        return
    created = scheduler.load_schedule(schedule_id)
    assert created is not None
    scheduler.transition_ready(
        schedule_id,
        ["worker"],
        checkpoint={"phase": "workers-ready"},
        expected_event_count=int(created["event_count"]),
    )
    if boundary == 3:
        return
    campaigns.bind_frontier_batch(campaign_id, batch_id, schedule_id)
    if boundary == 4:
        return
    campaigns.add_wave(campaign_id, 0, "frontier", schedule_id)
    if boundary == 5:
        return
    connection.execute(
        """UPDATE merlin_storage.scheduler_nodes SET state = 'completed'
           WHERE schedule_id = %s""",
        (schedule_id,),
    )
    connection.execute(
        """UPDATE merlin_storage.scheduler_schedules SET status = 'completed'
           WHERE id = %s""",
        (schedule_id,),
    )
    if boundary == 6:
        return
    campaigns.finish_wave(campaign_id, 0, "completed", {"completed": 1})
    if boundary == 7:
        return
    campaigns.settle_frontier_batch(
        campaign_id,
        batch_id,
        {"candidate/a": "retryable"},
    )


def main() -> None:
    database_url, campaign_id, session_id, boundary_text, _schedule_id, mode = sys.argv[1:]
    with psycopg.connect(database_url, row_factory=dict_row, autocommit=True) as connection:
        _apply_successor_boundary(
            connection,
            campaign_id=campaign_id,
            session_id=session_id,
            boundary=int(boundary_text),
        )
    if mode == "crash":
        os.kill(os.getpid(), signal.SIGKILL)


if __name__ == "__main__":
    main()
