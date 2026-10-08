#!/usr/bin/env python3
"""Live-provider single-technique campaign driver.

A live-LLM variant of the ``api`` lane in
:mod:`scripts.headless_packaged_uat`: it compiles the exact same
one-technique GOAT campaign (same manifest, dynamic-specialist merge and
durable-service machinery) against the fixture target server, but swaps the
scripted ``StubProvider`` for the production
:class:`merlin_providers.OpenAICompatibleProvider` built from environment
configuration.

Prerequisites (run from the repo root)::

    uv run python scripts/fixture_target_server.py --port 8377 &

Environment (never hard-coded here)::

    MERLIN_PROVIDER_BASE_URL   e.g. https://api.deepseek.com/v1
    MERLIN_PROVIDER_MODEL      e.g. deepseek-v4-flash
    MERLIN_CREDENTIAL_API_KEY  provider API key
    MERLIN_PROVIDER_OUTPUT_TOKEN_PARAMETER  e.g. max_tokens (Deepseek)

Run command::

    uv run python scripts/live_small_campaign.py \
        --target-origin http://127.0.0.1:8377 \
        --workdir target/live-small-campaign

The driver prints one JSON evidence object: campaign completion, the GOAT
gate receipt, durable tool effects (target exchanges), per-call provider
latency and streamed token-usage receipts, and the terminal worker states.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from headless_packaged_uat import (  # noqa: E402
    _allow,
    _contract_values,
    _digest,
    _goat_contract,
    _prepare_campaign_store,
)
from merlin_agents import (  # noqa: E402
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
from merlin_agents.campaign import (  # noqa: E402
    CampaignCompilation,
    compile_execution_agenda,
)
from merlin_agents.goat import GoatContract  # noqa: E402
from merlin_assessments import (  # noqa: E402
    OBJECTIVE,
    FixtureTargetServer,
    ReviewedApiTargetTool,
)
from merlin_config import ConfigPaths, CredentialStore, load_config  # noqa: E402
from merlin_contracts import ModelUsage  # noqa: E402
from merlin_providers import (  # noqa: E402
    AnthropicMessagesConfig,
    AnthropicMessagesProvider,
    GeminiConfig,
    GeminiProvider,
    OpenAICompatibleProvider,
    OpenAIResponsesConfig,
    OpenAIResponsesProvider,
    ProviderConfig,
)
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

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKDIR = ROOT / "target" / "live-small-campaign"


class RemoteFixture(FixtureTargetServer):
    """Client-side view of the fixture server running in another process."""

    def __init__(self, origin: str) -> None:
        super().__init__()
        self._origin = origin.rstrip("/")

    @property
    def origin(self) -> str:  # noqa: D102
        return self._origin


class RecordingProvider:
    """Wraps the live provider: counts calls, latency, and usage receipts."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def stream(self, request: object):
        started = time.monotonic()
        usage: list[dict[str, Any]] = []
        event_log: list[dict[str, Any]] = []
        error: str | None = None
        try:
            async for event in self._inner.stream(request):
                summary = str(event)
                event_log.append(
                    {
                        "type": type(event).__name__,
                        "summary": summary[:500],
                    }
                )
                if isinstance(event, ModelUsage):
                    usage.append(asdict(event) if is_dataclass(event) else dict(vars(event)))
                yield event
        except Exception as exc:  # record and re-raise: receipts matter even on failure
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.calls.append(
                {
                    "latency_seconds": round(time.monotonic() - started, 3),
                    "events": event_log,
                    "usage": usage,
                    "error": error,
                }
            )


_NATIVE_TRANSPORTS = ("openai_compatible", "anthropic_messages", "openai_responses", "gemini")

# Transport-specific API-key env vars (keys never travel on the command line).
_KEY_ENV = {
    "anthropic_messages": "MERLIN_ANTHROPIC_API_KEY",
    "openai_responses": "MERLIN_OPENAI_API_KEY",
    "gemini": "MERLIN_GEMINI_API_KEY",
}

_DEFAULT_BASE_URLS = {
    "anthropic_messages": "https://api.anthropic.com/v1",
    "openai_responses": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}


def _native_provider(
    transport: str, base_url: str, api_key: str, model: str
) -> OpenAICompatibleProvider:
    common: dict[str, Any] = {
        "timeout": None,
        "connect_timeout": 10.0,
        "read_timeout": 180.0,
        "total_timeout": 180.0,
        "max_tokens": None,
        "default_max_output_tokens": 4096,
    }
    if transport == "anthropic_messages":
        return AnthropicMessagesProvider(
            AnthropicMessagesConfig(base_url, api_key, model, **common)
        )
    if transport == "openai_responses":
        return OpenAIResponsesProvider(OpenAIResponsesConfig(base_url, api_key, model, **common))
    if transport == "gemini":
        return GeminiProvider(GeminiConfig(base_url, api_key, model, **common))
    raise ValueError(f"unknown native transport: {transport}")


def _live_provider(transport: str = "openai_compatible") -> tuple[RecordingProvider, str]:
    if transport != "openai_compatible":
        model = os.environ.get("MERLIN_PROVIDER_MODEL", "").strip()
        if not model:
            raise ValueError("provider model unavailable; set MERLIN_PROVIDER_MODEL")
        key_env = _KEY_ENV[transport]
        api_key = os.environ.get(key_env, "").strip()
        if not api_key:
            raise ValueError(f"provider API key unavailable; set {key_env}")
        base_url = (
            os.environ.get("MERLIN_PROVIDER_BASE_URL", "").strip() or _DEFAULT_BASE_URLS[transport]
        )
        return RecordingProvider(_native_provider(transport, base_url, api_key, model)), model
    config = load_config()
    values = config.values.get("provider")
    if not isinstance(values, dict) or not isinstance(values.get("base_url"), str):
        raise ValueError("provider base_url unavailable; set MERLIN_PROVIDER_BASE_URL")
    model = values.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("provider model unavailable; set MERLIN_PROVIDER_MODEL")
    api_key = CredentialStore(ConfigPaths.default()).load().get("api_key", "")
    if not api_key:
        raise ValueError("provider API key unavailable; set MERLIN_CREDENTIAL_API_KEY")
    inner = OpenAICompatibleProvider(
        ProviderConfig(
            values["base_url"],
            api_key,
            model,
            timeout=None,
            connect_timeout=10.0,
            read_timeout=180.0,
            total_timeout=180.0,
            max_tokens=None,
            default_max_output_tokens=4096,
            # Deepseek speaks the classic chat-completions knob, not the
            # newer max_completion_tokens default; select it via
            # MERLIN_PROVIDER_OUTPUT_TOKEN_PARAMETER.
            output_token_parameter=config.provider_runtime.output_token_parameter,
        )
    )
    return RecordingProvider(inner), model


def _goat_contract_live(
    *,
    target_id: str,
    adapter_digest: str,
    oracle_digest: str,
    semantic_suffix: str,
    provider_model: str,
) -> GoatContract:
    """UAT ``_goat_contract`` with the real routed model in the route digest."""
    contract = _goat_contract(
        target_id=target_id,
        adapter_digest=adapter_digest,
        oracle_digest=oracle_digest,
        semantic_suffix=semantic_suffix,
    )
    values = dict(_contract_values(contract))
    values["attacker_route_digest"] = _digest(
        {
            "provider_id": "provider",
            "model": provider_model,
            "role": "worker",
            "tool_transport": "buffered",
        }
    )
    return GoatContract(
        version=values["version"],
        semantic_technique_id=values["semantic_technique_id"],
        goal_digest=values["goal_digest"],
        toolbox=contract.toolbox,
        toolbox_catalog_digest=values["toolbox_catalog_digest"],
        terminal_oracle_digest=values["terminal_oracle_digest"],
        reviewed_adapter_digest=values["reviewed_adapter_digest"],
        attacker_route_digest=values["attacker_route_digest"],
        attacker_tool_transport=values["attacker_tool_transport"],
        target_profile_id=values["target_profile_id"],
        session_policy=values["session_policy"],
        modality=values["modality"],
        language=values["language"],
        target_turns=int(values["target_turns"]),
    )


BUDGET = AssignmentBudget(max_turns=8, max_seconds=300)


def _campaign(
    *,
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
    """Copy of the UAT ``_campaign`` with live-run budgets (max_turns=8, max_seconds=300)."""
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


async def run_live_lane(
    workdir: Path, origin: str, transport: str = "openai_compatible"
) -> dict[str, Any]:
    """One GOAT technique against the fixture target with a real LLM."""
    target = RemoteFixture(origin)
    campaign_id = "campaign/live-small-api"
    target_id = "target-live-small-api"
    adapter = target.api_adapter("api")
    oracle_digest = target.oracle_digest()
    provider, model = _live_provider(transport)
    contract = _goat_contract_live(
        target_id=target_id,
        adapter_digest=_digest(adapter),
        oracle_digest=oracle_digest,
        semantic_suffix="live-small-api",
        provider_model=model,
    )
    data_dir = workdir / "postgres-live"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("live small campaign lane")
            manifest, context, assignment, worker_profile = _campaign(
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
            effects = [
                row
                for attempt in attempts
                for row in ToolEffectRepository(connection).list_for_attempt(str(attempt["id"]))
            ]
            exchanges = [
                exchange
                for row in effects
                for exchange in (row.result_metadata or {}).get("target_exchanges", ())
            ]
            worker_results = [
                row
                for row in scheduler.list_results(live.id)
                if row["node_id"] == f"worker:{assignment.id}"
                and row["schema_id"] == "schema/result"
            ]
    return {
        "lane": "live-small-api",
        "transport": transport,
        "campaign_id": campaign_id,
        "technique_id": assignment.id,
        "strategy_id": assignment.strategy_id,
        "terminal_attempt_states": [str(row["state"]) for row in attempts],
        "terminal_attempt_errors": [str(row["error"]) for row in attempts if row.get("error")],
        "gate": gate,
        "effect_tools": [row.tool_name for row in effects],
        "target_exchanges": exchanges,
        "worker_results": [
            {"content": str(row["payload"].get("content")), "usage": row["payload"].get("usage")}
            for row in worker_results
        ],
        "provider_calls": provider.calls,
        "ground_truth": target.ground_truth(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-origin", default=os.environ.get("MERLIN_TARGET_ORIGIN", ""))
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    parser.add_argument(
        "--transport",
        choices=_NATIVE_TRANSPORTS,
        default=os.environ.get("MERLIN_PROVIDER_TRANSPORT", "openai_compatible"),
    )
    arguments = parser.parse_args(argv)
    if not arguments.target_origin:
        raise SystemExit("--target-origin (or MERLIN_TARGET_ORIGIN) is required")
    arguments.workdir.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(
        run_live_lane(arguments.workdir, arguments.target_origin, arguments.transport)
    )
    evidence_path = arguments.workdir / "live-small-api.json"
    evidence_path.write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    print(f"evidence={evidence_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
