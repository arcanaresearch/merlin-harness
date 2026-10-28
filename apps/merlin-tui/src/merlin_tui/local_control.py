from __future__ import annotations

import asyncio
import base64
import contextvars
import errno
import fcntl
import importlib.metadata
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum, StrEnum
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO
from uuid import uuid4

from merlin_contracts import ToolContext, ToolSpec
from merlin_permissions import PermissionDecision

from .backend import StreamEvent
from .wire_events import (
    CLIENT_EVENT_SCHEMA_VERSION,
    ClientEventDecodeError,
    decode_client_event,
    encode_client_event,
)


def _wire_client_event(value: Mapping[str, object]) -> dict[str, object]:
    """Wrap journal presentations without changing retention or cursor identity."""
    if "schema_version" not in value:
        value = {
            **value,
            "schema_version": CLIENT_EVENT_SCHEMA_VERSION,
            "presentation": {"kind": value.get("kind"), "text": value.get("text")},
        }
    return encode_client_event(decode_client_event(value))


def _default_socket_path() -> Path:
    configured = os.environ.get("MERLIN_SOCKET_PATH")
    if configured:
        return Path(configured)
    candidate = Path.home() / ".merlin" / "daemon.sock"
    if len(os.fsencode(candidate)) <= 96:
        return candidate
    identity = sha256(os.fsencode(Path.home())).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"merlin-{os.getuid()}-{identity}" / "daemon.sock"


_SOCKET = _default_socket_path()
_DAEMON_LOG = Path.home() / ".merlin" / "logs" / "daemon.log"
PROTOCOL_VERSION = "merlin.local-control.v1"
_MAX_FRAME_BYTES = 1_000_000
_MAX_CHUNK_BYTES = 700_000
_MAX_MESSAGE_BYTES = 64_000_000
_MAX_CACHED_REQUESTS = 256
_MAX_COMPLETED_REPLAY_BYTES = 16_000_000
_EXPIRED_COMPLETED_REPLAY = {
    "type": "error",
    "error": (
        "The completed command result is no longer available for replay; "
        "refresh Merlin state instead of repeating the command."
    ),
}
# These snapshots can be queried again safely after reconnecting. Persisting
# every UI poll serializes reads behind command replay writes and accumulates
# hundreds of copies of large agent/history responses in the journal.
_SNAPSHOT_METHODS = frozenset(
    {
        "get_runtime_status",
        "get_agent_transcript",
        "get_campaign_lead_log",
        "list_agents",
        "list_history",
        "list_resume_sessions",
        "list_resume_session_page",
        "conversation_context",
        "conversation_history",
        "campaign_status",
        "list_campaigns",
        "list_active_schedules",
        "list_schedules",
    }
)
_READ_ONLY_METHODS = _SNAPSHOT_METHODS | frozenset(
    {
        "list_packs",
        "list_targets",
        "list_skills",
        "list_mcp_servers",
        "list_findings",
        "list_reports",
        "get_permission_mode",
        "list_permission_rules",
        "list_permission_history",
        "list_promotion_candidates",
        "list_promotion_history",
        "list_memory_records",
        "list_browser_contexts",
        "list_logs",
        "list_lessons",
        "export_safe_config",
    }
)


class LocalStorageExhaustedError(RuntimeError):
    """Local replay durability failed; new foreground mutations are fenced."""

    code = "local_storage_exhausted"

    def __init__(self) -> None:
        super().__init__("Local storage is exhausted; new foreground mutations are disabled")

    def response(self) -> dict[str, object]:
        return {
            "type": "error",
            "error": str(self),
            "code": self.code,
            "failure_class": "infrastructure",
            "infrastructure_failure_category": "storage_exhausted",
        }


_BATCHABLE_STREAM_KINDS = frozenset({"status", "content", "thinking"})
_STARTER_EXIT_GRACE_SECONDS = 0.5
_RECOVERY_WATCHDOG_SECONDS = 5.0
_IDLE_SHUTDOWN_CONFIRM_SECONDS = 0.25
_FOREGROUND_HEARTBEAT_SECONDS = 1.0
# Provider startup and durable-store reconciliation can briefly starve the Python
# event loop on slower workspaces.  Keep this comfortably above those bounded
# stalls while retaining renewable heartbeats to detect an abandoned frontend.
_FOREGROUND_LEASE_TIMEOUT_SECONDS = 60.0
_FOREGROUND_ATTACH_GRACE_SECONDS = 10.0
_DAEMON_HEARTBEAT_SECONDS = 5.0
_PROCESS_LIFETIME_LOCKS: list[BinaryIO] = []


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    protocol: str
    distribution_version: str
    executable: str
    module_root: str
    mode: str


class DaemonCompatibility(StrEnum):
    UNREACHABLE = "unreachable"
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class DaemonCompatibilityProbe:
    status: DaemonCompatibility
    identity: RuntimeIdentity | None = None
    reason: str | None = None


class IncompatibleDaemonError(RuntimeError):
    """A reachable daemon belongs to a different Merlin runtime."""


def _capture_runtime_identity() -> RuntimeIdentity:
    return RuntimeIdentity(
        protocol=PROTOCOL_VERSION,
        distribution_version=importlib.metadata.version("merlin-tui"),
        executable=str(Path(sys.executable).resolve()),
        module_root=str(Path(__file__).resolve().parent),
        mode="frozen" if getattr(sys, "frozen", False) else "source",
    )


_RUNTIME_IDENTITY = _capture_runtime_identity()
_ALLOWED = {
    "configure_provider",
    "configure_model_endpoint",
    "set_model",
    "set_model_route",
    "set_model_routing",
    "get_runtime_status",
    "list_packs",
    "list_targets",
    "list_skills",
    "list_mcp_servers",
    "configure_remote_mcp",
    "list_agents",
    "get_agent_transcript",
    "get_campaign_lead_log",
    "list_active_schedules",
    "list_schedules",
    "list_findings",
    "list_reports",
    "export_campaign_report",
    "get_permission_mode",
    "set_permission_mode",
    "list_permission_rules",
    "list_permission_history",
    "list_promotion_candidates",
    "list_promotion_history",
    "list_memory_records",
    "list_browser_contexts",
    "takeover_browser_context",
    "run_doctor",
    "list_logs",
    "list_history",
    "list_resume_sessions",
    "list_resume_session_page",
    "conversation_context",
    "conversation_history",
    "export_safe_config",
    "interrupt",
    "steer",
    "campaign_status",
    "execute_campaign",
    "decide_campaign_saturation",
    "guide_campaign",
    "list_campaigns",
    "cancel_campaign",
    "pause_campaign",
    "resume_campaign",
    "cancel_task",
    "list_lessons",
}


@dataclass(frozen=True, slots=True)
class LocalControlTuning:
    """Validated replay/IPC bounds; these do not limit campaign work or provider output."""

    max_stream_events: int = 65_536
    stream_journal_batch: int = 512
    request_timeout_seconds: float = 30
    config_reload_poll_seconds: float = 1

    def __post_init__(self) -> None:
        if not 256 <= self.max_stream_events <= 1_000_000:
            raise ValueError("max_stream_events must be between 256 and 1000000")
        if not 1 <= self.stream_journal_batch <= 4_096:
            raise ValueError("stream_journal_batch must be between 1 and 4096")
        if not 1 <= self.request_timeout_seconds <= 300:
            raise ValueError("request_timeout_seconds must be between 1 and 300")
        if not 0.1 <= self.config_reload_poll_seconds <= 60:
            raise ValueError("config_reload_poll_seconds must be between 0.1 and 60")


_DEFAULT_LOCAL_CONTROL_TUNING = LocalControlTuning()


def configured_local_control_tuning() -> LocalControlTuning:
    from merlin_config import load_config

    runtime = load_config().runtime
    return LocalControlTuning(
        max_stream_events=runtime.local_control_max_stream_events,
        stream_journal_batch=runtime.local_control_stream_journal_batch,
        request_timeout_seconds=runtime.local_control_request_timeout_seconds,
        config_reload_poll_seconds=runtime.config_reload_poll_seconds,
    )


def _acquire_daemon_lock(socket_path: Path) -> BinaryIO:
    """Own a process-lifetime lock before inspecting or replacing the daemon socket."""
    lock_path = socket_path.with_suffix(".lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("Merlin daemon is already starting or running") from None
    # Record the owning pid so a later `stop --force` can terminate an orphaned
    # runtime that still holds the lock but no longer serves the socket.
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()).encode())
    handle.flush()
    return handle


def _daemon_lock_owner(socket_path: Path) -> int | None:
    """Return the live pid recorded in the daemon lock file, if any."""
    try:
        pid = int(socket_path.with_suffix(".lock").read_text().strip())
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


class _Channel:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self.approval_lock = asyncio.Lock()

    async def send(self, value: object) -> None:
        frame = (json.dumps(_json(value), separators=(",", ":")) + "\n").encode()
        if len(frame) <= _MAX_FRAME_BYTES:
            self.writer.write(frame)
            await self.writer.drain()
            return
        if len(frame) > _MAX_MESSAGE_BYTES:
            raise ValueError("local-control message exceeds configured bound")
        message_id = str(uuid4())
        total = (len(frame) + _MAX_CHUNK_BYTES - 1) // _MAX_CHUNK_BYTES
        for index, offset in enumerate(range(0, len(frame), _MAX_CHUNK_BYTES)):
            chunk = frame[offset : offset + _MAX_CHUNK_BYTES]
            part = (
                json.dumps(
                    {
                        "type": "multipart",
                        "message_id": message_id,
                        "index": index,
                        "total": total,
                        "data": base64.b64encode(chunk).decode("ascii"),
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            if len(part) > _MAX_FRAME_BYTES:
                raise ValueError("local-control multipart frame exceeds configured bound")
            self.writer.write(part)
            await self.writer.drain()

    async def receive(self, *, deadline_seconds: float | None = None) -> dict[str, Any]:
        async def read_frame() -> bytes:
            read = self.reader.readline()
            line = (
                await asyncio.wait_for(read, deadline_seconds)
                if deadline_seconds is not None
                else await read
            )
            if not line:
                raise ConnectionError("local-control client disconnected")
            if len(line) > _MAX_FRAME_BYTES:
                raise ValueError("local-control frame exceeds configured bound")
            return line

        line = await read_frame()
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("invalid local-control frame")
        if value.get("type") == "multipart":
            message_id = value.get("message_id")
            total = value.get("total")
            max_parts = (_MAX_MESSAGE_BYTES + _MAX_CHUNK_BYTES - 1) // _MAX_CHUNK_BYTES
            if (
                not isinstance(message_id, str)
                or not isinstance(total, int)
                or not 2 <= total <= max_parts
            ):
                raise ValueError("invalid local-control multipart frame")
            payload = bytearray()
            for expected_index in range(total):
                part = value if expected_index == 0 else json.loads(await read_frame())
                if (
                    not isinstance(part, dict)
                    or part.get("type") != "multipart"
                    or part.get("message_id") != message_id
                    or part.get("index") != expected_index
                    or part.get("total") != total
                    or not isinstance(part.get("data"), str)
                ):
                    raise ValueError("invalid local-control multipart sequence")
                payload.extend(base64.b64decode(part["data"], validate=True))
                if len(payload) > _MAX_MESSAGE_BYTES:
                    raise ValueError("local-control message exceeds configured bound")
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise ValueError("invalid local-control multipart payload")
        return value


_active_channel: contextvars.ContextVar[_Channel | None] = contextvars.ContextVar(
    "merlin_local_control_channel", default=None
)


@dataclass(slots=True)
class _StreamState:
    request_id: str
    prompt: str
    events: list[dict[str, Any]] = field(default_factory=list)
    first_cursor: int = 1
    done: bool = False
    error: str | None = None
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task[None] | None = None


def _trim_stream_events(
    events: list[dict[str, Any]], remaining: int
) -> tuple[list[dict[str, Any]], int, int]:
    """Spend the replay budget on token deltas only; always retain evidence events.

    Tool calls, tool errors, campaign reviews, and other non-batchable events are
    the forensic record of a turn — run15's execution turn was trimmed to zero
    events, erasing why the model never called the execute tool. Only batchable
    stream noise (status/content/thinking deltas) is trimmable.

    Returns ``(kept, removed, spent)`` where ``removed`` is the count of dropped
    leading-position events (advancing ``first_cursor``) and ``spent`` is the
    replay budget consumed.
    """
    noise = sum(1 for event in events if event.get("kind") in _BATCHABLE_STREAM_KINDS)
    spent = min(noise, max(0, remaining))
    drop = noise - spent
    kept: list[dict[str, Any]] = []
    for event in events:
        if drop and event.get("kind") in _BATCHABLE_STREAM_KINDS:
            drop -= 1
            continue
        kept.append(event)
    return kept, noise - spent, spent


def _bounded_completed_replays(
    completed: Mapping[str, dict[str, Any]],
    max_bytes: int = _MAX_COMPLETED_REPLAY_BYTES,
) -> dict[str, dict[str, Any]]:
    """Bound persisted results without allowing an old command to execute twice.

    Recent responses remain replayable after a daemon restart. Older or unusually
    large responses retain a small request-id tombstone: a reconnect receives an
    explicit replay-expired error instead of causing a mutating command to rerun.
    """
    items = list(completed.items())[-_MAX_CACHED_REQUESTS:]
    retained: dict[str, dict[str, Any]] = {}
    remaining = max(0, max_bytes)
    for request_id, response in reversed(items):
        encoded_size = len(
            json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        if encoded_size <= remaining:
            retained[request_id] = response
            remaining -= encoded_size
        else:
            retained[request_id] = _EXPIRED_COMPLETED_REPLAY
    return {request_id: retained[request_id] for request_id, _ in items}


def _load_journal(
    path: Path,
    max_stream_events: int = _DEFAULT_LOCAL_CONTROL_TUNING.max_stream_events,
    max_completed_bytes: int = _MAX_COMPLETED_REPLAY_BYTES,
) -> tuple[dict[str, dict[str, Any]], dict[str, _StreamState]]:
    if not path.exists():
        return {}, {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, {}
    completed = raw.get("completed", {}) if isinstance(raw, dict) else {}
    stream_values = raw.get("streams", {}) if isinstance(raw, dict) else {}
    valid_streams: list[tuple[str, dict[str, Any]]] = []
    for request_id, value in stream_values.items():
        if not isinstance(request_id, str) or not isinstance(value, dict):
            continue
        events = value.get("events", [])
        if not isinstance(events, list) or not all(isinstance(item, dict) for item in events):
            continue
        valid_streams.append((request_id, value))

    # The replay bound applies to the journal as a whole, not independently to
    # every historical stream, and it bounds only batchable stream noise —
    # tool/review/error events are always retained (see _trim_stream_events).
    # Otherwise hundreds of completed turns each retain a full token-delta
    # window and make daemon startup scale with all prior conversations.
    # Newest streams receive the available replay budget first; older completed
    # streams remain terminal tombstones, so an expired request cannot
    # accidentally execute again.
    retained: dict[str, tuple[list[dict[str, Any]], int]] = {}
    remaining = max_stream_events
    for request_id, value in reversed(valid_streams):
        kept, removed, spent = _trim_stream_events(value["events"], remaining)
        retained[request_id] = (kept, removed)
        remaining -= spent

    streams: dict[str, _StreamState] = {}
    for request_id, value in valid_streams:
        events, removed = retained[request_id]
        streams[request_id] = _StreamState(
            request_id=request_id,
            prompt=str(value.get("prompt", "")),
            events=events,
            first_cursor=max(1, int(value.get("first_cursor", 1)) + removed),
            done=True,
            error=(
                str(value["error"])
                if value.get("error")
                else (
                    None
                    if value.get("done") is True
                    else "daemon restarted; durable campaign work continues—reattach for status"
                )
            ),
        )
    safe_completed = {
        str(key): value
        for key, value in completed.items()
        if isinstance(key, str) and isinstance(value, dict)
    }
    return _bounded_completed_replays(safe_completed, max_completed_bytes), streams


def _write_journal(
    path: Path,
    completed: dict[str, dict[str, Any]],
    streams: dict[str, _StreamState],
    max_stream_events: int = _DEFAULT_LOCAL_CONTROL_TUNING.max_stream_events,
    max_completed_bytes: int = _MAX_COMPLETED_REPLAY_BYTES,
) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    remaining = max_stream_events
    persisted_streams: dict[str, dict[str, Any]] = {}
    for key, state in reversed(tuple(streams.items())):
        kept, removed, spent = _trim_stream_events(state.events, remaining)
        persisted_streams[key] = {
            "prompt": state.prompt,
            "events": kept,
            "first_cursor": state.first_cursor + removed,
            "done": state.done,
            "error": state.error,
        }
        remaining -= spent
    payload = {
        "completed": _bounded_completed_replays(completed, max_completed_bytes),
        # Preserve insertion order after allocating the global replay budget
        # from newest to oldest.  Live in-memory streams remain unabridged so a
        # connected slow client cannot be overtaken by a fast provider.
        "streams": {key: persisted_streams[key] for key in streams},
    }
    descriptor, temporary = tempfile.mkstemp(prefix=".control-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            # Encode once rather than issuing a Python write for every JSON
            # fragment. Large replay journals otherwise hold the writer queue
            # for seconds even when the filesystem itself is fast.
            handle.write(json.dumps(payload, separators=(",", ":")))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _request_id(request: dict[str, Any]) -> str:
    value = request.get("request_id")
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("local-control request_id must be a non-empty string")
    return value


def _peer_is_owner(writer: asyncio.StreamWriter) -> bool:
    transport_socket = writer.get_extra_info("socket")
    getpeereid = getattr(transport_socket, "getpeereid", None)
    if not callable(getpeereid):
        return True
    uid, _ = getpeereid()
    return uid == os.getuid()


async def daemon_tool_confirmer(
    spec: ToolSpec, context: ToolContext, arguments: dict[str, object]
) -> PermissionDecision | bool:
    channel = _active_channel.get()
    if channel is None:
        return False
    async with channel.approval_lock:
        approval_id = str(uuid4())
        await channel.send(
            {
                "type": "approval",
                "approval_id": approval_id,
                "spec": spec,
                "context": context,
                "arguments": arguments,
            }
        )
        response = await channel.receive()
    if response.get("type") != "approval_response" or response.get("approval_id") != approval_id:
        return False
    decision = response.get("decision")
    if isinstance(decision, bool):
        return decision
    try:
        return PermissionDecision(str(decision))
    except ValueError:
        return False


async def serve(
    socket_path: Path = _SOCKET,
    *,
    backend_factory: Callable[..., Any] | None = None,
    tuning: LocalControlTuning | None = None,
    hold_lock_until_process_exit: bool = False,
) -> None:
    from .bootstrap import build_backend

    runtime_identity = _RUNTIME_IDENTITY
    tuning = tuning or _DEFAULT_LOCAL_CONTROL_TUNING
    await asyncio.to_thread(socket_path.parent.mkdir, mode=0o700, parents=True, exist_ok=True)
    if socket_path.parent == _SOCKET.parent:
        parent_stat = await asyncio.to_thread(socket_path.parent.stat)
        if parent_stat.st_uid != os.getuid():
            raise PermissionError("local-control directory is not owned by the current user")
        os.chmod(socket_path.parent, 0o700)
    daemon_lock = await asyncio.to_thread(_acquire_daemon_lock, socket_path)
    backend = None
    run_store = None
    shutdown_record = None
    run_id = str(uuid4())
    cleanup_started = False
    run_started = False
    owns_socket = False
    try:
        if await asyncio.to_thread(socket_path.exists):
            try:
                reader, writer = await asyncio.open_unix_connection(
                    socket_path, limit=_MAX_FRAME_BYTES + 1
                )
                writer.close()
                await writer.wait_closed()
                raise RuntimeError("Merlin daemon is already running")
            except (ConnectionError, OSError):
                await asyncio.to_thread(socket_path.unlink, missing_ok=True)
        backend = (
            backend_factory(tool_confirmer=daemon_tool_confirmer)
            if backend_factory is not None
            else build_backend(tool_confirmer=daemon_tool_confirmer, daemon=True)
        )
        run_store = getattr(backend, "daemon_run_repository", None)
        if run_store is not None:
            await asyncio.to_thread(run_store.start, run_id, pid=os.getpid())
            run_started = True
        recover = getattr(backend, "recover_background", None)
        if callable(recover):
            await recover()

        shutdown = asyncio.Event()
        clean_shutdown = False
        foreground_lease_id: str | None = None
        foreground_channel: _Channel | None = None
        foreground_accepts_close = False
        unclaimed_shutdown: asyncio.Task[None] | None = None
        shutdown_record: dict[str, Any] | None = None
        stop_kind = "unknown"
        storage_exhausted = False

        async def shutdown_state() -> dict[str, object]:
            """Capture best-effort durable state before owned resources are closed."""
            snapshot: dict[str, object] = {}
            for name in ("list_campaigns", "list_active_schedules"):
                reader = getattr(backend, name, None)
                if not callable(reader):
                    continue
                try:
                    snapshot[name] = _json(await reader())
                except Exception as error:
                    snapshot[f"{name}_error"] = (
                        f"{type(error).__module__}.{type(error).__qualname__}: {error}"
                    )
            return snapshot

        async def request_shutdown(
            reason: str,
            *,
            clean: bool,
            details: Mapping[str, object] | None = None,
        ) -> None:
            """Attribute every orderly daemon shutdown before teardown begins."""
            nonlocal clean_shutdown, shutdown_record, stop_kind
            if shutdown_record is not None:
                return
            clean_shutdown = clean
            stop_kind = (
                "signal"
                if reason in {"SIGTERM", "SIGINT"}
                else ("clean" if clean else "fatal_exception")
            )
            shutdown_record = {
                "event": "merlin_daemon_shutdown_requested",
                "reason": reason,
                "clean": clean,
                "occurred_at": time.time(),
                "foreground_lease_id": foreground_lease_id,
                "details": dict(details or {}),
                "caller_stack": traceback.format_stack()[:-1],
            }
            shutdown_record["state"] = await shutdown_state()
            if run_store is not None:
                try:
                    await asyncio.to_thread(
                        run_store.request_stop,
                        run_id,
                        stop_kind=stop_kind,
                        reason=reason,
                        details=shutdown_record,
                    )
                except Exception:
                    logging.getLogger(__name__).exception("Could not persist daemon stop request")
            logging.getLogger(__name__).warning(
                "MERLIN_DAEMON_SHUTDOWN %s",
                json.dumps(shutdown_record, separators=(",", ":"), default=str),
            )
            shutdown.set()

        async def heartbeat_run() -> None:
            while not shutdown.is_set():
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=_DAEMON_HEARTBEAT_SECONDS)
                except TimeoutError:
                    if run_store is not None:
                        try:
                            await asyncio.to_thread(run_store.heartbeat, run_id)
                        except Exception:
                            logging.getLogger(__name__).exception("Daemon heartbeat failed")

        async def stop_if_daemon_remains_unclaimed(*, grace_seconds: float) -> None:
            """Retire an unclaimed daemon after its durable background work settles."""
            nonlocal clean_shutdown
            if grace_seconds:
                await asyncio.sleep(grace_seconds)
            activity_reader = getattr(backend, "has_active_background_work", None)
            inactive_observations = 0
            observations: list[dict[str, object]] = []
            while foreground_lease_id is None and not shutdown.is_set():
                active_stream = any(
                    state.task is not None and not state.task.done() for state in streams.values()
                )
                try:
                    background_active = (
                        bool(await activity_reader()) if callable(activity_reader) else False
                    )
                    active = active_stream or background_active
                    activity_error = None
                except Exception as error:
                    # Losing the daemon would pause and invalidate live target contexts.
                    # Preserve possible work and retry the authoritative activity check.
                    active = True
                    background_active = None
                    activity_error = f"{type(error).__module__}.{type(error).__qualname__}: {error}"
                observations.append(
                    {
                        "observed_at": time.time(),
                        "active_stream": active_stream,
                        "background_active": background_active,
                        "activity_error": activity_error,
                        "active": active,
                    }
                )
                if active:
                    inactive_observations = 0
                else:
                    inactive_observations += 1
                if inactive_observations >= 2:
                    await request_shutdown(
                        "idle_watchdog",
                        clean=True,
                        details={
                            "grace_seconds": grace_seconds,
                            "inactive_observations": inactive_observations,
                            "observations": observations[-10:],
                        },
                    )
                    return
                try:
                    await asyncio.wait_for(
                        shutdown.wait(),
                        timeout=(
                            _IDLE_SHUTDOWN_CONFIRM_SECONDS
                            if inactive_observations
                            else _RECOVERY_WATCHDOG_SECONDS
                        ),
                    )
                except TimeoutError:
                    pass

        async def arm_idle_shutdown(*, grace_seconds: float) -> None:
            nonlocal unclaimed_shutdown
            if unclaimed_shutdown is not None:
                unclaimed_shutdown.cancel()
                await asyncio.gather(unclaimed_shutdown, return_exceptions=True)
            unclaimed_shutdown = asyncio.create_task(
                stop_if_daemon_remains_unclaimed(grace_seconds=grace_seconds),
                name="local-control:foreground-idle-watchdog",
            )

        async def watch_configuration() -> None:
            from merlin_config import ConfigPaths, load_config

            path = ConfigPaths.default().config
            try:
                initial = await asyncio.to_thread(path.stat)
                signature: tuple[int, int, int] | None = (
                    initial.st_ino,
                    initial.st_mtime_ns,
                    initial.st_size,
                )
            except FileNotFoundError:
                signature = None
            while not shutdown.is_set():
                try:
                    await asyncio.wait_for(
                        shutdown.wait(), timeout=tuning.config_reload_poll_seconds
                    )
                    continue
                except TimeoutError:
                    pass
                try:
                    stat = await asyncio.to_thread(path.stat)
                    observed = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                except FileNotFoundError:
                    observed = None
                if observed == signature:
                    continue
                signature = observed
                try:
                    newer = await asyncio.to_thread(load_config)
                    await reload_configuration(newer)
                except Exception as error:
                    # Atomic config writers will produce another signature when corrected.
                    # Keep serving with the last valid applied configuration meanwhile.
                    failed = getattr(backend, "configuration_reload_failed", None)
                    if callable(failed):
                        failed(error)
                    continue

        async def watch_campaign_ownership() -> None:
            """Renew daemon ownership of durable campaigns after supervisor failure."""
            if not callable(recover):
                return
            last_error: str | None = None
            last_reported = 0.0
            while not shutdown.is_set():
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=_RECOVERY_WATCHDOG_SECONDS)
                    continue
                except TimeoutError:
                    pass
                try:
                    await recover()
                except Exception as error:
                    # Keep the watchdog alive across transient database teardown;
                    # durable state is retried on the next pass. Report a persistent
                    # failure once a minute instead of spamming every pass.
                    message = str(error)
                    now = time.monotonic()
                    if message != last_error or now - last_reported >= 60.0:
                        print(f"campaign recovery watchdog: {error}", file=sys.stderr)
                        last_reported = now
                    last_error = message
                else:
                    last_error = None

        reload_configuration = getattr(backend, "reload_configuration", None)
        config_watcher: asyncio.Task[None] | None = None
        recovery_watcher: asyncio.Task[None] | None = None
        journal_path = socket_path.with_suffix(".journal.json")
        completed, streams = await asyncio.to_thread(
            _load_journal, journal_path, tuning.max_stream_events
        )
        journal_lock = asyncio.Lock()

        async def persist_journal() -> None:
            nonlocal storage_exhausted
            async with journal_lock:
                if storage_exhausted:
                    raise LocalStorageExhaustedError()
                # Snapshot event-loop-owned collections before handing them to a writer thread.
                # Otherwise another client may add a request while json is iterating the dict.
                completed_snapshot = dict(completed)
                stream_snapshot = {
                    key: _StreamState(
                        request_id=state.request_id,
                        prompt=state.prompt,
                        events=list(state.events),
                        first_cursor=state.first_cursor,
                        done=state.done,
                        error=state.error,
                    )
                    for key, state in tuple(streams.items())
                }
                try:
                    await asyncio.to_thread(
                        _write_journal,
                        journal_path,
                        completed_snapshot,
                        stream_snapshot,
                        tuning.max_stream_events,
                    )
                except OSError as error:
                    if error.errno != errno.ENOSPC:
                        raise
                    storage_exhausted = True
                    raise LocalStorageExhaustedError() from error

        async def produce_stream(state: _StreamState, channel: _Channel) -> None:
            nonlocal storage_exhausted
            token = _active_channel.set(channel)
            unpersisted = 0
            try:
                async for event in backend.stream(state.prompt):
                    encoded = _json(event)
                    session_id = getattr(getattr(backend, "kernel", None), "session_id", None)
                    if isinstance(session_id, str) and session_id:
                        encoded.setdefault("session_id", session_id)
                    async with state.changed:
                        state.events.append(encoded)
                        state.changed.notify_all()
                    unpersisted += 1
                    if (
                        unpersisted >= tuning.stream_journal_batch
                        or encoded.get("kind") not in _BATCHABLE_STREAM_KINDS
                    ):
                        await persist_journal()
                        unpersisted = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if isinstance(error, OSError) and error.errno == errno.ENOSPC:
                    storage_exhausted = True
                state.error = str(error)[:1_000]
            finally:
                _active_channel.reset(token)
                async with state.changed:
                    state.done = True
                    state.changed.notify_all()
                if not storage_exhausted:
                    try:
                        await persist_journal()
                    except LocalStorageExhaustedError:
                        state.error = str(LocalStorageExhaustedError())

        async def send_stream(channel: _Channel, state: _StreamState, cursor: int) -> None:
            if cursor < state.first_cursor - 1:
                raise ValueError("local-control stream cursor is no longer available")
            while True:
                async with state.changed:
                    index = cursor - state.first_cursor + 1
                    if 0 <= index < len(state.events):
                        value = state.events[index]
                    elif state.done:
                        # Do not acknowledge terminal delivery until the replay
                        # journal also records it.  An immediate daemon restart
                        # must not turn a completed turn into an interrupted one.
                        await persist_journal()
                        if state.error:
                            await channel.send({"type": "error", "error": state.error})
                        else:
                            await channel.send({"type": "end", "cursor": cursor})
                        # Once terminal delivery succeeds, keep a tombstone at the
                        # final cursor instead of every token delta. A reconnect at
                        # that cursor can still receive `end`; an older cursor gets
                        # an explicit replay-gap error and can never rerun the turn.
                        state.first_cursor += len(state.events)
                        state.events.clear()
                        await persist_journal()
                        return
                    else:
                        await state.changed.wait()
                        continue
                cursor += 1
                await channel.send(
                    {"type": "event", "cursor": cursor, "value": _wire_client_event(value)}
                )

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal clean_shutdown, storage_exhausted
            nonlocal foreground_lease_id, foreground_channel, foreground_accepts_close
            nonlocal unclaimed_shutdown
            channel = _Channel(reader, writer)
            try:
                if not _peer_is_owner(writer):
                    raise PermissionError("local-control peer is not the daemon owner")
                request = await channel.receive(deadline_seconds=tuning.request_timeout_seconds)
                method = request.get("method")
                if method == "ping":
                    await channel.send(
                        {
                            "type": "result",
                            "value": "pong",
                            "protocol": PROTOCOL_VERSION,
                            "runtime_identity": asdict(runtime_identity),
                        }
                    )
                    return
                if method in {"shutdown", "shutdown_clean"}:
                    requested_lease = request.get("lease_id")
                    if foreground_lease_id is not None and requested_lease != foreground_lease_id:
                        raise PermissionError("clean shutdown is reserved for the foreground owner")
                    await channel.send({"type": "result", "value": "stopping"})
                    await request_shutdown(
                        "explicit_request",
                        clean=True,
                        details={"method": method, "requested_lease_id": requested_lease},
                    )
                    return
                if method == "close_foreground":
                    if foreground_channel is not None:
                        if not foreground_accepts_close:
                            raise RuntimeError(
                                "The existing Merlin window needs an update. "
                                "Close it with /quit once, then relaunch Merlin."
                            )
                        await foreground_channel.send({"type": "foreground_close"})
                        await channel.send({"type": "result", "value": "closing"})
                    else:
                        await channel.send({"type": "result", "value": "no_owner"})
                    return
                if method == "arm_unclaimed_shutdown":
                    if foreground_lease_id is None:
                        await arm_idle_shutdown(grace_seconds=_FOREGROUND_ATTACH_GRACE_SECONDS)
                        value = "armed"
                    else:
                        value = "claimed"
                    await channel.send({"type": "result", "value": value})
                    return
                if method == "foreground_lease":
                    if foreground_lease_id is not None:
                        raise RuntimeError("Merlin already has a foreground owner")
                    lease_id = str(uuid4())
                    foreground_lease_id = lease_id
                    foreground_channel = channel
                    foreground_accepts_close = request.get("close_notifications") is True
                    if unclaimed_shutdown is not None:
                        unclaimed_shutdown.cancel()
                        await asyncio.gather(unclaimed_shutdown, return_exceptions=True)
                        unclaimed_shutdown = None
                    await channel.send(
                        {
                            "type": "result",
                            "value": "attached",
                            "lease_id": lease_id,
                            "heartbeat_seconds": _FOREGROUND_HEARTBEAT_SECONDS,
                        }
                    )
                    lease_end_reason = "foreground_eof"
                    last_heartbeat_at = time.monotonic()
                    try:
                        # EOF handles normal and abrupt exits. Heartbeats also detect a
                        # suspended frontend, which otherwise keeps its Unix socket open
                        # forever while the daemon continues serving stale code.
                        while True:
                            try:
                                heartbeat = await channel.receive(
                                    deadline_seconds=_FOREGROUND_LEASE_TIMEOUT_SECONDS
                                )
                            except TimeoutError:
                                lease_end_reason = "heartbeat_timeout"
                                break
                            if (
                                heartbeat.get("method") != "foreground_heartbeat"
                                or heartbeat.get("lease_id") != lease_id
                            ):
                                lease_end_reason = "invalid_heartbeat"
                                raise ValueError("invalid foreground lease heartbeat")
                            last_heartbeat_at = time.monotonic()
                    finally:
                        logging.getLogger(__name__).warning(
                            "MERLIN_FOREGROUND_LEASE_RELEASED %s",
                            json.dumps(
                                {
                                    "event": "merlin_foreground_lease_released",
                                    "reason": lease_end_reason,
                                    "lease_id": lease_id,
                                    "seconds_since_heartbeat": max(
                                        0.0, time.monotonic() - last_heartbeat_at
                                    ),
                                    "timeout_seconds": _FOREGROUND_LEASE_TIMEOUT_SECONDS,
                                },
                                separators=(",", ":"),
                            ),
                        )
                        if foreground_lease_id == lease_id:
                            foreground_lease_id = None
                            foreground_channel = None
                            foreground_accepts_close = False
                        if not shutdown.is_set():
                            await arm_idle_shutdown(grace_seconds=0)
                    return
                if method == "stream":
                    request_id = _request_id(request)
                    prompt = str(request.get("prompt", ""))
                    cursor = request.get("cursor", 0)
                    if not isinstance(cursor, int) or cursor < 0:
                        raise ValueError("local-control cursor must be a non-negative integer")
                    state = streams.get(request_id)
                    if state is None:
                        if storage_exhausted:
                            raise LocalStorageExhaustedError()
                        state = _StreamState(request_id, prompt)
                        streams[request_id] = state
                        state.task = asyncio.create_task(
                            produce_stream(state, channel), name=f"local-control:{request_id}"
                        )
                    elif state.prompt != prompt:
                        raise ValueError("request_id was already used with a different prompt")
                    await send_stream(channel, state, cursor)
                    return
                if method not in _ALLOWED:
                    raise ValueError("local-control method is not admitted")
                request_id = _request_id(request)
                if cached := completed.get(request_id):
                    if storage_exhausted and method not in _READ_ONLY_METHODS:
                        raise LocalStorageExhaustedError()
                    await channel.send(cached)
                    return
                if storage_exhausted and method not in _READ_ONLY_METHODS:
                    raise LocalStorageExhaustedError()
                function = getattr(backend, str(method))
                value = await function(*request.get("args", ()), **request.get("kwargs", {}))
                response = {"type": "result", "value": _json(value)}
                if method in _SNAPSHOT_METHODS or (
                    storage_exhausted and method in _READ_ONLY_METHODS
                ):
                    await channel.send(response)
                    return
                completed[request_id] = response
                if len(completed) > _MAX_CACHED_REQUESTS:
                    completed.pop(next(iter(completed)))
                await persist_journal()
                await channel.send(response)
            except (BrokenPipeError, ConnectionError, asyncio.IncompleteReadError):
                return
            except ClientEventDecodeError as error:
                await channel.send(
                    {
                        "type": "error",
                        "error": str(error),
                        "code": error.code,
                        "schema_version": error.schema_version,
                    }
                )
            except Exception as error:
                logging.getLogger(__name__).exception("Local-control request failed")
                try:
                    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
                        storage_exhausted = True
                        storage_exhausted_error = LocalStorageExhaustedError()
                    else:
                        storage_exhausted_error = error
                    await channel.send(
                        storage_exhausted_error.response()
                        if isinstance(storage_exhausted_error, LocalStorageExhaustedError)
                        else {"type": "error", "error": str(error)}
                    )
                except (BrokenPipeError, ConnectionError):
                    pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (BrokenPipeError, ConnectionError):
                    pass

        server = await asyncio.start_unix_server(handle, socket_path, limit=_MAX_FRAME_BYTES + 1)
        owns_socket = True
        os.chmod(socket_path, 0o600)
        if callable(reload_configuration):
            config_watcher = asyncio.create_task(
                watch_configuration(), name="local-control:config-watcher"
            )
        if callable(recover):
            recovery_watcher = asyncio.create_task(
                watch_campaign_ownership(), name="local-control:campaign-recovery-watchdog"
            )
        exit_error: BaseException | None = None
        heartbeat_task = asyncio.create_task(heartbeat_run(), name="daemon-run:heartbeat")
        signal_tasks: set[asyncio.Task[None]] = set()
        loop = asyncio.get_running_loop()
        previous_signal_handlers = {
            signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)
        }

        def on_signal(name: str) -> None:
            task = asyncio.create_task(request_shutdown(name, clean=True))
            signal_tasks.add(task)
            task.add_done_callback(signal_tasks.discard)

        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, on_signal, signum.name)
        try:
            async with server:
                await shutdown.wait()
        except BaseException as error:
            exit_error = error
            await request_shutdown(
                "fatal_exception",
                clean=False,
                details={"error": str(error), "traceback": traceback.format_exc()},
            )
            raise
        finally:
            cleanup_started = True
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(signum)
                signal.signal(signum, previous_signal_handlers[signum])
            if signal_tasks:
                await asyncio.gather(*signal_tasks, return_exceptions=True)
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            if unclaimed_shutdown is not None:
                unclaimed_shutdown.cancel()
                await asyncio.gather(unclaimed_shutdown, return_exceptions=True)
            if config_watcher is not None:
                config_watcher.cancel()
                await asyncio.gather(config_watcher, return_exceptions=True)
            if recovery_watcher is not None:
                recovery_watcher.cancel()
                await asyncio.gather(recovery_watcher, return_exceptions=True)
            for state in streams.values():
                if state.task is not None and not state.task.done():
                    state.task.cancel()
            await asyncio.gather(
                *(state.task for state in streams.values() if state.task is not None),
                return_exceptions=True,
            )
            cleanup_error: BaseException | None = None
            try:
                if not storage_exhausted:
                    await persist_journal()
            except LocalStorageExhaustedError:
                pass
            except Exception as error:
                cleanup_error = error
            try:
                detach = getattr(backend, "detach", None)
                close = getattr(backend, "close", None)
                if clean_shutdown and callable(close):
                    await close()
                elif callable(detach):
                    await detach()
                elif callable(close):
                    await close()
                else:
                    raise RuntimeError("local-control backend exposes neither close nor detach")
            except BaseException as error:
                cleanup_error = error
            finally:
                final_record = {
                    "event": "merlin_daemon_shutdown_finished",
                    "reason": (shutdown_record or {}).get("reason", "unknown"),
                    "clean": clean_shutdown,
                    "local_storage_exhausted": storage_exhausted,
                    "exit_error": (
                        None
                        if exit_error is None
                        else (
                            f"{type(exit_error).__module__}."
                            f"{type(exit_error).__qualname__}: {exit_error}"
                        )
                    ),
                    "cleanup_error": (
                        None
                        if cleanup_error is None
                        else (
                            f"{type(cleanup_error).__module__}."
                            f"{type(cleanup_error).__qualname__}: {cleanup_error}"
                        )
                    ),
                }
                logging.getLogger(__name__).warning(
                    "MERLIN_DAEMON_SHUTDOWN_FINISHED %s",
                    json.dumps(final_record, separators=(",", ":"), default=str),
                )
                if run_store is not None and cleanup_error is None:
                    await asyncio.to_thread(
                        run_store.finish,
                        run_id,
                        stop_kind=stop_kind,
                        reason=(shutdown_record or {}).get("reason", "unknown"),
                        details=final_record,
                    )
            if cleanup_error is not None:
                raise cleanup_error
    except BaseException as error:
        if backend is not None and not cleanup_started:
            # Startup can fail before the server's normal teardown is armed.
            if run_started:
                try:
                    await asyncio.to_thread(
                        run_store.request_stop,
                        run_id,
                        stop_kind="fatal_exception",
                        reason="startup_failure",
                        details={"error": str(error)},
                    )
                except Exception:
                    logging.getLogger(__name__).exception("Could not attribute startup failure")
            close = getattr(backend, "detach", None) or getattr(backend, "close", None)
            if callable(close):
                await close()
                if run_started:
                    await asyncio.to_thread(
                        run_store.finish,
                        run_id,
                        stop_kind="fatal_exception",
                        reason="startup_failure",
                        details={"error": str(error)},
                    )
        raise
    finally:
        try:
            if run_store is not None:
                await asyncio.to_thread(run_store.connection.close)
        finally:
            try:
                database = getattr(backend, "daemon_database", None)
                if database is not None:
                    await asyncio.to_thread(database.stop)
            finally:
                if owns_socket:
                    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
                if hold_lock_until_process_exit:
                    if daemon_lock not in _PROCESS_LIFETIME_LOCKS:
                        _PROCESS_LIFETIME_LOCKS.append(daemon_lock)
                else:
                    daemon_lock.close()


def _legacy_foreground_pid(socket_path: Path) -> int:
    """Find a same-user Merlin frontend connected to this exact macOS daemon."""
    daemon_pid = _daemon_lock_owner(socket_path)
    lsof = shutil.which("lsof")
    if sys.platform != "darwin" or daemon_pid is None or lsof is None:
        raise RuntimeError("Could not identify the existing Merlin window automatically")
    result = subprocess.run(
        [lsof, "-n", "-P", "-U", "-a", "-u", str(os.getuid()), "-l", "+c", "0"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    records: list[tuple[int, str, str]] = []
    for line in result.stdout.splitlines()[1:]:
        fields = line.split(maxsplit=7)
        if len(fields) != 8 or fields[4] != "unix":
            continue
        if not fields[1].isdigit() or fields[2] != str(os.getuid()):
            continue
        records.append((int(fields[1]), fields[5], fields[7]))
    endpoints = {
        address for pid, address, name in records if pid == daemon_pid and name == str(socket_path)
    }
    peers = {
        pid
        for pid, _, name in records
        if pid not in {daemon_pid, os.getpid()} and name.removeprefix("->") in endpoints
    }
    frontends = []
    for pid in peers:
        process = subprocess.run(
            ["ps", "-p", str(pid), "-o", "uid=,args="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        identity = process.stdout.strip().split(maxsplit=1)
        if len(identity) != 2 or identity[0] != str(os.getuid()):
            continue
        argv = shlex.split(identity[1])
        frozen = len(argv) == 1 and Path(argv[0]).name in {"merlin", "merlin-runtime"}
        source = (
            len(argv) == 2
            and Path(argv[0]).name.lower().startswith("python")
            and Path(argv[1]).name == "merlin"
        )
        if frozen or source:
            frontends.append(pid)
    if len(frontends) != 1:
        raise RuntimeError("Could not uniquely identify the existing Merlin window automatically")
    return frontends[0]


def _resume_stopped_foreground(socket_path: Path) -> None:
    """Let a verified, suspended frontend process its pending close notification."""
    try:
        pid = _legacy_foreground_pid(socket_path)
        state = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
        if state.startswith("T"):
            os.kill(pid, signal.SIGCONT)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        # Normal frontends and unsupported discovery platforms still use the
        # existing close notification and bounded cleanup wait.
        return


def _verified_daemon_owner(socket_path: Path) -> bool:
    """Require a same-user daemon identity before automatic orphan recovery."""
    pid = _daemon_lock_owner(socket_path)
    if pid is None or pid == os.getpid():
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "uid=,args="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        identity = result.stdout.strip().split(maxsplit=1)
        if len(identity) != 2 or identity[0] != str(os.getuid()):
            return False
        argv = shlex.split(identity[1])
        source = len(argv) == 4 and argv[1:] == ["-m", "merlin_tui.local_control", "daemon"]
        frozen = (
            len(argv) == 2
            and Path(argv[0]).name in {"merlin", "merlin-runtime"}
            and argv[1] == "daemon"
        )
        return (source or frozen) and _daemon_lock_owner(socket_path) == pid
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


async def _recover_restart_orphan(socket_path: Path) -> None:
    if not await asyncio.to_thread(_verified_daemon_owner, socket_path):
        raise RuntimeError(
            "Could not verify the unreachable Merlin daemon owner; refusing recovery"
        )
    await stop_daemon(socket_path, force=True)


class LocalControlClient:
    def __init__(
        self,
        confirmer: Any | None = None,
        socket_path: Path = _SOCKET,
        *,
        owns_daemon: bool = False,
    ) -> None:
        self._confirmer = confirmer
        self._socket_path = socket_path
        self._owns_daemon = owns_daemon
        self._lease_reader: asyncio.StreamReader | None = None
        self._lease_writer: asyncio.StreamWriter | None = None
        self._lease_id: str | None = None
        self._lease_heartbeat: asyncio.Task[None] | None = None

    async def acquire_foreground_lease(self, *, close_notifications: bool = False) -> None:
        """Keep the daemon alive only while this foreground TUI exists."""
        if self._lease_writer is not None:
            return
        reader, writer = await asyncio.open_unix_connection(
            self._socket_path, limit=_MAX_FRAME_BYTES + 1
        )
        channel = _Channel(reader, writer)
        try:
            await channel.send(
                {"method": "foreground_lease", "close_notifications": close_notifications}
            )
            response = await channel.receive()
            if response.get("type") == "error":
                raise RuntimeError(str(response.get("error", "foreground lease rejected")))
            if response.get("value") != "attached":
                raise RuntimeError("Merlin daemon did not acknowledge foreground ownership")
            lease_id = response.get("lease_id")
            if not isinstance(lease_id, str) or not lease_id:
                raise RuntimeError("Merlin daemon returned an invalid foreground lease")
            heartbeat_seconds = response.get("heartbeat_seconds")
            legacy_lease = heartbeat_seconds is None
            if not legacy_lease and (
                not isinstance(heartbeat_seconds, (int, float)) or heartbeat_seconds <= 0
            ):
                raise RuntimeError("Merlin daemon returned an invalid heartbeat interval")
        except BaseException:
            await _close_writer(writer)
            raise
        if legacy_lease:
            # A daemon from before renewable foreground leases can keep stale code
            # alive forever after its TUI is suspended. We own this just-acquired
            # idle lease, so replace it cleanly before exposing the new UI.
            try:
                await stop_daemon(self._socket_path, lease_id=lease_id)
            finally:
                await _close_writer(writer)
            self._owns_daemon = bool(await ensure_daemon(self._socket_path))
            await self.acquire_foreground_lease(close_notifications=close_notifications)
            return
        self._close_notifications = close_notifications
        self._lease_reader = reader
        self._lease_writer = writer
        self._lease_id = lease_id
        self._lease_heartbeat = asyncio.create_task(
            self._renew_foreground_lease(writer, lease_id, float(heartbeat_seconds)),
            name="local-control:foreground-heartbeat",
        )

    async def _renew_foreground_lease(
        self,
        writer: asyncio.StreamWriter,
        lease_id: str,
        heartbeat_seconds: float,
    ) -> None:
        """Prove that the foreground event loop is still running."""
        try:
            while writer is self._lease_writer and lease_id == self._lease_id:
                await asyncio.sleep(heartbeat_seconds)
                if writer is not self._lease_writer or lease_id != self._lease_id:
                    return
                frame = json.dumps(
                    {"method": "foreground_heartbeat", "lease_id": lease_id},
                    separators=(",", ":"),
                )
                writer.write((frame + "\n").encode())
                await writer.drain()
        except (BrokenPipeError, ConnectionError, OSError):
            return

    async def _drop_foreground_lease(self) -> asyncio.StreamWriter | None:
        heartbeat = self._lease_heartbeat
        self._lease_heartbeat = None
        self._lease_reader = None
        writer = self._lease_writer
        self._lease_writer = None
        self._lease_id = None
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        return writer

    async def wait_for_foreground_close(self) -> None:
        """Wait for another window's explicit request to close this session."""
        while True:
            if getattr(self, "_reconnecting", False):
                await asyncio.sleep(0.05)
                continue
            reader = self._lease_reader
            if reader is None:
                raise RuntimeError("No foreground session is attached")
            # The server owns liveness expiry; this reader only receives explicit
            # close requests or EOF after lease expiry.
            line = await reader.readline()
            if getattr(self, "_reconnecting", False) or reader is not self._lease_reader:
                continue
            if not line:
                raise ConnectionError(
                    "The Merlin session disconnected. Run ./merlin and choose "
                    '"Resume last session" to reconnect.'
                )
            if json.loads(line).get("type") != "foreground_close":
                raise RuntimeError("Unexpected foreground session notification")
            return

    async def release_foreground_lease(self) -> None:
        """Release foreground ownership before terminal teardown can suspend the TUI."""
        writer = await self._drop_foreground_lease()
        if writer is not None:
            await _close_writer(writer)
            # From this point the daemon's campaign-aware idle watcher owns its
            # lifetime.  A later client close must not bypass that watcher and
            # pause durable work that is still active.
            self._owns_daemon = False

    async def replace_foreground(self, *, restart: bool = False) -> None:
        """Ask the existing window to quit, then wait for owned-resource cleanup."""
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.open_unix_connection(
                self._socket_path, limit=_MAX_FRAME_BYTES + 1
            )
            channel = _Channel(reader, writer)
            await channel.send({"method": "close_foreground"})
            response = await channel.receive(
                deadline_seconds=_configured_daemon_timeout("daemon_shutdown_seconds", 30)
                if restart
                else None
            )
            value = response.get("value")
            if response.get("type") == "error":
                error = str(response.get("error", "Could not close the existing session"))
                legacy = any(
                    reason in error.lower()
                    for reason in (
                        "not admitted",
                        "not allowed",
                        "unknown",
                        "needs an update",
                    )
                )
                if not legacy:
                    raise RuntimeError(error)
                value = "legacy"
            if value not in {"closing", "no_owner", "legacy"}:
                raise RuntimeError("The existing session did not acknowledge the close request")
        except TimeoutError as error:
            raise TimeoutError("The existing Merlin session did not respond to restart") from error
        except (OSError, ConnectionError):
            if not await asyncio.to_thread(_daemon_lock_released, self._socket_path):
                if restart:
                    await _recover_restart_orphan(self._socket_path)
                else:
                    await stop_daemon(self._socket_path, force=True)
            value = "no_owner"
        finally:
            if writer is not None:
                await _close_writer(writer)
        if value == "legacy":
            pid = await asyncio.to_thread(_legacy_foreground_pid, self._socket_path)
            try:
                # SIGINT gives the old Textual app its normal cancellation and
                # terminal-restoration path; lease EOF triggers daemon cleanup.
                os.kill(pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            value = "closing"
        if value == "closing":
            await asyncio.to_thread(_resume_stopped_foreground, self._socket_path)
            seconds = _configured_daemon_timeout("daemon_shutdown_seconds", 30)
            deadline = asyncio.get_running_loop().time() + seconds
            while asyncio.get_running_loop().time() < deadline:
                if await asyncio.to_thread(_daemon_lock_released, self._socket_path):
                    break
                await asyncio.sleep(0.05)
            else:
                if restart and not await asyncio.to_thread(self._socket_path.exists):
                    await _recover_restart_orphan(self._socket_path)
                else:
                    raise TimeoutError("The existing session is still closing. Try again shortly.")
        elif restart:
            try:
                await stop_daemon(self._socket_path)
            except TimeoutError:
                if await asyncio.to_thread(self._socket_path.exists):
                    raise
                await _recover_restart_orphan(self._socket_path)
        self._owns_daemon = bool(await ensure_daemon(self._socket_path))

    async def stream(self, prompt: str):
        request_id = str(uuid4())
        cursor = 0
        reconnects = 0
        recovered_new_connection = False
        request_sent = False
        while True:
            writer: asyncio.StreamWriter | None = None
            try:
                reader, writer = await asyncio.open_unix_connection(
                    self._socket_path, limit=_MAX_FRAME_BYTES + 1
                )
                channel = _Channel(reader, writer)
                request_sent = True
                await channel.send(
                    {
                        "method": "stream",
                        "request_id": request_id,
                        "prompt": prompt,
                        "cursor": cursor,
                    }
                )
                while True:
                    response = await channel.receive()
                    kind = response.get("type")
                    if kind == "event":
                        next_cursor = response.get("cursor")
                        if not isinstance(next_cursor, int) or next_cursor != cursor + 1:
                            raise RuntimeError("daemon returned an out-of-order stream cursor")
                        cursor = next_cursor
                        event = decode_client_event(response["value"])
                        yield StreamEvent(kind=event.kind, text=event.text)
                    elif kind == "approval":
                        decision: object = False
                        if self._confirmer is not None:
                            decision = await self._confirmer(
                                SimpleNamespace(**response["spec"]),
                                SimpleNamespace(**response["context"]),
                                response["arguments"],
                            )
                        await channel.send(
                            {
                                "type": "approval_response",
                                "approval_id": response["approval_id"],
                                "decision": decision,
                            }
                        )
                    elif kind == "end":
                        return
                    elif kind == "error":
                        if response.get("code") in {
                            "unsupported_client_event_version",
                            "invalid_client_event",
                        }:
                            raise ClientEventDecodeError(
                                response["code"],
                                str(response.get("error", "invalid client event")),
                                schema_version=response.get("schema_version"),
                            )
                        raise RuntimeError(str(response.get("error", "daemon request failed")))
            except (ConnectionError, OSError) as exc:
                if (
                    isinstance(exc, (FileNotFoundError, ConnectionRefusedError))
                    and prompt.strip() == "/new"
                    and writer is None
                    and cursor == 0
                    and not request_sent
                    and not recovered_new_connection
                ):
                    recovered_new_connection = True
                    yield StreamEvent(
                        "status", "Reconnecting to Merlin before creating the new session…"
                    )
                    await self._recover_new_session_connection()
                    continue
                if reconnects >= 2:
                    raise
                reconnects += 1
                await asyncio.sleep(0.05)
            finally:
                if writer is not None:
                    await _close_writer(writer)
        raise RuntimeError("daemon request failed")

    async def _recover_new_session_connection(self) -> None:
        """Restore transport before an unsent /new request, preserving the lease."""
        self._reconnecting = True
        try:
            had_lease = self._lease_writer is not None
            self._owns_daemon = bool(await ensure_daemon(self._socket_path))
            writer = await self._drop_foreground_lease()
            if writer is not None:
                await _close_writer(writer)
            if had_lease:
                await self.acquire_foreground_lease(
                    close_notifications=getattr(self, "_close_notifications", False)
                )
        finally:
            self._reconnecting = False

    async def close(self) -> None:
        if self._owns_daemon:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Stop the owned daemon and fully close recon/browser/storage resources."""
        reader, writer = await asyncio.open_unix_connection(
            self._socket_path, limit=_MAX_FRAME_BYTES + 1
        )
        try:
            channel = _Channel(reader, writer)
            await channel.send({"method": "shutdown_clean", "lease_id": self._lease_id})
            response = await channel.receive()
            if response.get("type") == "error":
                raise RuntimeError(str(response.get("error", "clean shutdown rejected")))
            if response.get("value") != "stopping":
                raise RuntimeError("Merlin daemon did not acknowledge clean shutdown")
            self._owns_daemon = False
        finally:
            await _close_writer(writer)
            lease_writer = await self._drop_foreground_lease()
            if lease_writer is not None:
                await _close_writer(lease_writer)

        shutdown_seconds = _configured_daemon_timeout("daemon_shutdown_seconds", 30)
        deadline = asyncio.get_running_loop().time() + shutdown_seconds
        while asyncio.get_running_loop().time() < deadline:
            socket_removed = not await asyncio.to_thread(self._socket_path.exists)
            if socket_removed and await asyncio.to_thread(_daemon_lock_released, self._socket_path):
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("Merlin daemon did not finish clean shutdown")

    async def apply_saved_model_routing(self) -> str:
        """Reconnect the daemon so newly saved provider routes take effect."""
        self._reconnecting = True
        try:
            had_foreground_lease = self._lease_writer is not None
            stopped = await stop_daemon(self._socket_path, lease_id=self._lease_id)
            if not stopped:
                raise RuntimeError("model routing was saved, but the daemon did not stop cleanly")
            lease_writer = await self._drop_foreground_lease()
            if lease_writer is not None:
                await _close_writer(lease_writer)
            self._owns_daemon = bool(await ensure_daemon(self._socket_path))
            if had_foreground_lease:
                if getattr(self, "_close_notifications", False):
                    await self.acquire_foreground_lease(close_notifications=True)
                else:
                    await self.acquire_foreground_lease()
            return "Daemon reconnected automatically; new campaigns use these routes."
        finally:
            self._reconnecting = False

    def __getattr__(self, name: str):
        if name not in _ALLOWED:
            raise AttributeError(name)

        async def call(*args: object, **kwargs: object) -> Any:
            return await self._call(name, args, kwargs)

        return call

    async def _call(self, method: str, args: object, kwargs: object) -> Any:
        request_id = str(uuid4())
        for attempt in range(2):
            writer: asyncio.StreamWriter | None = None
            try:
                reader, writer = await asyncio.open_unix_connection(
                    self._socket_path, limit=_MAX_FRAME_BYTES + 1
                )
                channel = _Channel(reader, writer)
                await channel.send(
                    {
                        "method": method,
                        "request_id": request_id,
                        "args": args,
                        "kwargs": kwargs,
                    }
                )
                response = await channel.receive()
                if response.get("type") == "error":
                    raise RuntimeError(str(response.get("error", "daemon request failed")))
                return _decode(response.get("value"))
            except (ConnectionError, OSError):
                if attempt:
                    raise
                await asyncio.sleep(0.05)
            finally:
                if writer is not None:
                    await _close_writer(writer)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    """Close a local-control socket without leaking expected peer shutdown errors."""
    writer.close()
    try:
        await writer.wait_closed()
    except (BrokenPipeError, ConnectionError, OSError):
        # The daemon may have already closed or restarted after delivering the
        # durable stream cursor. Closing that stale client pipe is cleanup, not
        # a provider failure and must not abort the operator's turn.
        pass


async def ensure_daemon(
    socket_path: Path = _SOCKET,
    *,
    startup_seconds: float | None = None,
    progress: Callable[[float], None] | None = None,
) -> bool:
    if startup_seconds is None:
        startup_seconds = _configured_daemon_timeout("daemon_startup_seconds", 180)
    started = asyncio.get_running_loop().time()
    deadline = started + startup_seconds
    probe = await _probe_daemon_compatibility(socket_path)
    if probe.status is DaemonCompatibility.COMPATIBLE:
        return False
    if probe.status is DaemonCompatibility.INCOMPATIBLE:
        raise _incompatible_daemon_error(socket_path, probe)
    while not await asyncio.to_thread(_daemon_lock_released, socket_path):
        now = asyncio.get_running_loop().time()
        if now >= deadline:
            raise TimeoutError(
                "The previous Merlin daemon is still starting or closing after "
                f"{startup_seconds:g}s; inspect {_DAEMON_LOG}"
            )
        if progress is not None:
            progress(now - started)
        await asyncio.sleep(min(0.05, max(0, deadline - now)))
        probe = await _probe_daemon_compatibility(socket_path)
        if probe.status is DaemonCompatibility.COMPATIBLE:
            return False
        if probe.status is DaemonCompatibility.INCOMPATIBLE:
            raise _incompatible_daemon_error(socket_path, probe)
    process = await asyncio.to_thread(_spawn_daemon)
    exited_at: float | None = None
    exit_code: int | None = None
    while (now := asyncio.get_running_loop().time()) < deadline:
        if progress is not None:
            progress(now - started)
        await asyncio.sleep(0.05)
        probe = await _probe_daemon_compatibility(socket_path)
        if probe.status is DaemonCompatibility.COMPATIBLE:
            # A competing starter can win the daemon lock while our child exits.
            # Only the process whose child is still alive owns orderly cleanup.
            return process.poll() is None
        if probe.status is DaemonCompatibility.INCOMPATIBLE:
            raise _incompatible_daemon_error(socket_path, probe)
        return_code = process.poll()
        if return_code is not None:
            if exited_at is None:
                exited_at = now
                exit_code = return_code
            if now - exited_at < _STARTER_EXIT_GRACE_SECONDS:
                continue
            detail = _daemon_failure_detail()
            raise RuntimeError(
                f"Merlin daemon exited during startup (status {exit_code}). {detail}"
            )
    raise TimeoutError(f"Merlin daemon did not become ready; inspect {_DAEMON_LOG}")


def _spawn_daemon() -> subprocess.Popen[bytes]:
    _DAEMON_LOG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        daemon_cwd = Path(os.getcwd())
    except OSError:
        daemon_cwd = Path.home()
    with _DAEMON_LOG.open("ab") as log:
        return subprocess.Popen(
            _daemon_command(),
            cwd=daemon_cwd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _daemon_failure_detail() -> str:
    try:
        lines = _DAEMON_LOG.read_text(errors="replace").splitlines()
    except OSError:
        return f"Inspect {_DAEMON_LOG}."
    for line in reversed(lines):
        message = line.strip()
        if message and not message.startswith("[PYI-"):
            return f"Last log message: {message} (log: {_DAEMON_LOG})"
    return f"Inspect {_DAEMON_LOG}."


def _daemon_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "daemon"]
    return [sys.executable, "-m", "merlin_tui.local_control", "daemon"]


async def _ping(socket_path: Path) -> bool:
    return (await _probe_daemon_compatibility(socket_path)).status is not (
        DaemonCompatibility.UNREACHABLE
    )


async def _probe_daemon_compatibility(socket_path: Path) -> DaemonCompatibilityProbe:
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path, limit=_MAX_FRAME_BYTES + 1)
        channel = _Channel(reader, writer)
        await channel.send({"method": "ping"})
        response = await asyncio.wait_for(channel.receive(), timeout=5.0)
        if response.get("value") != "pong":
            return DaemonCompatibilityProbe(
                DaemonCompatibility.INCOMPATIBLE, reason="invalid ping response"
            )
        raw_identity = response.get("runtime_identity")
        if not isinstance(raw_identity, Mapping):
            return DaemonCompatibilityProbe(
                DaemonCompatibility.INCOMPATIBLE, reason="missing runtime identity"
            )
        fields = (
            raw_identity.get("protocol"),
            raw_identity.get("distribution_version"),
            raw_identity.get("executable"),
            raw_identity.get("module_root"),
            raw_identity.get("mode"),
        )
        if not all(isinstance(value, str) for value in fields) or fields[4] not in {
            "source",
            "frozen",
        }:
            return DaemonCompatibilityProbe(
                DaemonCompatibility.INCOMPATIBLE, reason="invalid runtime identity"
            )
        identity = RuntimeIdentity(*fields)
        if identity != _RUNTIME_IDENTITY:
            return DaemonCompatibilityProbe(
                DaemonCompatibility.INCOMPATIBLE,
                identity=identity,
                reason="runtime identity mismatch",
            )
        return DaemonCompatibilityProbe(DaemonCompatibility.COMPATIBLE, identity=identity)
    except ValueError:
        return DaemonCompatibilityProbe(
            DaemonCompatibility.INCOMPATIBLE, reason="invalid ping response"
        )
    except (OSError, TimeoutError, ConnectionError):
        return DaemonCompatibilityProbe(DaemonCompatibility.UNREACHABLE)
    finally:
        if writer is not None:
            await _close_writer(writer)


def _incompatible_daemon_error(
    socket_path: Path, probe: DaemonCompatibilityProbe
) -> IncompatibleDaemonError:
    detail = probe.reason or "unknown incompatibility"
    return IncompatibleDaemonError(
        "The running Merlin daemon is incompatible with this client "
        f"({detail}; socket: {socket_path}). Run `merlin restart` from the Merlin "
        "installation or checkout you want to use. The existing daemon was left running."
    )


def _daemon_lock_released(socket_path: Path) -> bool:
    """Return only after the prior daemon has surrendered process ownership."""
    try:
        handle = _acquire_daemon_lock(socket_path)
    except RuntimeError:
        return False
    handle.close()
    return True


async def restart_daemon(socket_path: Path = _SOCKET) -> None:
    """Close the owning TUI and wait for resource cleanup before starting anew."""
    await LocalControlClient(socket_path=socket_path).replace_foreground(restart=True)
    if await _ping(socket_path):
        await _arm_unclaimed_shutdown(socket_path)


async def _arm_unclaimed_shutdown(socket_path: Path = _SOCKET) -> None:
    """Give a newly started daemon a bounded window for a TUI to attach."""
    reader, writer = await asyncio.open_unix_connection(socket_path, limit=_MAX_FRAME_BYTES + 1)
    try:
        channel = _Channel(reader, writer)
        await channel.send({"method": "arm_unclaimed_shutdown"})
        response = await channel.receive()
        if response.get("type") == "error":
            raise RuntimeError(str(response.get("error", "idle shutdown was rejected")))
        if response.get("value") not in {"armed", "claimed"}:
            raise RuntimeError("Merlin daemon did not acknowledge restart ownership")
    finally:
        await _close_writer(writer)


async def stop_daemon(
    socket_path: Path = _SOCKET,
    *,
    shutdown_seconds: float | None = None,
    lease_id: str | None = None,
    force: bool = False,
) -> bool:
    if shutdown_seconds is None:
        shutdown_seconds = _configured_daemon_timeout("daemon_shutdown_seconds", 30)
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path, limit=_MAX_FRAME_BYTES + 1)
        channel = _Channel(reader, writer)
        await channel.send({"method": "shutdown_clean", "lease_id": lease_id})
        response = await channel.receive()
        if response.get("type") == "error":
            error = str(response.get("error", "daemon shutdown rejected"))
            if force and "foreground owner" in error:
                # Upgrade/install path: a live foreground lease must not strand
                # `stop --force`; terminate the owner pid like an orphaned runtime.
                owner = await asyncio.to_thread(_daemon_lock_owner, socket_path)
                if owner is not None:
                    os.kill(owner, signal.SIGTERM)
                    deadline = asyncio.get_running_loop().time() + shutdown_seconds
                    while asyncio.get_running_loop().time() < deadline:
                        if await asyncio.to_thread(_daemon_lock_released, socket_path):
                            return True
                        await asyncio.sleep(0.05)
                    raise TimeoutError(
                        f"Merlin daemon owner (pid {owner}) ignored termination"
                    ) from None
            raise RuntimeError(error)
        if response.get("value") != "stopping":
            return False
        deadline = asyncio.get_running_loop().time() + shutdown_seconds
        while asyncio.get_running_loop().time() < deadline:
            socket_removed = not await asyncio.to_thread(socket_path.exists)
            ownership_released = socket_removed and await asyncio.to_thread(
                _daemon_lock_released, socket_path
            )
            if ownership_released:
                return True
            await asyncio.sleep(0.05)
        raise TimeoutError("Merlin daemon acknowledged shutdown but did not finish cleanup")
    except (OSError, ConnectionError, ValueError):
        # A missing socket means "already stopped" only when no process still
        # owns the daemon lifetime lock.  Starting through a stale owner is how
        # repeated restart/install commands accumulate detached runtimes.
        if await asyncio.to_thread(_daemon_lock_released, socket_path):
            return False
        owner = await asyncio.to_thread(_daemon_lock_owner, socket_path)
        if force and owner is not None:
            # Orphaned runtime: alive enough to hold the lock, dead on the
            # socket. Terminate it so a fresh daemon can start.
            os.kill(owner, signal.SIGTERM)
            deadline = asyncio.get_running_loop().time() + shutdown_seconds
            while asyncio.get_running_loop().time() < deadline:
                if await asyncio.to_thread(_daemon_lock_released, socket_path):
                    return True
                await asyncio.sleep(0.05)
            raise TimeoutError(f"Merlin daemon owner (pid {owner}) ignored termination") from None
        detail = (
            "Merlin daemon is unreachable but still owns its process lock; "
            "refusing to start another runtime"
        )
        if owner is not None:
            detail += f" (owner pid {owner}; run 'merlin stop --force' to terminate it)"
        raise RuntimeError(detail) from None
    finally:
        if writer is not None:
            await _close_writer(writer)


def _configured_daemon_timeout(name: str, fallback: float) -> float:
    """Use validated owner config while preserving lifecycle recovery if it is malformed."""
    from merlin_config import load_config

    try:
        return float(getattr(load_config().runtime, name))
    except (OSError, TypeError, ValueError):
        return fallback


def _json(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json(model_dump(mode="json"))
    if is_dataclass(value):
        return _json(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class _DecodedObject(SimpleNamespace, Mapping[str, Any]):
    """Attribute-friendly IPC object that preserves mapping semantics."""

    def __getitem__(self, key: str) -> Any:
        return vars(self)[key]

    def __iter__(self) -> Iterator[str]:
        return iter(vars(self))

    def __len__(self) -> int:
        return len(vars(self))


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        return _DecodedObject(**{key: _decode(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_decode(item) for item in value)
    return value


if __name__ == "__main__" and sys.argv[1:] == ["daemon"]:
    asyncio.run(serve(tuning=configured_local_control_tuning(), hold_lock_until_process_exit=True))
