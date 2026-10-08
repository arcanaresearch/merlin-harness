import asyncio

import pytest
from merlin_tui.kernel_backend import KernelBackend


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", ["profile", "preflight"])
async def test_interrupt_bypasses_pending_choices_and_waits_for_ack(pending: str) -> None:
    started = asyncio.Event()
    acknowledged = asyncio.Event()

    class Kernel:
        async def interrupt(self) -> bool:
            started.set()
            await acknowledged.wait()
            return True

        async def submit(self, text: str) -> None:
            raise AssertionError("interrupt must bypass profile and model submission")

    backend = KernelBackend(Kernel())  # type: ignore[arg-type]
    objective = "Assess https://example.test authentication"
    backend._pending_campaign_objective = objective
    preflight = (objective, "quickscan", "autonomous")
    if pending == "preflight":
        backend._pending_route_preflight = preflight

    async def collect():
        return [event async for event in backend.stream(" /interrupt ")]

    task = asyncio.create_task(collect())
    await asyncio.wait_for(started.wait(), timeout=1)
    assert not task.done()
    acknowledged.set()
    events = await asyncio.wait_for(task, timeout=1)

    assert [(event.kind, event.text) for event in events] == [
        ("status", "Foreground interruption acknowledged.")
    ]
    assert backend._pending_campaign_objective == objective
    assert backend._pending_route_preflight == (preflight if pending == "preflight" else None)
