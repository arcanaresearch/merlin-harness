from types import SimpleNamespace

import pytest
from merlin_agents.campaign_service import CampaignService
from merlin_agents.operator_campaign_tools import CampaignMessageTool
from merlin_contracts import ToolContext


class GuidanceStore:
    def __init__(self):
        self.rows = {
            "campaign-1": {"id": "campaign-1", "session_id": "session-1", "status": "executing"}
        }
        self.wave = 0
        self.journal = {}

    def get(self, campaign_id):
        return self.rows.get(campaign_id)

    def list_waves(self, campaign_id):
        return [{"wave": self.wave}]

    def commit_lead_decision(self, campaign_id, wave, decision_id, payload, directives):
        identity = (campaign_id, decision_id)
        if identity in self.journal and self.journal[identity] != payload:
            raise ValueError("lead decision identity conflicts with different payload")
        self.journal[identity] = dict(payload)
        return dict(payload)


def context(**overrides):
    return ToolContext(
        session_id="session-1", workspace_root="/workspace", call_id="call-1", **overrides
    )


def service(store):
    instance = CampaignService.__new__(CampaignService)
    instance._store = store
    return instance


async def test_guidance_retry_survives_service_recreation_and_wave_transition():
    store = GuidanceStore()
    first = await service(store).guide("session-1", "campaign-1", "focus auth", request_id="call-1")
    store.wave = 2
    retry = await service(store).guide("session-1", "campaign-1", "focus auth", request_id="call-1")
    assert retry == first
    assert len(store.journal) == 1
    with pytest.raises(ValueError, match="conflicts"):
        await service(store).guide("session-1", "campaign-1", "different", request_id="call-1")
    assert len(store.journal) == 1


@pytest.mark.parametrize(
    "session,status", [("another-session", "executing"), ("session-1", "completed")]
)
async def test_guidance_rejects_foreign_and_terminal_campaigns(session, status):
    store = GuidanceStore()
    store.rows["campaign-1"].update(session_id=session, status=status)
    result = await CampaignMessageTool(service(store)).invoke(
        context(), {"campaign_id": "campaign-1", "message": "focus auth"}
    )
    assert result.is_error
    assert result.metadata["delivery_status"] == "not_queued"
    assert not store.journal


async def test_tool_resolves_single_active_campaign_and_reports_only_queued():
    store = GuidanceStore()
    instance = service(store)

    async def list_campaigns(session_id):
        assert session_id == "session-1"
        return (
            {"campaign_id": "campaign-1", "status": "paused"},
            {"campaign_id": "old", "status": "completed"},
        )

    instance.list = list_campaigns
    result = await CampaignMessageTool(instance).invoke(context(), {"message": "focus auth"})
    assert not result.is_error
    assert result.metadata["campaign_id"] == "campaign-1"
    assert result.metadata["delivery_status"] == "queued"
    assert result.metadata["delivery"] == "next_lead_boundary"
    assert "read" not in result.metadata


@pytest.mark.parametrize("count", [0, 2])
async def test_tool_never_guesses_ambiguous_or_absent_recipient(count):
    async def list_campaigns(session_id):
        return tuple({"campaign_id": f"campaign-{i}", "status": "executing"} for i in range(count))

    result = await CampaignMessageTool(SimpleNamespace(list=list_campaigns)).invoke(
        context(), {"message": "focus auth"}
    )
    assert result.is_error
    assert result.metadata["delivery_status"] == "not_queued"


async def test_worker_cannot_impersonate_foreground_operator():
    result = await CampaignMessageTool(SimpleNamespace()).invoke(
        context(agent_id="worker-1"), {"message": "focus auth"}
    )
    assert result.is_error
    assert "foreground" in result.content
