from __future__ import annotations

import asyncio
import errno
import json
import signal
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from merlin_tui import local_control
from merlin_tui.backend import StreamEvent


@pytest.fixture
def socket_path() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="merlin-fault-", dir="/tmp") as directory:
        yield Path(directory) / "daemon.sock"


class LifecycleStore:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.connection = SimpleNamespace(close=lambda: events.append("connection_closed"))

    def start(self, run_id: str, **kwargs: object) -> None:
        self.events.append(("start", run_id, kwargs))

    def heartbeat(self, run_id: str) -> None:
        self.events.append(("heartbeat", run_id))

    def request_stop(self, run_id: str, **kwargs: object) -> None:
        self.events.append(("request_stop", run_id, kwargs))

    def finish(self, run_id: str, **kwargs: object) -> None:
        self.events.append(("finish", run_id, kwargs))


class Backend:
    def __init__(self, *, fail_cleanup: bool = False, fail_recovery: bool = False) -> None:
        self.events: list[object] = []
        self.daemon_run_repository = LifecycleStore(self.events)
        self.daemon_database = SimpleNamespace(stop=lambda: self.events.append("database_stopped"))
        self.fail_cleanup = fail_cleanup
        self.fail_recovery = fail_recovery
        self.mutations = 0
        self.campaign: asyncio.Task[None] | None = None
        self.campaign_progress = 0

    async def recover_background(self) -> None:
        self.events.append("recovery")
        if self.fail_recovery:
            raise RuntimeError("recovery failed")
        self.campaign = asyncio.create_task(self.run_campaign())

    async def run_campaign(self) -> None:
        while True:
            self.campaign_progress += 1
            await asyncio.sleep(0.001)

    async def get_runtime_status(self) -> str:
        return "available"

    async def get_permission_mode(self) -> str:
        return "prompt"

    async def set_model(self) -> None:
        self.mutations += 1

    async def stream(self, prompt: str):
        yield StreamEvent(kind="content", text="answer")

    async def close(self) -> None:
        self.events.append("backend_closed")
        if self.campaign is not None:
            self.campaign.cancel()
            await asyncio.gather(self.campaign, return_exceptions=True)
        if self.fail_cleanup:
            raise RuntimeError("cleanup failed")

    detach = close


async def request(path: Path, method: str) -> dict[str, object]:
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        writer.write((json.dumps({"method": method, "request_id": str(uuid4())}) + "\n").encode())
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), timeout=2))
    finally:
        writer.close()
        await writer.wait_closed()


async def wait_started(path: Path, task: asyncio.Task[None]) -> None:
    async with asyncio.timeout(3):
        while not await asyncio.to_thread(path.exists):
            if task.done():
                await task
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("stream", [False, True])
async def test_enospc_fences_mutations_and_preserves_background(
    socket_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    backend = Backend()
    path = socket_path
    writes = 0

    def disk_full(*args: object) -> None:
        nonlocal writes
        writes += 1
        raise OSError(errno.ENOSPC, "injected disk full")

    monkeypatch.setattr(local_control, "_write_journal", disk_full)
    task = asyncio.create_task(local_control.serve(path, backend_factory=lambda **_: backend))
    try:
        await wait_started(path, task)
        if stream:
            # Consume the event followed by the structured durability failure.
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(b'{"method":"stream","request_id":"stream","prompt":"hello"}\n')
            await writer.drain()
            response = json.loads(await reader.readline())
            while response["type"] == "event":
                response = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
        else:
            response = await request(path, "set_model")
        assert response["code"] == "local_storage_exhausted"
        assert response["failure_class"] == "infrastructure"
        assert response["infrastructure_failure_category"] == "storage_exhausted"
        assert (await request(path, "set_model"))["code"] == "local_storage_exhausted"
        assert backend.mutations == (0 if stream else 1)
        assert (await request(path, "get_runtime_status"))["value"] == "available"
        assert (await request(path, "get_permission_mode"))["value"] == "prompt"
        progress = backend.campaign_progress
        await asyncio.sleep(0.02)
        assert backend.campaign_progress > progress
        assert not task.done()
        await request(path, "shutdown_clean")
        await asyncio.wait_for(task, timeout=3)
        assert writes == 1
        assert backend.events[-3:] == [
            next(
                event
                for event in backend.events
                if isinstance(event, tuple) and event[0] == "finish"
            ),
            "connection_closed",
            "database_stopped",
        ]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
async def test_signal_uses_attributed_shutdown_and_connection_closes_last(
    socket_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signum: signal.Signals,
) -> None:
    backend = Backend()
    handlers: dict[signal.Signals, tuple[object, tuple[object, ...]]] = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, cb, *args: handlers.update({sig: (cb, args)})
    )
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: handlers.pop(sig, None))
    monkeypatch.setattr(local_control, "_DAEMON_HEARTBEAT_SECONDS", 0.005)
    path = socket_path
    task = asyncio.create_task(local_control.serve(path, backend_factory=lambda **_: backend))
    try:
        await wait_started(path, task)
        async with asyncio.timeout(2):
            while not any(  # noqa: ASYNC110 - poll synchronous repository calls on a worker thread
                isinstance(event, tuple) and event[0] == "heartbeat" for event in backend.events
            ):
                await asyncio.sleep(0.001)
        callback, args = handlers[signum]
        callback(*args)
        await asyncio.wait_for(task, timeout=3)
        stop = next(
            event
            for event in backend.events
            if isinstance(event, tuple) and event[0] == "request_stop"
        )
        finish = next(
            event for event in backend.events if isinstance(event, tuple) and event[0] == "finish"
        )
        assert stop[2]["stop_kind"] == "signal"
        assert stop[2]["reason"] == signum.name
        assert finish[1] == stop[1]
        assert backend.events.index(stop) < backend.events.index("backend_closed")
        assert backend.events.index("backend_closed") < backend.events.index(finish)
        assert backend.events[-2:] == ["connection_closed", "database_stopped"]
        assert not handlers
        assert not path.exists()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("startup", [False, True])
async def test_failure_releases_lifecycle_resources(socket_path: Path, startup: bool) -> None:
    backend = Backend(fail_recovery=startup, fail_cleanup=not startup)
    path = socket_path
    task = asyncio.create_task(local_control.serve(path, backend_factory=lambda **_: backend))
    if not startup:
        await wait_started(path, task)
        await request(path, "shutdown_clean")
    with pytest.raises(RuntimeError, match="recovery failed" if startup else "cleanup failed"):
        await task
    assert backend.events[-2:] == ["connection_closed", "database_stopped"]
    assert not path.exists()
    assert local_control._daemon_lock_released(path)
    if not startup:
        assert not any(
            isinstance(event, tuple) and event[0] == "finish" for event in backend.events
        )


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
async def test_os_signal_finishes_daemon(socket_path: Path, signum: signal.Signals) -> None:
    code = """
import asyncio, json, runpy, sys
from pathlib import Path
from merlin_tui.local_control import serve
Backend = runpy.run_path(sys.argv[2])["Backend"]
async def main():
    backend = Backend()
    await serve(Path(sys.argv[1]), backend_factory=lambda **_: backend)
    print(json.dumps(backend.events), flush=True)
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        str(socket_path),
        __file__,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(5):
            while not await asyncio.to_thread(socket_path.exists):
                assert process.returncode is None
                await asyncio.sleep(0.01)
        # Socket service is ready only after signal handlers have been registered.
        assert (await request(socket_path, "ping"))["value"] == "pong"
        process.send_signal(signum)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        assert process.returncode == 0, stderr.decode()
        events = json.loads(stdout)
        finish = next(event for event in events if isinstance(event, list) and event[0] == "finish")
        assert finish[2]["stop_kind"] == "signal"
        assert finish[2]["reason"] == signum.name
        assert events[-2:] == ["connection_closed", "database_stopped"]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
