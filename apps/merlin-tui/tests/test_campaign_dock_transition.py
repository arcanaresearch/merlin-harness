from dataclasses import replace

import pytest
from merlin_tui.app import LiveSwarmLog
from merlin_tui.backend import CampaignView, DemoBackend, ReportView
from merlin_tui.bootstrap import build_app


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_prepared", [False, True])
async def test_new_preparation_hides_previous_report_until_execution(restore_prepared):
    class Backend(DemoBackend):
        previous = CampaignView(
            "campaign/previous",
            "session-demo",
            "Previous scan",
            "quickscan",
            "failed",
            "old-digest",
        )
        latest = CampaignView(
            "campaign/new",
            "session-demo",
            "New scan",
            "quickscan",
            "prepared",
            "new-digest",
        )
        preparing = restore_prepared

        async def list_campaigns(self):
            return (self.latest, self.previous) if self.preparing else (self.previous,)

        async def list_reports(self):
            return (
                ReportView(
                    self.previous.id,
                    "durable-revision",
                    "/saved/previous.html",
                    "ready",
                    0,
                    report_state="incomplete",
                    stop_reason="failed",
                ),
            )

        async def list_agents(self):
            return ()

        async def execute_campaign(self, campaign_id, manifest_digest):
            assert (campaign_id, manifest_digest) == (self.latest.id, "new-digest")
            self.latest = replace(self.latest, status="executing")

    backend = Backend()
    app = build_app(backend)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        if not restore_prepared:
            await app._poll_swarm_progress()
            assert app.query_one("#scan-actions").has_class("visible")
            assert app._scan_summary_report is not None
        backend.preparing = True
        await app._poll_swarm_progress()
        assert app._scan_summary_report is None
        assert not app.query_one("#scan-actions").has_class("visible")
        assert not app.query_one("#scan-next").has_class("visible")
        live = app.query_one("#swarm-live", LiveSwarmLog)
        assert not live.has_class("visible")
        await app._execute_approved_campaign(backend.latest.id, "new-digest")
        await pilot.pause()
        await pilot.pause()
        assert live.has_class("visible")
        assert live.animating
        assert "New scan" in "".join(line.text for line in live.lines)
        assert "/saved/previous.html" not in "".join(line.text for line in live.lines)
        assert not app.query_one("#scan-actions").has_class("visible")
