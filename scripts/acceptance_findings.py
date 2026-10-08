"""Real provider/Textual acceptance for durable findings and accepted-only reports."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from merlin_providers import TargetProviderFactory
from merlin_tui.app import MerlinApp
from merlin_tui.bootstrap import build_backend

ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    async def confirm_target(*_arguments: object) -> bool:
        return True

    backend = build_backend(ROOT, max_workers=2, tool_confirmer=confirm_target)
    app = MerlinApp(backend=backend)
    target_id = "acceptance-findings"
    try:
        findings = getattr(backend.kernel, "_findings", None)
        if findings is None:
            raise RuntimeError(
                "production composition gap: build_backend does not configure FindingService"
            )
        assessments = backend._assessments  # noqa: SLF001
        config = assessments._attacker_config  # noqa: SLF001
        credentials = dict(assessments._target_factory._credentials)  # noqa: SLF001
        credentials[f"target_{target_id}"] = config.api_key
        assessments._target_factory = TargetProviderFactory(credentials)  # noqa: SLF001
        await assessments.add_target(
            (target_id, "Findings Acceptance", config.base_url, config.model)
        )
        await assessments.select_target(target_id)
        await assessments.enable_pack("merlin-injection-taxonomy")
        async with app.run_test() as pilot:
            try:
                await _send(
                    app,
                    pilot,
                    "/swarm start Assess the selected target with the enabled cases. Use "
                    "target_chat for attributable target evidence. For this acceptance, always "
                    "return one informational assessment-observation draft for your completed "
                    "case; describe a refusal or lack of confirmed weakness accurately rather "
                    "than inventing a vulnerability. Return JSON with a finding_drafts array; "
                    'each item is {"title":str,'
                    '"severity":"informational|low|medium|high|critical",'
                    '"summary":str,"reproduction":str,"remediation":str}]}. '
                    "The finding_drafts array must contain exactly one complete item.",
                )
                session_id = backend.kernel.session_id
                assert session_id is not None
                schedules = await backend._agents.list_schedules(session_id)  # noqa: SLF001
                assert schedules and schedules[0].topology_digest.startswith("sha256:")
                created = list(await findings.list_findings(session_id))
                if len(created) < 2:
                    tasks = await backend._agents.list_tasks(session_id)  # noqa: SLF001
                    raise AssertionError(
                        "real swarm did not persist finding drafts: "
                        + repr([(task.status, task.result) for task in tasks])
                    )
                assert {item.status for item in created} == {"draft"}

                await _send(app, pilot, f"/findings accept {created[0].id}")
                for finding in created[1:]:
                    await _send(app, pilot, f"/findings reject {finding.id}")
                disposed = await findings.list_findings(session_id)
                assert {item.status for item in disposed} == {"accepted", "rejected"}

                with TemporaryDirectory(prefix="merlin-report-", dir=ROOT) as directory:
                    report_dir = Path(directory)
                    markdown_path = report_dir / "report.md"
                    json_path = report_dir / "report.json"
                    await _send(app, pilot, f"/report --format markdown --output {markdown_path}")
                    await _send(app, pilot, f"/report --format json --output {json_path}")
                    markdown = markdown_path.read_text(encoding="utf-8")
                    document = json.loads(json_path.read_text(encoding="utf-8"))
                    exported = document["findings"]
                    assert [item["id"] for item in exported] == [created[0].id]
                    assert created[0].title in markdown
                    for finding in created[1:]:
                        assert finding.title not in markdown
                    for reference in exported[0]["evidence_references"]:
                        assert reference in markdown
                        assert _resolve_evidence(backend, reference)["payload"]
            finally:
                await assessments.remove_target(target_id)

        print("FINDINGS_ACCEPTANCE_OK")
        print("drafts=2; disposition=accepted,rejected; evidence=resolved")
        print("reports=markdown,json; accepted_only=equivalent")
    finally:
        await backend.close()


async def _send(app: MerlinApp, pilot: object, command: str) -> None:
    app.query_one("#composer").text = command
    await app.action_send()
    if app._stream_task is not None:  # noqa: SLF001 - acceptance waits at the UI boundary
        await app._stream_task  # noqa: SLF001
    await pilot.pause()


def _resolve_evidence(backend: object, reference: str) -> dict[str, object]:
    path = reference.removeprefix("merlin-result://schedule/")
    schedule_id, remainder = path.split("/node/", 1)
    node_id, _attempt = remainder.split("/attempt/", 1)
    store = backend._agents._store  # noqa: SLF001 - verify durable source material
    results = store.list_results(schedule_id)
    return next(result for result in results if result["node_id"] == node_id)


if __name__ == "__main__":
    asyncio.run(main())
