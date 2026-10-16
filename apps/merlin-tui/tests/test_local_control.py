import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
from merlin_config import Config
from merlin_contracts import ToolContext, ToolSpec
from merlin_tui.backend import StreamEvent
from merlin_tui.local_control import (
    PROTOCOL_VERSION,
    LocalControlClient,
    LocalControlTuning,
    _acquire_daemon_lock,
    _close_writer,
    _configured_daemon_timeout,
    _daemon_command,
    _daemon_lock_released,
    _default_socket_path,
    ensure_daemon,
    serve,
    stop_daemon,
)


@pytest.mark.asyncio
async def test_saved_model_routing_reconnects_daemon_automatically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, Path]] = []

    async def stopped(path: Path, *, lease_id: str | None = None) -> bool:
        assert lease_id is None
        calls.append(("stop", path))
        return True

    async def started(path: Path) -> None:
        calls.append(("start", path))

    monkeypatch.setattr("merlin_tui.local_control.stop_daemon", stopped)
    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", started)
    socket_path = tmp_path / "merlin.sock"

    message = await LocalControlClient(socket_path=socket_path).apply_saved_model_routing()

    assert calls == [("stop", socket_path), ("start", socket_path)]
    assert "reconnected automatically" in message


@pytest.mark.asyncio
async def test_saved_model_routing_preserves_foreground_ownership(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[object] = []

    class LeaseWriter:
        def close(self) -> None:
            calls.append("lease_closed")

        async def wait_closed(self) -> None:
            return None

    async def stopped(path: Path, *, lease_id: str | None = None) -> bool:
        calls.append(("stop", path, lease_id))
        return True

    async def started(path: Path) -> bool:
        calls.append(("start", path))
        return True

    monkeypatch.setattr("merlin_tui.local_control.stop_daemon", stopped)
    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", started)
    socket_path = tmp_path / "merlin.sock"
    client = LocalControlClient(socket_path=socket_path)
    client._lease_id = "lease-1"
    client._lease_writer = LeaseWriter()  # type: ignore[assignment]

    async def reacquire() -> None:
        calls.append("lease_reacquired")

    monkeypatch.setattr(client, "acquire_foreground_lease", reacquire)

    await client.apply_saved_model_routing()

    assert calls == [
        ("stop", socket_path, "lease-1"),
        "lease_closed",
        ("start", socket_path),
        "lease_reacquired",
    ]


@pytest.mark.asyncio
async def test_client_socket_cleanup_suppresses_peer_broken_pipe() -> None:
    class BrokenWriter:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            raise BrokenPipeError(32, "Broken pipe")

    writer = BrokenWriter()
    await _close_writer(writer)  # type: ignore[arg-type]
    assert writer.closed is True


def test_daemon_lifecycle_deadlines_use_valid_config_and_safe_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "merlin_config.load_config",
        lambda: Config(
            values={"runtime": {"daemon_startup_seconds": 300, "daemon_shutdown_seconds": 60}}
        ),
    )
    assert _configured_daemon_timeout("daemon_startup_seconds", 180) == 300
    assert _configured_daemon_timeout("daemon_shutdown_seconds", 30) == 60

    def malformed():
        raise ValueError("invalid owner configuration")

    monkeypatch.setattr("merlin_config.load_config", malformed)
    assert _configured_daemon_timeout("daemon_shutdown_seconds", 30) == 30


@pytest.mark.asyncio
async def test_slow_stream_client_is_not_stranded_when_replay_journal_rotates() -> None:
    class BurstBackend(Backend):
        async def stream(self, prompt):
            del prompt
            for index in range(300):
                yield StreamEvent("content", str(index))

    socket_path = Path(f"/tmp/merlin-burst-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    await asyncio.to_thread(socket_path.with_suffix(".journal.json").unlink, missing_ok=True)
    task = asyncio.create_task(
        serve(
            socket_path,
            backend_factory=lambda **_: BurstBackend(),
            tuning=LocalControlTuning(max_stream_events=256),
        )
    )
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        client = LocalControlClient(socket_path=socket_path)
        assert [event.text async for event in client.stream("burst")] == [
            str(index) for index in range(300)
        ]
        journal = json.loads(socket_path.with_suffix(".journal.json").read_text())
        persisted = next(iter(journal["streams"].values()))
        assert len(persisted["events"]) == 256
        assert persisted["first_cursor"] == 45
        assert persisted["done"] is True
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_client_accepts_bounded_event_larger_than_asyncio_default_line_limit() -> None:
    class LargeEventBackend(Backend):
        async def stream(self, prompt):
            del prompt
            yield StreamEvent("tool", "x" * 70_000)

    socket_path = Path(f"/tmp/merlin-large-frame-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    await asyncio.to_thread(socket_path.with_suffix(".journal.json").unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: LargeEventBackend()))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        events = [
            event async for event in LocalControlClient(socket_path=socket_path).stream("large")
        ]
        assert [(event.kind, len(event.text)) for event in events] == [("tool", 70_000)]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_client_reassembles_status_larger_than_one_physical_frame() -> None:
    class LargeStatusBackend(Backend):
        async def campaign_status(self, campaign_id):
            return {
                "campaign_id": campaign_id,
                "status": "completed",
                "completion_document": {"receipts": ["x" * 1_100_000]},
            }

    socket_path = Path(f"/tmp/merlin-large-status-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    await asyncio.to_thread(socket_path.with_suffix(".journal.json").unlink, missing_ok=True)
    daemon = asyncio.create_task(
        serve(socket_path, backend_factory=lambda **_: LargeStatusBackend())
    )
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        status = await LocalControlClient(socket_path=socket_path).campaign_status("campaign-1")
        assert status.campaign_id == "campaign-1"
        assert status.status == "completed"
        assert isinstance(status, Mapping)
        assert isinstance(status.completion_document, Mapping)
        assert status.completion_document.get("receipts") == ("x" * 1_100_000,)
        assert len(status.completion_document.receipts[0]) == 1_100_000
    finally:
        daemon.cancel()
        with pytest.raises(asyncio.CancelledError):
            await daemon


def test_loading_journal_rebases_cursor_when_retention_is_reduced(tmp_path: Path) -> None:
    from merlin_tui.local_control import _load_journal

    journal = tmp_path / "daemon.journal.json"
    journal.write_text(
        json.dumps(
            {
                "streams": {
                    "request": {
                        "prompt": "test",
                        "events": [{"kind": "content", "text": str(index)} for index in range(300)],
                        "first_cursor": 1,
                        "done": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    _, streams = _load_journal(journal, max_stream_events=256)

    assert len(streams["request"].events) == 256
    assert streams["request"].first_cursor == 45


def test_loading_journal_applies_one_global_replay_budget(tmp_path: Path) -> None:
    from merlin_tui.local_control import _load_journal

    journal = tmp_path / "daemon.journal.json"
    journal.write_text(
        json.dumps(
            {
                "streams": {
                    f"request-{stream}": {
                        "prompt": "test",
                        "events": [{"kind": "content", "text": str(index)} for index in range(200)],
                        "first_cursor": 1,
                        "done": True,
                    }
                    for stream in range(3)
                }
            }
        ),
        encoding="utf-8",
    )

    _, streams = _load_journal(journal, max_stream_events=256)

    assert sum(len(state.events) for state in streams.values()) == 256
    assert streams["request-0"].events == []
    assert streams["request-0"].first_cursor == 201
    assert len(streams["request-1"].events) == 56
    assert len(streams["request-2"].events) == 200


def test_loading_journal_preserves_tool_events_under_budget_pressure(tmp_path: Path) -> None:
    """Tool/error events are the forensic record of a turn; only deltas trim."""
    from merlin_tui.local_control import _load_journal

    events = [
        *[{"kind": "content", "text": str(index)} for index in range(300)],
        {"kind": "tool", "text": "assessment_execute failed: digest mismatch"},
        {"kind": "error", "text": "provider failed"},
        *[{"kind": "thinking", "text": str(index)} for index in range(100)],
    ]
    journal = tmp_path / "daemon.journal.json"
    journal.write_text(
        json.dumps(
            {
                "streams": {
                    "request": {
                        "prompt": "test",
                        "events": events,
                        "first_cursor": 1,
                        "done": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    _, streams = _load_journal(journal, max_stream_events=64)

    persisted = streams["request"]
    kinds = [event["kind"] for event in persisted.events]
    assert kinds.count("tool") == 1
    assert kinds.count("error") == 1
    assert len(persisted.events) == 64 + 2
    assert persisted.first_cursor == 1 + (400 - 64)


def test_write_journal_preserves_tool_events_under_budget_pressure(tmp_path: Path) -> None:
    from merlin_tui.local_control import _StreamState, _write_journal

    state = _StreamState("request", "test")
    state.events = [
        *[{"kind": "content", "text": str(index)} for index in range(300)],
        {"kind": "tool", "text": "assessment_execute completed: {...}"},
    ]
    state.done = True
    journal = tmp_path / "daemon.journal.json"

    _write_journal(journal, {}, {"request": state}, max_stream_events=64)

    persisted = json.loads(journal.read_text(encoding="utf-8"))["streams"]["request"]
    assert len(persisted["events"]) == 65
    assert persisted["events"][-1]["kind"] == "tool"
    assert persisted["first_cursor"] == 1 + 236


def test_completed_replay_payload_is_byte_bounded_with_idempotency_tombstones(
    tmp_path: Path,
) -> None:
    from merlin_tui.local_control import _load_journal, _write_journal

    journal = tmp_path / "daemon.journal.json"
    completed = {f"request-{index}": {"type": "result", "value": "x" * 300} for index in range(6)}

    _write_journal(journal, completed, {}, max_completed_bytes=700)

    persisted = json.loads(journal.read_text(encoding="utf-8"))["completed"]
    assert list(persisted) == list(completed)
    assert persisted["request-5"] == completed["request-5"]
    assert persisted["request-4"] == completed["request-4"]
    assert persisted["request-0"]["type"] == "error"
    assert "no longer available for replay" in persisted["request-0"]["error"]

    loaded, _ = _load_journal(journal, max_completed_bytes=700)
    assert loaded == persisted


@pytest.mark.asyncio
async def test_expired_completed_replay_cannot_repeat_command(tmp_path: Path) -> None:
    del tmp_path
    socket_path = Path(f"/tmp/merlin-expired-replay-{os.getpid()}.sock")
    journal = socket_path.with_suffix(".journal.json")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    await asyncio.to_thread(journal.unlink, missing_ok=True)
    journal.write_text(
        json.dumps(
            {
                "completed": {
                    "old-command": {
                        "type": "error",
                        "error": "The completed command result is no longer available for replay",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    backend = Backend()
    daemon = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        response = await _request(
            socket_path,
            {
                "method": "get_permission_mode",
                "request_id": "old-command",
                "args": [],
                "kwargs": {},
            },
        )
        assert response["type"] == "error"
        assert backend.permission_reads == 0
    finally:
        daemon.cancel()
        with pytest.raises(asyncio.CancelledError):
            await daemon
        await asyncio.to_thread(socket_path.unlink, missing_ok=True)
        await asyncio.to_thread(journal.unlink, missing_ok=True)


@pytest.mark.asyncio
async def test_protocol_reports_cursor_gap_instead_of_silent_event_loss() -> None:
    socket_path = Path(f"/tmp/merlin-cursor-gap-{os.getpid()}.sock")
    journal = socket_path.with_suffix(".journal.json")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    await asyncio.to_thread(journal.unlink, missing_ok=True)
    journal.write_text(
        json.dumps(
            {
                "streams": {
                    "retained-stream": {
                        "prompt": "test",
                        "events": [{"kind": "content", "text": str(index)} for index in range(256)],
                        "first_cursor": 45,
                        "done": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    daemon = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: Backend()))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        response = await _request(
            socket_path,
            {
                "method": "stream",
                "request_id": "retained-stream",
                "prompt": "test",
                "cursor": 0,
            },
        )
        assert response["type"] == "error"
        assert "cursor is no longer available" in str(response["error"])
    finally:
        daemon.cancel()
        with pytest.raises(asyncio.CancelledError):
            await daemon
        await asyncio.to_thread(journal.unlink, missing_ok=True)


@pytest.mark.parametrize(
    ("kwargs", "setting"),
    [
        ({"max_stream_events": 255}, "max_stream_events"),
        ({"stream_journal_batch": 0}, "stream_journal_batch"),
        ({"request_timeout_seconds": 301}, "request_timeout_seconds"),
        ({"config_reload_poll_seconds": 0.01}, "config_reload_poll_seconds"),
    ],
)
def test_local_control_tuning_rejects_unsafe_bounds(kwargs, setting: str) -> None:
    with pytest.raises(ValueError, match=setting):
        LocalControlTuning(**kwargs)


@pytest.mark.asyncio
async def test_reasoning_deltas_do_not_force_one_journal_rewrite_each(
    monkeypatch,
) -> None:
    class ReasoningBackend(Backend):
        async def stream(self, prompt):
            del prompt
            for index in range(12):
                yield StreamEvent("thinking", f"reasoning-{index}")

    from merlin_tui import local_control

    writes = 0
    write_journal = local_control._write_journal

    def counted_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        return write_journal(*args, **kwargs)

    monkeypatch.setattr(local_control, "_write_journal", counted_write)
    socket_path = Path(f"/tmp/merlin-reasoning-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    await asyncio.to_thread(socket_path.with_suffix(".journal.json").unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: ReasoningBackend()))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        client = LocalControlClient(socket_path=socket_path)
        events = [event async for event in client.stream("reason")]
        assert len(events) == 12
        # One completion flush, the terminal-delivery durability acknowledgement,
        # and one tombstone compaction after successful delivery;
        # never one synchronous full-journal rewrite per reasoning fragment.
        assert writes == 3
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_daemon_command_uses_module_from_source(monkeypatch) -> None:
    monkeypatch.delattr("sys.frozen", raising=False)
    command = _daemon_command()
    assert command[1:] == ["-m", "merlin_tui.local_control", "daemon"]


def test_daemon_command_restarts_frozen_executable(monkeypatch) -> None:
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", "/release/payload/merlin-runtime")
    assert _daemon_command() == ["/release/payload/merlin-runtime", "daemon"]


def test_socket_path_falls_back_when_home_exceeds_macos_unix_limit(monkeypatch) -> None:
    monkeypatch.delenv("MERLIN_SOCKET_PATH", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: Path("/tmp") / ("isolated-home-segment-" * 8))
    socket_path = _default_socket_path()
    assert len(os.fsencode(socket_path)) <= 96
    assert socket_path.name == "daemon.sock"


@pytest.mark.asyncio
async def test_ensure_daemon_fails_fast_when_spawned_process_exits(monkeypatch) -> None:
    class ExitedProcess:
        def poll(self):
            return 7

    async def never_ready(_socket_path):
        return False

    monkeypatch.setattr("merlin_tui.local_control._ping", never_ready)
    monkeypatch.setattr("merlin_tui.local_control._spawn_daemon", lambda: ExitedProcess())
    monkeypatch.setattr(
        "merlin_tui.local_control._daemon_failure_detail",
        lambda: "Last log message: provider missing",
    )

    with pytest.raises(RuntimeError, match="status 7.*provider missing"):
        await ensure_daemon(Path("/tmp/merlin-never-ready.sock"), startup_seconds=30)


@pytest.mark.asyncio
async def test_ensure_daemon_reports_whether_this_client_started_it(monkeypatch) -> None:
    ready = iter((False, True))

    async def ping(_path: Path) -> bool:
        return next(ready)

    class Process:
        def poll(self):
            return None

    monkeypatch.setattr("merlin_tui.local_control._ping", ping)
    monkeypatch.setattr("merlin_tui.local_control._spawn_daemon", Process)

    assert await ensure_daemon(Path("/tmp/merlin-owned-daemon.sock"), startup_seconds=1)


@pytest.mark.asyncio
async def test_concurrent_daemon_start_cannot_replace_live_socket() -> None:
    socket_path = Path(f"/tmp/merlin-single-owner-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    await asyncio.to_thread(socket_path.with_suffix(".lock").unlink, missing_ok=True)
    primary = Backend()
    first = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: primary))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        assert await asyncio.to_thread(socket_path.exists)

        with pytest.raises(RuntimeError, match="already starting or running"):
            await serve(socket_path, backend_factory=lambda **_: Backend())

        client = LocalControlClient(socket_path=socket_path)
        assert await client.get_permission_mode() == "prompt"
        assert not primary.closed
    finally:
        assert await stop_daemon(socket_path)
        await first


@pytest.mark.asyncio
async def test_daemon_watchdog_reconciles_campaign_ownership_repeatedly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WatchdogBackend(Backend):
        def __init__(self) -> None:
            self.recoveries = 0
            self.reconciled = asyncio.Event()

        async def recover_background(self):
            self.recoveries += 1
            if self.recoveries >= 2:
                self.reconciled.set()

    monkeypatch.setattr("merlin_tui.local_control._RECOVERY_WATCHDOG_SECONDS", 0.01)
    socket_path = Path(f"/tmp/merlin-recovery-watchdog-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    await asyncio.to_thread(socket_path.with_suffix(".lock").unlink, missing_ok=True)
    backend = WatchdogBackend()
    daemon = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    try:
        await asyncio.wait_for(backend.reconciled.wait(), timeout=1)
        assert backend.recoveries >= 2
    finally:
        assert await stop_daemon(socket_path)
        await daemon


class Backend:
    recovered = False
    closed = False
    permission_reads = 0

    async def recover_background(self):
        self.recovered = True

    async def stream(self, prompt):
        yield StreamEvent("content", f"daemon:{prompt}")

    async def get_permission_mode(self):
        self.permission_reads += 1
        return "prompt"

    async def conversation_context(self):
        return {
            "session_id": "session-1",
            "messages": [{"sequence": 1, "role": "user"}],
            "compactions": [],
        }

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_owner_config_watcher_retains_invalid_then_applies_valid_config(
    tmp_path: Path, monkeypatch
) -> None:
    class ReloadBackend(Backend):
        def __init__(self) -> None:
            self.reloads = []
            self.failure_types = []
            self.reloaded = asyncio.Event()
            self.failed = asyncio.Event()

        async def reload_configuration(self, config):
            self.reloads.append(config)
            self.reloaded.set()

        def configuration_reload_failed(self, error):
            self.failure_types.append(type(error).__name__)
            self.failed.set()

    home = tmp_path / "home"
    config_dir = home / ".merlin"
    config_dir.mkdir(parents=True, mode=0o700)
    config_path = config_dir / "config.toml"
    config_path.write_text('permission_mode="prompt"\n', encoding="utf-8")
    config_path.chmod(0o600)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    backend = ReloadBackend()
    socket_path = Path(f"/tmp/merlin-config-reload-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    await asyncio.to_thread(socket_path.with_suffix(".journal.json").unlink, missing_ok=True)
    daemon = asyncio.create_task(
        serve(
            socket_path,
            backend_factory=lambda **_: backend,
            tuning=LocalControlTuning(config_reload_poll_seconds=0.1),
        )
    )
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)

        invalid = config_dir / "config.invalid"
        invalid.write_text("mystery=true\n", encoding="utf-8")
        invalid.chmod(0o600)
        invalid.replace(config_path)
        await asyncio.wait_for(backend.failed.wait(), timeout=2)
        assert backend.reloads == []
        assert backend.failure_types == ["ValueError"]

        valid = config_dir / "config.valid"
        valid.write_text('permission_mode="yolo"\n', encoding="utf-8")
        valid.chmod(0o600)
        valid.replace(config_path)
        await asyncio.wait_for(backend.reloaded.wait(), timeout=2)
        assert backend.reloads[-1].values["permission_mode"] == "yolo"
    finally:
        daemon.cancel()
        with pytest.raises(asyncio.CancelledError):
            await daemon
        await asyncio.to_thread(socket_path.with_suffix(".journal.json").unlink, missing_ok=True)


@pytest.mark.asyncio
async def test_local_control_keeps_backend_in_daemon_and_streams_to_client() -> None:
    backend = Backend()
    socket_path = Path(f"/tmp/merlin-test-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        client = LocalControlClient(socket_path=socket_path)
        assert await client.get_permission_mode() == "prompt"
        context = await client.conversation_context()
        assert context.session_id == "session-1"
        assert context.messages[0].sequence == 1
        assert [event.text async for event in client.stream("hello")] == ["daemon:hello"]
        await client.close()
        assert backend.recovered
        assert not backend.closed
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert backend.closed


@pytest.mark.asyncio
async def test_stop_waits_for_backend_cleanup_and_socket_removal() -> None:
    class LifecycleBackend(Backend):
        detached = False

        async def detach(self) -> None:
            self.detached = True

    backend = LifecycleBackend()
    socket_path = Path(f"/tmp/merlin-stop-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    for _ in range(100):
        if await asyncio.to_thread(socket_path.exists):
            break
        await asyncio.sleep(0.01)

    assert await stop_daemon(socket_path)
    await task
    assert backend.closed
    assert not backend.detached
    assert not await asyncio.to_thread(socket_path.exists)
    assert await asyncio.to_thread(_daemon_lock_released, socket_path)


@pytest.mark.asyncio
async def test_backend_cleanup_failure_still_releases_daemon_process_ownership() -> None:
    class FailingBackend(Backend):
        async def close(self) -> None:
            raise RuntimeError("resource cleanup failed")

    socket_path = Path(f"/tmp/merlin-failed-cleanup-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: FailingBackend()))
    for _ in range(100):
        if await asyncio.to_thread(socket_path.exists):
            break
        await asyncio.sleep(0.01)

    assert (await _request(socket_path, {"method": "shutdown_clean"}))["value"] == "stopping"
    with pytest.raises(RuntimeError, match="resource cleanup failed"):
        await task
    assert not await asyncio.to_thread(socket_path.exists)
    assert await asyncio.to_thread(_daemon_lock_released, socket_path)


@pytest.mark.asyncio
async def test_daemon_lock_records_owner_pid_and_force_stop_terminates_orphan() -> None:
    from merlin_tui.local_control import _daemon_lock_owner

    socket_path = Path(f"/tmp/merlin-orphan-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    await asyncio.to_thread(socket_path.with_suffix(".lock").unlink, missing_ok=True)

    # Simulate an orphaned runtime: a live process that holds the daemon lock
    # but never serves the socket.
    orphan = subprocess.Popen(  # noqa: ASYNC220 -- intentional orphan-process fixture
        [
            sys.executable,
            "-c",
            "import time; "
            "from pathlib import Path; "
            "from merlin_tui.local_control import _acquire_daemon_lock; "
            f"_handle = _acquire_daemon_lock(Path({str(socket_path)!r})); "
            "time.sleep(60)",
        ]
    )
    try:
        for _ in range(100):
            owner = await asyncio.to_thread(_daemon_lock_owner, socket_path)
            if owner == orphan.pid:
                break
            await asyncio.sleep(0.05)
        assert await asyncio.to_thread(_daemon_lock_owner, socket_path) == orphan.pid

        with pytest.raises(RuntimeError, match="stop --force"):
            await stop_daemon(socket_path)

        assert await stop_daemon(socket_path, force=True)
        assert await asyncio.to_thread(_daemon_lock_released, socket_path)
        assert orphan.wait() is not None
    finally:
        orphan.kill()
        orphan.wait()


@pytest.mark.asyncio
async def test_tui_client_clean_shutdown_closes_instead_of_detaching_backend() -> None:
    class LifecycleBackend(Backend):
        def __init__(self) -> None:
            self.closed = False
            self.detached = False

        async def close(self) -> None:
            self.closed = True

        async def detach(self) -> None:
            self.detached = True

    backend = LifecycleBackend()
    socket_path = Path(f"/tmp/merlin-clean-stop-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    for _ in range(100):
        if await asyncio.to_thread(socket_path.exists):
            break
        await asyncio.sleep(0.01)

    client = LocalControlClient(socket_path=socket_path)
    await client.shutdown()
    await task

    assert backend.closed
    assert not backend.detached
    assert not await asyncio.to_thread(socket_path.exists)


@pytest.mark.asyncio
async def test_client_that_started_daemon_closes_it_cleanly() -> None:
    class LifecycleBackend(Backend):
        def __init__(self) -> None:
            self.closed = False
            self.detached = False

        async def close(self) -> None:
            self.closed = True

        async def detach(self) -> None:
            self.detached = True

    backend = LifecycleBackend()
    socket_path = Path(f"/tmp/merlin-owned-client-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    for _ in range(100):
        if await asyncio.to_thread(socket_path.exists):
            break
        await asyncio.sleep(0.01)

    client = LocalControlClient(socket_path=socket_path, owns_daemon=True)
    await client.close()
    await task

    assert backend.closed
    assert not backend.detached
    assert not await asyncio.to_thread(socket_path.exists)


@pytest.mark.asyncio
async def test_foreground_lease_disconnect_stops_daemon_and_owned_resources() -> None:
    backend = Backend()
    socket_path = Path(f"/tmp/merlin-foreground-lease-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    for _ in range(100):
        if await asyncio.to_thread(socket_path.exists):
            break
        await asyncio.sleep(0.01)

    client = LocalControlClient(socket_path=socket_path)
    await client.acquire_foreground_lease()
    assert client._lease_writer is not None
    await client.release_foreground_lease()

    await asyncio.wait_for(task, timeout=2)
    assert backend.closed
    assert not await asyncio.to_thread(socket_path.exists)


@pytest.mark.asyncio
async def test_foreground_lease_disconnect_keeps_active_campaign_daemon_alive(
    monkeypatch,
) -> None:
    class CampaignBackend(Backend):
        def __init__(self) -> None:
            self.active = True
            self.checked = asyncio.Event()

        async def has_active_background_work(self) -> bool:
            self.checked.set()
            return self.active

    monkeypatch.setattr("merlin_tui.local_control._RECOVERY_WATCHDOG_SECONDS", 0.02)
    backend = CampaignBackend()
    socket_path = Path(f"/tmp/merlin-active-campaign-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)

        client = LocalControlClient(socket_path=socket_path)
        await client.acquire_foreground_lease()
        await client.release_foreground_lease()
        await asyncio.wait_for(backend.checked.wait(), timeout=1)
        await asyncio.sleep(0.05)

        assert not task.done()
        assert not backend.closed
        assert await asyncio.to_thread(socket_path.exists)

        backend.active = False
        await asyncio.wait_for(task, timeout=1)
        assert backend.closed
        assert not await asyncio.to_thread(socket_path.exists)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_foreground_lease_disconnect_rechecks_transient_idle_campaign(
    monkeypatch,
) -> None:
    class CampaignBackend(Backend):
        def __init__(self) -> None:
            self.checks = 0

        async def has_active_background_work(self) -> bool:
            self.checks += 1
            return self.checks > 1

    monkeypatch.setattr("merlin_tui.local_control._IDLE_SHUTDOWN_CONFIRM_SECONDS", 0.02)
    backend = CampaignBackend()
    socket_path = Path(f"/tmp/merlin-transient-idle-campaign-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    client = LocalControlClient(socket_path=socket_path)
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        await client.acquire_foreground_lease()
        await client.release_foreground_lease()
        for _ in range(100):
            if backend.checks >= 2:
                break
            await asyncio.sleep(0.01)

        assert backend.checks >= 2
        assert not task.done()
        assert await asyncio.to_thread(socket_path.exists)
        await client.shutdown()
        await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_foreground_lease_heartbeat_keeps_daemon_alive(monkeypatch) -> None:
    from uuid import uuid4

    monkeypatch.setattr("merlin_tui.local_control._FOREGROUND_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr("merlin_tui.local_control._FOREGROUND_LEASE_TIMEOUT_SECONDS", 0.05)
    backend = Backend()
    socket_path = Path(f"/tmp/merlin-heartbeat-live-{uuid4().hex[:12]}.sock")
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    client = LocalControlClient(socket_path=socket_path)
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        await client.acquire_foreground_lease()
        await asyncio.sleep(0.15)
        assert not task.done()
        assert not backend.closed
        await client.shutdown()
        await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_legacy_idle_daemon_is_replaced_before_foreground_attach(
    monkeypatch,
) -> None:
    from uuid import uuid4

    path = Path(f"/tmp/merlin-legacy-upgrade-{uuid4().hex[:12]}.sock")
    state = {"legacy": True}
    calls: list[tuple[str, str | None]] = []
    handlers: set[asyncio.Task[None]] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = json.loads(await reader.readline())
            assert request["method"] == "foreground_lease"
            response: dict[str, object] = {
                "type": "result",
                "value": "attached",
                "lease_id": "legacy-lease" if state["legacy"] else "current-lease",
            }
            if not state["legacy"]:
                response["heartbeat_seconds"] = 0.01
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    async def tracked_handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        try:
            await handle(reader, writer)
        finally:
            handlers.discard(task)

    server = await asyncio.start_unix_server(tracked_handle, path)

    async def stop(_path: Path, *, lease_id: str | None = None) -> bool:
        calls.append(("stop", lease_id))
        state["legacy"] = False
        return True

    async def start(_path: Path) -> bool:
        calls.append(("start", None))
        return True

    monkeypatch.setattr("merlin_tui.local_control.stop_daemon", stop)
    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", start)
    client = LocalControlClient(socket_path=path)
    try:
        await client.acquire_foreground_lease(close_notifications=True)
        assert client._lease_id == "current-lease"
        assert calls == [("stop", "legacy-lease"), ("start", None)]
        await client.release_foreground_lease()
    finally:
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.gather(*handlers)


@pytest.mark.asyncio
async def test_foreground_lease_without_heartbeats_stops_daemon(monkeypatch) -> None:
    from uuid import uuid4

    monkeypatch.setattr("merlin_tui.local_control._FOREGROUND_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr("merlin_tui.local_control._FOREGROUND_LEASE_TIMEOUT_SECONDS", 0.05)
    backend = Backend()
    socket_path = Path(f"/tmp/merlin-heartbeat-expired-{uuid4().hex[:12]}.sock")
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    client = LocalControlClient(socket_path=socket_path)
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        await client.acquire_foreground_lease()
        assert client._lease_heartbeat is not None
        client._lease_heartbeat.cancel()
        await asyncio.gather(client._lease_heartbeat, return_exceptions=True)
        client._lease_heartbeat = None

        await asyncio.wait_for(task, timeout=1)
        assert backend.closed
        assert not await asyncio.to_thread(socket_path.exists)
    finally:
        await client.release_foreground_lease()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_suspended_foreground_process_expires_its_daemon(monkeypatch, tmp_path) -> None:
    import signal
    from uuid import uuid4

    monkeypatch.setattr("merlin_tui.local_control._FOREGROUND_HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr("merlin_tui.local_control._FOREGROUND_LEASE_TIMEOUT_SECONDS", 0.1)
    backend = Backend()
    socket_path = Path(f"/tmp/merlin-heartbeat-suspended-{uuid4().hex[:12]}.sock")
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    script = tmp_path / "suspended_lease.py"
    await asyncio.to_thread(
        script.write_text,
        f"""import asyncio, os, signal
from pathlib import Path
from merlin_tui.local_control import LocalControlClient

async def main():
    client = LocalControlClient(socket_path=Path({str(socket_path)!r}))
    await client.acquire_foreground_lease()
    print('attached', flush=True)
    os.kill(os.getpid(), signal.SIGSTOP)
    try:
        await client.wait_for_foreground_close()
    except (ConnectionError, OSError, RuntimeError):
        pass
    finally:
        await client.release_foreground_lease()

asyncio.run(main())
""",
    )
    process = None
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert await process.stdout.readline() == b"attached\n"
        await asyncio.wait_for(task, timeout=1)
        assert backend.closed
        assert not await asyncio.to_thread(socket_path.exists)
        os.kill(process.pid, signal.SIGCONT)
        assert await asyncio.wait_for(process.wait(), timeout=1) == 0
    finally:
        if process is not None and process.returncode is None:
            os.kill(process.pid, signal.SIGCONT)
            process.terminate()
            await process.wait()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_foreground_lease_is_exclusive_and_only_owner_can_stop_daemon() -> None:
    backend = Backend()
    socket_path = Path(f"/tmp/merlin-exclusive-lease-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    for _ in range(100):
        if await asyncio.to_thread(socket_path.exists):
            break
        await asyncio.sleep(0.01)

    owner = LocalControlClient(socket_path=socket_path)
    observer = LocalControlClient(socket_path=socket_path)
    await owner.acquire_foreground_lease()
    with pytest.raises(RuntimeError, match="already has a foreground owner"):
        await observer.acquire_foreground_lease()
    with pytest.raises(RuntimeError, match="reserved for the foreground owner"):
        await observer.shutdown()
    raw_shutdown = await _request(socket_path, {"method": "shutdown"})
    assert raw_shutdown["type"] == "error"
    assert "reserved for the foreground owner" in raw_shutdown["error"]
    with pytest.raises(RuntimeError, match="reserved for the foreground owner"):
        await stop_daemon(socket_path)
    assert await _request(socket_path, {"method": "ping"}) == {
        "type": "result",
        "value": "pong",
        "protocol": PROTOCOL_VERSION,
    }

    await owner.shutdown()
    await task
    assert backend.closed


@pytest.mark.asyncio
async def test_force_stop_terminates_daemon_despite_foreground_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = Backend()
    socket_path = Path(f"/tmp/merlin-force-lease-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    for _ in range(100):
        if await asyncio.to_thread(socket_path.exists):
            break
        await asyncio.sleep(0.01)

    owner = LocalControlClient(socket_path=socket_path)
    await owner.acquire_foreground_lease()

    from merlin_tui import local_control

    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(local_control.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(local_control, "_daemon_lock_owner", lambda _path: 424242)
    monkeypatch.setattr(local_control, "_daemon_lock_released", lambda _path: True)

    assert await stop_daemon(socket_path, force=True)
    assert kills == [(424242, local_control.signal.SIGTERM)]

    await owner.shutdown()
    await task
    assert backend.closed


@pytest.mark.asyncio
async def test_legacy_shutdown_method_uses_clean_backend_teardown() -> None:
    class LifecycleBackend(Backend):
        def __init__(self) -> None:
            self.closed = False
            self.detached = False

        async def close(self) -> None:
            self.closed = True

        async def detach(self) -> None:
            self.detached = True

    backend = LifecycleBackend()
    socket_path = Path(f"/tmp/merlin-legacy-clean-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    for _ in range(100):
        if await asyncio.to_thread(socket_path.exists):
            break
        await asyncio.sleep(0.01)

    response = await _request(socket_path, {"method": "shutdown"})
    assert response["value"] == "stopping"
    await task

    assert backend.closed
    assert not backend.detached


@pytest.mark.asyncio
async def test_daemon_serializes_concurrent_approvals_on_one_channel() -> None:
    from merlin_tui.local_control import _active_channel, daemon_tool_confirmer

    class ApprovalChannel:
        def __init__(self) -> None:
            self.approval_lock = asyncio.Lock()
            self.pending_id: str | None = None
            self.order: list[str] = []

        async def send(self, value) -> None:
            assert self.pending_id is None
            self.pending_id = str(value["approval_id"])
            self.order.append(str(value["spec"].name))

        async def receive(self):
            await asyncio.sleep(0)
            approval_id, self.pending_id = self.pending_id, None
            return {
                "type": "approval_response",
                "approval_id": approval_id,
                "decision": True,
            }

    channel = ApprovalChannel()
    token = _active_channel.set(channel)  # type: ignore[arg-type]
    try:
        results = await asyncio.gather(
            daemon_tool_confirmer(
                ToolSpec(name="first", description="", input_schema={}),
                ToolContext(session_id="session", workspace_root="/workspace"),
                {},
            ),
            daemon_tool_confirmer(
                ToolSpec(name="second", description="", input_schema={}),
                ToolContext(session_id="session", workspace_root="/workspace"),
                {},
            ),
        )
    finally:
        _active_channel.reset(token)

    assert results == [True, True]
    assert channel.order == ["first", "second"]


@pytest.mark.asyncio
async def test_exact_command_session_permission_roundtrips_daemon_socket(tmp_path: Path) -> None:
    from uuid import uuid4

    from merlin_permissions import PermissionDecision, PermissionEngine, PermissionMode
    from merlin_tools import workspace_tool_broker

    class Store:
        def __init__(self):
            self.rules = {}
            self.audits = []

        async def find_rule(self, request):
            return self.rules.get((request.scope.fingerprint, request.effect))

        async def save_rule(self, rule):
            self.rules[(rule.scope_fingerprint, rule.effect)] = rule

        async def record_audit(self, audit):
            self.audits.append(audit)

    results = []

    class CommandBackend(Backend):
        def __init__(self, tool_confirmer):
            self.broker = workspace_tool_broker(
                permissions=PermissionEngine(PermissionMode.PROMPT, Store()),
                confirmer=tool_confirmer,
            )
            self.context = ToolContext(session_id="same-session", workspace_root=str(tmp_path))

        async def stream(self, prompt):
            result = await self.broker.invoke(
                "shell_command", self.context, {"argv": ["/bin/echo", prompt]}
            )
            results.append(result)
            yield StreamEvent("error" if result.is_error else "content", result.content.strip())

    prompts = []

    async def confirm(spec, context, arguments):
        assert spec.name == "shell_command"
        assert context.session_id == "same-session"
        assert arguments["__permission_command_scope"] is True
        assert arguments["argv"] == ["/bin/echo", "a" if not prompts else "b"]
        prompts.append(arguments)
        return (
            PermissionDecision.ALLOW_COMMAND_SESSION
            if len(prompts) == 1
            else PermissionDecision.DENY_ONCE
        )

    socket_path = Path(f"/tmp/merlin-command-{uuid4().hex}.sock")
    daemon = asyncio.create_task(serve(socket_path, backend_factory=CommandBackend))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        client = LocalControlClient(confirmer=confirm, socket_path=socket_path)
        for _ in range(2):
            events = [event async for event in client.stream("a")]
            assert [(event.kind, event.text) for event in events] == [("content", "a")]
        assert len(prompts) == 1

        events = [event async for event in client.stream("b")]
        assert len(prompts) == 2
        assert [(event.kind, event.text) for event in events] == [
            ("error", "permission denied for tool: shell_command")
        ]
        assert [result.metadata.get("exit_code") for result in results] == [0, 0, None]
        assert results[-1].metadata["permission_denied"] is True
    finally:
        daemon.cancel()
        with pytest.raises(asyncio.CancelledError):
            await daemon
        socket_path.with_suffix(".journal.json").unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_process_daemon_retains_owner_lock_after_socket_cleanup() -> None:
    from merlin_tui.local_control import _PROCESS_LIFETIME_LOCKS

    backend = Backend()
    socket_path = Path(f"/tmp/merlin-process-lock-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    task = asyncio.create_task(
        serve(
            socket_path,
            backend_factory=lambda **_: backend,
            hold_lock_until_process_exit=True,
        )
    )
    for _ in range(100):
        if await asyncio.to_thread(socket_path.exists):
            break
        await asyncio.sleep(0.01)

    response = await _request(socket_path, {"method": "shutdown"})
    assert response["value"] == "stopping"
    await task
    assert not await asyncio.to_thread(socket_path.exists)
    assert not await asyncio.to_thread(_daemon_lock_released, socket_path)

    retained = _PROCESS_LIFETIME_LOCKS.pop()
    retained.close()
    assert await asyncio.to_thread(_daemon_lock_released, socket_path)


def test_daemon_lock_release_is_part_of_shutdown_completion(tmp_path) -> None:
    socket_path = tmp_path / "daemon.sock"
    owner = _acquire_daemon_lock(socket_path)
    try:
        assert not _daemon_lock_released(socket_path)
    finally:
        owner.close()
    assert _daemon_lock_released(socket_path)


@pytest.mark.asyncio
async def test_stop_rejects_unreachable_daemon_that_still_owns_process_lock(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "daemon.sock"
    owner = _acquire_daemon_lock(socket_path)
    try:
        with pytest.raises(RuntimeError, match="unreachable.*process lock"):
            await stop_daemon(socket_path, shutdown_seconds=0.1)
    finally:
        owner.close()


@pytest.mark.asyncio
async def test_stop_of_absent_daemon_with_released_lock_is_idempotent(tmp_path: Path) -> None:
    assert not await stop_daemon(tmp_path / "daemon.sock", shutdown_seconds=0.1)


@pytest.mark.asyncio
async def test_concurrent_client_approvals_stay_on_their_own_channels() -> None:
    class ApprovalBackend(Backend):
        def __init__(self, confirmer):
            self.confirmer = confirmer

        async def stream(self, prompt):
            decision = await self.confirmer(
                ToolSpec(name="demo", description="demo", input_schema={}),
                ToolContext(session_id=prompt, workspace_root="/tmp"),
                {"prompt": prompt},
            )
            yield StreamEvent("content", f"{prompt}:{decision}")

    socket_directory = Path(f"/tmp/rp-{os.getpid()}")
    await asyncio.to_thread(socket_directory.mkdir, mode=0o700, exist_ok=True)
    socket_path = socket_directory / "approval.sock"
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    backend = None

    def factory(**kwargs):
        nonlocal backend
        backend = ApprovalBackend(kwargs["tool_confirmer"])
        return backend

    task = asyncio.create_task(serve(socket_path, backend_factory=factory))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)

        async def allow(_spec, context, _arguments):
            await asyncio.sleep(0.01)
            return context.session_id == "allow-client"

        first = LocalControlClient(confirmer=allow, socket_path=socket_path)
        second = LocalControlClient(confirmer=allow, socket_path=socket_path)

        async def collect(client, prompt):
            return [event.text async for event in client.stream(prompt)]

        allowed, denied = await asyncio.gather(
            collect(first, "allow-client"), collect(second, "deny-client")
        )
        assert "allow-client:True" in allowed[0]
        assert "deny-client:False" in denied[0]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_disconnected_approval_cannot_be_claimed_by_another_client() -> None:
    class ApprovalBackend(Backend):
        def __init__(self, confirmer):
            self.confirmer = confirmer

        async def stream(self, prompt):
            decision = await self.confirmer(
                ToolSpec(name="demo", description="demo", input_schema={}),
                ToolContext(session_id=prompt, workspace_root="/tmp"),
                {"prompt": prompt},
            )
            yield StreamEvent("content", f"decision:{decision}")

    socket_path = Path(f"/tmp/merlin-approval-disconnect-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)

    def factory(**kwargs):
        return ApprovalBackend(kwargs["tool_confirmer"])

    daemon = asyncio.create_task(serve(socket_path, backend_factory=factory))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)

        reader, writer = await asyncio.open_unix_connection(socket_path)
        writer.write(
            (
                json.dumps(
                    {
                        "method": "stream",
                        "request_id": "orphaned-approval",
                        "prompt": "owner-client",
                        "cursor": 0,
                    }
                )
                + "\n"
            ).encode()
        )
        await writer.drain()
        approval = json.loads(await reader.readline())
        assert approval["type"] == "approval"
        writer.close()
        await writer.wait_closed()

        for _ in range(100):
            response = await _request(
                socket_path,
                {
                    "method": "stream",
                    "request_id": "orphaned-approval",
                    "prompt": "owner-client",
                    "cursor": 0,
                },
            )
            if response["type"] == "error":
                break
            await asyncio.sleep(0.01)
        assert response["type"] == "error"
        assert "disconnected" in str(response["error"])

        approvals_seen = 0

        async def allow(*_args):
            nonlocal approvals_seen
            approvals_seen += 1
            return True

        other = LocalControlClient(confirmer=allow, socket_path=socket_path)
        assert [event.text async for event in other.stream("other-client")] == ["decision:True"]
        assert approvals_seen == 1
    finally:
        daemon.cancel()
        with pytest.raises(asyncio.CancelledError):
            await daemon


async def _request(socket_path: Path, value: dict[str, object]) -> dict[str, object]:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    writer.write((json.dumps(value) + "\n").encode())
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    return response


@pytest.mark.asyncio
async def test_request_ids_make_completed_calls_idempotent() -> None:
    backend = Backend()
    socket_path = Path(f"/tmp/merlin-idempotent-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        request = {
            "method": "get_permission_mode",
            "request_id": "same-request",
            "args": [],
            "kwargs": {},
        }
        assert (await _request(socket_path, request))["value"] == "prompt"
        assert (await _request(socket_path, request))["value"] == "prompt"
        assert backend.permission_reads == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_snapshot_reads_do_not_wait_for_or_grow_the_replay_journal(monkeypatch) -> None:
    import threading

    from merlin_tui import local_control

    entered = threading.Event()
    release = threading.Event()
    write_journal = local_control._write_journal

    def blocked_write(*args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test journal was never released")
        return write_journal(*args, **kwargs)

    class SnapshotBackend(Backend):
        async def list_agents(self):
            return ["agent output" * 10_000]

    monkeypatch.setattr(local_control, "_write_journal", blocked_write)
    socket_path = Path(f"/tmp/merlin-snapshot-{os.getpid()}.sock")
    journal_path = socket_path.with_suffix(".journal.json")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    await asyncio.to_thread(journal_path.unlink, missing_ok=True)
    daemon = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: SnapshotBackend()))
    stream = None
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        client = LocalControlClient(socket_path=socket_path)

        async def create_session():
            return [event async for event in client.stream("/new")]

        stream = asyncio.create_task(create_session())
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=1)
        # The stream's terminal acknowledgment must still wait for durable replay.
        assert not stream.done()
        for _ in range(3):
            context = await asyncio.wait_for(client.conversation_context(), timeout=1)
            assert context.session_id == "session-1"
            assert await asyncio.wait_for(client.list_agents(), timeout=1)
        assert not journal_path.exists()
        release.set()
        assert len(await asyncio.wait_for(stream, timeout=2)) == 1
        journal = json.loads(journal_path.read_text())
        assert not journal["completed"]
        assert next(iter(journal["streams"].values()))["done"] is True
    finally:
        release.set()
        if stream is not None:
            await asyncio.gather(stream, return_exceptions=True)
        daemon.cancel()
        await asyncio.gather(daemon, return_exceptions=True)
        await asyncio.to_thread(socket_path.unlink, missing_ok=True)
        await asyncio.to_thread(journal_path.unlink, missing_ok=True)


@pytest.mark.asyncio
async def test_stream_can_resume_from_ordered_cursor() -> None:
    backend = Backend()
    socket_path = Path(f"/tmp/merlin-cursor-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        first = await _request(
            socket_path,
            {"method": "stream", "request_id": "stream-request", "prompt": "hello", "cursor": 0},
        )
        assert first["type"] == "event" and first["cursor"] == 1
        resumed = await _request(
            socket_path,
            {"method": "stream", "request_id": "stream-request", "prompt": "hello", "cursor": 1},
        )
        assert resumed == {"type": "end", "cursor": 1}
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_completed_request_replay_survives_daemon_restart(tmp_path: Path) -> None:
    del tmp_path
    socket_path = Path(f"/tmp/merlin-restart-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    await asyncio.to_thread(socket_path.with_suffix(".journal.json").unlink, missing_ok=True)
    first = Backend()
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: first))
    for _ in range(100):
        if await asyncio.to_thread(socket_path.exists):
            break
        await asyncio.sleep(0.01)
    request = {
        "method": "get_permission_mode",
        "request_id": "restart-safe-request",
        "args": [],
        "kwargs": {},
    }
    assert (await _request(socket_path, request))["value"] == "prompt"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    second = Backend()
    restarted = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: second))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        assert (await _request(socket_path, request))["value"] == "prompt"
        assert second.permission_reads == 0
    finally:
        restarted.cancel()
        with pytest.raises(asyncio.CancelledError):
            await restarted


@pytest.mark.asyncio
async def test_replace_foreground_waits_for_existing_session_cleanup(monkeypatch) -> None:
    from uuid import uuid4

    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()

    class ClosingBackend(Backend):
        async def close(self):
            cleanup_started.set()
            await allow_cleanup.wait()
            await super().close()

    backend = ClosingBackend()
    path = Path(f"/tmp/merlin-replace-{uuid4().hex[:12]}.sock")
    server = asyncio.create_task(serve(path, backend_factory=lambda **_: backend))
    owner = LocalControlClient(socket_path=path)
    observer = LocalControlClient(socket_path=path)
    starts = []

    async def start(socket_path):
        assert backend.closed
        assert not await asyncio.to_thread(socket_path.exists)
        starts.append(socket_path)
        return True

    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", start)
    close_owner = None
    replacement = None
    try:
        async with asyncio.timeout(5):
            for _ in range(500):
                if await asyncio.to_thread(path.exists):
                    break
                await asyncio.sleep(0.01)
            await owner.acquire_foreground_lease(close_notifications=True)

            async def quit_owner():
                await owner.wait_for_foreground_close()
                await owner.shutdown()

            close_owner = asyncio.create_task(quit_owner())
            replacement = asyncio.create_task(observer.replace_foreground())
            await cleanup_started.wait()
            assert not replacement.done()
            assert starts == []
            allow_cleanup.set()
            await replacement
            await close_owner
            await server
            assert starts == [path]
    finally:
        allow_cleanup.set()
        for task in (close_owner, replacement, server):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (close_owner, replacement, server) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_replace_foreground_completes_after_owner_releases_lease(monkeypatch) -> None:
    from uuid import uuid4

    backend = Backend()
    path = Path(f"/tmp/merlin-release-replace-{uuid4().hex[:12]}.sock")
    server = asyncio.create_task(serve(path, backend_factory=lambda **_: backend))
    owner = LocalControlClient(socket_path=path)
    observer = LocalControlClient(socket_path=path)
    starts = []

    async def start(socket_path):
        assert backend.closed
        starts.append(socket_path)
        return True

    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", start)
    close_owner = None
    try:
        async with asyncio.timeout(5):
            for _ in range(500):
                if await asyncio.to_thread(path.exists):
                    break
                await asyncio.sleep(0.01)
            await owner.acquire_foreground_lease(close_notifications=True)

            async def release_owner():
                await owner.wait_for_foreground_close()
                await owner.release_foreground_lease()

            close_owner = asyncio.create_task(release_owner())
            await observer.replace_foreground()
            await close_owner
            await server
            assert starts == [path]
    finally:
        for task in (close_owner, server):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (close_owner, server) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_legacy_foreground_owner_is_not_closed_without_notification_support() -> None:
    from uuid import uuid4

    path = Path(f"/tmp/merlin-legacy-{uuid4().hex[:12]}.sock")
    backend = Backend()
    server = asyncio.create_task(serve(path, backend_factory=lambda **_: backend))
    owner = LocalControlClient(socket_path=path)
    observer = LocalControlClient(socket_path=path)
    try:
        async with asyncio.timeout(5):
            for _ in range(500):
                if await asyncio.to_thread(path.exists):
                    break
                await asyncio.sleep(0.01)
            await owner.acquire_foreground_lease()
            with pytest.raises(RuntimeError, match="identify the existing Merlin window"):
                await observer.replace_foreground()
            assert not backend.closed
            await owner.shutdown()
            await server
    finally:
        if not server.done():
            server.cancel()
        await asyncio.gather(server, return_exceptions=True)


@pytest.mark.asyncio
async def test_foreground_close_monitor_survives_model_route_restart(monkeypatch) -> None:
    client = LocalControlClient(socket_path=Path("/tmp/merlin-monitor-test.sock"))
    old_reader = asyncio.StreamReader()
    new_reader = asyncio.StreamReader()
    client._lease_reader = old_reader
    client._lease_writer = object()
    client._close_notifications = True
    started = asyncio.Event()
    continue_restart = asyncio.Event()

    async def stop(*args, **kwargs):
        old_reader.feed_eof()
        started.set()
        await continue_restart.wait()
        return True

    async def close(writer):
        pass

    async def start(path):
        return True

    async def acquire(*, close_notifications=False):
        assert close_notifications
        client._lease_reader = new_reader

    monkeypatch.setattr("merlin_tui.local_control.stop_daemon", stop)
    monkeypatch.setattr("merlin_tui.local_control._close_writer", close)
    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", start)
    monkeypatch.setattr(client, "acquire_foreground_lease", acquire)
    monitor = asyncio.create_task(client.wait_for_foreground_close())
    restart = asyncio.create_task(client.apply_saved_model_routing())
    try:
        async with asyncio.timeout(5):
            await started.wait()
            await asyncio.sleep(0.05)
            assert not monitor.done()
            continue_restart.set()
            await restart
            new_reader.feed_data(b'{"type":"foreground_close"}\n')
            await monitor
    finally:
        for task in (monitor, restart):
            if not task.done():
                task.cancel()
        await asyncio.gather(monitor, restart, return_exceptions=True)


@pytest.mark.asyncio
async def test_release_foreground_lease_closes_owner_channel() -> None:
    class Writer:
        def __init__(self) -> None:
            self.closed = False
            self.waited = False

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            self.waited = True

    client = LocalControlClient(socket_path=Path("/tmp/merlin-release-test.sock"))
    reader = asyncio.StreamReader()
    writer = Writer()
    client._lease_reader = reader
    client._lease_writer = writer
    client._lease_id = "lease-1"

    await client.release_foreground_lease()

    assert writer.closed is True
    assert writer.waited is True
    assert client._lease_reader is None
    assert client._lease_writer is None
    assert client._lease_id is None


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="Legacy macOS socket discovery")
async def test_legacy_takeover_closes_real_connected_frontend(monkeypatch, tmp_path) -> None:
    from uuid import uuid4

    path = Path(f"/tmp/merlin-old-{uuid4().hex[:12]}.sock")
    backend = Backend()
    server = asyncio.create_task(serve(path, backend_factory=lambda **_: backend))
    script = tmp_path / "merlin"
    await asyncio.to_thread(
        script.write_text,
        """import asyncio, sys
from pathlib import Path
from merlin_tui.local_control import LocalControlClient
async def main():
    client = LocalControlClient(socket_path=Path(sys.argv[0]).with_suffix('.sock'))
    await client.acquire_foreground_lease()
    print('attached', flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await client.shutdown()
try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
""".replace("Path(sys.argv[0]).with_suffix('.sock')", f"Path({str(path)!r})"),
    )
    observer = LocalControlClient(socket_path=path)
    starts = []

    async def start(socket_path):
        assert backend.closed
        starts.append(socket_path)
        return True

    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", start)
    process = None
    try:
        async with asyncio.timeout(15):
            for _ in range(500):
                if await asyncio.to_thread(path.exists):
                    break
                await asyncio.sleep(0.01)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert await process.stdout.readline() == b"attached\n"
            await observer.replace_foreground()
            assert await process.wait() == 0
            await server
            assert starts == [path]
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            await process.wait()
        if not server.done():
            server.cancel()
        await asyncio.gather(server, return_exceptions=True)


def test_legacy_discovery_excludes_unrelated_processes(monkeypatch) -> None:
    from merlin_tui.local_control import _legacy_foreground_pid

    monkeypatch.setattr("merlin_tui.local_control._daemon_lock_owner", lambda _: 100)
    monkeypatch.setattr("merlin_tui.local_control.shutil.which", lambda _: "/usr/sbin/lsof")
    monkeypatch.setattr("merlin_tui.local_control.sys.platform", "darwin")
    uid = os.getuid()
    path = Path("/tmp/merlin-discovery.sock")
    inventory = f"""COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME
merlin-runtime 100 {uid} 20u unix 0xabc 0t0 {path}
Python 200 {uid} 6u unix 0xdef 0t0 ->0xabc
Python 300 {uid} 6u unix 0xghi 0t0 ->0xother
Python 400 {uid} 6u unix 0xjkl 0t0 ->0xabc
Python {os.getpid()} {uid} 6u unix 0xself 0t0 ->0xabc
Python 500 {uid + 1} 6u unix 0xuser 0t0 ->0xabc
"""

    def run(args, **kwargs):
        if args[0] == "/usr/sbin/lsof":
            return subprocess.CompletedProcess(args, 0, inventory, "")
        pid = int(args[2])
        command = (
            "/usr/bin/python /repo/.venv/bin/merlin"
            if pid == 200
            else "/usr/bin/python unrelated.py"
        )
        return subprocess.CompletedProcess(args, 0, f"{uid} {command}", "")

    monkeypatch.setattr("merlin_tui.local_control.subprocess.run", run)
    assert _legacy_foreground_pid(path) == 200


@pytest.mark.asyncio
async def test_replace_foreground_recovers_when_old_session_already_exited(monkeypatch) -> None:
    from uuid import uuid4

    path = Path(f"/tmp/merlin-exited-{uuid4().hex[:12]}.sock")
    starts = []

    async def start(socket_path):
        starts.append(socket_path)
        return True

    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", start)
    client = LocalControlClient(socket_path=path)
    await client.replace_foreground()
    assert starts == [path]


@pytest.mark.asyncio
async def test_unresponsive_locked_daemon_does_not_spawn_competing_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def unreachable(path: Path) -> bool:
        return False

    def unexpected_spawn():
        pytest.fail("A competing daemon must not be spawned while the lifetime lock is held")

    monkeypatch.setattr("merlin_tui.local_control._ping", unreachable)
    monkeypatch.setattr("merlin_tui.local_control._daemon_lock_released", lambda path: False)
    monkeypatch.setattr("merlin_tui.local_control._spawn_daemon", unexpected_spawn)

    with pytest.raises(TimeoutError, match="still starting or closing"):
        await ensure_daemon(tmp_path / "merlin.sock", startup_seconds=0.02)


@pytest.mark.asyncio
async def test_retained_worker_transcript_is_available_over_local_control(tmp_path):
    class TranscriptBackend(Backend):
        calls = []

        async def get_agent_transcript(self, assignment_id):
            self.calls.append(assignment_id)
            return ("assistant: retained output", "tool: target response")

    backend = TranscriptBackend()
    socket_path = Path(f"/tmp/merlin-transcript-rpc-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        client = LocalControlClient(socket_path=socket_path)
        assert await client.get_agent_transcript("assignment-7") == (
            "assistant: retained output",
            "tool: target response",
        )
        assert backend.calls == ["assignment-7"]
        await client.close()
        assert not backend.closed
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_selected_campaign_export_is_available_over_local_control(tmp_path):
    from merlin_tui.backend import ReportView

    class ExportBackend(Backend):
        calls = []

        async def export_campaign_report(self, campaign_id, source_output, format):
            self.calls.append((campaign_id, source_output, format))
            return ReportView(campaign_id, format, "/saved/selected.json", "ready", 2)

    backend = ExportBackend()
    socket_path = Path(f"/tmp/merlin-report-rpc-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        client = LocalControlClient(socket_path=socket_path)
        result = await client.export_campaign_report(
            "campaign/selected", "/saved/selected.html", "json"
        )
        assert result.output == "/saved/selected.json"
        assert result.run == "campaign/selected"
        assert backend.calls == [("campaign/selected", "/saved/selected.html", "json")]
        await client.close()
        assert not backend.closed
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_new_session_recovers_missing_socket_before_sending_once(monkeypatch, tmp_path):
    calls = []
    frames = []

    class Writer:
        def write(self, data):
            frames.append(json.loads(data))

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connect(*args, **kwargs):
        calls.append("connect")
        if calls.count("connect") == 1:
            raise FileNotFoundError(2, "No such file or directory")
        reader = asyncio.StreamReader()
        for frame in [
            {"type": "event", "cursor": 1, "value": {"kind": "status", "text": "new session"}},
            {"type": "end"},
        ]:
            reader.feed_data((json.dumps(frame) + "\n").encode())
        return reader, Writer()

    async def ensure(path):
        calls.append("ensure")
        return True

    monkeypatch.setattr(asyncio, "open_unix_connection", connect)
    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", ensure)
    client = LocalControlClient(socket_path=tmp_path / "missing.sock")
    events = [event async for event in client.stream("/new")]
    assert calls == ["connect", "ensure", "connect"]
    assert [frame["prompt"] for frame in frames] == ["/new"]
    assert events[-1] == StreamEvent("status", "new session")
    assert "Reconnecting" in events[0].text


@pytest.mark.asyncio
async def test_new_connection_recovery_restores_foreground_lease(monkeypatch, tmp_path):
    calls = []

    class Writer:
        def close(self):
            calls.append("closed")

        async def wait_closed(self):
            pass

    client = LocalControlClient(socket_path=tmp_path / "missing.sock")
    client._lease_writer = Writer()
    client._lease_id = "old-lease"
    client._close_notifications = True

    async def ensure(path):
        assert client._reconnecting
        calls.append("ensure")
        return True

    async def acquire(**kwargs):
        assert kwargs == {"close_notifications": True}
        assert client._lease_id is None
        calls.append("lease")
        client._lease_id = "new-lease"

    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", ensure)
    monkeypatch.setattr(client, "acquire_foreground_lease", acquire)
    await client._recover_new_session_connection()
    assert calls == ["ensure", "closed", "lease"]
    assert client._lease_id == "new-lease"
    assert not client._reconnecting


@pytest.mark.asyncio
async def test_new_session_does_not_restart_daemon_after_possible_delivery(monkeypatch, tmp_path):
    attempts = 0
    sent = []

    class Writer:
        def write(self, data):
            sent.append(json.loads(data))

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            raise FileNotFoundError(2, "No such file or directory")
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return reader, Writer()

    async def ensure(path):
        raise AssertionError("must not restart after a possibly delivered /new")

    monkeypatch.setattr(asyncio, "open_unix_connection", connect)
    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", ensure)
    client = LocalControlClient(socket_path=tmp_path / "missing.sock")
    with pytest.raises(FileNotFoundError):
        _ = [event async for event in client.stream("/new")]
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_ensure_daemon_waits_for_existing_starter_without_spawning(monkeypatch, tmp_path):
    pings = 0
    progress = []

    async def ping(path):
        nonlocal pings
        pings += 1
        return pings >= 3

    def unexpected_spawn():
        raise AssertionError("existing starter must retain exclusive daemon ownership")

    monkeypatch.setattr("merlin_tui.local_control._ping", ping)
    monkeypatch.setattr("merlin_tui.local_control._daemon_lock_released", lambda path: False)
    monkeypatch.setattr("merlin_tui.local_control._spawn_daemon", unexpected_spawn)
    result = await ensure_daemon(
        tmp_path / "starting.sock", startup_seconds=1, progress=progress.append
    )
    assert result is False
    assert len(progress) == 2


@pytest.mark.asyncio
async def test_ensure_daemon_waits_for_shutdown_lock_release_before_starting(monkeypatch, tmp_path):
    lock_checks = 0
    spawned = False

    def released(path):
        nonlocal lock_checks
        lock_checks += 1
        return lock_checks >= 3

    async def ping(path):
        return spawned

    class Process:
        def poll(self):
            return None

    def spawn():
        nonlocal spawned
        assert lock_checks >= 3
        spawned = True
        return Process()

    monkeypatch.setattr("merlin_tui.local_control._ping", ping)
    monkeypatch.setattr("merlin_tui.local_control._daemon_lock_released", released)
    monkeypatch.setattr("merlin_tui.local_control._spawn_daemon", spawn)
    assert await ensure_daemon(tmp_path / "closing.sock", startup_seconds=1)


@pytest.mark.asyncio
async def test_new_session_recovers_while_real_daemon_holds_startup_lock(monkeypatch, tmp_path):
    recovering = asyncio.Event()
    release = asyncio.Event()
    prompts = []

    class StartingBackend(Backend):
        async def recover_background(self):
            recovering.set()
            await release.wait()

        async def stream(self, prompt):
            prompts.append(prompt)
            yield StreamEvent("status", "new session")

    def unexpected_spawn():
        raise AssertionError("must wait for the daemon already holding the lock")

    monkeypatch.setattr("merlin_tui.local_control._spawn_daemon", unexpected_spawn)
    from uuid import uuid4

    path = Path(f"/tmp/merlin-start-{uuid4().hex[:12]}.sock")
    server = asyncio.create_task(serve(path, backend_factory=lambda **kwargs: StartingBackend()))
    try:
        await asyncio.wait_for(recovering.wait(), 2)
        assert not await asyncio.to_thread(path.exists)
        assert not _daemon_lock_released(path)
        asyncio.get_running_loop().call_later(0.1, release.set)
        client = LocalControlClient(socket_path=path)
        events = [event async for event in client.stream("/new")]
        assert events[-1] == StreamEvent("status", "new session")
        assert prompts == ["/new"]
        assert not client._owns_daemon
    finally:
        release.set()
        server.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server


@pytest.mark.asyncio
async def test_restart_foreground_waits_for_lease_eof_and_backend_cleanup(monkeypatch) -> None:
    from uuid import uuid4

    from merlin_tui.local_control import restart_daemon

    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    owner_notified = asyncio.Event()

    class RestartBackend(Backend):
        async def close(self):
            cleanup_started.set()
            await allow_cleanup.wait()
            await super().close()

    backend = RestartBackend()
    path = Path(f"/tmp/merlin-restart-owner-{uuid4().hex[:12]}.sock")
    server = asyncio.create_task(serve(path, backend_factory=lambda **_: backend))
    owner = LocalControlClient(socket_path=path)
    starts = []

    async def start(socket_path):
        assert owner_notified.is_set()
        assert backend.closed
        assert not socket_path.exists()
        assert _daemon_lock_released(socket_path)
        starts.append(socket_path)
        return True

    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", start)
    quit_owner = None
    restarting = None
    try:
        async with asyncio.timeout(5):
            for _ in range(500):
                if await asyncio.to_thread(path.exists):
                    break
                await asyncio.sleep(0.01)
            await owner.acquire_foreground_lease(close_notifications=True)

            async def release_on_notification():
                await owner.wait_for_foreground_close()
                owner_notified.set()
                assert owner._lease_writer is not None
                await owner.release_foreground_lease()

            quit_owner = asyncio.create_task(release_on_notification())
            restarting = asyncio.create_task(restart_daemon(path))
            await cleanup_started.wait()
            assert owner_notified.is_set()
            assert not restarting.done()
            assert starts == []
            allow_cleanup.set()
            await restarting
            await quit_owner
            await server
            assert starts == [path]
    finally:
        allow_cleanup.set()
        tasks = [task for task in (quit_owner, restarting, server) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        path.with_suffix(".journal.json").unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_foreground_disconnect_explains_how_to_resume() -> None:
    client = LocalControlClient(socket_path=Path("/tmp/merlin-disconnect-test.sock"))
    reader = asyncio.StreamReader()
    reader.feed_eof()
    client._lease_reader = reader

    with pytest.raises(ConnectionError, match="Run ./merlin.*Resume last session"):
        await client.wait_for_foreground_close()


@pytest.mark.asyncio
async def test_restart_idle_daemon_cleans_resources_before_start(monkeypatch) -> None:
    from uuid import uuid4

    from merlin_tui.local_control import restart_daemon

    backend = Backend()
    path = Path(f"/tmp/merlin-restart-idle-{uuid4().hex[:12]}.sock")
    server = asyncio.create_task(serve(path, backend_factory=lambda **_: backend))
    starts = []

    async def start(socket_path):
        assert backend.closed
        assert not socket_path.exists()
        assert _daemon_lock_released(socket_path)
        starts.append(socket_path)
        return True

    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", start)
    try:
        async with asyncio.timeout(5):
            for _ in range(500):
                if await asyncio.to_thread(path.exists):
                    break
                await asyncio.sleep(0.01)
            await restart_daemon(path)
            await server
        assert starts == [path]
    finally:
        if not server.done():
            server.cancel()
        await asyncio.gather(server, return_exceptions=True)
        path.with_suffix(".journal.json").unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_restarted_daemon_stops_when_no_foreground_attaches(monkeypatch) -> None:
    from uuid import uuid4

    from merlin_tui.local_control import _arm_unclaimed_shutdown

    monkeypatch.setattr("merlin_tui.local_control._FOREGROUND_ATTACH_GRACE_SECONDS", 0.05)
    backend = Backend()
    path = Path(f"/tmp/merlin-restart-unclaimed-{uuid4().hex[:12]}.sock")
    server = asyncio.create_task(serve(path, backend_factory=lambda **_: backend))
    try:
        for _ in range(100):
            if await asyncio.to_thread(path.exists):
                break
            await asyncio.sleep(0.01)
        await _arm_unclaimed_shutdown(path)
        await asyncio.wait_for(server, timeout=1)
        assert backend.closed
        assert not await asyncio.to_thread(path.exists)
    finally:
        if not server.done():
            server.cancel()
        await asyncio.gather(server, return_exceptions=True)


@pytest.mark.asyncio
async def test_foreground_claim_cancels_restart_idle_shutdown(monkeypatch) -> None:
    from uuid import uuid4

    from merlin_tui.local_control import _arm_unclaimed_shutdown

    monkeypatch.setattr("merlin_tui.local_control._FOREGROUND_ATTACH_GRACE_SECONDS", 0.05)
    backend = Backend()
    path = Path(f"/tmp/merlin-restart-claimed-{uuid4().hex[:12]}.sock")
    server = asyncio.create_task(serve(path, backend_factory=lambda **_: backend))
    client = LocalControlClient(socket_path=path)
    try:
        for _ in range(100):
            if await asyncio.to_thread(path.exists):
                break
            await asyncio.sleep(0.01)
        await _arm_unclaimed_shutdown(path)
        await client.acquire_foreground_lease()
        await asyncio.sleep(0.1)
        assert not server.done()
        assert not backend.closed
        await client.shutdown()
        await server
    finally:
        if not server.done():
            server.cancel()
        await asyncio.gather(server, return_exceptions=True)


@pytest.mark.asyncio
async def test_restart_absent_daemon_starts_once(monkeypatch, tmp_path) -> None:
    from merlin_tui.local_control import restart_daemon

    path = tmp_path / "absent.sock"
    starts = []

    async def start(socket_path):
        starts.append(socket_path)
        return True

    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", start)

    await restart_daemon(path)

    assert starts == [path]


@pytest.mark.asyncio
async def test_restart_unreachable_lock_owner_never_force_kills_or_starts(monkeypatch) -> None:
    from uuid import uuid4

    from merlin_tui.local_control import restart_daemon

    path = Path(f"/tmp/merlin-restart-unreachable-{uuid4().hex[:12]}.sock")
    lock = _acquire_daemon_lock(path)
    monkeypatch.setattr("merlin_tui.local_control._configured_daemon_timeout", lambda *args: 0.05)

    async def unexpected_start(*args, **kwargs):
        pytest.fail("a daemon whose cleanup is unconfirmed must not be replaced")

    original_kill = os.kill

    def unexpected_signal(pid, sig):
        if sig != 0:
            pytest.fail("restart must not forcibly kill the locked daemon")
        original_kill(pid, sig)

    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", unexpected_start)
    monkeypatch.setattr("merlin_tui.local_control.os.kill", unexpected_signal)
    try:
        with pytest.raises(RuntimeError):
            await restart_daemon(path)
        assert not _daemon_lock_released(path)
    finally:
        lock.close()


@pytest.mark.asyncio
async def test_restart_owner_timeout_preserves_session_and_does_not_start(monkeypatch) -> None:
    from uuid import uuid4

    from merlin_tui.local_control import restart_daemon

    path = Path(f"/tmp/merlin-restart-timeout-{uuid4().hex[:12]}.sock")
    backend = Backend()
    server = asyncio.create_task(serve(path, backend_factory=lambda **_: backend))
    owner = LocalControlClient(socket_path=path)
    monkeypatch.setattr("merlin_tui.local_control._configured_daemon_timeout", lambda *args: 0.1)

    async def unexpected_start(*args, **kwargs):
        pytest.fail("restart must wait for the foreground owner to release its lease")

    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", unexpected_start)
    try:
        async with asyncio.timeout(5):
            for _ in range(500):
                if await asyncio.to_thread(path.exists):
                    break
                await asyncio.sleep(0.01)
            await owner.acquire_foreground_lease(close_notifications=True)
            with pytest.raises(TimeoutError, match="existing session is still closing"):
                await restart_daemon(path)
            await owner.wait_for_foreground_close()
            assert not backend.closed
            assert not _daemon_lock_released(path)
            await owner.shutdown()
            await server
    finally:
        if not server.done():
            server.cancel()
        await asyncio.gather(server, return_exceptions=True)
        path.with_suffix(".journal.json").unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_restart_connected_silent_daemon_times_out_without_start_or_signal(
    monkeypatch,
) -> None:
    from uuid import uuid4

    from merlin_tui.local_control import restart_daemon

    path = Path(f"/tmp/merlin-restart-silent-{uuid4().hex[:12]}.sock")
    connected = asyncio.Event()
    release = asyncio.Event()
    peer_tasks = []

    async def silent_peer(reader, writer):
        peer_tasks.append(asyncio.current_task())
        try:
            request = json.loads(await reader.readline())
            assert request == {"method": "close_foreground"}
            connected.set()
            await release.wait()
        finally:
            await _close_writer(writer)

    async def unexpected_start(*args, **kwargs):
        pytest.fail("restart must not start before an existing daemon acknowledges shutdown")

    def unexpected_signal(*args, **kwargs):
        pytest.fail("a silent daemon must not be signalled by restart")

    monkeypatch.setattr("merlin_tui.local_control._configured_daemon_timeout", lambda *args: 0.02)
    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", unexpected_start)
    monkeypatch.setattr("merlin_tui.local_control.os.kill", unexpected_signal)
    server = await asyncio.start_unix_server(silent_peer, path)
    try:
        async with asyncio.timeout(5):
            with pytest.raises(TimeoutError, match="did not respond"):
                await restart_daemon(path)
            assert connected.is_set()
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*peer_tasks)
        await asyncio.to_thread(path.unlink, missing_ok=True)


@pytest.mark.asyncio
async def test_restart_resumes_suspended_foreground_for_normal_cleanup(
    monkeypatch, tmp_path
) -> None:
    import signal
    from uuid import uuid4

    from merlin_tui.local_control import restart_daemon

    path = Path(f"/tmp/merlin-restart-stopped-{uuid4().hex[:12]}.sock")
    backend = Backend()
    server = asyncio.create_task(serve(path, backend_factory=lambda **_: backend))
    script = tmp_path / "suspended_frontend.py"
    await asyncio.to_thread(
        script.write_text,
        f"""import asyncio, os, signal
from pathlib import Path
from merlin_tui.local_control import LocalControlClient
async def main():
    client = LocalControlClient(socket_path=Path({str(path)!r}))
    await client.acquire_foreground_lease(close_notifications=True)
    print('attached', flush=True)
    os.kill(os.getpid(), signal.SIGSTOP)
    await client.wait_for_foreground_close()
    await client.shutdown()
asyncio.run(main())
""",
    )
    starts = []

    async def start(socket_path):
        assert backend.closed
        assert not await asyncio.to_thread(socket_path.exists)
        assert await asyncio.to_thread(_daemon_lock_released, socket_path)
        starts.append(socket_path)
        return True

    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", start)
    process = None
    try:
        async with asyncio.timeout(15):
            for _ in range(500):
                if await asyncio.to_thread(path.exists):
                    break
                await asyncio.sleep(0.01)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert await process.stdout.readline() == b"attached\n"
            # Wait for the child's SIGSTOP, rather than racing its stdout flush.
            for _ in range(500):
                status_process = await asyncio.create_subprocess_exec(
                    "ps",
                    "-o",
                    "stat=",
                    "-p",
                    str(process.pid),
                    stdout=asyncio.subprocess.PIPE,
                )
                status, _ = await status_process.communicate()
                if status.strip().startswith(b"T"):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("foreground test child did not enter the suspended state")
            monkeypatch.setattr(
                "merlin_tui.local_control._legacy_foreground_pid", lambda socket_path: process.pid
            )
            await restart_daemon(path)
            assert await process.wait() == 0
            await server
            assert starts == [path]
    finally:
        if process is not None and process.returncode is None:
            os.kill(process.pid, signal.SIGCONT)
            process.terminate()
            await process.wait()
        if not server.done():
            server.cancel()
        await asyncio.gather(server, return_exceptions=True)
        await asyncio.to_thread(path.with_suffix(".journal.json").unlink, missing_ok=True)


def test_restart_resume_leaves_running_foreground_unsignalled(monkeypatch) -> None:
    from merlin_tui.local_control import _resume_stopped_foreground

    path = Path("/tmp/merlin-running-resume-test.sock")
    monkeypatch.setattr("merlin_tui.local_control._legacy_foreground_pid", lambda _: 123)
    monkeypatch.setattr(
        "merlin_tui.local_control.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="S+\n"),
    )
    monkeypatch.setattr(
        "merlin_tui.local_control.os.kill",
        lambda *args: pytest.fail("running foreground must not be signalled"),
    )

    _resume_stopped_foreground(path)


def test_restart_resume_skips_unidentified_foreground(monkeypatch) -> None:
    from merlin_tui.local_control import _resume_stopped_foreground

    def unidentified(_):
        raise RuntimeError("Cannot identify the existing Merlin window")

    monkeypatch.setattr("merlin_tui.local_control._legacy_foreground_pid", unidentified)
    monkeypatch.setattr(
        "merlin_tui.local_control.os.kill",
        lambda *args: pytest.fail("unidentified processes must not be signalled"),
    )

    _resume_stopped_foreground(Path("/tmp/merlin-unidentified-resume-test.sock"))


@pytest.mark.asyncio
async def test_restart_recovers_verified_real_orphan_before_start(monkeypatch) -> None:
    from uuid import uuid4

    from merlin_tui.local_control import _daemon_lock_owner, restart_daemon

    path = Path(f"/tmp/merlin-restart-orphan-{uuid4().hex[:12]}.sock")
    orphan = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; from pathlib import Path; "
        "from merlin_tui.local_control import _acquire_daemon_lock; "
        f"lock = _acquire_daemon_lock(Path({str(path)!r})); "
        "print('locked', flush=True); time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    starts = []

    def verify_fixture_owner(socket_path):
        return socket_path == path and _daemon_lock_owner(path) == orphan.pid

    async def start(socket_path):
        assert socket_path == path
        assert await asyncio.to_thread(_daemon_lock_released, path)
        assert await orphan.wait() is not None
        starts.append(socket_path)
        return True

    monkeypatch.setattr("merlin_tui.local_control._verified_daemon_owner", verify_fixture_owner)
    monkeypatch.setattr("merlin_tui.local_control.ensure_daemon", start)
    try:
        async with asyncio.timeout(5):
            assert await orphan.stdout.readline() == b"locked\n"
            assert await asyncio.to_thread(_daemon_lock_owner, path) == orphan.pid
            await restart_daemon(path)
            assert starts == [path]
    finally:
        if orphan.returncode is None:
            orphan.kill()
            await orphan.wait()
        await asyncio.to_thread(path.with_suffix(".lock").unlink, missing_ok=True)


@pytest.mark.parametrize(
    ("uid_offset", "command", "is_self", "expected"),
    [
        (0, "/usr/bin/python -m merlin_tui.local_control daemon", False, True),
        (0, "/Applications/Merlin/merlin-runtime daemon", False, True),
        (1, "/usr/bin/python -m merlin_tui.local_control daemon", False, False),
        (0, "/usr/bin/python unrelated.py", False, False),
        (0, "/usr/bin/python -m merlin_tui.local_control daemon", True, False),
    ],
)
def test_restart_orphan_verifies_daemon_identity(
    monkeypatch, uid_offset, command, is_self, expected
) -> None:
    from merlin_tui.local_control import _verified_daemon_owner

    pid = os.getpid() if is_self else os.getpid() + 100000
    monkeypatch.setattr("merlin_tui.local_control._daemon_lock_owner", lambda _: pid)
    monkeypatch.setattr(
        "merlin_tui.local_control.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, stdout=f"{os.getuid() + uid_offset} {command}\n"
        ),
    )

    assert _verified_daemon_owner(Path("/tmp/merlin-orphan-identity.sock")) is expected


def test_restart_orphan_rechecks_lock_owner_after_process_identity(monkeypatch) -> None:
    from merlin_tui.local_control import _verified_daemon_owner

    owners = iter([os.getpid() + 100000, os.getpid() + 100001])
    monkeypatch.setattr("merlin_tui.local_control._daemon_lock_owner", lambda _: next(owners))
    monkeypatch.setattr(
        "merlin_tui.local_control.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, stdout=f"{os.getuid()} /usr/bin/python -m merlin_tui.local_control daemon\n"
        ),
    )

    assert not _verified_daemon_owner(Path("/tmp/merlin-orphan-changed-identity.sock"))


@pytest.mark.asyncio
async def test_ping_accepts_slow_live_daemon_response() -> None:
    from uuid import uuid4

    from merlin_tui.local_control import _ping

    path = Path(f"/tmp/merlin-slow-ping-{uuid4().hex[:12]}.sock")
    peers = []

    async def slow_peer(reader, writer):
        peers.append(asyncio.current_task())
        try:
            assert json.loads(await reader.readline()) == {"method": "ping"}
            await asyncio.sleep(0.6)
            writer.write(
                (
                    json.dumps({"type": "result", "value": "pong", "protocol": PROTOCOL_VERSION})
                    + "\n"
                ).encode()
            )
            await writer.drain()
        finally:
            await _close_writer(writer)

    server = await asyncio.start_unix_server(slow_peer, path)
    try:
        assert await _ping(path)
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.gather(*peers)
        await asyncio.to_thread(path.unlink, missing_ok=True)


@pytest.mark.asyncio
async def test_ping_closes_connection_after_probe_timeout(monkeypatch) -> None:
    from merlin_tui.local_control import _ping

    closed = []

    class Writer:
        def write(self, value):
            assert json.loads(value) == {"method": "ping"}

        async def drain(self):
            return None

    writer = Writer()

    async def connect(*args, **kwargs):
        return object(), writer

    async def timeout_probe(awaitable, **kwargs):
        assert kwargs["timeout"] == 5.0
        awaitable.close()
        raise TimeoutError

    async def close_writer(value):
        closed.append(value)

    monkeypatch.setattr("merlin_tui.local_control.asyncio.open_unix_connection", connect)
    monkeypatch.setattr("merlin_tui.local_control.asyncio.wait_for", timeout_probe)
    monkeypatch.setattr("merlin_tui.local_control._close_writer", close_writer)

    assert not await _ping(Path("/tmp/merlin-ping-timeout.sock"))
    assert closed == [writer]
