import asyncio
from types import SimpleNamespace

import pytest
from merlin_agents.campaign_service import (
    CampaignService,
    LeadAssessment,
    _OperatorDrainComplete,
)
from merlin_contracts import AgentTask


class Journal:
    def __init__(self):
        self.rows = []

    def append_lead(self, _campaign, _wave, payload):
        self.rows.append({"payload": payload})

    def list_lead(self, _campaign):
        return self.rows


@pytest.mark.asyncio
@pytest.mark.parametrize("drain", [False, True])
async def test_guidance_wakes_lead_without_worker_progress(drain):
    journal = Journal()
    execution = asyncio.get_running_loop().create_future()
    consulted = asyncio.Event()

    class Agents:
        drains = []

        async def list_schedule_tasks(self, _schedule):
            return (AgentTask(id="task", objective="work", status="running"),)

        async def drain_schedule(self, schedule, *, reason):
            assert not execution.done()  # owned work must remain alive
            self.drains.append((schedule, reason))

    agents = Agents()
    service = CampaignService(journal, agents, None, None)

    async def assess(*_args):
        consulted.set()
        journal.append_lead(
            "campaign",
            0,
            {
                "kind": "operator_guidance_consumed",
                "guidance_id": "message-1",
            },
        )
        return LeadAssessment("Follow operator's request", False, finish_current_work=drain)

    async def budget(*_args):
        return {"exhausted": ()}

    async def lessons(*_args):
        pass

    service._assess_and_journal = assess
    service._campaign_budget_state = budget
    service._record_lessons = lessons
    live = SimpleNamespace(id="schedule", execution=execution)
    manifest = SimpleNamespace(session_id="session")
    monitor = asyncio.create_task(service._monitor_live("campaign", manifest, 0, live))
    # The unchanged projection has already been observed before guidance arrives.
    await asyncio.sleep(0.03)
    assert not consulted.is_set()
    journal.append_lead(
        "campaign",
        0,
        {
            "kind": "operator_guidance",
            "guidance_id": "message-1",
            "message": "operator text",
        },
    )
    await asyncio.wait_for(consulted.wait(), 2)
    await asyncio.sleep(0.02)
    assert agents.drains == ([("schedule", "operator_stopped")] if drain else [])
    execution.set_result(None)
    if drain:
        with pytest.raises(_OperatorDrainComplete):
            await monitor
    else:
        await monitor


@pytest.mark.asyncio
async def test_recovery_replays_committed_finish_decision_before_waiting():
    journal = Journal()
    journal.append_lead("campaign", 0, {"finish_current_work": True, "summary": "stop early"})
    drains = []

    class Agents:
        async def drain_schedule(self, schedule, *, reason):
            drains.append((schedule, reason))

    service = CampaignService(journal, Agents(), None, None)
    await service._restore_operator_drain("campaign", 0, SimpleNamespace(id="schedule"))
    assert drains == [("schedule", "operator_stopped")]
    with pytest.raises(_OperatorDrainComplete):
        await service._raise_if_operator_drained("campaign")


@pytest.mark.asyncio
async def test_recovery_replays_direct_operator_drain_without_lead_decision():
    journal = Journal()
    journal.append_lead(
        "campaign",
        0,
        {
            "kind": "operator_drain_requested",
            "request_id": "call-1",
            "reason": "finish owned work",
        },
    )
    drains = []

    class Agents:
        async def drain_schedule(self, schedule, *, reason):
            drains.append((schedule, reason))

    service = CampaignService(journal, Agents(), None, None)
    restored = await service._restore_operator_drain("campaign", 0, SimpleNamespace(id="schedule"))

    assert restored is True
    assert drains == [("schedule", "operator_stopped")]
    with pytest.raises(_OperatorDrainComplete):
        await service._raise_if_operator_drained("campaign")


@pytest.mark.asyncio
async def test_consumed_guidance_is_not_delivered_again():
    journal = Journal()
    journal.append_lead("campaign", 0, {"kind": "operator_guidance", "guidance_id": "one"})
    journal.append_lead(
        "campaign",
        0,
        {
            "kind": "operator_guidance_consumed",
            "guidance_id": "one",
        },
    )
    journal.append_lead("campaign", 0, {"kind": "operator_guidance", "guidance_id": "two"})
    service = CampaignService(journal, None, None, None)
    assert [row["guidance_id"] for row in await service._pending_operator_guidance("campaign")] == [
        "two",
    ]
