from __future__ import annotations

import pytest
from merlin_agents.service import AgentService
from merlin_contracts import ChatRequest, ModelCompleted, TextDelta


class PlanningProvider:
    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def stream(self, request: ChatRequest):
        self.requests.append(request)
        yield TextDelta(text='{"tasks":[{"id":"one","objective":"Inspect target"}]}')
        yield ModelCompleted(finish_reason="stop")


@pytest.mark.asyncio
async def test_in_memory_service_uses_planner_workload_for_planning() -> None:
    provider = PlanningProvider()
    service = AgentService(provider, model="test")  # type: ignore[arg-type]

    planned = await service._plan("Assess target", 1)  # noqa: SLF001

    assert planned == [("one", "Inspect target")]
    assert provider.requests[0].workload == "planner"
