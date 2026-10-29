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


@pytest.mark.parametrize(
    ("action", "batch_state", "schedule_status", "wave_status", "queued"),
    [
        ("resume_active", "scheduled", "running", "running", 0),
        ("resume_active", None, "running", "running", 0),
        ("persist_successor_schedule", "claimed", None, "completed", 0),
        ("bind_successor_schedule", "claimed", "running", "completed", 0),
        ("persist_wave_binding", "scheduled", "running", "completed", 0),
        ("finalize_and_settle_terminal_batch", "scheduled", "completed", "running", 0),
        ("settle_terminal_batch", "scheduled", "completed", "completed", 0),
        ("finalize_terminal_wave", None, "completed", "running", 0),
        ("claim_successor", None, "completed", "completed", 1),
    ],
)
async def test_recorded_recovery_actions_match_legacy_boundaries(
    monkeypatch, action, batch_state, schedule_status, wave_status, queued
) -> None:
    import asyncio
    from types import SimpleNamespace

    import merlin_agents.campaign_service as module

    calls = []
    batch = (
        None
        if batch_state is None
        else {
            "id": "batch",
            "wave": 1,
            "state": batch_state,
            "schedule_id": "schedule" if batch_state == "scheduled" else None,
        }
    )
    wave = (
        0
        if action
        in {"persist_successor_schedule", "bind_successor_schedule", "persist_wave_binding"}
        else 1
    )
    waves = [{"wave": wave, "status": wave_status, "schedule_id": "schedule"}]
    live = SimpleNamespace(id="schedule", assignments=(), execution=None)

    class Store:
        def get(self, campaign_id):
            return {
                "id": campaign_id,
                "status": "executing",
                "manifest": {"manifest": {}, "assignment_context": {}},
            }

        def list_executing(self):
            return [self.get("campaign/test")]

        def list_waves(self, campaign_id):
            return waves

        def load_active_frontier_batch(self, campaign_id):
            return batch

        def frontier_counts(self, campaign_id):
            return {"queued": queued}

    class Agents:
        async def schedule_status(self, schedule_id):
            return schedule_status

        async def reopen(self, schedule_id, **kwargs):
            return live

    class Recovery:
        def claim(self, campaign_id, **kwargs):
            assert kwargs["state_digest"] == _RECOVERY_FIELDS["recovery_state_digest"]
            assert kwargs["decision_digest"] == _RECOVERY_FIELDS["recovery_decision_digest"]
            return "claim/test"

        def apply(self, campaign_id, **kwargs):
            assert kwargs == {"claim_id": "claim/test"}

    monkeypatch.setattr(module, "_manifest", lambda value: object())
    monkeypatch.setattr(module, "_assignment_context", lambda value: object())

    async def prepare(*args):
        raise AssertionError

    service = module.CampaignService(Store(), Agents(), prepare)

    async def restore(*args):
        return live

    async def successor(*args):
        return live, 2

    async def supervise(*args, **kwargs):
        calls.append(
            (args[0], args[3].id, kwargs["initial_wave"], kwargs.get("wave_running", True))
        )

    monkeypatch.setattr(service, "_recover_active_frontier_fenced", restore)
    monkeypatch.setattr(service, "_recover_settled_frontier_successor", successor)
    monkeypatch.setattr(service, "_supervise", supervise)
    assert await service.recover() == 1
    await asyncio.gather(*service._supervisors.values())
    legacy = calls.pop()
    service._supervisors.clear()
    service._recovery_repository = Recovery()

    async def forbidden(**kwargs):
        raise AssertionError("production must not recompute legacy recovery")

    monkeypatch.setattr(service, "_recover_owned", forbidden)
    report = lifecycle_recovery_report(
        {
            "schema": LIFECYCLE_RECOVERY_SCHEMA,
            "reconciled_at": datetime.now(UTC),
            "expired_global_leases": 0,
            "due_quota_wakes": 0,
            "campaigns": [
                {
                    **_RECOVERY_FIELDS,
                    "campaign_id": "campaign/test",
                    "execution_format": STATE_SCOPE_SCHEMA_VERSION,
                    "status": "executing",
                    "decision": "resume",
                    "wake_at": None,
                    "open_state_scopes": 0,
                    "acknowledged_state_scopes": 0,
                    "cleanup_obligations": 0,
                    "recovery_action": action,
                    "scheduler_action": action,
                    "recovery_fence_digest": "sha256:" + "c" * 64,
                }
            ],
        }
    )
    assert await service.recover(lifecycle_report=report) == 1
    await asyncio.gather(*service._supervisors.values())
    assert calls == [legacy]


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
    report = await recovery.reconcile(
        exclude_campaign_ids=("campaign/live",),
        include_campaign_ids=("campaign/resumable", "campaign/quota"),
    )

    assert calls == [
        {
            "claimant_id": "daemon/a",
            "readable_formats": (
                LEGACY_ATTEMPT_SCHEMA_VERSION,
                STATE_SCOPE_SCHEMA_VERSION,
            ),
            "writable_format": STATE_SCOPE_SCHEMA_VERSION,
            "lease_seconds": 45,
            "exclude_campaign_ids": ("campaign/live",),
            "include_campaign_ids": ("campaign/resumable", "campaign/quota"),
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


@pytest.mark.parametrize(
    "state",
    [
        {
            "schema": "merlin.target-reobservation-recovery-state.v1",
            "phase": "reobserve",
            "generation_id": "sha256:" + "1" * 64,
            "target_session_id": "target-session/reobserve",
            "event_set_digest": "sha256:" + "2" * 64,
            "source_wave": 1,
            "schedule_id": None,
            "settlement_id": None,
            "lead_decision_id": None,
        },
        {
            "schema": "merlin.target-reobservation-recovery-state.v1",
            "phase": "review",
            "generation_id": "sha256:" + "1" * 64,
            "target_session_id": "target-session/reobserve",
            "event_set_digest": "sha256:" + "2" * 64,
            "source_wave": 1,
            "schedule_id": "schedule/refresh",
            "settlement_id": "settlement/refresh",
            "lead_decision_id": "lead/review",
        },
    ],
)
def test_lifecycle_recovery_parses_typed_target_reobservation_state(state) -> None:
    report = lifecycle_recovery_report(
        {
            "schema": LIFECYCLE_RECOVERY_SCHEMA,
            "reconciled_at": datetime.now(UTC),
            "expired_global_leases": 0,
            "due_quota_wakes": 0,
            "campaigns": [
                {
                    "campaign_id": "campaign/reobserve",
                    "execution_format": STATE_SCOPE_SCHEMA_VERSION,
                    "status": "executing",
                    "decision": "resume",
                    "wake_at": None,
                    "open_state_scopes": 0,
                    "acknowledged_state_scopes": 0,
                    "cleanup_obligations": 0,
                    **_RECOVERY_FIELDS,
                    "recovery_action": "recover_target_reobservation",
                    "scheduler_action": "recover_target_reobservation",
                    "target_reobservation_recovery_state": state,
                }
            ],
        }
    )

    parsed = report.campaigns[0].target_reobservation_recovery_state
    assert parsed is not None
    assert parsed.phase == state["phase"]
    assert parsed.generation_id == state["generation_id"]


@pytest.mark.parametrize(
    "state",
    [
        {
            "schema": "merlin.target-reobservation-recovery-state.v1",
            "phase": "reobserve",
            "generation_id": "sha256:" + "1" * 64,
            "event_set_digest": "sha256:" + "2" * 64,
            "source_wave": 1,
            "schedule_id": None,
            "settlement_id": "settlement/not-allowed",
            "lead_decision_id": None,
        },
        {
            "schema": "merlin.target-reobservation-recovery-state.v1",
            "phase": "review",
            "generation_id": "sha256:" + "1" * 64,
            "event_set_digest": "sha256:" + "2" * 64,
            "source_wave": 1,
            "schedule_id": None,
            "settlement_id": "settlement/missing-schedule",
            "lead_decision_id": None,
        },
    ],
)
def test_lifecycle_recovery_rejects_inconsistent_target_reobservation_state(state) -> None:
    with pytest.raises(ValueError, match="re-observation|review phase"):
        lifecycle_recovery_report(
            {
                "schema": LIFECYCLE_RECOVERY_SCHEMA,
                "reconciled_at": datetime.now(UTC),
                "expired_global_leases": 0,
                "due_quota_wakes": 0,
                "campaigns": [
                    {
                        "campaign_id": "campaign/reobserve",
                        "execution_format": STATE_SCOPE_SCHEMA_VERSION,
                        "status": "executing",
                        "decision": "resume",
                        "wake_at": None,
                        "open_state_scopes": 0,
                        "acknowledged_state_scopes": 0,
                        "cleanup_obligations": 0,
                        **_RECOVERY_FIELDS,
                        "recovery_action": "recover_target_reobservation",
                        "scheduler_action": "recover_target_reobservation",
                        "target_reobservation_recovery_state": state,
                    }
                ],
            }
        )
