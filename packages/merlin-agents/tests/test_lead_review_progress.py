from datetime import UTC, datetime, timedelta

import pytest
from merlin_agents import CampaignService


@pytest.mark.asyncio
async def test_wave_progress_projects_current_blocking_lead_review() -> None:
    started_at = (datetime.now(UTC) - timedelta(seconds=3)).isoformat()

    class Store:
        def list_waves(self, _campaign_id):
            return [
                {
                    "wave": 2,
                    "kind": "followup",
                    "status": "completed",
                    "schedule_id": "schedule-2",
                    "target_id": "target-1",
                }
            ]

        def list_lead(self, _campaign_id):
            return [
                {
                    "payload": {
                        "kind": "lead_review_started",
                        "review_id": "lead-review:abc",
                        "state": "reviewing",
                        "blocking": True,
                        "advisory": False,
                        "review_kind": "wave results",
                        "started_at": started_at,
                        "last_activity": "immutable review snapshot sealed",
                        "waiting_for": "Lead decision before the next wave can be scheduled",
                    }
                },
                {
                    "payload": {
                        "kind": "lead_review_progress",
                        "review_id": "lead-review:abc",
                        "state": "reviewing",
                        "blocking": True,
                        "advisory": False,
                        "completed_chunks": 12,
                        "last_activity": "provider output received",
                        "waiting_for": "Lead decision before the next wave can be scheduled",
                    }
                },
            ]

    class Agents:
        async def target_progress(self, _campaign_id, _target_id):
            return {}

    async def prepare(*_args, **_kwargs):  # pragma: no cover - constructor seam
        raise AssertionError("not used")

    service = CampaignService(Store(), Agents(), prepare)  # type: ignore[arg-type]

    progress = await service.wave_progress("campaign-1")

    review = progress["review_progress"]
    assert review["state"] == "reviewing"
    assert review["blocking"] is True
    assert review["advisory"] is False
    assert review["completed_chunks"] == 12
    assert review["elapsed_seconds"] >= 2
    assert review["waiting_for"] == "Lead decision before the next wave can be scheduled"


@pytest.mark.asyncio
async def test_wave_progress_omits_completed_review() -> None:
    class Store:
        def list_waves(self, _campaign_id):
            return [
                {
                    "wave": 1,
                    "kind": "initial",
                    "status": "completed",
                    "schedule_id": "schedule-1",
                    "target_id": "target-1",
                }
            ]

        def list_lead(self, _campaign_id):
            return [
                {
                    "payload": {
                        "kind": "lead_review_completed",
                        "review_id": "lead-review:abc",
                        "state": "completed",
                    }
                }
            ]

    class Agents:
        async def target_progress(self, _campaign_id, _target_id):
            return {}

    async def prepare(*_args, **_kwargs):  # pragma: no cover - constructor seam
        raise AssertionError("not used")

    service = CampaignService(Store(), Agents(), prepare)  # type: ignore[arg-type]

    assert (await service.wave_progress("campaign-1"))["review_progress"] == {}
