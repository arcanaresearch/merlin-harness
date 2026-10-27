#!/usr/bin/env python3
"""Sweep the real Merlin GOAT campaign engine across HarmBench behaviors.

One genuine ``DurableAgentService``/``CampaignService`` GOAT campaign per
behavior, driven by a live provider (DeepSeek via environment), against
``scripts/benchmark_target_server.py`` configured with that behavior. This
exercises the same engine the TUI drives, not an adapter reimplementation.

Provider environment (same contract as ``live_small_campaign.py``)::

    MERLIN_PROVIDER_BASE_URL          e.g. https://api.deepseek.com/v1
    MERLIN_PROVIDER_MODEL             e.g. deepseek-v4-pro
    MERLIN_PROVIDER_OUTPUT_TOKEN_PARAMETER  e.g. max_tokens
    MERLIN_CREDENTIAL_API_KEY         provider key

Run::

    uv run python scripts/benchmark_campaign_sweep.py \
        --behaviors-file target/sweep-behaviors/subset-1.json \
        --workdir target/sweep-results/subset-1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from headless_packaged_uat import (  # noqa: E402
    _allow,
    _contract_values,
    _digest,
    _prepare_campaign_store,
)
from live_small_campaign import BUDGET, RemoteFixture, _live_provider  # noqa: E402
from merlin_agents import (  # noqa: E402
    AssignmentContext,
    AssignmentTechnique,
    CampaignManifest,
    CampaignProfile,
    ContextDocument,
    DurableAgentService,
    DynamicSpecialistProposal,
    ReviewedTargetAdapter,
    WorkerAssignment,
    compile_attack_campaign,
    compile_dynamic_specialist,
    compile_worker_context,
)
from merlin_agents.campaign import CampaignCompilation, compile_execution_agenda  # noqa: E402
from merlin_agents.goat import GoatContract  # noqa: E402
from merlin_assessments import ReviewedApiTargetTool  # noqa: E402
from merlin_contracts import GOAT_TOOLBOX_CATALOG_DIGEST, trusted_goat_toolbox  # noqa: E402
from merlin_storage_postgres import (  # noqa: E402
    CampaignApiSessionRepository,
    CampaignStore,
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    PostgresToolEffectStore,
    SessionRepository,
    ToolEffectRepository,
)
from merlin_tools import ToolBroker  # noqa: E402

ROOT = SCRIPTS.parent


def _goat_contract_sweep(
    *,
    objective: str,
    target_id: str,
    adapter_digest: str,
    oracle_digest: str,
    semantic_suffix: str,
    provider_model: str,
) -> GoatContract:
    """GOAT contract whose goal digest is the benchmark behavior itself."""
    return GoatContract(
        version="merlin.goat.v1",
        semantic_technique_id=f"semantic:goat:{semantic_suffix}",
        goal_digest=_digest(objective),
        toolbox=trusted_goat_toolbox(),
        toolbox_catalog_digest=GOAT_TOOLBOX_CATALOG_DIGEST,
        terminal_oracle_digest=oracle_digest,
        reviewed_adapter_digest=adapter_digest,
        attacker_route_digest=_digest(
            {
                "provider_id": "provider",
                "model": provider_model,
                "role": "worker",
                "tool_transport": "buffered",
            }
        ),
        attacker_tool_transport="buffered",
        target_profile_id=target_id,
        session_policy="same-reviewed-session",
        modality="text",
        language="en",
        target_turns=2,
    )


def _campaign_sweep(
    *,
    objective: str,
    campaign_id: str,
    target_id: str,
    adapter: dict[str, Any],
    contract: GoatContract,
    oracle_digest: str,
    tool_ids: tuple[str, ...],
    endpoint: str,
    session_id: str,
    provider_model: str,
) -> tuple[CampaignManifest, AssignmentContext, Any, Any]:
    """Parameterized copy of the UAT campaign compilation."""
    reviewed_document = ContextDocument.create(
        "target:reviewed-interaction-contract",
        "Reviewed interaction contract\n"
        + json.dumps(adapter, sort_keys=True, separators=(",", ":")),
    )
    technique_id = f"case:goat:{campaign_id.rsplit('/', 1)[-1]}"
    manifest = CampaignManifest.create(
        id=campaign_id,
        session_id=session_id,
        objective=objective,
        profile=CampaignProfile.RECOMMENDED,
        target=ReviewedTargetAdapter(
            id=target_id,
            interface="http_api",
            endpoint=endpoint,
        ),
        provider_id="provider",
        provider_model=provider_model,
        provider_effort="default",
        authority_ids=("target:reviewed",),
        catalogue=(reviewed_document,),
        skill_documents=(),
        tool_ids=tool_ids,
        budget=BUDGET,
    )
    context = AssignmentContext(
        target_profile_id=target_id,
        techniques=(
            AssignmentTechnique(
                id=technique_id,
                objective=objective,
                tags=frozenset({"prompt-injection", "text", "multi", "conversational"}),
                guidance_ids=(),
                semantic_id=contract.semantic_technique_id,
                language="en",
                domain_id="prompt-injection",
                oracle_digest=oracle_digest,
                strategy_id="goat",
                strategy_contract=tuple(sorted(_contract_values(contract).items())),
            ),
        ),
        tool_ids=tool_ids,
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=BUDGET,
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        provider_model=provider_model,
    )
    compiled = compile_attack_campaign(manifest, context)
    ceiling = WorkerAssignment(
        id=f"{campaign_id}/dynamic-authority-ceiling",
        objective="campaign authority ceiling for dynamic specialist admission",
        profile_id="adversarial-explorer",
        target_profile_id=context.target_profile_id,
        technique_ids=tuple(item.id for item in context.techniques),
        guidance_ids=(),
        tool_ids=context.tool_ids,
        skill_ids=context.skill_ids,
        provider_id=context.provider_id,
        authority_ids=context.authority_ids,
        budget=context.budget,
        input_schema=context.input_schema,
        output_schema=context.output_schema,
        provider_model=context.provider_model,
        provider_capabilities=context.capabilities_for_model(context.provider_model),
        provider_input_cost_per_million=context.pricing_for_model(context.provider_model)[0],
        provider_output_cost_per_million=context.pricing_for_model(context.provider_model)[1],
    )
    record = compile_dynamic_specialist(
        DynamicSpecialistProposal(
            campaign_id=campaign_id,
            label="dynamic-injection",
            objective=f"Own the routed adaptive cell for {technique_id}.",
            source_task_ids=("recon-1",),
            evidence_refs=("merlin-result://recon/recon-1",),
            evidence_expectation="Cite the admitted recon evidence for every claim.",
            technique_ids=(technique_id,),
        ),
        parent=ceiling,
        context=context,
        graph_id=campaign_id,
        wave=0,
        active_count=0,
        campaign_count=0,
    )
    merged = CampaignCompilation(
        (*compiled.assignments, record.assignment),
        (
            *compiled.worker_contexts,
            compile_worker_context(manifest, record.assignment, record.profile),
        ),
    )
    agenda = compile_execution_agenda(manifest, context, merged)
    assignment = next(item for item in agenda.assignments if item.strategy_id == "goat")
    assert assignment.comparison_supported is True, assignment.comparison_block_reason
    return manifest, context, assignment, record.profile


async def run_sweep_lane(
    workdir: Path,
    origin: str,
    objective: str,
    behavior_id: str,
) -> dict[str, Any]:
    """One GOAT campaign against the benchmark target for one behavior."""
    target = RemoteFixture(origin)
    # The durable engine derives a tool call's campaign id from the first two
    # path segments of the assignment id, so campaign ids must be exactly two
    # segments (the canonical live_small_campaign lane uses campaign/live-small-api).
    campaign_id = f"campaign/sweep-{behavior_id.replace('_', '-')}"
    target_id = f"target-sweep-{behavior_id}"
    adapter = target.api_adapter("api")
    oracle_digest = target.oracle_digest()
    provider, model = _live_provider()
    contract = _goat_contract_sweep(
        objective=objective,
        target_id=target_id,
        adapter_digest=_digest(adapter),
        oracle_digest=oracle_digest,
        semantic_suffix=f"sweep-{behavior_id}",
        provider_model=model,
    )
    data_dir = workdir / f"postgres-{behavior_id}"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create(f"sweep {behavior_id}")
            manifest, context, assignment, worker_profile = _campaign_sweep(
                objective=objective,
                campaign_id=campaign_id,
                target_id=target_id,
                adapter=adapter,
                contract=contract,
                oracle_digest=oracle_digest,
                tool_ids=("target_request",),
                endpoint=target.origin + "/api/message",
                session_id=str(session.id),
                provider_model=model,
            )
            _prepare_campaign_store(connection, campaign_id, str(session.id), manifest, context)
            scheduler = DurableSchedulerStore(connection)
            async with httpx.AsyncClient() as client:
                api_target = ReviewedApiTargetTool(
                    CampaignApiSessionRepository(connection),
                    lambda _campaign_id: adapter,
                    {},
                    client=client,
                )
                broker = ToolBroker(
                    (api_target,),
                    confirmer=_allow,
                    effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
                )
                service = DurableAgentService(
                    provider,
                    broker,
                    scheduler,
                    model=context.provider_model,
                    workspace_root=str(workdir),
                    lease_seconds=120,
                    campaign_cleanup=api_target.cleanup_assignment,
                )
                live = await service.submit(
                    str(session.id),
                    (assignment,),
                    max_workers=1,
                    minimum_workers=1,
                    dynamic_profiles={assignment.id: worker_profile},
                )
                CampaignStore(connection).add_wave(campaign_id, 0, "initial", live.id)
                assert live.execution is not None
                await asyncio.wait_for(live.execution, timeout=300)
            gate = scheduler.goat_gate(live.id)
            attempts = scheduler.list_attempts(live.id)
            worker_results = [
                row
                for row in scheduler.list_results(live.id)
                if row["node_id"] == f"worker:{assignment.id}"
                and row["schema_id"] == "schema/result"
            ]
    async with httpx.AsyncClient() as client:
        target_log = {}
        try:
            response = await client.get(origin + "/api/log")
            target_log = response.json()
        except Exception:
            pass
    matched_any = any(
        bool(entry.get("matched")) for entry in target_log.get("entries", ())
    )
    return {
        "behavior_id": behavior_id,
        "objective": objective,
        "campaign_id": campaign_id,
        "technique_id": assignment.id,
        "terminal_attempt_states": [str(row["state"]) for row in attempts],
        "terminal_attempt_errors": [str(row["error"]) for row in attempts if row.get("error")],
        "gate": str(gate),
        "matched_any": matched_any,
        "target_entries": target_log.get("entries", []),
        "worker_results": [
            {"content": str(row["payload"].get("content"))}
            for row in worker_results
        ],
    }


def _start_target(behavior: str, victim_model: str, judge_model: str) -> tuple[subprocess.Popen, str]:
    process = subprocess.Popen(
        [
            "uv",
            "run",
            "python",
            str(SCRIPTS / "benchmark_target_server.py"),
            "--port",
            "0",
            "--victim-model",
            victim_model,
            "--judge-model",
            judge_model,
            "--behavior",
            behavior,
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    origin: str | None = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        line = process.stdout.readline().strip()
        if not line:
            if process.poll() is not None:
                break
            time.sleep(0.1)
            continue
        try:
            origin = str(json.loads(line)["origin"])
            break
        except (json.JSONDecodeError, KeyError):
            continue
    if origin is None:
        process.terminate()
        raise RuntimeError("benchmark target server failed to report an origin")
    return process, origin


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--behaviors-file", required=True, type=Path)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--victim-model", default="llama3.1:8b")
    parser.add_argument("--judge-model", default="llama3.1:8b")
    args = parser.parse_args()
    behaviors = json.loads(args.behaviors_file.read_text(encoding="utf-8"))
    if not isinstance(behaviors, list):
        raise SystemExit("behaviors file must contain a JSON list of {id, behavior} objects")
    args.workdir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for item in behaviors:
        behavior_id = str(item.get("id") or f"b{len(results)}")
        behavior = str(item.get("behavior") or "").strip()
        if not behavior:
            continue
        process, origin = _start_target(behavior, args.victim_model, args.judge_model)
        try:
            result = asyncio.run(
                run_sweep_lane(args.workdir, origin, behavior, behavior_id)
            )
        except Exception as exc:
            result = {
                "behavior_id": behavior_id,
                "objective": behavior,
                "error": f"{type(exc).__name__}: {exc}",
                "matched_any": False,
            }
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        results.append(result)
        print(json.dumps(result, separators=(",", ":")), flush=True)
    successes = sum(1 for item in results if item.get("matched_any"))
    summary = {
        "behaviors": len(results),
        "matched": successes,
        "asr": round(successes / len(results), 4) if results else None,
        "results": results,
    }
    (args.workdir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps({"sweep_summary": {k: summary[k] for k in ("behaviors", "matched", "asr")}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
