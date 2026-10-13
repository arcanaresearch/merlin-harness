from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from merlin_contracts.state_scope import (
    LEGACY_ATTEMPT_SCHEMA_VERSION,
    STATE_SCOPE_MIGRATION,
    STATE_SCOPE_SCHEMA_VERSION,
    StateScopeEnvelope,
)
from merlin_storage_postgres import (
    CampaignApiSessionRepository,
    CampaignStore,
    Database,
    DatabaseConfig,
    MigrationRunner,
    SessionRepository,
    load_migrations,
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


def _prepare_campaign(connection, campaign_id: str) -> dict[str, object]:
    session = SessionRepository(connection).create(f"state scope {campaign_id}")
    return CampaignStore(connection).prepare(
        campaign_id,
        str(session.id),
        {"objective": "verify durable state isolation", "campaign": campaign_id},
        f"sha256:{campaign_id}",
    )


def _missing(reason: str = "not_yet_observed") -> dict[str, object]:
    return {
        "availability": "missing",
        "evidence_refs": [],
        "item_count": 0,
        "missing": {"reason": reason, "retryable": reason == "transport_failed"},
    }


def _present(reference: str) -> dict[str, object]:
    return {"availability": "present", "evidence_refs": [reference], "item_count": 1}


def _envelope(
    *,
    campaign_id: str,
    scope_id: str,
    action_id: str,
    revision: int = 1,
    isolation_lease_id: str | None = None,
    delivered: bool = False,
    complete: bool = False,
    principal_reference: str | None = "principal-one",
    principal_digest: str | None = None,
) -> StateScopeEnvelope:
    return StateScopeEnvelope.model_validate(
        {
            "revision": revision,
            "scope_id": scope_id,
            "campaign_id": campaign_id,
            "experiment_id": f"experiment:{scope_id}",
            "attempt_id": f"attempt:{scope_id}",
            "action_id": action_id,
            "action_kind": "candidate",
            "target_authority": "https://target.example",
            "candidate_id": f"candidate:{scope_id}",
            "isolation_lease_id": isolation_lease_id,
            **(
                {
                    "principal_identity": {
                        "kind": "keyed_digest",
                        "algorithm": "hmac-sha256",
                        "key_id": "identity-v1",
                        "digest": principal_digest,
                    }
                }
                if principal_digest is not None
                else {
                    "principal_identity": {
                        "kind": "opaque_reference",
                        "namespace": "identity",
                        "reference_id": principal_reference,
                    }
                }
                if principal_reference is not None
                else {}
            ),
            "physical_state_identity": {
                "kind": "keyed_digest",
                "algorithm": "hmac-sha256",
                "key_id": "state-v1",
                "digest": "a" * 64,
            },
            "before": _present(f"evidence:{scope_id}:before"),
            "action": _present(f"evidence:{scope_id}:action") if delivered else _missing(),
            "responses": (_present(f"evidence:{scope_id}:responses") if complete else _missing()),
            "after": _present(f"evidence:{scope_id}:after") if complete else _missing(),
        }
    )


def test_conservative_lease_rekeys_atomically_to_reviewed_principal_and_alias(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            _prepare_campaign(connection, "campaign/refine-a")
            _prepare_campaign(connection, "campaign/refine-b")
            _prepare_campaign(connection, "campaign/refine-c")
            _prepare_campaign(connection, "campaign/refine-d")
            store = CampaignApiSessionRepository(connection)
            for suffix in ("a", "b", "c", "d"):
                store.create_state_scope(
                    f"campaign/refine-{suffix}",
                    "https://target.example",
                    _envelope(
                        campaign_id=f"campaign/refine-{suffix}",
                        scope_id=f"scope-refine-{suffix}",
                        action_id=f"action-refine-{suffix}",
                        principal_reference=None,
                    ),
                )

            first = store.claim_state_scope_lease(
                "scope-refine-a",
                target_authority="https://target.example",
                principal_key="anonymous",
                equivalence_key="conservative-shared-state",
                claimant_id="daemon-a",
                lease_seconds=60,
            )
            assert first is not None
            principal = "keyed_digest:hmac-sha256:identity-v1:" + "b" * 64
            alias = "keyed_digest:hmac-sha256:identity-v1:" + "c" * 64
            assert store.rekey_state_scope_lease(
                first,
                "daemon-a",
                principal_key=principal,
                equivalence_key=alias,
            )
            row = connection.execute(
                """SELECT principal_key, equivalence_key FROM merlin_storage.state_scope_leases
                   WHERE claim_id = %s""",
                (first,),
            ).fetchone()
            assert row == {"principal_key": principal, "equivalence_key": alias}

            second = store.claim_state_scope_lease(
                "scope-refine-b",
                target_authority="https://target.example",
                principal_key="anonymous",
                equivalence_key="conservative-shared-state",
                claimant_id="daemon-b",
                lease_seconds=60,
            )
            assert second is not None
            assert (
                store.rekey_state_scope_lease(
                    second,
                    "daemon-b",
                    principal_key=principal,
                    equivalence_key=alias,
                )
                is False
            )
            different_principal = "keyed_digest:hmac-sha256:identity-v1:" + "d" * 64
            assert store.rekey_state_scope_lease(
                second,
                "daemon-b",
                principal_key=different_principal,
                equivalence_key=alias,
            )
            third = store.claim_state_scope_lease(
                "scope-refine-c",
                target_authority="https://target.example",
                principal_key="anonymous",
                equivalence_key="conservative-shared-state",
                claimant_id="daemon-c",
                lease_seconds=60,
            )
            assert third is not None
            assert store.rekey_state_scope_lease(
                third,
                "daemon-c",
                principal_key=principal,
                equivalence_key="keyed_digest:hmac-sha256:identity-v1:" + "e" * 64,
                maximum_parallel_states=2,
            )
            fourth = store.claim_state_scope_lease(
                "scope-refine-d",
                target_authority="https://target.example",
                principal_key="anonymous",
                equivalence_key="conservative-shared-state",
                claimant_id="daemon-d",
                lease_seconds=60,
            )
            assert fourth is not None
            assert (
                store.rekey_state_scope_lease(
                    fourth,
                    "daemon-d",
                    principal_key=principal,
                    equivalence_key="keyed_digest:hmac-sha256:identity-v1:" + "f" * 64,
                    maximum_parallel_states=2,
                )
                is False
            )
            assert store.reserve_resource(
                "scope-refine-a",
                "reservation-refine-a",
                target_authority="https://target.example",
                principal_key=principal,
                resource_kind="turn",
                amount=1,
                limit=1,
                window_key="shared-window",
                claim_id=first,
                claimant_id="daemon-a",
            )["admitted"]
            assert store.reserve_resource(
                "scope-refine-b",
                "reservation-refine-b",
                target_authority="https://target.example",
                principal_key=different_principal,
                resource_kind="turn",
                amount=1,
                limit=1,
                window_key="shared-window",
                claim_id=second,
                claimant_id="daemon-b",
            )["admitted"]
            store.revise_state_scope(
                "scope-refine-a",
                1,
                _envelope(
                    campaign_id="campaign/refine-a",
                    scope_id="scope-refine-a",
                    action_id="action-refine-a",
                    revision=2,
                    isolation_lease_id=first,
                    delivered=True,
                    principal_reference=None,
                    principal_digest="b" * 64,
                ),
                claim_id=first,
                claimant_id="daemon-a",
            )
            store.acknowledge_state_scope("scope-refine-a", claim_id=first, claimant_id="daemon-a")
            with pytest.raises(ValueError, match="principal identity cannot be changed"):
                store.rekey_state_scope_lease(
                    first,
                    "daemon-a",
                    principal_key=different_principal,
                    equivalence_key=alias,
                )


def test_state_scopes_fence_cross_campaign_effects_and_share_principal_quota(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            _prepare_campaign(connection, "campaign/a")
            _prepare_campaign(connection, "campaign/b")
            store = CampaignApiSessionRepository(connection)
            store.create_state_scope(
                "campaign/a",
                "https://target.example",
                _envelope(campaign_id="campaign/a", scope_id="scope-a", action_id="action-a"),
            )
            store.create_state_scope(
                "campaign/b",
                "https://target.example",
                _envelope(campaign_id="campaign/b", scope_id="scope-b", action_id="action-b"),
            )

            claim_a = store.claim_state_scope_lease(
                "scope-a",
                target_authority="https://target.example",
                principal_key="opaque_reference:identity:principal-one",
                equivalence_key="keyed_digest:hmac-sha256:state-v1:" + "a" * 64,
                claimant_id="daemon-a",
                lease_seconds=60,
            )
            assert claim_a is not None
            with database.connect() as competing_connection:
                competing = CampaignApiSessionRepository(competing_connection)
                assert (
                    competing.claim_state_scope_lease(
                        "scope-b",
                        target_authority="https://target.example",
                        principal_key="opaque_reference:identity:principal-one",
                        equivalence_key="keyed_digest:hmac-sha256:state-v1:" + "a" * 64,
                        claimant_id="daemon-b",
                        lease_seconds=60,
                    )
                    is None
                )
                assert (
                    competing.create_and_claim_state_scope(
                        "campaign/b",
                        "https://target.example",
                        _envelope(
                            campaign_id="campaign/b",
                            scope_id="scope-loser",
                            action_id="action-loser",
                        ),
                        principal_key="opaque_reference:identity:principal-one",
                        equivalence_key="keyed_digest:hmac-sha256:state-v1:" + "a" * 64,
                        claimant_id="daemon-b",
                        lease_seconds=60,
                    )
                    is None
                )
                assert competing.load_state_scope("scope-loser") is None

            reservation = store.reserve_resource(
                "scope-a",
                "reservation-a",
                target_authority="https://target.example",
                principal_key="opaque_reference:identity:principal-one",
                resource_kind="turn",
                amount=1,
                limit=1,
                window_key="game-run-one",
                claim_id=claim_a,
                claimant_id="daemon-a",
            )
            assert reservation["admitted"] is True
            store.revise_state_scope(
                "scope-a",
                1,
                _envelope(
                    campaign_id="campaign/a",
                    scope_id="scope-a",
                    action_id="action-a",
                    revision=2,
                    isolation_lease_id=claim_a,
                    delivered=True,
                ),
                claim_id=claim_a,
                claimant_id="daemon-a",
            )
            store.acknowledge_state_scope("scope-a", claim_id=claim_a, claimant_id="daemon-a")
            store.acknowledge_resource("reservation-a", claim_id=claim_a, claimant_id="daemon-a")
            assert (
                store.reserve_resource(
                    "scope-a",
                    "reservation-a",
                    target_authority="https://target.example",
                    principal_key="opaque_reference:identity:principal-one",
                    resource_kind="turn",
                    amount=1,
                    limit=1,
                    window_key="game-run-one",
                    claim_id=claim_a,
                    claimant_id="daemon-a",
                )["admitted"]
                is False
            )
            wake_at = datetime.now(UTC) + timedelta(minutes=5)
            store.settle_resource(
                "reservation-a",
                actual_amount=1,
                terminal_block=True,
                wake_at=wake_at,
                claim_id=claim_a,
                claimant_id="daemon-a",
            )
            settled = store.settle_state_scope(
                "scope-a",
                2,
                _envelope(
                    campaign_id="campaign/a",
                    scope_id="scope-a",
                    action_id="action-a",
                    revision=3,
                    isolation_lease_id=claim_a,
                    delivered=True,
                    complete=True,
                ),
                transition_disposition="progress",
                isolation_disposition="fresh",
                cleanup_disposition="completed",
                claim_id=claim_a,
                claimant_id="daemon-a",
            )
            assert settled["status"] == "settled"
            assert store.release_state_scope_lease(claim_a, "daemon-a") is True

            claim_b = store.claim_state_scope_lease(
                "scope-b",
                target_authority="https://target.example",
                principal_key="opaque_reference:identity:principal-one",
                equivalence_key="keyed_digest:hmac-sha256:state-v1:" + "a" * 64,
                claimant_id="daemon-b",
                lease_seconds=60,
            )
            assert claim_b is not None
            denied = store.reserve_resource(
                "scope-b",
                "reservation-b",
                target_authority="https://target.example",
                principal_key="opaque_reference:identity:principal-one",
                resource_kind="turn",
                amount=1,
                limit=1,
                window_key="game-run-one",
                claim_id=claim_b,
                claimant_id="daemon-b",
            )
            assert denied["admitted"] is False
            paused = store.pause_state_scope(
                "scope-b",
                1,
                _envelope(
                    campaign_id="campaign/b",
                    scope_id="scope-b",
                    action_id="action-b",
                    revision=2,
                    isolation_lease_id=claim_b,
                ),
                transition_disposition="infrastructure_failure",
                isolation_disposition="shared_serialized",
                cleanup_disposition="not_required",
                claim_id=claim_b,
                claimant_id="daemon-b",
            )
            assert paused["status"] == "paused"
            assert paused["effect_acknowledged_at"] is None
            store.release_state_scope_lease(claim_b, "daemon-b")

            report = store.reconcile_durable_lifecycle(
                claimant_id="recovery-a",
                readable_formats=STATE_SCOPE_MIGRATION.readable_versions,
                writable_format=STATE_SCOPE_SCHEMA_VERSION,
                lease_seconds=30,
            )
            campaign_b = next(
                row for row in report["campaigns"] if row["campaign_id"] == "campaign/b"
            )
            assert campaign_b["decision"] == "wait_quota"
            assert campaign_b["status"] == "paused"
            assert campaign_b["wake_at"] == wake_at

            connection.execute(
                """UPDATE merlin_storage.principal_resources
                   SET wake_at = now() - interval '1 second'
                   WHERE target_authority = 'https://target.example'"""
            )
            resumed = store.reconcile_durable_lifecycle(
                claimant_id="recovery-b",
                readable_formats=STATE_SCOPE_MIGRATION.readable_versions,
                writable_format=STATE_SCOPE_SCHEMA_VERSION,
                lease_seconds=30,
            )
            resumed_b = next(
                row for row in resumed["campaigns"] if row["campaign_id"] == "campaign/b"
            )
            assert resumed_b["decision"] == "resume"
            assert resumed_b["status"] == "prepared"

            connection.execute(
                """UPDATE merlin_storage.state_scopes SET cleanup_disposition = 'pending'
                   WHERE id = 'scope-b'"""
            )
            cleanup = store.reconcile_durable_lifecycle(
                claimant_id="recovery-cleanup",
                readable_formats=STATE_SCOPE_MIGRATION.readable_versions,
                writable_format=STATE_SCOPE_SCHEMA_VERSION,
                lease_seconds=30,
            )
            cleanup_b = next(
                row for row in cleanup["campaigns"] if row["campaign_id"] == "campaign/b"
            )
            assert cleanup_b["decision"] == "cleanup_required"
            assert cleanup_b["cleanup_obligations"] == 1
            assert cleanup_b["status"] == "paused"

            connection.execute(
                """UPDATE merlin_storage.state_scopes SET cleanup_disposition = 'completed'
                   WHERE id = 'scope-b'"""
            )
            connection.execute(
                """UPDATE merlin_storage.principal_resources
                   SET state = 'exhausted', wake_at = NULL
                   WHERE target_authority = 'https://target.example'"""
            )
            indefinite = store.reconcile_durable_lifecycle(
                claimant_id="recovery-indefinite",
                readable_formats=STATE_SCOPE_MIGRATION.readable_versions,
                writable_format=STATE_SCOPE_SCHEMA_VERSION,
                lease_seconds=30,
            )
            indefinite_b = next(
                row for row in indefinite["campaigns"] if row["campaign_id"] == "campaign/b"
            )
            assert indefinite_b["decision"] == "wait_quota"
            assert indefinite_b["quota_indefinite"] is True
            assert indefinite_b["wake_at"] is None


def test_state_scope_migration_preserves_legacy_campaign_and_fixed_forward_gate(
    tmp_path: Path,
) -> None:
    migrations = load_migrations()
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply(migrations[:-1])
            original = _prepare_campaign(connection, "campaign/legacy")
            MigrationRunner(connection).apply(migrations)
            stored = CampaignStore(connection).get("campaign/legacy")
            assert stored is not None
            assert stored["manifest"] == original["manifest"]
            assert stored["manifest_digest"] == original["manifest_digest"]
            assert stored["execution_format"] == LEGACY_ATTEMPT_SCHEMA_VERSION

            repository = CampaignApiSessionRepository(connection)
            assert repository.set_scope_claims_enabled(STATE_SCOPE_SCHEMA_VERSION, False)
            with pytest.raises(ValueError, match="unavailable"):
                repository.create_state_scope(
                    "campaign/legacy",
                    "https://target.example",
                    _envelope(
                        campaign_id="campaign/legacy",
                        scope_id="scope-disabled",
                        action_id="action-disabled",
                    ),
                )
            assert (
                CampaignStore(connection).get("campaign/legacy")["manifest_digest"]
                == original["manifest_digest"]
            )
            connection.execute(
                """UPDATE merlin_storage.assessment_campaigns
                   SET execution_format = 'merlin.state-scope.v2', status = 'executing'
                   WHERE id = 'campaign/legacy'"""
            )
            report = repository.reconcile_durable_lifecycle(
                claimant_id="older-reader",
                readable_formats=STATE_SCOPE_MIGRATION.readable_versions,
                writable_format=STATE_SCOPE_SCHEMA_VERSION,
                lease_seconds=30,
            )
            legacy = next(
                row for row in report["campaigns"] if row["campaign_id"] == "campaign/legacy"
            )
            assert legacy["decision"] == "pause_fixed_forward"
            assert legacy["status"] == "paused"
            with pytest.raises(ValueError, match="lifecycle-paused"):
                CampaignStore(connection).resume("campaign/legacy")


def test_objective_gate_checkpoint_is_fenced_and_reusable_across_restore_scopes(
    tmp_path: Path,
) -> None:
    campaign_id = "campaign/checkpoint"
    authority = "https://target.example"
    principal = "opaque_reference:identity:principal-one"
    equivalence = "keyed_digest:hmac-sha256:state-v1:" + "a" * 64
    recipe_digest = "sha256:" + "1" * 64
    evidence_digest = "sha256:" + "2" * 64

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            _prepare_campaign(connection, campaign_id)
            scopes = CampaignApiSessionRepository(connection)
            campaigns = CampaignStore(connection)
            scopes.create_state_scope(
                campaign_id,
                authority,
                _envelope(
                    campaign_id=campaign_id,
                    scope_id="scope-capture",
                    action_id="action-capture",
                ),
            )
            capture_claim = scopes.claim_state_scope_lease(
                "scope-capture",
                target_authority=authority,
                principal_key=principal,
                equivalence_key=equivalence,
                claimant_id="capture-worker",
                lease_seconds=60,
            )
            assert capture_claim is not None
            scopes.revise_state_scope(
                "scope-capture",
                1,
                _envelope(
                    campaign_id=campaign_id,
                    scope_id="scope-capture",
                    action_id="action-capture",
                    revision=2,
                    isolation_lease_id=capture_claim,
                    delivered=True,
                ),
                claim_id=capture_claim,
                claimant_id="capture-worker",
            )
            scopes.acknowledge_state_scope(
                "scope-capture", claim_id=capture_claim, claimant_id="capture-worker"
            )
            checkpoint = campaigns.record_objective_gate_checkpoint(
                campaign_id,
                "gate-login",
                manifest_digest=f"sha256:{campaign_id}",
                recipe_digest=recipe_digest,
                checkpoint_handle={
                    "kind": "opaque_reference",
                    "namespace": "state",
                    "reference_id": "checkpoint-vault-entry-7",
                },
                evidence_digest=evidence_digest,
                scope_id="scope-capture",
                claim_id=capture_claim,
                claimant_id="capture-worker",
            )
            assert checkpoint["checkpoint_handle"]["reference_id"] == "checkpoint-vault-entry-7"
            scopes.settle_state_scope(
                "scope-capture",
                2,
                _envelope(
                    campaign_id=campaign_id,
                    scope_id="scope-capture",
                    action_id="action-capture",
                    revision=3,
                    isolation_lease_id=capture_claim,
                    delivered=True,
                    complete=True,
                ),
                transition_disposition="progress",
                isolation_disposition="fresh",
                cleanup_disposition="completed",
                claim_id=capture_claim,
                claimant_id="capture-worker",
            )
            scopes.release_state_scope_lease(capture_claim, "capture-worker")

            scopes.create_state_scope(
                campaign_id,
                authority,
                _envelope(
                    campaign_id=campaign_id,
                    scope_id="scope-restore",
                    action_id="action-restore",
                ),
            )
            restore_claim = scopes.claim_state_scope_lease(
                "scope-restore",
                target_authority=authority,
                principal_key=principal,
                equivalence_key=equivalence,
                claimant_id="restore-worker",
                lease_seconds=60,
            )
            assert restore_claim is not None
            restore = campaigns.claim_objective_gate_checkpoint_restore(
                campaign_id,
                "gate-login",
                scope_id="scope-restore",
                claim_id=restore_claim,
                claimant_id="restore-worker",
            )
            assert restore["already_restored"] is False
            assert restore["checkpoint_handle"] == checkpoint["checkpoint_handle"]
            assert campaigns.validate_objective_gate_checkpoint_restore(
                campaign_id,
                "gate-login",
                scope_id="scope-restore",
                claim_id=restore_claim,
                claimant_id="restore-worker",
            )
            scopes.revise_state_scope(
                "scope-restore",
                1,
                _envelope(
                    campaign_id=campaign_id,
                    scope_id="scope-restore",
                    action_id="action-restore",
                    revision=2,
                    isolation_lease_id=restore_claim,
                    delivered=True,
                ),
                claim_id=restore_claim,
                claimant_id="restore-worker",
            )
            scopes.acknowledge_state_scope(
                "scope-restore", claim_id=restore_claim, claimant_id="restore-worker"
            )
            with pytest.raises(ValueError, match="active state-scope lease"):
                campaigns.mark_objective_gate_checkpoint_restored(
                    campaign_id,
                    "gate-login",
                    scope_id="scope-restore",
                    claim_id=restore_claim,
                    claimant_id="stale-worker",
                    evidence_digest="sha256:" + "3" * 64,
                )
            restored = campaigns.mark_objective_gate_checkpoint_restored(
                campaign_id,
                "gate-login",
                scope_id="scope-restore",
                claim_id=restore_claim,
                claimant_id="restore-worker",
                evidence_digest="sha256:" + "3" * 64,
            )
            assert restored["state"] == "restored"
            assert not campaigns.validate_objective_gate_checkpoint_restore(
                campaign_id,
                "gate-login",
                scope_id="scope-restore",
                claim_id=restore_claim,
                claimant_id="restore-worker",
            )
            replay = campaigns.claim_objective_gate_checkpoint_restore(
                campaign_id,
                "gate-login",
                scope_id="scope-restore",
                claim_id=restore_claim,
                claimant_id="restore-worker",
            )
            assert replay["already_restored"] is True


def test_campaign_resource_budget_uses_proven_principal_and_global_reservations(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-batch-budget")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            for campaign_id in ("campaign/budget-a", "campaign/budget-b", "campaign/unknown"):
                _prepare_campaign(connection, campaign_id)
            store = CampaignApiSessionRepository(connection)
            principal_digest = "b" * 64
            for suffix in ("a", "b"):
                store.create_state_scope(
                    f"campaign/budget-{suffix}",
                    "https://target.example",
                    _envelope(
                        campaign_id=f"campaign/budget-{suffix}",
                        scope_id=f"scope-budget-{suffix}",
                        action_id=f"action-budget-{suffix}",
                        principal_digest=principal_digest,
                    ),
                )
            claim = store.claim_state_scope_lease(
                "scope-budget-a",
                target_authority="https://target.example",
                principal_key=("keyed_digest:hmac-sha256:identity-v1:" + principal_digest),
                equivalence_key="shared-budget",
                claimant_id="campaign-a",
                lease_seconds=60,
            )
            assert claim is not None
            assert store.reserve_resource(
                "scope-budget-a",
                "reservation-budget-a",
                target_authority="https://target.example",
                principal_key=("keyed_digest:hmac-sha256:identity-v1:" + principal_digest),
                resource_kind="quota",
                amount=3,
                limit=5,
                window_key="daily",
                claim_id=claim,
                claimant_id="campaign-a",
            )["admitted"]

            resources = (
                {
                    "ownership_name": "account_quota",
                    "resource_kind": "quota",
                    "reservation_amount": 1,
                    "limit": 5,
                    "window_key": "daily",
                },
            )
            # Campaign B resolves to the same reviewed principal, so A's active
            # reservation clamps B's claim budget rather than being double-counted.
            assert (
                store.remaining_campaign_resource_budget(
                    "campaign/budget-b", "https://target.example", resources
                )
                == 2
            )
            assert (
                store.remaining_campaign_resource_budget(
                    "campaign/unknown", "https://target.example", resources
                )
                is None
            )
