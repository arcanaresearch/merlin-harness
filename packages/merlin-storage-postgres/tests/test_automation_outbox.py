from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest
from merlin_agents.automation import (
    ArtifactAdmission,
    ArtifactRun,
    AutomationArtifact,
    AutomationAudit,
    AutomationOutcome,
    AutomationPipeline,
    AutomationStep,
    RunState,
)
from merlin_agents.automation_outbox import outcome_payload, reconcile_automation_requests
from merlin_agents.campaign_service import CampaignService, LeadAutomationRequest
from merlin_contracts import TechniqueAttempt, TechniqueAttemptControlKind
from merlin_storage_postgres import (
    Database,
    DatabaseConfig,
    MigrationRunner,
    PostgresAutomationJournal,
    SessionRepository,
    TechniqueAttemptRepository,
)
from merlin_storage_postgres.campaign_store import CampaignStore


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    with Database(DatabaseConfig(data_dir=tmp_path_factory.mktemp("outbox") / "pg")) as db:
        with db.connect() as connection:
            MigrationRunner(connection).apply()
        yield db


@pytest.fixture
def runtime(database):
    with database.connect() as connection:
        store = CampaignStore(connection)
        campaign_id = f"campaign/{uuid4()}"
        session = SessionRepository(connection).create("automation outbox")
        store.prepare(campaign_id, str(session.id), {}, "sha256:reviewed")
        request = LeadAutomationRequest(
            request_id="request/one",
            owning_attempt_id=str(uuid4()),
            objective="Capture bounded evidence",
            artifact_kind="declarative_browser_flow",
            allowed_operations=("capture",),
            evidence_expectations=("response",),
            cleanup_expectations=("restore",),
            source_task_ids=("task/one",),
        )
        yield connection, store, SimpleNamespace(id=campaign_id), request


def commit(store, manifest, request, decision="decision/one"):
    return store.commit_lead_decision(
        manifest.id, 0, decision, {"automation_requests": [asdict(request)]}, ()
    )


def load(connection, manifest):
    return connection.execute(
        "SELECT * FROM merlin_storage.automation_requests WHERE campaign_id = %s",
        (manifest.id,),
    ).fetchone()


def expire(connection, manifest):
    connection.execute(
        """UPDATE merlin_storage.automation_requests
           SET lease_expires_at = now() - interval '1 second' WHERE campaign_id = %s""",
        (manifest.id,),
    )


def persist_run(connection, request, run_id, state):
    TechniqueAttemptRepository(connection).begin(
        TechniqueAttempt(
            id=request.owning_attempt_id,
            campaign_id="campaign/test",
            wave=0,
            task_id="task/one",
            technique_id="technique/one",
            strategy_id="direct",
            control_kind=TechniqueAttemptControlKind.ATTACK,
            oracle_digest="sha256:" + "a" * 64,
        )
    )
    journal = PostgresAutomationJournal(connection)
    pipeline = AutomationPipeline(journal)
    artifact = pipeline.author(
        AutomationArtifact(
            request_id=request.request_id,
            owning_attempt_id=request.owning_attempt_id,
            author_identity="author",
            author_model="fixture",
            origin="https://example.test",
            max_steps=1,
            steps=(AutomationStep("capture", "capture", {}, ("response",)),),
        )
    )
    audit = AutomationAudit(
        artifact_digest=artifact.digest,
        auditor_identity="auditor",
        auditor_model="fixture",
        decision="accept",
        reasons=("bounded",),
        static_check_refs=("static:one",),
        fixture_evidence_refs=("fixture:one",),
    )
    artifact = pipeline.audit(artifact, audit)
    pipeline.admit(
        artifact,
        audit,
        ArtifactAdmission(
            artifact_digest=artifact.digest,
            owning_attempt_id=request.owning_attempt_id,
            origin=artifact.origin,
            allowed_operations=frozenset({"capture"}),
            environment_digest="sha256:" + "b" * 64,
        ),
    )
    journal.record_run(
        ArtifactRun(
            run_id=run_id,
            artifact_digest=artifact.digest,
            owning_attempt_id=request.owning_attempt_id,
            state=state,
            evidence_refs=("evidence:one",),
            error=None if state == RunState.COMPLETED else "stopped",
        )
    )


@pytest.mark.parametrize("claimed", (False, True))
async def test_decision_before_dispatch_survives_connection_and_service_restart(
    runtime, database, claimed
):
    connection, store, manifest, request = runtime
    commit(store, manifest, request)
    assert load(connection, manifest)["state"] == "planned"
    if claimed:
        store.claim_automation_request(manifest.id)
        expire(connection, manifest)
    calls = []

    async def runner(_manifest, value):
        calls.append(value.request_id)
        return AutomationOutcome(
            value.request_id, value.owning_attempt_id, "execution", "completed"
        )

    # Replacement connection and service have no in-memory decision or request list.
    with database.connect() as replacement:
        recovered = CampaignService(
            CampaignStore(replacement), None, None, None, automation_runner=runner
        )
        await recovered._execute_automation_requests(manifest, ())
        await recovered._execute_automation_requests(manifest, ())
    assert calls == [request.request_id]
    assert load(connection, manifest)["state"] == "completed"
    assert len(store.list_lead(manifest.id)) == 2  # decision plus atomic outcome


async def test_journal_wins_when_runner_fails_after_terminal_run(runtime):
    connection, store, manifest, request = runtime
    commit(store, manifest, request)
    calls = []

    async def runner(*_):
        calls.append(1)
        run_id = load(connection, manifest)["run_id"]
        persist_run(connection, request, run_id, RunState.COMPLETED)
        raise OSError("accounting failed after target effects committed")

    await reconcile_automation_requests(store, manifest, runner)
    await reconcile_automation_requests(store, manifest, runner)
    assert calls == [1]
    assert load(connection, manifest)["state"] == "completed"
    assert load(connection, manifest)["outcome"]["evidence_refs"] == ["evidence:one"]


@pytest.mark.parametrize("state", list(RunState))
async def test_run_before_outbox_receipt_never_replays(runtime, database, state):
    connection, store, manifest, request = runtime
    commit(store, manifest, request)
    claim = store.claim_automation_request(manifest.id)
    assert store.start_automation_request(manifest.id, request.request_id, claim["claim_id"])
    persist_run(connection, request, claim["run_id"], state)
    expire(connection, manifest)

    async def forbidden(*_):
        pytest.fail("persisted run must not dispatch again")

    with database.connect() as replacement:
        await reconcile_automation_requests(CampaignStore(replacement), manifest, forbidden)
    row = load(connection, manifest)
    assert row["state"] == ("completed" if state == RunState.COMPLETED else "rejected")
    assert row["outcome"]["state"] == ("ambiguous" if state == RunState.EXECUTING else state)
    assert row["outcome"]["evidence_refs"] == ["evidence:one"]
    assert row["outcome"]["run_id"] == claim["run_id"]
    assert len(store.list_lead(manifest.id)) == 2


def test_claim_expiry_fences_old_owner_and_renewal(runtime, database):
    connection, store, manifest, request = runtime
    commit(store, manifest, request)
    first = store.claim_automation_request(manifest.id)
    with database.connect() as replacement:
        other = CampaignStore(replacement)
        assert other.claim_automation_request(manifest.id) is None
        assert store.renew_automation_request(manifest.id, request.request_id, first["claim_id"])
        expire(connection, manifest)
        second = other.claim_automation_request(manifest.id)
        assert second["attempt_count"] == 2
        assert first["claim_id"] != second["claim_id"]
        identity = manifest.id, request.request_id, first["claim_id"]
        assert not store.start_automation_request(*identity)
        assert not store.renew_automation_request(*identity)
        assert not store.settle_automation_request(*identity, {"state": "completed"})
        assert other.start_automation_request(manifest.id, request.request_id, second["claim_id"])


async def test_expired_running_without_journal_is_ambiguous(runtime):
    connection, store, manifest, request = runtime
    commit(store, manifest, request)
    claim = store.claim_automation_request(manifest.id)
    store.start_automation_request(manifest.id, request.request_id, claim["claim_id"])
    expire(connection, manifest)
    # Recovery can itself crash after taking ownership. Preserve the running
    # marker across repeated claims so the next restart still refuses replay.
    abandoned_recovery = store.claim_automation_request(manifest.id)
    assert abandoned_recovery["state"] == "running"
    expire(connection, manifest)

    async def forbidden(*_):
        pytest.fail("expired running request could still have an active former owner")

    await reconcile_automation_requests(store, manifest, forbidden)
    assert load(connection, manifest)["outcome"]["state"] == "ambiguous"


def test_duplicate_payload_conflict_rolls_back_entire_decision(runtime):
    connection, store, manifest, request = runtime
    commit(store, manifest, request)
    commit(store, manifest, request)
    assert len(store.list_lead(manifest.id)) == 1
    payload = asdict(request)
    payload["objective"] = "changed"
    with pytest.raises(ValueError, match="automation request identity conflicts"):
        store.commit_lead_decision(
            manifest.id, 0, "decision/two", {"automation_requests": [payload]}, ()
        )
    assert len(store.list_lead(manifest.id)) == 1
    assert load(connection, manifest)["payload"]["objective"] == request.objective
    assert load(connection, manifest)["attempt_count"] == 0


async def test_retry_wait_is_due_only_and_bounded(runtime):
    connection, store, manifest, request = runtime
    commit(store, manifest, request)
    calls = []

    async def runner(*_):
        calls.append(1)
        raise OSError("transport down before execution")

    for attempt in range(1, 4):
        await reconcile_automation_requests(store, manifest, runner)
        row = load(connection, manifest)
        assert row["attempt_count"] == attempt
        assert row["state"] == ("retry_wait" if attempt < 3 else "rejected")
        assert store.claim_automation_request(manifest.id) is None
        if attempt < 3:
            connection.execute(
                """UPDATE merlin_storage.automation_requests SET next_retry_at = now()
                   WHERE campaign_id = %s""",
                (manifest.id,),
            )
    assert len(calls) == 3
    assert len(store.list_lead(manifest.id)) == 2


def test_outcome_and_lead_receipt_are_atomic(runtime, monkeypatch):
    connection, store, manifest, request = runtime
    commit(store, manifest, request)
    claim = store.claim_automation_request(manifest.id)
    identity = manifest.id, request.request_id, claim["claim_id"]
    payload = outcome_payload(
        AutomationOutcome(request.request_id, request.owning_attempt_id, "execution", "completed")
    )

    def fail(*_):
        raise OSError("crash during Lead outcome persistence")

    with monkeypatch.context() as patch:
        patch.setattr(store, "append_lead", fail)
        with pytest.raises(OSError):
            store.settle_automation_request(*identity, payload)
    assert load(connection, manifest)["state"] == "claimed"
    assert store.settle_automation_request(*identity, payload)
    assert not store.settle_automation_request(*identity, payload)
    assert len(store.list_lead(manifest.id)) == 2
