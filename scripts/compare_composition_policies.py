#!/usr/bin/env python3
"""Controlled comparison of specialist composition policies.

Runs the same deterministic fixture-target GOAT campaign twice through the
same durable engine — once with ``specialist_composition="opinionated"``
(the versioned ``merlin-core-specialists`` v1 template routed at wave 0) and
once with ``specialist_composition="autonomous"`` (the Campaign Lead composes
specialists via typed follow-up proposals) — mirroring the headless packaged
UAT machinery in :mod:`scripts/headless_packaged_uat`: embedded PostgreSQL,
``DurableAgentService``, ``ToolBroker``, the reviewed API target tool and a
scripted stub provider (no live LLM).

The deterministic stub Lead emits no wave-0 composition, so the autonomous
run legitimately executes the structural adversarial-explorer lane only; that
is a valid comparison outcome and is recorded honestly in the summary.

Per run the driver extracts eight metric categories from the durable store
and receipts: validated findings, reproduction/validation receipts (GOAT
gate), evidence quality (evidence refs per finding and the rejected-vs-
admitted specialist ratio), terminal reliability, wall latency, target
request count, token usage and cost (via the existing
``DurableAgentService.schedule_usage`` per-inference accounting).  It emits a
compact JSON + markdown side-by-side summary under the workdir.  There is no
pass/fail gate: this is an informational comparison, not a certification or a
statistical reliability study.

Run commands::

    uv run python scripts/compare_composition_policies.py
    uv run python scripts/compare_composition_policies.py --policy opinionated \
        --workdir target/composition-policy-compare

The lanes are also exercised as a pytest smoke test; see
``packages/merlin-assessments/tests/test_compare_composition_policies.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from merlin_agents import (
    AssignmentBudget,
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
from merlin_agents.campaign import CampaignCompilation, compile_execution_agenda
from merlin_agents.campaign_service import _json_value
from merlin_agents.specialist_templates import (
    template_by_id,
    template_technique_routing,
)
from merlin_assessments import (
    OBJECTIVE,
    FixtureTargetServer,
    ReviewedApiTargetTool,
)
from merlin_contracts import ModelCompleted, ModelUsage, ToolCall
from merlin_storage_postgres import (
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
from merlin_tools import ToolBroker

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKDIR = ROOT / "target" / "composition-policy-compare"

POLICIES = ("opinionated", "autonomous")

METRIC_CATEGORIES = (
    "findings",
    "reproduction",
    "evidence_quality",
    "reliability",
    "latency",
    "target_requests",
    "tokens",
    "cost",
)

# Deterministic per-inference usage the metered stub reports, so both policies
# are metered through the same provider-reported accounting path.
_STUB_USAGE = {
    "input_tokens": 1_200,
    "output_tokens": 300,
    "total_tokens": 1_500,
    "estimated_cost_usd": 0.0045,
}


def _digest(value: object) -> str:
    import hashlib

    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


async def _allow(*_args: object) -> bool:
    return True


class MeteredStubProvider:
    """Scripted attacker provider with provider-reported usage telemetry."""

    capabilities = SimpleNamespace(vision=False, context_window_tokens=32_000)

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.calls = 0
        self.usage_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
        }

    async def stream(self, _request: object):
        index = self.calls
        self.calls += 1
        for key, value in _STUB_USAGE.items():
            self.usage_totals[key] += value
        response = f"reviewed seeded response {index}"
        yield ToolCall(
            call_id=f"seeded-call-{index}",
            name=self.tool_name,
            arguments={
                "input": response,
                "strategy_plan": {
                    "observation": f"reviewed observation {index}",
                    "thought": f"reviewed thought {index}",
                    "strategy": ["hypothetical"],
                    "response": response,
                },
                "strategy_turn_index": index,
            },
        )
        yield ModelUsage(
            input_tokens=_STUB_USAGE["input_tokens"],
            output_tokens=_STUB_USAGE["output_tokens"],
            total_tokens=_STUB_USAGE["total_tokens"],
            estimated_cost_usd=_STUB_USAGE["estimated_cost_usd"],
            uncached_input_tokens=_STUB_USAGE["input_tokens"],
            latency_ms=5,
        )
        yield ModelCompleted(finish_reason="tool_calls")


def _goat_contract(
    *,
    target_id: str,
    adapter_digest: str,
    oracle_digest: str,
    semantic_suffix: str,
):
    from merlin_agents.goat import (
        GOAT_TOOLBOX_CATALOG_DIGEST,
        GoatContract,
        trusted_goat_toolbox,
    )

    return GoatContract(
        version="merlin.goat.v1",
        semantic_technique_id=f"semantic:goat:{semantic_suffix}",
        goal_digest=_digest(OBJECTIVE),
        toolbox=trusted_goat_toolbox(),
        toolbox_catalog_digest=GOAT_TOOLBOX_CATALOG_DIGEST,
        terminal_oracle_digest=oracle_digest,
        reviewed_adapter_digest=adapter_digest,
        attacker_route_digest=_digest(
            {
                "provider_id": "provider",
                "model": "model",
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


def _contract_values(contract) -> dict[str, str]:
    return {
        "version": contract.version,
        "semantic_technique_id": contract.semantic_technique_id,
        "goal_digest": contract.goal_digest,
        "toolbox_json": json.dumps(contract.toolbox, separators=(",", ":")),
        "toolbox_catalog_digest": contract.toolbox_catalog_digest,
        "terminal_oracle_digest": contract.terminal_oracle_digest,
        "reviewed_adapter_digest": contract.reviewed_adapter_digest,
        "attacker_route_digest": contract.attacker_route_digest,
        "attacker_tool_transport": contract.attacker_tool_transport,
        "target_profile_id": contract.target_profile_id,
        "session_policy": contract.session_policy,
        "modality": contract.modality,
        "language": contract.language,
        "target_turns": str(contract.target_turns),
    }


def _campaign(
    *,
    composition: str,
    campaign_id: str,
    target_id: str,
    adapter: Mapping[str, Any],
    contract,
    oracle_digest: str,
    endpoint: str,
    session_id: str,
) -> tuple[CampaignManifest, AssignmentContext, Any, Any, dict[str, Any]]:
    """Compile the seeded campaign under one composition policy.

    Opinionated routes the technique through the versioned team template and
    admits the claiming entry as a wave-0 dynamic specialist, exactly as the
    execute-time merge does.  Autonomous admits nothing at wave 0 (the
    deterministic stub Lead emits no composition), so the structural explorer
    lane owns the technique — the honest baseline outcome.
    """
    reviewed_document = ContextDocument.create(
        "target:reviewed-interaction-contract",
        "Reviewed interaction contract\n"
        + json.dumps(adapter, sort_keys=True, separators=(",", ":")),
    )
    technique_id = f"case:goat:{campaign_id.rsplit('/', 1)[-1]}"
    manifest = CampaignManifest.create(
        id=campaign_id,
        session_id=session_id,
        objective=OBJECTIVE,
        profile=CampaignProfile.RECOMMENDED,
        target=ReviewedTargetAdapter(
            id=target_id,
            interface="http_api",
            endpoint=endpoint,
        ),
        provider_id="provider",
        provider_model="model",
        provider_effort="default",
        authority_ids=("target:reviewed",),
        catalogue=(reviewed_document,),
        skill_documents=(),
        tool_ids=("target_request",),
        budget=AssignmentBudget(max_seconds=60),
        specialist_composition=composition,
    )
    context = AssignmentContext(
        target_profile_id=target_id,
        techniques=(
            AssignmentTechnique(
                id=technique_id,
                objective=OBJECTIVE,
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
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_seconds=60),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        provider_model="model",
    )
    compiled = compile_attack_campaign(manifest, context)
    template = template_by_id("merlin-core-specialists", 1)
    routing = template_technique_routing(template, context.techniques)
    composition_report: dict[str, Any] = {
        "policy": composition,
        "template_id": f"{template.id}@{template.version}",
        "template_entries": [entry.label for entry in template.entries],
        "routing": {key: list(ids) for key, ids in routing.items()},
    }
    worker_profile = None
    if composition == "opinionated" and any(
        profile_id != "adversarial-explorer" for profile_id in routing
    ):
        routed_profile = next(
            profile_id for profile_id in routing if profile_id != "adversarial-explorer"
        )
        entry = next(item for item in template.entries if item.source_profile_id == routed_profile)
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
                label=entry.label,
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
        compiled = CampaignCompilation(
            (*compiled.assignments, record.assignment),
            (
                *compiled.worker_contexts,
                compile_worker_context(manifest, record.assignment, record.profile),
            ),
        )
        worker_profile = record.profile
        admitted = [entry.label]
    elif composition == "opinionated":
        admitted = []
        composition_report["note"] = (
            "technique routed to the structural explorer fallback; no template specialist admitted"
        )
    else:
        admitted = []
        composition_report["note"] = (
            "autonomous run: the deterministic fixture Lead emitted no wave-0 "
            "composition, so the campaign ran its structural lanes only"
        )
    rejected = [item.label for item in template.entries if item.label not in admitted]
    composition_report["admitted_specialists"] = admitted
    composition_report["rejected_specialists"] = rejected
    agenda = compile_execution_agenda(manifest, context, compiled)
    assignment = next(item for item in agenda.assignments if item.strategy_id == "goat")
    assert assignment.comparison_supported is True, assignment.comparison_block_reason
    return manifest, context, assignment, worker_profile, composition_report


async def run_policy_lane(
    workdir: Path, target: FixtureTargetServer, composition: str
) -> dict[str, Any]:
    """One seeded GOAT campaign under a single composition policy."""
    since = len(target.invocations("message"))
    campaign_id = f"campaign/policy-compare-{composition}"
    target_id = f"target-policy-{composition}"
    adapter = target.api_adapter("api")
    oracle_digest = target.oracle_digest()
    contract = _goat_contract(
        target_id=target_id,
        adapter_digest=_digest(adapter),
        oracle_digest=oracle_digest,
        semantic_suffix=f"policy-{composition}",
    )
    provider = MeteredStubProvider("target_request")
    data_dir = workdir / f"postgres-{composition}"
    started = time.monotonic()
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create(
                f"composition policy comparison: {composition}"
            )
            manifest, context, assignment, worker_profile, composition_report = _campaign(
                composition=composition,
                campaign_id=campaign_id,
                target_id=target_id,
                adapter=adapter,
                contract=contract,
                oracle_digest=oracle_digest,
                endpoint=target.origin + "/api/message",
                session_id=str(session.id),
            )
            CampaignStore(connection).prepare(
                campaign_id,
                str(session.id),
                {
                    "manifest": _json_value(asdict(manifest)),
                    "assignment_context": _json_value(asdict(context)),
                },
                manifest.digest,
            )
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
                    model="model",
                    workspace_root=str(workdir),
                    lease_seconds=0.1,
                    campaign_cleanup=api_target.cleanup_assignment,
                )
                live = await service.submit(
                    str(session.id),
                    (assignment,),
                    max_workers=1,
                    minimum_workers=1,
                    dynamic_profiles=(
                        {assignment.id: worker_profile} if worker_profile is not None else None
                    ),
                )
                CampaignStore(connection).add_wave(campaign_id, 0, "initial", live.id)
                assert live.execution is not None
                await asyncio.wait_for(live.execution, timeout=30)
            wall_seconds = time.monotonic() - started

            gate = scheduler.goat_gate(live.id)
            receipt = gate["strategies"].get(assignment.id, {})
            steps = receipt.get("steps", ())
            attempts = scheduler.list_attempts(live.id)
            effects = [
                row
                for attempt in attempts
                for row in ToolEffectRepository(connection).list_for_attempt(str(attempt["id"]))
            ]
            oracle_observations = [
                observation
                for row in effects
                for exchange in (row.result_metadata or {}).get("target_exchanges", ())
                for observation in exchange.get("oracle_observations", ())
            ]
            usage = dict(await service.schedule_usage(live.id))
    validated = len(gate["positive"])
    message_count = len(target.invocations("message")) - since
    return {
        "policy": composition,
        "composition": composition_report,
        "findings": {
            "validated_count": validated,
            "gate_verdict": receipt.get("evidence_verdict"),
            "win_marker_observed": bool(oracle_observations)
            and any(item.get("matched") for item in oracle_observations),
        },
        "reproduction": {
            "gate_receipt_steps": len(steps),
            "gate_receipt_attempt_ids": [str(step.get("scheduler_attempt_id")) for step in steps],
            "acknowledged_effects": sum(1 for row in effects if row.state.name == "ACKNOWLEDGED"),
        },
        "evidence_quality": {
            "oracle_observations": len(oracle_observations),
            "evidence_refs_per_finding": (
                round(len(oracle_observations) / validated, 3) if validated else 0.0
            ),
            "admitted_specialists": len(composition_report["admitted_specialists"]),
            "rejected_specialists": len(composition_report["rejected_specialists"]),
            "rejected_vs_admitted_ratio": (
                round(
                    len(composition_report["rejected_specialists"])
                    / len(composition_report["admitted_specialists"]),
                    3,
                )
                if composition_report["admitted_specialists"]
                else None
            ),
        },
        "reliability": {
            "sample_size": 1,
            "execution_completed": live.execution.done() and live.execution.exception() is None,
            "attempt_count": len(attempts),
            "completed_attempt_count": sum(
                1 for attempt in attempts if attempt["state"] == "completed"
            ),
            "failed_attempt_count": sum(1 for attempt in attempts if attempt["state"] == "failed"),
            "acknowledged_effect_fraction": round(
                sum(1 for row in effects if row.state.name == "ACKNOWLEDGED") / len(effects),
                3,
            )
            if effects
            else None,
        },
        "latency": {
            "wall_seconds": round(wall_seconds, 3),
        },
        "target_requests": {
            "message_count": message_count,
            "effect_count": len(effects),
        },
        "tokens": {
            "input_tokens": provider.usage_totals["input_tokens"],
            "output_tokens": provider.usage_totals["output_tokens"],
            "total_tokens": provider.usage_totals["total_tokens"],
            "inference_count": provider.calls,
            "schedule_usage_projection": {
                "total_tokens": usage["total_tokens"],
                "turns": usage["turns"],
            },
        },
        "cost": {
            "estimated_cost_usd": round(provider.usage_totals["estimated_cost_usd"], 6),
            "schedule_usage_projection": usage.get("estimated_cost_usd"),
        },
    }


def _summary(results: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    per_policy = {
        policy: {key: results[policy][key] for key in METRIC_CATEGORIES} for policy in results
    }
    deltas: dict[str, Any] = {}
    if all(policy in results for policy in POLICIES):
        opinionated, autonomous = (results[policy] for policy in POLICIES)
        deltas = {
            "validated_findings": (
                opinionated["findings"]["validated_count"]
                - autonomous["findings"]["validated_count"]
            ),
            "failed_attempts": (
                opinionated["reliability"]["failed_attempt_count"]
                - autonomous["reliability"]["failed_attempt_count"]
            ),
            "wall_seconds": round(
                opinionated["latency"]["wall_seconds"] - autonomous["latency"]["wall_seconds"],
                3,
            ),
            "message_count": (
                opinionated["target_requests"]["message_count"]
                - autonomous["target_requests"]["message_count"]
            ),
            "total_tokens": (
                opinionated["tokens"]["total_tokens"] - autonomous["tokens"]["total_tokens"]
            ),
            "estimated_cost_usd": round(
                opinionated["cost"]["estimated_cost_usd"]
                - autonomous["cost"]["estimated_cost_usd"],
                6,
            ),
        }
    return {
        "comparison": "specialist_composition policy (opinionated vs autonomous)",
        "gate": None,
        "note": (
            "Informational deterministic fixture comparison, not a certification "
            "or statistical reliability study (one sample per policy); opinionated remains "
            "available as a product policy if autonomous underperforms. Tokens "
            "and cost are provider-reported ModelUsage telemetry metered at the "
            "provider boundary; schedule_usage_projection exposes the engine's "
            "durable usage projection, which the GOAT daemon path does not "
            "currently populate."
        ),
        "limitations": [
            "The scripted provider is deterministic, so this measures orchestration behavior, "
            "not live-model finding quality.",
            "Each policy has one sample and lanes run sequentially; wall latency is descriptive "
            "and may include startup or ordering noise.",
            "The single seeded GOAT fixture cannot establish finding breadth or generalize to "
            "other targets and techniques.",
            "Token and cost values use fixed synthetic provider-reported usage; they validate "
            "accounting and relative work, not actual provider spend.",
        ],
        "metric_categories": list(METRIC_CATEGORIES),
        "policies": per_policy,
        "deltas_opinionated_minus_autonomous": deltas,
    }


def _markdown(summary: Mapping[str, Any]) -> str:
    lines = [
        "# Specialist composition policy comparison",
        "",
        f"_{summary['note']}_",
        "",
        "| metric | opinionated | autonomous |",
        "| --- | --- | --- |",
    ]
    rows = (
        ("validated findings", ("findings", "validated_count")),
        ("gate verdict", ("findings", "gate_verdict")),
        ("reproduction receipt steps", ("reproduction", "gate_receipt_steps")),
        ("execution completed", ("reliability", "execution_completed")),
        ("failed attempts", ("reliability", "failed_attempt_count")),
        ("acknowledged effect fraction", ("reliability", "acknowledged_effect_fraction")),
        ("oracle observations", ("evidence_quality", "oracle_observations")),
        ("evidence refs / finding", ("evidence_quality", "evidence_refs_per_finding")),
        ("admitted specialists", ("evidence_quality", "admitted_specialists")),
        ("rejected specialists", ("evidence_quality", "rejected_specialists")),
        ("wall seconds", ("latency", "wall_seconds")),
        ("target messages", ("target_requests", "message_count")),
        ("inference count", ("tokens", "inference_count")),
        ("total tokens", ("tokens", "total_tokens")),
        ("estimated cost (USD)", ("cost", "estimated_cost_usd")),
    )
    for label, path in rows:
        values = []
        for policy in POLICIES:
            node: Any = summary["policies"].get(policy, {})
            for key in path:
                node = node.get(key) if isinstance(node, Mapping) else None
            values.append("—" if node is None else str(node))
        lines.append(f"| {label} | {values[0]} | {values[1]} |")
    lines += ["", "## Composition outcome", ""]
    for policy in POLICIES:
        composition = summary.get("runs", {}).get(policy, {}).get("composition", {})
        note = composition.get("note")
        admitted = composition.get("admitted_specialists", ())
        lines.append(f"- **{policy}**: admitted={list(admitted)}" + (f" — {note}" if note else ""))
    lines += ["", "## Limitations", ""]
    lines.extend(f"- {limitation}" for limitation in summary["limitations"])
    lines.append("")
    return "\n".join(lines)


async def _run(policies: Sequence[str], workdir: Path) -> dict[str, Any]:
    workdir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    results: dict[str, Any] = {}
    async with FixtureTargetServer() as target:
        for policy in policies:
            lane_workdir = workdir / policy
            lane_workdir.mkdir(parents=True, exist_ok=True)
            result = await run_policy_lane(lane_workdir, target, policy)
            results[policy] = result
            (workdir / f"{policy}.json").write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(f"[{policy}] completed; evidence={workdir / f'{policy}.json'}", flush=True)
    summary = {**_summary(results), "runs": results}
    (workdir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (workdir / "summary.md").write_text(_markdown(summary), encoding="utf-8")
    print(f"summary: {workdir / 'summary.json'}", flush=True)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", choices=(*POLICIES, "all"), default="all")
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    arguments = parser.parse_args(argv)
    policies = POLICIES if arguments.policy == "all" else (arguments.policy,)
    summary = asyncio.run(_run(policies, arguments.workdir))
    print(json.dumps(summary["policies"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
