#!/usr/bin/env python3
"""Run the deterministic production integration checkpoint serially.

This runner starts with one joined production fixture that crosses a physical
PostgreSQL/service replacement while retaining the exact worker checkpoint,
mailbox acknowledgement, loopback HTTP target effects, pause/resume events,
cleanup, CampaignService completion and sealed report.  It then composes the
remaining focused production regressions.  The API and browser target lanes are
separate because one reviewed campaign manifest cannot honestly claim both
adapter contracts.  The headless packaged UAT groups run one seeded campaign
per execution mode (api, browser, hybrid) against the standalone fixture
target via ``scripts/headless_packaged_uat.py`` — the release binary still
ships no seeded target, so this is the headless stand-in for packaged-campaign
certification.

Remote-provider, authorized external-target, and packaged-binary certification
remain separate operator-authorized boundaries; a local pass never represents
those external gates as complete.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Keep this matrix explicit.  Each node is a production-path regression fixture;
# pytest runs the complete list in one process so the embedded PostgreSQL port is
# never shared with a concurrent acceptance process.
CHECKPOINTS: dict[str, tuple[str, ...]] = {
    "joined_production_restart": (
        "packages/merlin-storage-postgres/tests/test_goat_joined_acceptance.py::test_joined_goat_pending_turn_survives_restart_without_regeneration_and_reports",
    ),
    "provider_transport": (
        "packages/merlin-providers/tests/test_openai_compatible.py::test_retries_rate_limit_before_output_and_honors_retry_after",
        "packages/merlin-providers/tests/test_openai_compatible.py::test_cancelling_active_stream_releases_shared_provider_admission",
        "packages/merlin-providers/tests/test_openai_compatible.py::test_incomplete_stream_preserves_partial_without_retry",
    ),
    "mailbox_replacement_compaction": (
        "packages/merlin-storage-postgres/tests/test_scheduler_store.py::test_pg0_live_mail_checkpoint_survives_restart_without_redelivery",
        "packages/merlin-storage-postgres/tests/test_scheduler_store.py::test_pg0_worker_restart_restores_mail_context_budget_and_retry_state",
        "packages/merlin-storage-postgres/tests/test_scheduler_store.py::test_pg0_unacknowledged_completion_is_projected_after_restart",
        "packages/merlin-agents/tests/test_campaign_service.py::test_campaign_lead_claims_typed_messages_and_commits_before_ack",
        "packages/merlin-agents/tests/test_campaign_service.py::test_campaign_lead_failure_leaves_worker_message_pending_for_replacement",
    ),
    "pause_resume_budget_lease": (
        "packages/merlin-storage-postgres/tests/test_scheduler_store.py::test_pg0_paused_schedule_fences_claims_and_resumes_from_checkpoint",
        "packages/merlin-storage-postgres/tests/test_campaign_budget_reservations.py",
        "packages/merlin-agents/tests/test_campaign_service.py::test_campaign_pause_and_resume_preserve_the_reviewed_wave",
    ),
    "api_effect_and_cleanup": (
        "apps/merlin-tui/tests/test_phase12_production_readiness.py::test_production_reviewed_api_session_survives_backend_restart",
        "apps/merlin-tui/tests/test_phase12_production_readiness.py::test_ambiguous_api_effect_is_not_replayed_after_postgres_restart",
        "packages/merlin-assessments/tests/test_api_target.py::test_reviewed_api_tool_reuses_one_session_and_runs_idempotent_cleanup",
        "packages/merlin-assessments/tests/test_api_target.py::test_reviewed_api_tool_cleans_ambiguous_session_before_next_assignment",
        "packages/merlin-storage-postgres/tests/test_effect_store.py::test_attempt_owned_effects_do_not_collide_and_survive_postgres_restart",
        "packages/merlin-storage-postgres/tests/test_permission_effect_restart.py::test_permissions_effects_and_cleanup_survive_postgres_restart",
    ),
    "browser_effect_and_cleanup": (
        "apps/merlin-tui/tests/test_phase12_production_readiness.py::test_cancelled_browser_effect_and_interrupted_report_survive_postgres_restart",
        "packages/merlin-storage-postgres/tests/test_browser_boundary.py::test_real_chromium_pg0_restart_cancel_and_evidence",
        "packages/merlin-storage-postgres/tests/test_browser_boundary.py::test_handoff_evidence_verification_is_target_bound_across_restart",
    ),
    "hierarchical_cancellation": (
        "packages/merlin-agents/tests/test_durable_service.py::test_exact_task_cancel_interrupts_live_provider_and_runs_cleanup",
        "packages/merlin-agents/tests/test_durable_service.py::test_schedule_cancel_interrupts_all_live_provider_streams",
        "packages/merlin-agents/tests/test_durable_service.py::test_reopen_resumes_cleanup_owed_by_cancelled_schedule",
        "packages/merlin-agents/tests/test_campaign_service.py::test_exact_cancel_resolves_active_campaign_across_console_sessions",
    ),
    "daemon_campaign_restart": (
        "packages/merlin-storage-postgres/tests/test_campaign_supervision_restart.py::test_concurrent_campaign_execute_rejoins_once_across_pg0_restart",
        "packages/merlin-storage-postgres/tests/test_campaign_supervision_restart.py::test_campaign_service_reconciles_existing_mailbox_effect_after_pg0_restart",
        "packages/merlin-storage-postgres/tests/test_campaign_supervision_restart.py::test_staged_attack_handoff_completes_before_execution_across_pg0_restart",
    ),
    "completion_and_report_seal": (
        "packages/merlin-agents/tests/test_campaign_service.py::test_completion_receipt_rejects_not_ready_saturation_and_conflicting_replay",
        "packages/merlin-findings/tests/test_report_parity_postgres.py::test_sealed_and_legacy_reports_restart_idempotently_and_fail_on_index_drift",
    ),
    "headless_packaged_api": (
        "packages/merlin-assessments/tests/test_headless_packaged_uat.py::test_headless_api_mode_governs_execution_and_exports_report",
        "packages/merlin-assessments/tests/test_headless_packaged_uat.py::test_headless_api_lane_cancel_restart_compaction_replay",
    ),
    "headless_packaged_browser": (
        "packages/merlin-assessments/tests/test_headless_packaged_uat.py::test_headless_browser_mode_uses_compiled_browser_flow",
    ),
    "headless_packaged_hybrid": (
        "packages/merlin-assessments/tests/test_headless_packaged_uat.py::test_headless_hybrid_mode_runs_api_with_read_only_browser_checkpoints",
        "packages/merlin-assessments/tests/test_headless_packaged_uat.py::test_headless_recon_reuse_revalidates_bot_session_oracle_evidence",
    ),
}

SCHEMA = "merlin.acceptance-integration-checkpoint"
VERSION = 1
DEFAULT_STATE = ROOT / "target" / "acceptance-integration-slice" / "state.json"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _source_identity() -> dict[str, str]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    digest = hashlib.sha256(status + b"\0" + diff).hexdigest()
    return {"head": head, "dirty_diff_digest": f"sha256:{digest}"}


def _write_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _load_resume_state(path: Path, source: dict[str, str]) -> dict[str, object]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot resume from {path}: {error}") from error
    if (
        not isinstance(state, dict)
        or state.get("schema") != SCHEMA
        or state.get("version") != VERSION
    ):
        raise SystemExit(f"cannot resume from {path}: unsupported checkpoint schema")
    if state.get("source") != source:
        raise SystemExit(f"cannot resume from {path}: repository source identity changed")
    if not isinstance(state.get("groups"), dict):
        raise SystemExit(f"cannot resume from {path}: checkpoint groups are malformed")
    return state


def _run_group(name: str, nodes: tuple[str, ...], log_path: Path) -> tuple[list[str], int]:
    command = [sys.executable, "-m", "pytest", "-q", *nodes]
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb") as log:
        try:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                check=False,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        finally:
            log.flush()
            os.fsync(log.fileno())
    return command, completed.returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true", help="resume passed checkpoint groups")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    arguments = parser.parse_args(argv)

    state_path = arguments.state.resolve()
    source = _source_identity()
    state = (
        _load_resume_state(state_path, source)
        if arguments.resume
        else {
            "schema": SCHEMA,
            "version": VERSION,
            "source": source,
            "started_at": _now(),
            "finished_at": None,
            "status": "running",
            "groups": {},
        }
    )
    groups = state["groups"]
    assert isinstance(groups, dict)
    state["status"] = "running"
    state["finished_at"] = None
    _write_state(state_path, state)

    for name, nodes in CHECKPOINTS.items():
        prior = groups.get(name)
        if arguments.resume and isinstance(prior, dict) and prior.get("status") == "passed":
            continue
        log_path = state_path.parent / f"{name}.log"
        command = [sys.executable, "-m", "pytest", "-q", *nodes]
        group = {
            "name": name,
            "node_ids": list(nodes),
            "command": command,
            "log": str(log_path),
            "started_at": _now(),
            "finished_at": None,
            "returncode": None,
            "status": "running",
        }
        groups[name] = group
        _write_state(state_path, state)
        print(f"[{name}] running; log={log_path}", flush=True)
        try:
            _, returncode = _run_group(name, nodes, log_path)
        except BaseException:
            group["status"] = "interrupted"
            group["finished_at"] = _now()
            state["status"] = "interrupted"
            state["finished_at"] = _now()
            _write_state(state_path, state)
            raise
        group["returncode"] = returncode
        group["finished_at"] = _now()
        group["status"] = "passed" if returncode == 0 else "failed"
        state["status"] = "running" if returncode == 0 else "failed"
        state["finished_at"] = None if returncode == 0 else _now()
        _write_state(state_path, state)
        print(f"[{name}] {group['status']}; returncode={returncode}", flush=True)
        if returncode != 0:
            return returncode

    state["status"] = "passed"
    state["finished_at"] = _now()
    _write_state(state_path, state)
    evidence = {
        "checkpoint": "INTEGRATION_SLICE_COMPLETE",
        **state,
        "state_path": str(state_path),
        "external_gates": {
            "packaged_binary": "not exercised; requires an explicit release request",
            "remote_provider": "not exercised; requires configured credentials",
            "authorized_target": "not exercised; requires explicit target authorization",
        },
    }
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
