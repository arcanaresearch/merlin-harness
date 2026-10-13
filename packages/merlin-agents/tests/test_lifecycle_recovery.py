from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from merlin_agents.lifecycle_recovery import (
    LIFECYCLE_RECOVERY_SCHEMA,
    DurableLifecycleRecovery,
    lifecycle_recovery_report,
)
from merlin_contracts.state_scope import (
    LEGACY_ATTEMPT_SCHEMA_VERSION,
    STATE_SCOPE_SCHEMA_VERSION,
)

_RECOVERY_FIELDS = {
    "recovery_action": "idle",
    "scheduler_action": "idle",
    "recovery_state_digest": "sha256:" + "a" * 64,
    "recovery_decision_digest": "sha256:" + "b" * 64,
    "target_call_count": 0,
}


async def test_lifecycle_recovery_fences_global_state_before_campaign_resume() -> None:
    calls = []
    now = datetime.now(UTC)

    class Store:
        def reconcile_durable_lifecycle(self, **kwargs):
            calls.append(kwargs)
            return {
                "schema": LIFECYCLE_RECOVERY_SCHEMA,
                "reconciled_at": now,
                "expired_global_leases": 2,
                "due_quota_wakes": 1,
                "campaigns": [
                    {
                        "campaign_id": "campaign/resumable",
                        "execution_format": STATE_SCOPE_SCHEMA_VERSION,
                        "status": "executing",
                        "decision": "resume",
                        "wake_at": None,
                        "open_state_scopes": 1,
                        "acknowledged_state_scopes": 0,
                        "cleanup_obligations": 0,
                        "quota_indefinite": False,
                        **_RECOVERY_FIELDS,
                    },
                    {
                        "campaign_id": "campaign/quota",
                        "execution_format": STATE_SCOPE_SCHEMA_VERSION,
                        "status": "paused",
                        "decision": "wait_quota",
                        "wake_at": now + timedelta(hours=1),
                        "open_state_scopes": 0,
                        "acknowledged_state_scopes": 0,
                        "cleanup_obligations": 0,
                        "quota_indefinite": False,
                        **_RECOVERY_FIELDS,
                        "recovery_action": "pause_lifecycle",
                    },
                ],
            }

    recovery = DurableLifecycleRecovery(Store(), claimant_id="daemon/a", lease_seconds=45)
    report = await recovery.reconcile()

    assert calls == [
        {
            "claimant_id": "daemon/a",
            "readable_formats": (
                LEGACY_ATTEMPT_SCHEMA_VERSION,
                STATE_SCOPE_SCHEMA_VERSION,
            ),
            "writable_format": STATE_SCOPE_SCHEMA_VERSION,
            "lease_seconds": 45,
        }
    ]
    assert report.resumable_campaign_ids == ("campaign/resumable",)
    assert report.due_quota_wakes == 1
    assert report.expired_global_leases == 2


def test_lifecycle_recovery_preserves_fixed_forward_pause() -> None:
    report = lifecycle_recovery_report(
        {
            "schema": LIFECYCLE_RECOVERY_SCHEMA,
            "reconciled_at": "2026-10-13T00:00:00Z",
            "expired_global_leases": 0,
            "due_quota_wakes": 0,
            "campaigns": [
                {
                    "campaign_id": "campaign/new-format",
                    "execution_format": "merlin.state-scope.v2",
                    "status": "paused",
                    "decision": "pause_fixed_forward",
                    "wake_at": None,
                    "open_state_scopes": 1,
                    "acknowledged_state_scopes": 1,
                    "cleanup_obligations": 1,
                    "quota_indefinite": False,
                    **_RECOVERY_FIELDS,
                    "recovery_action": "pause_lifecycle",
                }
            ],
        }
    )

    assert report.fixed_forward_campaign_ids == ("campaign/new-format",)
    assert report.resumable_campaign_ids == ()


@pytest.mark.parametrize(
    "row",
    [
        {"decision": "wait_quota", "wake_at": None},
        {"decision": "resume", "wake_at": "2026-10-13T01:00:00Z"},
        {"decision": "resume", "execution_format": ""},
        {"decision": "unknown"},
    ],
)
def test_lifecycle_recovery_rejects_ambiguous_resume_state(row) -> None:
    campaign = {
        "campaign_id": "campaign/1",
        "execution_format": STATE_SCOPE_SCHEMA_VERSION,
        "status": "paused",
        "decision": "resume",
        "wake_at": None,
        "open_state_scopes": 0,
        "acknowledged_state_scopes": 0,
        "cleanup_obligations": 0,
        "quota_indefinite": False,
        **_RECOVERY_FIELDS,
        **row,
    }
    with pytest.raises(ValueError):
        lifecycle_recovery_report(
            {
                "schema": LIFECYCLE_RECOVERY_SCHEMA,
                "reconciled_at": datetime.now(UTC),
                "expired_global_leases": 0,
                "due_quota_wakes": 0,
                "campaigns": [campaign],
            }
        )


def test_lifecycle_recovery_preserves_indefinite_quota_pause() -> None:
    report = lifecycle_recovery_report(
        {
            "schema": LIFECYCLE_RECOVERY_SCHEMA,
            "reconciled_at": datetime.now(UTC),
            "expired_global_leases": 0,
            "due_quota_wakes": 0,
            "campaigns": [
                {
                    "campaign_id": "campaign/quota",
                    "execution_format": STATE_SCOPE_SCHEMA_VERSION,
                    "status": "paused",
                    "decision": "wait_quota",
                    "wake_at": None,
                    "quota_indefinite": True,
                    "open_state_scopes": 0,
                    "acknowledged_state_scopes": 0,
                    "cleanup_obligations": 0,
                    **_RECOVERY_FIELDS,
                    "recovery_action": "pause_lifecycle",
                }
            ],
        }
    )

    assert report.campaigns[0].quota_indefinite is True
    assert report.resumable_campaign_ids == ()


def test_lifecycle_recovery_rejects_duplicate_campaign_decisions() -> None:
    campaign = {
        "campaign_id": "campaign/1",
        "execution_format": STATE_SCOPE_SCHEMA_VERSION,
        "status": "executing",
        "decision": "resume",
        "wake_at": None,
        "open_state_scopes": 0,
        "acknowledged_state_scopes": 0,
        "cleanup_obligations": 0,
        **_RECOVERY_FIELDS,
    }
    with pytest.raises(ValueError, match="repeated a campaign"):
        lifecycle_recovery_report(
            {
                "schema": LIFECYCLE_RECOVERY_SCHEMA,
                "reconciled_at": datetime.now(UTC),
                "expired_global_leases": 0,
                "due_quota_wakes": 0,
                "campaigns": [campaign, campaign],
            }
        )
