from __future__ import annotations

from collections.abc import Mapping

import pytest
from merlin_storage_postgres.state_scope_store import _scheduler_recovery_action


@pytest.mark.parametrize("with_active_batch", (False, True))
@pytest.mark.parametrize(
    ("campaign_status", "wave_status", "schedule_status", "expected"),
    (
        ("paused", "paused", "paused", "paused"),
        ("executing", "paused", "paused", "pause_inconsistent"),
        ("paused", "running", "paused", "pause_inconsistent"),
        ("paused", "paused", "running", "paused"),
        ("executing", "running", "running", "resume_active"),
    ),
)
def test_scheduler_recovery_action_classifies_paused_topology(
    campaign_status: str,
    wave_status: str,
    schedule_status: str,
    expected: str,
    with_active_batch: bool,
) -> None:
    latest_wave: Mapping[str, object] = {
        "wave": 1,
        "schedule_id": "schedule/1",
        "wave_status": wave_status,
        "schedule_status": schedule_status,
    }
    active_batch: Mapping[str, object] | None = None
    if with_active_batch:
        active_batch = {
            "wave": 1,
            "state": "scheduled",
            "schedule_id": "schedule/1",
            "schedule_status": schedule_status,
        }

    assert (
        _scheduler_recovery_action(
            campaign_status,
            latest_wave,
            active_batch,
            {"queued": 0},
        )
        == expected
    )
