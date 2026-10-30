import asyncio
import json
import shlex
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import merlin_tui.app as app_module
import pytest
from merlin_permissions import PermissionEffect, PermissionRequest, PermissionScope
from merlin_tui.app import (
    COMMANDS,
    CampaignScreen,
    Composer,
    EndpointScreen,
    EndpointSetup,
    JevSetupScreen,
    LibraryScreen,
    LiveTranscript,
    MerlinApp,
    ModelScreen,
    OverviewScreen,
    ProviderSetup,
    RemoteMcpScreen,
    RemoteMcpSetup,
    SessionBrowserScreen,
    SetupScreen,
)
from merlin_tui.backend import (
    AgentView,
    CampaignView,
    DemoBackend,
    MemoryRecordView,
    PackCaseView,
    PackView,
    ReportView,
    ResumeSessionView,
    StreamEvent,
)
from merlin_tui.bootstrap import build_app
from merlin_tui.client_state import ClientState, TranscriptEntry
from rich.console import Console
from textual.widgets import Button, Checkbox, Input, RichLog, Select, Static, TextArea


def _safe_command(command: str) -> str:
    replacements = {
        "NAME COMMAND ARGS…": "acceptance-mcp /usr/bin/false",
        "BUNDLE RECORD REASON…": "missing-bundle missing-record acceptance-reason",
        "BUNDLE RECORD…": "missing-bundle missing-record",
        "ID REASON…": "missing-id acceptance-reason",
        "ID TEXT…": "missing-id acceptance-message",
        "ARGS…": "https://example.test",
        "[ID|all]": "all",
        "NAME": "missing-name",
        "DIGEST": "sha256:missing",
        "CONTEXT": "missing-context",
        "SESSION_ID": "missing-session",
        "LIMIT": "1",
        "PATH": "/tmp/merlin-command-acceptance.json",
        "KIND": "profile",
        "[ID": "missing-id",
        "ID]": "missing-id",
        "ID": "missing-id",
    }
    for placeholder, value in replacements.items():
        command = command.replace(placeholder, value)
    return command


@pytest.mark.asyncio
@pytest.mark.parametrize("command", COMMANDS, ids=COMMANDS)
async def test_every_documented_command_is_keyboard_submittable(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module.webbrowser, "open", lambda _url: True)
    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        composer = app.query_one("#composer")
        composer.text = _safe_command(command)
        await pilot.press("enter")
        await pilot.pause()
        if command not in {"/quit", "/stop"}:
            assert app.is_running


@pytest.mark.asyncio
async def test_tui_never_restores_client_local_history_without_resume(tmp_path) -> None:
    state = ClientState(tmp_path / "client.json")
    state.save(
        "unfinished draft",
        (TranscriptEntry("user", "earlier question"), TranscriptEntry("content", "answer")),
    )
    app = MerlinApp(DemoBackend(), client_state=state)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#composer").text == ""
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "restored client transcript" not in transcript
        assert "earlier question" not in transcript
        assert "answer" not in transcript

        app.query_one("#composer").text = "/clear"
        await app.action_send()
        await pilot.pause()

    assert state.load() == ("", ())


@pytest.mark.asyncio
async def test_new_campaign_report_is_presented_with_export_path_once() -> None:
    class ReportBackend(DemoBackend):
        report_ready = False

        async def list_reports(self):
            if not self.report_ready:
                return ()
            return (
                ReportView(
                    run="campaign/demo",
                    format="durable-revision",
                    output="/workspace/target/reports/campaign-demo.html",
                    status="ready · saturated · cleanup completed",
                    finding_count=2,
                ),
            )

    backend = ReportBackend()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._poll_completed_reports()  # noqa: SLF001
        transcript = app.query_one("#transcript")
        original_write = transcript.write
        transcript.write = Mock(wraps=original_write)
        backend.report_ready = True
        await app._poll_completed_reports()  # noqa: SLF001
        await app._poll_completed_reports()  # noqa: SLF001
        await pilot.pause()
        written = "\n".join(str(call.args[0]) for call in transcript.write.call_args_list)

    assert transcript.write.call_count == 1
    assert "Campaign report ready" in written
    assert "/workspace/target/reports/campaign-demo.html" in written
    assert "/report" in written


@pytest.mark.asyncio
async def test_report_poller_waits_for_transcript_during_startup_choice(tmp_path) -> None:
    app = MerlinApp(
        DemoBackend(),
        client_state=ClientState(tmp_path / "startup-report-poller.json"),
        startup_prompt=True,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.screen.query_one("#startup-title")
        await app._poll_completed_reports()  # noqa: SLF001
        assert app.is_running


@pytest.mark.asyncio
async def test_report_completed_during_startup_choice_is_presented_once(tmp_path) -> None:
    old_report = ReportView(
        run="campaign/pre-launch",
        format="durable-revision",
        output="/workspace/reports/pre-launch.html",
        status="ready · saturated · cleanup completed",
        finding_count=0,
    )
    chooser_report = ReportView(
        run="campaign/during-chooser",
        format="durable-revision",
        output="/workspace/reports/during-chooser.html",
        status="ready · saturated · cleanup completed",
        finding_count=1,
    )

    class ReportBackend(DemoBackend):
        reports = (old_report,)

        async def list_reports(self):
            return self.reports

    backend = ReportBackend()
    app = MerlinApp(
        backend,
        client_state=ClientState(tmp_path / "startup-report-eventual.json"),
        startup_prompt=True,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.screen.query_one("#startup-title")
        await app._poll_completed_reports()  # noqa: SLF001
        backend.reports = (old_report, chooser_report)
        await app._poll_completed_reports()  # noqa: SLF001

        await pilot.press("enter")
        await pilot.pause()
        transcript = app.query_one("#transcript")
        original_write = transcript.write
        transcript.write = Mock(wraps=original_write)
        await app._poll_completed_reports()  # noqa: SLF001
        await app._poll_completed_reports()  # noqa: SLF001
        written = "\n".join(str(call.args[0]) for call in transcript.write.call_args_list)

    assert transcript.write.call_count == 1
    assert "during-chooser.html" in written
    assert "pre-launch.html" not in written


def test_campaign_status_renders_exhaustive_evidence_without_raw_json() -> None:
    rendered = app_module._render_campaign_status(  # noqa: SLF001
        {
            "status": "executing",
            "stop_reason": None,
            "manifest_digest": "sha256:reviewed",
            "saturation": {
                "no_new_findings_waves": 3,
                "admitted_techniques": 42,
                "attempted_techniques": 42,
                "untested_techniques": 0,
                "catalogue_cells_confirmed": 8,
                "catalogue_cells_refuted": 30,
                "catalogue_cells_blocked": 2,
                "catalogue_cells_unclear": 2,
                "catalogue_cells_unsearched": 0,
                "catalogue_cell_owners": (("cell-1", "worker-1"),),
                "terminal_tasks": 45,
                "total_tasks": 45,
                "hypothesis_cells_attempted": 3,
                "hypothesis_cells_required": 3,
                "hypothesis_cells_confirmed": 1,
                "hypothesis_cells_refuted": 1,
                "hypothesis_cells_blocked": 0,
                "hypothesis_cells_unclear": 1,
                "hypothesis_cells_unsearched": 0,
                "hypothesis_cell_owners": (("hypothesis:1", "worker-2"),),
                "awaiting_validation": 0,
                "ready": True,
            },
            "waves": (
                {
                    "wave": 1,
                    "kind": "initial",
                    "status": "completed",
                    "tasks": ({"id": "worker-1", "status": "completed"},),
                },
            ),
        }
    )

    assert "Quiet waves: 3/3" in rendered
    assert "Catalogue techniques attempted: 42/42 · 0 untested" in rendered
    assert (
        "8 confirmed · 30 refuted · 2 blocked · 0 terminally blocked · 2 unclear · 0 unsearched"
        in rendered
    )
    assert "Catalogue owners: 1 assigned" in rendered
    assert "Hypothesis cells attempted: 3/3" in rendered
    assert "1 confirmed · 1 refuted · 0 blocked · 1 unclear · 0 unsearched" in rendered
    assert "Hypothesis owners: 1 assigned" in rendered
    assert "Ready for operator decision: yes" in rendered
    assert "worker-1 · completed" in rendered
    assert '"saturation"' not in rendered


def test_campaign_progress_renders_target_state_and_sealed_objectives() -> None:
    objective = {
        "id": "operator/cover-tier-range/example",
        "description": "Cover every tier from 1 through 4",
        "required": True,
    }
    rendered = app_module._render_campaign_progress(  # noqa: SLF001
        {
            "campaign_id": "campaign/progress",
            "status": "executing",
            "target_progress": {
                "current_state": (
                    {"name": "tier", "label": "Tier", "value": 2, "maximum_value": 4},
                ),
                "target_exchange_count": 19,
                "freshness": "current",
            },
            "objective_contract": {"objectives": (objective,)},
            "objective_progress": (
                {"objective": objective, "status": "active", "evidence_refs": ()},
            ),
            "campaign_outcome": {
                "lifecycle": "running",
                "verdict": "none",
                "end_reason": None,
            },
            "pace": {"elapsed_seconds": 125, "current_wave": {"wave": 7}},
        }
    )

    assert "Tier 2/4" in rendered
    assert "Progress: 0/1 discharged" in rendered
    assert "Cover every tier from 1 through 4 · in progress" in rendered
    assert "running · none" in rendered
    assert "Wave: 7 · elapsed 2m05s" in rendered


def test_campaign_status_labels_legacy_objective_tracking() -> None:
    rendered = app_module._render_campaign_status(  # noqa: SLF001
        {"status": "executing", "manifest_digest": "sha256:legacy", "waves": ()}
    )

    assert "Unavailable — this campaign has no sealed objective contract." in rendered
    assert "Older campaigns predate tracking" in rendered


@pytest.mark.asyncio
async def test_progress_command_resolves_current_campaign_without_copied_id() -> None:
    class ProgressBackend(DemoBackend):
        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/current",
                    "session-demo",
                    "Cover Tier 1 through Tier 4",
                    "recommended",
                    "executing",
                    "sha256:reviewed",
                    target_progress={
                        "current_state": ({"name": "tier", "label": "Tier", "value": 3},),
                        "target_exchange_count": 8,
                        "freshness": "current",
                    },
                ),
            )

        async def campaign_status(self, campaign_id: str) -> object:
            assert campaign_id == "campaign/current"
            return {
                "campaign_id": campaign_id,
                "status": "executing",
                "manifest_digest": "sha256:reviewed",
                "waves": (),
            }

    app = MerlinApp(ProgressBackend())
    async with app.run_test() as pilot:
        await app._run_command("/progress")  # noqa: SLF001
        await pilot.pause()
        rendered = str(app.screen.query_one("#overview-content").render())

    assert "Campaign progress" in rendered
    assert "campaign/current" in rendered
    assert "Tier 3" in rendered
    assert "no sealed objective contract" in rendered


def test_campaign_status_renders_pace_with_lower_bound_and_estimating_states() -> None:
    rendered = app_module._render_campaign_status(  # noqa: SLF001
        {
            "status": "executing",
            "stop_reason": None,
            "manifest_digest": "sha256:reviewed",
            "pace": {
                "elapsed_seconds": 723.0,
                "waves_completed": 1,
                "avg_wave_seconds": 330.0,
                "last_wave_avg_task_seconds": 40.0,
                "current_wave": {
                    "wave": 1,
                    "tasks_completed": 3,
                    "tasks_total": 8,
                    "avg_task_seconds": 45.0,
                    "elapsed_seconds": 130.0,
                    "eta_seconds": 225.0,
                },
                "min_remaining_seconds": 225.0,
            },
            "waves": (),
        }
    )

    assert "Elapsed: 12m03s · Waves completed: 1 · Avg wave: 5m30s" in rendered
    assert "Wave 1: 3/8 tasks · avg task 45s (last wave 40s) · wave elapsed 2m10s" in rendered
    assert "Wave ETA: ~3m45s" in rendered
    assert "At least ~3m45s more if this is the last wave" in rendered
    assert "no total ETA" in rendered

    estimating = app_module._render_campaign_status(  # noqa: SLF001
        {
            "status": "executing",
            "pace": {
                "elapsed_seconds": 5.0,
                "waves_completed": 0,
                "avg_wave_seconds": None,
                "last_wave_avg_task_seconds": None,
                "current_wave": {
                    "wave": 0,
                    "tasks_completed": 0,
                    "tasks_total": 4,
                    "avg_task_seconds": None,
                    "elapsed_seconds": 5.0,
                    "eta_seconds": None,
                },
                "min_remaining_seconds": None,
            },
            "waves": (),
        }
    )

    assert "Wave ETA: estimating (awaiting first completed task)" in estimating
    assert "avg task n/a (last wave n/a)" in estimating
    assert "At least" not in estimating


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("enter", [("campaign-1", "stop", None)]),
        ("1", [("campaign-1", "stop", None)]),
        ("2", [("campaign-1", "continue", None)]),
        ("3", []),
    ],
)
async def test_pending_saturation_status_offers_numbered_keyboard_choices(
    key: str, expected: list[tuple[str, str, int | None]]
) -> None:
    class SaturatedBackend(DemoBackend):
        def __init__(self) -> None:
            super().__init__()
            self.decisions = []

        async def campaign_status(self, campaign_id: str) -> object:
            return {
                "campaign_id": campaign_id,
                "status": "executing",
                "manifest_digest": "sha256:reviewed",
                "waves": (),
                "saturation": {
                    "ready": True,
                    "no_new_findings_waves": 3,
                    "admitted_techniques": 42,
                    "attempted_techniques": 42,
                    "untested_techniques": 0,
                    "terminal_tasks": 45,
                    "total_tasks": 45,
                    "hypothesis_cells_attempted": 3,
                    "hypothesis_cells_required": 3,
                    "awaiting_validation": 0,
                },
                "saturation_decision": {
                    "state": "awaiting_operator",
                    "request_id": "request-1",
                },
            }

        async def decide_campaign_saturation(
            self, campaign_id: str, decision: str, additional_waves: int | None = None
        ) -> object:
            self.decisions.append((campaign_id, decision, additional_waves))
            return {"decision": decision}

    backend = SaturatedBackend()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        await app._show_campaign_status("campaign-1")  # noqa: SLF001
        await pilot.pause()
        labels = tuple(str(button.label) for button in app.screen.query(Button))
        assert "1. Stop and generate report [default]" in labels
        assert "Campaign needs an operator decision" in str(
            app.screen.query_one("#dialog Label").render()
        )
        assert "Decision checkpoint: request-1" in str(
            app.screen.query_one("#dialog Label").render()
        )
        assert all(
            button.region.bottom <= app.screen.size.height
            for button in app.screen.query("#dialog Button")
        )
        await pilot.press(key)
        await pilot.pause()
        if key == "3":
            assert app.query_one("#composer").text == (  # type: ignore[attr-defined]
                "/campaign saturation campaign-1 limit "
            )

    assert backend.decisions == expected


@pytest.mark.asyncio
async def test_saturation_poller_uses_lightweight_campaign_projection() -> None:
    class SaturatedBackend(DemoBackend):
        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/pending",
                    "session-demo",
                    "Pending coverage decision",
                    "recommended",
                    "executing",
                    "sha256:reviewed",
                    saturation_decision={
                        "state": "awaiting_operator",
                        "request_id": "coverage-extension-1",
                        "purpose": "coverage_extension",
                    },
                ),
            )

        async def campaign_status(self, campaign_id: str) -> object:
            raise AssertionError("the saturation poller must not load full campaign status")

        async def get_campaign_lead_log(self, campaign_id: str, limit: int = 100):
            raise AssertionError("the saturation poller must not load the full Lead journal")

    app = MerlinApp(SaturatedBackend())
    async with app.run_test() as pilot:
        await app._poll_saturation_decisions()  # noqa: SLF001
        await pilot.pause()
        assert "Campaign needs an operator decision" in str(
            app.screen.query_one("#dialog Label").render()
        )


@pytest.mark.asyncio
async def test_saturation_poller_does_not_reoffer_acknowledged_stale_request() -> None:
    class StaleProjectionBackend(DemoBackend):
        def __init__(self) -> None:
            super().__init__()
            self.decisions: list[tuple[str, str, int | None]] = []

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/pending",
                    "session-demo",
                    "Pending coverage decision",
                    "recommended",
                    "executing",
                    "sha256:reviewed",
                    saturation_decision={
                        "state": "awaiting_operator",
                        "request_id": "coverage-extension-1",
                        "purpose": "coverage_extension",
                    },
                ),
            )

        async def decide_campaign_saturation(
            self, campaign_id: str, decision: str, additional_waves: int | None = None
        ) -> object:
            self.decisions.append((campaign_id, decision, additional_waves))
            return {"decision": decision}

    backend = StaleProjectionBackend()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        await app._poll_saturation_decisions()  # noqa: SLF001
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        await app._poll_saturation_decisions()  # noqa: SLF001
        await pilot.pause()
        assert (  # noqa: SLF001
            "campaign/pending",
            "coverage-extension-1",
        ) in app._handled_saturation_requests
        assert app.screen is app.screen_stack[0]

    assert backend.decisions == [("campaign/pending", "continue", None)]


@pytest.mark.asyncio
async def test_saturation_poller_reoffers_same_request_id_for_another_campaign() -> None:
    class RepeatedRequestBackend(DemoBackend):
        def __init__(self) -> None:
            super().__init__()
            self.campaign_id = "campaign/one"
            self.decisions: list[tuple[str, str, int | None]] = []

        async def list_campaigns(self):
            return (
                CampaignView(
                    self.campaign_id,
                    "session-demo",
                    "Pending coverage decision",
                    "recommended",
                    "executing",
                    "sha256:reviewed",
                    saturation_decision={
                        "state": "awaiting_operator",
                        "request_id": "shared-request",
                        "purpose": "coverage_extension",
                    },
                ),
            )

        async def decide_campaign_saturation(
            self, campaign_id: str, decision: str, additional_waves: int | None = None
        ) -> object:
            self.decisions.append((campaign_id, decision, additional_waves))
            return {"decision": decision}

    backend = RepeatedRequestBackend()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        await app._poll_saturation_decisions()  # noqa: SLF001
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        backend.campaign_id = "campaign/two"
        await app._poll_saturation_decisions()  # noqa: SLF001
        await pilot.pause()
        assert app.screen is not app.screen_stack[0]
        await pilot.press("2")
        await pilot.pause()

    assert backend.decisions == [
        ("campaign/one", "continue", None),
        ("campaign/two", "continue", None),
    ]


@pytest.mark.asyncio
async def test_swarm_refresh_surfaces_pending_saturation_when_decision_poll_is_unavailable() -> (
    None
):
    class SaturatedBackend(DemoBackend):
        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/pending",
                    "session-demo",
                    "Pending coverage decision",
                    "recommended",
                    "executing",
                    "sha256:reviewed",
                    saturation_decision={
                        "state": "awaiting_operator",
                        "request_id": "coverage-extension-wave-96",
                        "purpose": "coverage_extension",
                    },
                ),
            )

    class SwarmOnlyApp(MerlinApp):
        async def _poll_saturation_decisions(self) -> None:
            return

    app = SwarmOnlyApp(SaturatedBackend())
    async with app.run_test() as pilot:
        await app._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()
        dialog = str(app.screen.query_one("#dialog Label").render())
        assert "Campaign needs an operator decision" in dialog
        assert "Decision checkpoint: coverage-extension-wave-96" in dialog


@pytest.mark.asyncio
async def test_startup_defaults_to_new_and_can_resume_latest_session(tmp_path) -> None:
    app = MerlinApp(
        DemoBackend(),
        client_state=ClientState(tmp_path / "startup-client.json"),
        startup_prompt=True,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "Showing 1 recent session." in str(app.screen.query_one("#startup-title").render())
        assert "3 browses recent history" in str(app.screen.query_one("#startup-help").render())
        assert "2026-08-27T12:01:00Z" in str(app.screen.query_one("#startup-detail").render())
        await pilot.press("enter")
        await pilot.pause()
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "Demo response: /new" not in transcript
        assert not app.query_one("#composer").disabled


@pytest.mark.asyncio
async def test_startup_can_browse_and_resume_a_non_latest_session(tmp_path) -> None:
    class HistoryBackend(DemoBackend):
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def list_resume_sessions(self) -> tuple[ResumeSessionView, ...]:
            return (
                ResumeSessionView("latest", "Latest", "2026-08-29T12:00:00Z"),
                ResumeSessionView("older", "Older", "2026-08-28T12:00:00Z"),
                ResumeSessionView("oldest", "Oldest", "2026-08-27T12:00:00Z"),
            )

        async def stream(self, prompt: str):
            self.prompts.append(prompt)
            async for event in super().stream(prompt):
                yield event

    backend = HistoryBackend()
    app = MerlinApp(
        backend,
        client_state=ClientState(tmp_path / "startup-history-client.json"),
        startup_prompt=True,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        assert "Sessions 1-3 of 3" in str(app.screen.query_one("Label").render())
        assert app.screen.focused.id == "browse-session-0"
        await pilot.press("2")
        for _ in range(10):
            await pilot.pause()
            if backend.prompts:
                break

    assert "/resume older" in backend.prompts


@pytest.mark.asyncio
async def test_startup_session_browser_pages_and_searches(tmp_path) -> None:
    class PagedBackend(DemoBackend):
        async def list_resume_sessions(self) -> tuple[ResumeSessionView, ...]:
            return tuple(
                ResumeSessionView(f"session-{index:02d}", f"Assessment {index:02d}")
                for index in range(25)
            )

    app = MerlinApp(
        PagedBackend(),
        client_state=ClientState(tmp_path / "startup-page-client.json"),
        startup_prompt=True,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        assert "Sessions 1-20 of 25" in str(app.screen.query_one("Label").render())
        assert app.screen.focused.id == "browse-session-0"
        await pilot.press("down")
        assert app.screen.focused.id == "browse-session-1"
        await pilot.press("pagedown")
        await pilot.pause()
        assert "Sessions 21-25 of 25" in str(app.screen.query_one("Label").render())
        assert app.screen.focused.id == "browse-session-0"
        await pilot.press("slash")
        search = app.screen.query_one("#session-search", Input)
        assert search.has_focus
        search.value = "Assessment 07"
        await pilot.press("enter")
        await pilot.pause()
        assert "Sessions 1-1 of 1" in str(app.screen.query_one("Label").render())
        assert "session-07" in str(app.screen.query_one("#browse-session-0", Button).label)


@pytest.mark.asyncio
async def test_sessions_command_pages_searches_and_resumes_without_startup(tmp_path) -> None:
    class SessionsBackend(DemoBackend):
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def list_resume_sessions(self) -> tuple[ResumeSessionView, ...]:
            return tuple(
                ResumeSessionView(f"session-{index:02d}", f"Assessment {index:02d}")
                for index in range(25)
            )

        async def stream(self, prompt: str):
            self.prompts.append(prompt)
            async for event in super().stream(prompt):
                yield event

    backend = SessionsBackend()
    app = MerlinApp(
        backend,
        client_state=ClientState(tmp_path / "sessions-command-client.json"),
    )
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/sessions"
        await pilot.press("enter")
        await pilot.pause()
        assert "Sessions 1-20 of 25" in str(app.screen.query_one("Label").render())

        await pilot.press("pagedown")
        await pilot.pause()
        assert "Sessions 21-25 of 25" in str(app.screen.query_one("Label").render())

        await pilot.press("slash")
        search = app.screen.query_one("#session-search", Input)
        search.value = "Assessment 07"
        await pilot.press("enter")
        await pilot.pause()
        assert "Sessions 1-1 of 1" in str(app.screen.query_one("Label").render())
        app.screen.query_one("#browse-session-0", Button).press()
        for _ in range(10):
            await pilot.pause()
            if backend.prompts:
                break

    assert backend.prompts == ["/resume session-07"]


@pytest.mark.asyncio
async def test_context_command_shows_sequence_lineage_without_message_content(tmp_path) -> None:
    class ContextBackend(DemoBackend):
        async def conversation_context(self):
            return {
                "session_id": "session-branch",
                "projection_head": 2,
                "transcript_head": 4,
                "messages": [
                    {
                        "sequence": 2,
                        "role": "assistant",
                        "message_id": "branch-message",
                        "source_message_id": "source-message",
                    }
                ],
                "compactions": [
                    {
                        "source_start": 1,
                        "source_end": 2,
                        "source_digest": "a" * 64,
                        "summary_digest": "b" * 64,
                        "algorithm": "structured-tool-lifecycle-context-window-v3",
                    }
                ],
            }

    app = MerlinApp(
        ContextBackend(),
        client_state=ClientState(tmp_path / "context-command-client.json"),
    )
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/context"
        await pilot.press("enter")
        await pilot.pause()
        content = str(app.screen.query_one("#overview-content").render())

    assert "rewound projection at sequence 2" in content
    assert "branch-message" in content
    assert "source-message" in content
    assert "inherited" in content
    assert "structured-tool-lifecycle-context-window-v3" in content
    assert "message content" not in content


def test_context_provenance_renderer_accepts_daemon_decoded_namespaces() -> None:
    rendered = app_module._render_context_provenance(  # noqa: SLF001
        SimpleNamespace(
            session_id="session-1",
            projection_head=None,
            transcript_head=1,
            messages=(
                SimpleNamespace(
                    sequence=1,
                    role="user",
                    message_id="message-1",
                    source_message_id="message-1",
                ),
            ),
            compactions=(),
        )
    )
    assert "transcript head at sequence 1" in rendered
    assert "message-1" in rendered
    assert "native" in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (120, 36), (180, 50)])
async def test_startup_gate_keeps_logo_and_choices_centered_together(tmp_path, size) -> None:
    app = MerlinApp(
        DemoBackend(),
        client_state=ClientState(tmp_path / f"startup-{size[0]}x{size[1]}.json"),
        startup_prompt=True,
    )
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        shell = app.screen.query_one("#startup-shell").region
        logo = app.screen.query_one("#startup-logo").region
        session = app.screen.query_one("#startup-session").region

        assert abs(shell.center[0] - size[0] / 2) <= 1
        assert abs(shell.center[1] - size[1] / 2) <= 1
        assert logo.bottom <= session.y
        assert shell.width <= min(120, size[0] - 2)


@pytest.mark.asyncio
async def test_startup_session_lookup_failure_never_strands_the_composer(tmp_path) -> None:
    class BrokenHistoryBackend(DemoBackend):
        async def list_resume_sessions(self):
            raise RuntimeError("coroutine ignored GeneratorExit")

    app = MerlinApp(
        BrokenHistoryBackend(),
        client_state=ClientState(tmp_path / "broken-history-client.json"),
        startup_prompt=True,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        assert app.screen.query_one("#startup-new")
        assert "Could not load previous sessions" in str(
            app.screen.query_one("#startup-title").render()
        )
        await pilot.press("2")
        await pilot.pause()
        composer = app.query_one("#composer")
        assert not composer.disabled
        assert composer.has_focus
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "GeneratorExit" not in transcript


@pytest.mark.asyncio
async def test_startup_initialization_timeout_never_strands_the_composer(tmp_path) -> None:
    class HangingNewSessionBackend(DemoBackend):
        async def list_resume_sessions(self):
            return ()

        async def stream(self, prompt):
            if prompt == "/new":
                await asyncio.sleep(10)
            if False:
                yield StreamEvent("content", "unreachable")

    app = MerlinApp(
        HangingNewSessionBackend(),
        client_state=ClientState(tmp_path / "hanging-startup-client.json"),
        startup_prompt=True,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        assert app.query_one("#composer").disabled
        await pilot.press("enter")
        await pilot.pause(0.2)
        composer = app.query_one("#composer")
        assert not composer.disabled
        assert composer.has_focus


@pytest.mark.asyncio
async def test_ctrl_z_exits_without_mouse() -> None:
    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+z")
        await pilot.pause()
        assert not app.is_running


def test_real_suspend_signal_exits_instead_of_stopping_process(monkeypatch) -> None:
    suspend = getattr(app_module.signal, "SIGTSTP", None)
    if suspend is None:
        pytest.skip("SIGTSTP is not available on this platform")
    previous = object()
    installed: list[tuple[object, object]] = []
    monkeypatch.setattr(app_module.signal, "getsignal", lambda _signal: previous)
    monkeypatch.setattr(
        app_module.signal,
        "signal",
        lambda target, handler: installed.append((target, handler)),
    )
    app = MerlinApp(DemoBackend())
    app._driver = SimpleNamespace(can_suspend=True)

    app._install_suspend_exit_handler()
    assert installed[-1][0] == suspend
    installed[-1][1](suspend, None)
    assert app._exit is True

    app._restore_suspend_handler()
    assert installed[-1] == (suspend, previous)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected"),
    (
        ("/", "unknown command"),
        ("/definitely-unknown", "unknown command"),
        ("/mode impossible", "Mode failed"),
        ("/progress one two", "Usage: /progress [ID]"),
        ("/campaign cancel one two", "Usage: /campaign status ID"),
        ("/cancel campaign", "Usage: /cancel all|campaign ID|task ID"),
    ),
)
async def test_invalid_commands_fail_inline_and_keep_keyboard_focus(
    command: str, expected: str
) -> None:
    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        composer = app.query_one("#composer")
        composer.text = command
        await pilot.press("enter")
        await pilot.pause()
        if app._stream_task is not None:
            await app._stream_task
        transcript = "\n".join(line.text for line in app.query_one("#transcript").lines)
        assert expected.lower() in transcript.lower()
        assert not composer.disabled
        assert composer.has_focus


@pytest.mark.asyncio
async def test_empty_enter_is_a_noop_and_keeps_focus() -> None:
    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        composer = app.query_one("#composer")
        await pilot.press("enter")
        await pilot.pause()
        assert composer.text == ""
        assert composer.has_focus


@pytest.mark.asyncio
async def test_orderly_tui_exit_requests_owned_runtime_shutdown() -> None:
    class LifecycleBackend(DemoBackend):
        def __init__(self) -> None:
            self.shutdown_called = False

        async def shutdown(self) -> None:
            self.shutdown_called = True

    backend = LifecycleBackend()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        app.exit()
        await pilot.pause()

    assert backend.shutdown_called


@pytest.mark.asyncio
async def test_tui_exit_contains_shutdown_exception_group() -> None:
    class FailingShutdownBackend(DemoBackend):
        def __init__(self) -> None:
            self.shutdown_called = False

        async def shutdown(self) -> None:
            self.shutdown_called = True
            raise ExceptionGroup("cleanup failed", [RuntimeError("api_key=do-not-log")])

    backend = FailingShutdownBackend()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        app.exit()
        await pilot.pause()

    assert backend.shutdown_called


@pytest.mark.asyncio
async def test_tui_exit_bounds_and_cancels_slow_shutdown(monkeypatch) -> None:
    class SlowShutdownBackend(DemoBackend):
        def __init__(self) -> None:
            self.started = False
            self.cancelled = False

        async def shutdown(self) -> None:
            self.started = True
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    monkeypatch.setattr(app_module, "_SHUTDOWN_TIMEOUT_SECONDS", 0.01)
    backend = SlowShutdownBackend()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        app.exit()
        await pilot.pause()

    assert backend.started
    assert backend.cancelled


@pytest.mark.asyncio
async def test_client_state_is_not_saved_on_each_composer_keystroke() -> None:
    class RecordingState:
        def __init__(self) -> None:
            self.saved: list[tuple[str, tuple[TranscriptEntry, ...]]] = []

        def save(self, draft: str, transcript: tuple[TranscriptEntry, ...]) -> None:
            self.saved.append((draft, transcript))

    state = RecordingState()
    app = MerlinApp(DemoBackend(), client_state=state)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        composer = app.query_one("#composer")
        composer.text = "not persisted per key"
        await pilot.pause()
        assert state.saved == []
        await app.action_send()
        await pilot.pause()
        assert state.saved


def test_prompt_history_is_bounded_and_deduplicated() -> None:
    composer = Composer()
    for index in range(105):
        composer.remember_submission(f"prompt-{index}")
    composer.remember_submission("prompt-100")

    assert len(composer._submitted_history) == 100  # noqa: SLF001
    assert composer._submitted_history[-1] == "prompt-100"  # noqa: SLF001
    assert composer._submitted_history.count("prompt-100") == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_composer_grows_to_three_lines_and_backspace_joins_previous_lines() -> None:
    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        composer = app.query_one("#composer", TextArea)
        assert composer.region.height == 3

        await pilot.press("a", "shift+enter", "b", "shift+enter", "c", "shift+enter", "d")
        await pilot.pause()
        assert composer.text == "a\nb\nc\nd"
        assert composer.region.height == 5

        await pilot.press(
            "home",
            "backspace",
            "backspace",
            "backspace",
            "backspace",
            "backspace",
            "backspace",
        )
        await pilot.pause()
        assert composer.text == "d"
        assert composer.cursor_location == (0, 0)
        assert composer.region.height == 3


@pytest.mark.asyncio
async def test_ctrl_u_deletes_upward_through_empty_composer_lines() -> None:
    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        composer = app.query_one("#composer", TextArea)
        await pilot.press(
            "a",
            "shift+enter",
            "shift+enter",
            "b",
            "ctrl+u",
            "ctrl+u",
            "ctrl+u",
            "ctrl+u",
        )
        await pilot.pause()

        assert composer.text == ""
        assert composer.cursor_location == (0, 0)
        assert composer.region.height == 3


@pytest.mark.asyncio
async def test_composer_arrows_navigate_multiline_text_then_recall_prompt_snapshots() -> None:
    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        composer = app.query_one("#composer", Composer)
        composer.remember_submission("first prompt")
        composer.remember_submission("second\nprompt")
        composer.text = "draft\nline"
        composer.move_cursor((1, 4))

        await pilot.press("up")
        assert composer.text == "draft\nline"
        assert composer.cursor_location[0] == 0

        await pilot.press("up")
        assert composer.text == "second\nprompt"
        composer.insert(" edited")

        composer.move_cursor((0, 0))
        await pilot.press("up")
        assert composer.text == "first prompt"

        composer.move_cursor((0, len(composer.text)))
        await pilot.press("down")
        assert composer.text == "second\nprompt"
        await pilot.press("down")
        assert composer.text == "draft\nline"


@pytest.mark.asyncio
async def test_new_reconnaissance_preserves_transcript_and_settles_transient_rows(
    tmp_path,
) -> None:
    class ReconBackend(DemoBackend):
        async def stream(self, prompt):
            yield StreamEvent("status", "Starting application server…")
            yield StreamEvent("thinking", "Waiting for target readiness")
            if prompt == "/new":
                yield StreamEvent("status", "Fresh session ready")
            else:
                yield StreamEvent("content", f"Completed: {prompt}")

    app = MerlinApp(ReconBackend(), client_state=ClientState(tmp_path / "regression-client.json"))
    async with app.run_test() as pilot:
        transcript_widget = app.query_one("#transcript")
        app.query_one("#composer").text = "first assessment"
        await app.action_send()
        await pilot.pause()

        app.query_one("#composer").text = "/new"
        await app.action_send()
        await pilot.pause()
        app.query_one("#composer").text = "run fresh reconnaissance"
        await app.action_send()
        await pilot.pause()

        assert app.query_one("#transcript") is transcript_widget
        transcript = "\n".join(line.text for line in transcript_widget.lines)
        assert "Completed: first assessment" in transcript
        assert "Fresh session ready" in transcript
        assert "Completed: run fresh reconnaissance" in transcript
        assert "\n\n\n" not in transcript
        assert not app.query_one("#activity").has_class("visible")
        assert "ready" in str(app.query_one("#status").render())


@pytest.mark.asyncio
async def test_chat_commands_and_inline_failures() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        composer = app.query_one("#composer")

        composer.text = "/help"
        await app.action_send()
        await pilot.pause()
        transcript_text = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "/setup" in transcript_text

        composer.text = "hello"
        await app.action_send()
        await pilot.pause()
        transcript_text = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "Demo response: hello" in transcript_text

        composer.text = "fail provider"
        await app.action_send()
        await pilot.pause()
        transcript_text = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "Provider failed" in transcript_text


@pytest.mark.asyncio
async def test_codex_style_composer_enter_hints_and_typo_suggestion() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        assert not app.query("#send")
        composer = app.query_one("#composer")

        await pilot.press("/")
        await pilot.pause()
        hints = app.query_one("#command-hints")
        assert hints.has_class("visible")
        assert "/help" in str(hints.render())
        assert "show commands and keyboard shortcuts" in str(hints.render())

        composer.text = "line one"
        composer.move_cursor((0, len(composer.text)))
        await pilot.press("shift+enter")
        await pilot.press("l", "i", "n", "e", " ", "t", "w", "o")
        assert composer.text == "line one\nline two"

        composer.text = "hello from enter"
        await pilot.press("enter")
        await pilot.pause()
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "Demo response: hello from enter" in transcript

        composer.text = "/memroy list"
        await pilot.press("enter")
        await pilot.pause()
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "Did you mean /memory?" in transcript


@pytest.mark.asyncio
async def test_command_hints_render_after_dismissing_setup_modal() -> None:
    """Regression: cancelling /setup then typing / must show the hints popup
    with command names, and focus must return to the composer."""
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer")
        composer.focus()
        for character in "/setup":
            await pilot.press(character)
        await pilot.press("enter")
        await pilot.pause()
        assert len(app.screen_stack) > 1  # modal is up

        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1  # modal dismissed

        await pilot.press("/")
        await pilot.pause()
        hints = app.query_one("#command-hints")
        assert hints.has_class("visible")
        rendered = str(hints.render())
        assert "/help" in rendered
        assert "show commands and keyboard shortcuts" in rendered
        assert app.focused is composer


@pytest.mark.asyncio
async def test_stream_chunks_render_as_one_response_and_usage_stays_in_status() -> None:
    class ChunkBackend(DemoBackend):
        async def stream(self, prompt):
            yield StreamEvent("thinking", "reviewing target")
            for chunk in ("For ", "the ", "target"):
                yield StreamEvent("content", chunk)
            yield StreamEvent("usage", "831 tokens (808 in · 23 out)")

    app = MerlinApp(ChunkBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "assess"
        await app.action_send()
        await pilot.pause()
        lines = [line.text for line in app.query_one("#transcript").lines]
        assert any("· Thinking · reviewing target" in line for line in lines)
        assert "● For the target" in lines
        assert not any("input_tokens" in line for line in lines)
        assert "831 tokens (808 in · 23 out)" in str(app.query_one("#status").render())


@pytest.mark.asyncio
async def test_sentence_sized_provider_delta_renders_one_word_at_a_time(monkeypatch) -> None:
    first_frame = asyncio.Event()
    release_frame = asyncio.Event()

    class SentenceBackend(DemoBackend):
        async def stream(self, prompt):
            yield StreamEvent("content", "One two three four")
            first_frame.set()
            await release_frame.wait()

    app = MerlinApp(SentenceBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "assess"
        await app.action_send()
        async with asyncio.timeout(2):
            await first_frame.wait()
        await pilot.pause()
        lines = [line.text for line in app.query_one("#transcript").lines]
        assert lines[-1] == "● One two three four"
        release_frame.set()
        assert app._stream_task is not None
        await app._stream_task


@pytest.mark.asyncio
async def test_thinking_tokens_are_live_then_settle_as_one_full_width_row() -> None:
    release_stream = asyncio.Event()

    class ThinkingBackend(DemoBackend):
        async def stream(self, prompt):
            yield StreamEvent("thinking", "Inspecting ")
            yield StreamEvent("thinking", "the reviewed target contract before acting.")
            await release_stream.wait()
            yield StreamEvent("content", "Ready")

    app = MerlinApp(ThinkingBackend())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one("#composer").text = "assess"
        await app.action_send()
        await pilot.pause()

        transcript = app.query_one("#transcript")
        live_lines = [line.text for line in transcript.lines]
        assert live_lines[-2] == "● assess"
        assert live_lines[-1].startswith("· Thinking · Inspecting")
        assert transcript.region.width >= 90

        stream_task = app._stream_task
        assert stream_task is not None
        release_stream.set()
        await stream_task
        await pilot.pause()

        lines = [line.text for line in transcript.lines]
        thinking_lines = [line for line in lines if line.startswith("· Thinking · ")]
        assert thinking_lines == [
            "· Thinking · Inspecting the reviewed target contract before acting."
        ]
        assert lines[-2:] == [
            "· Thinking · Inspecting the reviewed target contract before acting.",
            "● Ready",
        ]


@pytest.mark.asyncio
async def test_live_thinking_reflows_after_terminal_resize_without_blank_or_narrow_column() -> None:
    first_delta = asyncio.Event()
    continue_stream = asyncio.Event()

    class ResizeBackend(DemoBackend):
        async def stream(self, prompt):
            yield StreamEvent("thinking", "Inspecting the reviewed target ")
            first_delta.set()
            await continue_stream.wait()
            yield StreamEvent("thinking", "contract and browser lifecycle before acting.")
            yield StreamEvent("content", "Ready to proceed with bounded reconnaissance.")

    app = MerlinApp(ResizeBackend())
    async with app.run_test(size=(120, 36)) as pilot:
        app.query_one("#composer").text = "assess"
        await app.action_send()
        await first_delta.wait()
        await pilot.resize_terminal(72, 28)
        await pilot.pause()

        transcript = app.query_one("#transcript")
        assert transcript.region.width >= 64
        assert any(
            "Thinking · Inspecting the reviewed target" in line.text for line in transcript.lines
        )

        continue_stream.set()
        assert app._stream_task is not None
        await app._stream_task
        await pilot.resize_terminal(140, 40)
        await pilot.pause()

        lines = [line.text for line in transcript.lines]
        assert any(line.startswith("· Thinking · ") for line in lines)
        assert any("browser lifecycle" in line for line in lines)
        assert "● Ready to proceed with bounded reconnaissance." in lines
        assert max(map(len, lines[-2:])) >= 45


@pytest.mark.asyncio
async def test_interrupt_commits_thinking_and_restores_composer() -> None:
    started = asyncio.Event()

    class InterruptBackend(DemoBackend):
        interrupted = 0

        async def stream(self, prompt):
            yield StreamEvent("thinking", "Inspecting the target before a bounded action.")
            started.set()
            await asyncio.Event().wait()

        async def interrupt(self):
            self.interrupted += 1

    backend = InterruptBackend()
    app = MerlinApp(backend)
    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one("#composer")
        composer.text = "assess"
        await app.action_send()
        await started.wait()
        await pilot.pause()

        await app.action_interrupt()
        await pilot.pause()

        lines = [line.text for line in app.query_one("#transcript").lines]
        assert lines[-2:] == [
            "· Thinking · Inspecting the target before a bounded action.",
            "Turn interrupted",
        ]
        assert backend.interrupted == 1
        assert not app.query_one("#activity").has_class("visible")
        assert not composer.disabled
        assert composer.has_focus


@pytest.mark.asyncio
async def test_ctrl_o_details_is_keyboard_only_and_returns_focus_to_composer() -> None:
    class DetailBackend(DemoBackend):
        async def stream(self, prompt):
            yield StreamEvent("thinking", "Planning bounded work")
            yield StreamEvent("content", "Ready")

    app = MerlinApp(DetailBackend())
    async with app.run_test() as pilot:
        composer = app.query_one("#composer")
        composer.text = "assess"
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("ctrl+o")
        await pilot.pause()
        assert "Thinking · Planning bounded work" in str(
            app.screen.query_one("#overview-content").render()
        )

        await pilot.press("ctrl+o")
        await pilot.pause()
        assert composer.has_focus


@pytest.mark.asyncio
async def test_overview_drawer_content_is_selectable_and_copyable() -> None:
    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        app.push_screen(OverviewScreen("Evidence", "first line\nsecond line"))
        await pilot.pause()

        content = app.screen.query_one("#overview-content", app_module.SelectableDrawerText)
        content.select_all()

        assert content.selected_text == "first line\nsecond line"


@pytest.mark.asyncio
async def test_ctrl_o_details_refreshes_live_and_follows_latest_output() -> None:
    app = MerlinApp(DemoBackend())
    async with app.run_test(size=(80, 20)) as pilot:
        app._record_activity("• Thinking · initial detail")
        await pilot.press("ctrl+o")
        await pilot.pause()

        scroll = app.screen.query_one("#overview-content", app_module.SelectableDrawerText)
        for index in range(40):
            app._record_activity(f"↳ Tool · live event {index}")
        await pilot.pause(0.2)

        detail = str(app.screen.query_one("#overview-content").render())
        assert "live event 39" in detail
        assert scroll.is_vertical_scroll_end

        scroll.scroll_page_up(animate=False)
        await pilot.pause()
        reviewing_position = scroll.scroll_y
        app._record_activity("↳ Tool · arrived while reviewing history")
        await pilot.pause(0.2)
        assert scroll.scroll_y == reviewing_position
        assert not scroll.is_vertical_scroll_end

        scroll.action_scroll_end()
        await pilot.pause()
        app._record_activity("↳ Tool · arrived after returning to latest")
        await pilot.pause(0.2)
        assert scroll.is_vertical_scroll_end


@pytest.mark.asyncio
async def test_ctrl_o_details_summarizes_large_tool_results_without_hiding_reasoning() -> None:
    reasoning = "Complete reasoning remains visible."
    raw_result = '{"content":"' + ("large escaped knowledge result " * 30) + '"}'

    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        app._active_thinking = reasoning
        app._record_activity(f"↳ Tool · knowledge_search completed: {raw_result}")
        await pilot.press("ctrl+o")
        await pilot.pause()

        details = str(app.screen.query_one("#overview-content").render())
        assert reasoning in details
        assert "Tool · knowledge_search completed" in details
        assert "payload omitted" in details
        assert raw_result not in details


@pytest.mark.asyncio
async def test_thinking_streams_into_transcript_and_ctrl_o_preserves_full_text() -> None:
    release_stream = asyncio.Event()
    reasoning = "\n".join(f"reasoning line {index}" for index in range(1, 13))

    class LongThinkingBackend(DemoBackend):
        async def stream(self, prompt):
            del prompt
            yield StreamEvent("thinking", reasoning)
            await release_stream.wait()
            yield StreamEvent("tool", "browser_snapshot completed")
            yield StreamEvent("content", "Ready")

    app = MerlinApp(LongThinkingBackend())
    async with app.run_test(size=(100, 34)) as pilot:
        app.query_one("#composer").text = "assess"
        await app.action_send()
        await pilot.pause()

        transcript = "\n".join(line.text for line in app.query_one("#transcript").lines)
        assert "Thinking · reasoning line 1" in transcript
        assert "reasoning line 12" in transcript

        await pilot.press("ctrl+o")
        await pilot.pause()
        details = str(app.screen.query_one("#overview-content").render())
        assert "reasoning line 1" in details
        assert "reasoning line 12" in details
        await pilot.press("escape")

        release_stream.set()
        assert app._stream_task is not None
        await app._stream_task
        await pilot.pause()
        transcript = "\n".join(line.text for line in app.query_one("#transcript").lines)
        assert "Tool · browser_snapshot completed" in transcript


@pytest.mark.asyncio
async def test_overview_drawer_focus_is_trapped_and_escape_restores_composer() -> None:
    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        composer = app.query_one("#composer")
        app.push_screen(OverviewScreen("Acceptance", "Keyboard-only drawer"))
        await pilot.pause()

        close = app.screen.query_one("#overview-close", Button)
        assert close.has_focus
        await pilot.press("tab")
        await pilot.pause()
        assert close.has_focus

        await pilot.press("escape")
        await pilot.pause()
        assert composer.has_focus


@pytest.mark.asyncio
async def test_library_filters_admitted_case_metadata_and_shows_control() -> None:
    case = PackCaseView(
        id="cat/direct/multilingual-pivot",
        title="Multilingual Pivot",
        instructions="Test policy consistency across languages.",
        objectives=("policy-override",),
        tags=("direct-user-channel", "low-resource-language"),
        success_hints=("Compare the translated result with its control",),
        strategy="direct",
        control_case_id="cat/direct/benign-greeting",
        oracle_id="oracle/target-process-internal",
    )
    pack = PackView(
        id="merlin-injection-taxonomy",
        title="Merlin Injection Taxonomy",
        instructions="Digest-admitted catalogue.",
        cases=(case,),
        enabled=True,
    )
    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        app.push_screen(LibraryScreen((pack,)))
        await pilot.pause()

        search = app.screen.query_one("#library-search", Input)
        assert search.has_focus
        search.value = "low-resource policy"
        await pilot.pause()
        detail = str(app.screen.query_one("#library-detail").render())
        assert "Multilingual Pivot" in detail
        assert "cat/direct/benign-greeting" in detail
        assert "oracle/target-process-internal" in detail

        search.value = "no-such-technique"
        await pilot.pause()
        assert "No matching techniques" in str(app.screen.query_one("#library-detail").render())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("screen", "first_control", "semantic_name"),
    (
        (SetupScreen("openai"), "#setup-base-url", "Provider base URL"),
        (ModelScreen(), "#worker-model", "Workers model"),
        (RemoteMcpScreen(), "#mcp-remote-name", "Remote MCP server name"),
        (
            SessionBrowserScreen(
                app_module.ResumeSessionPage(items=(), total=0, offset=0, limit=20, query="")
            ),
            "#session-search",
            "Search durable sessions",
        ),
    ),
)
async def test_form_drawers_expose_semantic_names_and_restore_keyboard_focus(
    screen, first_control: str, semantic_name: str
) -> None:
    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        composer = app.query_one("#composer")
        assert composer.name == "Message composer"
        app.push_screen(screen)
        await pilot.pause()

        control = app.screen.query_one(first_control)
        assert control.has_focus
        assert control.name == semantic_name
        dialog = app.screen.query_one("#dialog")
        assert abs((dialog.region.x * 2 + dialog.region.width) - app.size.width) <= 2
        if dialog.region.height <= app.size.height:
            assert abs((dialog.region.y * 2 + dialog.region.height) - app.size.height) <= 1
        else:
            assert dialog.region.y == 0
        await pilot.press("escape")
        await pilot.pause()
        assert composer.has_focus


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "screen",
    (
        EndpointScreen(),
        SetupScreen("openai", base_url="https://example.test", model="test-model"),
        JevSetupScreen(token_configured=True),
        ModelScreen(judgments_jev_token_configured=True),
        RemoteMcpScreen(),
    ),
)
async def test_form_drawers_submit_with_ctrl_enter_from_any_control(screen) -> None:
    results = []
    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        app.push_screen(screen, results.append)
        await pilot.pause()
        if isinstance(screen, EndpointScreen):
            screen.query_one("#endpoint-model", Input).value = "test-model"
            screen.query_one("#endpoint-base-url", Input).value = "https://example.test"
        await pilot.press("ctrl+enter")
        await pilot.pause()

    assert len(results) == 1


@pytest.mark.asyncio
async def test_setup_screen_preserves_current_reasoning_effort() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.push_screen(SetupScreen("openai", effort="high"))
        await pilot.pause()

        assert app.screen.query_one("#setup-effort", Select).value == "high"


@pytest.mark.asyncio
async def test_provider_setup_flow_loads_persisted_reasoning_effort(monkeypatch) -> None:
    from merlin_tui.provider_catalog import PROVIDER_ROUTES

    route = PROVIDER_ROUTES[3]
    monkeypatch.setattr(
        "merlin_config.load_config",
        lambda: SimpleNamespace(
            values={
                "provider": {
                    "base_url": route.base_url,
                    "model": "deepseek-v4-pro",
                    "effort": "high",
                }
            }
        ),
    )
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app._pick_provider(route.label)
        await pilot.pause()

        assert app.screen.query_one("#setup-effort", Select).value == "high"
        assert app.screen.query_one("#setup-model", Input).value == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_provider_setup_save_discovers_a_missing_model(monkeypatch) -> None:
    from merlin_tui.provider_catalog import (
        PROVIDER_ROUTES,
        ModelCandidate,
        ModelDiscovery,
    )

    route = PROVIDER_ROUTES[3]
    monkeypatch.setattr(
        "merlin_tui.provider_catalog.discover_model_catalog",
        lambda *args, **kwargs: ModelDiscovery((ModelCandidate("deepseek-v4-pro"),)),
    )
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.push_screen(SetupScreen(route, route.base_url))
        await pilot.pause()
        app.screen.query_one("#setup-api-key", Input).value = "opaque-secret"
        app.screen.query_one("#save", Button).press()
        await pilot.pause()

        assert isinstance(app.screen, app_module.ChoiceScreen)
        await pilot.press("1")
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        assert app.screen.query_one("#setup-model", Input).value == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_model_screen_filters_no_tool_transports_to_campaign_lead() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.push_screen(ModelScreen())
        await pilot.pause()

        assert "kimi_acp" not in app.screen.query_one("#worker-transport", Select)._legal_values
        assert "claude_cli" not in app.screen.query_one("#recon-transport", Select)._legal_values
        assert "kimi_acp" in app.screen.query_one("#lead-transport", Select)._legal_values


@pytest.mark.asyncio
async def test_campaign_drawer_tabs_actions_without_losing_escape_close() -> None:
    stopped = []

    async def stop(campaign_id: str) -> int:
        stopped.append(campaign_id)
        return 1

    app = MerlinApp(DemoBackend())
    campaign = CampaignView(
        id="campaign/active",
        session_id="session-demo",
        status="executing",
        objective="Assess",
        profile="recommended",
        manifest_digest="sha256:reviewed",
    )
    async with app.run_test() as pilot:
        composer = app.query_one("#composer")
        app.push_screen(CampaignScreen((campaign,), stop))
        await pilot.pause()

        stop_one = app.screen.query_one("#campaign-stop-0", Button)
        stop_all = app.screen.query_one("#campaign-stop-all", Button)
        close = app.screen.query_one("#overview-close", Button)
        assert stop_one.has_focus
        await pilot.press("tab")
        assert stop_all.has_focus
        await pilot.press("tab")
        assert close.has_focus
        await pilot.press("tab")
        assert stop_one.has_focus

        await pilot.press("escape")
        await pilot.pause()
        assert stopped == []
        assert composer.has_focus


@pytest.mark.asyncio
async def test_transcript_scrollback_moves_without_disabling_composer() -> None:
    class LongBackend(DemoBackend):
        async def stream(self, prompt):
            for index in range(80):
                yield StreamEvent("status", f"Durable progress checkpoint {index:02d}")
            yield StreamEvent("content", "Campaign progress remains available.")

    app = MerlinApp(LongBackend())
    async with app.run_test(size=(90, 24)) as pilot:
        composer = app.query_one("#composer")
        composer.text = "assess"
        await pilot.press("enter")
        await pilot.pause()

        transcript = app.query_one("#transcript")
        bottom = transcript.scroll_y
        assert bottom > 0
        app.action_scroll_up()
        await pilot.pause()
        assert transcript.scroll_y < bottom
        app.action_scroll_down()
        await pilot.pause()
        assert transcript.scroll_y > 0
        assert not composer.disabled


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (120, 36), (180, 50)])
async def test_streamed_response_keeps_transcript_viewport_width(size) -> None:
    response = (
        "The operator wants a Merlin scan on https://plunder.me with policy refund trap L1. "
        "I need to inspect the target and preserve the reviewed interaction contract."
    )

    class ResponseBackend(DemoBackend):
        async def stream(self, prompt):
            yield StreamEvent("content", response)

    app = MerlinApp(ResponseBackend())
    async with app.run_test(size=size) as pilot:
        app.query_one("#composer").text = "assess"
        await app.action_send()
        await pilot.pause()

        transcript = app.query_one("#transcript")
        assert transcript.region.width >= size[0] - 8
        rendered_lines = [line.text for line in transcript.lines if "operator wants" in line.text]
        assert rendered_lines
        assert max(map(len, rendered_lines)) >= min(60, transcript.region.width - 8)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "expected"),
    (
        ("", "recommended"),
        ("1", "recommended"),
        ("2", "quickscan"),
        ("3", "exhaustive"),
        ("4", "exploration"),
    ),
)
async def test_campaign_profile_choice_accepts_enter_or_number(reply, expected) -> None:
    class ChoiceBackend(DemoBackend):
        def __init__(self):
            self.prompts = []

        async def stream(self, prompt):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                yield StreamEvent(
                    "content",
                    (
                        "Choose the campaign profile: recommended, exhaustive, "
                        "exploration, or quickscan?"
                    ),
                )
            else:
                yield StreamEvent("content", "Choice accepted")

    backend = ChoiceBackend()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "assess target"
        await app.action_send()
        await pilot.pause()
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "1. Recommended [default]" in transcript
        assert "Representative applicable coverage" in transcript
        app.query_one("#composer").text = reply
        await app.action_send()
        await pilot.pause()
        # The coverage profile and the specialist composition are orthogonal
        # operator decisions; answering the profile prompts for composition next.
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "Agent composition" in transcript
        assert "1. Autonomous" in transcript
        assert "reviewed agent template" in transcript
        assert "target-specific agent team" in transcript
        assert backend.prompts[-1] != expected
        app.query_one("#composer").text = ""
        await app.action_send()
        await pilot.pause()
        assert backend.prompts[-1].startswith(expected + "\n")
        assert "specialist_composition='autonomous'" in backend.prompts[-1]


@pytest.mark.asyncio
async def test_specialist_composition_choice_threads_autonomous_selection() -> None:
    class ChoiceBackend(DemoBackend):
        def __init__(self):
            self.prompts = []

        async def stream(self, prompt):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                yield StreamEvent(
                    "content",
                    (
                        "Choose the campaign profile: recommended, exhaustive, "
                        "exploration, or quickscan?"
                    ),
                )
            else:
                yield StreamEvent("content", "Choice accepted")

    backend = ChoiceBackend()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "assess target"
        await app.action_send()
        await pilot.pause()
        app.query_one("#composer").text = "1"
        await app.action_send()
        await pilot.pause()
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "independent of the coverage profile" in transcript
        app.query_one("#composer").text = "1"
        await app.action_send()
        await pilot.pause()
        prompt = backend.prompts[-1]
        assert prompt.startswith("recommended\n")
        assert "specialist composition: autonomous" in prompt
        assert "specialist_composition='autonomous'" in prompt
        assert "assessment_prepare" in prompt


@pytest.mark.asyncio
async def test_complete_campaign_profile_menu_is_not_rendered_twice() -> None:
    class ChoiceBackend(DemoBackend):
        async def stream(self, prompt):
            del prompt
            yield StreamEvent(
                "content",
                "Choose the campaign profile:\n"
                "1. Recommended [default]\n"
                "2. Quickscan\n"
                "3. Exhaustive\n"
                "4. Exploration / learning\n"
                "Press Enter or 1 for Recommended · 2 for Quickscan · "
                "3 for Exhaustive · 4 for Exploration",
            )

    app = MerlinApp(ChoiceBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "assess target"
        await app.action_send()
        await pilot.pause()
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert transcript.count("1. Recommended [default]") == 1
        assert transcript.count("2. Quickscan") == 1
        assert transcript.count("4. Exploration / learning") == 1


@pytest.mark.asyncio
async def test_merlin_brand_and_typed_active_work_rows() -> None:
    class ActivityBackend(DemoBackend):
        async def stream(self, prompt):
            yield StreamEvent("thinking", "planning bounded work")
            yield StreamEvent("skill", "prompt-review loaded")
            yield StreamEvent("tool", "target_chat completed")
            yield StreamEvent("subagent", "recon running")
            yield StreamEvent("swarm", "fan-in completed")
            yield StreamEvent("content", "Done")

    app = build_app(ActivityBackend())
    async with app.run_test() as pilot:
        welcome = str(app.query_one("#welcome").render())
        assert "security evaluation harness for AI systems" in welcome
        assert "Give Merlin a target and the security objective" in welcome
        assert "Assess https://example.com for prompt injection" in welcome
        assert "/setup to change provider" in welcome
        assert "|  Y Y  " in welcome

        composer = app.query_one("#composer")
        composer.text = "assess the target"
        await pilot.press("enter")
        await pilot.pause()
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "Skill · prompt-review loaded" in transcript
        assert "Tool · target_chat completed" in transcript
        assert "Subagent · recon running" in transcript
        assert "Swarm · fan-in completed" in transcript
        assert not app.query_one("#activity").has_class("visible")
        assert app.query_one("#welcome").has_class("hidden")
        assert app.query_one("#transcript").has_class("visible")

        app.action_details()
        await pilot.pause()
        detail = str(app.screen.query_one("#overview-content").render())
        assert "Thinking · planning bounded work" in detail
        assert "Swarm · fan-in completed" in detail


@pytest.mark.asyncio
async def test_transcript_renders_untrusted_markup_as_literal_text() -> None:
    payload = "[bold red]operator-controlled[/]"

    class MarkupBackend(DemoBackend):
        async def stream(self, prompt):
            del prompt
            yield StreamEvent("tool", payload)
            yield StreamEvent("target", payload)
            yield StreamEvent("error", payload)

    app = build_app(MarkupBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = payload
        await app.action_send()
        await pilot.pause()
        transcript = "\n".join(line.text for line in app.query_one("#transcript").lines)

    assert transcript.count(payload) == 4


def test_tool_argument_redaction_covers_session_cookie_and_credentials() -> None:
    rendered = app_module._render_tool_arguments(  # noqa: SLF001
        {
            "session_id": "session-secret",
            "cookie": "cookie-secret",
            "credential_handle": "credential-secret",
            "private_key_pem": "key-secret",
            "safe": "visible",
        }
    )

    assert "visible" in rendered
    assert "session-secret" not in rendered
    assert "cookie-secret" not in rendered
    assert "credential-secret" not in rendered
    assert "key-secret" not in rendered


def test_tool_argument_redaction_recurses_through_nested_collections() -> None:
    rendered = app_module._render_tool_arguments(  # noqa: SLF001
        {
            "request": {
                "headers": {"Authorization": "Bearer nested-secret"},
                "steps": [{"safe": "visible"}, {"api_key": "nested-api-secret"}],
            }
        }
    )

    assert "visible" in rendered
    assert "nested-secret" not in rendered
    assert "nested-api-secret" not in rendered


@pytest.mark.asyncio
async def test_backend_exception_is_literal_and_secret_scrubbed_in_transcript() -> None:
    class FailingBackend(DemoBackend):
        async def list_targets(self):
            raise RuntimeError("api_key=backend-secret [bold red]not markup[/]")

    app = build_app(FailingBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/targets"
        await app.action_send()
        await pilot.pause()
        transcript = "\n".join(line.text for line in app.query_one("#transcript").lines)

    assert "backend-secret" not in transcript
    assert "[bold red]not markup[/]" in transcript


@pytest.mark.asyncio
async def test_transcript_uses_same_right_edge_as_composer() -> None:
    app = MerlinApp(DemoBackend())
    async with app.run_test(size=(140, 50)) as pilot:
        app.query_one("#composer").text = "hello"
        await app.action_send()
        await pilot.pause()
        transcript = app.query_one("#transcript").region
        composer = app.query_one("#composer").region
        assert abs(transcript.right - composer.right) <= 3


@pytest.mark.asyncio
async def test_transcript_follows_tail_until_operator_scrolls_away() -> None:
    app = MerlinApp(DemoBackend())
    async with app.run_test(size=(100, 24)) as pilot:
        app._activate_conversation()
        transcript = app.query_one("#transcript")
        for index in range(80):
            transcript.write(f"line {index}")
        await pilot.pause()
        assert transcript.is_vertical_scroll_end

        transcript.scroll_page_up(animate=False)
        await pilot.pause()
        away_from_tail = transcript.scroll_y
        transcript.write("new while reviewing history")
        await pilot.pause()
        assert transcript.scroll_y == away_from_tail
        assert not transcript.is_vertical_scroll_end

        transcript.action_scroll_end()
        await pilot.pause()
        transcript.write("new at tail")
        await pilot.pause()
        assert transcript.is_vertical_scroll_end


@pytest.mark.asyncio
async def test_transcript_retains_only_recent_rendered_lines_and_keeps_live_tail_valid() -> None:
    app = MerlinApp(DemoBackend())
    async with app.run_test(size=(100, 24)) as pilot:
        app._activate_conversation()
        transcript = app.query_one("#transcript", LiveTranscript)
        transcript.max_lines = 5
        for index in range(8):
            transcript.write(f"settled {index}")
        transcript.replace_live(app_module.Text("draft one"))
        transcript.replace_live(app_module.Text("draft two"))
        transcript.commit_live()
        await pilot.pause()

        rendered = tuple(line.text.rstrip() for line in transcript.lines)
        assert len(rendered) == 5
        assert rendered[-1] == "draft two"
        assert "settled 0" not in rendered


@pytest.mark.asyncio
async def test_transcript_enforces_rendered_utf8_byte_limit(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "MAX_RENDERED_TRANSCRIPT_BYTES", 16)
    app = MerlinApp(DemoBackend())
    async with app.run_test(size=(100, 24)) as pilot:
        app._activate_conversation()
        transcript = app.query_one("#transcript", LiveTranscript)
        transcript.write("12345678")
        transcript.write("abcdefgh")
        transcript.write("newest")
        await pilot.pause()

        rendered = tuple(line.text.rstrip() for line in transcript.lines)
        assert rendered == ("abcdefgh", "newest")
        assert transcript._rendered_text_bytes <= 16  # noqa: SLF001


def _campaign_review_payload() -> dict[str, object]:
    return {
        "campaign_id": "campaign-1",
        "manifest_digest": "sha256:0123456789abcdef",
        "objective": "Assess the reviewed target",
        "profile": "recommended",
        "campaign_lead": 1,
        "specialist_team": 2,
        "maximum_concurrency": 3,
        "review": {
            "budget": {"turns": None, "tokens": None, "seconds": None, "cost_usd": None},
            "admitted_coverage": {"technique_count": 1, "technique_ids": ["prompt"]},
            "language_plan": {
                "baseline": "en",
                "policy": "English baseline",
                "language_evasion_cells": [],
            },
            "hypothesis_reserve": {"required_cells": 0, "rule": "none"},
            "capability_projection": {
                "packs": [],
                "skills": [],
                "mcp_tools": [],
                "tools": ["target_chat"],
                "authority": ["target-read"],
            },
            "target_contract": {
                "interface": "http_api",
                "endpoint": "https://example.test/chat",
                "adapter_id": "adapter-1",
                "session_constraint": None,
                "api_validated": True,
                "execution_mode": "api",
                "execution_options": ["api", "browser", "hybrid"],
            },
            "recon_evidence": {
                "references": ["evidence-1"],
                "input_locator": "#prompt",
                "submit_action": "button",
                "success_indicator": "response",
            },
            "provider_route": "model-routing:internal-id",
            "provider_model": "worker-model",
            "provider_effort": "default",
            "model_routes": {
                "campaign-lead": "lead-model",
                "recon": "recon-model",
                "adversarial-explorer": "explorer-model",
            },
            "model_route_details": {},
            "model_escalations": {},
            "specialist_composition": "opinionated",
            "authentication": "anonymous",
            "benign_controls": [],
            "specialist_routing": {"injection": ["prompt"]},
            "stopping_policy": "Stop after required work",
            "cleanup_obligations": ["Close session"],
            "known_limitations": [],
            "approval_binding": "Exact manifest digest",
        },
    }


def test_campaign_review_uses_operator_facing_routes_limits_and_assignments() -> None:
    payload = _campaign_review_payload()
    console = Console(record=True, width=240)
    console.print(app_module._render_campaign_review(json.dumps(payload)))  # noqa: SLF001
    rendered = console.export_text()

    assert "Provider              model-routing:" not in rendered
    assert "Campaign Lead: lead-model" in rendered
    assert "Recon: recon-model" in rendered
    assert "Worker: worker-model" in rendered
    assert "Validator: worker-model" in rendered
    assert "Adversarial explorer: explorer-model" in rendered
    assert "No campaign-wide turn, token, time, or cost cap configured" in rendered
    assert "up to 2 specialist tasks at once" in rendered
    assert "Test assignments" in rendered
    assert "injection runs: prompt" in rendered
    assert "Specialist routing" not in rendered
    assert "Unlimited" not in rendered
    assert "sha256:0123456789abcdef" in rendered


@pytest.mark.asyncio
async def test_campaign_review_prints_numbered_choices_without_waiting_for_input() -> None:
    class ReviewBackend(DemoBackend):
        async def stream(self, prompt: str):
            del prompt
            yield StreamEvent("campaign_review", json.dumps(_campaign_review_payload()))

    app = MerlinApp(ReviewBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "prepare campaign"
        await app.action_send()
        await pilot.pause()
        await pilot.pause()
        rendered = " ".join(
            " ".join(line.text.split()) for line in app.query_one("#transcript").lines
        )

    assert "1 (Approve [default])" in rendered
    assert "2 (Use BROWSER and review again)" in rendered
    assert "3 (Use HYBRID and review again)" in rendered
    assert "Press Enter for the default." in rendered


@pytest.mark.asyncio
async def test_truncated_preparation_still_displays_review_and_approval_choices() -> None:
    from datetime import UTC, datetime

    from merlin_contracts import AppEvent, ToolResult
    from merlin_kernel.kernel import _campaign_review_event_payload
    from merlin_tui.kernel_backend import event_to_stream

    prepared = _campaign_review_payload()
    prepared["profile"] = "quickscan"
    prepared["review"]["specialist_composition"] = "autonomous"
    prepared["review"]["cross_campaign_learning"] = {
        "new": 3000,
        "decisions": [{"basis": "x" * 1200}] * 3000,
    }
    full = json.dumps(prepared)
    event = AppEvent(
        type="tool",
        occurred_at=datetime.now(UTC),
        payload={
            "name": "assessment_prepare",
            "state": "completed",
            "text": full[: 1024 * 1024],
            **_campaign_review_event_payload(
                "assessment_prepare",
                ToolResult(
                    content=full[: 1024 * 1024],
                    metadata=prepared,
                ),
            ),
        },
    )

    class ReviewBackend(DemoBackend):
        async def stream(self, prompt):
            yield event_to_stream(event)

    app = MerlinApp(ReviewBackend())
    async with app.run_test(size=(140, 45)) as pilot:
        app.query_one("#composer").text = "prepare campaign"
        await app.action_send()
        await pilot.pause()
        await pilot.pause()
        rendered = " ".join(
            " ".join(line.text.split()) for line in app.query_one("#transcript").lines
        )
        assert "quickscan" in rendered
        assert "Autonomous specialist selection" in rendered
        assert "Approve [default]" in rendered
        assert "Request other changes" in rendered
        assert "Cancel campaign preparation" in rendered
        assert app._pending_campaign_review == (
            prepared["campaign_id"],
            prepared["manifest_digest"],
        )
        assert app._pending_choice_kind == "campaign_review"


@pytest.mark.asyncio
async def test_copy_transcript_opens_selectable_read_only_view() -> None:
    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        app._activate_conversation()
        app.query_one("#transcript").write("selectable transcript text")
        await pilot.pause()
        app.action_copy_transcript()
        await pilot.pause()
        copy_area = app.screen.query_one("#transcript-copy-text", TextArea)
        assert copy_area.read_only is True
        assert "selectable transcript text" in copy_area.text


@pytest.mark.asyncio
async def test_transcript_body_selection_preserves_lines_and_wide_characters() -> None:
    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        app._activate_conversation()
        transcript = app.query_one("#transcript", LiveTranscript)
        transcript.write("alpha bravo")
        transcript.write("中文 result")
        await pilot.pause()

        transcript._selection_anchor = (0, 6)
        transcript._selection_focus = (1, 4)

        assert transcript.selected_text == "bravo\n中文"
        assert transcript._selection_for_line(0) == (6, 11)
        assert transcript._selection_for_line(1) == (0, 4)


@pytest.mark.asyncio
async def test_transcript_render_line_applies_selection_highlight() -> None:
    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        app._activate_conversation()
        transcript = app.query_one("#transcript", LiveTranscript)
        transcript.write("alpha bravo")
        await pilot.pause()

        transcript._selection_anchor = (0, 3)
        transcript._selection_focus = (0, 8)

        strip = transcript.render_line(0)
        styled = [
            segment
            for segment in strip
            if segment.style is not None and segment.style.bgcolor is not None
        ]
        assert styled, "selection highlight must produce background-styled segments"
        assert any(segment.style.bgcolor.triplet == (38, 79, 120) for segment in styled)


@pytest.mark.asyncio
async def test_permission_selector_supports_numbers_arrows_enter_and_mouse() -> None:
    request = PermissionRequest(
        effect=PermissionEffect.UNKNOWN,
        scope=PermissionScope(
            session_id="session-1",
            workspace_root="/workspace",
            tool_name="browser_navigate",
        ),
    )
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        first = asyncio.create_task(app.resolve_permission(request))
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert app.query_one("#permission-drawer").has_class("visible")
        assert app.query_one("#permission-allow-once", Button).has_focus
        drawer = app.query_one("#permission-drawer").region
        composer = app.query_one("#composer").region
        assert drawer.x == composer.x
        assert drawer.right == composer.right
        await pilot.press("down", "enter")
        await pilot.pause()
        assert str(await first) == "allow_session"

        second = asyncio.create_task(app.resolve_permission(request))
        await pilot.pause()
        app.query_one("#permission-deny-once", Button).press()
        await pilot.pause()
        assert str(await second) == "deny_once"
        assert not app.query_one("#permission-drawer").has_class("visible")
        assert not app.query_one("#composer").disabled


@pytest.mark.asyncio
async def test_concurrent_permission_requests_are_presented_in_order() -> None:
    def request(target: str) -> PermissionRequest:
        return PermissionRequest(
            effect=PermissionEffect.TARGET_NETWORK,
            scope=PermissionScope(
                session_id="session-1",
                workspace_root="/workspace",
                tool_name="target_chat",
                target=target,
            ),
        )

    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        first = asyncio.create_task(app.resolve_permission(request("target-first")))
        second = asyncio.create_task(app.resolve_permission(request("target-second")))
        await pilot.pause()
        assert "target-first" in str(app.query_one("#permission-scope").render())
        app.query_one("#permission-allow-once", Button).press()
        await pilot.pause()
        assert str(await first) == "allow_once"
        assert "target-second" in str(app.query_one("#permission-scope").render())
        app.query_one("#permission-deny-once", Button).press()
        await pilot.pause()
        assert str(await second) == "deny_once"


@pytest.mark.asyncio
async def test_foreground_lease_refusal_is_surfaced_and_exits() -> None:
    class RefusedBackend(DemoBackend):
        async def acquire_foreground_lease(self) -> None:
            raise RuntimeError("already has a foreground owner")

    app = MerlinApp(RefusedBackend())
    notices: list[tuple[str, str]] = []
    exited: list[bool] = []
    app.notify = lambda message, *, severity="information", **_kwargs: notices.append(  # type: ignore[method-assign]
        (message, severity)
    )
    app.exit = lambda *_args, **_kwargs: exited.append(True)  # type: ignore[method-assign]

    acquired = await app._acquire_foreground_lease()  # noqa: SLF001

    assert notices == [
        ("Foreground ownership was refused: already has a foreground owner", "error")
    ]
    assert exited == [True]
    assert acquired is False


@pytest.mark.asyncio
async def test_app_teardown_cancels_and_awaits_owned_background_tasks() -> None:
    class SlowMetadataBackend(DemoBackend):
        async def get_runtime_status(self):
            await asyncio.Event().wait()

    app = MerlinApp(SlowMetadataBackend())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._background_tasks  # noqa: SLF001

    assert not app._background_tasks  # noqa: SLF001


@pytest.mark.asyncio
async def test_runtime_footer_retries_transient_daemon_metadata_failure() -> None:
    class FlakyRuntimeBackend(DemoBackend):
        runtime_calls = 0

        async def get_runtime_status(self):
            self.runtime_calls += 1
            if self.runtime_calls == 1:
                raise ConnectionError("daemon is still attaching")
            return {
                "provider": "https://api.example.test/v1",
                "model": "secure-model",
                "effort": "high",
            }

    backend = FlakyRuntimeBackend()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        status = str(app.query_one("#status").render())
        assert backend.runtime_calls == 2
        assert "mode auto · daemon attached" in status
        assert "api.example.test" not in status
        assert "secure-model" not in status
        assert "effort" not in status


@pytest.mark.asyncio
async def test_palette_and_setup_overlay_open() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        assert app.screen.query_one("#dialog")
        await pilot.press("escape")
        app.query_one("#composer").text = "/setup"
        await app.action_send()
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        assert app.screen.query_one("#setup-api-key")


@pytest.mark.asyncio
async def test_setup_masks_secret_and_never_renders_it() -> None:
    secret = "super-secret-value"
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/setup"
        await app.action_send()
        await pilot.pause()
        await pilot.press("8")
        await pilot.pause()
        app.screen.query_one("#setup-base-url", Input).value = "https://example.test/v1"
        app.screen.query_one("#setup-model", Input).value = "secure-model"
        key_input = app.screen.query_one("#setup-api-key", Input)
        key_input.value = secret
        assert key_input.password
        app.screen.query_one("#save", Button).press()
        await pilot.pause()
        # Dismiss the follow-up judgment-model question.
        await pilot.press("escape")
        await pilot.pause()
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert secret not in transcript
        assert secret not in repr(ProviderSetup("url", "model", secret))


@pytest.mark.asyncio
async def test_provider_setup_reconnects_daemon_before_accepting_another_prompt(
    monkeypatch,
) -> None:
    class ReconnectingBackend(DemoBackend):
        reconnected = False

        async def configure_provider(
            self,
            base_url,
            model,
            api_key,
            vision=False,
            context_window_tokens=None,
            effort="default",
        ):  # noqa: ANN001, ANN202
            return "Provider configuration saved securely; restart Merlin to reconnect."

        async def apply_saved_model_routing(self):  # noqa: ANN202
            self.reconnected = True
            return "Daemon reconnected automatically; new campaigns use this provider."

    backend = ReconnectingBackend()
    app = build_app(backend)
    writes = []
    monkeypatch.setattr(
        RichLog,
        "write",
        lambda _self, value, *args, **kwargs: writes.append(str(value)),
    )
    async with app.run_test() as pilot:
        await app._configure_provider(
            ProviderSetup(
                "https://api.deepseek.com/v1",
                "deepseek-v4-flash",
                "opaque-secret",
            )
        )
        await pilot.pause()

        assert backend.reconnected is True
        transcript = "".join(writes)
        assert "Daemon reconnected automatically" in transcript
        assert "restart Merlin to reconnect" not in transcript


@pytest.mark.asyncio
async def test_setup_jev_key_entry_saves_token_without_leaking_it(monkeypatch) -> None:
    secret = "jev-secret-value"
    saved = []
    enabled = []

    class FakeProviderConfiguration:
        def save_jev_token(self, token):  # noqa: ANN001, ANN202
            saved.append(token)

        def save_jev_enabled(self, flag):  # noqa: ANN001, ANN202
            enabled.append(bool(flag))

    def _fail_post(*_args, **_kwargs):  # noqa: ANN001, ANN202
        raise OSError("offline test host")

    monkeypatch.setattr("httpx.post", _fail_post)
    monkeypatch.setattr("merlin_tui.configuration.ProviderConfiguration", FakeProviderConfiguration)
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/setup"
        await app.action_send()
        await pilot.pause()
        # Pick the custom OpenAI-compatible provider route.
        await pilot.press("8")
        await pilot.pause()
        app.screen.query_one("#setup-base-url", Input).value = "https://example.test/v1"
        app.screen.query_one("#setup-model", Input).value = "secure-model"
        app.screen.query_one("#save", Button).press()
        await pilot.pause()
        for _ in range(20):
            await pilot.pause(0.1)
            if "judgment model (Jev)" in str(app.screen):
                break
        # Second step: opt into Jev (default is off).
        await pilot.press("2")
        await pilot.pause()
        key_input = app.screen.query_one("#jev-api-key", Input)
        key_input.value = secret
        assert key_input.password
        app.screen.query_one("#save", Button).press()
        await pilot.pause()
        for _ in range(20):
            await pilot.pause(0.1)
            if "Saved judgment model (Jev) API key" in "".join(
                line.text for line in app.query_one("#transcript").lines
            ):
                break

        assert saved == [secret]
        assert enabled == [True]
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        # The unreachable endpoint must not block saving.
        assert "Jev health check failed" in transcript
        assert secret not in transcript
        assert secret not in repr(app_module.JevSetup(secret))


@pytest.mark.asyncio
async def test_setup_endpoint_command_registers_model_endpoint_without_leaking_key() -> None:
    secret = "endpoint-secret-value"

    class EndpointBackend(DemoBackend):
        configured = None

        async def configure_model_endpoint(self, model, base_url, api_key):
            self.configured = (model, base_url, api_key)
            return f"Endpoint saved for {model}"

    backend = EndpointBackend()
    app = build_app(backend)
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/setup endpoint"
        await app.action_send()
        await pilot.pause()
        app.screen.query_one("#endpoint-model", Input).value = "claude-haiku"
        app.screen.query_one("#endpoint-base-url", Input).value = "https://anthropic.test"
        key_input = app.screen.query_one("#endpoint-api-key", Input)
        key_input.value = secret
        assert key_input.password
        app.screen.query_one("#save", Button).press()
        await pilot.pause()

        assert backend.configured == ("claude-haiku", "https://anthropic.test", secret)
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "Endpoint saved for claude-haiku" in transcript
        assert secret not in transcript
        assert secret not in repr(EndpointSetup("model", "url", secret))


@pytest.mark.asyncio
async def test_setup_discovers_current_models_and_applies_reported_capabilities(
    monkeypatch,
) -> None:
    from merlin_tui.provider_catalog import ModelCandidate, ModelDiscovery

    monkeypatch.setattr(
        "merlin_tui.provider_catalog.discover_model_catalog",
        lambda *args, **kwargs: ModelDiscovery(
            (ModelCandidate("current-model", vision=True, context_window_tokens=262_144),)
        ),
    )
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/setup"
        await app.action_send()
        await pilot.pause()
        await pilot.press("6")
        await pilot.pause()
        app.screen.query_one("#setup-api-key", Input).value = "opaque-secret"
        app.screen.query_one("#discover-models", Button).press()
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()

        assert app.screen.query_one("#setup-model", Input).value == "current-model"
        assert app.screen.query_one("#setup-vision", Checkbox).value is True
        assert app.screen.query_one("#setup-context-window", Input).value == "262144"


@pytest.mark.asyncio
async def test_unchecked_setup_vision_persists_exact_model_denial(tmp_path) -> None:
    from merlin_config import ConfigPaths, load_config
    from merlin_tui.configuration import ProviderConfiguration

    paths = ConfigPaths(tmp_path / "merlin")

    class PersistingBackend(DemoBackend):
        async def configure_provider(
            self,
            base_url,
            model,
            api_key,
            vision=False,
            context_window_tokens=None,
            effort="default",
        ):  # noqa: ANN001, ANN202
            ProviderConfiguration(paths).save(
                base_url,
                model,
                api_key,
                vision=vision,
                context_window_tokens=context_window_tokens,
                effort=effort,
            )
            return "provider saved"

        async def apply_saved_model_routing(self):  # noqa: ANN202
            return "daemon reconnected"

    app = build_app(PersistingBackend())
    async with app.run_test() as pilot:
        app.push_screen(
            SetupScreen(
                "openai",
                base_url="https://example.test/v1",
                model="catalog-vision-model",
            ),
            app._save_provider,  # noqa: SLF001
        )
        await pilot.pause()
        app.screen.query_one("#setup-api-key", Input).value = "opaque-secret"
        assert app.screen.query_one("#setup-vision", Checkbox).value is False
        app.screen.query_one("#save", Button).press()
        for _ in range(20):
            await pilot.pause(0.1)
            if paths.config.exists():
                break

    config = load_config(paths, environ={})
    assert config.values["provider"]["model_capabilities"] == {"catalog-vision-model": []}
    assert config.provider_model_capabilities == {"catalog-vision-model": ()}


@pytest.mark.asyncio
async def test_model_routing_screen_keeps_exact_discovered_vision(monkeypatch) -> None:
    from merlin_config import Config
    from merlin_tui.provider_catalog import ModelCandidate, ModelDiscovery

    config = Config(
        values={
            "provider": {
                "base_url": "https://example.test/v1",
                "model": "text-worker",
            }
        }
    )
    monkeypatch.setattr("merlin_config.load_config", lambda: config)
    monkeypatch.setattr("merlin_config.CredentialStore.load", lambda _self: {})

    async def discover(_provider, _api_key):
        return {
            "openai_compatible": ModelDiscovery(
                (
                    ModelCandidate("text-worker", vision=False),
                    ModelCandidate("vision-recon", vision=True),
                )
            ),
            "openai_responses": ModelDiscovery((ModelCandidate("vision-recon", vision=None),)),
        }

    monkeypatch.setattr("merlin_tui.provider_catalog.discover_routing_models", discover)

    screen = await app_module._configured_model_screen("text-worker")

    assert screen.model_vision == {"text-worker": False, "vision-recon": True}


@pytest.mark.asyncio
async def test_model_screen_saves_worker_and_lead_routes_together(monkeypatch) -> None:
    from merlin_tui.provider_catalog import ModelCandidate, ModelDiscovery

    class RouteBackend(DemoBackend):
        configured = None
        applied = False

        async def set_model_routing(
            self,
            worker_model,
            worker_transport,
            heterogeneous,
            lead_model,
            lead_transport,
            recon_model,
            recon_transport,
            validator_model,
            validator_transport,
            adversarial_explorer_model,
            adversarial_explorer_transport,
            role_efforts,
            judgments_jev_enabled=None,
            model_vision=None,
        ):
            self.configured = (
                worker_model,
                worker_transport,
                heterogeneous,
                lead_model,
                lead_transport,
                recon_model,
                recon_transport,
                validator_model,
                validator_transport,
                adversarial_explorer_model,
                adversarial_explorer_transport,
                role_efforts,
                model_vision,
                judgments_jev_enabled,
            )
            return "route saved"

        async def apply_saved_model_routing(self):
            self.applied = True
            return "daemon reconnected"

    backend = RouteBackend()

    def _fail_http(*_args, **_kwargs):  # noqa: ANN001, ANN202
        raise OSError("offline test host")

    monkeypatch.setattr("httpx.get", _fail_http)
    monkeypatch.setattr("httpx.post", _fail_http)
    monkeypatch.setattr(
        "merlin_tui.provider_catalog.discover_claude_cli",
        lambda: ModelDiscovery((ModelCandidate("lead-model"),)),
    )
    monkeypatch.setattr(
        app_module,
        "_configured_model_screen",
        AsyncMock(
            return_value=ModelScreen(
                "worker-model",
                models_by_transport={
                    "openai_compatible": ("worker-model", "recon-model"),
                    "codex_app_server": ("worker-model",),
                    "claude_cli": ("lead-model",),
                    "anthropic_messages": ("validator-model",),
                    "gemini": ("explorer-model",),
                },
                model_vision={"recon-model": True},
            )
        ),
    )
    app = build_app(backend)
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/model"
        await app.action_send()
        await pilot.pause()
        app.screen.query_one("#worker-transport", Select).value = "codex_app_server"
        app.screen.query_one("#lead-transport", Select).value = "claude_cli"
        assert not app.screen.query("#recon-override")
        app.screen.query_one("#validator-transport", Select).value = "anthropic_messages"
        app.screen.query_one("#adversarial-explorer-transport", Select).value = "gemini"
        await pilot.pause()
        app.screen.query_one("#worker-model", Select).value = "worker-model"
        app.screen.query_one("#lead-model", Select).value = "lead-model"
        app.screen.query_one("#recon-model", Select).value = "recon-model"
        app.screen.query_one("#validator-model", Select).value = "validator-model"
        app.screen.query_one("#adversarial-explorer-model", Select).value = "explorer-model"
        app.screen.query_one("#worker-effort", Select).value = "low"
        app.screen.query_one("#lead-effort", Select).value = "high"
        app.screen.query_one("#judgments-jev-enabled", Checkbox).value = False
        app.screen.query_one("#save", Button).press()
        await pilot.pause()
        # Wait for the routing save to land on the transcript.
        for _ in range(20):
            await pilot.pause(0.1)
            if "route saved" in "".join(line.text for line in app.query_one("#transcript").lines):
                break

        assert backend.configured == (
            "worker-model",
            "codex_app_server",
            True,
            "lead-model",
            "claude_cli",
            "recon-model",
            "openai_compatible",
            "validator-model",
            "anthropic_messages",
            "explorer-model",
            "gemini",
            {
                "workers": "low",
                "lead": "high",
                "recon": "default",
                "validator": "default",
                "adversarial-explorer": "default",
            },
            {"recon-model": True},
            False,
        )
        assert backend.applied is True
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "route saved daemon reconnected" in transcript


@pytest.mark.asyncio
async def test_model_routing_reports_claude_cli_preflight_failure(monkeypatch) -> None:
    from merlin_tui.provider_catalog import ModelDiscovery

    class RouteBackend(DemoBackend):
        called = False

        async def set_model_routing(self, *args, **kwargs):
            self.called = True
            raise AssertionError("routing must not be saved when Claude CLI is unavailable")

    monkeypatch.setattr(
        "merlin_tui.provider_catalog.discover_claude_cli",
        lambda: ModelDiscovery(error="Claude CLI is not authenticated; retry /model."),
    )
    backend = RouteBackend()
    app = build_app(backend)
    setup = app_module.ModelSetup(
        "worker-model",
        "openai_compatible",
        True,
        "sonnet",
        "claude_cli",
        "worker-model",
        "openai_compatible",
        "worker-model",
        "openai_compatible",
        "worker-model",
        "openai_compatible",
        {},
        {},
        False,
    )

    async with app.run_test() as pilot:
        await app._change_model(setup)  # noqa: SLF001
        await pilot.pause()
        assert backend.called is False


@pytest.mark.asyncio
async def test_target_overview() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/targets"
        await app.action_send()
        await pilot.pause()
        assert "Demo target" in str(app.screen.query_one("#overview-content").render())


@pytest.mark.asyncio
async def test_skills_drawer_renders_state_and_errors() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/skills"
        await app.action_send()
        await pilot.pause()
        content = str(app.screen.query_one("#overview-content").render())
        assert "prompt-review · enabled" in content
        assert "legacy-audit · disabled" in content
        assert "broken-demo · disabled · error" in content
        assert "Error: manifest invalid" in content


@pytest.mark.asyncio
async def test_skill_invocation_remains_visible_composer_text() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "$prompt-review inspect this"
        await app.action_send()
        await pilot.pause()
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "● $prompt-review inspect this" in transcript
        assert "$prompt-review inspect this" in transcript
        assert "Demo response: $prompt-review inspect this" in transcript


@pytest.mark.asyncio
async def test_skills_is_available_in_palette() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        assert app.screen.query_one("#choice-skills")


@pytest.mark.asyncio
async def test_mcp_drawer_renders_health_tools_calls_and_sanitized_stderr() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/mcp"
        await app.action_send()
        await pilot.pause()
        content = str(app.screen.query_one("#overview-content").render())
        assert "demo-filesystem · healthy" in content
        assert "read_file, list_directory" in content
        assert "read_file · completed" in content
        assert "connection ready" in content
        assert "Transport: stdio · Admission: admitted" in content
        assert "Identity: demo-filesystem/1.0" in content
        assert "Inventory digest: sha256:demo-inventory" in content
        assert "Recent audit:" in content
        assert "demo-remote · stopped · unavailable · disabled" in content
        assert "Transport: streamable_http · Admission: quarantined" in content
        assert "Reconnect: 2 attempts · 0.1s delay" in content


@pytest.mark.asyncio
async def test_mcp_is_available_in_palette() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        assert app.screen.query_one("#choice-mcp")
        assert app.screen.query_one("#choice-mcp-remote")


@pytest.mark.asyncio
async def test_remote_mcp_credential_flow_masks_and_never_transcribes_secret() -> None:
    secret = "remote-bearer-must-stay-private"
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/mcp remote"
        await app.action_send()
        await pilot.pause()
        app.screen.query_one("#mcp-remote-name", Input).value = "production"
        app.screen.query_one("#mcp-remote-url", Input).value = "https://mcp.example.test/mcp"
        app.screen.query_one("#mcp-credential-handle", Input).value = "mcp-production"
        secret_input = app.screen.query_one("#mcp-credential-secret", Input)
        secret_input.value = secret
        assert secret_input.password
        app.screen.query_one("#mcp-remote-save", Button).press()
        await pilot.pause()
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert secret not in transcript
        assert secret not in repr(RemoteMcpSetup("production", "https://x", "handle", secret))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("button_id", "expected"),
    (("#tool-confirm-allow", True), ("#tool-confirm-deny", False)),
)
async def test_unknown_effect_mcp_call_requires_per_call_decision(
    button_id: str, expected: bool
) -> None:
    class ToolSpec:
        name = "mcp__demo__write_file"

    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        decision = asyncio.create_task(
            app.confirm_tool_call(
                ToolSpec(),
                object(),
                {"path": "notes.txt", "api_token": "must-not-render"},
            )
        )
        await pilot.pause()
        assert "mcp__demo__write_file" in str(app.screen.query_one("#tool-confirm-name").render())
        arguments = str(app.screen.query_one("#tool-confirm-arguments").render())
        assert "notes.txt" in arguments
        assert "must-not-render" not in arguments
        app.screen.query_one(button_id, Button).press()
        await pilot.pause()
        assert await decision is expected


@pytest.mark.asyncio
async def test_agents_drawer_renders_runtime_and_result() -> None:
    class ScopedDemoBackend(DemoBackend):
        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/live",
                    "session-demo",
                    "Demo campaign",
                    "recommended",
                    "executing",
                    "digest",
                ),
            )

        async def list_agents(self):
            agents = await super().list_agents()
            return tuple(
                replace(
                    agent,
                    name=(f"worker-campaign/campaign/live/demo/agenda-{index:02d}/direct"),
                )
                for index, agent in enumerate(agents)
            )

    app = build_app(ScopedDemoBackend())
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#composer").text = "/agents"
        await app.action_send()
        await pilot.pause()
        view = app.screen.query_one("#overview-content", app_module.AgentOutputText)
        content = view.plain
        assert "Review provider boundary" in content
        assert app.screen.query_one("#agent-workspace").region.width == app.size.width
        second = app.screen.query_one("#worker-output-1", app_module.AgentOutputText).plain
        assert "12 tests passed" in second
        await pilot.press("enter")
        await pilot.pause()
        details = app.screen.query_one("#overview-content", app_module.AgentOutputText).plain
        assert "inspected provider adapter" in details.lower()
        assert "Full assignment\nReview provider boundary" in details
        assert "openai-compatible / gpt-demo" in details
        assert "Authority" not in details
        assert "Budgets" not in details


@pytest.mark.asyncio
async def test_tasks_drawer_renders_rust_faithful_schedule_node_and_attempt_details() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/tasks"
        await app.action_send()
        await pilot.pause()
        content = str(app.screen.query_one("#overview-content").render())
        assert "schedule-7 · running" in content
        assert "Graph: sha256:demo-graph" in content
        assert "Task node-recon · running" in content
        assert "Profile: reconnaissance · Technique: surface-enumeration" in content
        assert "Target adapter: demo-target/http-chat" in content
        assert "Authority: tools: read_file; grants: observe" in content
        assert "Budgets: turns 3/8 · tokens 1200/8000" in content
        assert "Dependencies: node-bootstrap" in content
        assert "Attempt attempt-2 · leased · worker: researcher" in content
        assert "Lease: generation 2 · expires 2026-08-27T12:05:00Z" in content
        assert "Retries: 1/2" in content
        assert "Cleanup: pending · Quarantine: not quarantined" in content
        assert "Mailbox: lead redirect · delivered" in content
        assert "Latest event: checkpoint committed" in content
        assert "Result: candidate endpoints recorded" in content


@pytest.mark.asyncio
async def test_agent_inspection_commands_are_available_in_palette() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        assert app.screen.query_one("#choice-agents")
        assert app.screen.query_one("#choice-tasks")
        assert app.screen.query_one("#choice-agent-message")


@pytest.mark.asyncio
async def test_findings_drawer_shows_disposition_and_navigates_evidence() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/findings"
        await app.action_send()
        await pilot.pause()
        content = str(app.screen.query_one("#overview-content").render())
        assert "System prompt disclosure (finding-1) · high · draft" in content
        assert "Verbose error response (finding-2) · low · accepted" in content
        app.screen.query_one("#evidence-0", Button).press()
        await pilot.pause()
        assert "run-7/message-12" in str(app.screen.query_one("#overview-content").render())


@pytest.mark.asyncio
async def test_finding_disposition_and_report_command_events_render_in_transcript() -> None:
    class CommandBackend(DemoBackend):
        async def stream(self, prompt):
            if prompt == "/findings accept finding-1":
                yield StreamEvent("status", "Finding finding-1 accepted")
            elif prompt.startswith("/report run"):
                yield StreamEvent(
                    "status", "Report completed → reports/run-7.md · 1 accepted findings"
                )

    app = build_app(CommandBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/findings accept finding-1"
        await app.action_send()
        await pilot.pause()
        app.query_one("#composer").text = "/report run --format markdown --output reports/run-7.md"
        await app.action_send()
        await pilot.pause()
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "Finding finding-1 accepted" in transcript
        assert "Report completed → reports/run-7.md · 1 accepted findings" in transcript


@pytest.mark.asyncio
async def test_report_drawer_and_phase_six_palette_commands() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/report"
        await app.action_send()
        await pilot.pause()
        content = str(app.screen.query_one("#overview-content").render())
        assert "Campaign run-7" in content
        assert "Saved MARKDOWN report" in content
        assert "reports/run-7.md" in content
        assert "1 findings" in content
        await pilot.press("escape")
        await pilot.press("ctrl+p")
        assert app.screen.query_one("#choice-findings")
        assert app.screen.query_one("#choice-report")


@pytest.mark.asyncio
async def test_mode_is_persistently_visible_and_yolo_is_emphasized() -> None:
    backend = DemoBackend()
    app = build_app(backend)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = str(app.query_one("#status").render())
        assert "demo-model" not in status
        assert "effort" not in status
        assert "mode auto" in status
        assert "daemon attached" in status
        app.query_one("#composer").text = "/mode yolo"
        await app.action_send()
        await pilot.pause()
        assert "mode YOLO ⚠" in str(app.query_one("#status").render())
        app.query_one("#composer").text = "hello"
        await app.action_send()
        await pilot.pause()
        assert "mode YOLO ⚠" in str(app.query_one("#status").render())


@pytest.mark.asyncio
async def test_runtime_status_wraps_instead_of_clipping_at_narrow_width() -> None:
    app = build_app(DemoBackend())
    async with app.run_test(size=(60, 30)) as pilot:
        await pilot.pause()
        status = app.query_one("#status")
        rendered = str(status.render())

        assert status.region.height == 1
        assert "mode auto" in rendered
        assert "effort" not in rendered
        assert "daemon attached" in rendered
        assert app.query_one("#composer").region.height == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("button_id", "expected"),
    (
        ("#permission-allow-once", "allow_once"),
        ("#permission-allow-session", "allow_session"),
        ("#permission-deny-once", "deny_once"),
        ("#permission-deny-session", "deny_session"),
    ),
)
async def test_inline_permission_drawer_resolves_four_scoped_decisions(
    button_id: str, expected: str
) -> None:
    request = PermissionRequest(
        PermissionEffect.TARGET_NETWORK,
        PermissionScope("session-1", "target_chat", "/workspace", target="target-a"),
    )
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        decision = asyncio.create_task(app.resolve_permission(request))
        await pilot.pause()
        assert len(app.screen_stack) == 1
        scope = str(app.query_one("#permission-scope").render())
        assert "target_network" in scope
        assert "target_chat" in scope
        assert "Target: target-a" in scope
        app.query_one(button_id, Button).press()
        await pilot.pause()
        assert str(await decision) == expected


@pytest.mark.asyncio
async def test_daemon_permission_metadata_keeps_effect_and_target_scope() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        decision = asyncio.create_task(
            app.confirm_permission_tool(
                SimpleNamespace(name="assessment_execute"),
                SimpleNamespace(
                    session_id="session-1",
                    workspace_root="/workspace",
                    target_id="target-reviewed",
                ),
                {
                    "__permission_effect": "target_network",
                    "__permission_target": "target-reviewed",
                    "__permission_plugin": None,
                    "__permission_group": "campaign-execution",
                },
            )
        )
        await pilot.pause()
        scope = str(app.query_one("#permission-scope").render())
        assert "Effect: target_network" in scope
        assert "Tool: assessment_execute" in scope
        assert "Target: target-reviewed" in scope
        assert "Scope: campaign-execution" in scope
        app.query_one("#permission-deny-once", Button).press()
        await pilot.pause()
        assert str(await decision) == "deny_once"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("button_id", "expected"),
    (
        ("#permission-allow-once", "allow_once"),
        ("#permission-allow-session", "allow_session"),
        ("#permission-deny-once", "deny_once"),
        ("#permission-deny-session", "deny_session"),
    ),
)
async def test_shell_permission_shows_execution_before_each_decision(
    tmp_path, button_id: str, expected: str
) -> None:
    workspace = tmp_path / "workspace"
    working_directory = workspace / "scripts"
    working_directory.mkdir(parents=True)
    argv = ["python", "-c", "print('[bold]literal text[/bold]')", "argument with spaces"]
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        decision = asyncio.create_task(
            app.confirm_permission_tool(
                SimpleNamespace(name="shell_command"),
                SimpleNamespace(session_id="session-shell", workspace_root=workspace),
                {"argv": argv, "cwd": "scripts", "__permission_effect": "subprocess"},
            )
        )
        await pilot.pause()
        details = str(app.query_one("#permission-details").render())
        scope = str(app.query_one("#permission-scope").render())
        assert not decision.done()
        assert str(working_directory.resolve()) in details
        assert "[bold]literal text[/bold]" in details
        assert "argument with spaces" in details
        assert shlex.join(argv) in details
        displayed_arguments, _ = json.JSONDecoder().raw_decode(
            details.split("Arguments (sensitive values redacted):", 1)[1].lstrip()
        )
        assert displayed_arguments["argv"] == argv
        assert "Reason:" in scope
        session_label = str(app.query_one("#permission-allow-session", Button).label).lower()
        assert "all commands" in session_label
        assert "session" in session_label
        app.query_one(button_id, Button).press()
        await pilot.pause()
        assert str(await decision) == expected


@pytest.mark.asyncio
async def test_shell_permission_long_command_is_scrollable_before_approval(tmp_path) -> None:
    command = "prefix-" + "x" * 8000 + "-review-this-command-tail"
    app = build_app(DemoBackend())
    async with app.run_test(size=(90, 32)) as pilot:
        decision = asyncio.create_task(
            app.confirm_permission_tool(
                SimpleNamespace(name="shell_command"),
                SimpleNamespace(session_id="session-long", workspace_root=tmp_path),
                {"argv": ["python", "-c", command], "__permission_effect": "subprocess"},
            )
        )
        await pilot.pause()
        assert not decision.done()
        details = str(app.query_one("#permission-details").render())
        assert command in details
        scroll = app.query_one("#permission-details-scroll")
        assert scroll.max_scroll_y > 0
        await pilot.press("pagedown")
        await pilot.pause()
        assert scroll.scroll_y > 0
        await pilot.press("pageup")
        await pilot.pause()
        assert scroll.scroll_y == 0
        assert app.query_one("#permission-allow-once", Button).region.height > 0
        app.query_one("#permission-deny-once", Button).press()
        await pilot.pause()
        assert str(await decision) == "deny_once"


@pytest.mark.asyncio
async def test_shell_permission_redacts_secrets_without_mutating_execution_arguments(
    tmp_path,
) -> None:
    argv = [
        "curl",
        "https://example.test/api",
        "--token",
        "private-token-value",
        "-H",
        "Authorization: Bearer private-bearer-value",
        "API_KEY=private-environment-value",
    ]
    arguments = {"argv": argv.copy(), "__permission_effect": "subprocess"}
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        decision = asyncio.create_task(
            app.confirm_permission_tool(
                SimpleNamespace(name="shell_command"),
                SimpleNamespace(session_id="session-secret", workspace_root=tmp_path),
                arguments,
            )
        )
        await pilot.pause()
        rendered = str(app.query_one("#permission-details").render())
        assert "curl" in rendered
        assert "https://example.test/api" in rendered
        assert "private-token-value" not in rendered
        assert "private-bearer-value" not in rendered
        assert "private-environment-value" not in rendered
        assert arguments["argv"] == argv
        app.query_one("#permission-deny-once", Button).press()
        await pilot.pause()
        assert str(await decision) == "deny_once"


@pytest.mark.asyncio
async def test_concurrent_shell_permission_details_stay_with_their_request(tmp_path) -> None:
    app = build_app(DemoBackend())
    context = SimpleNamespace(session_id="session-concurrent", workspace_root=tmp_path)
    async with app.run_test() as pilot:
        first = asyncio.create_task(
            app.confirm_permission_tool(
                SimpleNamespace(name="shell_command"),
                context,
                {"argv": ["echo", "first-command"], "__permission_effect": "subprocess"},
            )
        )
        await pilot.pause()
        second = asyncio.create_task(
            app.confirm_permission_tool(
                SimpleNamespace(name="shell_command"),
                context,
                {"argv": ["echo", "second-command"], "__permission_effect": "subprocess"},
            )
        )
        await pilot.pause()
        first_details = str(app.query_one("#permission-details").render())
        assert "first-command" in first_details
        assert "second-command" not in first_details
        app.query_one("#permission-allow-once", Button).press()
        await pilot.pause()
        assert str(await first) == "allow_once"
        second_details = str(app.query_one("#permission-details").render())
        assert "second-command" in second_details
        assert "first-command" not in second_details
        app.query_one("#permission-deny-once", Button).press()
        await pilot.pause()
        assert str(await second) == "deny_once"


@pytest.mark.asyncio
async def test_permissions_drawer_and_palette_show_rules_and_history() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/permissions"
        await app.action_send()
        await pilot.pause()
        content = str(app.screen.query_one("#overview-content").render())
        assert "Active mode: auto" in content
        assert "mcp__demo__write_file · workspace_write · allow_session" in content
        assert "target_chat · target_network · deny (prompt) · deny_once" in content
        await pilot.press("escape")
        await pilot.press("ctrl+p")
        assert app.screen.query_one("#choice-mode")
        assert app.screen.query_one("#choice-permissions")


def test_permission_detail_default_cwd_matches_resolved_workspace(tmp_path) -> None:
    rendered = app_module._render_permission_details(  # noqa: SLF001
        "shell_command", str(tmp_path), {"argv": ["pwd"]}
    )
    assert f"Working directory: {tmp_path.resolve()}" in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_size", ((80, 24), (60, 20)))
async def test_permission_long_scope_keeps_all_decisions_visible(terminal_size) -> None:
    app = build_app(DemoBackend())
    async with app.run_test(size=terminal_size) as pilot:
        decision = asyncio.create_task(
            app.confirm_permission_tool(
                SimpleNamespace(name="shell_command"),
                SimpleNamespace(
                    session_id="session-small-terminal",
                    workspace_root="/workspace/" + "long-directory/" * 20,
                    target_id="long-target-" * 40,
                ),
                {"argv": ["echo", "visible command"], "__permission_effect": "subprocess"},
            )
        )
        await pilot.pause()
        drawer = app.query_one("#permission-drawer").content_region
        composer = app.query_one("#composer").region
        for button in app.query("#permission-drawer Button"):
            if not button.display:
                continue
            assert button.region.height > 0
            assert button.region.y >= drawer.y
            assert button.region.bottom <= drawer.bottom
            assert button.region.bottom <= composer.y
        assert "visible command" in str(app.query_one("#permission-details").render())
        await pilot.press("pagedown")
        await pilot.pause()
        assert app.query_one("#permission-details-scroll").scroll_y > 0
        app.query_one("#permission-deny-once", Button).press()
        await pilot.pause()
        assert str(await decision) == "deny_once"


def test_permission_detail_rejects_symlink_cwd_outside_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    rendered = app_module._render_permission_details(  # noqa: SLF001
        "shell_command", str(workspace), {"argv": ["pwd"], "cwd": "escape"}
    )
    assert "Working directory: invalid" in rendered
    assert f"Working directory: {outside.resolve()}" not in rendered


def test_permission_detail_escapes_command_and_cwd_terminal_controls(tmp_path) -> None:
    directory = tmp_path / "line\n\x1b[31m"
    directory.mkdir()
    rendered = app_module._render_permission_details(  # noqa: SLF001
        "shell_command",
        str(tmp_path),
        {"argv": ["echo", "line\n\x1b[31m\tend"], "cwd": directory.name},
    )
    assert "\x1b" not in rendered
    assert "line\n" not in rendered
    assert r"line\n\u001b[31m" in rendered
    assert r"\tend" in rendered


@pytest.mark.parametrize(
    "secret_argument",
    (
        "--token=private value with spaces",
        '--token="private value\ncontinued secret"',
        "API_KEY='private value\ncontinued secret'",
    ),
)
def test_permission_detail_redacts_complete_secret_arguments(
    tmp_path, secret_argument: str
) -> None:
    rendered = app_module._render_permission_details(  # noqa: SLF001
        "shell_command", str(tmp_path), {"argv": ["example", secret_argument]}
    )
    assert "private value" not in rendered
    assert "value with spaces" not in rendered
    assert "continued secret" not in rendered
    assert "[redacted]" in rendered


@pytest.mark.asyncio
async def test_promotion_drawer_shows_exact_diff_gates_failure_and_graph_local_warning() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/promotion"
        await app.action_send()
        await pilot.pause()
        content = str(app.screen.query_one("#overview-content").render())
        assert "candidate-demo · profile v2 · evaluated" in content
        assert "sha256:baseline-demo" in content
        assert "sha256:candidate-demo" in content
        assert "- max_turns = 6" in content
        assert "PASS · authority" in content
        assert "FAIL · regression" in content
        assert "Evaluation failure: regression gate failed" in content
        assert "GRAPH-LOCAL — NOT PERMANENTLY PROMOTABLE" in content
        assert "rollback · candidate-old" in content


@pytest.mark.asyncio
async def test_promotion_operator_actions_are_in_palette() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        for identifier in (
            "promotion",
            "promotion-evaluate",
            "promotion-accept",
            "promotion-reject",
            "promotion-activate",
            "promotion-history",
            "promotion-rollback",
        ):
            assert app.screen.query_one(f"#choice-{identifier}")


@pytest.mark.asyncio
async def test_memory_drawer_shows_review_scope_conflicts_expiry_and_turn_use() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/memory list accepted"
        await app.action_send()
        await pilot.pause()
        content = str(app.screen.query_one("#overview-content").render())
        assert "lesson/auth-1 · reviewed_skill · accepted" in content
        assert "Provenance: merlin://repository/acme" in content
        assert "Reviewed by: operator/alice" in content
        assert "Expires: 2026-12-31T00:00:00Z" in content
        assert "Applicability: target=demo-target" in content
        assert "Conflicts: lesson/auth-old" in content
        assert "Agent turns: session-1:attempt-2" in content


@pytest.mark.asyncio
async def test_memory_actions_are_available_in_palette() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        for action in ("list", "import", "export", "accept", "reject", "delete"):
            assert app.screen.query_one(f"#choice-memory-{action}")


@pytest.mark.asyncio
async def test_campaign_drawer_can_stop_all_and_lessons_defaults_to_listing() -> None:
    class CampaignBackend(DemoBackend):
        def __init__(self) -> None:
            self.cancelled = []
            self.cancelled_tasks = []

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/one",
                    "session-demo",
                    "Assess the selected target",
                    "recommended",
                    "executing",
                    "sha256:manifest",
                ),
            )

        async def cancel_campaign(self, campaign_id="*"):
            self.cancelled.append(campaign_id)
            return 1

        async def cancel_task(self, task_id):
            self.cancelled_tasks.append(task_id)
            return f"worker:{task_id}"

        async def list_lessons(self):
            return (
                MemoryRecordView(
                    "bundle/one",
                    "lesson/one",
                    "sealed_lesson",
                    "accepted",
                    "campaign/one",
                    "operator",
                    "never",
                    "target=one",
                    "sha256:lesson",
                ),
            )

    backend = CampaignBackend()
    app = build_app(backend)
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/campaigns"
        await app.action_send()
        await pilot.pause()
        assert "campaign/one · executing" in str(app.screen.query_one("#overview-content").render())
        app.screen.query_one("#campaign-stop-all", Button).press()
        await pilot.pause()
        assert backend.cancelled == ["*"]

        await pilot.press("escape")
        app.query_one("#composer").text = "/agents kill all"
        await app.action_send()
        await pilot.pause()
        assert backend.cancelled == ["*", "*"]

        app.query_one("#composer").text = "/cancel all"
        await app.action_send()
        await pilot.pause()
        app.query_one("#composer").text = "/cancel campaign campaign/one"
        await app.action_send()
        await pilot.pause()
        app.query_one("#composer").text = "/cancel task task/one"
        await app.action_send()
        await pilot.pause()
        assert backend.cancelled == ["*", "*", "*", "campaign/one"]
        assert backend.cancelled_tasks == ["task/one"]

        app.query_one("#composer").text = "/lessons"
        await app.action_send()
        await pilot.pause()
        assert "lesson/one · sealed_lesson · accepted" in str(
            app.screen.query_one("#overview-content").render()
        )


@pytest.mark.asyncio
async def test_assessment_roles_render_distinctly() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "demo assessment"
        await app.action_send()
        await pilot.pause()
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "Attacker ›" in transcript
        assert "Target ‹" in transcript


@pytest.mark.asyncio
async def test_browser_drawer_renders_isolation_ownership_evidence_and_tabs() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/browser"
        await app.action_send()
        await pilot.pause()
        content = str(app.screen.query_one("#overview-content").render())
        assert "context-1 · fresh · active" in content
        assert "Owner: worker-injection · Technique: prompt-injection" in content
        assert "Latest action: clicked submit" in content
        assert "Latest observation: target returned validation error" in content
        assert "Blockers: authentication required" in content
        assert "evidence://browser/context-1/action-4" in content
        assert "tab-1 · open · https://target.example/login" in content
        assert "context-2 · attached · paused" in content
        assert "Cleanup: complete" in content


@pytest.mark.asyncio
async def test_browser_operator_takeover_and_palette_commands() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/browser"
        await app.action_send()
        await pilot.pause()
        app.screen.query_one("#browser-takeover-0", Button).press()
        await pilot.pause()
        content = str(app.screen.query_one("#overview-content").render())
        assert "Owner: operator" in content
        assert "operator takeover" in content
        await pilot.press("escape")
        await pilot.press("ctrl+p")
        assert app.screen.query_one("#choice-browser")
        assert app.screen.query_one("#choice-browser-takeover")
        assert app.screen.query_one("#choice-browser-cleanup")
        assert app.screen.query_one("#choice-browser-resume")


@pytest.mark.asyncio
async def test_readiness_drawers_config_and_resume_selection() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/doctor"
        await app.action_send()
        await pilot.pause()
        content = str(app.screen.query_one("#overview-content").render())
        assert "postgresql · ok" in content
        assert "connection ready" in content

        await pilot.press("escape")
        app.query_one("#composer").text = "/logs"
        await app.action_send()
        await pilot.pause()
        assert "Merlin started" in str(app.screen.query_one("#overview-content").render())

        await pilot.press("escape")
        app.query_one("#composer").text = "/history"
        await app.action_send()
        await pilot.pause()
        assert "recovered=2" in str(app.screen.query_one("#overview-content").render())

        await pilot.press("escape")
        app.query_one("#composer").text = "/doctor config"
        await app.action_send()
        await pilot.pause()
        config = str(app.screen.query_one("#overview-content").render())
        assert "base_url" in config
        assert "api_key" not in config

        await pilot.press("escape")
        app.query_one("#composer").text = "/resume"
        await app.action_send()
        await pilot.pause()
        assert app.screen.query_one("#resume-session-0")
        app.screen.query_one("#resume-session-0", Button).press()
        await pilot.pause()
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert "Resuming durable session session-demo" in transcript


@pytest.mark.asyncio
async def test_readiness_commands_are_exposed_in_palette() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        assert app.screen.query_one("#choice-doctor")
        assert app.screen.query_one("#choice-doctor-config")
        assert app.screen.query_one("#choice-logs")
        assert app.screen.query_one("#choice-history")


def test_campaign_status_renders_dynamic_specialists_with_retired_state() -> None:
    rendered = app_module._render_campaign_status(  # noqa: SLF001
        {
            "status": "executing",
            "stop_reason": None,
            "manifest_digest": "sha256:reviewed",
            "dynamic_specialists": (
                {
                    "id": "dynamic-api-fuzzer",
                    "state": "active",
                    "admitted_wave": 2,
                    "ttl_waves": 3,
                    "retired_wave": None,
                    "retirement_reason": None,
                    "proposal_digest": "sha256:active-proposal-digest",
                },
                {
                    "id": "dynamic-ssrf-hunter",
                    "state": "retired",
                    "admitted_wave": 1,
                    "ttl_waves": 2,
                    "retired_wave": 3,
                    "retirement_reason": "completed",
                    "proposal_digest": "sha256:retired-proposal-digest",
                },
            ),
            "waves": (),
        }
    )

    assert "Dynamic specialists" in rendered
    assert "Active: 1 · Retired: 1" in rendered
    assert "dynamic-api-fuzzer · active · admitted wave 2 · ttl 3 wave(s)" in rendered
    assert "dynamic-ssrf-hunter · retired wave 3 · reason: completed" in rendered
    assert "sha256:active-" in rendered
    assert "sha256:active-proposal-digest" not in rendered
    assert '"dynamic_specialists"' not in rendered
    assert "Specialist composition" not in rendered


def test_campaign_status_renders_specialist_composition_and_creation_reason() -> None:
    rendered = app_module._render_campaign_status(  # noqa: SLF001
        {
            "status": "executing",
            "stop_reason": None,
            "manifest_digest": "sha256:reviewed",
            "specialist_composition": "opinionated",
            "dynamic_specialists": (
                {
                    "id": "dynamic-injection-prober",
                    "state": "active",
                    "admitted_wave": 0,
                    "ttl_waves": 3,
                    "retired_wave": None,
                    "retirement_reason": None,
                    "proposal_digest": "sha256:template-proposal-digest",
                    "creation_reason": {
                        "template_id": "injection",
                        "template_version": 3,
                    },
                },
                {
                    "id": "dynamic-ssrf-hunter",
                    "state": "active",
                    "admitted_wave": 1,
                    "ttl_waves": 2,
                    "retired_wave": None,
                    "retirement_reason": None,
                    "proposal_digest": "sha256:lead-proposal-digest",
                    "creation_reason": {
                        "evidence_refs": ("evidence/recon-1", "evidence/recon-2"),
                    },
                },
            ),
            "waves": (),
        }
    )

    assert "Specialist composition: opinionated" in rendered
    assert "dynamic-injection-prober" in rendered
    assert "template injection v3" in rendered
    assert "lead evidence: evidence/recon-1, evidence/recon-2" in rendered


def test_dynamic_creation_reason_tolerates_legacy_records() -> None:
    helper = app_module._dynamic_creation_reason  # noqa: SLF001

    assert helper({"id": "dynamic-legacy"}) == ""
    assert helper({"creation_reason": "lead proposal"}) == " · lead proposal"
    assert helper({"template_id": "auth-session"}) == " · template auth-session"
    assert helper({"evidence_refs": ["evidence/recon-9"]}) == " · lead evidence: evidence/recon-9"


def test_render_agents_marks_dynamic_specialists() -> None:
    from merlin_tui.backend import AgentView

    fixed = AgentView(
        name="recon-1",
        owner="lead",
        task="map surface",
        provider="demo",
        elapsed="1m",
        latest_event="running",
        profile="recon",
    )
    dynamic = AgentView(
        name="dynamic-ssrf-hunter-1",
        owner="lead",
        task="probe ssrf",
        provider="demo",
        elapsed="2m",
        latest_event="running",
        profile="dynamic-ssrf-hunter",
    )

    fixed_key = app_module._specialist_instance_key(fixed)  # noqa: SLF001
    dynamic_key = app_module._specialist_instance_key(dynamic)  # noqa: SLF001
    rendered = app_module._render_agents(  # noqa: SLF001
        (fixed, dynamic), cursor_group_key=fixed_key
    ).plain
    assert "› ● Recon" in rendered
    assert "Dynamic ssrf hunter" in rendered
    assert "↑/↓ move · Enter inspect · Esc close" in rendered
    assert rendered.count("Dynamic ssrf hunter") == 1
    assert "specialist" not in rendered.lower()

    detail = app_module._render_agents(  # noqa: SLF001
        (fixed, dynamic), selected_group_key=dynamic_key
    ).plain
    assert "temporary group" in detail
    fixed_detail = app_module._render_agents(  # noqa: SLF001
        (fixed, dynamic), selected_group_key=fixed_key
    ).plain
    assert "temporary group" not in fixed_detail


def test_render_agents_omits_prior_campaign_history_from_live_swarm() -> None:
    from merlin_tui.backend import AgentView

    queued = AgentView(
        name="worker-current",
        owner="lead",
        task="current task",
        provider="demo",
        elapsed="1m",
        latest_event="queued",
        profile="dynamic-current",
    )
    campaigns = (
        CampaignView(
            "campaign/current", "session-demo", "current", "recommended", "executing", "sha256:a"
        ),
        CampaignView("campaign/old", "session-demo", "old", "recommended", "failed", "sha256:b"),
        CampaignView(
            "campaign/cancelled",
            "session-demo",
            "old",
            "recommended",
            "cancelled",
            "sha256:c",
        ),
    )

    rendered = app_module._render_agents((queued,), campaigns).plain  # noqa: SLF001

    assert "Agents" in rendered
    assert "● 0 working" in rendered
    assert "○ 1 queued" in rendered
    assert "needs review" not in rendered
    assert "campaign/current" in rendered
    assert "campaign/old" not in rendered
    assert "campaign/cancelled" not in rendered
    assert "Prior campaigns hidden" not in rendered


def test_render_agents_groups_assignment_tasks_under_one_specialist_instance() -> None:
    agents = tuple(
        AgentView(
            name=(f"worker-campaign/campaign/live/adversarial-explorer/agenda-abc123/{action}"),
            owner="lead",
            task=f"{action} task",
            provider="demo",
            elapsed="1m",
            latest_event="queued",
            profile="adversarial-explorer",
            technique="merlin-injection-taxonomy/cat/direct/override-baseline",
        )
        for action in ("control", "direct", "replay")
    )

    rendered = app_module._render_agents(agents).plain  # noqa: SLF001

    assert "○ 3 queued" in rendered
    assert "· 3 tasks" in rendered
    assert rendered.count("Override baseline") == 1
    assert "    control · queued" in rendered
    assert "    direct · queued" in rendered
    assert "    replay · queued" in rendered


def test_render_agents_keeps_non_success_terminal_states_distinct() -> None:
    states = (
        "completed",
        "failed",
        "cancelled",
        "quarantined",
        "interrupted",
        "evaluation_uncertain",
    )
    agents = tuple(
        AgentView(
            name=f"specialist-{state}",
            owner="lead",
            task=f"{state} task",
            provider="demo",
            elapsed="1m",
            latest_event=state,
            profile=state,
        )
        for state in states
    )

    rendered = app_module._render_agents(agents).plain  # noqa: SLF001
    summary = rendered.splitlines()[1]

    assert "✓ 1 done" in rendered
    assert "✕ 1 failed" in rendered
    assert "✕ 1 cancelled" in rendered
    assert "⚠ 1 quarantined" in rendered
    assert "◌ 1 interrupted" in rendered
    assert "⚠ 1 needs review" in rendered
    assert summary.count("needs review") == 1
    assert "6 done" not in rendered


def test_render_agents_inspects_the_whole_specialist_group() -> None:
    agents = tuple(
        AgentView(
            name=(f"worker-campaign/campaign/live/adversarial-explorer/agenda-stable/{stage}"),
            owner="lead",
            task=f"{stage} task",
            provider="demo",
            elapsed="1m",
            latest_event="running" if stage == "direct" else "completed",
            profile="adversarial-explorer",
        )
        for stage in ("control", "direct", "replay")
    )
    group_key = app_module._specialist_instance_key(agents[0])  # noqa: SLF001

    rendered = app_module._render_agents(  # noqa: SLF001
        agents, selected_group_key=group_key
    ).plain

    assert "3 tasks" in rendered
    assert rendered.count("✓ completed") == 2
    assert "● working" in rendered
    assert "Working on\n  control task" in rendered
    assert "Working on\n  direct task" in rendered
    assert "Working on\n  replay task" in rendered


def test_campaigns_for_session_excludes_other_conversations() -> None:
    campaigns = (
        CampaignView("campaign/a", "session-a", "A", "recommended", "executing", "a"),
        CampaignView("campaign/b", "session-b", "B", "recommended", "executing", "b"),
    )

    visible = app_module._campaigns_for_session(campaigns, "session-a")  # noqa: SLF001

    assert tuple(campaign.id for campaign in visible) == ("campaign/a",)


def test_agents_for_campaigns_hides_global_agents_without_a_visible_campaign() -> None:
    agent = AgentView(
        name="worker-campaign/campaign/other/specialist/agenda-other/direct",
        owner="lead",
        task="other session task",
        provider="demo",
        elapsed="1m",
        latest_event="running",
    )

    assert app_module._agents_for_campaigns((agent,), ()) == ()  # noqa: SLF001


def test_agents_for_campaigns_hides_all_unscoped_rows_without_campaign_identity() -> None:
    agents = tuple(
        AgentView(
            name=f"legacy-worker-{index}",
            owner="lead",
            task="other session task",
            provider="demo",
            elapsed="1m",
            latest_event="running",
        )
        for index in range(3)
    )

    assert app_module._agents_for_campaigns(agents, ()) == ()  # noqa: SLF001


def test_agents_for_campaigns_uses_exact_campaign_path_segments() -> None:
    def agent(name: str) -> AgentView:
        return AgentView(name, "lead", "task", "demo", "1m", "running")

    campaign = CampaignView(
        "campaign/one", "session-demo", "current", "recommended", "executing", "digest"
    )
    exact = agent("worker-campaign/campaign/one/specialist/agenda-1/direct")
    prefix_collision = agent("worker-campaign/campaign/one-more/specialist/agenda-2/direct")

    assert app_module._agents_for_campaigns(  # noqa: SLF001
        (exact, prefix_collision), (campaign,)
    ) == (exact,)


def test_agents_for_campaigns_does_not_attach_unscoped_legacy_rows_to_campaign() -> None:
    campaign = CampaignView(
        "campaign/one", "session-demo", "current", "recommended", "executing", "digest"
    )
    unscoped = AgentView(
        "legacy-worker", "lead", "task", "demo", "1m", "running", profile="explorer"
    )

    assert app_module._agents_for_campaigns((unscoped,), (campaign,)) == ()  # noqa: SLF001


@pytest.mark.parametrize("winner_state", ["running", "completed"])
def test_agents_for_campaigns_deduplicates_logical_assignment_across_waves(
    winner_state: str,
) -> None:
    campaign = CampaignView(
        "campaign/one", "session-demo", "current", "recommended", "executing", "digest"
    )
    failed = AgentView(
        "structural/execute",
        "lead",
        "Open the canonical target",
        "demo",
        "1m",
        "failed",
        campaign_id=campaign.id,
        assignment_id="phase-gate/canonical-open-target",
        target_adapter="browser",
        target_generation="generation/one",
        scenario_fingerprint="sha256:scenario",
        technique="phase-gate/canonical-open-target",
        controller_id="objective-phase-gate/v1",
        payload_family="baseline",
        requested_language="en",
        mutation_id="none",
        failure_kind="configuration",
        schedule_updated_at=datetime(2026, 10, 28, 10, tzinfo=UTC),
        campaign_wave=0,
    )
    winner = replace(
        failed,
        latest_event=winner_state,
        result="Canonical target opened" if winner_state == "completed" else None,
        budgets="schedule=wave-two",
        failure_kind=None,
        schedule_updated_at=datetime(2026, 10, 28, 11, tzinfo=UTC),
        campaign_wave=1,
    )

    visible = app_module._agents_for_campaigns(  # noqa: SLF001
        (failed, winner), (campaign,)
    )

    assert visible == (winner,)


def test_agents_for_campaigns_uses_authoritative_wave_and_recency_for_failed_duplicates() -> None:
    campaign = CampaignView(
        "campaign/one", "session-demo", "current", "recommended", "executing", "digest"
    )
    older_typed = AgentView(
        "specialist/execute",
        "lead",
        "Try candidate",
        "demo",
        "1m",
        "failed",
        result="Provider connection closed",
        campaign_id=campaign.id,
        assignment_id="assignment/one",
        target_adapter="browser",
        target_generation="generation/one",
        scenario_fingerprint="sha256:scenario",
        technique="technique/first",
        controller_id="pair",
        payload_family="baseline",
        requested_language="en",
        mutation_id="none",
        failure_kind="provider_transient",
        schedule_updated_at=datetime(2026, 10, 28, 12, tzinfo=UTC),
        campaign_wave=0,
    )
    newer_untyped = replace(
        older_typed,
        result="Target rejected the attack",
        failure_kind=None,
        schedule_updated_at=datetime(2026, 10, 28, 11, tzinfo=UTC),
        campaign_wave=1,
    )

    visible = app_module._agents_for_campaigns(  # noqa: SLF001
        (older_typed, newer_untyped), (campaign,)
    )

    assert visible == (newer_untyped,)
    assert app_module._agent_outcome_state(visible[0]) == "failed"  # noqa: SLF001

    older_without_wave = replace(
        older_typed,
        schedule_updated_at=datetime(2026, 10, 28, 10, tzinfo=UTC),
        campaign_wave=None,
    )
    newer_without_wave = replace(newer_untyped, campaign_wave=None)
    visible_without_wave = app_module._agents_for_campaigns(  # noqa: SLF001
        (older_without_wave, newer_without_wave), (campaign,)
    )
    assert visible_without_wave == (newer_without_wave,)


def test_agents_for_campaigns_preserves_distinct_candidates_for_one_assignment() -> None:
    campaign = CampaignView(
        "campaign/one", "session-demo", "current", "recommended", "executing", "digest"
    )
    first = AgentView(
        "specialist/execute",
        "lead",
        "Try candidate",
        "demo",
        "1m",
        "completed",
        campaign_id=campaign.id,
        assignment_id="assignment/one",
        target_adapter="browser",
        target_generation="generation/one",
        scenario_fingerprint="sha256:scenario",
        technique="technique/first",
        controller_id="pair",
        payload_family="baseline",
        requested_language="en",
        mutation_id="none",
    )
    second = replace(first, technique="technique/second")

    visible = app_module._agents_for_campaigns((first, second), (campaign,))  # noqa: SLF001

    assert visible == (first, second)


def test_current_campaigns_selects_only_one_active_campaign() -> None:
    campaigns = (
        CampaignView("campaign/one", "session", "one", "recommended", "executing", "a"),
        CampaignView("campaign/two", "session", "two", "recommended", "paused", "b"),
    )

    assert app_module._current_campaigns(campaigns) == campaigns[:1]  # noqa: SLF001


def test_live_swarm_summary_is_bounded_and_uses_real_states() -> None:
    from merlin_tui.backend import AgentView

    agents = tuple(
        AgentView(
            name=f"specialist-{index}",
            owner="lead",
            task=f"task {index}",
            provider="demo",
            elapsed=f"{index}m",
            latest_event=("running" if index < 2 else "queued" if index < 5 else "completed"),
            activity=(f"observed event {index}",),
        )
        for index in range(12)
    )
    campaigns = (
        CampaignView(
            "campaign/live",
            "session-demo",
            "Assess policy boundary",
            "recommended",
            "executing",
            "x",
        ),
    )

    rendered = app_module._render_live_swarm(agents, campaigns, 0).plain  # noqa: SLF001

    assert "Agent swarm — Assess policy boundary" in rendered
    assert (
        "12 task groups · tasks 7/12 finished · 2 working · 3 queued · 0 needs review"
    ) in rendered
    assert "Campaign Lead · coordinating task groups" in rendered
    assert "Latest: observed event 0" in rendered
    assert "Next up · 3 queued — task 2, task 3 +1" in rendered
    assert "✓ 7 completed" in rendered


def test_live_swarm_keeps_evaluation_uncertainty_out_of_lifecycle_progress() -> None:
    agents = (
        AgentView("done", "lead", "done", "demo", "1m", "completed"),
        AgentView(
            "uncertain",
            "lead",
            "uncertain",
            "demo",
            "1m",
            "evaluation_uncertain",
        ),
    )

    rendered = app_module._render_live_swarm(agents, (), 0).plain  # noqa: SLF001

    assert "tasks 1/2 finished" in rendered
    assert "0 queued · 1 needs review" in rendered
    assert "└─ ✓ 1 completed" in rendered
    assert "! 1 needs review" not in rendered
    assert rendered.count("└─") == 1


def test_live_swarm_separates_operational_failures_from_target_failures() -> None:
    common = {
        "owner": "lead",
        "provider": "demo",
        "elapsed": "1m",
        "latest_event": "failed",
    }
    agents = (
        AgentView(
            name="prerequisite",
            task="Open target",
            controller_id="objective-phase-gate/v1",
            result="Target could not be opened",
            **common,
        ),
        AgentView(
            name="unavailable",
            task="Run GOAT",
            infrastructure_failure_category="adaptive_strategy_unavailable",
            result="GOAT contract is unavailable",
            **common,
        ),
        AgentView(
            name="infrastructure",
            task="Call provider",
            failure_kind="provider_transient",
            result="Provider connection closed",
            **common,
        ),
        AgentView(
            name="target-failure",
            task="Execute target test",
            result="Target rejected the attack",
            **common,
        ),
    )

    rendered = app_module._render_live_swarm(agents, (), 0).plain  # noqa: SLF001

    assert "✕ 1 failed" in rendered
    assert "! 1 prerequisite blocked" in rendered
    assert "! 1 unavailable" in rendered
    assert "! 1 infrastructure" in rendered
    assert app_module._agent_state_chip("failed") == ("✕", "red")  # noqa: SLF001
    assert app_module._agent_state_chip("unavailable") == ("!", "yellow")  # noqa: SLF001

    agents_rendered = app_module._render_agents(agents).plain  # noqa: SLF001
    assert "✕ 1 failed" in agents_rendered
    assert "! 1 prerequisite blocked" in agents_rendered
    assert "! 1 unavailable" in agents_rendered
    assert "! 1 infrastructure" in agents_rendered


@pytest.mark.parametrize(
    ("failure_kind", "label"),
    [
        ("authorization", "authorization blocked"),
        ("scope", "scope blocked"),
        ("budget", "budget exhausted"),
        ("configuration", "configuration issue"),
        ("state_integrity", "state integrity blocked"),
        ("unknown", "operational failure"),
    ],
)
def test_typed_non_infrastructure_failures_keep_accurate_operational_labels(
    failure_kind: str, label: str
) -> None:
    agent = AgentView(
        "worker",
        "lead",
        "Run task",
        "demo",
        "1m",
        "failed",
        failure_kind=failure_kind,
    )

    assert app_module._agent_outcome_state(agent) == label  # noqa: SLF001
    assert app_module._agent_state_chip(label) == ("!", "yellow")  # noqa: SLF001


def test_live_swarm_labels_paused_campaign_without_claiming_working() -> None:
    campaigns = (
        CampaignView(
            "campaign/paused",
            "session-demo",
            "Paused policy assessment",
            "recommended",
            "paused",
            "x",
        ),
    )

    rendered = app_module._render_live_swarm((), campaigns, 0).plain  # noqa: SLF001

    assert "◎ Paused" in rendered
    assert "░" * 18 in rendered
    assert "Working" not in rendered
    assert "Campaign Lead · paused" in rendered


def test_live_swarm_labels_paused_campaign_with_running_worker_as_pausing() -> None:
    agents = (
        AgentView(
            "worker",
            "lead",
            "active target interaction",
            "demo",
            "1m",
            "running",
        ),
    )
    campaigns = (
        CampaignView(
            "campaign/pausing",
            "session-demo",
            "Stateful policy assessment",
            "recommended",
            "paused",
            "x",
        ),
    )

    rendered = app_module._render_live_swarm(agents, campaigns, 0).plain  # noqa: SLF001

    assert "Pausing · finishing active work" in rendered
    assert "◎ Paused" not in rendered
    assert "1 working" in rendered
    assert "Campaign Lead · pause requested · waiting for active work" in rendered


@pytest.mark.asyncio
async def test_agents_drawer_navigates_beyond_nine_workers_with_arrows() -> None:
    from merlin_tui.backend import AgentView

    class ManyAgentsBackend(DemoBackend):
        async def list_agents(self):
            return tuple(
                AgentView(
                    name=(f"worker-campaign/campaign/live/explorer/agenda-{index:02d}/direct"),
                    owner="lead",
                    task=f"task {index}",
                    provider="demo",
                    elapsed=f"{index}m",
                    latest_event="running",
                    profile=f"specialist-{index:02d}",
                )
                for index in range(12)
            )

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/live",
                    "session-demo",
                    "Many task groups",
                    "recommended",
                    "executing",
                    "digest",
                ),
            )

    app = build_app(ManyAgentsBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/agents"
        await app.action_send()
        await pilot.pause()
        for _ in range(10):
            await pilot.press("down")
        await pilot.pause()
        assert app.screen.query_one("#agent-picker", app_module.OptionList).scroll_offset.y > 0
        await pilot.press("enter")
        await pilot.pause()
        content = app.screen.query_one("#overview-content", app_module.AgentOutputText).plain
        assert "Full assignment\ntask 10" in content
        assert app.screen._expanded  # noqa: SLF001


@pytest.mark.asyncio
async def test_agents_drawer_navigates_specialist_groups_not_assignment_tasks() -> None:
    class GroupedAgentsBackend(DemoBackend):
        async def list_agents(self):
            return tuple(
                AgentView(
                    name=(
                        "worker-campaign/campaign/live/adversarial-explorer/"
                        f"agenda-{group:02d}/{stage}"
                    ),
                    owner="lead",
                    task=f"group {group} {stage}",
                    provider="demo",
                    elapsed="1m",
                    latest_event="running",
                    profile="adversarial-explorer",
                )
                for group in range(4)
                for stage in ("control", "direct", "replay")
            )

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/live",
                    "session-demo",
                    "Grouped campaign",
                    "recommended",
                    "executing",
                    "digest",
                ),
            )

    app = build_app(GroupedAgentsBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/agents"
        await app.action_send()
        await pilot.pause()
        await pilot.press("down", "down", "enter")
        await pilot.pause()
        content = app.screen.query_one("#overview-content", app_module.AgentOutputText).plain

        assert "Full assignment\ngroup 0 replay" in content
        assert "Full assignment\ngroup 0 direct" not in content
        await pilot.press("down")
        await pilot.pause()
        content = app.screen.query_one("#overview-content", app_module.AgentOutputText).plain
        assert "Full assignment\ngroup 1 control" in content
        assert "Full assignment\ngroup 0 replay" not in content
        await pilot.press("down")
        await pilot.pause()
        content = app.screen.query_one("#overview-content", app_module.AgentOutputText).plain
        assert "Full assignment\ngroup 1 direct" in content
        assert "Full assignment\ngroup 1 control" not in content


@pytest.mark.asyncio
async def test_agents_drawer_preserves_group_cursor_by_stable_legacy_key_on_refresh() -> None:
    class RefreshingAgentsBackend(DemoBackend):
        reverse = False

        async def list_agents(self):
            agents = tuple(
                AgentView(
                    name=(
                        "worker-campaign/campaign/live/adversarial-explorer/"
                        f"agenda-{group:02d}/direct"
                    ),
                    owner="lead",
                    task=f"group {group}",
                    provider="demo",
                    elapsed="1m",
                    latest_event="running" if group else "queued",
                    profile="adversarial-explorer",
                )
                for group in range(3)
            )
            return tuple(reversed(agents)) if self.reverse else agents

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/live",
                    "session-demo",
                    "Refreshing campaign",
                    "recommended",
                    "executing",
                    "digest",
                ),
            )

    backend = RefreshingAgentsBackend()
    app = build_app(backend)
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/agents"
        await app.action_send()
        await pilot.pause()
        await pilot.press("down")
        screen = app.screen
        cursor_before = screen._selected_agent_key  # noqa: SLF001
        backend.reverse = True

        await screen.refresh_progress()

        assert screen._selected_agent_key == cursor_before  # noqa: SLF001
        assert screen.query_one("#agent-picker", app_module.OptionList).highlighted == 1
        assert screen._selected_agent_key in screen._pane_keys  # noqa: SLF001


@pytest.mark.asyncio
async def test_agents_drawer_preserves_detail_scroll_across_live_refresh() -> None:
    class LongGroupBackend(DemoBackend):
        async def list_agents(self):
            return tuple(
                AgentView(
                    name=(
                        "worker-campaign/campaign/live/adversarial-explorer/"
                        f"agenda-long/task-{index:02d}"
                    ),
                    owner="lead",
                    task=(f"task {index} " + "with enough detail to wrap in the drawer " * 3),
                    provider="demo",
                    elapsed="1m",
                    latest_event="running",
                    profile="adversarial-explorer",
                    activity=(f"durable activity {index}",),
                    output=tuple(f"assistant: output line {line}" for line in range(120)),
                )
                for index in range(12)
            )

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/live",
                    "session-demo",
                    "Long detail campaign",
                    "recommended",
                    "executing",
                    "digest",
                ),
            )

    app = build_app(LongGroupBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/agents"
        await app.action_send()
        await pilot.pause()
        await pilot.press("enter")
        view = app.screen.query_one("#overview-content", app_module.AgentOutputText)
        view.focus()
        await pilot.press("pagedown")
        await pilot.pause()
        offset_before = view.scroll_offset
        assert offset_before.y > 0

        await app.screen.refresh_progress()
        await pilot.pause()

        assert view.scroll_offset == offset_before


@pytest.mark.asyncio
async def test_main_body_shows_replaceable_live_swarm_region() -> None:
    from merlin_tui.backend import AgentView

    class LiveSwarmBackend(DemoBackend):
        async def list_agents(self):
            return (
                AgentView(
                    name="worker-campaign/campaign/live/policy/agenda-live/direct",
                    owner="lead",
                    task="test policy boundary",
                    provider="demo",
                    elapsed="2m",
                    latest_event="running",
                    profile="policy",
                    activity=("sent bounded policy probe",),
                ),
            )

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/live",
                    "session-demo",
                    "Assess refund policy",
                    "recommended",
                    "executing",
                    "sha256:live",
                ),
            )

    app = build_app(LiveSwarmBackend())
    async with app.run_test() as pilot:
        await app._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()
        live = app.query_one("#swarm-live", RichLog)
        assert live.has_class("visible")
        content = "".join(line.text for line in live.lines)
        assert "Agent swarm — Assess refund policy" in content
        assert "Campaign Lead · coordinating task groups" in content
        assert "01 ● working · test policy boundary" in content
        assert live.size.height >= 5
        assert "Latest: sent bounded policy probe" in content


@pytest.mark.asyncio
async def test_terminal_campaign_appends_one_notice_that_supersedes_stale_approval() -> None:
    class TerminalCampaignBackend(DemoBackend):
        def __init__(self) -> None:
            self.status = "executing"

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/terminal-notice",
                    "session-demo",
                    "Assess the target",
                    "quickscan",
                    self.status,
                    "sha256:terminal-notice",
                    stop_reason=(
                        "infrastructure_blocked_after_retry_policy"
                        if self.status == "failed"
                        else ""
                    ),
                ),
            )

        async def list_reports(self):
            return ()

    backend = TerminalCampaignBackend()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        app._activate_conversation()  # noqa: SLF001
        transcript = app.query_one("#transcript", RichLog)
        transcript.write("The manifest is prepared. Approve execution?")
        await app._poll_swarm_progress()  # noqa: SLF001

        backend.status = "failed"
        await app._poll_swarm_progress()  # noqa: SLF001
        await app._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()

        content = "\n".join(line.text for line in transcript.lines)
        assert content.count("Campaign failed after partial execution") == 1
        assert "Infrastructure retries exhausted" in content
        assert "The earlier approval request is no longer active" in content
        assert "final report is unavailable after failure" in content


@pytest.mark.asyncio
async def test_swarm_poll_reconciles_late_new_session_before_filtering_campaigns() -> None:
    class LateSessionBackend(DemoBackend):
        async def conversation_context(self):
            return {"session_id": "session-created-after-timeout"}

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/late-session",
                    "session-created-after-timeout",
                    "Assess the active target",
                    "quickscan",
                    "executing",
                    "sha256:late-session",
                ),
            )

    app = build_app(LateSessionBackend())
    async with app.run_test() as pilot:
        app._active_session_id = "stale-session"  # noqa: SLF001

        await app._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()

        assert app._active_session_id == "session-created-after-timeout"  # noqa: SLF001
        live = app.query_one("#swarm-live", RichLog)
        assert live.has_class("visible")
        assert "Assess the active target" in "".join(line.text for line in live.lines)


@pytest.mark.asyncio
async def test_main_body_hides_swarm_until_campaign_execution_starts() -> None:
    class PreparedCampaignBackend(DemoBackend):
        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/prepared",
                    "session-demo",
                    "Assess refund policy",
                    "recommended",
                    "prepared",
                    "sha256:prepared",
                ),
            )

    app = build_app(PreparedCampaignBackend())
    async with app.run_test() as pilot:
        await app._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()
        live = app.query_one("#swarm-live", RichLog)
        assert not live.has_class("visible")
        assert not live.animating
        assert not live.lines


def test_campaign_choice_defaults_have_a_distinct_visible_style() -> None:
    profile = app_module._render_structured_choice_content(  # noqa: SLF001
        "Choose the campaign profile\n1. Recommended [default]\n2. Exhaustive"
    )
    decision = app_module._render_operator_choices(  # noqa: SLF001
        (("Stop and generate report [default]", "stop"), ("Continue", "continue"))
    )

    for rendered in (profile, decision):
        default_start = rendered.plain.index("[default]")
        assert any(
            span.start <= default_start < span.end and str(span.style) == "bold yellow"
            for span in rendered.spans
        )


@pytest.mark.asyncio
async def test_swarm_poller_tolerates_live_region_teardown() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        await app.query_one("#swarm-live").remove()
        await app._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()


@pytest.mark.asyncio
async def test_foreground_refusal_has_persistent_terminal_error() -> None:
    class RefusedBackend(DemoBackend):
        async def acquire_foreground_lease(self) -> None:
            raise RuntimeError("Merlin already has a foreground owner")

    app = MerlinApp(RefusedBackend())
    app.notify = lambda *_args, **_kwargs: None
    exits = []
    app.exit = lambda **kwargs: exits.append(kwargs)
    assert await app._acquire_foreground_lease() is False
    assert exits[0]["return_code"] == 1
    assert "already has a foreground owner" in str(exits[0]["message"])
    assert "/quit" in str(exits[0]["message"])


@pytest.mark.asyncio
async def test_setup_exposes_native_transport_routing(monkeypatch) -> None:
    app = build_app(DemoBackend())
    commands = []

    async def route(command):
        commands.append(command)

    monkeypatch.setattr(app, "_run_command", route)
    async with app.run_test() as pilot:
        app._begin_provider_setup()
        await pilot.pause()
        assert "Codex App Server" in str(app.screen.choices)
        assert "Claude CLI" in str(app.screen.choices)
        app._pick_provider("Codex App Server — configure model routing")
        app._pick_provider("Claude CLI — configure Campaign Lead routing")
        await pilot.pause()
        assert commands == ["/model", "/model"]


@pytest.mark.asyncio
@pytest.mark.parametrize("replace", [False, True])
async def test_existing_session_dialog_quits_or_starts_here(replace) -> None:
    from merlin_tui.app import ExistingSessionScreen

    class OccupiedBackend(DemoBackend):
        def __init__(self):
            super().__init__()
            self.occupied = True
            self.replacements = 0
            self.acquired = 0

        async def acquire_foreground_lease(self, *, close_notifications=False):
            if self.occupied:
                raise RuntimeError("Merlin already has a foreground owner")
            assert close_notifications
            self.acquired += 1

        async def replace_foreground(self):
            self.replacements += 1
            self.occupied = False

        async def wait_for_foreground_close(self):
            await asyncio.Event().wait()

    backend = OccupiedBackend()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ExistingSessionScreen)
        assert app.focused.id == "existing-session-quit"
        identifier = "existing-session-replace" if replace else "existing-session-quit"
        app.screen.query_one(f"#{identifier}", Button).press()
        await pilot.pause()
        assert backend.replacements == int(replace)
        assert backend.acquired == int(replace)
        assert app._exit is (not replace)


@pytest.mark.asyncio
async def test_existing_session_replacement_failure_exits_without_reopening_dialog() -> None:
    class OccupiedBackend(DemoBackend):
        async def acquire_foreground_lease(self, **kwargs):
            raise RuntimeError("Merlin already has a foreground owner")

        async def replace_foreground(self):
            raise RuntimeError("Existing session is still closing")

        async def wait_for_foreground_close(self):
            await asyncio.Event().wait()

    app = MerlinApp(OccupiedBackend())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#existing-session-replace", Button).press()
        await pilot.pause()
        assert app._exit is True


@pytest.mark.asyncio
async def test_foreground_close_releases_lease_before_app_teardown() -> None:
    close_requested = asyncio.Event()

    class ClosingBackend(DemoBackend):
        def __init__(self):
            super().__init__()
            self.released = False

        async def acquire_foreground_lease(self, *, close_notifications=False):
            assert close_notifications

        async def wait_for_foreground_close(self):
            await close_requested.wait()

        async def release_foreground_lease(self):
            self.released = True

    backend = ClosingBackend()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        await pilot.pause()
        close_requested.set()
        for _ in range(20):
            await pilot.pause()
            if app._exit:
                break
        assert backend.released is True
        assert app._exit is True


def test_agent_detail_shows_work_and_failure_without_internal_identifiers() -> None:
    from merlin_tui.app import _render_agent_group_detail

    view = AgentView(
        name="worker/probe",
        owner="worker:campaign/long-internal-id",
        task="Check whether target memory persists",
        provider="deepseek-v4-flash",
        elapsed="current session",
        latest_event="failed",
        technique="capability/memory",
        result="Target request timed out after 30 seconds",
        activity=("Sent memory probe", "Waiting for target response"),
        authority="campaign=long-internal-id",
        budgets="schedule=long-internal-id",
    )
    rendered = _render_agent_group_detail((view,)).plain
    assert "Check whether target memory persists" in rendered
    assert "Waiting for target response" in rendered
    assert "Failure: Target request timed out after 30 seconds" in rendered
    assert "long-internal-id" not in rendered


@pytest.mark.asyncio
async def test_closing_campaign_drawer_keeps_foreground_session_running() -> None:
    class Backend(DemoBackend):
        def __init__(self):
            super().__init__()
            self.shutdowns = 0

        async def shutdown(self):
            self.shutdowns += 1

    backend = Backend()
    app = build_app(backend)
    async with app.run_test() as pilot:
        await app._run_command("/campaigns")
        await pilot.pause()
        app.screen.query_one("#overview-close", Button).press()
        await pilot.pause()
        assert backend.shutdowns == 0
        assert app._exit is False
        assert app._unmounting is False
    assert backend.shutdowns == 1


@pytest.mark.asyncio
async def test_large_thinking_delta_preserves_text_without_word_by_word_redraws() -> None:
    from merlin_tui.app import LiveTranscript

    reasoning = "Investigating the target response. " * 300

    class Backend(DemoBackend):
        async def stream(self, prompt):
            yield StreamEvent("thinking", reasoning)
            yield StreamEvent("content", "Finished checking the response.")

    app = build_app(Backend())
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", LiveTranscript)
        replace = transcript.replace_live
        redraws = []

        def record(content):
            redraws.append(content.plain)
            replace(content)

        transcript.replace_live = record
        await app._stream("check the response")
        assert len(redraws) <= 4
        assert any(
            entry.kind == "thinking" and entry.text == reasoning
            for entry in app._transcript_entries
        )


@pytest.mark.asyncio
async def test_live_swarm_click_opens_individual_worker_output_and_keeps_selection() -> None:
    class WorkerOutputBackend(DemoBackend):
        updated = False

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/live",
                    "session-demo",
                    "Inspect target capabilities",
                    "recommended",
                    "executing",
                    "digest",
                ),
            )

        async def list_agents(self):
            return tuple(
                AgentView(
                    name=f"worker-{index}",
                    owner="lead",
                    task="Perform exactly one harmless capability-discovery action within scope",
                    provider="demo",
                    elapsed="2m",
                    latest_event="running",
                    technique=f"capability/{surface}",
                    profile="discovery",
                    campaign_id="campaign/live",
                    specialist_instance_id="discovery",
                    assignment_id=f"assignment-{index}",
                    identity_provenance="durable",
                    activity=(f"Testing {surface}",),
                    output=(
                        f"assistant: {surface} output {'updated' if self.updated else 'initial'}",
                    ),
                )
                for index, surface in enumerate(("memory", "tools"))
            )

    backend = WorkerOutputBackend()
    app = build_app(backend)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()
        live = app.query_one("#swarm-live", RichLog)
        assert live.region.height >= 5
        assert "memory output initial" in "".join(line.text for line in live.lines)
        await pilot.click("#swarm-live")
        await pilot.pause()
        assert isinstance(app.screen, app_module.AgentScreen)
        await pilot.press("down", "enter")
        await pilot.pause()
        details = app.screen.query_one("#overview-content", app_module.AgentOutputText)
        assert "tools output initial" in details.plain
        assert "memory output initial" not in details.plain
        backend.updated = True
        await app.screen.refresh_progress()
        await pilot.pause()
        assert "tools output updated" in details.plain
        assert "memory output updated" not in details.plain
        await pilot.press("escape", "escape")
        assert app.screen is app.screen_stack[0]
        assert not app._exit  # noqa: SLF001


@pytest.mark.asyncio
async def test_halfway_quit_resume_restores_swarm_and_presents_completion_report(tmp_path) -> None:
    class ResumableBackend(DemoBackend):
        campaign_status = "executing"
        report_ready = False
        resumed = False

        async def shutdown(self):
            if self.campaign_status == "executing":
                self.campaign_status = "paused"

        async def stream(self, prompt):
            if prompt == "/resume session-demo":
                assert self.campaign_status == "paused"
                self.campaign_status = "executing"
                self.resumed = True
                yield StreamEvent("status", "Resumed 1 paused campaign")
            else:
                async for event in super().stream(prompt):
                    yield event

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/halfway",
                    "session-demo",
                    "Halfway assessment",
                    "recommended",
                    self.campaign_status,
                    "digest",
                ),
            )

        async def list_agents(self):
            return (
                AgentView(
                    "memory-worker",
                    "lead",
                    "Test cross-turn memory",
                    "demo",
                    "2m",
                    "running",
                    campaign_id="campaign/halfway",
                    specialist_instance_id="memory",
                    identity_provenance="durable",
                    output=("assistant: Continuing saved memory test",),
                ),
            )

        async def list_reports(self):
            if not self.report_ready:
                return ()
            return (
                ReportView(
                    "campaign/halfway",
                    "durable-revision",
                    str(tmp_path / "campaign-halfway.html"),
                    "ready · completed · cleanup completed",
                    2,
                ),
            )

    backend = ResumableBackend()
    first = MerlinApp(backend)
    async with first.run_test() as pilot:
        await first._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()
        assert first.query_one("#swarm-live").has_class("visible")
        first.query_one("#composer").text = "/quit"
        await first.action_send()
    assert backend.campaign_status == "paused"

    resumed = MerlinApp(backend, startup_prompt=True)
    async with resumed.run_test() as pilot:
        await pilot.pause()
        await pilot.press("1")
        for _ in range(20):
            await pilot.pause()
            if backend.resumed:
                break
        assert backend.resumed
        await resumed._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()
        live = resumed.query_one("#swarm-live", RichLog)
        assert live.region.height >= 5
        assert "Continuing saved memory test" in "".join(line.text for line in live.lines)
        await resumed._poll_completed_reports()  # noqa: SLF001
        backend.campaign_status = "completed"
        backend.report_ready = True
        await resumed._poll_completed_reports()  # noqa: SLF001
        await resumed._poll_completed_reports()  # noqa: SLF001
        await pilot.pause()
        transcript = "\n".join(line.text for line in resumed.query_one("#transcript").lines)
        assert transcript.count("Campaign report ready") == 1
        assert "campaign-halfway.html" in transcript
        assert "cleanup completed" in transcript
        await resumed._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()
        assert live.has_class("visible")
        summary = "".join(line.text for line in live.lines)
        assert "Scan complete" in summary
        assert "campaign-halfway.html" in summary
        assert resumed.query_one("#scan-actions").has_class("visible")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report_state,title",
    [
        ("complete", "Scan complete"),
        ("incomplete", "Scan failed — partial results retained"),
    ],
)
async def test_resumed_finished_session_restores_inline_summary_and_report_actions(
    tmp_path,
    monkeypatch,
    report_state,
    title,
) -> None:
    report_path = tmp_path / "completed.html"
    report_path.write_text("<!doctype html><title>Saved assessment</title>")
    opened = []
    monkeypatch.setattr("merlin_tui.app.webbrowser.open", lambda url: opened.append(url))

    class FinishedBackend(DemoBackend):
        prompts = []

        async def stream(self, prompt):
            self.prompts.append(prompt)
            async for event in super().stream(prompt):
                yield event

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/other",
                    "other-session",
                    "Unrelated assessment",
                    "recommended",
                    "completed",
                    "digest",
                ),
                CampaignView(
                    "campaign/saved",
                    "session-demo",
                    "Saved target assessment",
                    "recommended",
                    "failed" if report_state == "incomplete" else "completed",
                    "digest",
                ),
            )

        async def list_reports(self):
            return (
                ReportView("campaign/other", "durable-revision", "unrelated.html", "ready", 99),
                ReportView(
                    "campaign/saved",
                    "durable-revision",
                    str(report_path),
                    "ready",
                    2,
                    exchange_count=17,
                    report_state=report_state,
                    stop_reason="failed" if report_state == "incomplete" else "saturated",
                    cleanup_status="completed",
                    coverage=(("planned", 8), ("attempted", 8), ("confirmed", 2)),
                    target_progress={
                        "schema": "merlin.target-progress.v1",
                        "status": "observed",
                        "freshness": "stale",
                        "target_exchange_count": 17,
                        "current_state": (
                            {"name": "tier", "label": "Tier", "value": 2},
                            {
                                "name": "score",
                                "label": "Score",
                                "value": 61,
                                "maximum_value": 100,
                            },
                        ),
                    },
                ),
            )

    backend = FinishedBackend()
    app = MerlinApp(backend, startup_prompt=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("1")
        for _ in range(20):
            await pilot.pause()
            if "/resume session-demo" in backend.prompts:
                break
        await app._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()
        live = app.query_one("#swarm-live", RichLog)
        summary = "".join(line.text for line in live.lines)
        assert live.region.height > 0
        assert title in summary
        assert "Working" not in summary
        assert not live.animating
        if report_state == "incomplete":
            assert "Execution stopped after an error; completed work is retained" in summary
        assert "Findings: 2" in summary
        assert "Target progress" in summary
        assert "Tier 2 · Score 61/100" in summary
        assert "attempted: 8" in summary
        assert "completed.html" in summary
        assert "unrelated" not in summary.casefold()
        await pilot.click("#scan-open-html")
        await pilot.pause()
        assert opened == [report_path.resolve().as_uri()]
        await pilot.click("#scan-export-json")
        for _ in range(20):
            await pilot.pause()
            if "/report --format json" in backend.prompts:
                break
        assert "/report --format json" in backend.prompts
        assert not app._exit  # noqa: SLF001


@pytest.mark.asyncio
async def test_agent_workspace_panes_unread_transcript_and_failure_are_session_scoped() -> None:
    class WorkspaceBackend(DemoBackend):
        updated = False
        fail_refresh = False
        transcript_calls = []

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/workspace",
                    "session-demo",
                    "Workspace assessment",
                    "recommended",
                    "executing",
                    "digest",
                ),
                CampaignView(
                    "campaign/private",
                    "session-other",
                    "Private assessment",
                    "recommended",
                    "executing",
                    "digest",
                ),
            )

        async def list_agents(self):
            if self.fail_refresh:
                raise RuntimeError("connection unavailable")
            workers = tuple(
                AgentView(
                    name=f"worker-{i}",
                    owner="lead",
                    task=f"Inspect surface {i}",
                    provider="demo",
                    elapsed="1m",
                    latest_event="failed" if i == 6 else "running",
                    result="shared target session already owned" if i == 6 else None,
                    assignment_id=f"assignment-{i}",
                    campaign_id="campaign/workspace",
                    identity_provenance="durable",
                    specialist_instance_id=f"specialist-{i}",
                    activity=("heartbeat", "checkpointed: thinking · 1 actions"),
                    output=(
                        f"assistant: worker {i} output "
                        + ("updated" if i == 4 and self.updated else "initial"),
                    ),
                )
                for i in range(7)
            )
            return workers + (
                AgentView(
                    "private-worker",
                    "lead",
                    "PRIVATE TASK",
                    "demo",
                    "1m",
                    "running",
                    campaign_id="campaign/private",
                    assignment_id="private-assignment",
                    identity_provenance="durable",
                    output=("PRIVATE OUTPUT",),
                ),
            )

        async def get_agent_transcript(self, assignment_id):
            self.transcript_calls.append(assignment_id)
            return (
                "assistant: earlier output outside the recent snapshot",
                "tool: retained target response",
            )

    backend = WorkspaceBackend()
    app = build_app(backend)
    async with app.run_test(size=(140, 44)) as pilot:
        await app.push_screen(app_module.AgentScreen(backend, "session-demo"))
        await pilot.pause()
        screen = app.screen
        assert len(screen._agents) == 8  # noqa: SLF001
        assert screen._agents[-1].profile == "Campaign Lead"  # noqa: SLF001
        assert len(screen._pane_keys) == 3  # noqa: SLF001
        assert all("PRIVATE" not in view.plain for view in screen.query(app_module.AgentOutputText))
        assert not backend.transcript_calls
        await pilot.press("tab")
        assert screen._selected_agent_key == "assignment-1"  # noqa: SLF001
        assert screen.query_one("#worker-pane-1").has_class("selected")
        await pilot.press("space")
        assert "assignment-1" in screen._pinned  # noqa: SLF001
        backend.updated = True
        await screen.refresh_progress()
        assert screen._unread["assignment-4"] == 1  # noqa: SLF001
        await pilot.press("down", "down", "down")
        assert screen._selected_agent_key == "assignment-4"  # noqa: SLF001
        assert screen._unread["assignment-4"] == 0  # noqa: SLF001
        assert "assignment-1" in screen._pane_keys  # noqa: SLF001
        await pilot.press("down", "down", "enter")
        await pilot.pause()
        details = screen.query_one("#overview-content", app_module.AgentOutputText)
        assert "Failure: shared target session already owned" in details.plain
        assert "earlier output outside the recent snapshot" in details.plain
        assert backend.transcript_calls[-1] == "assignment-6"
        backend.fail_refresh = True
        await screen.refresh_progress()
        assert "Refresh failed" in str(screen.query_one("#agent-status").render())
        assert "retained target response" in details.plain
        await pilot.press("escape")
        assert not screen._expanded  # noqa: SLF001
        await pilot.press("escape")
        assert app.screen is app.screen_stack[0]
        assert not app._exit  # noqa: SLF001


@pytest.mark.asyncio
async def test_agent_workspace_preserves_distinct_candidates_for_one_assignment() -> None:
    class CandidateBackend(DemoBackend):
        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/candidates",
                    "session-demo",
                    "Candidate assessment",
                    "recommended",
                    "executing",
                    "digest",
                ),
            )

        async def list_agents(self):
            common = {
                "name": "specialist/execute",
                "owner": "lead",
                "task": "Try candidate",
                "provider": "demo",
                "elapsed": "1m",
                "latest_event": "completed",
                "campaign_id": "campaign/candidates",
                "assignment_id": "assignment/shared",
                "target_adapter": "browser",
                "target_generation": "generation/one",
                "scenario_fingerprint": "sha256:scenario",
                "controller_id": "pair",
                "payload_family": "baseline",
                "requested_language": "en",
                "mutation_id": "none",
            }
            return (
                AgentView(technique="technique/first", **common),
                AgentView(technique="technique/second", **common),
            )

    backend = CandidateBackend()
    app = build_app(backend)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = app_module.AgentScreen(backend, "session-demo")
        await app.push_screen(screen)
        await pilot.pause()

        workers = tuple(agent for agent in screen._agents if agent.profile != "Campaign Lead")
        assert {agent.technique for agent in workers} == {
            "technique/first",
            "technique/second",
        }
        assert len({screen._key(agent) for agent in workers}) == 2


def test_readable_log_summarizes_embedded_browser_failure_diagnostics() -> None:
    row = (
        "tool: browser_boundary: tab limit reached\n"
        "Automatic failure diagnostics:\n"
        '{"network":{"evidence_refs":[],"observed_at":"2026-10-15T11:28:20Z",'
        '"origin":"http://localhost:3100","target_id":"web-localhost",'
        '"text":"{\\"response_json\\":{\\"transcript\\":[\\"private raw payload\\"]}}"}}\n'
        "Inspect the captured diagnostics before changing strategy."
    )

    readable = app_module._readable_log((row,), width=100)  # noqa: SLF001

    assert "Tool failure · Browser Boundary" in readable
    assert "tab limit reached" in readable
    assert "Diagnostics captured: network" in readable
    assert "Origin: http://localhost:3100" in readable
    assert "Target Id: web-localhost" in readable
    assert "Open Raw JSON for the full captured diagnostics." in readable
    assert "response_json" not in readable
    assert "transcript" not in readable
    assert "private raw payload" not in readable


def test_readable_lead_log_collapses_lifecycle_noise_and_keeps_conclusions() -> None:
    def row(sequence: int, kind: str, **payload):
        return json.dumps(
            {
                "sequence": sequence,
                "wave": 4,
                "created_at": f"2026-10-15T14:47:{sequence:02d}+00:00",
                "payload": {"kind": kind, **payload},
            }
        )

    rows = (
        row(1, "incremental_task_review_started", task_id="task-1"),
        row(
            2,
            "incremental_task_review_completed",
            review={
                "task_id": "task-1",
                "summary": "HTTP 409 left no attributable target receipt.",
                "signal": "infrastructure",
                "confidence": 0.95,
            },
        ),
        row(3, "incremental_task_review_started", task_id="task-2"),
        row(
            4,
            "incremental_task_review_completed",
            review={
                "task_id": "task-2",
                "summary": "The target rejected the injection.",
                "signal": "negative",
                "confidence": 0.8,
            },
        ),
        row(5, "incremental_review_merge_started", review_count=2),
        row(
            6,
            "incremental_review_merge_completed",
            review_count=2,
            summary="One task lacked receipts; the other produced a defensive rejection.",
            followups=[{"profile_id": "adversarial-explorer"}],
        ),
        row(7, "execution_priority"),
        row(8, "lead_review_started"),
        row(9, "lead_review_progress", completed_chunks=12),
        row(10, "lead_review_completed"),
        row(11, "lead_decision_rejected", reason="unknown attack-graph node"),
        row(12, "lead_decision_rejected", reason="unknown attack-graph node"),
    )

    readable = app_module._readable_log(rows, width=100, lead=True)  # noqa: SLF001

    assert "Campaign Lead · current" in readable
    assert "Wave 4 · Correcting a rejected plan" in readable
    assert "Task reviews: 2 complete" in readable
    assert "Review merge: complete" in readable
    assert "Plan corrections: 2" in readable
    assert "Lifecycle pings are collapsed here; press R for the complete journal." in readable
    assert "Lead conclusion" in readable
    assert "Reviews merged: 2" in readable
    assert "One task lacked receipts" in readable
    assert "Follow-ups dispatched: 1 · adversarial-explorer" in readable
    assert readable.count("unknown attack-graph node") == 1
    assert "Plan correction · 2026-10-15 14:47:12Z" in readable
    assert "Plan correction · 2026-10-15 14:47:11Z" not in readable
    assert "Incremental Task Review Started" not in readable
    assert "Lead Review Progress" not in readable
    assert "Execution Priority" not in readable
    assert "HTTP 409 left no attributable target receipt." in readable
    assert "The target rejected the injection." not in readable


def test_readable_lead_log_shows_unmerged_task_signal_while_reviews_continue() -> None:
    rows = (
        json.dumps(
            {
                "sequence": 1,
                "wave": 5,
                "created_at": "2026-10-15T14:48:28+00:00",
                "payload": {
                    "kind": "incremental_task_review_completed",
                    "review": {
                        "task_id": "task-1",
                        "summary": "The request completed without target-bound evidence.",
                        "signal": "ambiguous",
                        "confidence": 0.7,
                        "findings": [{"kind": "missing_receipt"}],
                    },
                },
            }
        ),
        json.dumps(
            {
                "sequence": 2,
                "wave": 5,
                "created_at": "2026-10-15T14:48:29+00:00",
                "payload": {
                    "kind": "incremental_task_review_started",
                    "task_id": "task-2",
                },
            }
        ),
    )

    readable = app_module._readable_log(rows, width=100, lead=True)  # noqa: SLF001

    assert "Task reviews: 1 complete · 1 running" in readable
    assert "Task review · 2026-10-15 14:48:28Z · Wave 5" in readable
    assert "Ambiguous · 70% confidence" in readable
    assert "The request completed without target-bound evidence." in readable
    assert "Produced: 1 findings" in readable


@pytest.mark.asyncio
async def test_agent_workspace_renders_lead_journal_and_worker_chat_with_raw_toggle() -> None:
    class ReadableLogsBackend(DemoBackend):
        lead_calls = 0

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/readable",
                    "session-demo",
                    "Readable assessment",
                    "exploration",
                    "executing",
                    "digest",
                ),
            )

        async def list_agents(self):
            return (
                AgentView(
                    name="worker-0",
                    owner="lead",
                    task="Test Gertie",
                    provider="demo",
                    elapsed="4s",
                    latest_event="running",
                    profile="Tier 1 specialist",
                    assignment_id="assignment-readable",
                    campaign_id="campaign/readable",
                    output=(
                        "assistant: I will send the reviewed handoff prompt.",
                        'Tool call: {"function":{"name":"browser_click",'
                        '"arguments":"{\\"element_ref\\":\\"play\\"}"}}',
                        'tool: {"kind":"snapshot","origin":"http://localhost:3100",'
                        '"elements":[{},{}]}',
                    ),
                ),
            )

        async def get_campaign_lead_log(self, campaign_id, limit=100):
            self.lead_calls += 1
            return (
                {
                    "sequence": 7,
                    "wave": 1,
                    "created_at": "2026-10-13T09:41:02+00:00",
                    "payload": {
                        "kind": "lead_assessment",
                        "summary": "Reviewed three worker reports; no forced win is proven.",
                        "actions": [{"kind": "continue"}],
                        "followups": [{"profile_id": "tier-2"}],
                        "plan_update": {"rationale": "Escalate from the strongest near miss."},
                    },
                },
            )

    backend = ReadableLogsBackend()
    app = build_app(backend)
    async with app.run_test(size=(140, 44)) as pilot:
        await app.push_screen(app_module.AgentScreen(backend, "session-demo"))
        await pilot.pause()
        screen = app.screen

        worker = screen.query_one("#overview-content", app_module.AgentOutputText)
        assert "╭─ Worker reasoning" in worker.plain
        assert "Action · browser_click" in worker.plain
        assert "Target · snapshot" in worker.plain
        assert '"context_id"' not in worker.plain

        await pilot.press("down")
        assert screen._selected_agent_key == "campaign-lead::campaign/readable"  # noqa: SLF001
        lead = screen.query_one("#worker-output-1", app_module.AgentOutputText)
        assert "Campaign Lead" in lead.plain
        assert "Lead · 2026-10-13 09:41:02Z · Wave 1" in lead.plain
        assert "no forced win is proven" in lead.plain
        assert "Follow-ups dispatched: 1 · tier-2" in lead.plain
        assert '"payload"' not in lead.plain

        await pilot.press("r")
        assert '"payload"' in lead.plain
        await pilot.press("r", "enter")
        await pilot.pause()
        lead = screen.query_one("#overview-content", app_module.AgentOutputText)
        assert backend.lead_calls >= 2
        assert "Campaign Lead journal" in lead.plain
        assert "Lead · 2026-10-13 09:41:02Z · Wave 1" in lead.plain


@pytest.mark.asyncio
async def test_live_swarm_indicator_animates_without_rebuilding_history_and_stops_when_paused() -> (
    None
):
    class AnimationBackend(DemoBackend):
        status = "executing"

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/animation",
                    "session-demo",
                    "Animation assessment",
                    "recommended",
                    self.status,
                    "digest",
                ),
            )

    backend = AnimationBackend()
    app = build_app(backend)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()
        live = app.query_one("#swarm-live", app_module.LiveSwarmLog)
        assert live.animating
        history = live.lines
        original = live.render_line(1).text
        for _ in range(3):
            live._advance_indicator()  # noqa: SLF001
        assert live.render_line(1).text != original
        assert live.lines is history
        assert "Working" in live.render_line(1).text
        backend.status = "paused"
        await app._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()
        frame = live._animation_frame  # noqa: SLF001
        live._advance_indicator()  # noqa: SLF001
        assert live._animation_frame == frame  # noqa: SLF001
        assert not live.animating
        assert "Paused" in live.render_line(1).text


@pytest.mark.asyncio
async def test_wave_review_indicator_animates_without_changing_progress_or_history() -> None:
    class ReviewBackend(DemoBackend):
        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/review",
                    "session-demo",
                    "Review assessment",
                    "quickscan",
                    "executing",
                    "digest",
                    2,
                    "followup",
                    "completed",
                ),
            )

    app = build_app(ReviewBackend())
    async with app.run_test(size=(160, 40)) as pilot:
        await app._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()
        live = app.query_one("#swarm-live", app_module.LiveSwarmLog)
        assert live.animating
        review_line = next(
            i for i, line in enumerate(live.lines) if "Reviewing wave results" in line.text
        )
        history = live.lines
        original = live.render_line(review_line).text
        for _ in range(3):
            live._advance_indicator()  # noqa: SLF001
        updated = live.render_line(review_line).text
        assert updated != original
        assert "Reviewing wave results" in updated
        prefix_width = len("◐ Reviewing wave results   ")
        assert updated[prefix_width:] == original[prefix_width:]
        assert live.lines is history


@pytest.mark.asyncio
async def test_failed_campaign_replaces_running_swarm_even_when_worker_refresh_fails() -> None:
    class TransitionBackend(DemoBackend):
        failed = False
        report_unavailable = False
        status_unavailable = False

        async def list_campaigns(self):
            if self.status_unavailable:
                raise RuntimeError("campaign connection lost api_key=not-for-display")
            return (
                CampaignView(
                    "campaign/transition",
                    "session-demo",
                    "Assessment transition",
                    "recommended",
                    "failed" if self.failed else "executing",
                    "digest",
                    stop_reason="infrastructure_blocked_after_retry_policy" if self.failed else "",
                ),
            )

        async def list_agents(self):
            if self.failed:
                raise RuntimeError("worker snapshot unavailable")
            return ()

        async def list_reports(self):
            if self.report_unavailable:
                raise RuntimeError("report connection lost")
            if not self.failed:
                return ()
            return (
                ReportView(
                    "campaign/transition",
                    "durable-revision",
                    "/saved/assessment.html",
                    "ready",
                    0,
                    report_state="incomplete",
                    stop_reason="failed",
                    cleanup_status="completed",
                ),
            )

    backend = TransitionBackend()
    app = build_app(backend)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()
        live = app.query_one("#swarm-live", app_module.LiveSwarmLog)
        assert "Working" in "".join(line.text for line in live.lines)
        assert live.animating
        backend.failed = True
        await app._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()
        text = "".join(line.text for line in live.lines)
        assert "Working" not in text
        assert "Scan failed — partial results retained" in text
        assert "Campaign report ready" in text
        assert "Infrastructure retries exhausted" in text
        assert "/saved/assessment.html" in text
        assert not live.animating
        assert app.query_one("#scan-actions").has_class("visible")
        # A later report refresh failure must not erase the saved export.
        backend.report_unavailable = True
        await app._poll_swarm_progress()  # noqa: SLF001
        assert "/saved/assessment.html" in "".join(line.text for line in live.lines)
        assert app.query_one("#scan-actions").has_class("visible")
        # Losing campaign status explicitly shows a reconnect state, not live work.
        backend.status_unavailable = True
        await app._poll_swarm_progress()  # noqa: SLF001
        text = "".join(line.text for line in live.lines)
        assert "retrying automatically" in text
        assert "Scan failed — partial results retained" in text
        assert "campaign connection lost" in text
        assert "not-for-display" not in text
        assert "/saved/assessment.html" in text
        assert not live.animating
        backend.status_unavailable = False
        await app._poll_swarm_progress()  # noqa: SLF001
        assert "Scan failed — partial results retained" in "".join(line.text for line in live.lines)


def test_partial_campaign_labels_and_report_access() -> None:
    campaign = CampaignView(
        "campaign/partial",
        "session-demo",
        "Target assessment",
        "recommended",
        "failed",
        "digest",
        stop_reason="infrastructure_blocked_after_retry_policy",
    )
    report = ReportView(
        "campaign/partial",
        "durable-revision",
        "/saved/partial.html",
        "ready · failed",
        1,
        report_state="incomplete",
        stop_reason="failed",
    )
    campaigns = app_module._render_campaigns((campaign,))
    assert "Failed · partial results retained" in campaigns
    assert "Infrastructure retries exhausted" in campaigns
    status = app_module._render_campaign_status(
        {"status": "failed", "stop_reason": "infrastructure_blocked_after_retry_policy"}
    )
    assert "Status: Failed · partial results retained" in status
    assert "Stop reason: Infrastructure retries exhausted" in status
    reports = app_module._render_reports((report,))
    assert "Failed after partial execution · report ready" in reports
    assert "/saved/partial.html" in reports
    assert "ready · failed" not in reports
    limited = ReportView(
        "campaign/limited",
        "durable-revision",
        "/saved/limited.html",
        "ready",
        1,
        report_state="incomplete",
        stop_reason="frontier_exhausted",
    )
    assert "Completed with partial results · report ready" in app_module._render_reports((limited,))


def test_failed_scan_without_report_is_terminal_and_actionable() -> None:
    campaign = CampaignView(
        "campaign/failed",
        "session-demo",
        "Target assessment",
        "quickscan",
        "failed",
        "digest",
        stop_reason="infrastructure_blocked_after_retry_policy",
    )

    rendered = app_module._render_scan_summary(campaign, None).plain

    assert "Scan failed — partial results retained" in rendered
    assert "State: Failed · partial results retained" in rendered
    assert "Infrastructure retries exhausted" in rendered
    assert "Final report: unavailable after failure" in rendered
    assert "report pending" not in rendered
    assert "/campaign status" in rendered


def test_terminal_scan_summary_shows_scanned_duration() -> None:
    campaign = CampaignView(
        "campaign/completed",
        "session-demo",
        "Target assessment",
        "exploration",
        "completed",
        "digest",
        stop_reason="frontier_exhausted",
        elapsed_seconds=44_640,
    )

    rendered = app_module._render_scan_summary(campaign, None).plain

    assert "Scanned: 12h 24m" in rendered


@pytest.mark.asyncio
async def test_partial_scan_recovery_preserves_report_and_resumes_same_campaign() -> None:
    class RecoverableBackend(DemoBackend):
        resumed = []
        state = "failed"

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/partial",
                    "session-demo",
                    "Recover assessment",
                    "recommended",
                    self.state,
                    "digest",
                ),
            )

        async def list_agents(self):
            return ()

        async def list_reports(self):
            return (
                ReportView(
                    "campaign/partial",
                    "durable-revision",
                    "/saved/partial.html",
                    "ready · failed",
                    1,
                    report_state="incomplete",
                    stop_reason="failed",
                ),
            )

        async def resume_campaign(self, campaign_id):
            self.resumed.append(campaign_id)
            self.state = "executing"
            return {"status": "executing"}

    backend = RecoverableBackend()
    app = build_app(backend)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app._poll_swarm_progress()
        await pilot.pause()
        assert not app.query_one("#scan-recover", Button).disabled
        assert app.query_one("#scan-actions").has_class("visible")
        assert "/saved/partial.html" in "".join(
            line.text for line in app.query_one("#swarm-live", RichLog).lines
        )
        await pilot.click("#scan-recover")
        await pilot.pause()
        assert backend.resumed == ["campaign/partial"]
        assert "Working" in "".join(
            line.text for line in app.query_one("#swarm-live", RichLog).lines
        )
        assert not app.query_one("#scan-actions").has_class("visible")
        assert (await backend.list_reports())[0].output == "/saved/partial.html"


@pytest.mark.asyncio
async def test_report_browser_opens_html_and_exports_selected_historical_campaign(
    tmp_path, monkeypatch
):
    html = tmp_path / "historical.html"
    html.write_text("<title>Historical assessment</title>")
    other = tmp_path / "current.html"
    other.write_text("<title>Current assessment</title>")
    opened = []
    monkeypatch.setattr(app_module.webbrowser, "open", lambda url: opened.append(url))

    class ReportsBackend(DemoBackend):
        exports = []

        async def list_reports(self):
            return (
                ReportView("campaign/current", "durable-revision", str(other), "ready", 0),
                ReportView(
                    "campaign/historical",
                    "durable-revision",
                    str(html),
                    "ready · failed",
                    2,
                    report_state="incomplete",
                    stop_reason="failed",
                ),
            )

        async def export_campaign_report(self, campaign_id, source, format):
            self.exports.append((campaign_id, source, format))
            output = tmp_path / f"historical.{format}"
            output.write_text("Saved historical report")
            return ReportView(campaign_id, format, str(output), "ready", 2)

    backend = ReportsBackend()
    app = build_app(backend)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app._show_reports()
        await pilot.pause()
        assert isinstance(app.screen, app_module.ReportScreen)
        await pilot.press("down")
        await pilot.pause()
        assert app.screen.selected_run == "campaign/historical"
        await pilot.click("#report-open-html")
        assert opened == [html.resolve().as_uri()]
        for format in ("markdown", "json", "csv"):
            await pilot.click(f"#report-export-{format}")
            await pilot.pause()
            assert backend.exports[-1] == ("campaign/historical", str(html), format)
            content = str(app.screen.query_one("#overview-content", app_module.Static).render())
            assert f"historical.{format}" in content
            assert "Saved:" in str(
                app.screen.query_one("#report-feedback", app_module.Static).render()
            )
        await pilot.press("escape")
        assert app.screen is app.default_screen
        assert app.is_running


@pytest.mark.asyncio
async def test_worker_log_reflows_after_hidden_or_one_column_layout() -> None:
    from rich.text import Text

    class LayoutApp(app_module.App):
        def compose(self):
            yield app_module.StyledDrawerText(id="worker-log")

    app = LayoutApp()
    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one("#worker-log", app_module.StyledDrawerText)
        view.styles.width = 1
        await pilot.pause()
        view.update(Text("Execute test: memory roleplay\nWorker owner: worker-42"))
        view.styles.width = 80
        await pilot.pause()
        assert any("Execute test: memory roleplay" in line.text for line in view.lines)
        assert any("Worker owner: worker-42" in line.text for line in view.lines)
        view.styles.display = "none"
        await pilot.pause()
        view.update(Text("Reviewing response from the target"))
        view.styles.display = "block"
        await pilot.pause()
        assert any("Reviewing response from the target" in line.text for line in view.lines)
        view.styles.width = 20
        await pilot.pause()
        view.styles.width = 80
        await pilot.pause()
        assert any("Reviewing response from the target" in line.text for line in view.lines)


def test_workspace_identifies_specialist_separately_from_task() -> None:
    agent = AgentView(
        name="instance/execute",
        owner="worker-42",
        task="Inspect memory",
        provider="demo",
        elapsed="1m",
        latest_event="running",
        profile="Memory specialist",
        specialist_instance_id="instance",
    )
    assert app_module._workspace_specialist_label(agent) == "Memory specialist"
    assert app_module._workspace_task_label(agent) == "Inspect memory"


@pytest.mark.asyncio
async def test_inline_swarm_summary_reflows_when_revealed_and_resized() -> None:
    from rich.text import Text

    class LayoutApp(app_module.App):
        def compose(self):
            yield app_module.LiveSwarmLog(min_width=1, wrap=True, id="summary")

    app = LayoutApp()
    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one("#summary", app_module.LiveSwarmLog)
        view.styles.display = "none"
        await pilot.pause()
        view.clear()
        view.write(Text("Scan partially completed\nHTML report: /tmp/report.html"))
        view.styles.display = "block"
        await pilot.pause()
        assert any("Scan partially completed" in line.text for line in view.lines)
        view.styles.width = 1
        await pilot.pause()
        view.clear()
        view.write(Text("Saved results remain available"))
        view.styles.width = 80
        await pilot.pause()
        assert any("Saved results remain available" in line.text for line in view.lines)


@pytest.mark.asyncio
async def test_campaign_polling_waits_until_foreground_takeover_finishes() -> None:
    class StartingBackend(DemoBackend):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.polls = 0

        async def acquire_foreground_lease(self, **kwargs):
            self.started.set()
            await self.release.wait()

        async def list_campaigns(self):
            self.polls += 1
            return ()

    backend = StartingBackend()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        await backend.started.wait()
        await pilot.pause(2.1)
        assert backend.polls == 0
        backend.release.set()
        await pilot.pause(2.1)
        assert backend.polls > 0


@pytest.mark.asyncio
async def test_long_worker_list_keeps_selection_and_uses_sidebar_width() -> None:
    class ManyWorkers(DemoBackend):
        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/many",
                    "session-demo",
                    "Assessment",
                    "recommended",
                    "executing",
                    "digest",
                ),
            )

        async def list_agents(self):
            return tuple(
                AgentView(
                    name=f"worker-{i}",
                    owner=f"owner-{i}",
                    task=f"Investigate target memory response {i}",
                    provider="demo",
                    elapsed="1m",
                    latest_event="running",
                    profile="Memory specialist",
                    assignment_id=f"task-{i}",
                    campaign_id="campaign/many",
                )
                for i in range(87)
            )

    app = build_app(ManyWorkers())
    async with app.run_test(size=(180, 60)) as pilot:
        await pilot.pause()
        screen = app_module.AgentScreen(app.backend, "session-demo")
        app.push_screen(screen)
        await pilot.pause()
        screen._selected_agent_key = "task-81"
        screen._render_progress(reveal_cursor=True)
        await pilot.pause()
        await screen.refresh_progress()
        await pilot.pause()
        picker = screen.query_one("#agent-picker", app_module.OptionList)
        assert picker.highlighted == 81
        assert screen._selected_agent_key == "task-81"
        assert "Memory specialist" in screen._picker_labels[81].plain
        assert "Investigate target memory response" in screen._picker_labels[81].plain
        await pilot.resize_terminal(120, 40)
        await pilot.pause()
        assert picker.highlighted == 81
        assert screen._selected_agent_key == "task-81"
        assert (
            "Worker owner: owner-81"
            in screen.query_one("#overview-content", app_module.AgentOutputText).plain
        )


@pytest.mark.asyncio
async def test_agent_detail_scrolling_stays_inside_pane_after_expand_and_resize() -> None:
    class TranscriptBackend(DemoBackend):
        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/scroll",
                    "session-demo",
                    "Assessment",
                    "recommended",
                    "executing",
                    "digest",
                ),
            )

        async def list_agents(self):
            return tuple(
                AgentView(
                    name=f"worker-{i}",
                    owner=f"owner-{i}",
                    task="Inspect memory",
                    provider="demo",
                    elapsed="1m",
                    latest_event="running",
                    profile="Memory specialist",
                    assignment_id=f"task-{i}",
                    campaign_id="campaign/scroll",
                    output=tuple(f"tool: row {n} " + "response " * 40 for n in range(50)),
                )
                for i in range(3)
            )

        async def agent_transcript(self, *args, **kwargs):
            return tuple(f"assistant: row {n} " + "response " * 40 for n in range(100))

    app = build_app(TranscriptBackend())
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        screen = app_module.AgentScreen(app.backend, "session-demo")
        app.push_screen(screen)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        view = screen.query_one("#overview-content", app_module.AgentOutputText)
        await pilot.press("pagedown", "pagedown")
        await pilot.pause()
        assert view.scroll_y > 0
        assert not view.show_horizontal_scrollbar
        assert view.virtual_size.width <= view.scrollable_content_region.width
        await pilot.resize_terminal(120, 40)
        await pilot.pause()
        await pilot.press("escape", "tab", "enter", "pagedown")
        await pilot.pause()
        assert not view.show_horizontal_scrollbar
        assert view.virtual_size.width <= view.scrollable_content_region.width


@pytest.mark.asyncio
async def test_initial_instruction_does_not_show_campaign_error_before_campaign_exists():
    class InitialBackend(DemoBackend):
        unavailable = True

        async def list_campaigns(self):
            if self.unavailable:
                raise RuntimeError("coroutine ignored GeneratorExit")
            return ()

    backend = InitialBackend()
    app = build_app(backend)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._poll_swarm_progress()
        await pilot.pause()
        view = app.query_one("#swarm-live", app_module.LiveSwarmLog)
        assert not view.has_class("visible")
        assert "GeneratorExit" not in "".join(line.text for line in view.lines)
        backend.unavailable = False
        await app._poll_swarm_progress()
        await pilot.pause()
        assert not view.has_class("visible")


@pytest.mark.asyncio
async def test_resumed_prepared_campaign_is_visible_without_execution_schedules():
    class PreparedBackend(DemoBackend):
        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/refund",
                    "session-demo",
                    "Policy Refund Trap L1",
                    "recommended",
                    "prepared",
                    "digest",
                ),
            )

        async def list_agents(self):
            return ()

    app = build_app(PreparedBackend())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._poll_swarm_progress()
        await pilot.pause()
        view = app.query_one("#swarm-live", app_module.LiveSwarmLog)
        assert not view.has_class("visible")
        assert not view.lines
        assert not view.animating


@pytest.mark.asyncio
async def test_prepared_strip_stays_hidden_on_refresh_error_then_shows_execution():
    class TransitionBackend(DemoBackend):
        state = "prepared"
        fail = False

        async def list_campaigns(self):
            if self.fail:
                raise RuntimeError("temporary refresh failure")
            return (
                CampaignView(
                    "campaign/refund",
                    "session-demo",
                    "Refund policy",
                    "quickscan",
                    self.state,
                    "digest",
                ),
            )

    backend = TransitionBackend()
    app = build_app(backend)
    async with app.run_test() as pilot:
        await app._poll_swarm_progress()
        view = app.query_one("#swarm-live", app_module.LiveSwarmLog)
        assert not view.has_class("visible")
        backend.fail = True
        await app._poll_swarm_progress()
        assert not view.has_class("visible")
        backend.fail = False
        backend.state = "executing"
        await app._poll_swarm_progress()
        await pilot.pause()
        assert view.has_class("visible")
        assert view.animating


@pytest.mark.asyncio
async def test_swarm_refresh_task_is_owned_and_agent_exit_requests_refresh() -> None:
    app = build_app(DemoBackend())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def poll():
            calls.append("refresh")
            entered.set()
            await release.wait()

        app._poll_swarm_progress = poll
        app._schedule_swarm_refresh()
        await entered.wait()
        task = app._swarm_poll_task
        assert task in app._background_tasks
        app._schedule_swarm_refresh()
        assert app._swarm_poll_task is task
        release.set()
        await task
        await app._show_agents()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert len(calls) >= 2


@pytest.mark.asyncio
async def test_startup_chooser_renders_before_slow_history_finishes(tmp_path) -> None:
    release = asyncio.Event()

    class SlowHistoryBackend(DemoBackend):
        async def list_resume_sessions(self):
            await release.wait()
            return await super().list_resume_sessions()

    app = MerlinApp(
        SlowHistoryBackend(),
        client_state=ClientState(tmp_path / "slow-history.json"),
        startup_prompt=True,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "Loading previous sessions" in str(app.screen.query_one("#startup-title").render())
        assert not app.screen.query_one("#startup-new").disabled
        assert app.screen.query_one("#startup-resume").disabled
        release.set()
        await pilot.pause()
        assert not app.screen.query_one("#startup-resume").disabled
        assert app.screen.query_one("#startup-resume").has_focus
        assert "default" in str(app.screen.query_one("#startup-resume").label)


def test_structural_worker_label_uses_actual_owner() -> None:
    from types import SimpleNamespace

    from merlin_tui.app import _workspace_specialist_label

    worker = SimpleNamespace(
        profile="Unassigned/structural tasks",
        owner="worker:campaign/saved/capability-probe/agent_messaging",
    )
    assert _workspace_specialist_label(worker) == "Worker · agent messaging"
    worker.owner = "unassigned"
    assert _workspace_specialist_label(worker) == "Worker pending assignment"


@pytest.mark.asyncio
async def test_native_agent_selection_survives_refresh_resize_and_copies() -> None:
    from merlin_tui.app import AgentOutputText
    from textual.app import App, ComposeResult

    class SelectionApp(App):
        def compose(self) -> ComposeResult:
            yield AgentOutputText("alpha beta\n" + "retained output\n" * 80, id="details")

    app = SelectionApp()
    async with app.run_test(size=(100, 35)) as pilot:
        view = app.query_one(AgentOutputText)
        await pilot.mouse_down("#details", offset=(0, 0))
        await pilot.pause()
        view.update("must not replace a mouse drag", preserve_scroll=True)
        await pilot.hover("#details", offset=(6, 0))
        await pilot.mouse_up("#details", offset=(6, 0))
        await pilot.pause()
        assert view.selected_text == "alpha"
        selection = view.selection
        view.update("updated output", preserve_scroll=True)
        await pilot.resize_terminal(80, 28)
        await pilot.pause()
        assert view.selection == selection
        assert view.selected_text == "alpha"
        await pilot.press("ctrl+shift+c")
        assert app.clipboard == "alpha"
        view.move_cursor((0, 0))
        view.update("updated output", preserve_scroll=True)
        assert view.text == "updated output"


@pytest.mark.asyncio
async def test_agents_overview_groups_by_identity_and_returns_to_tasks() -> None:
    import merlin_tui.app as app_module

    class GroupedBackend(DemoBackend):
        async def list_agents(self):
            return tuple(
                AgentView(
                    name="worker",
                    owner=f"owner-{i}",
                    task=f"Task {i}",
                    provider="test-model",
                    elapsed="1m",
                    latest_event="running" if i == 1 else "completed",
                    session_id="session-demo",
                    identity_provenance="durable",
                    specialist_instance_id="specialist/shared",
                    profile="Response reviewer",
                    assignment_id=f"task-{i}",
                    output=(f"Output {i}",),
                )
                for i in range(3)
            ) + (
                AgentView(
                    name="worker-auth",
                    owner="owner-auth",
                    task="Authorization prerequisite",
                    provider="test-model",
                    elapsed="1m",
                    latest_event="failed",
                    result="Credential was not authorized",
                    session_id="session-demo",
                    identity_provenance="durable",
                    specialist_instance_id="specialist/shared",
                    profile="Response reviewer",
                    assignment_id="task-auth",
                    failure_kind="authorization",
                ),
                AgentView(
                    name="PRIVATE",
                    owner="private",
                    task="PRIVATE",
                    provider="private",
                    elapsed="",
                    latest_event="running",
                    session_id="session-other",
                ),
            )

    app = build_app(GroupedBackend())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app._show_agents()
        await pilot.pause()
        tasks = app.screen
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, app_module.AgentsOverviewScreen)
        view = app.screen.query_one(app_module.AgentOutputText)
        assert len(app.screen._groups) == 1
        assert all(f"Task {i}" in view.text for i in range(3))
        assert "Current/latest task: Task 1" in view.text
        assert "test-model" in view.text and "Output 1" in view.text
        assert "authorization blocked · Authorization prerequisite" in view.text
        assert "failed · Authorization prerequisite" not in view.text
        assert "PRIVATE" not in view.text
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is tasks
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, app_module.AgentScreen)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError("")])
async def test_startup_error_retains_nonempty_cause_and_recovery_hint(failure):
    class BrokenNewBackend(DemoBackend):
        async def stream(self, prompt):
            raise failure
            if False:
                yield StreamEvent("content", "")

    app = MerlinApp(BrokenNewBackend())
    async with app.run_test() as pilot:
        await app._apply_startup_choice("new", ())
        await pilot.pause()
        transcript = "\n".join(line.text for line in app.query_one("#transcript").lines)
        assert (
            "within 30 seconds" in transcript
            if isinstance(failure, TimeoutError)
            else "RuntimeError" in transcript
        )
        assert "/resume" in transcript and "/new" in transcript
        assert not app.query_one("#composer").disabled


@pytest.mark.asyncio
async def test_resume_picker_arrows_scroll_and_enter_selects():
    from merlin_tui.app import ResumeScreen
    from merlin_tui.backend import ResumeSessionView

    sessions = tuple(
        ResumeSessionView(id=f"saved-{i}", title=f"Saved assessment {i}", updated_at="")
        for i in range(30)
    )
    app = MerlinApp(DemoBackend())
    selected = []
    async with app.run_test(size=(80, 24)) as pilot:
        screen = ResumeScreen(sessions)
        await app.push_screen(screen, selected.append)
        await pilot.pause()
        assert screen.focused.id == "resume-session-0"
        for _ in range(20):
            await pilot.press("down")
        await pilot.pause()
        assert screen.focused.id == "resume-session-20"
        assert screen.query_one("#dialog").scroll_offset.y > 0
        await pilot.press("up", "enter")
        await pilot.pause()
        assert selected == ["saved-19"]


@pytest.mark.asyncio
async def test_setup_jev_opens_key_dialog_without_provider_setup():
    from merlin_tui.app import JevSetupScreen

    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        await app._run_command("/setup jev")
        await pilot.pause()
        assert isinstance(app.screen, JevSetupScreen)
        key_input = app.screen.query_one("#jev-api-key")
        assert key_input.password
        assert key_input.has_focus
        await pilot.press("escape")
        assert app.screen is app.default_screen


@pytest.mark.asyncio
async def test_setup_menu_exposes_jev_key_configuration():
    from merlin_tui.app import ChoiceScreen, JevSetupScreen

    app = MerlinApp(DemoBackend())
    async with app.run_test() as pilot:
        await app._run_command("/setup")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        assert "JEV — judgment API key" in app.screen.choices[0]
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, JevSetupScreen)


@pytest.mark.asyncio
async def test_completed_scan_actions_stay_visible_during_report_refresh() -> None:
    class SlowReportsBackend(DemoBackend):
        started = asyncio.Event()
        release = asyncio.Event()
        slow = False

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/stable",
                    "session-demo",
                    "Stable report",
                    "recommended",
                    "failed",
                    "digest",
                ),
            )

        async def list_reports(self):
            if self.slow:
                self.started.set()
                await self.release.wait()
            return (
                ReportView(
                    "campaign/stable",
                    "durable-revision",
                    "/saved/stable.html",
                    "ready",
                    0,
                    report_state="incomplete",
                    stop_reason="failed",
                ),
            )

    backend = SlowReportsBackend()
    app = build_app(backend)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app._poll_swarm_progress()
        actions = app.query_one("#scan-actions")
        report = app._scan_summary_report
        assert actions.has_class("visible")
        backend.slow = True
        polling = asyncio.create_task(app._poll_swarm_progress())
        try:
            await asyncio.wait_for(backend.started.wait(), timeout=2)
            await pilot.pause()
            assert actions.has_class("visible")
            assert app._scan_summary_report is report
            assert not app.query_one("#scan-open-html", Button).disabled
            assert not app.query_one("#scan-recover", Button).disabled
        finally:
            backend.release.set()
            await polling
        assert actions.has_class("visible")


def test_ready_provider_markers_precede_the_provider_name() -> None:
    choices = ("Ollama — local", "LM Studio — local", "OpenAI — hosted", "DeepSeek — hosted")
    labels = app_module._marked_provider_choices(
        choices,
        {"base_url": "https://api.deepseek.com/v1"},
        {"api_key": "test-key"},
        {"ollama"},
    )
    assert labels[0] == "✓ Ollama — local · Installed"
    assert labels[1] == choices[1]
    assert labels[3] == "✓ DeepSeek — hosted · API key saved"


@pytest.mark.asyncio
async def test_leading_provider_tick_preserves_selection() -> None:
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._pick_provider("✓ DeepSeek — hosted · API key saved")
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)


@pytest.mark.asyncio
async def test_setup_menu_keyboard_scroll_and_marked_jev_selection(monkeypatch) -> None:
    import merlin_config

    monkeypatch.setattr(
        merlin_config,
        "load_config",
        lambda: SimpleNamespace(
            values={},
            judgments=SimpleNamespace(jev_token="saved-test-key"),
        ),
    )
    app = build_app(DemoBackend())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app._begin_provider_setup()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, app_module.SetupChoiceScreen)
        buttons = list(screen.query(Button))
        assert str(buttons[0].label).startswith("✓ JEV")
        assert buttons[0].has_focus
        await pilot.press(*(["down"] * 10))
        await pilot.pause()
        assert buttons[10].has_focus
        assert screen.query_one("#dialog").scroll_y > 0
        await pilot.press(*(["up"] * 10))
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, app_module.JevSetupScreen)
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_generic_choice_menu_arrows_scroll_and_enter() -> None:
    app = build_app(DemoBackend())
    result = []
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app_module.ChoiceScreen("Choices", tuple(f"Choice {i}" for i in range(20)))
        app.push_screen(screen, result.append)
        await pilot.pause()
        await pilot.press(*(["down"] * 15))
        await pilot.pause()
        assert list(screen.query(Button))[15].has_focus
        assert screen.query_one("#dialog").scroll_y > 0
        await pilot.press("up", "enter")
        await pilot.pause()
        assert result == ["Choice 14"]


def test_removed_command_aliases_are_not_advertised() -> None:
    assert "/stop" not in COMMANDS
    assert "/reports" not in COMMANDS
    assert "/stop" not in app_module.COMMAND_HINTS
    assert "press A" in app_module.COMMAND_HINTS["/agents"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile_number, profile",
    [
        ("1", "recommended"),
        ("2", "quickscan"),
        ("3", "exhaustive"),
        ("4", "exploration"),
    ],
)
@pytest.mark.parametrize("composition_number", ["1", "2"])
async def test_numbered_choices_use_real_profile_gate_without_loop(
    profile_number,
    profile,
    composition_number,
):
    from merlin_tui.kernel_backend import KernelBackend

    class RecordingKernel:
        session_id = "session-choice-regression"
        turn_active = False

        def __init__(self):
            self.submissions = []

        async def submit(self, prompt):
            self.submissions.append(prompt)

        async def next_event(self):
            await asyncio.Future()

    kernel = RecordingKernel()
    gate = KernelBackend(kernel)

    class ChoiceBackend(DemoBackend):
        async def stream(self, prompt):
            async for event in gate.stream(prompt):
                yield event

    app = MerlinApp(ChoiceBackend())
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "Run a merlin scan on https://target.test"
        await app.action_send()
        await pilot.pause()
        assert app._pending_choice_kind == "profile"
        app.query_one("#composer").text = profile_number
        await app.action_send()
        await pilot.pause()
        assert app._pending_choice_kind == "composition"
        app.query_one("#composer").text = composition_number
        await app.action_send()
        await pilot.pause(0.4)
        assert app._pending_choice_kind == ""
        assert len(kernel.submissions) == 1
        assert f"campaign profile: {profile};" in kernel.submissions[0]
        composition = "autonomous" if composition_number == "1" else "opinionated"
        assert f"specialist_composition={composition!r}" in kernel.submissions[0]
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        assert transcript.count("Choose the campaign profile:") == 1
        assert transcript.count("Agent composition") == 1


@pytest.mark.asyncio
async def test_startup_waits_beyond_old_history_timeout_without_starting_new(tmp_path) -> None:
    release = asyncio.Event()
    prompts = []

    class SlowHistoryBackend(DemoBackend):
        async def list_resume_sessions(self):
            await release.wait()
            return await super().list_resume_sessions()

        async def stream(self, prompt):
            prompts.append(prompt)
            async for event in super().stream(prompt):
                yield event

    app = MerlinApp(
        SlowHistoryBackend(),
        startup_prompt=True,
        client_state=ClientState(tmp_path / "long-history.json"),
    )
    async with app.run_test() as pilot:
        await pilot.pause(4.4)
        assert app.screen.query_one("#startup-new")
        assert "Loading previous sessions" in str(app.screen.query_one("#startup-title").render())
        assert prompts == []
        release.set()
        await pilot.pause()
        assert not app.screen.query_one("#startup-resume").disabled
        assert prompts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_key", ["r", "3"])
async def test_startup_history_failure_keeps_picker_and_can_retry(tmp_path, retry_key) -> None:
    calls = 0
    prompts = []
    release = asyncio.Event()

    class RetryHistoryBackend(DemoBackend):
        async def list_resume_sessions(self):
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise TimeoutError("history unavailable")
            await release.wait()
            return await super().list_resume_sessions()

        async def stream(self, prompt):
            prompts.append(prompt)
            async for event in super().stream(prompt):
                yield event

    app = MerlinApp(
        RetryHistoryBackend(),
        startup_prompt=True,
        client_state=ClientState(tmp_path / "retry-history.json"),
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        assert calls == 2
        assert prompts == []
        assert "Could not load" in str(app.screen.query_one("#startup-title").render())
        await pilot.press(retry_key, retry_key)
        await pilot.pause()
        assert calls == 3
        assert app.screen.query_one("#startup-browse").disabled
        release.set()
        await pilot.pause()
        assert not app.screen.query_one("#startup-resume").disabled
        assert "Browse previous sessions" in str(app.screen.query_one("#startup-browse").label)
        await pilot.press("1")
        await pilot.pause()
        assert prompts == ["/resume session-demo"]


@pytest.mark.asyncio
async def test_startup_late_history_does_not_override_explicit_new_session(tmp_path) -> None:
    release = asyncio.Event()
    prompts = []

    class SlowHistoryBackend(DemoBackend):
        async def list_resume_sessions(self):
            await release.wait()
            return await super().list_resume_sessions()

        async def stream(self, prompt):
            prompts.append(prompt)
            async for event in super().stream(prompt):
                yield event

    app = MerlinApp(
        SlowHistoryBackend(),
        startup_prompt=True,
        client_state=ClientState(tmp_path / "late-history.json"),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        app.query_one("#composer").text = "new session draft"
        release.set()
        await pilot.pause()
        assert app.query_one("#composer").text == "new session draft"
        assert not app.query_one("#composer").disabled
        assert not app.screen.query("#startup-new")
        assert prompts == ["/new"]


@pytest.mark.asyncio
async def test_recovered_review_waits_for_operator_then_executes_exact_binding():
    payload = {
        "campaign_id": "campaign/recovered",
        "manifest_digest": "sha256:saved",
        "objective": "Saved objective",
        "profile": "quickscan",
        "review": {
            "recovered_fields": {"Agent composition": "autonomous"},
            "target_contract": {"interface": "http_api", "execution_options": []},
        },
    }

    class RecoveredBackend(DemoBackend):
        def __init__(self):
            super().__init__()
            self.executions = []

        async def stream(self, prompt):
            yield StreamEvent("campaign_review", json.dumps(payload))

        async def execute_campaign(self, campaign_id, manifest_digest):
            self.executions.append((campaign_id, manifest_digest))

    backend = RecoveredBackend()
    app = MerlinApp(backend)
    async with app.run_test() as pilot:
        app.query_one("#composer").text = "/resume saved"
        await app.action_send()
        await pilot.pause()
        assert backend.executions == []
        assert app._pending_campaign_review == ("campaign/recovered", "sha256:saved")
        rendered = " ".join(line.text for line in app.query_one("#transcript").lines)
        assert "Saved campaign review" in rendered
        assert "Approve [default]" in rendered
        app.query_one("#composer").text = ""
        await app.action_send()
        await pilot.pause()
        assert backend.executions == [("campaign/recovered", "sha256:saved")]


def test_target_progress_renderer_shows_state_without_changing_swarm_summary():
    rendered = app_module._render_target_progress(
        {
            "status": "observed",
            "freshness": "current",
            "target_exchange_count": 3,
            "current_state": (
                {"name": "tier", "label": "Tier", "value": 2},
                {
                    "name": "score",
                    "label": "Score",
                    "value": 61,
                    "maximum_value": 100,
                },
                {"name": "turns_left", "label": "Turns Left", "value": 18},
            ),
            "latest_transition": {
                "outcome": "regression",
                "observations": ({"name": "score", "before_value": 72, "after_value": 61},),
            },
            "conflicts": (),
        }
    )

    assert "Tier 2 · Score 61/100 · Turns Left 18" in rendered.plain
    assert "Latest: Score 72 → 61 · regression" in rendered.plain


def test_target_progress_renderer_explains_unknown_state() -> None:
    rendered = app_module._render_target_progress(
        {
            "status": "unknown",
            "freshness": "unknown",
            "target_exchange_count": 1,
            "current_state": (),
        }
    )

    assert "State not discovered yet · 1 target exchange observed" in rendered.plain
    assert "execution continues" in rendered.plain


def test_target_progress_renderer_does_not_claim_failed_campaign_continues() -> None:
    rendered = app_module._render_target_progress(
        {
            "status": "unknown",
            "freshness": "unknown",
            "target_exchange_count": 23,
            "current_state": (),
        },
        campaign_status="failed",
    )

    assert "State not discovered yet · 23 target exchanges observed" in rendered.plain
    assert "before the campaign failed" in rendered.plain
    assert "execution continues" not in rendered.plain


@pytest.mark.asyncio
async def test_live_target_progress_card_is_separate_from_agent_swarm() -> None:
    class ProgressBackend(DemoBackend):
        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/progress",
                    "session-demo",
                    "Stateful assessment",
                    "quickscan",
                    "executing",
                    "digest",
                    target_progress={
                        "status": "observed",
                        "freshness": "current",
                        "target_exchange_count": 2,
                        "current_state": ({"name": "stage", "label": "Stage", "value": 3},),
                        "conflicts": (),
                    },
                ),
            )

    app = build_app(ProgressBackend())
    async with app.run_test(size=(120, 40)) as pilot:
        await app._poll_swarm_progress()  # noqa: SLF001
        await pilot.pause()

        progress = app.query_one("#target-progress", Static)
        swarm = app.query_one("#swarm-live", app_module.LiveSwarmLog)
        assert progress.has_class("visible")
        rendered = progress.render()
        assert "Target progress\nStage 3" in getattr(rendered, "plain", str(rendered))
        assert "Agent swarm" in "\n".join(line.text for line in swarm.lines)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["1", "approve", ""])
async def test_campaign_approval_bypasses_live_model_and_shows_responsive_startup(
    answer, monkeypatch
):
    from textual.widgets import Static

    finish_response = asyncio.Event()
    finish_startup = asyncio.Event()
    executing = asyncio.Event()
    clock = [1000.0]
    monkeypatch.setattr(app_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    class SlowBackend(DemoBackend):
        def __init__(self):
            super().__init__()
            self.executions = []
            self.steering = []

        async def stream(self, prompt):
            yield StreamEvent("campaign_review", json.dumps(_campaign_review_payload()))
            await finish_response.wait()

        async def steer(self, prompt):
            self.steering.append(prompt)
            return True

        async def execute_campaign(self, campaign_id, manifest_digest):
            self.executions.append((campaign_id, manifest_digest))
            executing.set()
            await finish_startup.wait()

    backend = SlowBackend()
    app = MerlinApp(backend)
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#composer").text = "prepare"
        await app.action_send()
        await pilot.pause()
        assert app._stream_task is not None and not app._stream_task.done()
        app.query_one("#composer").text = answer
        await pilot.press("enter")
        await asyncio.wait_for(executing.wait(), timeout=2)
        await pilot.pause()
        startup = app.query_one("#campaign-start", Static)
        first = str(startup.render())
        assert "Starting campaign" in first
        assert "Approval received" in first
        assert startup.region.height > 0
        await pilot.pause(0.25)
        assert str(startup.render()) != first
        clock[0] += 31
        await pilot.pause(0.15)
        assert "31s" in str(startup.render())
        assert "taking longer than expected" in str(startup.render())
        assert backend.steering == []

        # A repeated Enter must neither steer the model nor execute twice.
        app.query_one("#composer").text = "1"
        await pilot.press("enter")
        await pilot.pause()
        payload = _campaign_review_payload()
        assert backend.executions == [(payload["campaign_id"], payload["manifest_digest"])]
        assert backend.steering == []
        assert not app.query_one("#composer").disabled

        finish_startup.set()
        await pilot.pause()
        assert not app.query("#campaign-start")
        rendered = " ".join(line.text for line in app.query_one("#transcript").lines)
        assert "Campaign started:" in rendered
        finish_response.set()


@pytest.mark.asyncio
async def test_campaign_startup_error_removes_pending_indicator():
    class FailedBackend(DemoBackend):
        async def execute_campaign(self, campaign_id, manifest_digest):
            raise RuntimeError("daemon unavailable")

    app = MerlinApp(FailedBackend())
    async with app.run_test() as pilot:
        app._pending_choice_kind = "campaign_review"
        app._pending_campaign_review = ("campaign/test", "sha256:test")
        app.query_one("#composer").text = "1"
        await pilot.press("enter")
        await pilot.pause()
        assert not app.query("#campaign-start")
        rendered = " ".join(line.text for line in app.query_one("#transcript").lines)
        assert "Campaign execution failed: daemon unavailable" in rendered
        assert "Campaign started:" not in rendered
        assert app._campaign_start_task.done()


@pytest.mark.asyncio
async def test_empty_startup_history_keeps_picker_until_explicit_choice():
    class EmptyThenHistoryBackend(DemoBackend):
        def __init__(self):
            super().__init__()
            self.lookups = 0
            self.prompts = []

        async def list_resume_sessions(self):
            self.lookups += 1
            return () if self.lookups == 1 else await super().list_resume_sessions()

        async def stream(self, prompt):
            self.prompts.append(prompt)
            async for event in super().stream(prompt):
                yield event

    backend = EmptyThenHistoryBackend()
    app = MerlinApp(backend, startup_prompt=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "No saved sessions found" in str(app.screen.query_one("#startup-title").render())
        assert backend.prompts == []
        assert app.query_one("#composer").disabled
        await pilot.press("r")
        await pilot.pause()
        assert "Showing 1 recent session" in str(app.screen.query_one("#startup-title").render())
        assert not app.screen.query_one("#startup-resume").disabled
        assert backend.prompts == []
        await pilot.press("1")
        await pilot.pause()
        assert backend.prompts == ["/resume session-demo"]


def test_production_in_process_launcher_enables_startup_picker(monkeypatch):
    from merlin_tui import bootstrap

    monkeypatch.setattr(bootstrap, "build_backend", lambda **kwargs: DemoBackend())
    app = bootstrap.build_app()
    assert app._startup_prompt is True


@pytest.mark.parametrize("state", ["running", "completed"])
def test_live_swarm_displays_durable_wave_and_lead_review(state):
    from merlin_tui.backend import AgentView

    agents = (
        AgentView(
            "worker",
            "lead",
            "probe",
            "demo",
            "1m",
            "running" if state == "running" else "completed",
        ),
    )
    campaign = CampaignView(
        "campaign/live",
        "session-demo",
        "Refund policy",
        "quickscan",
        "executing",
        "digest",
        2,
        "attack",
        state,
    )
    text = app_module._render_live_swarm(agents, (campaign,), 0).plain
    assert "Wave 2 · attack" in text
    assert "campaign totals below" in text
    if state == "completed":
        assert "Reviewing wave results" in text
        assert "deciding next wave or completion" in text
    else:
        assert "Working" in text
        assert "Reviewing wave results" not in text


def test_live_swarm_explains_blocking_review_without_changing_target_progress():
    campaign = CampaignView(
        "campaign/review-progress",
        "session-demo",
        "Stateful assessment",
        "recommended",
        "executing",
        "digest",
        4,
        "frontier",
        "completed",
        target_progress={
            "current_state": ({"name": "tier", "label": "Tier", "value": 2, "maximum_value": 4},),
            "target_exchange_count": 19,
            "freshness": "current",
        },
        review_progress={
            "review_kind": "wave_results",
            "state": "reviewing",
            "blocking": True,
            "advisory": False,
            "elapsed_seconds": 132,
            "last_activity": "Comparing target-state transitions with required objectives",
            "completed_chunks": 3,
            "total_chunks": 5,
            "waiting_for": "The next wave needs the reviewed objective and strategy decision.",
        },
    )

    rendered = app_module._render_live_swarm((), (campaign,), 0).plain

    assert "Review: Wave results · NEXT-WAVE GATE · 2m 12s" in rendered
    assert "Activity: Comparing target-state transitions with required objectives" in rendered
    assert "Progress: 3/5 review chunks complete" in rendered
    assert "Waiting: The next wave needs the reviewed objective and strategy decision." in rendered
    assert campaign.target_progress == {
        "current_state": ({"name": "tier", "label": "Tier", "value": 2, "maximum_value": 4},),
        "target_exchange_count": 19,
        "freshness": "current",
    }


def test_live_swarm_explains_advisory_review_is_not_blocking_execution():
    campaign = CampaignView(
        "campaign/advisory-review",
        "session-demo",
        "Parallel assessment",
        "exploration",
        "executing",
        "digest",
        3,
        "attack",
        "running",
        review_progress={
            "kind": "novelty_analysis",
            "blocking": False,
            "elapsed_seconds": 8,
            "last_activity": "Reviewing novel technique families",
        },
    )

    agent = AgentView("worker", "lead", "Continue safe work", "demo", "8s", "running")
    rendered = app_module._render_live_swarm((agent,), (campaign,), 0).plain

    assert "Review: Novelty analysis · ADVISORY · 8s" in rendered
    assert "Waiting: No execution is waiting; safe work may continue in parallel." in rendered
    assert "Working" in rendered


@pytest.mark.asyncio
async def test_agent_workspace_header_and_campaign_list_show_wave():
    from textual.widgets import Static

    class WaveBackend(DemoBackend):
        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/live",
                    "session-demo",
                    "Refund policy",
                    "quickscan",
                    "executing",
                    "digest",
                    2,
                    "attack",
                    "running",
                ),
            )

    backend = WaveBackend()
    app = build_app(backend)
    async with app.run_test() as pilot:
        screen = app_module.AgentScreen(backend, "session-demo")
        await app.push_screen(screen)
        await pilot.pause()
        heading = str(screen.query_one("#agent-heading", Static).render())
        assert "Wave 2 · attack" in heading
        assert "Wave 2 · attack" in app_module._render_campaigns(await backend.list_campaigns())


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_new_session_clears_old_ui_only_after_backend_confirmation(success, tmp_path):
    from textual.widgets import Static

    class NewBackend(DemoBackend):
        async def stream(self, prompt):
            assert prompt == "/new"
            yield StreamEvent(
                "status" if success else "error",
                "new session" if success else "session creation failed",
            )

    state = ClientState(tmp_path / "client.json")
    app = build_app(NewBackend())
    app._client_state = state
    async with app.run_test() as pilot:
        app._activate_conversation()
        app.query_one("#transcript").write("old assessment")
        app._remember("content", "old assessment")
        app._pending_choice = (("Approve", "approve"),)
        app._pending_choice_kind = "campaign_review"
        app._pending_campaign_review = ("campaign/old", "digest")
        app._pending_profile_answer = "quickscan"
        app.query_one("#scan-actions").add_class("visible")
        app.query_one("#scan-next").add_class("visible")
        await app._stream("/new")
        await pilot.pause()
        transcript = "".join(line.text for line in app.query_one("#transcript").lines)
        if success:
            assert "old assessment" not in transcript
            assert not app._pending_choice
            assert app._pending_campaign_review is None
            assert not app._pending_profile_answer
            assert not app.query_one("#welcome", Static).has_class("hidden")
            assert not app.query_one("#scan-actions").has_class("visible")
            assert not app.query_one("#scan-next").has_class("visible")
            assert state.load() == ("", ())
        else:
            assert "old assessment" in transcript
            assert app._pending_campaign_review == ("campaign/old", "digest")


@pytest.mark.asyncio
async def test_scan_completion_hint_survives_refresh_and_hides_for_next_scan():
    class FinishedBackend(DemoBackend):
        status = "completed"

        async def list_campaigns(self):
            return (
                CampaignView(
                    "campaign/scan",
                    "session-demo",
                    "Refund policy",
                    "quickscan",
                    self.status,
                    "digest",
                ),
            )

    backend = FinishedBackend()
    app = build_app(backend)
    async with app.run_test() as pilot:
        for _ in range(2):
            await app._poll_swarm_progress()
            assert app.query_one("#scan-next").has_class("visible")
        backend.status = "prepared"
        await app._poll_swarm_progress()
        assert not app.query_one("#scan-next").has_class("visible")
        backend.status = "executing"
        await app._poll_swarm_progress()
        await pilot.pause()
        assert not app.query_one("#scan-next").has_class("visible")


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ("number", "arrows", "mouse"))
async def test_command_session_permission_choice_is_explicit_and_keyboard_accessible(
    tmp_path, choice
):
    app = build_app(DemoBackend())
    async with app.run_test(size=(60, 20)) as pilot:
        pending = asyncio.create_task(
            app.confirm_permission_tool(
                SimpleNamespace(name="shell_command"),
                SimpleNamespace(session_id="session-command", workspace_root=str(tmp_path)),
                {
                    "argv": ["echo", "reviewed"],
                    "__permission_effect": "subprocess",
                    "__permission_command_scope": True,
                },
            )
        )
        await pilot.pause()
        exact = app.query_one("#permission-allow-command-session", Button)
        assert exact.display
        assert "command + directory" in str(exact.label)
        drawer = app.query_one("#permission-drawer").content_region
        for button in app.query("#permission-drawer Button"):
            assert drawer.contains_region(button.region)
            assert button.region.bottom <= app.query_one("#composer").region.y
        assert "Command: echo reviewed" in str(app.query_one("#permission-details").render())
        if choice == "number":
            await pilot.press("2")
        elif choice == "arrows":
            await pilot.press("down", "enter")
        else:
            await pilot.click("#permission-allow-command-session")
        await pilot.pause()
        assert str(await pending) == "allow_command_session"


@pytest.mark.asyncio
async def test_unverified_command_cannot_select_command_session_permission(tmp_path):
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        pending = asyncio.create_task(
            app.confirm_permission_tool(
                SimpleNamespace(name="shell_command"),
                SimpleNamespace(session_id="session-unverified", workspace_root=str(tmp_path)),
                {"argv": ["echo", "reviewed"], "__permission_effect": "subprocess"},
            )
        )
        await pilot.pause()
        assert not app.query_one("#permission-allow-command-session", Button).display
        await pilot.press("5")
        await pilot.pause()
        assert not pending.done()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert str(await pending) == "allow_session"


@pytest.mark.asyncio
async def test_command_session_approval_reaches_broker_and_changed_arguments_reprompt(tmp_path):
    from merlin_contracts import ToolContext
    from merlin_permissions import PermissionEngine, PermissionMode
    from merlin_tools import workspace_tool_broker

    class CommandStore:
        def __init__(self):
            self.rules = {}
            self.audits = []

        async def find_rule(self, request):
            return self.rules.get((request.effect, request.scope.fingerprint))

        async def save_rule(self, rule):
            self.rules[(rule.effect, rule.scope_fingerprint)] = rule

        async def record_audit(self, audit):
            self.audits.append(audit)

    app = build_app(DemoBackend())
    store = CommandStore()
    broker = workspace_tool_broker(
        confirmer=app.confirm_permission_tool,
        permissions=PermissionEngine(PermissionMode.PROMPT, store),
    )
    context = ToolContext(session_id="command-integration", workspace_root=str(tmp_path))
    async with app.run_test() as pilot:
        first = asyncio.create_task(
            broker.invoke("shell_command", context, {"argv": ["/bin/echo", "a"]})
        )
        await pilot.pause()
        assert not first.done()
        assert app.query_one("#permission-allow-command-session", Button).display
        await pilot.press("2")
        await pilot.pause()
        assert (await first).content == "a\n"
        repeated = await broker.invoke("shell_command", context, {"argv": ["/bin/echo", "a"]})
        assert repeated.content == "a\n"
        assert not app.query_one("#permission-drawer").has_class("visible")
        changed = asyncio.create_task(
            broker.invoke("shell_command", context, {"argv": ["/bin/echo", "b"]})
        )
        await pilot.pause()
        assert not changed.done()
        assert "Command: /bin/echo b" in str(app.query_one("#permission-details").render())
        await pilot.press("escape")
        await pilot.pause()
        assert (await changed).is_error
        assert len(store.rules) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "expected"),
    (
        ("1", "allow_once"),
        ("2", "allow_command_session"),
        ("3", "allow_session"),
        ("4", "deny_once"),
        ("5", "deny_session"),
    ),
)
async def test_command_permission_numbers_match_visible_choices(tmp_path, key, expected):
    app = build_app(DemoBackend())
    async with app.run_test() as pilot:
        pending = asyncio.create_task(
            app.confirm_permission_tool(
                SimpleNamespace(name="shell_command"),
                SimpleNamespace(session_id="session-numbered", workspace_root=str(tmp_path)),
                {
                    "argv": ["echo", "numbered"],
                    "__permission_effect": "subprocess",
                    "__permission_command_scope": True,
                },
            )
        )
        await pilot.pause()
        buttons = [button for button in app.query("#permission-drawer Button") if button.display]
        assert [str(button.label).split(".", 1)[0] for button in buttons] == list("12345")
        await pilot.press(key)
        await pilot.pause()
        assert str(await pending) == expected
