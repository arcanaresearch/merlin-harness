#!/usr/bin/env python3
"""Run one authorized live benchmark through Merlin's public campaign CLI.

This is benchmark tooling, not product code.  Review preparation, durable
manifest validation, and approval remain owned by ``merlin campaign submit
--approve``.  This driver only owns the daemon it starts, identifies the one
new campaign, waits for its terminal state and report, and records the result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol
from urllib.parse import urlsplit

LIVE_FLAG = "MERLIN_RUN_LIVE_BENCHMARK"
AUTHORIZED_ORIGIN = "MERLIN_LIVE_AUTHORIZED_ORIGIN"
AUTHORIZED_TARGET = "MERLIN_LIVE_AUTHORIZED_TARGET"
EXECUTABLE = "MERLIN_LIVE_RELEASE_BINARY"
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "paused"})
REPORT_STATES = frozenset({"complete", "provisional", "incomplete"})
DURABLE_READY_STATUS = re.compile(r"ready · [^·\r\n]+ · cleanup [^·\r\n]+")


class BenchmarkFailure(RuntimeError):
    """The benchmark could not prove a required safety or result invariant."""


class BenchmarkCleanupFailure(BenchmarkFailure):
    """A benchmark failure was followed by a cleanup failure."""

    def __init__(self, primary: Exception, cleanup: Exception) -> None:
        self.primary = primary
        self.cleanup = cleanup
        super().__init__(
            f"benchmark failed: {type(primary).__name__}: {primary}; "
            f"cleanup also failed: {type(cleanup).__name__}: {cleanup}"
        )


class ControlClient(Protocol):
    async def acquire_foreground_lease(self) -> None: ...
    async def list_campaigns(self) -> object: ...
    async def campaign_status(self, campaign_id: str) -> object: ...
    async def list_reports(self, *, session_only: bool = False) -> object: ...
    async def stream(self, prompt: str) -> AsyncIterator[object]: ...
    async def shutdown(self) -> None: ...


Launcher = Callable[..., Awaitable[str]]
ClientFactory = Callable[..., ControlClient]


def _plain(value: object) -> object:
    if isinstance(value, SimpleNamespace):
        return {key: _plain(item) for key, item in vars(value).items()}
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _normalized_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise BenchmarkFailure(
            "benchmark origin must be an HTTP(S) origin without path or userinfo"
        )
    scheme = parsed.scheme.casefold()
    try:
        port = parsed.port
    except ValueError as error:
        raise BenchmarkFailure("benchmark origin has an invalid port") from error
    default_port = 443 if scheme == "https" else 80
    port_suffix = "" if port in {None, default_port} else f":{port}"
    return f"{scheme}://{parsed.hostname.casefold()}{port_suffix}"


def _canonical_target(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise BenchmarkFailure("benchmark target must be an HTTP(S) URL without userinfo")
    origin = _normalized_origin(f"{parsed.scheme}://{parsed.netloc}")
    return f"{origin}{parsed.path}"


def authorized_executable(origin: str, target: str) -> Path:
    """Require explicit live authorization for this exact target and binary."""
    if os.environ.get(LIVE_FLAG) != "1":
        raise BenchmarkFailure(f"set {LIVE_FLAG}=1 for a live benchmark")
    requested_origin = _normalized_origin(origin)
    raw_authorized_origin = os.environ.get(AUTHORIZED_ORIGIN, "")
    try:
        authorized_origin = _normalized_origin(raw_authorized_origin)
    except BenchmarkFailure:
        authorized_origin = ""
    if authorized_origin != requested_origin:
        raise BenchmarkFailure(
            f"{AUTHORIZED_ORIGIN} must authorize the normalized origin {requested_origin!r}"
        )
    requested_target = _canonical_target(target)
    raw_authorized_target = os.environ.get(AUTHORIZED_TARGET, "")
    try:
        authorized_target = _canonical_target(raw_authorized_target)
    except BenchmarkFailure:
        authorized_target = ""
    if authorized_target != requested_target:
        raise BenchmarkFailure(
            f"{AUTHORIZED_TARGET} must authorize the exact target {requested_target!r}"
        )
    raw = os.environ.get(EXECUTABLE)
    if not raw:
        raise BenchmarkFailure(f"set {EXECUTABLE} to the release executable")
    binary = Path(raw).expanduser()
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise BenchmarkFailure(f"{EXECUTABLE} must be an absolute executable file")
    return binary.resolve()


def _validate_objective_origin(origin: str, target: str, objective: str) -> None:
    """Bind every URL in the live objective to the authorized origin and exact target."""
    authorized_origin = _normalized_origin(origin)
    raw_urls = [value.rstrip(".,;:)]}") for value in re.findall(r"https?://\S+", objective)]
    urls = [urlsplit(value) for value in raw_urls]
    if not urls:
        raise BenchmarkFailure("the benchmark objective must name the authorized origin")
    try:
        observed = {_normalized_origin(f"{value.scheme}://{value.netloc}") for value in urls}
    except BenchmarkFailure as error:
        raise BenchmarkFailure("the benchmark objective contains an invalid target URL") from error
    if observed != {authorized_origin}:
        raise BenchmarkFailure(
            "every URL in the benchmark objective must use the authorized benchmark origin"
        )
    try:
        observed_targets = {_canonical_target(value) for value in raw_urls}
    except BenchmarkFailure as error:
        raise BenchmarkFailure("the benchmark objective contains an invalid target URL") from error
    if observed_targets != {_canonical_target(target)}:
        raise BenchmarkFailure(
            "every URL in the benchmark objective must equal the exact authorized target"
        )


async def _launcher(binary: Path, *arguments: str, timeout_seconds: float) -> str:
    process = await asyncio.create_subprocess_exec(
        str(binary),
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except TimeoutError:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()
        raise BenchmarkFailure(f"command timed out: {binary.name} {' '.join(arguments)}") from None
    if process.returncode != 0:
        raise BenchmarkFailure(
            f"command failed ({process.returncode}): {' '.join(arguments)}\n"
            f"stdout={stdout.decode(errors='replace')}\n"
            f"stderr={stderr.decode(errors='replace')}"
        )
    return stdout.decode(errors="replace")


def _default_client_factory(*, owns_daemon: bool = False) -> ControlClient:
    from merlin_tui.local_control import LocalControlClient

    # No confirmer is supplied. Any permission not narrowly handled by the
    # public campaign submit --approve flow is therefore denied by the daemon.
    return LocalControlClient(owns_daemon=owns_daemon)


async def _start_owned(
    binary: Path, launcher: Launcher, client_factory: ClientFactory, command_deadline: float
) -> ControlClient:
    state = (await launcher(binary, "status", timeout_seconds=command_deadline)).strip()
    if state != "stopped":
        raise BenchmarkFailure(
            "a Merlin daemon is already running; refusing to replace or stop an unrelated owner"
        )
    await launcher(binary, "start", timeout_seconds=command_deadline)
    # Do not claim the exclusive foreground lease here. The public campaign
    # submit CLI claims it while binding review and execution to one session.
    return client_factory(owns_daemon=True)


async def _claim_owned_lease(client: ControlClient, deadline_seconds: float) -> None:
    """Claim the CLI's released lease, allowing only its bounded EOF handoff race."""
    deadline = time.monotonic() + deadline_seconds
    while True:
        try:
            await client.acquire_foreground_lease()
            return
        except RuntimeError as error:
            if "already has a foreground owner" not in str(error) or time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.05)


async def _restart_owned(
    binary: Path,
    owner: ControlClient,
    launcher: Launcher,
    client_factory: ClientFactory,
    command_deadline: float,
) -> ControlClient:
    await owner.shutdown()
    state = (await launcher(binary, "status", timeout_seconds=command_deadline)).strip()
    if state != "stopped":
        raise BenchmarkFailure("the owned daemon did not stop cleanly")
    await launcher(binary, "start", timeout_seconds=command_deadline)
    replacement = client_factory(owns_daemon=True)
    await replacement.acquire_foreground_lease()
    return replacement


def _campaign_rows(value: object) -> list[dict[str, object]]:
    plain = _plain(value)
    if not isinstance(plain, list) or not all(isinstance(item, dict) for item in plain):
        raise BenchmarkFailure("campaign inventory is not an array of objects")
    return plain


def _new_campaign(
    before: Sequence[Mapping[str, object]], after: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    previous = {str(row.get("id")) for row in before}
    created = [dict(row) for row in after if str(row.get("id")) not in previous]
    if len(created) != 1:
        raise BenchmarkFailure(f"expected exactly one newly admitted campaign, found {created!r}")
    return created[0]


def _budget_projection(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        budget = value.get("budget")
        if isinstance(budget, Mapping):
            if "reviewed_limits" in budget:
                reviewed = budget.get("reviewed_limits")
                if not isinstance(reviewed, Mapping):
                    raise BenchmarkFailure("campaign reviewed budget projection is malformed")
                return reviewed
            limits = budget.get("limits")
            if isinstance(limits, Mapping):
                return limits
            return budget
        review = value.get("review")
        if isinstance(review, Mapping) and isinstance(review.get("budget"), Mapping):
            return review["budget"]  # type: ignore[return-value]
    return None


def _validate_campaign_binding(
    row: Mapping[str, object],
    status: Mapping[str, object],
    *,
    objective: str,
    profile: str,
    composition: str,
    limits: Mapping[str, int | float],
) -> tuple[str, str]:
    campaign_id = row.get("id")
    session_id = row.get("session_id")
    if not isinstance(campaign_id, str) or not campaign_id:
        raise BenchmarkFailure("new campaign omitted its ID")
    if not isinstance(session_id, str) or not session_id:
        raise BenchmarkFailure("new campaign omitted its owning session")
    if status.get("campaign_id") != campaign_id:
        raise BenchmarkFailure("campaign status identity does not match the new inventory row")
    if row.get("objective") != objective:
        raise BenchmarkFailure(
            "prepared campaign objective drifted from the exact benchmark objective"
        )
    if row.get("profile") != profile:
        raise BenchmarkFailure("prepared campaign profile drifted from the selected profile")
    policy = status.get("dynamic_specialist_policy")
    actual_composition = (
        policy.get("specialist_composition") if isinstance(policy, Mapping) else None
    )
    if actual_composition != composition:
        raise BenchmarkFailure(
            "prepared campaign composition drifted from the selected composition"
        )
    row_digest = row.get("manifest_digest")
    status_digest = status.get("manifest_digest")
    if row_digest is not None and status_digest is not None and row_digest != status_digest:
        raise BenchmarkFailure("campaign inventory and status disagree on the manifest digest")
    if limits:
        budget = _budget_projection(status)
        if budget is None:
            budget = _budget_projection(row)
        if budget is None:
            raise BenchmarkFailure("campaign omitted the selected benchmark budget")
        for name, expected in limits.items():
            if budget.get(name) != expected:
                raise BenchmarkFailure(
                    f"campaign budget drifted for {name}: expected {expected!r}, "
                    f"received {budget.get(name)!r}"
                )
    return campaign_id, session_id


async def _resume(client: ControlClient, session_id: str) -> None:
    async for event in client.stream(f"/resume {session_id}"):
        kind = str(getattr(event, "kind", ""))
        text = str(getattr(event, "text", ""))
        if kind == "user_input":
            raise BenchmarkFailure(f"campaign requested post-review operator input: {text}")
        if kind == "error":
            raise BenchmarkFailure(f"campaign session resume failed: {text}")


async def _wait_terminal(
    client: ControlClient, campaign_id: str, deadline_seconds: float, poll_interval: float
) -> dict[str, object]:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        status = _plain(await client.campaign_status(campaign_id))
        if not isinstance(status, dict):
            raise BenchmarkFailure("campaign status is not an object")
        state = status.get("status")
        if state in TERMINAL_STATUSES:
            if state != "completed":
                raise BenchmarkFailure(f"campaign ended without completion: {state!r}")
            return status
        await asyncio.sleep(poll_interval)
    raise BenchmarkFailure(f"campaign {campaign_id} missed its terminal deadline")


def _report_is_ready(report: Mapping[str, object]) -> bool:
    durable = report.get("format") == "durable-revision"
    report_state = report.get("report_state")
    if durable and report_state not in (None, ""):
        return isinstance(report_state, str) and report_state in REPORT_STATES
    status = report.get("status")
    if status == "ready":
        return True
    if not durable or not isinstance(status, str):
        return False
    return DURABLE_READY_STATUS.fullmatch(status) is not None


async def _wait_reports(
    client: ControlClient, deadline_seconds: float, poll_interval: float
) -> list[dict[str, object]]:
    deadline = time.monotonic() + deadline_seconds
    last: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        plain = _plain(await client.list_reports(session_only=True))
        if not isinstance(plain, list) or not all(isinstance(item, dict) for item in plain):
            raise BenchmarkFailure("report inventory is not an array of objects")
        last = plain
        if last and all(_report_is_ready(item) for item in last):
            return last
        await asyncio.sleep(poll_interval)
    raise BenchmarkFailure(f"report remained pending until the deadline: {last!r}")


def _submit_arguments(args: argparse.Namespace) -> list[str]:
    values = [
        "campaign",
        "submit",
        "--profile",
        args.profile,
        "--composition",
        args.composition,
        "--approve",
        "--strict-objective",
    ]
    for name in ("turns", "tokens", "seconds", "cost_usd"):
        value = getattr(args, f"max_{name}")
        if value is not None:
            values.extend((f"--max-{name.replace('_', '-')}", str(value)))
    values.append(args.objective)
    return values


async def run(
    args: argparse.Namespace,
    *,
    launcher: Launcher = _launcher,
    client_factory: ClientFactory = _default_client_factory,
) -> dict[str, object]:
    _validate_objective_origin(args.target_origin, args.target_url, args.objective)
    binary = authorized_executable(args.target_origin, args.target_url)
    owner: ControlClient | None = None
    owner_has_lease = False
    result: dict[str, object] | None = None
    primary_error: Exception | None = None
    try:
        owner = await _start_owned(binary, launcher, client_factory, args.command_timeout)
        before = _campaign_rows(await owner.list_campaigns())
        await launcher(
            binary,
            *_submit_arguments(args),
            timeout_seconds=args.approval_timeout,
        )
        # The CLI process has exited and released its exclusive lease. Claim it
        # now so all monitoring, restart, and cleanup belongs to this runner.
        await _claim_owned_lease(owner, args.command_timeout)
        owner_has_lease = True
        row = _new_campaign(before, _campaign_rows(await owner.list_campaigns()))
        campaign_id = str(row.get("id") or "")
        status = _plain(await owner.campaign_status(campaign_id))
        if not isinstance(status, dict):
            raise BenchmarkFailure("campaign status is not an object")
        limits = {
            name: value
            for name in ("turns", "tokens", "seconds", "cost_usd")
            if (value := getattr(args, f"max_{name}")) is not None
        }
        campaign_id, session_id = _validate_campaign_binding(
            row,
            status,
            objective=args.objective,
            profile=args.profile,
            composition=args.composition,
            limits=limits,
        )
        terminal = await _wait_terminal(
            owner, campaign_id, args.terminal_timeout, args.poll_interval
        )
        await _resume(owner, session_id)
        reports = await _wait_reports(owner, args.report_timeout, args.poll_interval)
        if args.verify_restart:
            owner = await _restart_owned(
                binary, owner, launcher, client_factory, args.command_timeout
            )
            owner_has_lease = True
            await _resume(owner, session_id)
            durable = _plain(await owner.campaign_status(campaign_id))
            if not isinstance(durable, dict) or durable.get("status") != "completed":
                raise BenchmarkFailure("completed campaign did not survive owned daemon restart")
            reports = await _wait_reports(owner, args.report_timeout, args.poll_interval)
            terminal = durable
        result = {
            "campaign_id": campaign_id,
            "session_id": session_id,
            "target_origin": _normalized_origin(args.target_origin),
            "objective": args.objective,
            "terminal": terminal,
            "reports": reports,
        }
    except Exception as error:
        primary_error = error
    finally:
        cleanup_error: Exception | None = None
        if owner is not None:
            try:
                if not owner_has_lease:
                    await _claim_owned_lease(owner, args.command_timeout)
                await owner.shutdown()
            except Exception as error:
                cleanup_error = error
        if primary_error is not None and cleanup_error is not None:
            raise BenchmarkCleanupFailure(primary_error, cleanup_error) from primary_error
        if primary_error is not None:
            raise primary_error
        if cleanup_error is not None:
            raise BenchmarkFailure(f"benchmark cleanup failed: {cleanup_error}") from cleanup_error
    assert result is not None
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-origin", required=True)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--objective", required=True)
    parser.add_argument("--profile", default="recommended")
    parser.add_argument("--composition", default="autonomous")
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--max-seconds", type=int)
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--command-timeout", type=float, default=120)
    parser.add_argument("--approval-timeout", type=float, default=1_800)
    parser.add_argument("--terminal-timeout", type=float, default=10_800)
    parser.add_argument("--report-timeout", type=float, default=300)
    parser.add_argument("--poll-interval", type=float, default=1)
    parser.add_argument("--verify-restart", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if (
        min(
            args.command_timeout,
            args.approval_timeout,
            args.terminal_timeout,
            args.report_timeout,
            args.poll_interval,
        )
        <= 0
    ):
        raise SystemExit("timeouts and poll interval must be positive")
    try:
        result = asyncio.run(run(args))
    except BenchmarkFailure as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    encoded = json.dumps(result, indent=2, sort_keys=True, default=str) + "\n"
    print(encoded, end="")
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
