from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

ROOT = Path(__file__).parents[1]
DRIVER = Path(__file__).with_name("headless_campaign.py")
SPEC = importlib.util.spec_from_file_location("benchmark_headless_campaign", DRIVER)
assert SPEC is not None and SPEC.loader is not None
headless = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(headless)


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "target_origin": "https://benchmark.test",
        "objective": "Assess https://benchmark.test exactly",
        "profile": "recommended",
        "composition": "autonomous",
        "max_turns": 8,
        "max_tokens": None,
        "max_seconds": None,
        "max_cost_usd": None,
        "command_timeout": 1.0,
        "approval_timeout": 1.0,
        "terminal_timeout": 1.0,
        "report_timeout": 1.0,
        "poll_interval": 0.001,
        "verify_restart": False,
        "output": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _authorize(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    binary = tmp_path / "merlin"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o700)
    monkeypatch.setenv(headless.LIVE_FLAG, "1")
    monkeypatch.setenv(headless.AUTHORIZED_ORIGIN, "https://benchmark.test")
    monkeypatch.setenv(headless.EXECUTABLE, str(binary))
    return binary


class FakeClient:
    def __init__(
        self,
        *,
        campaigns: list[dict[str, object]] | None = None,
        statuses: list[dict[str, object]] | None = None,
        reports: list[object] | None = None,
        resume_events: list[object] | None = None,
    ) -> None:
        self.campaigns = campaigns or []
        self.statuses = statuses or []
        self.reports = reports or []
        self.resume_events = resume_events or []
        self.shutdown_calls = 0
        self.lease_calls = 0
        self.resume_prompts: list[str] = []

    async def acquire_foreground_lease(self) -> None:
        self.lease_calls += 1

    async def list_campaigns(self) -> object:
        return self.campaigns

    async def campaign_status(self, campaign_id: str) -> object:
        assert campaign_id == "campaign/new"
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0]

    async def list_reports(self, *, session_only: bool = False) -> object:
        assert session_only is True
        if len(self.reports) > 1:
            return self.reports.pop(0)
        return self.reports[0]

    async def stream(self, prompt: str):
        self.resume_prompts.append(prompt)
        for event in self.resume_events:
            yield event

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


def _row(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "campaign/new",
        "session_id": "session/new",
        "objective": "Assess https://benchmark.test exactly",
        "profile": "recommended",
        "manifest_digest": "sha256:one",
        "budget": {"turns": 8},
    }
    value.update(changes)
    return value


def _status(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "campaign_id": "campaign/new",
        "status": "executing",
        "manifest_digest": "sha256:one",
        "dynamic_specialist_policy": {"specialist_composition": "autonomous"},
        "review": {"budget": {"turns": 8}},
    }
    value.update(changes)
    return value


def test_requires_exact_live_flag_normalized_origin_and_absolute_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = _authorize(monkeypatch, tmp_path)
    assert headless.authorized_executable("https://BENCHMARK.test:443/") == binary.resolve()
    monkeypatch.setenv(headless.AUTHORIZED_ORIGIN, "https://other.test")
    with pytest.raises(headless.BenchmarkFailure, match="normalized origin"):
        headless.authorized_executable("https://benchmark.test")
    monkeypatch.setenv(headless.AUTHORIZED_ORIGIN, "https://benchmark.test")
    monkeypatch.setenv(headless.EXECUTABLE, "relative-merlin")
    with pytest.raises(headless.BenchmarkFailure, match="absolute executable"):
        headless.authorized_executable("https://benchmark.test")


def test_objective_is_bound_to_only_the_authorized_origin() -> None:
    headless._validate_objective_origin(
        "https://benchmark.test", "Assess https://benchmark.test/path"
    )
    with pytest.raises(headless.BenchmarkFailure, match="every URL"):
        headless._validate_objective_origin(
            "https://benchmark.test",
            "Assess https://benchmark.test and https://other.test",
        )


def test_stale_or_multiple_inventory_rows_are_never_selected() -> None:
    old = _row(id="campaign/old")
    with pytest.raises(headless.BenchmarkFailure, match="exactly one"):
        headless._new_campaign([old], [old])
    with pytest.raises(headless.BenchmarkFailure, match="exactly one"):
        headless._new_campaign([old], [old, _row(), _row(id="campaign/other")])


def test_mismatched_manifest_is_rejected() -> None:
    with pytest.raises(headless.BenchmarkFailure, match="manifest digest"):
        headless._validate_campaign_binding(
            _row(),
            _status(manifest_digest="sha256:other"),
            objective="Assess https://benchmark.test exactly",
            profile="recommended",
            composition="autonomous",
            limits={"turns": 8},
        )


@pytest.mark.parametrize(
    ("row", "status", "match"),
    [
        (_row(objective="changed"), _status(), "objective drifted"),
        (_row(profile="quickscan"), _status(), "profile drifted"),
        (
            _row(),
            _status(dynamic_specialist_policy={"specialist_composition": "opinionated"}),
            "composition drifted",
        ),
        (_row(budget={"turns": 7}), _status(review={}), "budget drifted"),
    ],
)
def test_objective_profile_composition_and_budget_drift_fail_closed(
    row: dict[str, object], status: dict[str, object], match: str
) -> None:
    with pytest.raises(headless.BenchmarkFailure, match=match):
        headless._validate_campaign_binding(
            row,
            status,
            objective="Assess https://benchmark.test exactly",
            profile="recommended",
            composition="autonomous",
            limits={"turns": 8},
        )


def test_campaign_requires_an_owning_session() -> None:
    with pytest.raises(headless.BenchmarkFailure, match="owning session"):
        headless._validate_campaign_binding(
            _row(session_id=""),
            _status(),
            objective="Assess https://benchmark.test exactly",
            profile="recommended",
            composition="autonomous",
            limits={"turns": 8},
        )


async def test_post_review_input_fails_closed() -> None:
    client = FakeClient(resume_events=[SimpleNamespace(kind="user_input", text="Choose a tenant")])
    with pytest.raises(headless.BenchmarkFailure, match="post-review operator input"):
        await headless._resume(client, "session/new")


async def test_report_pending_is_polled_until_ready() -> None:
    client = FakeClient(
        reports=[
            [{"status": "pending", "output": "report.md"}],
            [{"status": "ready", "output": "report.md"}],
        ]
    )
    reports = await headless._wait_reports(client, 1, 0.001)
    assert reports == [{"status": "ready", "output": "report.md"}]


async def test_durable_report_shape_is_ready() -> None:
    report = {
        "run": "campaign/new",
        "format": "durable-revision",
        "output": "campaign/new:r1:abc123",
        "status": "ready · objective_satisfied · cleanup completed",
        "finding_count": 1,
        "exchange_count": 2,
        "report_state": "complete",
        "stop_reason": "objective_satisfied",
        "cleanup_status": "completed",
        "coverage": [],
        "limitations": [],
        "target_progress": None,
    }
    client = FakeClient(reports=[[report]])

    reports = await headless._wait_reports(client, 0.01, 0.001)

    assert reports == [report]


def test_legacy_durable_report_status_requires_the_exact_shape() -> None:
    report = {
        "format": "durable-revision",
        "status": "ready · objective_satisfied · cleanup completed",
    }

    assert headless._report_is_ready(report) is True
    assert headless._report_is_ready({**report, "status": f"{report['status']} · stale"}) is False
    assert headless._report_is_ready({**report, "report_state": "pending"}) is False


async def test_stream_end_without_review_is_a_failed_submit_and_owned_daemon_is_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authorize(monkeypatch, tmp_path)
    client = FakeClient()

    async def launcher(_binary: Path, *arguments: str, timeout_seconds: float) -> str:
        del timeout_seconds
        if arguments == ("status",):
            return "stopped\n"
        if arguments == ("start",):
            return ""
        raise headless.BenchmarkFailure(
            "command failed: campaign was not durably prepared; stream ended without review"
        )

    with pytest.raises(headless.BenchmarkFailure, match="without review"):
        await headless.run(_args(), launcher=launcher, client_factory=lambda **_kwargs: client)
    assert client.shutdown_calls == 1


async def test_existing_daemon_is_never_replaced_or_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = _authorize(monkeypatch, tmp_path)
    factory_called = False

    async def launcher(_binary: Path, *arguments: str, timeout_seconds: float) -> str:
        assert _binary == binary
        del timeout_seconds
        assert arguments == ("status",)
        return "running\n"

    def factory(**_kwargs: object) -> FakeClient:
        nonlocal factory_called
        factory_called = True
        return FakeClient()

    with pytest.raises(headless.BenchmarkFailure, match="unrelated owner"):
        await headless.run(_args(), launcher=launcher, client_factory=factory)
    assert factory_called is False


async def test_public_submit_path_and_owned_restart_are_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = _authorize(monkeypatch, tmp_path)
    first = FakeClient(
        campaigns=[],
        statuses=[_status(), _status(status="completed")],
        reports=[[{"status": "ready", "output": "report.md"}]],
    )
    second = FakeClient(
        campaigns=[_row()],
        statuses=[_status(status="completed")],
        reports=[[{"status": "ready", "output": "report.md"}]],
    )
    clients = [first, second]
    calls: list[tuple[str, ...]] = []
    status_calls = 0

    async def launcher(_binary: Path, *arguments: str, timeout_seconds: float) -> str:
        nonlocal status_calls
        assert _binary == binary
        del timeout_seconds
        calls.append(arguments)
        if arguments == ("status",):
            status_calls += 1
            return "stopped\n"
        if arguments == ("start",):
            return ""
        first.campaigns = [_row()]
        return "Campaign started: campaign/new\n"

    result = await headless.run(
        _args(verify_restart=True),
        launcher=launcher,
        client_factory=lambda **_kwargs: clients.pop(0),
    )

    assert result["campaign_id"] == "campaign/new"
    submit = next(call for call in calls if call[:2] == ("campaign", "submit"))
    assert submit == (
        "campaign",
        "submit",
        "--profile",
        "recommended",
        "--composition",
        "autonomous",
        "--approve",
        "--strict-objective",
        "--max-turns",
        "8",
        "Assess https://benchmark.test exactly",
    )
    assert status_calls == 2
    assert first.shutdown_calls == 1
    assert second.shutdown_calls == 1
    assert first.lease_calls == 1
    assert second.lease_calls == 1
    assert second.resume_prompts == ["/resume session/new"]


async def test_primary_and_cleanup_failures_are_both_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _authorize(monkeypatch, tmp_path)

    class CleanupFailClient(FakeClient):
        async def acquire_foreground_lease(self) -> None:
            raise RuntimeError("cleanup lease failed")

    async def launcher(_binary: Path, *arguments: str, timeout_seconds: float) -> str:
        del timeout_seconds
        if arguments == ("status",):
            return "stopped\n"
        if arguments == ("start",):
            return ""
        raise headless.BenchmarkFailure("preparation stream ended without review")

    with pytest.raises(headless.BenchmarkCleanupFailure) as raised:
        await headless.run(
            _args(), launcher=launcher, client_factory=lambda **_kwargs: CleanupFailClient()
        )
    assert "without review" in str(raised.value)
    assert "cleanup lease failed" in str(raised.value)
    assert isinstance(raised.value.primary, headless.BenchmarkFailure)
    assert isinstance(raised.value.cleanup, RuntimeError)


async def test_runner_and_real_cli_do_not_collide_on_foreground_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercise the real socket server, LocalControlClient, and CLI subprocess."""
    from merlin_tui.backend import ReportView, StreamEvent
    from merlin_tui.local_control import LocalControlClient, LocalControlTuning, serve

    socket_path = Path(f"/tmp/merlin-headless-{uuid4().hex[:12]}.sock")
    objective = "Assess https://benchmark.test exactly"

    class Backend:
        def __init__(self) -> None:
            self.campaigns: list[dict[str, object]] = []
            self.executed = False

        async def recover_background(self) -> None:
            return None

        async def stream(self, prompt: str):
            if prompt.startswith("/new") or prompt.startswith("/resume"):
                yield StreamEvent("session", "session/new")
                return
            inventory_row = _row(status="prepared")
            self.campaigns = [inventory_row]
            review = {**inventory_row, "campaign_id": inventory_row["id"]}
            yield StreamEvent("campaign_review", json.dumps(review))

        async def interrupt(self) -> None:
            return None

        async def list_campaigns(self) -> list[dict[str, object]]:
            return self.campaigns

        async def list_active_schedules(self) -> tuple[()]:
            return ()

        async def has_active_background_work(self) -> bool:
            return any(row.get("status") == "executing" for row in self.campaigns)

        async def execute_campaign(
            self, campaign_id: str, manifest_digest: str
        ) -> dict[str, object]:
            assert (campaign_id, manifest_digest) == ("campaign/new", "sha256:one")
            self.executed = True
            self.campaigns[0]["status"] = "executing"
            return {"campaign_id": campaign_id, "status": "executing"}

        async def campaign_status(self, campaign_id: str) -> dict[str, object]:
            assert campaign_id == "campaign/new"
            return _status(
                status="executing" if self.executed else "prepared",
                specialist_composition="autonomous",
                budget={"limits": {"turns": 8}},
            )

        async def list_reports(self, *, session_only: bool = False) -> tuple[ReportView, ...]:
            del session_only
            return (ReportView("session/new", "markdown", "report.md", "ready", 0),)

        async def detach(self) -> None:
            return None

        async def close(self) -> None:
            return None

    backend = Backend()
    daemon = asyncio.create_task(
        serve(
            socket_path,
            backend_factory=lambda **_kwargs: backend,
            tuning=LocalControlTuning(
                request_timeout_seconds=5,
                max_stream_events=256,
            ),
        )
    )
    for _ in range(100):
        if await asyncio.to_thread(socket_path.exists):
            break
        if daemon.done():
            await daemon
        await asyncio.sleep(0.01)
    assert await asyncio.to_thread(socket_path.exists)

    async def lifecycle_launcher(_binary: Path, *arguments: str, timeout_seconds: float) -> str:
        del _binary, timeout_seconds
        assert arguments in {("status",), ("start",)}
        return "stopped\n" if arguments == ("status",) else ""

    owner = await headless._start_owned(
        Path(sys.executable),
        lifecycle_launcher,
        lambda **kwargs: LocalControlClient(socket_path=socket_path, **kwargs),
        1,
    )
    assert owner._lease_id is None

    environment = dict(os.environ)
    environment["MERLIN_SOCKET_PATH"] = str(socket_path)
    argv = [
        "campaign",
        "submit",
        "--profile",
        "recommended",
        "--composition",
        "autonomous",
        "--approve",
        "--strict-objective",
        "--max-turns",
        "8",
        objective,
    ]
    program = f"import sys; from merlin_tui.cli import main; sys.argv=['merlin', *{argv!r}]; main()"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        program,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == 0, (stdout.decode(), stderr.decode())
    assert b"Campaign started: campaign/new" in stdout

    await headless._claim_owned_lease(owner, 2)
    await owner.shutdown()
    await asyncio.wait_for(daemon, 2)


def test_release_artifacts_exclude_benchmark_driver(tmp_path: Path) -> None:
    """Prove the wheel boundary and the standalone collection boundary."""
    import subprocess

    environment = dict(os.environ)
    # The outer test runner may use an isolated temporary uv cache. Building an
    # artifact must use the normal build cache just like the release scripts.
    environment.pop("UV_CACHE_DIR", None)
    subprocess.run(
        [
            "uv",
            "build",
            "--package",
            "merlin-tui",
            "--wheel",
            "--out-dir",
            str(tmp_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    wheels = list(tmp_path.glob("merlin_tui-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
    assert not any("headless_campaign" in name or name.startswith("benchmarks/") for name in names)

    standalone = (ROOT / "packaging" / "merlin_macos.spec").read_text(encoding="utf-8")
    entry = (ROOT / "scripts" / "frozen_entry.py").read_text(encoding="utf-8")
    assert "benchmarks" not in standalone
    assert "headless_campaign" not in standalone
    assert "benchmarks" not in entry
    assert "headless_campaign" not in entry
    assert DRIVER.is_relative_to(ROOT / "benchmarks")
    assert not DRIVER.is_relative_to(ROOT / "apps" / "merlin-tui" / "src")
