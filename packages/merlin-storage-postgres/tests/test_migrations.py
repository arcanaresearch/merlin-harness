import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

import pytest
from merlin_storage_postgres import (
    CampaignStore,
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    SessionRepository,
    load_migrations,
)
from psycopg.errors import CheckViolation


def test_migrations_have_stable_versions_and_checksums() -> None:
    migrations = load_migrations()
    assert [migration.version for migration in migrations] == list(range(1, 98))
    assert next(item for item in migrations if item.version == 93).sql
    assert next(item for item in migrations if item.version == 94).sql
    assert next(item for item in migrations if item.version == 95).sql
    assert next(item for item in migrations if item.version == 96).sql
    assert next(item for item in migrations if item.version == 97).sql
    assert all(len(migration.checksum) == 64 for migration in migrations)
    assert "'partial'" in migrations[74].sql
    assert "ADD COLUMN partial_signal" in migrations[74].sql
    assert "'reproduction'" in migrations[75].sql
    assert "'review'" in migrations[75].sql
    assert "'missing_target_effect'" in migrations[76].sql
    assert "'result_schema_invalid'" in migrations[76].sql
    assert "browser_session_policy" in migrations[88].sql
    assert "'ephemeral', 'persistent'" in migrations[88].sql
    assert "CREATE TABLE merlin_storage.campaign_events" in migrations[89].sql
    assert "campaign_events_append_only" in migrations[89].sql
    assert "campaign_lead_directives_ready_idx" in migrations[90].sql
    assert "'planned', 'retry_wait', 'rejected', 'applied'" in migrations[90].sql
    assert "CREATE TABLE merlin_storage.daemon_runs" in migrations[91].sql
    assert migrations[-1].version == 97
    assert "'storage_exhausted'" in next(item for item in migrations if item.version == 95).sql
    assert "daemon_runs_one_active_idx" in migrations[91].sql
    assert "CREATE TABLE merlin_storage.technique_history" in migrations[60].sql
    assert "'blocked_terminal'" in migrations[61].sql
    assert "ADD COLUMN language_id" in migrations[62].sql
    assert "ADD COLUMN mutation_id" in migrations[62].sql
    assert "ALTER COLUMN objective_matched DROP NOT NULL" in migrations[63].sql
    assert "CREATE TABLE merlin_storage.events" in migrations[0].sql
    assert "ADD COLUMN tool_calls" in migrations[1].sql
    assert "CREATE TABLE merlin_storage.pack_runs" in migrations[2].sql
    assert "CREATE TABLE merlin_storage.agent_tasks" in migrations[3].sql
    assert "CREATE TABLE merlin_storage.findings" in migrations[5].sql
    assert "CREATE TABLE merlin_storage.scheduler_schedules" in migrations[6].sql
    assert "CREATE TABLE merlin_storage.permission_rules" in migrations[7].sql
    assert "CREATE TABLE merlin_storage.browser_contexts" in migrations[10].sql
    assert "CREATE TABLE merlin_storage.assessment_campaigns" in migrations[11].sql
    assert "CREATE TABLE merlin_storage.campaign_lead_journal" in migrations[12].sql
    assert "CREATE TABLE merlin_storage.campaign_lessons" in migrations[13].sql
    assert "CREATE TABLE merlin_storage.interaction_lessons" in migrations[18].sql
    assert "ADD COLUMN reasoning_content" in migrations[19].sql
    assert "ADD CONSTRAINT scheduler_schedules_status_check" in migrations[20].sql
    assert "ADD COLUMN operation_group" in migrations[20].sql
    assert "CREATE TABLE merlin_storage.campaign_worker_messages" in migrations[21].sql
    assert "ADD COLUMN attempt_identity" in migrations[22].sql
    assert "ADD COLUMN valid_until" in migrations[23].sql
    assert "CREATE TABLE merlin_storage.campaign_budget_reservations" in migrations[24].sql
    assert "ADD COLUMN reserved_cost_usd" in migrations[25].sql
    assert "'adapter'" in migrations[26].sql
    assert "CREATE TABLE merlin_storage.campaign_api_sessions" in migrations[27].sql
    assert "CREATE TABLE merlin_storage.campaign_lead_directives" in migrations[30].sql
    assert "CREATE TABLE merlin_storage.conversation_compactions" in migrations[15].sql
    assert "CREATE TABLE merlin_storage.tool_effects" in migrations[14].sql
    assert "CREATE TABLE merlin_storage.cleanup_receipts" in migrations[14].sql
    assert "CREATE TABLE merlin_storage.crescendo_step_receipts" in migrations[34].sql
    assert "CREATE TABLE merlin_storage.crescendo_terminal_receipts" in migrations[34].sql
    assert "ADD COLUMN scenario_fingerprint" in migrations[38].sql
    assert "CREATE TABLE merlin_storage.exploration_critic_decisions" in migrations[39].sql
    assert "CREATE TABLE merlin_storage.campaign_technique_attempts" in migrations[41].sql
    assert "CREATE TABLE merlin_storage.automation_artifacts" in migrations[42].sql
    assert "CREATE TABLE merlin_storage.automation_artifact_runs" in migrations[42].sql
    assert "CREATE TABLE merlin_storage.campaign_dynamic_specialists" in migrations[45].sql
    assert "'operator-continuation'" in migrations[43].sql
    assert "DROP CONSTRAINT campaign_arms_source_check" in migrations[50].sql
    assert "ADD CONSTRAINT campaign_arms_source_check" in migrations[50].sql
    assert "'failure-derived'" in migrations[50].sql
    assert "DROP INDEX IF EXISTS" in migrations[51].sql
    assert "report_revisions_completion_digest_idx" in migrations[51].sql
    assert "CREATE TABLE merlin_storage.campaign_hypotheses" in migrations[52].sql
    assert "CREATE TABLE merlin_storage.campaign_counter_events" in migrations[52].sql
    assert "CREATE TABLE merlin_storage.campaign_arm_outcomes" in migrations[52].sql
    assert "CREATE TABLE merlin_storage.technique_language_evidence" in migrations[52].sql
    assert "ADD COLUMN causal_ambiguity" in migrations[53].sql
    assert "ADD COLUMN impact_level" in migrations[53].sql
    assert "ADD COLUMN requires_independent_reproduction" in migrations[53].sql
    assert "'catalog-mutation'" in migrations[54].sql
    assert "ADD COLUMN execution_reachability" in migrations[55].sql
    assert "ADD COLUMN usage" in migrations[56].sql
    assert "ADD COLUMN generation" in migrations[58].sql
    assert "ADD COLUMN probe_token" in migrations[59].sql
    assert "CREATE INDEX tool_effects_campaign_technique_attempt_target_idx" in migrations[64].sql
    assert "ADD COLUMN session_scope_id" in migrations[65].sql
    assert "ADD COLUMN cleanup_disposition" in migrations[66].sql
    assert "ADD COLUMN failure_kind" in migrations[67].sql
    assert "'provider_transient'" in migrations[68].sql
    assert "ALTER COLUMN objective_matched DROP NOT NULL" in migrations[70].sql
    assert "'cleanup_requirement_unestablished'" in migrations[71].sql
    assert "ALTER COLUMN ttl_waves DROP NOT NULL" in migrations[72].sql
    assert "CREATE TABLE merlin_storage.campaign_capability_observations" in migrations[73].sql
    assert "CREATE TABLE merlin_storage.campaign_frontier_transition_leases" in migrations[81].sql
    assert "CREATE TABLE merlin_storage.target_constraints" in migrations[82].sql
    assert "CREATE TABLE merlin_storage.state_scopes" in migrations[83].sql
    assert "CREATE TABLE merlin_storage.principal_resources" in migrations[83].sql
    assert "CREATE TABLE merlin_storage.gate_checkpoints" in migrations[83].sql
    assert "CREATE TABLE merlin_storage.checkpoint_restores" in migrations[83].sql
    assert "CREATE TABLE merlin_storage.campaign_recovery" in migrations[84].sql
    assert "objective_satisfied" in migrations[85].sql
    assert "infrastructure_blocked_after_retry_policy" in migrations[85].sql
    assert "CREATE TABLE merlin_storage.state_lanes" in migrations[86].sql
    assert "CREATE TABLE merlin_storage.state_lane_revisions" in migrations[86].sql
    evidence = next(migration for migration in migrations if migration.version == 17)
    assert "CREATE TABLE merlin_storage.campaign_observations" in evidence.sql
    assert "CREATE TABLE IF NOT EXISTS merlin_storage.report_revisions" in migrations[17].sql


def test_target_admission_migration_materializes_preupgrade_transition_hold(
    tmp_path: Path,
) -> None:
    migrations = load_migrations()
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-target-latch-upgrade")) as database:
        with database.connect() as connection:
            runner = MigrationRunner(connection)
            runner.apply(migrations[:96])
            session = SessionRepository(connection).create("preupgrade target transition")
            campaign_id = "campaign/preupgrade-target-transition"
            target_id = "target/preupgrade"
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                campaign_id,
                str(session.id),
                {"manifest": {"id": campaign_id, "target": {"id": target_id}}},
                "sha256:reviewed",
            )
            event_digest = "sha256:" + "a" * 64
            payload = {
                "kind": "target_surface_transition_observed",
                "schema": "merlin.target-surface-transition-observed.v2",
                "campaign_id": campaign_id,
                "session_id": str(session.id),
                "target_id": target_id,
                "target_session_id": "browser:preupgrade",
                "event_digest": event_digest,
            }
            campaigns.commit_lead_decision(
                campaign_id,
                0,
                "target-surface-transition-observed/" + event_digest.removeprefix("sha256:"),
                payload,
                (),
            )
            legacy_event_digest = "sha256:" + "b" * 64
            campaigns.commit_lead_decision(
                campaign_id,
                0,
                "target-surface-transition-observed/" + legacy_event_digest.removeprefix("sha256:"),
                {
                    "kind": "target_surface_transition_observed",
                    "schema": "merlin.target-surface-transition-observed.v1",
                    "campaign_id": campaign_id,
                    "session_id": str(session.id),
                    "target_id": target_id,
                    "target_session_id": "browser:untrusted-legacy-claim",
                    "event_digest": legacy_event_digest,
                },
                (),
            )

            runner.apply(migrations[96:])

            session_latch = connection.execute(
                """SELECT state, transition_count, pending_event_digests,
                          event_set_digest
                   FROM merlin_storage.target_admission_latches
                   WHERE campaign_id = %s AND target_id = %s
                     AND target_session_id = 'browser:preupgrade'""",
                (campaign_id, target_id),
            ).fetchone()
            assert session_latch == {
                "state": "latched",
                "transition_count": 1,
                "pending_event_digests": [event_digest],
                "event_set_digest": None,
            }
            assert (
                connection.execute(
                    """SELECT count(*) AS count
                   FROM merlin_storage.target_admission_latches
                   WHERE campaign_id = %s AND target_id = %s
                     AND target_session_id = 'browser:untrusted-legacy-claim'""",
                    (campaign_id, target_id),
                ).fetchone()["count"]
                == 0
            )
            head = connection.execute(
                """SELECT state, transition_count, latched_session_count
                   FROM merlin_storage.target_admission_latches
                   WHERE campaign_id = %s AND target_id = %s AND target_session_id = ''""",
                (campaign_id, target_id),
            ).fetchone()
            assert head == {
                "state": "latched",
                "transition_count": 2,
                "latched_session_count": 2,
            }


def test_target_admission_migration_ignores_untrusted_settlement_claims(
    tmp_path: Path,
) -> None:
    migrations = load_migrations()
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-target-latch-forgery")) as database:
        with database.connect() as connection:
            runner = MigrationRunner(connection)
            runner.apply(migrations[:96])
            session = SessionRepository(connection).create("forged transition settlement")
            campaign_id = "campaign/forged-transition-settlement"
            target_id = "target/forged-transition-settlement"
            target_session_id = "browser:forged-transition-settlement"
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                campaign_id,
                str(session.id),
                {"manifest": {"id": campaign_id, "target": {"id": target_id}}},
                "sha256:reviewed",
            )
            event_digest = "sha256:" + "c" * 64
            campaigns.commit_lead_decision(
                campaign_id,
                0,
                "target-surface-transition-observed/" + event_digest.removeprefix("sha256:"),
                {
                    "kind": "target_surface_transition_observed",
                    "schema": "merlin.target-surface-transition-observed.v2",
                    "campaign_id": campaign_id,
                    "session_id": str(session.id),
                    "target_id": target_id,
                    "target_session_id": target_session_id,
                    "event_digest": event_digest,
                },
                (),
            )
            for suffix, claimed_digests in (
                ("real", [event_digest]),
                ("unknown", ["sha256:" + "d" * 64]),
            ):
                campaigns.commit_lead_decision(
                    campaign_id,
                    0,
                    f"target-reobservation-settlement:{suffix}",
                    {
                        "kind": "target_reobservation_settled",
                        "schema": "merlin.target-reobservation-settlement.v1",
                        "status": "refreshed",
                        "campaign_id": campaign_id,
                        "session_id": str(session.id),
                        "target_id": target_id,
                        "target_session_id": target_session_id,
                        "event_digests": claimed_digests,
                    },
                    (),
                )

            runner.apply(migrations[96:])

            latch = connection.execute(
                """SELECT state, transition_count, pending_event_digests,
                          pending_event_count
                   FROM merlin_storage.target_admission_latches
                   WHERE campaign_id = %s AND target_id = %s
                     AND target_session_id = %s""",
                (campaign_id, target_id, target_session_id),
            ).fetchone()
            assert latch == {
                "state": "latched",
                "transition_count": 1,
                "pending_event_digests": [event_digest],
                "pending_event_count": 1,
            }


def test_target_admission_migration_rejects_fresh_v1_but_allows_exact_replay(
    tmp_path: Path,
) -> None:
    migrations = load_migrations()
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-target-v1-interlock")) as database:
        with database.connect() as connection:
            runner = MigrationRunner(connection)
            runner.apply(migrations[:96])
            session = SessionRepository(connection).create("legacy transition interlock")
            campaign_id = "campaign/legacy-transition-interlock"
            target_id = "target/legacy-transition-interlock"
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                campaign_id,
                str(session.id),
                {"manifest": {"id": campaign_id, "target": {"id": target_id}}},
                "sha256:reviewed",
            )
            event_digest = "sha256:" + "e" * 64
            decision_id = "target-surface-transition-observed/" + event_digest.removeprefix(
                "sha256:"
            )
            payload = {
                "kind": "target_surface_transition_observed",
                "schema": "merlin.target-surface-transition-observed.v1",
                "campaign_id": campaign_id,
                "session_id": str(session.id),
                "target_id": target_id,
                "event_digest": event_digest,
            }
            original = campaigns.commit_lead_decision(campaign_id, 0, decision_id, payload, ())

            runner.apply(migrations[96:])

            replay = campaigns.commit_lead_decision(campaign_id, 0, decision_id, payload, ())
            assert replay["sequence"] == original["sequence"]
            fresh_digest = "sha256:" + "f" * 64
            with pytest.raises(
                CheckViolation,
                match="legacy target transition observations are read-only",
            ):
                campaigns.commit_lead_decision(
                    campaign_id,
                    0,
                    "target-surface-transition-observed/" + fresh_digest.removeprefix("sha256:"),
                    {**payload, "event_digest": fresh_digest},
                    (),
                )
            assert (
                connection.execute(
                    """SELECT count(*) AS count
                   FROM merlin_storage.campaign_lead_journal
                   WHERE campaign_id = %s
                     AND payload->>'schema' =
                         'merlin.target-surface-transition-observed.v1'""",
                    (campaign_id,),
                ).fetchone()["count"]
                == 1
            )


def test_legacy_writer_racing_with_upgrader_cannot_append(
    tmp_path: Path,
) -> None:
    migrations = load_migrations()
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-target-v1-race")) as database:
        with database.connect() as connection:
            runner = MigrationRunner(connection)
            runner.apply(migrations[:96])
            session = SessionRepository(connection).create("legacy transition upgrade race")
            session_id = str(session.id)
            campaign_id = "campaign/legacy-transition-upgrade-race"
            target_id = "target/legacy-transition-upgrade-race"
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                campaign_id,
                session_id,
                {"manifest": {"id": campaign_id, "target": {"id": target_id}}},
                "sha256:reviewed",
            )
            existing_digest = "sha256:" + "1" * 64
            existing_decision_id = (
                "target-surface-transition-observed/" + existing_digest.removeprefix("sha256:")
            )
            base_payload = {
                "kind": "target_surface_transition_observed",
                "schema": "merlin.target-surface-transition-observed.v1",
                "campaign_id": campaign_id,
                "session_id": session_id,
                "target_id": target_id,
                "event_digest": existing_digest,
            }
            campaigns.commit_lead_decision(campaign_id, 0, existing_decision_id, base_payload, ())
            runner.apply(migrations[96:])

        upgrade_pids: Queue[int] = Queue()

        def upgrade() -> dict[str, object]:
            with database.connect() as upgrade_connection:
                upgrade_pids.put(
                    upgrade_connection.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
                )
                return DurableSchedulerStore(upgrade_connection).upgrade_legacy_target_admission(
                    campaign_id, session_id, target_id
                )

        def write_fresh_v1() -> str:
            with database.connect() as writer_connection:
                fresh_digest = "sha256:" + "2" * 64
                try:
                    CampaignStore(writer_connection).commit_lead_decision(
                        campaign_id,
                        0,
                        "target-surface-transition-observed/"
                        + fresh_digest.removeprefix("sha256:"),
                        {**base_payload, "event_digest": fresh_digest},
                        (),
                    )
                except CheckViolation as error:
                    return str(error.sqlstate)
                return "inserted"

        def wait_for_lock(connection: object, pid: int) -> None:
            deadline = time.monotonic() + 10
            last_row = None
            while time.monotonic() < deadline:
                row = connection.execute(
                    """SELECT state, wait_event_type, wait_event, query
                       FROM pg_stat_activity WHERE pid = %s""",
                    (pid,),
                ).fetchone()
                last_row = row
                if row is not None and row["wait_event_type"] == "Lock":
                    return
                time.sleep(0.01)
            pytest.fail(f"backend {pid} did not reach the expected lock wait: {last_row!r}")

        with database.connect() as blocker_connection:
            with ThreadPoolExecutor(max_workers=2) as executor:
                with blocker_connection.transaction():
                    blocker_connection.execute(
                        """SELECT id
                           FROM merlin_storage.assessment_campaigns
                           WHERE id = %s
                           FOR UPDATE""",
                        (campaign_id,),
                    ).fetchone()
                    upgrade_future = executor.submit(upgrade)
                    wait_for_lock(blocker_connection, upgrade_pids.get(timeout=10))
                    writer_future = executor.submit(write_fresh_v1)
                    writer_result = writer_future.result(timeout=10)
                    assert not upgrade_future.done()

                upgraded = upgrade_future.result(timeout=15)

        assert upgraded["status"] == "blocked"
        assert writer_result == "23514"
        with database.connect() as connection:
            assert (
                connection.execute(
                    """SELECT count(*) AS count
                   FROM merlin_storage.campaign_lead_journal
                   WHERE campaign_id = %s
                     AND payload->>'schema' =
                         'merlin.target-surface-transition-observed.v1'""",
                    (campaign_id,),
                ).fetchone()["count"]
                == 1
            )


def test_campaign_terminal_taxonomy_migrates_legacy_rows_and_rejects_legacy_writes(
    tmp_path: Path,
) -> None:
    migrations = load_migrations()
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-terminal-taxonomy")) as database:
        with database.connect() as connection:
            runner = MigrationRunner(connection)
            runner.apply(migrations[:85])
            session = SessionRepository(connection).create("legacy campaign terminal reason")
            campaigns = CampaignStore(connection)
            campaigns.prepare("campaign/legacy-terminal", str(session.id), {}, "sha256:legacy")
            connection.execute(
                """UPDATE merlin_storage.assessment_campaigns
                   SET status = 'failed', stop_reason = 'failed'
                   WHERE id = 'campaign/legacy-terminal'"""
            )
            connection.commit()

            runner.apply(migrations[85:])

            row = connection.execute(
                """SELECT stop_reason FROM merlin_storage.assessment_campaigns
                   WHERE id = 'campaign/legacy-terminal'"""
            ).fetchone()
            assert row["stop_reason"] == "infrastructure_blocked_after_retry_policy"
            with connection.transaction(), pytest.raises(CheckViolation):
                connection.execute(
                    """UPDATE merlin_storage.assessment_campaigns
                       SET stop_reason = 'failed'
                       WHERE id = 'campaign/legacy-terminal'"""
                )


def test_campaign_store_writes_only_current_terminal_reasons(tmp_path: Path) -> None:
    reasons = (
        "objective_satisfied",
        "frontier_exhausted",
        "budget_exhausted",
        "target_terminal",
        "operator_stopped",
        "infrastructure_blocked_after_retry_policy",
    )
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-current-terminal")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply(load_migrations())
            session = SessionRepository(connection).create("current campaign terminal reasons")
            campaigns = CampaignStore(connection)
            for index, reason in enumerate(reasons):
                campaign_id = f"campaign/current-terminal-{index}"
                campaigns.prepare(campaign_id, str(session.id), {}, f"sha256:{index}")
                status = (
                    "cancelled"
                    if reason == "operator_stopped"
                    else "failed"
                    if reason == "infrastructure_blocked_after_retry_policy"
                    else "completed"
                )
                row = campaigns.finish(campaign_id, status, stop_reason=reason)
                assert row["stop_reason"] == reason

            campaigns.prepare("campaign/reject-legacy", str(session.id), {}, "sha256:reject")
            with pytest.raises(ValueError, match="invalid current"):
                campaigns.finish("campaign/reject-legacy", "failed", stop_reason="failed")


def test_api_session_scope_migration_reserves_legacy_rows(tmp_path: Path) -> None:
    migrations = load_migrations()
    campaign_id = "campaign/legacy-api-session"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-api-scope")) as database:
        with database.connect() as connection:
            runner = MigrationRunner(connection)
            runner.apply(migrations[:65])
            session = SessionRepository(connection).create("legacy API scope")
            CampaignStore(connection).prepare(
                campaign_id,
                str(session.id),
                {"manifest": {"id": campaign_id}, "assignment_context": {}},
                "sha256:reviewed",
            )
            connection.execute(
                """INSERT INTO merlin_storage.campaign_api_sessions
                       (campaign_id, target_id, adapter_digest, state)
                   VALUES (%s, 'target-1', 'sha256:adapter', 'active')""",
                (campaign_id,),
            )
            connection.commit()

            runner.apply(migrations[65:])

            row = connection.execute(
                """SELECT target_id, session_scope_id, remote_session_id
                   FROM merlin_storage.campaign_api_sessions
                   WHERE campaign_id = %s""",
                (campaign_id,),
            ).fetchone()
            assert row == {
                "target_id": "target-1",
                "session_scope_id": "legacy:campaign",
                "remote_session_id": None,
            }


def test_stateful_target_migration_preserves_legacy_attempts_conservatively(tmp_path: Path) -> None:
    migrations = load_migrations()
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-stateful-legacy")) as database:
        with database.connect() as connection:
            runner = MigrationRunner(connection)
            runner.apply(migrations[:81])
            session = SessionRepository(connection).create("legacy stateful attempts")
            CampaignStore(connection).prepare(
                "campaign/legacy-stateful", str(session.id), {}, "sha256:legacy"
            )
            for identifier, technique_id, strategy_state in (
                (
                    "00000000-0000-0000-0000-000000000081",
                    "technique/model-judged",
                    {},
                ),
                (
                    "00000000-0000-0000-0000-000000000082",
                    "technique/target-authored",
                    {"outcome_authority": {"kind": "target"}},
                ),
            ):
                connection.execute(
                    """INSERT INTO merlin_storage.campaign_technique_attempts
                           (id, campaign_id, wave, task_id, technique_id, strategy_id,
                            control_kind, oracle_digest, state, strategy_state,
                            terminal_reason, activated_at, terminal_at)
                       VALUES (%s, 'campaign/legacy-stateful', 0, %s, %s, 'direct',
                               'attack', %s, 'refuted', %s::jsonb,
                               'legacy terminal result', now(), now())""",
                    (
                        identifier,
                        "worker/" + technique_id.rsplit("/", 1)[-1],
                        technique_id,
                        "sha256:" + "a" * 64,
                        json.dumps(strategy_state),
                    ),
                )
            connection.commit()

            runner.apply(migrations[81:])

            rows = connection.execute(
                """SELECT technique_id, disposition, disposition_reason
                   FROM merlin_storage.campaign_technique_attempts
                   WHERE campaign_id = 'campaign/legacy-stateful'
                   ORDER BY technique_id"""
            ).fetchall()
            assert [(row["technique_id"], row["disposition"]) for row in rows] == [
                ("technique/model-judged", "uncertain"),
                ("technique/target-authored", "valid"),
            ]
            assert all(row["disposition_reason"] for row in rows)


def test_observability_migration_discards_only_partial_arm_learning_rows(tmp_path: Path) -> None:
    migrations = load_migrations()
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            runner = MigrationRunner(connection)
            runner.apply(migrations[:52])
            session = SessionRepository(connection).create("preserved evidence authority")
            connection.execute(
                """INSERT INTO merlin_storage.campaign_arms
                       (id, target_id, scenario_fingerprint, semantic_id, strategy_id,
                        technique_id, objective, source)
                   VALUES ('old-arm', 'target-1', 'scenario-1', 'semantic-1', 'direct',
                           'prompt-override', 'Old partial arm', 'seed')"""
            )
            connection.commit()

            runner.apply(migrations)

            assert (
                connection.execute(
                    "SELECT count(*) AS value FROM merlin_storage.campaign_arms"
                ).fetchone()["value"]
                == 0
            )
            assert SessionRepository(connection).get(session.id) == session


def test_finding_validation_policy_migration_preserves_legacy_rows(tmp_path: Path) -> None:
    migrations = load_migrations()
    finding_id = "11111111-1111-1111-1111-111111111111"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-findings")) as database:
        with database.connect() as connection:
            runner = MigrationRunner(connection)
            runner.apply(migrations[:53])
            session = SessionRepository(connection).create("legacy finding")
            connection.execute(
                """INSERT INTO merlin_storage.findings
                       (id, session_id, title, severity, summary, reproduction,
                        remediation, creator, identity)
                   VALUES (%s, %s, 'Legacy finding', 'medium', 'Legacy evidence',
                           'Repeat legacy steps', 'Apply legacy remediation',
                           'agent:legacy', 'legacy:finding')""",
                (finding_id, session.id),
            )
            connection.commit()

            runner.apply(migrations)

            row = connection.execute(
                """SELECT causal_ambiguity, impact_level,
                          requires_independent_reproduction
                   FROM merlin_storage.findings WHERE id = %s""",
                (finding_id,),
            ).fetchone()
            assert row == {
                "causal_ambiguity": False,
                "impact_level": 0,
                "requires_independent_reproduction": False,
            }
