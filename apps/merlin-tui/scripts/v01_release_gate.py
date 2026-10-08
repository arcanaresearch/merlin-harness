#!/usr/bin/env python3
"""Bounded v0.1 journey across production composition and executable acceptances."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

PHASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "setup-packs-skills-and-tui",
        (
            "apps/merlin-tui/tests/test_app.py::test_setup_masks_secret_and_never_renders_it",
            "apps/merlin-tui/tests/test_app.py::test_pack_and_target_overviews",
            "apps/merlin-tui/tests/test_app.py::test_skill_invocation_remains_visible_composer_text",
            "packages/merlin-assessments/tests/test_service_inputs.py",
            "packages/merlin-skills/tests/test_skills.py",
        ),
    ),
    (
        "local-and-authenticated-remote-mcp",
        (
            "packages/merlin-mcp/tests/test_manager.py::test_real_stdio_server_lifecycle_and_invocation",
            "packages/merlin-mcp/tests/test_remote.py",
            "apps/merlin-tui/tests/test_app.py::test_remote_mcp_credential_flow_masks_and_never_transcribes_secret",
        ),
    ),
    (
        "browser-and-durable-specialists",
        (
            "apps/merlin-tui/tests/test_phase9_production_browser.py",
            "packages/merlin-agents/tests/test_durable_service.py::test_durable_service_persists_before_execution_and_reopens_projection",
            "packages/merlin-agents/tests/test_durable_service.py::test_failed_workers_are_retried_once_with_new_fenced_generations",
            "packages/merlin-agents/tests/test_durable_service.py::test_only_committed_attributable_structured_findings_reach_sink",
        ),
    ),
    (
        "findings-memory-and-promotion",
        (
            "packages/merlin-findings/tests/test_postgres_service.py",
            "packages/merlin-memory/tests/test_service_acceptance.py",
            "packages/merlin-memory/tests/test_portable_memory.py::test_import_is_quarantined_until_explicit_subset_acceptance",
            "packages/merlin-agents/tests/test_durable_service.py::test_durable_memory_is_scoped_and_recorded_only_after_worker_commit",
            "packages/merlin-promotion/tests/test_acceptance.py::test_phase_11_promotion_acceptance",
        ),
    ),
    (
        "restart-readiness-and-shutdown",
        (
            "apps/merlin-tui/tests/test_phase12_production_readiness.py",
            "apps/merlin-tui/tests/test_app.py::test_readiness_drawers_config_and_resume_selection",
            "apps/merlin-tui/tests/test_kernel_backend.py::test_close_is_idempotent_and_closes_all_resources_after_failure",
            "packages/merlin-readiness/tests/test_readiness.py::test_resource_shutdown_is_reverse_order_idempotent_and_best_effort",
        ),
    ),
)

EXECUTABLES: tuple[tuple[str, str, int], ...] = (
    ("findings-real-provider", "scripts/acceptance_findings.py", 180),
    ("permissions", "scripts/acceptance_permissions.py", 90),
    ("remote-mcp-reconnect", "apps/merlin-tui/scripts/remote_mcp_acceptance.py", 60),
    ("portable-memory", "scripts/acceptance_memory.py", 90),
    ("promotion-real-provider", "scripts/acceptance_promotion.py", 180),
)


def _configured_secrets() -> tuple[str, ...]:
    from merlin_config import CredentialStore

    return tuple(value for value in CredentialStore().load().values() if len(value) >= 8)


def _contains_secret(output: str, secrets: tuple[str, ...]) -> bool:
    return any(secret in output for secret in secrets)


def main() -> int:
    started = time.monotonic()
    completed: list[dict[str, object]] = []
    secrets = _configured_secrets()
    for name, selectors in PHASES:
        phase_started = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *selectors],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        if result.returncode:
            # Pytest selectors and tracebacks contain no configured credential values.
            sys.stderr.write(result.stdout)
            sys.stderr.write(result.stderr)
            return result.returncode
        if _contains_secret(result.stdout + result.stderr, secrets):
            sys.stderr.write(f"credential value rendered during {name}\n")
            return 1
        completed.append({"phase": name, "seconds": round(time.monotonic() - phase_started, 2)})

    for name, script, timeout in EXECUTABLES:
        phase_started = time.monotonic()
        result = subprocess.run(
            [sys.executable, script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        output = result.stdout + result.stderr
        if _contains_secret(output, secrets):
            sys.stderr.write(f"credential value rendered during {name}\n")
            return 1
        if result.returncode:
            # Output is emitted only after credential-value scanning.
            sys.stderr.write(output)
            return result.returncode
        completed.append({"phase": name, "seconds": round(time.monotonic() - phase_started, 2)})
    print(
        json.dumps(
            {
                "status": "passed",
                "release": "v0.1",
                "seconds": round(time.monotonic() - started, 2),
                "phases": completed,
                "secrets_rendered": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
