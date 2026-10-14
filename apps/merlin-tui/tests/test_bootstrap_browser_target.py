import json
from uuid import UUID

from merlin_contracts import ChatMessage
from merlin_tui.bootstrap import (
    _active_schedule_provider_assignment,
    _browser_target_objective,
    _campaign_status_allows_provider,
    _same_schedule_id,
)


def _provider_admission_fixture() -> tuple[dict[str, object], ...]:
    agent_id = "campaign/example/adversarial-explorer/target-derived-discovery"
    campaign = {"session_id": "session-1", "status": "paused"}
    wave = {"status": "running"}
    schedule = {
        "session_id": "session-1",
        "status": "running",
        "graph": {
            "assignments": [{"id": agent_id, "provider_id": "provider-1"}],
            "worker_contexts": {agent_id: {"digest": "sha256:context"}},
        },
    }
    attempts = (
        {
            "id": "attempt-1",
            "node_id": f"worker:{agent_id}",
            "state": "running",
        },
    )
    return campaign, wave, schedule, attempts


def test_browser_target_prefers_latest_configured_campaign_objective() -> None:
    messages = [
        ChatMessage(role="user", content="Test http://localhost:3100.Open the game"),
        ChatMessage(
            role="tool",
            name="campaign_configure",
            tool_call_id="configure",
            content=json.dumps(
                {
                    "status": "configured",
                    "settings": {"objective": "http://localhost:3100"},
                }
            ),
        ),
    ]

    assert _browser_target_objective(messages) == "http://localhost:3100"


def test_browser_target_falls_back_to_user_objective_without_configuration() -> None:
    messages = [
        ChatMessage(role="user", content="Test https://example.test"),
        ChatMessage(role="assistant", content="I will inspect it."),
    ]

    assert _browser_target_objective(messages) == "Test https://example.test"


def test_campaign_schedule_identity_accepts_uuid_retry_value() -> None:
    value = "4e9ffdc8-cfcd-5a84-811a-0e02cf3df983"

    assert _same_schedule_id(value, UUID(value))
    assert not _same_schedule_id(value, UUID("e438c3ad-01dc-42e7-a9b7-ea48d9aa95d3"))


def test_campaign_provider_continues_only_during_acknowledged_settlement() -> None:
    assert _campaign_status_allows_provider(
        {"status": "executing"}, allow_settlement_pause=False
    )
    settlement = {
        "status": "paused",
        "lifecycle_pause_reason": "acknowledged_settlement",
        "lifecycle_resume_status": "executing",
    }
    assert _campaign_status_allows_provider(settlement, allow_settlement_pause=True)
    assert not _campaign_status_allows_provider(settlement, allow_settlement_pause=False)
    for reason in ("quota", "cleanup", "fixed_forward"):
        assert not _campaign_status_allows_provider(
            {
                "status": "paused",
                "lifecycle_pause_reason": reason,
                "lifecycle_resume_status": "executing",
            },
            allow_settlement_pause=True,
        )
    assert not _campaign_status_allows_provider(
        {
            "status": "paused",
            "lifecycle_pause_reason": "acknowledged_settlement",
            "lifecycle_resume_status": "prepared",
        },
        allow_settlement_pause=True,
    )


def test_active_reviewed_worker_lease_authorizes_provider_during_projection_pause() -> None:
    campaign, wave, schedule, attempts = _provider_admission_fixture()
    agent_id = "campaign/example/adversarial-explorer/target-derived-discovery"

    assignment = _active_schedule_provider_assignment(
        campaign,
        wave,
        schedule,
        attempts,
        session_id="session-1",
        agent_id=agent_id,
        attempt_id="attempt-1",
        provider_id="provider-1",
    )

    assert assignment == {"id": agent_id, "provider_id": "provider-1"}


def test_provider_admission_rejects_inactive_or_mismatched_worker_lease() -> None:
    campaign, wave, schedule, attempts = _provider_admission_fixture()
    agent_id = "campaign/example/adversarial-explorer/target-derived-discovery"

    for replacement in (
        {**schedule, "status": "paused"},
        {**schedule, "session_id": "session-other"},
        {
            **schedule,
            "graph": {
                **schedule["graph"],
                "assignments": [{"id": agent_id, "provider_id": "provider-other"}],
            },
        },
    ):
        assert (
            _active_schedule_provider_assignment(
                campaign,
                wave,
                replacement,
                attempts,
                session_id="session-1",
                agent_id=agent_id,
                attempt_id="attempt-1",
                provider_id="provider-1",
            )
            is None
        )
    assert (
        _active_schedule_provider_assignment(
            campaign,
            wave,
            schedule,
            ({**attempts[0], "state": "failed"},),
            session_id="session-1",
            agent_id=agent_id,
            attempt_id="attempt-1",
            provider_id="provider-1",
        )
        is None
    )
