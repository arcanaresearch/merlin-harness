"""Deterministic durable-swarm to finding/report acceptance."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from merlin_agents import DurableAgentService
from merlin_contracts import (
    ChatRequest,
    FindingDraft,
    ModelCompleted,
    ModelEvent,
    ReportRequest,
    SwarmRequest,
    TextDelta,
)
from merlin_findings import FindingService
from merlin_storage_postgres import (
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    PostgresFindingStore,
    SessionRepository,
)


class FindingsSwarmProvider:
    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        prompt = "\n".join(message.content for message in request.messages).lower()
        if "assignment: test response header security" in prompt:
            content = json.dumps(
                {
                    "title": "Missing content security policy",
                    "severity": "medium",
                    "summary": "The target omitted CSP.",
                    "reproduction": "Request the root document.",
                    "remediation": "Set a restrictive CSP header.",
                }
            )
        elif "assignment: test error data exposure" in prompt:
            content = json.dumps(
                {
                    "title": "Verbose error response",
                    "severity": "low",
                    "summary": "The target returned internal details.",
                    "reproduction": "Send malformed JSON.",
                    "remediation": "Return a generic client error.",
                }
            )
        else:
            content = (
                '{"tasks":['
                '{"id":"headers","objective":"Test response header security"},'
                '{"id":"errors","objective":"Test error data exposure"}]}'
            )
        yield TextDelta(text=content)
        yield ModelCompleted(finish_reason="stop")


class NoTools:
    def describe(self) -> tuple[()]:
        return ()

    async def invoke(self, name: str, context: object, arguments: object) -> None:
        raise AssertionError("no tools admitted")


def test_durable_swarm_disposition_evidence_and_equivalent_exports(tmp_path: Path) -> None:
    asyncio.run(_durable_swarm_disposition_evidence_and_equivalent_exports(tmp_path))


async def _durable_swarm_disposition_evidence_and_equivalent_exports(
    tmp_path: Path,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("finding acceptance")
            scheduler_store = DurableSchedulerStore(connection)
            agents = DurableAgentService(
                FindingsSwarmProvider(),
                NoTools(),
                scheduler_store,
                model="deterministic",
            )
            events = [
                event
                async for event in agents.start_swarm(
                    SwarmRequest(
                        session_id=session.id,
                        objective="Split this assessment into finding candidates",
                        max_workers=2,
                    )
                )
            ]
            assert events[0].state == "admitted"
            schedule_id = events[0].text
            assert schedule_id
            tasks = await agents.list_tasks(session.id)
            assert len(tasks) == 2
            assert all(task.status == "completed" and task.result for task in tasks)

            findings = FindingService(PostgresFindingStore(connection), workspace_root=tmp_path)
            drafts = []
            for task in tasks:
                candidate = json.loads(task.result or "{}")
                reference = f"schedule://{schedule_id}/node/worker:{task.id}"
                drafts.append(
                    await findings.create_draft(
                        session.id,
                        FindingDraft(
                            **candidate,
                            creator=task.agent_id or task.id,
                            evidence_references=[reference],
                        ),
                    )
                )
            assert {draft.status for draft in drafts} == {"draft"}
            accepted = next(item for item in drafts if item.severity == "medium")
            rejected = next(item for item in drafts if item.severity == "low")
            await findings.accept_finding(session.id, accepted.id)
            await findings.reject_finding(session.id, rejected.id)

            markdown_result = await findings.generate_report(
                ReportRequest(session_id=session.id, format="markdown", output=Path("report.md"))
            )
            json_result = await findings.generate_report(
                ReportRequest(session_id=session.id, format="json", output=Path("report.json"))
            )
            markdown = markdown_result.output.read_text(encoding="utf-8")
            document = json.loads(json_result.output.read_text(encoding="utf-8"))
            exported = document["findings"]
            assert [item["id"] for item in exported] == [accepted.id]
            assert accepted.title in markdown and rejected.title not in markdown
            assert {item["id"] for item in exported} == {
                item.id
                for item in await findings.list_findings(session.id)
                if item.status == "accepted"
            }
            for reference in exported[0]["evidence_references"]:
                assert reference in markdown
                evidence = _resolve_evidence(scheduler_store, reference)
                assert evidence["payload"]["content"]


def _resolve_evidence(store: DurableSchedulerStore, reference: str) -> dict[str, object]:
    prefix = "schedule://"
    assert reference.startswith(prefix)
    schedule_id, node_id = reference.removeprefix(prefix).split("/node/", 1)
    return next(item for item in store.list_results(schedule_id) if item["node_id"] == node_id)
