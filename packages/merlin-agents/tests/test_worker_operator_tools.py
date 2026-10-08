from dataclasses import dataclass, field

import pytest
from merlin_agents.worker_operator_tools import AgentListTool, AgentMessageTool
from merlin_contracts import ToolContext
from merlin_contracts.agents import AgentSummary


def worker(**updates):
    return AgentSummary.model_validate(
        {
            "id": "assignment/sql",
            "node_id": "worker:assignment/sql",
            "name": "SQL investigator",
            "status": "running",
            "session_id": "session/owned",
            "schedule_id": "schedule/owned",
            "campaign_id": "campaign/owned",
            **updates,
        }
    )


@dataclass
class MailService:
    agents: list = field(default_factory=lambda: [worker()])
    receipts: dict = field(default_factory=dict)
    deliveries: int = 0
    fail: bool = False

    async def list_agents(self, session_id):
        return self.agents

    async def message_agent_receipt_exists(
        self, session_id, agent_id, message, *, schedule_id, message_id
    ):
        return self.receipts.get(message_id) == (schedule_id, session_id, agent_id, message)

    async def message_schedule_agent_once(
        self, schedule_id, session_id, agent_id, message, *, message_id, **kwargs
    ):
        if self.fail:
            raise RuntimeError("schedule ended during discovery")
        receipt = (schedule_id, session_id, agent_id, message)
        if message_id in self.receipts:
            if self.receipts[message_id] != receipt:
                raise ValueError("conflicting content")
            return False
        self.receipts[message_id] = receipt
        self.deliveries += 1
        return True


def context(**updates):
    return ToolContext(
        session_id="session/owned", workspace_root="/tmp", call_id="call-1", **updates
    )


async def test_discovery_returns_exact_worker_address_and_filters_foreign_sessions():
    service = MailService(agents=[worker(), worker(id="foreign", session_id="session/foreign")])
    result = await AgentListTool(service).invoke(context(), {})
    assert not result.is_error
    assert [row["id"] for row in result.metadata["agents"]] == ["assignment/sql"]
    assert result.metadata["agents"][0]["can_receive_guidance"] is True
    filtered = await AgentListTool(service).invoke(context(), {"campaign_id": "campaign/other"})
    assert filtered.metadata["agents"] == []


async def test_worker_guidance_is_durable_once_across_tool_recreation_and_completion():
    service = MailService()
    args = {"agent_id": "assignment/sql", "message": "Token expired; wait for renewed access."}
    first = await AgentMessageTool(service).invoke(context(), args)
    service.agents = [worker(status="completed")]
    replay = await AgentMessageTool(service).invoke(context(), args)
    assert service.deliveries == 1
    assert first.metadata["message_id"] == replay.metadata["message_id"]
    assert first.metadata["duplicate"] is False
    assert replay.metadata["duplicate"] is True
    assert replay.metadata["state"] == "queued"
    assert "does not establish" in replay.metadata["delivery_note"]


async def test_duplicate_call_id_cannot_change_message_or_recipient():
    service = MailService(agents=[worker(), worker(id="assignment/other", node_id="worker:other")])
    tool = AgentMessageTool(service)
    await tool.invoke(context(), {"agent_id": "assignment/sql", "message": "First"})
    changed = await tool.invoke(context(), {"agent_id": "assignment/sql", "message": "Changed"})
    recipient = await tool.invoke(context(), {"agent_id": "assignment/other", "message": "First"})
    assert changed.is_error and recipient.is_error
    assert service.deliveries == 1


@pytest.mark.parametrize(
    "status", ["completed", "failed", "cancelled", "quarantined", "cleanup_required"]
)
async def test_terminal_worker_cannot_receive_new_guidance(status):
    service = MailService(agents=[worker(status=status)])
    result = await AgentMessageTool(service).invoke(
        context(), {"agent_id": "assignment/sql", "message": "Hello"}
    )
    assert result.is_error
    assert service.deliveries == 0


@pytest.mark.parametrize("recipient", ["foreign", "SQL investigator", "missing"])
async def test_no_cross_session_delivery_or_fuzzy_name_routing(recipient):
    service = MailService(agents=[worker(), worker(id="foreign", session_id="session/foreign")])
    result = await AgentMessageTool(service).invoke(
        context(), {"agent_id": recipient, "message": "Hello"}
    )
    assert result.is_error
    assert not service.receipts


async def test_discovery_delivery_race_does_not_claim_success():
    service = MailService(fail=True)
    result = await AgentMessageTool(service).invoke(
        context(), {"agent_id": "assignment/sql", "message": "Hello"}
    )
    assert result.is_error
    assert not service.receipts


@pytest.mark.parametrize(
    "background_identity",
    [{"agent_id": "worker:assignment/sql"}, {"campaign_id": "campaign/owned"}],
)
async def test_foreground_only_and_call_id_required(background_identity):
    service = MailService()
    worker_context = context(**background_identity)
    args = {"agent_id": "assignment/sql", "message": "Hello"}
    assert (await AgentMessageTool(service).invoke(worker_context, args)).is_error
    assert (await AgentListTool(service).invoke(worker_context, {})).is_error
    missing_call = context().model_copy(update={"call_id": None})
    assert (await AgentMessageTool(service).invoke(missing_call, args)).is_error
    assert not service.receipts


async def test_exact_node_id_resolves_and_colliding_assignment_id_is_ambiguous():
    service = MailService()
    args = {"agent_id": "worker:assignment/sql", "message": "Hello"}
    result = await AgentMessageTool(service).invoke(context(), args)
    assert not result.is_error
    assert result.metadata["agent_id"] == "assignment/sql"
    service.agents.append(worker(id="worker:assignment/sql", node_id="worker:other"))
    ambiguous = await AgentMessageTool(service).invoke(context(), args)
    assert ambiguous.is_error
    assert service.deliveries == 1


@pytest.mark.parametrize("message", ["", "  ", "x" * 8193, None])
async def test_invalid_message_is_not_queued(message):
    service = MailService()
    result = await AgentMessageTool(service).invoke(
        context(), {"agent_id": "assignment/sql", "message": message}
    )
    assert result.is_error
    assert not service.receipts
