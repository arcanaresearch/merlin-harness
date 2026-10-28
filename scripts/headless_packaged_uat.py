#!/usr/bin/env python3
"""Headless packaged-campaign UAT driver.

Runs one seeded campaign per execution mode (``api``, ``browser``, ``hybrid``)
against the standalone fixture target (:mod:`scripts.fixture_target_server` /
``merlin_assessments.FixtureTargetServer``) using the same durable service
machinery as production — embedded PostgreSQL, ``DurableAgentService``,
``ToolBroker``, the reviewed API target tool and the compiled browser flow —
with a scripted stub provider (no live LLM), exactly as the acceptance tests
do.  The release binary itself still ships no seeded target; this lane is the
headless stand-in that certifies the packaged campaign machinery end-to-end.

Per lane the driver asserts:

- the selected execution mode actually governs execution: ``api`` runs through
  ``target_request`` (httpx), ``browser`` runs through ``browser_chat_exchange``
  (real Chromium), and ``hybrid`` runs the reviewed API steps plus read-only
  browser checkpoints (``browser_snapshot`` / ``browser_network_events``, never
  ``browser_chat_exchange`` / ``browser_fill``);
- mode lineage lands in the durable evidence (tool effect records, GOAT gate
  receipts, reviewed adapter digests);
- the automatic report export is produced (``<workspace>/target/reports``) and
  inspectable: the exported customer JSON and HTML parse and contain the
  expected sections, including adaptive controllers;
- the ``api`` lane additionally proves cancellation -> restart -> compaction
  replay: a cancelled pending turn resumes from its acknowledged checkpoint
  without provider regeneration or duplicate target effects;
- the ``recon-reuse`` lane runs the seeded scenarios (different bot identity,
  login state and tenant on one site) and proves shared site evidence is
  reused while bot/session/oracle-specific evidence is revalidated, per the
  lesson ledger's scenario-fingerprint rule.

Run commands::

    uv run python scripts/headless_packaged_uat.py --mode all
    uv run python scripts/headless_packaged_uat.py --mode api
    uv run python scripts/headless_packaged_uat.py --mode recon-reuse \
        --workdir target/headless-packaged-uat

Each lane writes its evidence JSON to ``<workdir>/<lane>.json``.  The lanes
are also exercised as pytest checkpoint groups; see
``scripts/acceptance_integration_slice.py`` and
``packages/merlin-assessments/tests/test_headless_packaged_uat.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

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
from merlin_agents.campaign import (
    CampaignCompilation,
    comparable_attempt_dimensions,
    compile_execution_agenda,
    partition_attempt_history,
)
from merlin_agents.campaign_service import _json_value, _validate_reviewed_adapter
from merlin_agents.goat import (
    GOAT_TOOLBOX_CATALOG_DIGEST,
    GoatContract,
    trusted_goat_toolbox,
)
from merlin_assessments import (
    DEFAULT_SCENARIOS,
    OBJECTIVE,
    WIN_MARKER,
    FixtureTargetServer,
    ReviewedApiTargetTool,
)
from merlin_browser import (
    ApiReconHandoff,
    BrowserBounds,
    BrowserMetadata,
    BrowserOwner,
    browser_tools,
    launch_isolated,
)
from merlin_contracts import ModelCompleted, ToolCall, ToolContext
from merlin_findings import DurableReportService
from merlin_storage_postgres import (
    CampaignApiSessionRepository,
    CampaignStore,
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    PostgresBrowserStore,
    PostgresToolEffectStore,
    SessionRepository,
    ToolEffectRepository,
)
from merlin_tools import ToolBroker

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKDIR = ROOT / "target" / "headless-packaged-uat"

LANES = ("api", "api-restart", "browser", "hybrid", "recon-reuse")
UAT_LEASE_SECONDS = 1.0


def _digest(value: object) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


async def _allow(*_args: object) -> bool:
    return True


class StubProvider:
    """Scripted attacker provider: one reviewed tool call per GOAT turn."""

    capabilities = SimpleNamespace(vision=False, context_window_tokens=32_000)

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.calls = 0

    async def stream(self, _request: object):
        index = self.calls
        self.calls += 1
        response = f"reviewed seeded response {index}"
        yield ToolCall(
            call_id=f"seeded-call-{index}",
            name=self.tool_name,
            arguments={
                ("prompt" if self.tool_name == "browser_chat_exchange" else "input"): response,
                "strategy_plan": {
                    "observation": f"reviewed observation {index}",
                    "thought": f"reviewed thought {index}",
                    "strategy": ["hypothetical"],
                    "response": response,
                },
                "strategy_turn_index": index,
            },
        )
        yield ModelCompleted(finish_reason="tool_calls")


def _goat_contract(
    *,
    target_id: str,
    adapter_digest: str,
    oracle_digest: str,
    semantic_suffix: str,
) -> GoatContract:
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


def _contract_values(contract: GoatContract) -> dict[str, str]:
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
    campaign_id: str,
    target_id: str,
    adapter: Mapping[str, Any],
    contract: GoatContract,
    oracle_digest: str,
    tool_ids: tuple[str, ...],
    interface: str,
    endpoint: str,
    session_id: str,
) -> tuple[CampaignManifest, AssignmentContext, Any, Any]:
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
            interface=interface,
            endpoint=endpoint,
            **(
                {
                    "browser_profile": "shared_persistent_authenticated",
                    "browser_concurrency": 1,
                    "session_constraint": "one shared authenticated chat",
                }
                if interface == "browser"
                else {}
            ),
        ),
        provider_id="provider",
        provider_model="model",
        provider_effort="default",
        authority_ids=("target:reviewed",),
        catalogue=(reviewed_document,),
        skill_documents=(),
        tool_ids=tool_ids,
        budget=AssignmentBudget(max_seconds=60),
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
        budget=AssignmentBudget(max_seconds=60),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        provider_model="model",
    )
    compiled = compile_attack_campaign(manifest, context)
    # Mirror the execute-time wave-0 merge: the routed technique is owned by an
    # admitted dynamic specialist, not by a fixed category worker.
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


def _prepare_campaign_store(
    connection, campaign_id: str, session_id: str, manifest, context
) -> None:
    CampaignStore(connection).prepare(
        campaign_id,
        session_id,
        {
            "manifest": _json_value(asdict(manifest)),
            "assignment_context": _json_value(asdict(context)),
        },
        manifest.digest,
    )


async def _export_and_check_report(
    workspace: Path,
    database_url: str,
    campaign_id: str,
    manifest: CampaignManifest,
) -> dict[str, Any]:
    """Run the production report runner and parse the automatic export."""
    import merlin_config
    from merlin_config import Config, CredentialStore
    from merlin_tui.bootstrap import build_backend

    original_load_config = merlin_config.load_config
    original_credential_load = CredentialStore.load
    merlin_config.load_config = lambda: Config(  # noqa: E731
        database_url=database_url,
        values={
            "provider": {"base_url": "http://127.0.0.1:9/v1", "model": "model"},
            "permission_mode": "auto",
        },
    )
    CredentialStore.load = lambda self: {"api_key": "secret"}  # noqa: E731
    backend = build_backend(workspace)
    try:
        await backend._campaigns._finish_campaign(campaign_id, "completed", "capped")  # type: ignore[attr-defined]
        await backend._campaigns._report_runner(campaign_id, manifest, "capped")  # type: ignore[attr-defined]
        revisions = backend._report_repository.list(campaign_id)  # type: ignore[attr-defined]
        assert len(revisions) == 1
        internal = backend._report_repository.get(revisions[-1].revision_id)  # type: ignore[attr-defined]
        customer = json.loads(
            DurableReportService(
                backend._report_repository,  # type: ignore[attr-defined]
                frontier_snapshot_provider=backend._campaigns._store,  # type: ignore[attr-defined]
            ).export(revisions[-1].revision_id, "json", "customer")
        )
    finally:
        merlin_config.load_config = original_load_config
        CredentialStore.load = original_credential_load
        await backend.close()
    expected_sections = {"coverage", "techniques", "lineage", "exchanges", "metadata"}
    missing = expected_sections - set(customer)
    assert not missing, f"customer report is missing sections: {sorted(missing)}"
    controllers = customer.get("metadata", {}).get("adaptive_controllers", ())
    assert controllers, "customer report must record adaptive controllers"
    safe_id = "".join(c if c.isalnum() or c in {"-", "_"} else "-" for c in campaign_id).strip("-")
    html_path = workspace / "target" / "reports" / f"{safe_id}.html"
    assert html_path.is_file(), f"automatic report export missing: {html_path}"
    rendered = html_path.read_text(encoding="utf-8")
    for section in (
        "Assessment in plain language",
        "Findings",
        "What Merlin tested",
        "Evidence transcript",
        "Test-by-test results",
        "Technical details",
    ):
        assert section in rendered, f"exported report is missing section: {section}"
    for internal_token in (
        "Evidence lineage",
        "Target exchanges",
        "evidence_refs",
        "sha256:",
        "session_id",
        "context_id",
        "&quot;",
    ):
        assert internal_token not in rendered, (
            f"human report leaked internal representation: {internal_token}"
        )
    return {
        "report_path": str(html_path),
        "customer_sections": sorted(set(customer)),
        "adaptive_controllers": [str(row.get("controller")) for row in controllers],
        "confirmed": internal.coverage["confirmed"],
        "goat_receipt": next(
            (
                row.get("goat_receipt")
                for row in internal.techniques
                if row.get("strategy_id") == "goat"
            ),
            None,
        ),
    }


def _mode_assertions(
    target: FixtureTargetServer,
    *,
    expect_browser: bool,
    expect_api: bool,
    since: int = 0,
    since_chat: int = 0,
) -> dict[str, Any]:
    # `--mode all` shares one fixture server across lanes, so only consider
    # invocations recorded after this lane's baseline offsets.
    messages = target.invocations("message")[since:]
    assert messages, "fixture target observed no chat messages"
    browser_uas = {entry["user_agent"] for entry in messages if "Mozilla" in entry["user_agent"]}
    api_uas = {entry["user_agent"] for entry in messages if "httpx" in entry["user_agent"]}
    page_loads = target.invocations("chat")[since_chat:]
    if expect_browser:
        assert browser_uas, "browser mode must exchange through the real browser"
        assert not api_uas, "browser mode must not bypass the reviewed browser flow"
        assert page_loads, "browser mode must load the seeded chatbot page"
    if expect_api:
        assert api_uas, "api mode must exchange through the reviewed API adapter"
        assert not browser_uas, "api mode must not drive the browser chat widget"
        assert not page_loads, "api mode must not load the chatbot page"
    return {
        "message_count": len(messages),
        "browser_user_agents": sorted(browser_uas),
        "api_user_agents": sorted(api_uas),
        "page_loads": len(page_loads),
    }


async def run_api_lane(workdir: Path, target: FixtureTargetServer) -> dict[str, Any]:
    """One seeded GOAT campaign in api execution mode."""
    since = len(target.invocations("message"))
    since_chat = len(target.invocations("chat"))
    campaign_id = "campaign/seeded-uat-api"
    target_id = "target-seeded-api"
    adapter = target.api_adapter("api")
    oracle_digest = target.oracle_digest()
    contract = _goat_contract(
        target_id=target_id,
        adapter_digest=_digest(adapter),
        oracle_digest=oracle_digest,
        semantic_suffix="seeded-api",
    )
    provider = StubProvider("target_request")
    data_dir = workdir / "postgres-api"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("seeded UAT api lane")
            manifest, context, assignment, worker_profile = _campaign(
                campaign_id=campaign_id,
                target_id=target_id,
                adapter=adapter,
                contract=contract,
                oracle_digest=oracle_digest,
                tool_ids=("target_request",),
                interface="http_api",
                endpoint=target.origin + "/api/message",
                session_id=str(session.id),
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
                    model="model",
                    workspace_root=str(workdir),
                    lease_seconds=UAT_LEASE_SECONDS,
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
                await asyncio.wait_for(live.execution, timeout=30)

            gate = scheduler.goat_gate(live.id)
            assert gate["ambiguous"] == (assignment.id,), gate
            receipt = gate["strategies"][assignment.id]
            assert receipt["evidence_verdict"] == "ambiguous"
            assert receipt["evaluation_uncertain"] is True
            assert len(receipt["steps"]) == 2
            assert receipt["steps"][-1]["objective_matched"] is True
            effects = [
                row
                for attempt in scheduler.list_attempts(live.id)
                for row in ToolEffectRepository(connection).list_for_attempt(str(attempt["id"]))
            ]
            tool_names = sorted({row.tool_name for row in effects})
            assert tool_names == ["target_request"], tool_names
            mode = _mode_assertions(
                target, expect_browser=False, expect_api=True, since=since, since_chat=since_chat
            )
            assert provider.calls == 2
        report = await _export_and_check_report(
            workdir / "api-workspace", database.url, campaign_id, manifest
        )
    return {
        "lane": "api",
        "execution_mode": "api",
        "ground_truth": target.ground_truth(),
        "mode": mode,
        "effect_tools": tool_names,
        "provider_calls": provider.calls,
        "gate_verdict": receipt["evidence_verdict"],
        "report": report,
    }


class _FaultAfterPendingCheckpointStore(DurableSchedulerStore):
    """Fail once the first pending GOAT turn is durably acknowledged."""

    def __init__(self, connection) -> None:
        super().__init__(connection)
        self.pending_persisted = threading.Event()
        self.release_fault = threading.Event()
        self.faulted_attempt_id: str | None = None

    def commit_partial(
        self,
        attempt_id: str,
        generation: int,
        *,
        result: dict[str, object],
        applied_message_ids=(),
    ) -> str:
        receipt = super().commit_partial(
            attempt_id,
            generation,
            result=result,
            applied_message_ids=applied_message_ids,
        )
        checkpoint = result.get("worker_checkpoint")
        strategy = checkpoint.get("strategy_state") if isinstance(checkpoint, dict) else None
        steps = strategy.get("steps") if isinstance(strategy, dict) else None
        if (
            self.faulted_attempt_id is None
            and isinstance(steps, (list, tuple))
            and len(steps) == 1
            and strategy.get("pending_call_id") == "seeded-call-1"
        ):
            self.faulted_attempt_id = attempt_id
            self.pending_persisted.set()
            assert self.release_fault.wait(timeout=5)
        return receipt


async def run_api_restart_lane(workdir: Path, target: FixtureTargetServer) -> dict[str, Any]:
    """Cancellation -> restart -> compaction replay on the api lane."""
    campaign_id = "campaign/seeded-uat-api-restart"
    target_id = "target-seeded-api-restart"
    adapter = target.api_adapter("api")
    oracle_digest = target.oracle_digest()
    contract = _goat_contract(
        target_id=target_id,
        adapter_digest=_digest(adapter),
        oracle_digest=oracle_digest,
        semantic_suffix="seeded-api-restart",
    )
    provider = StubProvider("target_request")
    baseline_starts = len(target.invocations("start"))
    baseline_cleanups = len(target.invocations("cleanup"))
    baseline_messages = len(target.invocations("message"))
    data_dir = workdir / "postgres-api-restart"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("seeded UAT restart lane")
            manifest, context, assignment, worker_profile = _campaign(
                campaign_id=campaign_id,
                target_id=target_id,
                adapter=adapter,
                contract=contract,
                oracle_digest=oracle_digest,
                tool_ids=("target_request",),
                interface="http_api",
                endpoint=target.origin + "/api/message",
                session_id=str(session.id),
            )
            _prepare_campaign_store(connection, campaign_id, str(session.id), manifest, context)
            scheduler = _FaultAfterPendingCheckpointStore(connection)
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
                    lease_seconds=UAT_LEASE_SECONDS,
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
                persisted = await asyncio.to_thread(scheduler.pending_persisted.wait, 5)
                assert persisted
                first_attempt_id = scheduler.faulted_attempt_id
                assert first_attempt_id is not None
                live.execution.cancel()
                scheduler.release_fault.set()
                await asyncio.gather(live.execution, return_exceptions=True)
                node_id = f"worker:{assignment.id}"
                message_id = str(uuid5(NAMESPACE_URL, f"seeded-uat:{live.id}"))
                message, created = scheduler.send_message_once(
                    message_id,
                    live.id,
                    "campaign-lead",
                    node_id,
                    "merlin.worker-coordination.v1",
                    {"message": "Continue from the acknowledged durable checkpoint."},
                )
                assert created is True
                scheduler.claim_message(first_attempt_id, 1, node_id, str(message.id))
                partial = max(
                    (
                        row
                        for row in scheduler.list_results(live.id)
                        if row["node_id"] == node_id and row["schema_id"] == "schema/partial-result"
                    ),
                    key=lambda row: int(row["generation"]),
                )
                scheduler.commit_partial(
                    first_attempt_id,
                    1,
                    result=dict(partial["payload"]),
                    applied_message_ids=(message_id,),
                )
                connection.execute(
                    """UPDATE merlin_storage.scheduler_attempts
                       SET lease_expires_at = now() - interval '1 second' WHERE id = %s""",
                    (first_attempt_id,),
                )
                connection.commit()
                schedule_id = live.id
                baseline_calls = provider.calls

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            scheduler = DurableSchedulerStore(connection)
            restored = scheduler.restore(schedule_id)
            assert len(restored["mailbox"]) == 1
            assert str(restored["mailbox"][0]["id"]) == message_id
            assert restored["mailbox"][0]["acknowledged_at"] is not None
            _, duplicate_created = scheduler.send_message_once(
                message_id,
                schedule_id,
                "campaign-lead",
                f"worker:{assignment.id}",
                "merlin.worker-coordination.v1",
                {"message": "Continue from the acknowledged durable checkpoint."},
            )
            assert duplicate_created is False, "mailbox compaction must dedupe the replay"
            scheduler.pause_schedule(schedule_id, reason="seeded-uat-restart-boundary")
            async with httpx.AsyncClient() as client:
                api_target = ReviewedApiTargetTool(
                    CampaignApiSessionRepository(connection),
                    lambda _campaign_id: adapter,
                    {},
                    client=client,
                )
                replacement = DurableAgentService(
                    provider,
                    ToolBroker(
                        (api_target,),
                        confirmer=_allow,
                        effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
                    ),
                    scheduler,
                    model="model",
                    workspace_root=str(workdir),
                    lease_seconds=UAT_LEASE_SECONDS,
                    campaign_cleanup=api_target.cleanup_assignment,
                )
                staged = await replacement.reopen(schedule_id, resume_execution=False)
                assert staged.execution is None
                reopened = await replacement.resume_schedule(schedule_id)
                assert reopened.execution is not None
                await asyncio.wait_for(reopened.execution, timeout=45)

            worker_attempts = [
                row
                for row in scheduler.list_attempts(schedule_id)
                if row["node_id"] == f"worker:{assignment.id}"
            ]
            assert len(worker_attempts) == 3
            assert worker_attempts[0]["state"] == "expired"
            assert worker_attempts[1]["state"] == "failed"
            assert worker_attempts[1]["failure_kind"] == "worker_interrupted"
            assert "conflicting target session identities" in str(worker_attempts[1]["error"])
            assert worker_attempts[2]["state"] == "completed"
            assert provider.calls == baseline_calls, "restart must not regenerate provider turns"
            assert len(target.invocations("start")) - baseline_starts == 2
            assert len(target.invocations("cleanup")) - baseline_cleanups == 2
            gate = scheduler.goat_gate(schedule_id)
            assert gate["ambiguous"] == (assignment.id,)
            receipt = gate["strategies"][assignment.id]
            assert receipt["evidence_verdict"] == "ambiguous"
            assert receipt["evaluation_uncertain"] is True
            assert len(receipt["steps"]) == 1
            assert receipt["steps"][-1]["objective_matched"] is True
            assert {str(step["scheduler_attempt_id"]) for step in receipt["steps"]} == {
                first_attempt_id,
            }
    return {
        "lane": "api-restart",
        "execution_mode": "api",
        "ground_truth": target.ground_truth(),
        "provider_calls": provider.calls,
        "provider_calls_after_interruption": baseline_calls,
        "message_count": len(target.invocations("message")) - baseline_messages,
        "gate_verdict": receipt["evidence_verdict"],
        "attempts": [row["state"] for row in worker_attempts],
        "session_conflict_fenced": (
            worker_attempts[1]["failure_kind"] == "worker_interrupted"
            and "conflicting target session identities" in str(worker_attempts[1]["error"])
        ),
        "receipt_attempt_ids": sorted(
            {str(step["scheduler_attempt_id"]) for step in receipt["steps"]}
        ),
        "service_replaced": True,
        "checkpoint_reused": provider.calls == baseline_calls,
        "mailbox_replayed": True,
        "mailbox_deduped": not duplicate_created,
    }


async def run_browser_lane(workdir: Path, target: FixtureTargetServer) -> dict[str, Any]:
    """One seeded GOAT campaign in browser execution mode (real Chromium)."""
    since = len(target.invocations("message"))
    since_chat = len(target.invocations("chat"))
    campaign_id = "campaign/seeded-uat-browser"
    target_id = "target-seeded-browser"
    adapter = target.browser_adapter()
    oracle_digest = target.oracle_digest()
    contract = _goat_contract(
        target_id=target_id,
        adapter_digest=_digest(adapter),
        oracle_digest=oracle_digest,
        semantic_suffix="seeded-browser",
    )
    provider = StubProvider("browser_chat_exchange")
    data_dir = workdir / "postgres-browser"
    browser = None
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("seeded UAT browser lane")
            manifest, context, assignment, worker_profile = _campaign(
                campaign_id=campaign_id,
                target_id=target_id,
                adapter=adapter,
                contract=contract,
                oracle_digest=oracle_digest,
                tool_ids=(
                    "browser_chat_exchange",
                    "browser_snapshot",
                    "browser_release",
                ),
                interface="browser",
                endpoint=target.origin,
                session_id=str(session.id),
            )
            _prepare_campaign_store(connection, campaign_id, str(session.id), manifest, context)
            scheduler = DurableSchedulerStore(connection)
            browser_store = PostgresBrowserStore(connection)
            state_scope_store = CampaignApiSessionRepository(connection)
            owner = BrowserOwner(
                session_id=str(session.id),
                worker_id="worker:seeded-browser",
                technique_id="recon",
            )
            browser = await launch_isolated(
                BrowserBounds(target_id=target_id, allowed_origins={target.origin}),
                owner,
            )
            try:
                await browser.new_tab(target.origin + "/chat")
                await browser_store.save_metadata(browser.metadata(restart=True))
                broker = ToolBroker(
                    browser_tools(
                        lambda _session_id: browser,
                        browser_store,
                        adapter_resolver=lambda _campaign_id: adapter,
                        state_scope_store=state_scope_store,
                    ),
                    confirmer=_allow,
                    effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
                )
                service = DurableAgentService(
                    provider,
                    broker,
                    scheduler,
                    model="model",
                    workspace_root=str(workdir),
                    lease_seconds=30,
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
                await asyncio.wait_for(live.execution, timeout=60)
            finally:
                if browser.metadata().state != "cleaned":
                    await browser.close()
                await browser_store.save_metadata(browser.metadata())

            effects = [
                row
                for attempt in scheduler.list_attempts(live.id)
                for row in ToolEffectRepository(connection).list_for_attempt(str(attempt["id"]))
            ]
            tool_names = sorted({row.tool_name for row in effects})
            assert tool_names == ["browser_chat_exchange"] * 1 and len(effects) == 2, effects
            oracle_observations = [
                observation
                for row in effects
                for exchange in (row.result_metadata or {}).get("target_exchanges", ())
                for observation in exchange.get("oracle_observations", ())
                if observation.get("purpose") == "objective"
            ]
            assert len(oracle_observations) == 2, effects
            exchanges = [
                exchange
                for row in effects
                for exchange in (row.result_metadata or {}).get("target_exchanges", ())
            ]
            assert WIN_MARKER in str(exchanges[-1].get("response")), exchanges[-1]
            assert all(
                observation.get("oracle_digest") == oracle_digest
                and str(observation.get("adapter_digest", "")).startswith("sha256:")
                for observation in oracle_observations
            ), "objective oracle lineage must bind the reviewed adapter and oracle digests"
            assert all(row.state.name == "ACKNOWLEDGED" for row in effects), (
                "browser exchanges must be acknowledged effects"
            )
            worker_results = [
                row
                for row in scheduler.list_results(live.id)
                if row["node_id"] == f"worker:{assignment.id}"
                and row["schema_id"] == "schema/result"
            ]
            assert len(worker_results) == 1
            assert "Daemon completed GOAT after 2 exact acknowledged target turns" in str(
                worker_results[0]["payload"].get("content")
            )
            assert any(observation.get("matched") is True for observation in oracle_observations), (
                "reviewed Browser objective oracle must match the observed target response"
            )
            gate = scheduler.goat_gate(live.id)
            receipt = gate["strategies"].get(assignment.id)
            assert isinstance(receipt, dict), gate
            assert receipt.get("execution_complete") is True, receipt
            assert len(receipt.get("steps", ())) == 2, receipt
            verdict = receipt.get("evidence_verdict")
            assert verdict == "ambiguous", receipt
            assert receipt.get("evaluation_uncertain") is True, receipt
            mode = _mode_assertions(
                target, expect_browser=True, expect_api=False, since=since, since_chat=since_chat
            )
            observations = await browser_store.list_observations(browser.context_id)
            assert observations, "browser checkpoints must be recorded as evidence"
        report = await _export_and_check_report(
            workdir / "browser-workspace", database.url, campaign_id, manifest
        )
    return {
        "lane": "browser",
        "execution_mode": "browser",
        "ground_truth": target.ground_truth(),
        "mode": mode,
        "effect_tools": tool_names,
        "provider_calls": provider.calls,
        "gate_verdict": verdict,
        "oracle_matched": any(
            observation.get("matched") is True for observation in oracle_observations
        ),
        "objective_oracle_observations": len(oracle_observations),
        "win_marker_observed": True,
        "observations": len(observations),
        "acknowledged_effects": sum(row.state.name == "ACKNOWLEDGED" for row in effects),
        "report": report,
    }


async def run_hybrid_lane(workdir: Path, target: FixtureTargetServer) -> dict[str, Any]:
    """Hybrid mode: reviewed API steps plus read-only browser checkpoints."""
    campaign_id = "campaign/seeded-uat-hybrid"
    target_id = "target-seeded-hybrid"
    adapter = target.api_adapter("hybrid")
    validation_view = json.loads(json.dumps(adapter))
    for step in validation_view["api_steps"]:
        predicate = step.get("oracle_predicate")
        if isinstance(predicate, dict):
            predicate.pop("purpose", None)
    assert _validate_reviewed_adapter(validation_view) is None
    oracle_digest = target.oracle_digest()
    contract = _goat_contract(
        target_id=target_id,
        adapter_digest=_digest(adapter),
        oracle_digest=oracle_digest,
        semantic_suffix="seeded-hybrid",
    )
    provider = StubProvider("target_request")
    data_dir = workdir / "postgres-hybrid"
    browser = None
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("seeded UAT hybrid lane")
            manifest, context, assignment, worker_profile = _campaign(
                campaign_id=campaign_id,
                target_id=target_id,
                adapter=adapter,
                contract=contract,
                oracle_digest=oracle_digest,
                tool_ids=("target_request",),
                interface="http_api",
                endpoint=target.origin + "/api/message",
                session_id=str(session.id),
            )
            _prepare_campaign_store(connection, campaign_id, str(session.id), manifest, context)
            scheduler = DurableSchedulerStore(connection)
            browser_store = PostgresBrowserStore(connection)
            state_scope_store = CampaignApiSessionRepository(connection)
            owner = BrowserOwner(
                session_id=str(session.id),
                worker_id="worker:seeded-hybrid",
                technique_id="recon",
            )
            browser = await launch_isolated(
                BrowserBounds(target_id=target_id, allowed_origins={target.origin}),
                owner,
            )
            try:
                tab = await browser.new_tab(target.origin + "/chat")
                await browser_store.save_metadata(browser.metadata(restart=True))
                async with httpx.AsyncClient() as client:
                    api_target = ReviewedApiTargetTool(
                        state_scope_store,
                        lambda _campaign_id: adapter,
                        {},
                        browser_api_requester=(
                            lambda _campaign_id, method, url, payload, headers: (
                                browser.request_json(method, url, payload, headers)
                            )
                        ),
                        client=client,
                    )
                    broker = ToolBroker(
                        (
                            api_target,
                            *browser_tools(
                                lambda _session_id: browser,
                                browser_store,
                                state_scope_store=state_scope_store,
                            ),
                        ),
                        confirmer=_allow,
                        effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
                    )
                    service = DurableAgentService(
                        provider,
                        broker,
                        scheduler,
                        model="model",
                        workspace_root=str(workdir),
                        lease_seconds=30,
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
                    await asyncio.wait_for(live.execution, timeout=30)
                    checkpoint = await broker.invoke(
                        "browser_snapshot",
                        ToolContext(
                            session_id=str(session.id),
                            workspace_root=str(workdir),
                            call_id="hybrid-checkpoint-1",
                            agent_id=owner.worker_id,
                        ),
                        {"tab_ref": tab, "step": 1},
                    )
                    assert not checkpoint.is_error, checkpoint.content
            finally:
                if browser.metadata().state != "cleaned":
                    await browser.close()
                await browser_store.save_metadata(browser.metadata())

            gate = scheduler.goat_gate(live.id)
            assert gate["ambiguous"] == (assignment.id,), gate
            receipt = gate["strategies"][assignment.id]
            assert receipt["evidence_verdict"] == "ambiguous"
            assert receipt["evaluation_uncertain"] is True
            assert receipt["steps"][-1]["objective_matched"] is True
            effects = [
                row
                for attempt in scheduler.list_attempts(live.id)
                for row in ToolEffectRepository(connection).list_for_attempt(str(attempt["id"]))
            ]
            tool_names = sorted({row.tool_name for row in effects})
            assert tool_names == ["target_request"], tool_names
            actions = await browser_store.list_actions(browser.context_id)
            mutation_kinds = {"fill", "click", "press", "navigate"} & {
                item.kind for item in actions
            }
            assert not mutation_kinds, f"hybrid checkpoints must be read-only: {mutation_kinds}"
            messages = target.invocations("message")
            assert messages, "hybrid mode must exchange through the reviewed API steps"
            assert target.invocations("chat"), "hybrid mode must load the page for checkpoints"
            mode = {
                "message_count": len(messages),
                "page_loads": len(target.invocations("chat")),
                "api_transport": "browser_context",
            }
            observations = await browser_store.list_observations(browser.context_id)
            assert observations, "hybrid read-only checkpoints must be recorded"
        report = await _export_and_check_report(
            workdir / "hybrid-workspace", database.url, campaign_id, manifest
        )
    return {
        "lane": "hybrid",
        "execution_mode": "hybrid",
        "ground_truth": target.ground_truth(),
        "mode": mode,
        "effect_tools": tool_names,
        "provider_calls": provider.calls,
        "checkpoint_actions": [item.kind for item in actions],
        "observations": len(observations),
        "gate_verdict": receipt["evidence_verdict"],
        "report": report,
    }


def _scenario_target(target: FixtureTargetServer, scenario) -> ReviewedTargetAdapter:
    return ReviewedTargetAdapter(
        id="target-seeded-recon",
        interface="browser",
        endpoint=f"{target.origin}/tenants/{scenario.tenant}/chat",
        model=scenario.bot,
        authentication=scenario.login_token or "anonymous",
        session_constraint="one conversation",
    )


async def run_recon_reuse_lane(workdir: Path, target: FixtureTargetServer) -> dict[str, Any]:
    """Shared site evidence reused; bot/session/oracle evidence revalidated."""
    data_dir = workdir / "postgres-recon-reuse"
    scenario_results: list[dict[str, Any]] = []
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session_id = str(SessionRepository(connection).create("seeded UAT recon reuse").id)
            store = PostgresBrowserStore(connection)
            target_id = "target-seeded-recon"
            shared_site_facts = json.dumps(
                {
                    "entry_url": target.origin + "/chat",
                    "page_digest": target.ground_truth()["page_digest"],
                    "api_contract_digest": target.ground_truth()["api_lane_adapter_digest"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            owner = BrowserOwner(
                session_id=session_id,
                worker_id="worker:seeded-recon",
                technique_id="recon",
            )
            history: list[dict[str, object]] = []
            baseline = DEFAULT_SCENARIOS[0]
            baseline_dimensions: dict[str, str] | None = None
            for scenario in DEFAULT_SCENARIOS:
                dimensions = comparable_attempt_dimensions(
                    _scenario_target(target, scenario),
                    (),
                    f"case:recon:{scenario.name}",
                    OBJECTIVE,
                    strategy_id="goat",
                    domain_id="prompt-injection",
                )
                if baseline_dimensions is None:
                    baseline_dimensions = dimensions
                    history.append(
                        {
                            "technique_id": f"case:recon:{scenario.name}",
                            "scenario_fingerprint": dimensions["scenario_fingerprint"],
                            "outcome": "finding",
                            "evidence_digest": dimensions["attempt_identity"],
                        }
                    )
                context_id = f"context-{scenario.name}"
                now = datetime.now(UTC)
                await store.save_metadata(
                    BrowserMetadata(
                        context_id=context_id,
                        mode="isolated",
                        target_id=target_id,
                        owner=owner,
                        allowed_origins=frozenset({target.origin}),
                        tabs=(),
                        state="active",
                        created_at=now,
                        updated_at=now,
                    )
                )
                request_ref = f"evidence:{scenario.name}:request"
                response_ref = f"evidence:{scenario.name}:response"
                oracle_ref = f"evidence:{scenario.name}:oracle"
                for ref, kind, payload in (
                    (request_ref, "network_request", {"method": "POST"}),
                    (response_ref, "network_response", {"status": 200}),
                    (oracle_ref, "oracle_observation", {"verdict": "positive"}),
                ):
                    await store.record_evidence(context_id, ref, kind, payload)
                handoff = ApiReconHandoff(
                    id=f"handoff:{scenario.name}",
                    context_id=context_id,
                    target_id=target_id,
                    owner=owner,
                    method="POST",
                    url=f"{target.origin}/api/message",
                    request_evidence_ref=request_ref,
                    response_evidence_ref=response_ref,
                    adapter='{"interface":"http_api"}',
                    reusable_adapter='{"interface":"http_api"}',
                    reusable_site_facts=shared_site_facts,
                    recon_scope=scenario.recon_scope,
                )
                await store.record_handoff(handoff)
                comparable, analogical = partition_attempt_history(
                    tuple(history), dimensions["scenario_fingerprint"]
                )
                own_verified = await store.verify_handoff_evidence(
                    handoff.id, target_id, (request_ref, response_ref, oracle_ref)
                )
                other = next(item for item in DEFAULT_SCENARIOS if item is not scenario)
                cross_verified = await store.verify_handoff_evidence(
                    handoff.id, target_id, (request_ref, f"evidence:{other.name}:response")
                )
                reused = dimensions["scenario_fingerprint"] == (baseline_dimensions or {}).get(
                    "scenario_fingerprint"
                )
                assert own_verified, f"{scenario.name}: own bot/session/oracle evidence must verify"
                assert not cross_verified, (
                    f"{scenario.name}: another scenario's session evidence must not verify"
                )
                if scenario is baseline:
                    assert comparable, "baseline scenario reuses the shared site evidence"
                    assert not analogical
                else:
                    assert not comparable, (
                        f"{scenario.name}: drifted bot/login/tenant evidence must revalidate"
                    )
                    assert analogical, f"{scenario.name}: prior evidence is analogy-only"
                scenario_results.append(
                    {
                        "scenario": scenario.name,
                        "bot": scenario.bot,
                        "tenant": scenario.tenant,
                        "login": scenario.login_token or "anonymous",
                        "scenario_fingerprint": dimensions["scenario_fingerprint"],
                        "site_evidence_reused": reused,
                        "revalidation_required": not reused,
                        "own_evidence_verified": own_verified,
                        "cross_scenario_evidence_rejected": not cross_verified,
                    }
                )
            handoffs = await store.list_handoffs(target_id)
            assert len(handoffs) == len(DEFAULT_SCENARIOS)
            assert {item.reusable_site_facts for item in handoffs} == {shared_site_facts}, (
                "shared site facts must be reused verbatim across scenarios"
            )
            assert len({item.recon_scope["agent_or_bot"] for item in handoffs}) > 1
            assert len({item.recon_scope["tenant"] for item in handoffs}) > 1
            assert len({item.recon_scope["authentication_context"] for item in handoffs}) > 1
    return {
        "lane": "recon-reuse",
        "ground_truth": target.ground_truth(),
        "shared_site_facts_digest": _digest(shared_site_facts),
        "scenarios": scenario_results,
    }


LANE_RUNNERS = {
    "api": run_api_lane,
    "api-restart": run_api_restart_lane,
    "browser": run_browser_lane,
    "hybrid": run_hybrid_lane,
    "recon-reuse": run_recon_reuse_lane,
}


async def _run(modes: Sequence[str], workdir: Path) -> dict[str, Any]:
    workdir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    evidence: dict[str, Any] = {}
    async with FixtureTargetServer(match_non_recon_message=True) as target:
        for mode in modes:
            lane_workdir = workdir / mode
            lane_workdir.mkdir(parents=True, exist_ok=True)
            result = await LANE_RUNNERS[mode](lane_workdir, target)
            evidence[mode] = result
            (workdir / f"{mode}.json").write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(f"[{mode}] passed; evidence={workdir / f'{mode}.json'}", flush=True)
    return evidence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=(*LANES, "all"), default="all")
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    arguments = parser.parse_args(argv)
    modes = LANES if arguments.mode == "all" else (arguments.mode,)
    evidence = asyncio.run(_run(modes, arguments.workdir))
    print(json.dumps({mode: sorted(result) for mode, result in evidence.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
