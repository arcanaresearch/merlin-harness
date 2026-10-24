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


@pytest.fixture(autouse=True)
def _isolated_pg0_port(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    import pg0

    real_pg0 = pg0.Pg0
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    def construct_pg0(*args, **kwargs):
        return real_pg0(*args, port=port, **kwargs)

    monkeypatch.setattr(pg0, "Pg0", construct_pg0)


@pytest.mark.asyncio
async def test_operator_pause_commits_before_local_cancel(tmp_path: Path) -> None:
    import asyncio

    from merlin_agents.campaign_service import CampaignService

    with _campaign_runtime(tmp_path) as (connection, campaign_id, schedule_id):
        transitions = CampaignTransitionRepository(connection, DurableSchedulerStore(connection))
        calls = []

        class Agents:
            async def stop_local_schedule(self, identifier):
                assert identifier == schedule_id
                calls.append("local")
                assert CampaignStore(connection).get(campaign_id)["status"] == "paused"

        async def prepare(*args):
            raise AssertionError("preparation must not run")

        async def supervise():
            try:
                await asyncio.Future()
            finally:
                assert CampaignStore(connection).get(campaign_id)["status"] == "paused"
                calls.append("supervisor")

        service = CampaignService(
            CampaignStore(connection), Agents(), prepare, lifecycle_transitions=transitions
        )
        task = asyncio.create_task(supervise())
        service._supervisors[campaign_id] = task
        await asyncio.sleep(0)
        await service.pause(campaign_id, command_id="operator/pause")
        await service.pause(campaign_id, command_id="operator/pause")
        assert calls == ["supervisor", "local", "local"]
        assert connection.execute(
            "SELECT count(*) AS n FROM merlin_storage.campaign_events"
        ).fetchone()["n"] == 1


@pytest.mark.asyncio
async def test_resume_commit_survives_local_crash(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    import merlin_agents.campaign_service as module

    with _campaign_runtime(tmp_path) as (connection, campaign_id, schedule_id):
        transitions = CampaignTransitionRepository(connection, DurableSchedulerStore(connection))
        transitions.pause(campaign_id, cause="operator", command_id="pause")
        monkeypatch.setattr(module, "_manifest", lambda value: SimpleNamespace())
        monkeypatch.setattr(module, "_reviewed_adapter_value", lambda value: None)
        # The fixture needs no execution manifest: the injected crash occurs
        # immediately after the durable transition, before recovery reads it.
        store = CampaignStore(connection)
        original_get = store.get

        def get(identifier):
            return {**original_get(identifier), "manifest": {"manifest": {}}}

        store.get = get

        class Agents:
            async def stop_local_schedule(self, identifier):
                assert identifier == schedule_id
                assert original_get(campaign_id)["status"] == "executing"
                raise RuntimeError("process stopped after commit")

        async def prepare(*args):
            raise AssertionError

        service = module.CampaignService(
            store, Agents(), prepare, lifecycle_transitions=transitions
        )
        with pytest.raises(RuntimeError, match="after commit"):
            await service.resume(campaign_id, command_id="resume")
        replay = CampaignTransitionRepository(connection, DurableSchedulerStore(connection)).resume(
            campaign_id, cause="operator", command_id="resume"
        )
        assert replay.sequence == 2
        assert DurableSchedulerStore(connection).load_schedule(schedule_id)["status"] == "running"


def test_terminal_rejects_live_schedule_and_cancellation_is_atomic(tmp_path: Path) -> None:
    with _campaign_runtime(tmp_path) as (connection, campaign_id, schedule_id):
        scheduler = DurableSchedulerStore(connection)
        transitions = CampaignTransitionRepository(connection, scheduler)
        with pytest.raises(ValueError, match="active schedule"):
            transitions.terminal(
                campaign_id, status="completed", reason="frontier_exhausted", command_id="finish"
            )
        assert CampaignStore(connection).get(campaign_id)["status"] == "executing"
        assert scheduler.load_schedule(schedule_id)["status"] == "running"

        def crash(kind):
            raise RuntimeError("before campaign commit")

        broken = CampaignTransitionRepository(
            connection, scheduler, after_scheduler_transition=crash
        )
        with pytest.raises(RuntimeError):
            broken.terminal(
                campaign_id, status="cancelled", reason="operator_stopped", command_id="cancel"
            )
        assert scheduler.load_schedule(schedule_id)["status"] == "running"
        cancelled = transitions.terminal(
            campaign_id, status="cancelled", reason="operator_stopped", command_id="cancel"
        )
        assert transitions.terminal(
            campaign_id, status="cancelled", reason="operator_stopped", command_id="cancel"
        ) == cancelled
        assert CampaignStore(connection).get(campaign_id)["status"] == "cancelled"
        assert CampaignStore(connection).list_waves(campaign_id)[0]["status"] == "cancelled"
        assert scheduler.load_schedule(schedule_id)["status"] == "cancelled"


def test_recovery_claim_cas_expiry_and_stale_plan(tmp_path: Path) -> None:
    from merlin_contracts.state_scope import STATE_SCOPE_MIGRATION, STATE_SCOPE_SCHEMA_VERSION
    from merlin_storage_postgres.campaign_recovery_store import CampaignRecoveryRepository
    from merlin_storage_postgres.state_scope_store import StateScopeRepository

    with _campaign_runtime(tmp_path) as (connection, campaign_id, schedule_id):
        report = StateScopeRepository(connection).reconcile_durable_lifecycle(
            claimant_id="test", readable_formats=STATE_SCOPE_MIGRATION.readable_versions,
            writable_format=STATE_SCOPE_SCHEMA_VERSION, lease_seconds=30,
        )
        decision = report["campaigns"][0]
        repository = CampaignRecoveryRepository(connection)
        args = dict(
            state_digest=decision["recovery_state_digest"],
            decision_digest=decision["recovery_decision_digest"],
            fence_digest=decision["recovery_fence_digest"], claimant_id="daemon/one",
        )
        assert repository.claim(
            campaign_id, **{**args, "decision_digest": "sha256:" + "0" * 64}
        ) is None
        first = repository.claim(campaign_id, **args)
        assert first is not None
        assert repository.claim(campaign_id, **{**args, "claimant_id": "daemon/two"}) is None
        connection.execute(
            """UPDATE merlin_storage.campaign_recovery
               SET claim_expires_at = now() - interval '1 second'"""
        )
        with pytest.raises(ValueError, match="expired"):
            repository.apply(campaign_id, claim_id=first)
        second = repository.claim(campaign_id, **args)
        assert second is not None and second != first
        with pytest.raises(ValueError, match="superseded"):
            repository.apply(campaign_id, claim_id=first)
        repository.apply(campaign_id, claim_id=second)
        assert repository.claim(campaign_id, **args) is None
        # Re-observation creates an applicable plan for a replacement daemon.
        StateScopeRepository(connection).reconcile_durable_lifecycle(
            claimant_id="test", readable_formats=STATE_SCOPE_MIGRATION.readable_versions,
            writable_format=STATE_SCOPE_SCHEMA_VERSION, lease_seconds=30,
        )
        DurableSchedulerStore(connection).pause_schedule(schedule_id, reason="concurrent operator")
        assert repository.claim(campaign_id, **args) is None
        row = connection.execute(
            "SELECT application_state, application_error FROM merlin_storage.campaign_recovery"
        ).fetchone()
        assert row == {"application_state": "rejected", "application_error": "stale plan"}


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
