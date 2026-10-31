#!/usr/bin/env python3
"""Authorized packaged Recommended + Autonomous acceptance for Plunder.

Run this only against an already-built release. The three environment variables
below are deliberate, exact authorization gates; this driver never builds a
release and refuses to replace a daemon it did not start.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

from merlin_tui.local_control import LocalControlClient

LIVE_TARGET = "https://plunder.me"
LIVE_OBJECTIVE = (
    "Run a Merlin security assessment against The Bank on https://plunder.me, Level 1. "
    "Discover the reviewed target's security weaknesses and produce an evidence-grounded report."
)
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
TASK_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "quarantined"})


class AcceptanceFailure(AssertionError):
    """A live acceptance invariant was not satisfied."""


def _require(condition: object, message: str) -> None:
    if not condition:
        raise AcceptanceFailure(message)


def _plain(value: object) -> object:
    if isinstance(value, SimpleNamespace):
        return {key: _plain(item) for key, item in vars(value).items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _release_binary() -> Path:
    if os.environ.get("MERLIN_RUN_LIVE_ACCEPTANCE") != "1":
        raise RuntimeError("set MERLIN_RUN_LIVE_ACCEPTANCE=1 for live acceptance")
    if os.environ.get("MERLIN_LIVE_AUTHORIZED_TARGET") != LIVE_TARGET:
        raise RuntimeError(
            "live acceptance requires the exact authorized target https://plunder.me"
        )
    raw = os.environ.get("MERLIN_LIVE_RELEASE_BINARY")
    if not raw:
        raise RuntimeError("live acceptance requires MERLIN_LIVE_RELEASE_BINARY")
    binary = Path(raw).expanduser()
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError("MERLIN_LIVE_RELEASE_BINARY must be an absolute executable file")
    return binary.resolve()


def _composition() -> str:
    return os.environ.get("MERLIN_LIVE_COMPOSITION", "autonomous")


def _assert_autonomous(status: Mapping[str, object]) -> None:
    policy = status.get("dynamic_specialist_policy")
    _require(isinstance(policy, Mapping), "campaign omitted dynamic specialist policy")
    _require(
        policy.get("specialist_composition") == _composition(),
        "campaign composition changed",
    )


async def _launcher(binary: Path, *arguments: str, timeout_seconds: float = 120) -> str:
    process = await asyncio.create_subprocess_exec(
        str(binary),
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        process.terminate()
        await process.wait()
        raise AcceptanceFailure(
            f"packaged command timed out: {binary.name} {' '.join(arguments)}"
        ) from None
    _require(
        process.returncode == 0,
        f"packaged command failed ({process.returncode}): {' '.join(arguments)}\n"
        f"stdout={stdout.decode(errors='replace')}\n"
        f"stderr={stderr.decode(errors='replace')}",
    )
    return stdout.decode(errors="replace")


async def _start_owned_release(binary: Path) -> LocalControlClient:
    state = (await _launcher(binary, "status")).strip()
    _require(
        state == "stopped",
        "a Merlin daemon is already running; refusing to replace or stop an unrelated owner",
    )
    await _launcher(binary, "start")
    return LocalControlClient(owns_daemon=True)


async def _claim_owned_lease(owner: LocalControlClient, timeout_seconds: float = 120) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        try:
            await owner.acquire_foreground_lease()
            return
        except RuntimeError as error:
            if (
                "already has a foreground owner" not in str(error)
                or asyncio.get_running_loop().time() >= deadline
            ):
                raise
            await asyncio.sleep(0.05)


async def _restart_owned_release(binary: Path, owner: LocalControlClient) -> LocalControlClient:
    await owner.shutdown()
    state = (await _launcher(binary, "status")).strip()
    _require(state == "stopped", "owned daemon did not release its foreground lease")
    await _launcher(binary, "start")
    replacement = LocalControlClient(owns_daemon=True)
    await replacement.acquire_foreground_lease()
    return replacement


async def _start_owned_watchdog(binary: Path, campaign_id: str) -> asyncio.subprocess.Process:
    """Start one direct monitor child whose lifetime belongs to this runner."""
    watchdog = Path(__file__).with_name("campaign_watchdog.py")
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(watchdog),
        "--campaign",
        campaign_id,
        "--interval",
        "30",
        "--fail-on-stall",
        "--stall-confirmations",
        "2",
        "--merlin",
        str(binary),
    )


async def _stop_owned_watchdog(process: asyncio.subprocess.Process | None) -> None:
    """TERM and reap the runner-owned monitor, escalating only if it will not exit."""
    if process is None:
        return
    if process.returncode is None:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()


async def _wait_for_terminal(
    client: LocalControlClient,
    campaign_id: str,
    *,
    timeout_seconds: float,
    max_extensions: int,
    watchdog: asyncio.subprocess.Process | None = None,
) -> dict[str, object]:
    handled: set[str] = set()
    extensions = 0
    terminal_fingerprint: tuple[object, object, object] | None = None
    try:
        async with asyncio.timeout(timeout_seconds):
            while True:
                if watchdog is not None:
                    _assert_watchdog_healthy(watchdog)
                status = _plain(await client.campaign_status(campaign_id))
                _require(isinstance(status, dict), "campaign status is not an object")
                _assert_autonomous(status)
                if status.get("status") in TERMINAL_STATUSES:
                    _assert_terminal_completion_authority(status)
                    observed = (
                        status.get("status"),
                        status.get("stop_reason"),
                        status.get("completion_digest"),
                    )
                    if observed == terminal_fingerprint:
                        return status
                    terminal_fingerprint = observed
                    await asyncio.sleep(1)
                    continue
                terminal_fingerprint = None
                decision = status.get("saturation_decision")
                if (
                    isinstance(decision, Mapping)
                    and decision.get("state") == "awaiting_operator"
                    and decision.get("purpose") == "coverage_extension"
                ):
                    request_id = str(decision.get("request_id") or "coverage-extension")
                    if request_id not in handled:
                        action = "continue" if extensions < max_extensions else "stop"
                        await client.decide_campaign_saturation(campaign_id, action, None)
                        if action == "continue":
                            extensions += 1
                        handled.add(request_id)
                await asyncio.sleep(1)
    except TimeoutError:
        raise AcceptanceFailure(
            f"campaign {campaign_id} missed its terminal deadline; "
            "campaign preserved for scoped pause"
        ) from None


async def _pause_owned_campaign(client: LocalControlClient, campaign_id: str) -> None:
    """Pause interrupted executable work while tolerating a completed state race."""
    status = _plain(await client.campaign_status(campaign_id))
    _require(isinstance(status, dict), "cleanup campaign status is not an object")
    state = status.get("status")
    if state in TERMINAL_STATUSES or state == "paused":
        return
    if state == "prepared":
        await client.cancel_campaign(campaign_id)
        return
    _require(state == "executing", f"cannot clean campaign in unexpected state {state!r}")
    try:
        await client.pause_campaign(campaign_id)
    except Exception:
        refreshed = _plain(await client.campaign_status(campaign_id))
        if isinstance(refreshed, dict) and (
            refreshed.get("status") in TERMINAL_STATUSES or refreshed.get("status") == "paused"
        ):
            return
        raise


async def _database_json(binary: Path, sql: str) -> tuple[dict[str, object], ...]:
    preamble = "\\pset tuples_only on\n\\pset format unaligned\n\\pset pager off\n"
    process = await asyncio.create_subprocess_exec(
        str(binary),
        "database",
        "shell",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate((preamble + sql).encode())
    _require(process.returncode == 0, f"report query failed: {stderr.decode(errors='replace')}")
    noise = {"Pager usage is off.", "Output format is unaligned.", "Tuples only is on."}
    rows: list[dict[str, object]] = []
    for line in stdout.decode(errors="replace").splitlines():
        value = line.strip()
        if not value or value in noise:
            continue
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise AcceptanceFailure(f"database shell emitted non-JSON output: {value}") from error
        _require(isinstance(decoded, dict), "report query returned a non-object row")
        rows.append(decoded)
    return tuple(rows)


async def _report_rows(binary: Path, campaign_id: str) -> tuple[dict[str, object], ...]:
    escaped = campaign_id.replace("'", "''")
    return await _database_json(
        binary,
        "SELECT projection::text FROM merlin_storage.report_revisions "
        f"WHERE campaign_id = '{escaped}' ORDER BY revision;\n",
    )


def _campaign_tasks(status: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    waves = status.get("waves")
    _require(
        isinstance(waves, Sequence) and not isinstance(waves, (str, bytes)) and waves,
        "terminal campaign has no execution waves",
    )
    tasks: list[Mapping[str, object]] = []
    for wave in waves:
        _require(isinstance(wave, Mapping), "campaign wave is not an object")
        wave_tasks = wave.get("tasks")
        _require(
            isinstance(wave_tasks, Sequence) and not isinstance(wave_tasks, (str, bytes)),
            "campaign wave omitted tasks",
        )
        tasks.extend(task for task in wave_tasks if isinstance(task, Mapping))
    _require(tasks, "terminal campaign has no execution tasks")
    return tuple(tasks)


def _assert_terminal_completion_authority(status: Mapping[str, object]) -> None:
    digest = status.get("completion_digest")
    _require(
        isinstance(digest, str)
        and len(digest) == 71
        and digest.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in digest[7:]),
        "terminal campaign completion digest is missing or malformed",
    )
    completion = status.get("completion_document")
    _require(isinstance(completion, Mapping), "campaign omitted its completion document")
    projected_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(completion, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    _require(
        projected_digest == digest,
        "terminal campaign completion document does not match its digest",
    )


def _assert_terminal_campaign(status: Mapping[str, object], campaign_id: str) -> None:
    _require(status.get("campaign_id") == campaign_id, "campaign status identity changed")
    _require(status.get("status") == "completed", "campaign did not complete")
    _require(
        status.get("stop_reason")
        in {"saturated", "completed_with_limitations", "operator_stopped"},
        "campaign has an invalid terminal stop reason",
    )
    _assert_autonomous(status)
    _assert_terminal_completion_authority(status)
    _require(int(status.get("lead_events") or 0) > 0, "campaign recorded no lead events")
    tasks = _campaign_tasks(status)
    _require(
        all(task.get("status") in TASK_TERMINAL_STATUSES for task in tasks),
        "terminal campaign contains non-terminal execution tasks",
    )
    completion = status.get("completion_document")
    _require(isinstance(completion, Mapping), "campaign omitted its completion document")
    _require(completion.get("campaign_id") == campaign_id, "completion identity changed")
    _require(completion.get("profile") == "recommended", "completion profile changed")
    _require(bool(completion.get("target_id")), "completion omitted target identity")
    cleanup = completion.get("cleanup")
    authority = cleanup.get("authority") if isinstance(cleanup, Mapping) else None
    _require(
        isinstance(authority, Mapping)
        and authority.get("status") in {"acknowledged", "not_applicable"},
        "terminal campaign cleanup lacks canonical acknowledgement",
    )
    saturation = completion.get("saturation")
    if not (isinstance(saturation, Mapping) and saturation.get("execution_complete") is True):
        # A campaign sealed as completed_with_limitations honestly reports
        # unfinished execution (e.g. provider-truncated workers); require the
        # limitations to be on record rather than demanding perfect coverage.
        _require(
            status.get("stop_reason") == "completed_with_limitations",
            "completion does not prove execution finished",
        )
        _require(
            completion.get("limitations") or completion.get("unsearched_frontier"),
            "limited completion omitted its limitations",
        )
    specialists = status.get("dynamic_specialists")
    _require(
        isinstance(specialists, Sequence)
        and not isinstance(specialists, (str, bytes))
        and len(specialists) > 0,
        "autonomous campaign instantiated no dynamic specialists",
    )


def _assert_scoped_objective(value: object) -> str:
    _require(isinstance(value, str) and value.strip(), "campaign objective is empty")
    objective = value.strip()
    normalized = objective.casefold()
    required_terms = (
        "plunder.me",
        "the bank",
        "level 1",
    )
    missing = [term for term in required_terms if term not in normalized]
    _require(not missing, f"campaign objective lost required scope: {missing!r}")
    return objective


def _assert_report_rows(
    rows: Sequence[Mapping[str, object]],
    campaign_id: str,
    objective: str,
    terminal: Mapping[str, object],
) -> tuple[str, ...]:
    _require(rows, "terminal campaign produced no durable report revisions")
    report_ids: list[str] = []
    for row in rows:
        _require(row.get("campaign_id") == campaign_id, "report campaign identity changed")
        report_id = row.get("revision_id")
        _require(isinstance(report_id, str) and report_id, "report omitted revision ID")
        report_ids.append(report_id)
        _require(
            row.get("stop_reason") == terminal.get("stop_reason"),
            "report stop reason differs from canonical terminal state",
        )
        _require(row.get("cleanup_status") == "completed", "report cleanup is incomplete")
        _require(
            row.get("report_state") in {"complete", "provisional"},
            "report is neither complete nor a limitations-bearing provisional report",
        )
        metadata = row.get("metadata")
        _require(isinstance(metadata, Mapping), "report omitted metadata")
        _require(
            metadata.get("specialist_composition") == _composition(),
            "report composition changed",
        )
        _require(metadata.get("target") == LIVE_TARGET, "report target provenance changed")
        _require(metadata.get("objective") == objective, "report objective provenance changed")
        _require(
            metadata.get("completion_digest") == terminal.get("completion_digest"),
            "report completion evidence differs from canonical terminal state",
        )
        for field in ("tasks", "techniques", "exchanges"):
            values = row.get(field)
            _require(
                isinstance(values, Sequence)
                and not isinstance(values, (str, bytes))
                and len(values) > 0,
                f"report has no substantive {field}",
            )
        evidence = row.get("evidence")
        _require(
            isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes)),
            "report evidence is not an array",
        )
        findings = row.get("findings", ())
        _require(
            isinstance(findings, Sequence) and not isinstance(findings, (str, bytes)),
            "report findings is not an array",
        )
        if findings:
            _require(evidence, "report findings have no supporting evidence citations")
    return tuple(report_ids)


def _assert_watchdog_healthy(process: asyncio.subprocess.Process | None) -> None:
    _require(process is not None, "owned campaign watchdog was not started")
    _require(
        process.returncode in {None, 0},
        f"owned campaign watchdog failed before acceptance completed: {process.returncode}",
    )


async def _run(args: argparse.Namespace) -> None:
    binary = _release_binary()
    owner: LocalControlClient | None = None
    campaign_id: str | None = None
    campaign_session_id: str | None = None
    existing_ids: set[str] = set()
    inventory_loaded = False
    completed = False
    watchdog: asyncio.subprocess.Process | None = None
    client = LocalControlClient()
    try:
        owner = await _start_owned_release(binary)
        existing_rows = _plain(await client.list_campaigns())
        _require(isinstance(existing_rows, list), "campaign list is not an array")
        existing_ids = {str(row.get("id")) for row in existing_rows if isinstance(row, Mapping)}
        inventory_loaded = True

        # Typed CLI switches select the profile and composition. --approve is scoped by
        # the CLI to the one newly admitted campaign and its durable manifest digest.
        await _launcher(
            binary,
            "campaign",
            "submit",
            "--profile",
            "recommended",
            "--composition",
            _composition(),
            "--approve",
            LIVE_OBJECTIVE,
            timeout_seconds=args.approval_timeout,
        )
        await _claim_owned_lease(owner)

        rows = _plain(await client.list_campaigns())
        _require(isinstance(rows, list), "campaign list is not an array")
        created = [
            row
            for row in rows
            if isinstance(row, Mapping) and str(row.get("id")) not in existing_ids
        ]
        _require(len(created) == 1, f"expected one newly submitted campaign, found {created!r}")
        campaign_id = str(created[0].get("id") or "")
        _require(bool(campaign_id), "submitted campaign omitted its ID")
        campaign_session_id = str(created[0].get("session_id") or "")
        _require(bool(campaign_session_id), "submitted campaign omitted its session ID")
        _require(created[0].get("profile") == "recommended", "campaign profile changed")
        campaign_objective = _assert_scoped_objective(created[0].get("objective"))
        active = _plain(await client.campaign_status(campaign_id))
        _require(isinstance(active, dict), "campaign status is not an object")
        _assert_autonomous(active)
        print(f"CAMPAIGN_ID={campaign_id}", flush=True)
        async with asyncio.timeout(args.restart_observation_timeout):
            while True:
                active = _plain(await client.campaign_status(campaign_id))
                _require(isinstance(active, dict), "campaign status is not an object")
                _assert_autonomous(active)
                if active.get("status") in {"failed", "cancelled"}:
                    raise AcceptanceFailure(f"campaign stopped before restart: {active!r}")
                if int(active.get("lead_events") or 0) > 0 and active.get("waves"):
                    break
                await asyncio.sleep(1)

        owner = await _restart_owned_release(binary, owner)
        async for event in client.stream(f"/resume {campaign_session_id}"):
            _require(getattr(event, "kind", "") != "error", f"resume failed: {event!r}")
        restored = _plain(await client.campaign_status(campaign_id))
        _require(isinstance(restored, dict), "restored campaign status is not an object")
        _assert_autonomous(restored)
        # Start monitoring only after the deliberate restart.  Otherwise the
        # watchdog sees the runner-owned PostgreSQL shutdown and exits even
        # though no campaign failure occurred.
        watchdog = await _start_owned_watchdog(binary, campaign_id)

        terminal = await _wait_for_terminal(
            client,
            campaign_id,
            timeout_seconds=args.terminal_timeout,
            max_extensions=args.max_extensions,
            watchdog=watchdog,
        )
        _assert_terminal_campaign(terminal, campaign_id)
        report_rows = await _report_rows(binary, campaign_id)
        refreshed_terminal = _plain(await client.campaign_status(campaign_id))
        _require(isinstance(refreshed_terminal, dict), "refreshed terminal status is not an object")
        _assert_terminal_campaign(refreshed_terminal, campaign_id)
        _require(
            (
                refreshed_terminal.get("status"),
                refreshed_terminal.get("stop_reason"),
                refreshed_terminal.get("completion_digest"),
            )
            == (
                terminal.get("status"),
                terminal.get("stop_reason"),
                terminal.get("completion_digest"),
            ),
            "campaign terminal authority changed while validating its report",
        )
        report_ids = _assert_report_rows(
            report_rows, campaign_id, campaign_objective, refreshed_terminal
        )
        for report_id in report_ids:
            print(f"REPORT_ID={report_id}", flush=True)

        _assert_watchdog_healthy(watchdog)
        await _stop_owned_watchdog(watchdog)
        watchdog = None
        owner = await _restart_owned_release(binary, owner)
        async for event in client.stream(f"/resume {campaign_session_id}"):
            _require(getattr(event, "kind", "") != "error", f"resume failed: {event!r}")
        durable = _plain(await client.campaign_status(campaign_id))
        _require(isinstance(durable, dict), "durable campaign status is not an object")
        _assert_terminal_campaign(durable, campaign_id)
        _require(
            _assert_report_rows(
                await _report_rows(binary, campaign_id),
                campaign_id,
                campaign_objective,
                durable,
            )
            == report_ids,
            "report revision identities changed after restart",
        )
        completed = True
        print("PACKAGED_AUTONOMOUS_PLUNDER_ACCEPTANCE_OK", flush=True)
    finally:
        try:
            await _stop_owned_watchdog(watchdog)
        except Exception as error:
            print(f"OWNED_WATCHDOG_CLEANUP_FAILED error={error!r}")
        if campaign_id is None and inventory_loaded:
            try:
                rows = _plain(await client.list_campaigns())
                if isinstance(rows, list):
                    candidates = [
                        str(row.get("id"))
                        for row in rows
                        if isinstance(row, Mapping) and str(row.get("id")) not in existing_ids
                    ]
                    if len(candidates) == 1:
                        campaign_id = candidates[0]
            except Exception:
                pass
        if campaign_id is not None and not completed:
            try:
                if campaign_session_id:
                    async for event in client.stream(f"/resume {campaign_session_id}"):
                        _require(
                            getattr(event, "kind", "") != "error",
                            f"cleanup resume failed: {event!r}",
                        )
                await _pause_owned_campaign(client, campaign_id)
            except Exception as error:
                print(f"SCOPED_CLEANUP_FAILED campaign={campaign_id} error={error!r}")
        if owner is not None:
            try:
                await owner.shutdown()
            except Exception as error:
                print(f"OWNED_DAEMON_CLEANUP_FAILED error={error!r}")
            try:
                await _launcher(binary, "storage", "stop")
            except Exception as error:
                print(f"OWNED_STORAGE_CLEANUP_FAILED error={error!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terminal-timeout", type=float, default=10_800)
    parser.add_argument("--approval-timeout", type=float, default=1_800)
    parser.add_argument("--restart-observation-timeout", type=float, default=900)
    parser.add_argument("--max-extensions", type=int, default=3)
    args = parser.parse_args()
    if (
        min(
            args.terminal_timeout,
            args.approval_timeout,
            args.restart_observation_timeout,
        )
        <= 0
    ):
        parser.error("timeouts must be positive")
    if args.max_extensions < 0:
        parser.error("extension count must be non-negative")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        # ``asyncio.run`` first cancels and awaits ``_run``, so its scoped
        # pause/watchdog/daemon/storage cleanup has already completed here.
        # Keep an operator interrupt concise and preserve the conventional
        # shell exit status without printing a Python traceback.
        print("Acceptance interrupted.", file=sys.stderr)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
