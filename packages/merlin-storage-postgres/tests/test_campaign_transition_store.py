from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from merlin_storage_postgres.campaign_store import CampaignStore
from merlin_storage_postgres.campaign_transition_store import CampaignTransitionRepository
from merlin_storage_postgres.database import Database, DatabaseConfig
from merlin_storage_postgres.migrations import MigrationRunner
from merlin_storage_postgres.repositories import SessionRepository
from merlin_storage_postgres.scheduler_store import DurableSchedulerStore
from psycopg.errors import RaiseException


@contextmanager
def _campaign_runtime(tmp_path: Path) -> Iterator[tuple[object, str, str]]:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("campaign transition boundary")
            scheduler = DurableSchedulerStore(connection)
            schedule_id = scheduler.create_schedule(
                str(session.id),
                {"nodes": ["worker"]},
                {"completed": []},
                ["worker"],
                ready_nodes=["worker"],
            )
            campaign_id = "campaign/transition"
            campaigns = CampaignStore(connection)
            campaigns.prepare(campaign_id, str(session.id), {}, "sha256:reviewed")
            campaigns.begin_execution(campaign_id, "sha256:reviewed", schedule_id)
            yield connection, campaign_id, schedule_id


def test_campaign_transition_pause_resume_is_atomic_and_idempotent(tmp_path: Path) -> None:
    with _campaign_runtime(tmp_path) as (connection, campaign_id, schedule_id):
        scheduler = DurableSchedulerStore(connection)
        transitions = CampaignTransitionRepository(connection, scheduler)

        paused = transitions.pause(
            campaign_id,
            cause="operator",
            command_id="command/pause-1",
            payload={"source": "test"},
        )
        replay = transitions.pause(
            campaign_id,
            cause="operator",
            command_id="command/pause-1",
            payload={"source": "test"},
        )

        assert replay == paused
        assert paused.schedule_ids == (schedule_id,)
        assert connection.execute(
            """SELECT status, pause_cause, event_count
               FROM merlin_storage.assessment_campaigns WHERE id = %s""",
            (campaign_id,),
        ).fetchone() == {"status": "paused", "pause_cause": "operator", "event_count": 1}
        assert (
            connection.execute(
                "SELECT status FROM merlin_storage.campaign_waves WHERE campaign_id = %s",
                (campaign_id,),
            ).fetchone()["status"]
            == "paused"
        )
        assert (
            connection.execute(
                "SELECT status FROM merlin_storage.scheduler_schedules WHERE id = %s",
                (schedule_id,),
            ).fetchone()["status"]
            == "paused"
        )

        resumed = transitions.resume(
            campaign_id,
            cause="operator",
            command_id="command/resume-1",
        )

        assert resumed.sequence == 2
        campaign = connection.execute(
            """SELECT status, pause_cause, event_count, last_event_digest
               FROM merlin_storage.assessment_campaigns WHERE id = %s""",
            (campaign_id,),
        ).fetchone()
        assert campaign == {
            "status": "executing",
            "pause_cause": None,
            "event_count": 2,
            "last_event_digest": resumed.digest,
        }
        events = connection.execute(
            """SELECT sequence, type, cause, command_id, payload, previous_digest, digest
               FROM merlin_storage.campaign_events WHERE campaign_id = %s ORDER BY sequence""",
            (campaign_id,),
        ).fetchall()
        assert [row["type"] for row in events] == ["campaign_paused", "campaign_resumed"]
        assert events[0]["previous_digest"] == "0" * 64
        assert events[1]["previous_digest"] == events[0]["digest"]
        for event in events:
            encoded = json.dumps(
                {
                    "campaign_id": campaign_id,
                    "sequence": event["sequence"],
                    "type": event["type"],
                    "cause": event["cause"],
                    "command_id": event["command_id"],
                    "payload": event["payload"],
                    "previous": event["previous_digest"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            assert event["digest"] == hashlib.sha256(encoded).hexdigest()


def test_campaign_transition_rolls_back_scheduler_and_campaign_together(tmp_path: Path) -> None:
    with _campaign_runtime(tmp_path) as (connection, campaign_id, schedule_id):
        scheduler = DurableSchedulerStore(connection)

        def fail_after_scheduler(_transition_type: str) -> None:
            raise RuntimeError("injected transition failure")

        transitions = CampaignTransitionRepository(
            connection,
            scheduler,
            after_scheduler_transition=fail_after_scheduler,
        )
        before = connection.execute(
            "SELECT event_count FROM merlin_storage.scheduler_schedules WHERE id = %s",
            (schedule_id,),
        ).fetchone()["event_count"]

        with pytest.raises(RuntimeError, match="injected transition failure"):
            transitions.pause(
                campaign_id,
                cause="recovery",
                command_id="command/fail",
            )

        assert connection.execute(
            "SELECT status, event_count FROM merlin_storage.assessment_campaigns WHERE id = %s",
            (campaign_id,),
        ).fetchone() == {"status": "executing", "event_count": 0}
        assert connection.execute(
            "SELECT status, event_count FROM merlin_storage.scheduler_schedules WHERE id = %s",
            (schedule_id,),
        ).fetchone() == {"status": "running", "event_count": before}
        assert (
            connection.execute(
                """SELECT count(*) AS count FROM merlin_storage.campaign_events
                   WHERE campaign_id = %s""",
                (campaign_id,),
            ).fetchone()["count"]
            == 0
        )


def test_campaign_events_are_append_only(tmp_path: Path) -> None:
    with _campaign_runtime(tmp_path) as (connection, campaign_id, _schedule_id):
        transitions = CampaignTransitionRepository(connection, DurableSchedulerStore(connection))
        transitions.pause(campaign_id, cause="operator", command_id="command/pause")

        with connection.transaction(), pytest.raises(RaiseException, match="append-only"):
            connection.execute(
                """UPDATE merlin_storage.campaign_events SET cause = 'recovery'
                   WHERE campaign_id = %s""",
                (campaign_id,),
            )
