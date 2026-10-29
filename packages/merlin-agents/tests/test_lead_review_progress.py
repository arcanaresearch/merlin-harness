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


@pytest.mark.asyncio
async def test_wave_progress_projects_pending_saturation_decision_without_coverage_payload() -> (
    None
):
    class Store:
        def list_waves(self, _campaign_id):
            return [
                {
                    "wave": 86,
                    "kind": "frontier",
                    "status": "completed",
                    "schedule_id": "schedule-86",
                    "target_id": "target-1",
                }
            ]

        def list_lead(self, _campaign_id):
            return [
                {
                    "payload": {
                        "kind": "saturation_decision_required",
                        "request_id": "coverage-extension-1",
                        "evidence_digest": "sha256:evidence",
                        "purpose": "coverage_extension",
                        "coverage_gaps": {"completion_frontier": tuple(range(10_000))},
                    }
                }
            ]

    class Agents:
        async def target_progress(self, _campaign_id, _target_id):
            return {}

    service = CampaignService(Store(), Agents(), None)  # type: ignore[arg-type]

    decision = (await service.wave_progress("campaign-1"))["saturation_decision"]

    assert decision == {
        "state": "awaiting_operator",
        "request_id": "coverage-extension-1",
        "evidence_digest": "sha256:evidence",
        "purpose": "coverage_extension",
    }


@pytest.mark.asyncio
async def test_wave_progress_projects_parallel_task_reviews() -> None:
    class Store:
        def list_waves(self, _campaign_id):
            return [
                {
                    "wave": 3,
                    "kind": "frontier",
                    "status": "running",
                    "schedule_id": "schedule-3",
                    "target_id": "target-1",
                }
            ]

        def list_lead(self, _campaign_id):
            return [
                {
                    "payload": {
                        "kind": "incremental_task_review_started",
                        "schedule_id": "schedule-3",
                        "task_id": "task-1",
                    }
                },
                {
                    "payload": {
                        "kind": "incremental_task_review_completed",
                        "schedule_id": "schedule-3",
                        "review": {"task_id": "task-1"},
                    }
                },
                {
                    "payload": {
                        "kind": "incremental_task_review_started",
                        "schedule_id": "schedule-3",
                        "task_id": "task-2",
                    }
                },
            ]

    class Agents:
        async def target_progress(self, _campaign_id, _target_id):
            return {}

    service = CampaignService(Store(), Agents(), None)  # type: ignore[arg-type]

    review = (await service.wave_progress("campaign-1"))["review_progress"]

    assert review == {
        "review_kind": "Reviewing task evidence in parallel",
        "state": "reviewing",
        "blocking": False,
        "advisory": True,
        "last_activity": "1 task reviews completed; 1 running",
        "waiting_for": "active workers and independent reviewers continue",
        "completed_chunks": 1,
    }


@pytest.mark.asyncio
async def test_wave_progress_projects_blocking_incremental_merge() -> None:
    class Store:
        def list_waves(self, _campaign_id):
            return [
                {
                    "wave": 3,
                    "kind": "frontier",
                    "status": "completed",
                    "schedule_id": "schedule-3",
                    "target_id": "target-1",
                }
            ]

        def list_lead(self, _campaign_id):
            return [
                {
                    "payload": {
                        "kind": "incremental_review_merge_started",
                        "schedule_id": "schedule-3",
                        "review_count": 4,
                        "waiting_for": "typed review merge before selecting the next wave",
                    }
                }
            ]

    class Agents:
        async def target_progress(self, _campaign_id, _target_id):
            return {}

    service = CampaignService(Store(), Agents(), None)  # type: ignore[arg-type]

    review = (await service.wave_progress("campaign-1"))["review_progress"]

    assert review["review_kind"] == "Merging task reviews"
    assert review["blocking"] is True
    assert review["completed_chunks"] == 4


@pytest.mark.asyncio
async def test_wave_progress_includes_reviewed_reconnaissance_state() -> None:
    observed_at = datetime.now(UTC).isoformat()

    class Store:
        def list_waves(self, _campaign_id):
            return [
                {
                    "wave": 1,
                    "kind": "initial",
                    "status": "running",
                    "schedule_id": "schedule-1",
                    "target_id": "target-1",
                }
            ]

        def list_lead(self, _campaign_id):
            return [
                {
                    "created_at": observed_at,
                    "payload": {
                        "kind": "target_state_attribution",
                        "schema": "merlin.target-state-attribution.v1",
                        "attribution_id": "target-state-attribution:recon-1",
                        "campaign_id": "campaign-1",
                        "target_id": "target-1",
                        "source_kind": "reconnaissance",
                        "provenance": "campaign",
                        "transition_digest": "sha256:recon-1",
                        "transition_outcome": "observed",
                        "changes": (
                            {
                                "name": "tier",
                                "role": "progress_ordinal",
                                "status": "observed",
                                "before_value": 1,
                                "after_value": 2,
                            },
                        ),
                    },
                }
            ]

    class Agents:
        async def target_progress(self, _campaign_id, _target_id):
            return {
                "schema": "merlin.target-progress.v1",
                "campaign_id": "campaign-1",
                "target_id": "target-1",
                "status": "unknown",
                "freshness": "unknown",
                "observed_at": None,
                "age_seconds": None,
                "target_exchange_count": 11,
                "history_truncated": False,
                "current_state": (),
                "latest_transition": None,
                "conflicts": (),
                "limitations": (
                    "Target state has not been discovered from reviewed observations yet.",
                ),
                "snapshot_revision": "sha256:unknown",
            }

    async def prepare(*_args, **_kwargs):  # pragma: no cover - constructor seam
        raise AssertionError("not used")

    service = CampaignService(Store(), Agents(), prepare)  # type: ignore[arg-type]

    progress = (await service.wave_progress("campaign-1"))["target_progress"]

    assert progress["status"] == "observed"
    assert progress["target_exchange_count"] == 11
    assert progress["limitations"] == ()
    assert progress["current_state"] == (
        {
            "name": "tier",
            "label": "Tier",
            "role": "progress_ordinal",
            "value": 2,
            "value_type": "integer",
            "trust_class": "reviewed_reconnaissance",
            "source_call_id": "target-state-attribution:recon-1",
            "observation_digest": "sha256:recon-1",
            "observed_at": observed_at,
        },
    )
    assert progress["latest_transition"]["observations"] == (
        {
            "name": "tier",
            "role": "progress_ordinal",
            "status": "observed",
            "before_value": 1,
            "after_value": 2,
        },
    )


@pytest.mark.asyncio
async def test_wave_progress_rejects_unverified_or_cross_target_attribution() -> None:
    class Store:
        def list_waves(self, _campaign_id):
            return [
                {
                    "wave": 1,
                    "kind": "initial",
                    "status": "running",
                    "schedule_id": "schedule-1",
                    "target_id": "target-1",
                }
            ]

        def list_lead(self, _campaign_id):
            return [
                {
                    "payload": {
                        "kind": "target_state_attribution",
                        "schema": "merlin.target-state-attribution.v1",
                        "campaign_id": "campaign-1",
                        "target_id": "target-other",
                        "provenance": "campaign",
                        "changes": ({"name": "tier", "after_value": 4},),
                    }
                },
                {
                    "payload": {
                        "kind": "target_state_attribution",
                        "schema": "merlin.target-state-attribution.v1",
                        "campaign_id": "campaign-1",
                        "target_id": "target-1",
                        "provenance": "unverified_inherited",
                        "changes": ({"name": "tier", "after_value": 4},),
                    }
                },
            ]

    class Agents:
        async def target_progress(self, _campaign_id, _target_id):
            return {"status": "unknown", "current_state": ()}

    async def prepare(*_args, **_kwargs):  # pragma: no cover - constructor seam
        raise AssertionError("not used")

    service = CampaignService(Store(), Agents(), prepare)  # type: ignore[arg-type]

    assert (await service.wave_progress("campaign-1"))["target_progress"] == {
        "status": "unknown",
        "current_state": (),
    }
