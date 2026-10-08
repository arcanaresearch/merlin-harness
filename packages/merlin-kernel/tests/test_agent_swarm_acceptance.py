"""Phase 5 deterministic swarm acceptance contract.

Remove the module-level skip when ``merlin-agents`` exposes the small service API
used below.  Keeping the scenario beside the kernel tests makes the orchestration
boundary visible without prematurely implementing the agent core in this phase.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from merlin_contracts import ChatRequest, ModelCompleted, ModelEvent, TextDelta


class DeterministicSwarmProvider:
    """Provider fixture with one lead split and two controllable workers."""

    def __init__(self) -> None:
        self.worker_started = {"headers": asyncio.Event(), "body": asyncio.Event()}
        self.release_body = asyncio.Event()

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        prompt = "\n".join(message.content for message in request.messages)
        if "split this assessment" in prompt.lower():
            yield TextDelta(
                text=(
                    '{"tasks":['
                    '{"id":"headers","objective":"Assess response headers"},'
                    '{"id":"body","objective":"Assess response body"}'
                    "]}"
                )
            )
            yield ModelCompleted(finish_reason="stop")
            return

        task_id = "headers" if "response headers" in prompt.lower() else "body"
        self.worker_started[task_id].set()
        yield TextDelta(text=f"{task_id}: started")
        if task_id == "body":
            await self.release_body.wait()
        yield TextDelta(text=f"{task_id}: complete")
        yield ModelCompleted(finish_reason="stop")


def test_two_worker_operator_control_and_combined_result() -> None:
    asyncio.run(_two_worker_operator_control_and_combined_result())


async def _two_worker_operator_control_and_combined_result() -> None:
    """Executable acceptance target for the Phase 5 ``AgentService`` API."""
    # Expected public API:
    #   AgentService(provider, on_progress=async_callback)
    #   start(objective, workers=2) -> SwarmHandle
    #   tasks(swarm_id) -> list[AgentTask]
    #   message(worker_id, text), stop(worker_id), wait(swarm_id) -> SwarmResult
    from merlin_agents import AgentService

    provider = DeterministicSwarmProvider()
    progress: list[object] = []

    async def capture(event: object) -> None:
        progress.append(event)

    service = AgentService(provider, on_progress=capture)
    swarm = await service.start("Split this assessment across target surfaces", workers=2)

    await asyncio.wait_for(
        asyncio.gather(*(event.wait() for event in provider.worker_started.values())),
        timeout=1,
    )
    tasks = {task.task_key: task for task in await service.tasks(swarm.id)}
    assert set(tasks) == {"headers", "body"}  # lead performed the bounded split
    assert {event.worker_id for event in progress} >= {
        tasks["headers"].worker_id,
        tasks["body"].worker_id,
    }  # both workers emitted live progress

    await service.message(tasks["headers"].worker_id, "Prioritize CSP evidence")
    assert "Prioritize CSP evidence" in service.messages(tasks["headers"].worker_id)

    await service.stop(tasks["body"].worker_id)
    provider.release_body.set()
    result = await service.wait(swarm.id)

    by_task = {item.task_key: item for item in result.tasks}
    assert by_task["headers"].status == "completed"
    assert by_task["headers"].content == "headers: startedheaders: complete"
    assert by_task["body"].status == "interrupted"
    assert by_task["body"].content == "body: started"
    assert result.status == "completed_with_interruptions"
