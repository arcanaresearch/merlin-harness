from __future__ import annotations

import signal
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

sys.path.insert(0, str(Path(__file__).with_name("fixtures")))

from merlin_contracts.state_scope import (
    LEGACY_ATTEMPT_SCHEMA_VERSION,
    STATE_SCOPE_SCHEMA_VERSION,
    EvidenceAvailability,
    StateScopeActionKind,
    StateScopeEnvelope,
    StateScopeEvidence,
)
from merlin_storage_postgres import (
    CampaignStore,
    Database,
    DatabaseConfig,
    MigrationRunner,
    SessionRepository,
    StateScopeRepository,
)
from prior_reader_v083 import (  # noqa: E402
    FROZEN_MIGRATION_MAX,
    FROZEN_SOURCE_COMMIT,
    FrozenPriorReaderV083,
)


@pytest.fixture(autouse=True)
def _isolated_pg0_port(monkeypatch: pytest.MonkeyPatch) -> None:
    import pg0

    real_pg0 = pg0.Pg0
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    def construct_pg0(*args, **kwargs):
        return real_pg0(*args, port=port, **kwargs)

    monkeypatch.setattr(pg0, "Pg0", construct_pg0)


def _empty_evidence() -> StateScopeEvidence:
    return StateScopeEvidence(availability=EvidenceAvailability.EMPTY)


def _scope(campaign_id: str, suffix: str) -> StateScopeEnvelope:
    return StateScopeEnvelope(
        scope_id=f"scope/{suffix}",
        campaign_id=campaign_id,
        experiment_id=f"experiment/{suffix}",
        attempt_id=f"attempt/{suffix}",
        action_id=f"action/{suffix}",
        action_kind=StateScopeActionKind.CANDIDATE,
        target_authority="target.example",
        candidate_id=f"candidate/{suffix}",
        before=_empty_evidence(),
        action=_empty_evidence(),
        responses=_empty_evidence(),
        after=_empty_evidence(),
    )


def _prepare_campaign(connection, suffix: str) -> tuple[CampaignStore, str]:
    session = SessionRepository(connection).create(f"lifecycle {suffix}")
    campaign_id = f"campaign/{suffix}"
    campaigns = CampaignStore(connection)
    campaigns.prepare(
        campaign_id,
        str(session.id),
        {"objective": f"lifecycle {suffix}"},
        f"sha256:{suffix}",
    )
    return campaigns, campaign_id


def _reconcile(repository: StateScopeRepository, claimant_id: str):
    return repository.reconcile_durable_lifecycle(
        claimant_id=claimant_id,
        readable_formats=(LEGACY_ATTEMPT_SCHEMA_VERSION, STATE_SCOPE_SCHEMA_VERSION),
        writable_format=STATE_SCOPE_SCHEMA_VERSION,
        lease_seconds=30,
    )


def _campaign_recovery(report, campaign_id: str):
    return next(item for item in report["campaigns"] if item["campaign_id"] == campaign_id)


def test_restart_settles_acknowledged_effect_once_after_lease_takeover(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            _campaigns, campaign_id = _prepare_campaign(connection, "acknowledged")
            repository = StateScopeRepository(connection)
            envelope = _scope(campaign_id, "acknowledged")
            repository.create_state_scope(campaign_id, "target.example", envelope)
            first_claim = repository.claim_state_scope_lease(
                envelope.scope_id,
                target_authority="target.example",
                principal_key="anonymous",
                equivalence_key="game-run/a",
                claimant_id="daemon/old",
                lease_seconds=30,
            )
            assert first_claim is not None
            repository.acknowledge_state_scope(
                envelope.scope_id,
                claim_id=first_claim,
                claimant_id="daemon/old",
            )

            connection.execute(
                """UPDATE merlin_storage.state_scope_leases
                   SET expires_at = now() - interval '1 second'
                   WHERE claim_id = %s""",
                (first_claim,),
            )
            report = _reconcile(repository, "daemon/new")
            row = next(item for item in report["campaigns"] if item["campaign_id"] == campaign_id)
            assert report["expired_global_leases"] == 1
            assert row["decision"] == "settle_acknowledged"
            assert row["status"] == "paused"
            assert row["target_call_count"] == 1
            assert row["recovery_action"] == "pause_lifecycle"
            with database.connect() as restarted_connection:
                replayed_row = _campaign_recovery(
                    _reconcile(StateScopeRepository(restarted_connection), "daemon/replayed"),
                    campaign_id,
                )
            assert replayed_row["recovery_state_digest"] == row["recovery_state_digest"]
            assert replayed_row["target_call_count"] == 1

            replacement_claim = repository.claim_state_scope_lease(
                envelope.scope_id,
                target_authority="target.example",
                principal_key="anonymous",
                equivalence_key="game-run/a",
                claimant_id="daemon/new",
                lease_seconds=30,
            )
            assert replacement_claim is not None
            terminal = envelope.model_copy(
                update={"revision": 2, "isolation_lease_id": replacement_claim}
            )
            settled = repository.settle_state_scope(
                envelope.scope_id,
                1,
                terminal,
                transition_disposition="unchanged",
                isolation_disposition="restored",
                cleanup_disposition="not_required",
                claim_id=replacement_claim,
                claimant_id="daemon/new",
            )
            replay = repository.settle_state_scope(
                envelope.scope_id,
                1,
                terminal,
                transition_disposition="unchanged",
                isolation_disposition="restored",
                cleanup_disposition="not_required",
                claim_id=replacement_claim,
                claimant_id="daemon/new",
            )
            assert settled["status"] == replay["status"] == "settled"
            assert replay["current_revision"] == 2


def test_resource_replay_after_acknowledgement_never_readmits_target_action(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            _campaigns, campaign_id = _prepare_campaign(connection, "resource-replay")
            repository = StateScopeRepository(connection)
            envelope = _scope(campaign_id, "resource-replay")
            repository.create_state_scope(campaign_id, "target.example", envelope)
            claim_id = repository.claim_state_scope_lease(
                envelope.scope_id,
                target_authority="target.example",
                principal_key="anonymous",
                equivalence_key="game-run/b",
                claimant_id="daemon/a",
                lease_seconds=30,
            )
            assert claim_id is not None
            arguments = {
                "target_authority": "target.example",
                "principal_key": "anonymous",
                "resource_kind": "turn",
                "amount": 1,
                "limit": 30,
                "window_key": "game-run/b",
                "claim_id": claim_id,
                "claimant_id": "daemon/a",
            }
            assert (
                repository.reserve_resource(envelope.scope_id, "reservation/b", **arguments)[
                    "admitted"
                ]
                is True
            )
            reserved = _campaign_recovery(_reconcile(repository, "daemon/a"), campaign_id)
            assert reserved["target_call_count"] == 0
            assert reserved["resource_reservations"]["reserved"] == 1
            repository.acknowledge_resource(
                "reservation/b", claim_id=claim_id, claimant_id="daemon/a"
            )
            repository.acknowledge_state_scope(
                envelope.scope_id, claim_id=claim_id, claimant_id="daemon/a"
            )
            acknowledged = _campaign_recovery(_reconcile(repository, "daemon/a"), campaign_id)
            with database.connect() as restarted_connection:
                restarted = _campaign_recovery(
                    _reconcile(StateScopeRepository(restarted_connection), "daemon/restarted"),
                    campaign_id,
                )
            assert acknowledged["target_call_count"] == restarted["target_call_count"] == 1
            assert acknowledged["recovery_state_digest"] == restarted["recovery_state_digest"]
            assert acknowledged["resource_reservations"]["acknowledged"] == 1
            assert (
                repository.reserve_resource(envelope.scope_id, "reservation/b", **arguments)[
                    "admitted"
                ]
                is False
            )
            repository.settle_resource(
                "reservation/b",
                actual_amount=1,
                claim_id=claim_id,
                claimant_id="daemon/a",
            )
            terminal = envelope.model_copy(update={"revision": 2, "isolation_lease_id": claim_id})
            repository.settle_state_scope(
                envelope.scope_id,
                1,
                terminal,
                transition_disposition="unchanged",
                isolation_disposition="restored",
                cleanup_disposition="not_required",
                claim_id=claim_id,
                claimant_id="daemon/a",
            )
            settled = _campaign_recovery(_reconcile(repository, "daemon/a"), campaign_id)
            assert settled["target_call_count"] == 1
            assert settled["resource_reservations"]["settled"] == 1
            assert (
                repository.reserve_resource(envelope.scope_id, "reservation/b", **arguments)[
                    "admitted"
                ]
                is False
            )


def test_fixed_forward_pause_cannot_be_resumed_by_generic_campaign_path(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            campaigns, campaign_id = _prepare_campaign(connection, "fixed-forward")
            repository = StateScopeRepository(connection)
            envelope = _scope(campaign_id, "fixed-forward")
            repository.create_state_scope(campaign_id, "target.example", envelope)

            report = repository.reconcile_durable_lifecycle(
                claimant_id="daemon/legacy",
                readable_formats=(
                    LEGACY_ATTEMPT_SCHEMA_VERSION,
                    STATE_SCOPE_SCHEMA_VERSION,
                ),
                writable_format=LEGACY_ATTEMPT_SCHEMA_VERSION,
                lease_seconds=30,
            )
            row = next(item for item in report["campaigns"] if item["campaign_id"] == campaign_id)
            assert row["decision"] == "pause_fixed_forward"
            assert row["status"] == "paused"
            assert campaigns.get(campaign_id)["lifecycle_pause_reason"] == "fixed_forward"
            with pytest.raises(ValueError, match="lifecycle"):
                campaigns.resume(campaign_id)


def test_terminal_campaign_clears_durable_lifecycle_pause(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            campaigns, campaign_id = _prepare_campaign(connection, "terminal-pause")
            connection.execute(
                """UPDATE merlin_storage.assessment_campaigns
                   SET status = 'paused',
                       lifecycle_pause_reason = 'acknowledged_settlement',
                       lifecycle_resume_status = 'executing'
                   WHERE id = %s""",
                (campaign_id,),
            )

            terminal = campaigns.finish(
                campaign_id,
                "cancelled",
                stop_reason="operator_stopped",
            )

            assert terminal["status"] == "cancelled"
            assert terminal["lifecycle_pause_reason"] is None
            assert terminal["lifecycle_resume_status"] is None
            assert terminal["lifecycle_wake_at"] is None


def test_legacy_only_rollback_settles_new_effect_once_without_rewriting_legacy(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = SessionRepository(connection)
            campaigns = CampaignStore(connection)
            legacy_id = "campaign/rollback-legacy"
            legacy_manifest = {
                "objective": "preserve the reviewed legacy campaign",
                "evidence": {
                    "request": "legacy-request:Ω-safe-text",
                    "response": ["accepted", {"score": 7}],
                },
            }
            legacy_digest = "sha256:" + "7" * 64
            legacy_session = sessions.create("rollback legacy")
            campaigns.prepare(
                legacy_id,
                str(legacy_session.id),
                legacy_manifest,
                legacy_digest,
            )
            connection.execute(
                "UPDATE merlin_storage.assessment_campaigns SET status = 'executing' WHERE id = %s",
                (legacy_id,),
            )
            connection.commit()
            legacy_before = connection.execute(
                """SELECT convert_to(manifest::text, 'UTF8') AS evidence_bytes,
                          manifest_digest
                   FROM merlin_storage.assessment_campaigns WHERE id = %s""",
                (legacy_id,),
            ).fetchone()
            assert legacy_before is not None

            assert FROZEN_SOURCE_COMMIT == "1b4f8bb"
            assert FROZEN_MIGRATION_MAX == 83
            prior_reader = FrozenPriorReaderV083(connection)
            legacy_first = prior_reader.get(legacy_id)
            assert legacy_first is not None
            first_resume_ids = {row["id"] for row in prior_reader.list_executing()}
            with database.connect() as restarted_connection:
                restarted_reader = FrozenPriorReaderV083(restarted_connection)
                legacy_restarted = restarted_reader.get(legacy_id)
                restarted_resume_ids = {row["id"] for row in restarted_reader.list_executing()}
            assert legacy_restarted is not None
            assert legacy_id in first_resume_ids == restarted_resume_ids
            assert legacy_first["status"] == legacy_restarted["status"] == "executing"
            assert legacy_first["manifest"] == legacy_restarted["manifest"] == legacy_manifest
            assert (
                legacy_first["manifest_digest"]
                == legacy_restarted["manifest_digest"]
                == legacy_digest
            )

            repository = StateScopeRepository(connection)

            new_campaign_id = "campaign/rollback-new"
            new_session = sessions.create("rollback new format")
            campaigns.prepare(
                new_campaign_id,
                str(new_session.id),
                {"objective": "settle once across rollback"},
                "sha256:" + "8" * 64,
            )
            connection.execute(
                "UPDATE merlin_storage.assessment_campaigns SET status = 'executing' WHERE id = %s",
                (new_campaign_id,),
            )
            connection.commit()
            envelope = _scope(new_campaign_id, "rollback-new")
            repository.create_state_scope(new_campaign_id, "target.example", envelope)
            claim_id = repository.claim_state_scope_lease(
                envelope.scope_id,
                target_authority="target.example",
                principal_key="anonymous",
                equivalence_key="game-run/rollback",
                claimant_id="daemon/current",
                lease_seconds=30,
            )
            assert claim_id is not None
            repository.acknowledge_state_scope(
                envelope.scope_id,
                claim_id=claim_id,
                claimant_id="daemon/current",
            )

            # Rollback fencing stops new-format claims, while the already
            # acknowledged effect is settled exactly once using its immutable
            # envelope identity and the existing claim.
            assert repository.set_scope_claims_enabled(STATE_SCOPE_SCHEMA_VERSION, False)
            terminal = envelope.model_copy(update={"revision": 2, "isolation_lease_id": claim_id})
            settled = repository.settle_state_scope(
                envelope.scope_id,
                1,
                terminal,
                transition_disposition="unchanged",
                isolation_disposition="restored",
                cleanup_disposition="not_required",
                claim_id=claim_id,
                claimant_id="daemon/current",
            )
            replayed = repository.settle_state_scope(
                envelope.scope_id,
                1,
                terminal,
                transition_disposition="unchanged",
                isolation_disposition="restored",
                cleanup_disposition="not_required",
                claim_id=claim_id,
                claimant_id="daemon/current",
            )
            assert settled["current_revision"] == replayed["current_revision"] == 2
            envelope_before = connection.execute(
                """SELECT revision, convert_to(envelope::text, 'UTF8') AS evidence_bytes,
                          envelope_digest
                   FROM merlin_storage.scope_envelopes WHERE scope_id = %s
                   ORDER BY revision""",
                (envelope.scope_id,),
            ).fetchall()

            # The current writer performs the rollback fence before the old
            # binary starts. The frozen v083 reader then uses only its original
            # list_executing query; it cannot opt into current repository flags.
            rolled_back = _campaign_recovery(
                repository.reconcile_durable_lifecycle(
                    claimant_id="daemon/rollback-fence",
                    readable_formats=(LEGACY_ATTEMPT_SCHEMA_VERSION,),
                    writable_format=LEGACY_ATTEMPT_SCHEMA_VERSION,
                    lease_seconds=30,
                ),
                new_campaign_id,
            )
            assert rolled_back["decision"] == "pause_fixed_forward"
            assert rolled_back["status"] == "paused"
            assert rolled_back["target_call_count"] == 1

            frozen_after_effect = FrozenPriorReaderV083(connection)
            old_resume_ids = {row["id"] for row in frozen_after_effect.list_executing()}
            assert legacy_id in old_resume_ids
            assert new_campaign_id not in old_resume_ids
            assert frozen_after_effect.get(new_campaign_id)["status"] == "paused"
            assert campaigns.get(new_campaign_id)["lifecycle_pause_reason"] == "fixed_forward"

            after_prior_reader = _campaign_recovery(
                repository.reconcile_durable_lifecycle(
                    claimant_id="daemon/rollback-fence-recheck",
                    readable_formats=(LEGACY_ATTEMPT_SCHEMA_VERSION,),
                    writable_format=LEGACY_ATTEMPT_SCHEMA_VERSION,
                    lease_seconds=30,
                ),
                new_campaign_id,
            )
            assert after_prior_reader["target_call_count"] == 1
            assert (
                after_prior_reader["recovery_state_digest"] == rolled_back["recovery_state_digest"]
            )

            assert repository.set_scope_claims_enabled(STATE_SCOPE_SCHEMA_VERSION, True)
            current = _campaign_recovery(
                _reconcile(repository, "daemon/current-restored"), new_campaign_id
            )
            assert current["decision"] == "resume"
            assert current["status"] == "executing"
            assert current["target_call_count"] == 1
            envelope_after = connection.execute(
                """SELECT revision, convert_to(envelope::text, 'UTF8') AS evidence_bytes,
                          envelope_digest
                   FROM merlin_storage.scope_envelopes WHERE scope_id = %s
                   ORDER BY revision""",
                (envelope.scope_id,),
            ).fetchall()
            assert envelope_after == envelope_before
            assert len(envelope_after) == 2

            legacy_after = connection.execute(
                """SELECT convert_to(manifest::text, 'UTF8') AS evidence_bytes,
                          manifest_digest
                   FROM merlin_storage.assessment_campaigns WHERE id = %s""",
                (legacy_id,),
            ).fetchone()
            assert legacy_after == legacy_before
            assert bytes(legacy_after["evidence_bytes"]) == bytes(legacy_before["evidence_bytes"])
            assert legacy_after["manifest_digest"] == legacy_digest


def test_quota_wake_resumes_only_after_acknowledged_use_is_settled(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            _campaigns, campaign_id = _prepare_campaign(connection, "quota-wake")
            connection.execute(
                """UPDATE merlin_storage.assessment_campaigns
                   SET status = 'executing' WHERE id = %s""",
                (campaign_id,),
            )
            connection.commit()
            repository = StateScopeRepository(connection)
            envelope = _scope(campaign_id, "quota-wake")
            repository.create_state_scope(campaign_id, "target.example", envelope)
            claim_id = repository.claim_state_scope_lease(
                envelope.scope_id,
                target_authority="target.example",
                principal_key="anonymous",
                equivalence_key="game-run/c",
                claimant_id="daemon/a",
                lease_seconds=30,
            )
            assert claim_id is not None
            repository.reserve_resource(
                envelope.scope_id,
                "reservation/c",
                target_authority="target.example",
                principal_key="anonymous",
                resource_kind="turn",
                amount=1,
                limit=1,
                window_key="game-run/c",
                claim_id=claim_id,
                claimant_id="daemon/a",
            )
            repository.acknowledge_resource(
                "reservation/c", claim_id=claim_id, claimant_id="daemon/a"
            )
            wake_at = datetime.now(UTC) + timedelta(minutes=5)
            repository.settle_resource(
                "reservation/c",
                actual_amount=1,
                terminal_block=True,
                wake_at=wake_at,
                claim_id=claim_id,
                claimant_id="daemon/a",
            )
            waiting = _reconcile(repository, "daemon/recovery")
            waiting_row = next(
                item for item in waiting["campaigns"] if item["campaign_id"] == campaign_id
            )
            assert waiting_row["decision"] == "wait_quota"
            assert waiting_row["wake_at"] == wake_at
            assert waiting_row["status"] == "paused"

            connection.execute(
                """UPDATE merlin_storage.principal_resources
                   SET wake_at = now() - interval '1 second'
                   WHERE target_authority = 'target.example'
                     AND principal_key = 'anonymous'
                     AND resource_kind = 'turn' AND window_key = 'game-run/c'"""
            )
            resumed = _reconcile(repository, "daemon/restarted")
            resumed_row = next(
                item for item in resumed["campaigns"] if item["campaign_id"] == campaign_id
            )
            assert resumed["due_quota_wakes"] == 1
            assert resumed_row["decision"] == "resume"
            assert resumed_row["status"] == "executing"


def test_restarted_reconciliation_matches_each_successor_persistence_boundary(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            crash_driver = Path(__file__).parent / "fixtures" / "forced_successor_crash.py"
            expected_actions = (
                "persist_successor_schedule",
                "bind_successor_schedule",
                "bind_successor_schedule",
                "persist_wave_binding",
                "resume_active",
                "finalize_and_settle_terminal_batch",
                "settle_terminal_batch",
                "claim_successor",
            )

            def prepare(suffix: str) -> tuple[str, str, str]:
                campaigns, campaign_id = _prepare_campaign(connection, suffix)
                session_id = str(campaigns.get(campaign_id)["session_id"])
                connection.execute(
                    """UPDATE merlin_storage.assessment_campaigns
                       SET status = 'executing' WHERE id = %s""",
                    (campaign_id,),
                )
                campaigns.register_frontier(
                    campaign_id,
                    [
                        {
                            "id": "family/a",
                            "document": {"id": "family/a", "objective": "test"},
                        }
                    ],
                    [
                        {
                            "id": "candidate/a",
                            "family_id": "family/a",
                            "ordinal": 0,
                            "requested_language": "en",
                            "variant_id": "native",
                        }
                    ],
                )
                repository = StateScopeRepository(connection)
                envelope = _scope(campaign_id, suffix)
                repository.create_state_scope(campaign_id, "target.example", envelope)
                claim_id = repository.claim_state_scope_lease(
                    envelope.scope_id,
                    target_authority="target.example",
                    principal_key="anonymous",
                    equivalence_key=f"game-run/{suffix}",
                    claimant_id=f"daemon/setup-{suffix}",
                    lease_seconds=30,
                )
                assert claim_id is not None
                repository.acknowledge_state_scope(
                    envelope.scope_id,
                    claim_id=claim_id,
                    claimant_id=f"daemon/setup-{suffix}",
                )
                repository.settle_state_scope(
                    envelope.scope_id,
                    1,
                    envelope.model_copy(update={"revision": 2, "isolation_lease_id": claim_id}),
                    transition_disposition="unchanged",
                    isolation_disposition="restored",
                    cleanup_disposition="not_required",
                    claim_id=claim_id,
                    claimant_id=f"daemon/setup-{suffix}",
                )
                schedule_id = str(
                    uuid5(NAMESPACE_URL, f"merlin.campaign-wave.v1\n{campaign_id}\n0")
                )
                return campaign_id, session_id, schedule_id

            def run_child(
                campaign_id: str,
                session_id: str,
                schedule_id: str,
                boundary: int,
                mode: str,
            ) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        sys.executable,
                        str(crash_driver),
                        database.url,
                        campaign_id,
                        session_id,
                        str(boundary),
                        schedule_id,
                        mode,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )

            def durable_records(campaign_id: str) -> dict[str, object]:
                candidate = connection.execute(
                    """SELECT state, active_batch_id IS NOT NULL AS has_batch
                       FROM merlin_storage.campaign_frontier_candidates
                       WHERE campaign_id = %s""",
                    (campaign_id,),
                ).fetchone()
                batch = connection.execute(
                    """SELECT state, schedule_id IS NOT NULL AS has_schedule
                       FROM merlin_storage.campaign_frontier_batches
                       WHERE campaign_id = %s""",
                    (campaign_id,),
                ).fetchone()
                wave = connection.execute(
                    """SELECT status FROM merlin_storage.campaign_waves
                       WHERE campaign_id = %s""",
                    (campaign_id,),
                ).fetchone()
                schedule = connection.execute(
                    """SELECT status FROM merlin_storage.scheduler_schedules
                       WHERE id = (SELECT schedule_id
                                   FROM merlin_storage.campaign_frontier_batches
                                   WHERE campaign_id = %s)""",
                    (campaign_id,),
                ).fetchone()
                scope = connection.execute(
                    """SELECT status, current_revision
                       FROM merlin_storage.state_scopes
                       WHERE campaign_id = %s""",
                    (campaign_id,),
                ).fetchone()
                return {
                    "candidate": None if candidate is None else dict(candidate),
                    "batch": None if batch is None else dict(batch),
                    "wave": None if wave is None else dict(wave),
                    "schedule": None if schedule is None else dict(schedule),
                    "scope": None if scope is None else dict(scope),
                }

            for boundary, expected_action in enumerate(expected_actions, start=1):
                control = prepare(f"successor-control-{boundary}")
                crashed = prepare(f"successor-crash-{boundary}")
                completed = run_child(*control, boundary, "exit")
                interrupted = run_child(*crashed, boundary, "crash")
                assert completed.returncode == 0, completed.stderr
                assert interrupted.returncode == -signal.SIGKILL, interrupted.stderr

                with database.connect() as restarted_connection:
                    restarted_repository = StateScopeRepository(restarted_connection)
                    control_recovery = _campaign_recovery(
                        _reconcile(restarted_repository, f"daemon/control-{boundary}"),
                        control[0],
                    )
                    crash_recovery = _campaign_recovery(
                        _reconcile(restarted_repository, f"daemon/restarted-{boundary}"),
                        crashed[0],
                    )
                    replayed = _campaign_recovery(
                        _reconcile(restarted_repository, f"daemon/replayed-{boundary}"),
                        crashed[0],
                    )

                assert control_recovery["scheduler_action"] == expected_action
                assert crash_recovery["scheduler_action"] == expected_action
                assert crash_recovery["recovery_state_digest"] == replayed["recovery_state_digest"]
                assert (
                    crash_recovery["recovery_decision_digest"]
                    == replayed["recovery_decision_digest"]
                )
                assert control_recovery["target_call_count"] == 1
                assert crash_recovery["target_call_count"] == 1
                assert replayed["target_call_count"] == 1
                assert durable_records(control[0]) == durable_records(crashed[0])
