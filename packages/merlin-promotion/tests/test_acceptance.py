import os
from pathlib import Path
from runpy import run_path

import pytest

ROOT = Path(__file__).resolve().parents[3]
ACCEPTANCE = run_path(str(ROOT / "scripts" / "acceptance_promotion.py"))
run_acceptance = ACCEPTANCE["run_acceptance"]
real_provider_textual_swarm = ACCEPTANCE["real_provider_textual_swarm"]


async def test_phase_11_promotion_acceptance(tmp_path: Path) -> None:
    result = await run_acceptance(tmp_path / "postgres")

    assert result == {
        "swarm_tasks": 2,
        "evidence_digest": result["evidence_digest"],
        "regression_rejected": True,
        "ninth_profile_accepted": True,
        "old_work": "baseline-eight",
        "new_work": "ninth-profile",
        "rolled_back": "baseline-eight",
        "model_self_promotion": "denied",
    }
    assert len(str(result["evidence_digest"])) == 64


@pytest.mark.skipif(
    os.environ.get("MERLIN_RUN_REAL_PROVIDER_ACCEPTANCE") != "1",
    reason="requires configured provider credentials",
)
async def test_real_provider_textual_swarm_proposes_committed_candidate() -> None:
    result = await real_provider_textual_swarm()

    assert str(result["candidate_id"]).startswith("real-swarm-")
    assert str(result["node_id"]).startswith("worker:")
    assert len(str(result["result_digest"])) == 64
