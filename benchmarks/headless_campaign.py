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
AUTHORIZED_TARGET = "MERLIN_LIVE_AUTHORIZED_TARGET"
EXECUTABLE = "MERLIN_LIVE_RELEASE_BINARY"
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "paused"})


class BenchmarkFailure(RuntimeError):
    """The benchmark could not prove a required safety or result invariant."""


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


def authorized_executable(target: str) -> Path:
    """Require explicit live authorization for this exact target and binary."""
    if os.environ.get(LIVE_FLAG) != "1":
        raise BenchmarkFailure(f"set {LIVE_FLAG}=1 for a live benchmark")
    if os.environ.get(AUTHORIZED_TARGET) != target:
        raise BenchmarkFailure(
            f"{AUTHORIZED_TARGET} must exactly equal the requested target {target!r}"
        )
    raw = os.environ.get(EXECUTABLE)
    if not raw:
        raise BenchmarkFailure(f"set {EXECUTABLE} to the release executable")
    binary = Path(raw).expanduser()
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise BenchmarkFailure(f"{EXECUTABLE} must be an absolute executable file")
    return binary.resolve()


def _validate_objective_target(target: str, objective: str) -> None:
    """Bind every URL in the live objective to the exactly authorized origin."""
    authorized = urlsplit(target)
    if authorized.scheme not in {"http", "https"} or not authorized.netloc:
        raise BenchmarkFailure("the authorized benchmark target must be an HTTP(S) URL")
    urls = [urlsplit(value.rstrip(".,;:)]}")) for value in re.findall(r"https?://\S+", objective)]
    if not urls:
        raise BenchmarkFailure("the benchmark objective must name the authorized target URL")
    authorized_origin = (authorized.scheme.casefold(), authorized.netloc.casefold())
    observed = {(value.scheme.casefold(), value.netloc.casefold()) for value in urls}
    if observed != {authorized_origin}:
        raise BenchmarkFailure(
            "every URL in the benchmark objective must use the exactly authorized target origin"
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
    owner = client_factory(owns_daemon=True)
    await owner.acquire_foreground_lease()
    return owner


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
        budget = _budget_projection(status) or _budget_projection(row)
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
        if last and all(str(item.get("status")) == "ready" for item in last):
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
    _validate_objective_target(args.target, args.objective)
    binary = authorized_executable(args.target)
    owner: ControlClient | None = None
    completed = False
    try:
        owner = await _start_owned(binary, launcher, client_factory, args.command_timeout)
        before = _campaign_rows(await owner.list_campaigns())
        await launcher(
            binary,
            *_submit_arguments(args),
            timeout_seconds=args.approval_timeout,
        )
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
            await _resume(owner, session_id)
            durable = _plain(await owner.campaign_status(campaign_id))
            if not isinstance(durable, dict) or durable.get("status") != "completed":
                raise BenchmarkFailure("completed campaign did not survive owned daemon restart")
            reports = await _wait_reports(owner, args.report_timeout, args.poll_interval)
            terminal = durable
        completed = True
        return {
            "campaign_id": campaign_id,
            "session_id": session_id,
            "target": args.target,
            "objective": args.objective,
            "terminal": terminal,
            "reports": reports,
        }
    finally:
        if owner is not None:
            try:
                await owner.shutdown()
            except Exception:
                if completed:
                    raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
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
