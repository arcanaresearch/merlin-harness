from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from merlin_tui.campaign_diagnostics import (
    MAX_UNCOMPRESSED_BYTES,
    CampaignDiagnosticsExporter,
)
from merlin_tui.local_control import LocalControlClient, serve

_FORBIDDEN = (
    "SUPER_SECRET_CANARY",
    "PROMPT_CANARY",
    "https://forbidden.example",
    "/Users/forbidden/path",
    "host.internal",
    "REASONING_CANARY",
    "OUTPUT_CANARY",
    "TOOL_ARGS_CANARY",
    "TARGET_EVIDENCE_CANARY",
    "SCREENSHOT_CANARY",
    "MAILBOX_CANARY",
    "CHECKPOINT_CANARY",
    "REPORT_CANARY",
)


class CampaignStore:
    def get(self, campaign_id: str):
        assert campaign_id == "campaign/diagnostic"
        return {
            "id": campaign_id,
            "session_id": "session-7",
            "status": "failed",
            "manifest_digest": "sha256:manifest",
            "schedule_id": "schedule-1",
            "stop_reason": "worker_failure",
            "created_at": "2026-10-29T01:00:00+00:00",
            "updated_at": "2026-10-29T02:00:00+00:00",
            "objective": "SUPER_SECRET_CANARY",
            "target_url": "https://forbidden.example",
            "workspace_path": "/Users/forbidden/path",
        }

    def list_waves(self, campaign_id: str):
        assert campaign_id == "campaign/diagnostic"
        return [
            {
                "campaign_id": campaign_id,
                "wave": 0,
                "kind": "initial",
                "schedule_id": "schedule-1",
                "status": "failed",
                "created_at": "2026-10-29T01:05:00+00:00",
                "updated_at": "2026-10-29T02:00:00+00:00",
                "objective": "PROMPT_CANARY",
            }
        ]

    def list_lead(self, campaign_id: str):
        assert campaign_id == "campaign/diagnostic"
        return [
            {
                "sequence": 9,
                "wave": 0,
                "created_at": "2026-10-29T01:06:00+00:00",
                "payload": {
                    "kind": "recon_completed",
                    "status": "completed",
                    "target_calls": 2,
                    "latency_ms": 125,
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 4,
                        "total_tokens": 7,
                        "estimated_cost_usd": 0.01,
                        "prompt": "PROMPT_CANARY",
                    },
                    "prompt": "PROMPT_CANARY",
                    "reasoning": "REASONING_CANARY",
                    "output": "OUTPUT_CANARY",
                    "tool_arguments": "TOOL_ARGS_CANARY",
                    "target_evidence": "TARGET_EVIDENCE_CANARY",
                    "screenshot": "SCREENSHOT_CANARY",
                    "mailbox": "MAILBOX_CANARY",
                    "checkpoint": "CHECKPOINT_CANARY",
                    "report": "REPORT_CANARY",
                    "secret": "SUPER_SECRET_CANARY",
                },
            },
            {
                "sequence": 10,
                "wave": 0,
                "created_at": "2026-10-29T01:07:00+00:00",
                "payload": {
                    "kind": "approval_recorded",
                    "state": "approved",
                    "reason": "SUPER_SECRET_CANARY",
                },
            },
        ]


class SchedulerStore:
    def load_schedule(self, schedule_id: str):
        assert schedule_id == "schedule-1"
        return {
            "id": schedule_id,
            "status": "failed",
            "event_count": 4,
            "created_at": "2026-10-29T01:05:00+00:00",
            "graph": {"prompt": "PROMPT_CANARY"},
            "checkpoint": "CHECKPOINT_CANARY",
        }

    def events(self, schedule_id: str):
        assert schedule_id == "schedule-1"
        return [
            {
                "sequence": 21,
                "type": "heartbeat",
                "created_at": "2026-10-29T01:10:00+00:00",
                "payload": {
                    "attempt_id": "attempt-1",
                    "generation": 3,
                    "host": "host.internal",
                },
            },
            {
                "sequence": 22,
                "type": "heartbeat",
                "created_at": "2026-10-29T01:11:00+00:00",
                "payload": {
                    "attempt_id": "attempt-1",
                    "generation": 3,
                    "path": "/Users/forbidden/path",
                },
            },
            {
                "sequence": 23,
                "type": "attempt_failed",
                "created_at": "2026-10-29T01:12:00+00:00",
                "payload": {
                    "attempt_id": "attempt-1",
                    "generation": 3,
                    "failure_kind": "provider_unavailable",
                    "retryable": True,
                    "error": "SUPER_SECRET_CANARY",
                    "tool_result": "TOOL_ARGS_CANARY",
                },
            },
        ]

    def list_nodes(self, schedule_id: str):
        assert schedule_id == "schedule-1"
        return [
            {
                "node_id": "worker:1",
                "state": "failed",
                "lease_generation": 3,
                "attempts_started": 1,
                "failed_attempts": 1,
                "result_digest": "TARGET_EVIDENCE_CANARY",
                "objective": "PROMPT_CANARY",
            }
        ]

    def list_attempts(self, schedule_id: str):
        assert schedule_id == "schedule-1"
        return [
            {
                "id": "attempt-1",
                "node_id": "worker:1",
                "generation": 3,
                "state": "failed",
                "failure_kind": "provider_unavailable",
                "started_at": "2026-10-29T01:09:00+00:00",
                "heartbeat_at": "2026-10-29T01:11:00+00:00",
                "completed_at": "2026-10-29T01:12:00+00:00",
                "error": "SUPER_SECRET_CANARY",
                "output": "OUTPUT_CANARY",
                "mailbox": "MAILBOX_CANARY",
            }
        ]


async def _usage(schedule_id: str):
    assert schedule_id == "schedule-1"
    return {
        "input_tokens": 11,
        "output_tokens": 13,
        "total_tokens": 24,
        "turns": 2,
        "estimated_cost_usd": 0.02,
        "prompt": "PROMPT_CANARY",
        "provider_response": "OUTPUT_CANARY",
    }


async def _effects(campaign_id: str, *, limit: int):
    assert campaign_id == "campaign/diagnostic"
    assert limit == 2_001
    return [
        {
            "idempotency_key": "effect-1",
            "session_id": "session-7",
            "call_id": "call-1",
            "agent_id": "worker-1",
            "attempt_id": "attempt-1",
            "campaign_id": campaign_id,
            "target_id": "target-1",
            "tool_name": "browser",
            "effect": "target_network",
            "arguments_digest": "sha256:arguments",
            "arguments_payload": {"secret": "TOOL_ARGS_CANARY"},
            "state": "ambiguous",
            "result_content": "TARGET_EVIDENCE_CANARY",
            "result_is_error": True,
            "result_metadata": {
                "effect_ambiguous": True,
                "replacement_blocked": True,
                "failure_code": "target_session_identity_unavailable",
                "screenshot": "SCREENSHOT_CANARY",
            },
            "reconciliation": "SUPER_SECRET_CANARY",
            "created_at": datetime(2026, 10, 29, 1, 12, tzinfo=UTC),
            "updated_at": datetime(2026, 10, 29, 1, 13, tzinfo=UTC),
        }
    ]


def _read_json_lines(bundle: zipfile.ZipFile, name: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in bundle.read(name).splitlines()]


@pytest.mark.asyncio
async def test_export_is_bounded_structural_and_preserves_source_sequences(tmp_path: Path) -> None:
    output = tmp_path / "diagnostics.zip"
    result = await CampaignDiagnosticsExporter(
        CampaignStore(), SchedulerStore(), _usage, _effects
    ).export("campaign/diagnostic", str(output))

    assert result["output"] == str(output)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with zipfile.ZipFile(output) as bundle:
        assert bundle.namelist() == [
            "manifest.json",
            "campaign.json",
            "timeline.jsonl",
            "workers.jsonl",
            "effects.jsonl",
            "usage.json",
        ]
        all_content = b"".join(bundle.read(name) for name in bundle.namelist()).decode()
        for forbidden in _FORBIDDEN:
            assert forbidden not in all_content

        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["privacy"] == {
            "classification": "local-only",
            "reason": (
                "The bundle contains durable internal identifiers and failure metadata; "
                "no field-level shareability review is asserted."
            ),
            "shareable": False,
        }
        assert manifest["ordering"]["total_causal_order"] is False
        assert manifest["pre_prepare_correlation"]["available"] is False
        assert "no campaign_id" in manifest["pre_prepare_correlation"]["limitation"]
        assert manifest["aggregation"] == {
            "heartbeat_source_rows": 2,
            "heartbeat_summary_rows": 1,
        }
        assert manifest["schedule_discovery"] == {
            "authenticated_target_reobservation_schedules": True,
            "other_auxiliary_schedules": False,
            "wave_schedules": True,
        }
        assert manifest["source_errors"] == []

        timeline = _read_json_lines(bundle, "timeline.jsonl")
        heartbeat = next(row for row in timeline if row["type"] == "scheduler.heartbeat_summary")
        assert heartbeat["source_sequence_start"] == 21
        assert heartbeat["source_sequence_end"] == 22
        assert heartbeat["data"] == {
            "attempt_id": "attempt-1",
            "count": 2,
            "generation": 3,
        }
        failure = next(row for row in timeline if row["type"] == "scheduler.attempt_failed")
        assert failure["source_sequence"] == 23
        assert failure["data"] == {
            "attempt_id": "attempt-1",
            "failure_kind": "provider_unavailable",
            "generation": 3,
            "retryable": True,
        }
        lead = next(row for row in timeline if row["type"] == "lead.recon_completed")
        assert lead["source_sequence"] == 9
        assert lead["category"] == "recon"
        assert lead["data"]["target_calls"] == 2
        assert lead["data"]["latency_ms"] == 125

        workers = _read_json_lines(bundle, "workers.jsonl")
        assert workers[0]["record_type"] == "node"
        assert workers[1]["failure_kind"] == "provider_unavailable"
        effects = _read_json_lines(bundle, "effects.jsonl")
        assert effects == [
            {
                "agent_id": "worker-1",
                "arguments_digest": "sha256:arguments",
                "arguments_payload_present": True,
                "attempt_id": "attempt-1",
                "call_id": "call-1",
                "campaign_id": "campaign/diagnostic",
                "created_at": "2026-10-29T01:12:00+00:00",
                "effect": "target_network",
                "idempotency_key": "effect-1",
                "receipt": {
                    "effect_ambiguous": True,
                    "failure_code": "target_session_identity_unavailable",
                    "fields_missing": [
                        "target_effect_dispatch_status",
                        "effect_applied",
                        "target_effect_attempted",
                        "interaction_count",
                        "limit_interaction_count",
                        "target_call_budget_charge",
                        "target_session_id",
                    ],
                    "fields_present": [],
                    "replacement_blocked": True,
                },
                "reconciliation_present": True,
                "record_type": "tool_effect",
                "result_content_present": True,
                "result_is_error": True,
                "schema_version": "merlin.campaign-diagnostics.v1",
                "source_sequence": 1,
                "state": "ambiguous",
                "tool_name": "browser",
                "updated_at": "2026-10-29T01:13:00+00:00",
            }
        ]
        usage = json.loads(bundle.read("usage.json"))
        assert usage["campaign_total"] == {
            "estimated_cost_usd": pytest.approx(0.03),
            "input_tokens": 14,
            "output_tokens": 17,
            "total_tokens": 31,
            "turns": 2,
        }
        actual_uncompressed = sum(info.file_size for info in bundle.infolist())
        assert actual_uncompressed == manifest["uncompressed_bytes"]
        assert actual_uncompressed <= MAX_UNCOMPRESSED_BYTES

    with pytest.raises(FileExistsError):
        await CampaignDiagnosticsExporter(CampaignStore(), SchedulerStore(), _usage).export(
            "campaign/diagnostic", str(output)
        )


@pytest.mark.asyncio
async def test_export_reports_deterministic_row_and_string_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("merlin_tui.campaign_diagnostics.MAX_STRING_CHARS", 12)
    monkeypatch.setitem(
        sys.modules["merlin_tui.campaign_diagnostics"]._ROW_CAPS, "timeline.jsonl", 3
    )
    monkeypatch.setitem(
        sys.modules["merlin_tui.campaign_diagnostics"]._ROW_CAPS, "workers.jsonl", 1
    )
    output = tmp_path / "capped.zip"

    await CampaignDiagnosticsExporter(CampaignStore(), SchedulerStore(), _usage).export(
        "campaign/diagnostic", str(output)
    )

    with zipfile.ZipFile(output) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["truncation"]["timeline"] == {
            "input": 7,
            "retained": 3,
            "dropped": 4,
        }
        assert manifest["truncation"]["workers"] == {
            "input": 2,
            "retained": 1,
            "dropped": 1,
        }
        assert manifest["truncation"]["strings"] > 0
        assert len(_read_json_lines(bundle, "timeline.jsonl")) == 3
        assert len(_read_json_lines(bundle, "workers.jsonl")) == 1


@pytest.mark.asyncio
async def test_export_fails_closed_at_uncompressed_hard_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("merlin_tui.campaign_diagnostics.MAX_UNCOMPRESSED_BYTES", 1)
    output = tmp_path / "too-large.zip"

    with pytest.raises(RuntimeError, match="16 MiB uncompressed cap"):
        await CampaignDiagnosticsExporter(CampaignStore(), SchedulerStore(), _usage).export(
            "campaign/diagnostic", str(output)
        )

    assert not output.exists()


@pytest.mark.asyncio
async def test_export_does_not_overwrite_output_created_during_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "raced.zip"

    def racing_link(source: object, destination: object) -> None:
        del source
        Path(destination).write_text("created by another process", encoding="utf-8")
        raise FileExistsError

    monkeypatch.setattr("merlin_tui.campaign_diagnostics.os.link", racing_link)

    with pytest.raises(FileExistsError, match="already exists"):
        await CampaignDiagnosticsExporter(CampaignStore(), SchedulerStore(), _usage).export(
            "campaign/diagnostic", str(output)
        )

    assert output.read_text(encoding="utf-8") == "created by another process"
    temporary_paths = await asyncio.to_thread(lambda: list(tmp_path.glob(".raced.zip.*.tmp")))
    assert temporary_paths == []


@pytest.mark.asyncio
async def test_export_preserves_partial_bundle_when_one_schedule_cannot_load(
    tmp_path: Path,
) -> None:
    class BrokenSchedulerStore(SchedulerStore):
        def load_schedule(self, schedule_id: str):
            assert schedule_id == "schedule-1"
            raise RuntimeError("SUPER_SECRET_CANARY")

    output = tmp_path / "partial.zip"
    await CampaignDiagnosticsExporter(CampaignStore(), BrokenSchedulerStore(), _usage).export(
        "campaign/diagnostic", str(output)
    )

    with zipfile.ZipFile(output) as bundle:
        assert b"SUPER_SECRET_CANARY" not in b"".join(
            bundle.read(name) for name in bundle.namelist()
        )
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["source_errors"] == [
            {
                "error_type": "RuntimeError",
                "schedule_id": "schedule-1",
                "source": "schedule",
                "stage": "load",
            }
        ]


def test_campaign_diagnostics_cli_calls_adjacent_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    async def call(method: str, *args: str) -> None:
        calls.append((method, *args))

    monkeypatch.setattr("merlin_tui.cli._repair_working_directory", lambda: Path.cwd())
    monkeypatch.setattr("merlin_tui.cli._print_call", call)
    monkeypatch.setattr(
        "sys.argv",
        ["merlin", "campaign", "diagnostics", "campaign/diagnostic", "bundle.zip"],
    )

    from merlin_tui.cli import main

    main()

    assert calls == [("export_campaign_diagnostics", "campaign/diagnostic", "bundle.zip")]


@pytest.mark.asyncio
async def test_campaign_diagnostics_is_available_over_local_control() -> None:
    class Backend:
        closed = False

        async def recover_background(self) -> None:
            return None

        async def export_campaign_diagnostics(self, campaign_id: str, output: str):
            return {"campaign_id": campaign_id, "output": output, "member_count": 5}

        async def close(self) -> None:
            self.closed = True

    socket_path = Path(f"/tmp/merlin-diagnostics-rpc-{os.getpid()}.sock")
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)
    backend = Backend()
    task = asyncio.create_task(serve(socket_path, backend_factory=lambda **_: backend))
    try:
        for _ in range(100):
            if await asyncio.to_thread(socket_path.exists):
                break
            await asyncio.sleep(0.01)
        else:
            await task
            raise AssertionError("local control socket did not start")
        client = LocalControlClient(socket_path=socket_path)
        result = await client.export_campaign_diagnostics(
            "campaign/diagnostic", "/tmp/diagnostics.zip"
        )
        assert result == SimpleNamespace(
            campaign_id="campaign/diagnostic",
            output="/tmp/diagnostics.zip",
            member_count=5,
        )
        await client.close()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
