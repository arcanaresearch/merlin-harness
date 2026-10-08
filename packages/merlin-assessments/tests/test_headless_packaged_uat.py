"""Headless packaged-campaign UAT lanes (see scripts/headless_packaged_uat.py).

Each test drives one seeded campaign lane against the standalone fixture
target with the same durable service machinery as production and a scripted
stub provider.  These node IDs are wired into
``scripts/acceptance_integration_slice.py`` as the headless packaged UAT
checkpoint groups.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from merlin_assessments import FixtureTargetServer

ROOT = Path(__file__).resolve().parents[3]
DRIVER_PATH = ROOT / "scripts" / "headless_packaged_uat.py"

spec = importlib.util.spec_from_file_location("headless_packaged_uat", DRIVER_PATH)
assert spec is not None and spec.loader is not None
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


async def test_headless_api_mode_governs_execution_and_exports_report(tmp_path: Path) -> None:
    async with FixtureTargetServer() as target:
        evidence = await driver.run_api_lane(tmp_path / "api", target)
    assert evidence["execution_mode"] == "api"
    assert evidence["effect_tools"] == ["target_request"]
    assert evidence["gate_verdict"] == "ambiguous"
    assert evidence["report"]["adaptive_controllers"]


async def test_headless_api_lane_cancel_restart_compaction_replay(tmp_path: Path) -> None:
    async with FixtureTargetServer() as target:
        evidence = await driver.run_api_restart_lane(tmp_path / "api-restart", target)
    assert evidence["mailbox_replayed"] is True
    assert evidence["mailbox_deduped"] is True
    assert evidence["attempts"] == ["expired", "completed"]
    assert evidence["service_replaced"] is True
    assert evidence["checkpoint_reused"] is True
    assert evidence["provider_calls"] == evidence["provider_calls_after_interruption"] == 2
    assert evidence["message_count"] == 2
    assert len(evidence["receipt_attempt_ids"]) == 2
    assert evidence["gate_verdict"] == "ambiguous"


async def test_headless_browser_mode_uses_compiled_browser_flow(tmp_path: Path) -> None:
    async with FixtureTargetServer() as target:
        evidence = await driver.run_browser_lane(tmp_path / "browser", target)
    assert evidence["execution_mode"] == "browser"
    assert evidence["effect_tools"] == ["browser_chat_exchange"]
    assert evidence["provider_calls"] == 2
    assert evidence["gate_verdict"] == "ambiguous"
    assert evidence["oracle_matched"] is True
    assert evidence["objective_oracle_observations"] == 2
    assert evidence["win_marker_observed"] is True
    assert evidence["mode"]["browser_user_agents"]
    assert evidence["mode"]["api_user_agents"] == []
    assert evidence["mode"]["page_loads"] >= 1
    assert evidence["observations"] >= 1
    assert evidence["acknowledged_effects"] == 2
    assert evidence["report"]["adaptive_controllers"]
    assert evidence["report"]["confirmed"] == 0
    assert str(evidence["report"]["goat_receipt"]).startswith("sha256:")


async def test_headless_hybrid_mode_runs_api_with_read_only_browser_checkpoints(
    tmp_path: Path,
) -> None:
    async with FixtureTargetServer() as target:
        evidence = await driver.run_hybrid_lane(tmp_path / "hybrid", target)
    assert evidence["execution_mode"] == "hybrid"
    assert evidence["effect_tools"] == ["target_request"]
    assert evidence["provider_calls"] == 2
    assert evidence["gate_verdict"] == "ambiguous"
    assert evidence["checkpoint_actions"] == ["snapshot"]
    assert evidence["observations"] >= 1
    assert evidence["mode"]["page_loads"] >= 1
    assert evidence["mode"]["message_count"] == 2
    assert evidence["mode"]["api_transport"] == "browser_context"
    assert evidence["report"]["adaptive_controllers"]
    assert evidence["report"]["goat_receipt"] is not None


async def test_headless_recon_reuse_revalidates_bot_session_oracle_evidence(
    tmp_path: Path,
) -> None:
    async with FixtureTargetServer() as target:
        evidence = await driver.run_recon_reuse_lane(tmp_path / "recon-reuse", target)
    scenarios = evidence["scenarios"]
    assert scenarios[0]["site_evidence_reused"] is True
    assert all(row["revalidation_required"] for row in scenarios[1:])
    assert all(row["own_evidence_verified"] for row in scenarios)
    assert all(row["cross_scenario_evidence_rejected"] for row in scenarios)
    assert len({row["bot"] for row in scenarios}) > 1
    assert len({row["tenant"] for row in scenarios}) > 1
    assert len({row["login"] for row in scenarios}) > 1
