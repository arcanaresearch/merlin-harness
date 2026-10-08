from pathlib import Path

import pytest
from merlin_storage_postgres import (
    CampaignApiSessionRepository,
    CampaignStore,
    Database,
    DatabaseConfig,
    MigrationRunner,
    RemoteSessionClaimConflict,
    SessionRepository,
)


def _prepare_campaign(connection, campaign_id: str = "campaign/api-session") -> None:
    session = SessionRepository(connection).create("API session acceptance")
    CampaignStore(connection).prepare(
        campaign_id,
        str(session.id),
        {"manifest": {"id": campaign_id}, "assignment_context": {}},
        "sha256:reviewed",
    )


def test_api_session_rejects_parallel_or_changed_adapter_state(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            _prepare_campaign(connection)
            sessions = CampaignApiSessionRepository(connection)
            created = sessions.create("campaign/api-session", "target-1", "sha256:adapter")
            assert created.state == "active"

            pending = sessions.begin_step(
                created.campaign_id,
                {
                    "id": "start",
                    "request_digest": "sha256:request",
                    "replay_after_ambiguous_result": False,
                },
            )
            assert pending.pending_step is not None
            with pytest.raises(ValueError, match="unresolved"):
                sessions.begin_step(created.campaign_id, {"id": "parallel"})
            with pytest.raises(ValueError, match="identity changed"):
                sessions.create(created.campaign_id, "target-2", "sha256:different")


def test_api_session_pending_step_and_captures_survive_postgres_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres"
    campaign_id = "campaign/api-session"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            _prepare_campaign(connection, campaign_id)
            sessions = CampaignApiSessionRepository(connection)
            sessions.create(campaign_id, "target-1", "sha256:adapter")
            sessions.begin_step(
                campaign_id,
                {
                    "id": "start",
                    "request_digest": "sha256:request",
                    "replay_after_ambiguous_result": False,
                },
            )
            sessions.finish_step(campaign_id, {"session_id": "opaque-session-42"})
            sessions.begin_step(
                campaign_id,
                {
                    "id": "message",
                    "request_digest": "sha256:message",
                    "replay_after_ambiguous_result": False,
                },
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = CampaignApiSessionRepository(connection)
            restored = sessions.load(campaign_id)
            assert restored is not None
            assert restored.generation == 1
            assert restored.variables == {"session_id": "opaque-session-42"}
            assert restored.pending_step == {
                "id": "message",
                "request_digest": "sha256:message",
                "replay_after_ambiguous_result": False,
            }
            ambiguous = sessions.mark_ambiguous(campaign_id)
            assert ambiguous.state == "ambiguous"
            cleaned = sessions.finish_cleanup(campaign_id)
            assert cleaned.state == "cleaned"
            assert sessions.finish_cleanup(campaign_id) == cleaned

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = CampaignApiSessionRepository(connection)
            recreated = sessions.create(campaign_id, "target-1", "sha256:adapter")
            assert recreated.generation == 2
            assert recreated.target_session_id == (f"api:{campaign_id}:target-1:generation:2")


def test_api_sessions_are_isolated_by_assignment_scope_and_enumerable(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-scopes")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            campaign_id = "campaign/api-scopes"
            _prepare_campaign(connection, campaign_id)
            sessions = CampaignApiSessionRepository(connection)

            legacy = sessions.create(campaign_id, "target-1", "sha256:adapter")
            sessions.begin_step(campaign_id, {"id": "legacy:active"})
            first = sessions.create(
                campaign_id,
                "target-1",
                "sha256:adapter",
                session_scope_id="assignment:first",
            )
            second = sessions.create(
                campaign_id,
                "target-1",
                "sha256:adapter",
                session_scope_id="assignment:second",
            )

            loaded_legacy = sessions.load(campaign_id)
            assert loaded_legacy is not None
            assert loaded_legacy.session_scope_id == legacy.session_scope_id
            assert loaded_legacy.pending_step == {"id": "legacy:active"}
            assert (
                sessions.load(
                    campaign_id,
                    target_id="target-1",
                    session_scope_id="assignment:first",
                )
                == first
            )
            assert first.target_session_id != second.target_session_id
            assert [row.session_scope_id for row in sessions.list_for_campaign(campaign_id)] == [
                "assignment:first",
                "assignment:second",
                "legacy:campaign",
            ]

            sessions.begin_step(
                campaign_id,
                {"id": "message:first"},
                target_id="target-1",
                session_scope_id="assignment:first",
            )
            untouched = sessions.load(
                campaign_id,
                target_id="target-1",
                session_scope_id="assignment:second",
            )
            assert untouched is not None
            assert untouched.pending_step is None


def test_scoped_api_session_state_survives_postgres_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres-scoped-restart"
    campaign_id = "campaign/scoped-restart"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            _prepare_campaign(connection, campaign_id)
            sessions = CampaignApiSessionRepository(connection)
            sessions.create(
                campaign_id,
                "target-1",
                "sha256:adapter",
                session_scope_id="assignment:stable",
            )
            sessions.begin_step(
                campaign_id,
                {"id": "start"},
                target_id="target-1",
                session_scope_id="assignment:stable",
            )
            sessions.finish_step(
                campaign_id,
                {"session_id": "remote-stable"},
                target_id="target-1",
                session_scope_id="assignment:stable",
            )
            sessions.claim_remote_session(
                campaign_id,
                "remote-stable",
                target_id="target-1",
                session_scope_id="assignment:stable",
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as restarted:
        with restarted.connect() as connection:
            MigrationRunner(connection).apply()
            restored = CampaignApiSessionRepository(connection).load(
                campaign_id,
                target_id="target-1",
                session_scope_id="assignment:stable",
            )
            assert restored is not None
            assert restored.generation == 1
            assert restored.variables == {"session_id": "remote-stable"}
            assert restored.remote_session_id == "remote-stable"


def test_remote_api_session_identity_has_one_live_scope_owner(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-remote-owner")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            campaign_id = "campaign/remote-owner"
            _prepare_campaign(connection, campaign_id)
            sessions = CampaignApiSessionRepository(connection)
            for scope in ("assignment:first", "assignment:second"):
                sessions.create(
                    campaign_id,
                    "target-1",
                    "sha256:adapter",
                    session_scope_id=scope,
                )

            claimed = sessions.claim_remote_session(
                campaign_id,
                "remote-session-42",
                target_id="target-1",
                session_scope_id="assignment:first",
            )
            assert claimed.remote_session_id == "remote-session-42"
            with pytest.raises(RemoteSessionClaimConflict) as conflict:
                sessions.claim_remote_session(
                    campaign_id,
                    "remote-session-42",
                    target_id="target-1",
                    session_scope_id="assignment:second",
                )
            assert conflict.value.owner_session_scope_id == "assignment:first"

            cleaned = sessions.finish_cleanup(
                campaign_id,
                target_id="target-1",
                session_scope_id="assignment:first",
            )
            assert cleaned.remote_session_id is None
            replacement = sessions.claim_remote_session(
                campaign_id,
                "remote-session-42",
                target_id="target-1",
                session_scope_id="assignment:second",
            )
            assert replacement.remote_session_id == "remote-session-42"


def test_adopt_remote_session_releases_only_terminal_quarantined_claims(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-remote-adopt")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            campaign_id = "campaign/remote-adopt"
            _prepare_campaign(connection, campaign_id)
            sessions = CampaignApiSessionRepository(connection)
            for scope in ("assignment:first", "assignment:second"):
                sessions.create(
                    campaign_id,
                    "target-1",
                    "sha256:adapter",
                    session_scope_id=scope,
                )
            sessions.claim_remote_session(
                campaign_id,
                "remote-shared",
                target_id="target-1",
                session_scope_id="assignment:first",
            )

            # An active owner's claim is never adopted.
            with pytest.raises(RemoteSessionClaimConflict) as conflict:
                sessions.adopt_remote_session(
                    campaign_id,
                    "remote-shared",
                    target_id="target-1",
                    session_scope_id="assignment:second",
                )
            assert conflict.value.owner_session_scope_id == "assignment:first"

            # Once the owner retires with an unestablished cleanup requirement,
            # its claim stays quarantined but terminal, and the next scope on
            # the single physical session adopts it.
            retired = sessions.finish_without_cleanup(
                campaign_id,
                target_id="target-1",
                session_scope_id="assignment:first",
                disposition="cleanup_requirement_unestablished",
            )
            assert retired.state == "ambiguous"
            assert retired.remote_session_id == "remote-shared"
            adopted = sessions.adopt_remote_session(
                campaign_id,
                "remote-shared",
                target_id="target-1",
                session_scope_id="assignment:second",
            )
            assert adopted.remote_session_id == "remote-shared"
            released = sessions.load(
                campaign_id,
                target_id="target-1",
                session_scope_id="assignment:first",
            )
            assert released is not None
            assert released.state == "ambiguous"
            assert released.cleanup_disposition == "cleanup_requirement_unestablished"
            assert released.remote_session_id is None


def test_adopt_remote_session_releases_orphaned_claims_of_terminal_campaigns(
    tmp_path: Path,
) -> None:
    """A killed daemon leaves 'active' claims whose campaign is terminal; adopt them."""
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-remote-orphan")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            _prepare_campaign(connection, "campaign/dead")
            _prepare_campaign(connection, "campaign/live")
            sessions = CampaignApiSessionRepository(connection)
            sessions.create(
                "campaign/dead", "target-1", "sha256:adapter", session_scope_id="scope:old"
            )
            sessions.claim_remote_session(
                "campaign/dead",
                "remote-shared",
                target_id="target-1",
                session_scope_id="scope:old",
            )
            sessions.create(
                "campaign/live", "target-1", "sha256:adapter", session_scope_id="scope:new"
            )
            # The owning campaign is terminalized without cleanup (kill -9).
            connection.execute(
                """UPDATE merlin_storage.assessment_campaigns
                   SET status = 'cancelled' WHERE id = 'campaign/dead'"""
            )

            adopted = sessions.adopt_remote_session(
                "campaign/live",
                "remote-shared",
                target_id="target-1",
                session_scope_id="scope:new",
            )

            assert adopted.remote_session_id == "remote-shared"
            orphaned = sessions.load(
                "campaign/dead", target_id="target-1", session_scope_id="scope:old"
            )
            assert orphaned is not None
            assert orphaned.state == "active"
            assert orphaned.remote_session_id is None


def test_scope_cleanup_and_release_never_touch_sibling_assignment(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-scope-cleanup")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            campaign_id = "campaign/scope-cleanup"
            _prepare_campaign(connection, campaign_id)
            sessions = CampaignApiSessionRepository(connection)
            for scope, remote in (
                ("assignment:first", "remote-first"),
                ("assignment:second", "remote-second"),
            ):
                sessions.create(
                    campaign_id,
                    "target-1",
                    "sha256:adapter",
                    session_scope_id=scope,
                )
                sessions.claim_remote_session(
                    campaign_id,
                    remote,
                    target_id="target-1",
                    session_scope_id=scope,
                )

            sessions.release_remote_session(
                campaign_id,
                target_id="target-1",
                session_scope_id="assignment:first",
                remote_session_id="remote-first",
            )
            sessions.finish_cleanup(
                campaign_id,
                target_id="target-1",
                session_scope_id="assignment:first",
            )
            sibling = sessions.load(
                campaign_id,
                target_id="target-1",
                session_scope_id="assignment:second",
            )
            assert sibling is not None
            assert sibling.state == "active"
            assert sibling.remote_session_id == "remote-second"
            assert sessions.list_for_campaign(campaign_id, include_cleaned=False) == (sibling,)


def test_quarantine_preserves_exact_scope_evidence_without_pending_step(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-quarantine")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            campaign_id = "campaign/quarantine"
            _prepare_campaign(connection, campaign_id)
            sessions = CampaignApiSessionRepository(connection)
            sessions.create(
                campaign_id,
                "target-1",
                "sha256:adapter",
                session_scope_id="assignment:unsafe-legacy",
            )
            sessions.claim_remote_session(
                campaign_id,
                "remote-quarantined",
                target_id="target-1",
                session_scope_id="assignment:unsafe-legacy",
            )
            sessions.begin_step(
                campaign_id,
                {"id": "start"},
                target_id="target-1",
                session_scope_id="assignment:unsafe-legacy",
            )
            sessions.finish_step(
                campaign_id,
                {"unrelated_capture": "preserved"},
                target_id="target-1",
                session_scope_id="assignment:unsafe-legacy",
            )

            quarantined = sessions.quarantine(
                campaign_id,
                target_id="target-1",
                session_scope_id="assignment:unsafe-legacy",
            )

            assert quarantined.state == "ambiguous"
            assert quarantined.variables == {"unrelated_capture": "preserved"}
            assert quarantined.pending_step is None
            assert quarantined.remote_session_id == "remote-quarantined"

            sessions.create(
                campaign_id,
                "target-1",
                "sha256:adapter",
                session_scope_id="assignment:pending",
            )
            sessions.begin_step(
                campaign_id,
                {"id": "unresolved"},
                target_id="target-1",
                session_scope_id="assignment:pending",
            )
            pending = sessions.quarantine(
                campaign_id,
                target_id="target-1",
                session_scope_id="assignment:pending",
            )
            assert pending.pending_step == {"id": "unresolved"}
            with pytest.raises(ValueError, match="quarantine"):
                sessions.quarantine(
                    campaign_id,
                    target_id="target-1",
                    session_scope_id="assignment:missing",
                )


def test_mark_ambiguous_is_idempotent_while_pending_cleanup_survives(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-ambiguous-retry")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            campaign_id = "campaign/ambiguous-retry"
            _prepare_campaign(connection, campaign_id)
            sessions = CampaignApiSessionRepository(connection)
            sessions.create(
                campaign_id,
                "target-1",
                "sha256:adapter",
                session_scope_id="assignment:cleanup",
            )
            sessions.claim_remote_session(
                campaign_id,
                "remote-session",
                target_id="target-1",
                session_scope_id="assignment:cleanup",
            )
            sessions.begin_cleanup(
                campaign_id,
                {"id": "cleanup"},
                target_id="target-1",
                session_scope_id="assignment:cleanup",
            )

            first = sessions.mark_ambiguous(
                campaign_id,
                target_id="target-1",
                session_scope_id="assignment:cleanup",
            )
            second = sessions.mark_ambiguous(
                campaign_id,
                target_id="target-1",
                session_scope_id="assignment:cleanup",
            )

            assert second == first
            assert second.pending_step == {"id": "cleanup"}
            assert second.remote_session_id == "remote-session"


def test_failed_cleanup_preserves_claim_and_no_cleanup_disposition_releases_it(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-cleanup-outcomes")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            campaign_id = "campaign/cleanup-outcomes"
            _prepare_campaign(connection, campaign_id)
            sessions = CampaignApiSessionRepository(connection)
            for scope in ("assignment:owner", "assignment:replacement"):
                sessions.create(
                    campaign_id,
                    "target-1",
                    "sha256:adapter",
                    session_scope_id=scope,
                )

            sessions.claim_remote_session(
                campaign_id,
                "remote-session",
                target_id="target-1",
                session_scope_id="assignment:owner",
            )
            sessions.begin_cleanup(
                campaign_id,
                {"id": "cleanup"},
                target_id="target-1",
                session_scope_id="assignment:owner",
            )
            failed = sessions.fail_cleanup(
                campaign_id,
                {"__cleanup_exchange": {"status": 500}},
                target_id="target-1",
                session_scope_id="assignment:owner",
            )
            assert failed.state == "ambiguous"
            assert failed.pending_step is None
            assert failed.remote_session_id == "remote-session"
            assert failed.variables == {"__cleanup_exchange": {"status": 500}}
            assert failed.cleanup_disposition is None

            retired = sessions.finish_without_cleanup(
                campaign_id,
                target_id="target-1",
                session_scope_id="assignment:owner",
                disposition="no_cleanup_obligation",
            )
            assert retired.state == "cleaned"
            assert retired.remote_session_id is None
            assert retired.cleanup_disposition == "no_cleanup_obligation"

            replacement = sessions.claim_remote_session(
                campaign_id,
                "remote-session",
                target_id="target-1",
                session_scope_id="assignment:replacement",
            )
            assert replacement.remote_session_id == "remote-session"


def test_finish_without_cleanup_preserves_unestablished_remote_claim(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-cleanup-unestablished")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            campaign_id = "campaign/cleanup-unestablished"
            _prepare_campaign(connection, campaign_id)
            sessions = CampaignApiSessionRepository(connection)
            sessions.create(
                campaign_id,
                "target-1",
                "sha256:adapter",
                session_scope_id="assignment:unknown",
            )
            sessions.claim_remote_session(
                campaign_id,
                "remote-session",
                target_id="target-1",
                session_scope_id="assignment:unknown",
            )

            unresolved = sessions.finish_without_cleanup(
                campaign_id,
                target_id="target-1",
                session_scope_id="assignment:unknown",
                disposition="cleanup_requirement_unestablished",
            )

            assert unresolved.state == "ambiguous"
            assert unresolved.remote_session_id == "remote-session"
            assert unresolved.cleanup_disposition == "cleanup_requirement_unestablished"
            sessions.create(
                campaign_id,
                "target-1",
                "sha256:adapter",
                session_scope_id="assignment:replacement",
            )
            with pytest.raises(RemoteSessionClaimConflict):
                sessions.claim_remote_session(
                    campaign_id,
                    "remote-session",
                    target_id="target-1",
                    session_scope_id="assignment:replacement",
                )


def test_finish_without_cleanup_records_claim_loser_without_touching_owner(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-cleanup-not-owned")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            campaign_id = "campaign/cleanup-not-owned"
            _prepare_campaign(connection, campaign_id)
            sessions = CampaignApiSessionRepository(connection)
            for scope in ("assignment:owner", "assignment:loser"):
                sessions.create(
                    campaign_id,
                    "target-1",
                    "sha256:adapter",
                    session_scope_id=scope,
                )
            owner = sessions.claim_remote_session(
                campaign_id,
                "remote-session",
                target_id="target-1",
                session_scope_id="assignment:owner",
            )

            loser = sessions.finish_without_cleanup(
                campaign_id,
                {"__cleanup_reason": "physical session owned by another scope"},
                target_id="target-1",
                session_scope_id="assignment:loser",
                disposition="cleanup_not_owned",
            )

            assert loser.cleanup_disposition == "cleanup_not_owned"
            assert loser.remote_session_id is None
            assert (
                sessions.load(
                    campaign_id,
                    target_id="target-1",
                    session_scope_id="assignment:owner",
                )
                == owner
            )
