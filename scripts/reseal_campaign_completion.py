"""Re-seal a terminal campaign's completion receipt after post-hoc evidence changes.

The report publisher refuses to seal when live evidence moved after the sealed
completion ("campaign evidence changed after completion was sealed; retry
completion"). This computes the live completion document through the real
CampaignService.status() projection and journals a fresh campaign_completion
receipt, mirroring CampaignService._finish_campaign. The daemon's report-retry
sweep then generates the report against the new seal.

Usage: uv run python scripts/reseal_campaign_completion.py <campaign_id>
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import psycopg
from merlin_agents.campaign_service import CampaignService
from merlin_agents.durable_service import DurableAgentService
from merlin_findings import FindingService
from merlin_storage_postgres import (
    CampaignStore,
    DurableSchedulerStore,
    PostgresFindingStore,
    ToolEffectRepository,
)
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get(
    "MERLIN_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5433/postgres"
)


async def main() -> int:
    campaign_id = sys.argv[1]
    connection = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)
    campaign_store = CampaignStore(connection)
    scheduler_store = DurableSchedulerStore(connection)
    effect_repository = ToolEffectRepository(connection)
    finding_store = PostgresFindingStore(connection)

    from merlin_tui.bootstrap import _verify_validator_evidence

    findings_service = FindingService(
        finding_store,
        workspace_root=Path.cwd(),
        validator_evidence_verifier=lambda witness, session_id, target_id: (
            _verify_validator_evidence(
                witness,
                session_id,
                target_id,
                scheduler_store=scheduler_store,
                effect_repository=effect_repository,
            )
        ),
    )

    async def completion_findings(session_id: str):
        values = await findings_service.list_findings_for_completion(session_id)
        projected = []
        for finding in values:
            if finding.validation != "validated":
                projected.append(finding)
                continue
            verified_witnesses = tuple(
                witness
                for witness in finding.witnesses
                if _verify_validator_evidence(
                    witness,
                    session_id,
                    finding.target_id,
                    scheduler_store=scheduler_store,
                    effect_repository=effect_repository,
                )
            )
            projected.append(
                finding.model_copy(
                    update={
                        "validation": ("validated" if verified_witnesses else "unvalidated"),
                        "witnesses": list(verified_witnesses),
                    }
                )
            )
        return tuple(projected)

    agents = DurableAgentService(None, None, scheduler_store)  # type: ignore[arg-type]
    service = CampaignService(
        campaign_store,
        agents,
        None,  # type: ignore[arg-type] - status() never touches preparation
        finding_reader=completion_findings,
    )
    status = await service.status(campaign_id)
    document = status.get("completion_document")
    digest = status.get("completion_digest")
    if not isinstance(document, dict) or not isinstance(digest, str):
        raise SystemExit("completion authority could not be sealed")
    row = campaign_store.get(campaign_id)
    waves = campaign_store.list_waves(campaign_id)
    wave = int(waves[-1]["wave"]) if waves else 0
    terminal_status = str(row.get("status"))
    stop_reason = str(row.get("stop_reason") or terminal_status)
    receipt = {
        "kind": "campaign_completion",
        "schema": "merlin.campaign-completion.v1",
        "completion_digest": digest,
        "terminal_status": terminal_status,
        "stop_reason": stop_reason,
        "document": document,
    }
    campaign_store.commit_lead_decision(
        campaign_id,
        wave,
        f"campaign-completion/{terminal_status}/{stop_reason}/{digest}",
        receipt,
        (),
    )
    print(f"re-sealed completion for {campaign_id}: {digest}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
