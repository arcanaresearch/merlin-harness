import asyncio

import pytest
from merlin_agents import AgentOrchestrator, AgentRole, TaskLimitError, TaskState
from merlin_contracts import ModelCompleted, ModelError, TextDelta


class FakeProvider:
    def __init__(self, responses: dict[str, str | Exception], *, gate: asyncio.Event | None = None):
        self.responses = responses
        self.gate = gate
        self.active = 0
        self.high_water = 0
        self.requests = []
        self.started = asyncio.Event()
        self.reached_three = asyncio.Event()

    async def stream(self, request):
        self.requests.append(request)
        self.active += 1
        self.high_water = max(self.high_water, self.active)
        self.started.set()
        if self.active == 3:
            self.reached_three.set()
        try:
            if self.gate:
                await self.gate.wait()
            prompt = request.messages[1].content
            response = self.responses[prompt]
            if isinstance(response, Exception):
                raise response
            if response.startswith("error:"):
                yield ModelError(message=response.removeprefix("error:"))
            else:
                yield TextDelta(text=response)
                yield ModelCompleted(finish_reason="stop")
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_runs_roles_and_includes_operator_message() -> None:
    provider = FakeProvider({"lead": "plan", "work": "finding"})
    swarm = AgentOrchestrator(provider, model="test")
    lead = swarm.create_task("lead", role=AgentRole.LEAD)
    worker = swarm.create_task("work")
    swarm.operator_message(worker.id, "focus on auth")
    result = await swarm.run_all()
    assert result.successful
    assert lead.result == "plan"
    assert worker.result == "finding"
    assert provider.requests[0].messages[0].content.startswith("You are the lead")
    assert provider.requests[1].messages[-1].content == "Operator message: focus on auth"


@pytest.mark.asyncio
async def test_workers_are_bounded_to_three() -> None:
    gate = asyncio.Event()
    provider = FakeProvider({str(index): "done" for index in range(4)}, gate=gate)
    swarm = AgentOrchestrator(provider, model="test", max_workers=3)
    for index in range(4):
        swarm.create_task(str(index))
    running = asyncio.create_task(swarm.run_all())
    await provider.reached_three.wait()
    assert provider.high_water == 3
    gate.set()
    await running
    assert provider.high_water == 3


@pytest.mark.asyncio
async def test_combined_result_preserves_worker_failures() -> None:
    provider = FakeProvider({"good": "evidence", "bad": "error:denied"})
    swarm = AgentOrchestrator(provider, model="test")
    good = swarm.create_task("good")
    bad = swarm.create_task("bad")
    result = await swarm.run_all()
    assert not result.successful
    assert good.state is TaskState.COMPLETED
    assert bad.state is TaskState.FAILED
    assert "evidence" in result.combined
    assert "failed" in result.combined
    assert "denied" in result.combined


@pytest.mark.asyncio
async def test_operator_can_cancel_queued_and_interrupt_running_task() -> None:
    gate = asyncio.Event()
    provider = FakeProvider({"wait": "done"}, gate=gate)
    swarm = AgentOrchestrator(provider, model="test")
    cancelled = swarm.create_task("never")
    swarm.stop(cancelled.id)
    assert cancelled.state is TaskState.CANCELLED

    running_task = swarm.create_task("wait")
    execution = asyncio.create_task(swarm.run_task(running_task.id))
    await provider.started.wait()
    swarm.stop(running_task.id)
    await execution
    assert running_task.state is TaskState.INTERRUPTED


def test_task_and_worker_bounds() -> None:
    provider = FakeProvider({})
    with pytest.raises(ValueError, match="between 1 and 3"):
        AgentOrchestrator(provider, model="test", max_workers=4)
    swarm = AgentOrchestrator(provider, model="test", max_tasks=1)
    swarm.create_task("one")
    with pytest.raises(TaskLimitError, match="limit"):
        swarm.create_task("two")


@pytest.mark.asyncio
async def test_hung_provider_stream_fails_worker_and_releases_slot() -> None:
    class HungProvider:
        async def stream(self, request):
            await asyncio.Event().wait()
            yield  # pragma: no cover

    swarm = AgentOrchestrator(
        HungProvider(), model="test", max_workers=1, turn_timeout_seconds=0.05
    )
    first = swarm.create_task("hang once")
    second = swarm.create_task("hang twice")
    await swarm.run_all()

    assert first.state is TaskState.FAILED
    assert "timed out" in (first.error or "")
    assert second.state is TaskState.FAILED
    assert "timed out" in (second.error or "")


def test_turn_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="turn timeout"):
        AgentOrchestrator(FakeProvider({}), model="test", turn_timeout_seconds=0)
