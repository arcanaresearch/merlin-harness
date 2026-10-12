"""Explicit browser acceptance fixture; no provider or target requests are sent."""

import asyncio
import json
import tempfile
from pathlib import Path

from aiohttp import web
from merlin_tui.backend import StreamEvent
from merlin_web.server import Workspace, create_app


class FixtureBackend:
    def __init__(self):
        self.campaign = {
            "id": "campaign/browser-fixture",
            "session_id": "fixture-session",
            "objective": "Fixture: assess the support assistant",
            "profile": "exploration",
            "status": "prepared",
            "manifest_digest": "sha256:fixture-reviewed",
        }
        self.directory = tempfile.TemporaryDirectory(prefix="merlin-web-fixture-")
        self.output = Path(self.directory.name) / "fixture.html"
        self.output.write_text(
            "<html><h1>Merlin fixture report</h1>"
            "<p>Explicit acceptance fixture: no live target.</p></html>"
        )
        self.reports = []
        self.turn_release = asyncio.Event()

    async def close(self):
        self.directory.cleanup()

    async def interrupt(self):
        self.turn_release.set()

    async def steer(self, prompt):
        return True

    async def conversation_context(self):
        return {"session_id": "fixture-session", "messages": []}

    async def list_resume_session_page(self, **kwargs):
        return {
            "items": [
                {
                    "id": "fixture-session",
                    "title": "Fixture: support assistant",
                    "updated_at": "2026-10-08",
                }
            ],
            "total": 1,
        }

    async def list_resume_sessions(self):
        return (await self.list_resume_session_page())["items"]

    async def list_campaigns(self):
        return [self.campaign]

    async def list_reports(self, *, session_only=False):
        return self.reports

    async def stream(self, prompt):
        if prompt == "/fixture-complete":
            self.campaign["status"] = "completed"
            self.reports = [
                {
                    "run": self.campaign["id"],
                    "format": "html",
                    "output": str(self.output),
                    "status": "completed",
                    "report_state": "completed",
                    "finding_count": 0,
                    "exchange_count": 12,
                    "cleanup_status": "completed",
                    "coverage": [["negative", 1], ["partial", 1], ["untested", 1]],
                    "limitations": ["Fixture evidence only"],
                }
            ]
            yield StreamEvent("content", "Fixture campaign completed; retained report available.")
            return
        if prompt.strip() == "/new":
            yield StreamEvent("status", "New fixture session created")
            return
        yield StreamEvent("status", "Fixture recon evidence restored")
        yield StreamEvent(
            "campaign_review",
            json.dumps(
                {
                    "campaign_id": self.campaign["id"],
                    "manifest_digest": self.campaign["manifest_digest"],
                    "review": {
                        "objective": self.campaign["objective"],
                        "profile": "exploration",
                        "specialist_composition": "autonomous",
                        "execution_mode": "api",
                        "model_routes": {"lead": "fixture-model", "worker": "fixture-model"},
                        "judgments": {"backend": "local fixture"},
                        "known_limitations": ["Fixture evidence only; no live target"],
                        "admitted_coverage": ["memory", "tool-use"],
                        "language_plan": ["en"],
                        "budget": {"source": "operator"},
                        "approval_binding": "Exact manifest digest",
                    },
                }
            ),
        )

    async def execute_campaign(self, campaign_id, digest):
        assert campaign_id == self.campaign["id"] and digest == self.campaign["manifest_digest"]
        self.campaign.update(
            status="executing",
            wave_number=2,
            wave_status="reviewing wave results",
            lead_activity="Reviewing retained evidence before completion",
        )
        return {"status": "executing"}

    async def pause_campaign(self, campaign_id):
        self.campaign["status"] = "paused"
        return "Paused fixture campaign"

    async def resume_campaign(self, campaign_id):
        self.campaign["status"] = "executing"
        return "Resumed fixture campaign"

    async def guide_campaign(self, *args):
        return {"delivery_state": "queued", "message": args[1]}

    async def export_campaign_report(self, campaign_id, source_output, format):
        assert campaign_id == self.campaign["id"] and source_output == str(self.output)
        suffix = {"markdown": ".md", "json": ".json", "csv": ".zip"}[format]
        path = self.output.with_suffix(suffix)
        path.write_text("Fixture report export: " + format)
        return {"output": str(path), "run": campaign_id, "format": format}

    async def list_agents(self):
        return [
            {
                "name": "memory-investigator/execute",
                "owner": "worker:fixture-1",
                "task": "Check remembered customer names against transfer verification",
                "provider": "fixture-model",
                "latest_event": "completed · retained evidence",
                "assignment_id": "assignment/fixture-1",
                "campaign_id": self.campaign["id"],
                "status": "completed",
                "evaluation_state": "negative",
                "output": ["Verification still required"],
                "activity": ["Sent request", "Retained reply"],
            },
            {
                "name": "discovery-investigator/execute",
                "owner": "worker:fixture-2",
                "task": "Test tool argument authority",
                "provider": "fixture-model",
                "latest_event": "working · target response retained",
                "assignment_id": "assignment/fixture-2",
                "campaign_id": self.campaign["id"],
                "status": "running",
            },
        ]

    async def get_agent_transcript(self, assignment_id):
        return [
            "Fixture transcript",
            "Request: Hello",
            "Response: Verification still required",
            f"Assignment: {assignment_id}",
        ]

    async def campaign_status(self, campaign_id):
        return {
            "campaign_id": campaign_id,
            "completion_document": {
                "cells": [
                    ["task/1", "memory", "negative"],
                    ["task/2", "tool-authority", "partial"],
                    ["task/3", "retrieval", "untested"],
                ],
                "hypotheses": [
                    {
                        "id": "hypothesis/1",
                        "mechanism": "Tool argument provenance confusion",
                        "outcome": "partial",
                    }
                ],
            },
            "budget": {"target_requests": 12},
            "pace": {"completed": 1},
            "saturation_decision": {"state": "awaiting_operator", "purpose": "coverage_extension"},
        }

    async def get_runtime_status(self):
        return {"model": "fixture-model", "provider": "fixture", "effort": "default"}

    def __getattr__(self, name):
        async def empty(*args, **kwargs):
            return []

        return empty


def main():
    workspace = Workspace(FixtureBackend())
    print(f"Fixture URL: http://127.0.0.1:8768/#token={workspace.token}", flush=True)
    web.run_app(
        create_app(workspace, Path(__file__).parents[1] / "frontend/dist"),
        host="127.0.0.1",
        port=8768,
        access_log=None,
        print=None,
    )


if __name__ == "__main__":
    main()
