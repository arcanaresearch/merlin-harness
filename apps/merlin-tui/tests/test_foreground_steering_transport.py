import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from merlin_contracts import AppEvent
from merlin_tui.backend import DemoBackend, FailureBackend, StreamEvent
from merlin_tui.kernel_backend import KernelBackend, event_to_stream
from merlin_tui.local_control import LocalControlClient, serve


def test_foreground_followup_message_is_rendered_on_existing_stream() -> None:
    text = "Use the existing login.\nKeep recon running."
    event = AppEvent(
        type="message",
        payload={"role": "user", "text": text, "foreground_followup": True},
        occurred_at=datetime.now(UTC),
    )
    assert event_to_stream(event) == StreamEvent("user", text)


@pytest.mark.parametrize(
    "payload",
    [
        {"role": "user", "text": "ordinary user"},
        {"role": "assistant", "text": "already streamed"},
        {"role": "user", "text": "historic", "resumed": True},
        {"role": "assistant", "text": "assistant", "foreground_followup": True},
    ],
)
def test_ordinary_and_resumed_messages_remain_suppressed(payload) -> None:
    assert (
        event_to_stream(AppEvent(type="message", payload=payload, occurred_at=datetime.now(UTC)))
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_kernel_backend_steering_preserves_text_and_acceptance(accepted: bool) -> None:
    calls = []

    class Kernel:
        async def steer(self, text: str) -> bool:
            calls.append(text)
            return accepted

    backend = KernelBackend(Kernel())  # type: ignore[arg-type]
    text = "Remember the target is L2.\nKeep recon running."
    assert await backend.steer(text) is accepted
    assert calls == [text]


@pytest.mark.asyncio
async def test_non_live_backends_do_not_claim_guidance_received() -> None:
    assert await DemoBackend().steer("focus on login") is False
    assert await FailureBackend("unavailable").steer("focus on login") is False


@pytest.mark.asyncio
async def test_steering_rpc_keeps_existing_stream_as_only_event_reader() -> None:
    started = asyncio.Event()
    received = asyncio.Event()
    release = asyncio.Event()
    calls = []

    class Backend:
        async def stream(self, prompt):
            calls.append(("stream", prompt))
            yield StreamEvent("tool", "recon started")
            started.set()
            await received.wait()
            yield StreamEvent("content", "Acknowledged guidance; continuing recon.")
            await release.wait()
            yield StreamEvent("tool", "recon completed")

        async def steer(self, text):
            calls.append(("steer", text))
            received.set()
            return True

        async def close(self):
            pass

    directory = TemporaryDirectory(dir="/tmp", prefix="merlin-steer-")
    socket_path = Path(directory.name) / "steer.sock"
    server = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: Backend()))
    reader = None
    try:
        for _ in range(100):
            if socket_path.exists():
                break
            await asyncio.sleep(0.01)
        client = LocalControlClient(socket_path=socket_path)

        async def collect():
            return [event async for event in client.stream("Assess original target")]

        reader = asyncio.create_task(collect())
        await asyncio.wait_for(started.wait(), 1)
        text = "Use the existing login.\nContinue on the original target."
        assert await client.steer(text) is True
        assert not reader.done()
        release.set()
        events = await asyncio.wait_for(reader, 2)
        assert [event.text for event in events] == [
            "recon started",
            "Acknowledged guidance; continuing recon.",
            "recon completed",
        ]
        assert calls == [("stream", "Assess original target"), ("steer", text)]
    finally:
        if reader is not None:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
        server.cancel()
        with suppress(asyncio.CancelledError):
            await server
        directory.cleanup()
