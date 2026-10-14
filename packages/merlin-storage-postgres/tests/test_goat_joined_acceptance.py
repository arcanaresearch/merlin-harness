import asyncio
import json
import threading
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from merlin_agents import (
    AssignmentBudget,
    AssignmentContext,
    AssignmentTechnique,
    CampaignManifest,
    CampaignProfile,
    ContextDocument,
    DurableAgentService,
    ReviewedTargetAdapter,
    WorkerAssignment,
    compile_attack_campaign,
)
from merlin_agents.campaign import compile_execution_agenda
from merlin_agents.campaign_service import _json_value
from merlin_agents.goat import (
    GOAT_TOOLBOX_CATALOG_DIGEST,
    GoatContract,
    trusted_goat_toolbox,
)
from merlin_assessments import ReviewedApiTargetTool
from merlin_config import Config, CredentialStore
from merlin_contracts import (
    EXECUTABLE_ADAPTER_IDENTITY_VERSION,
    ModelCompleted,
    ToolCall,
    ToolEffectRecord,
    ToolEffectState,
    executable_adapter_digest,
)
from merlin_findings import DurableReportService
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
from merlin_tui.bootstrap import build_backend


def _digest(value: object) -> str:
    return (
        "sha256:"
        + sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )


@pytest.mark.asyncio
async def test_joined_goat_pending_turn_survives_restart_without_regeneration_and_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the compiled GOAT path across its hardest durable boundary."""

    class Provider:
        capabilities = SimpleNamespace(vision=False, context_window_tokens=32_000)

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, _request):
            self.calls += 1
            index = self.calls - 1
            response = f"reviewed response {index}"
            yield ToolCall(
                call_id=f"goat-call-{index}",
                name="target_request",
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
            yield ModelCompleted(finish_reason="tool_calls")

    class FaultAfterPendingCheckpointStore(DurableSchedulerStore):
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
                and strategy.get("pending_call_id") == "goat-call-1"
            ):
                effects = ToolEffectRepository(self.connection).list_for_attempt(attempt_id)
                assert len(effects) == 1
                assert effects[0].state is ToolEffectState.ACKNOWLEDGED
                self.faulted_attempt_id = attempt_id
                self.pending_persisted.set()
                assert self.release_fault.wait(timeout=5)
            return receipt

    async def allow(*_args) -> bool:
        return True

    data_dir = tmp_path / "postgres-joined-goat"
    campaign_id = "campaign/joined-goat"
    target_id = "target-reviewed-api"
    technique_id = "case:goat:joined"
    objective = "Execute the reviewed fixed-K GOAT objective"
    oracle_digest = _digest("joined-goat-objective-oracle")
    http_invocations: list[str] = []

    async def reviewed_target(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = await reader.readline()
        _method, raw_path, _version = request_line.decode().strip().split(" ", 2)
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in {b"\r\n", b""}:
                break
            name, value = line.decode().split(":", 1)
            headers[name.lower()] = value.strip()
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        if body:
            json.loads(body)
        operation = raw_path.rstrip("/").rsplit("/", 1)[-1]
        http_invocations.append(operation)
        if operation == "start":
            payload = {"sessionId": "target-session-one"}
        elif operation == "message":
            turn = http_invocations.count("message")
            payload = {"reply": f"target reply {turn}", "matched": turn == 2}
        else:
            payload = {"closed": True}
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(encoded)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + encoded
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    target_server = await asyncio.start_server(reviewed_target, "127.0.0.1", 0)
    target_port = int(target_server.sockets[0].getsockname()[1])
    target_origin = f"http://127.0.0.1:{target_port}/api/"
    adapter: dict[str, object] = {
        "adapter_identity_version": EXECUTABLE_ADAPTER_IDENTITY_VERSION,
        "interface": "http_api",
        "endpoint": target_origin,
        "api_steps": [
            {
                "step_id": "start",
                "endpoint": "start",
                "method": "POST",
                "request_template": {},
                "response_selector": "/sessionId",
                "expected_status": 200,
                "success_witness": "session",
                "cleanup": False,
                "interaction": False,
                "captures": {"session_id": "/sessionId"},
                "repeat": 1,
                "idempotency_key": "start-joined-goat",
                "replay_after_ambiguous_result": True,
            },
            {
                "step_id": "interaction",
                "endpoint": "message",
                "method": "POST",
                "request_template": {
                    "sessionId": "{{capture:session_id}}",
                    "message": "{{input}}",
                },
                "response_selector": "/reply",
                "expected_status": 200,
                "success_witness": "reply",
                "cleanup": False,
                "interaction": True,
                "captures": {},
                "repeat": 1,
                "idempotency_key": None,
                "replay_after_ambiguous_result": False,
                "oracle_predicate": {
                    "purpose": "objective",
                    "oracle_digest": oracle_digest,
                    "pointer": "/matched",
                    "match_values": [True],
                    "modality": "text",
                    "turn": "multi",
                    "language": "en",
                },
            },
            {
                "step_id": "cleanup",
                "endpoint": "cleanup",
                "method": "POST",
                "request_template": {"sessionId": "{{capture:session_id}}"},
                "response_selector": "/closed",
                "expected_status": 200,
                "success_witness": "closed",
                "cleanup_acknowledgement": {
                    "pointer": "/closed",
                    "match_values": [True],
                    "status_codes": [200],
                },
                "cleanup": True,
                "interaction": False,
                "captures": {},
                "repeat": 1,
                "idempotency_key": "cleanup-joined-goat",
                "replay_after_ambiguous_result": True,
            },
        ],
    }
    adapter_digest = executable_adapter_digest(adapter)
    route_digest = _digest(
        {
            "provider_id": "provider",
            "model": "model",
            "role": "worker",
            "tool_transport": "buffered",
        }
    )
    contract = GoatContract(
        version="merlin.goat.v1",
        semantic_technique_id="semantic:goat:joined",
        goal_digest=_digest(objective),
        toolbox=trusted_goat_toolbox(),
        toolbox_catalog_digest=GOAT_TOOLBOX_CATALOG_DIGEST,
        terminal_oracle_digest=oracle_digest,
        reviewed_adapter_digest=adapter_digest,
        attacker_route_digest=route_digest,
        attacker_tool_transport="buffered",
        target_profile_id=target_id,
        session_policy="same-reviewed-session",
        modality="text",
        language="en",
        target_turns=2,
    )
    contract_values = {
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
    reviewed_document = ContextDocument.create(
        "target:reviewed-interaction-contract",
        "Reviewed interaction contract\n"
        + json.dumps(adapter, sort_keys=True, separators=(",", ":")),
    )
    goat_guidance = ContextDocument.create("attack-catalogue:goat", "Reviewed GOAT catalogue entry")
    manifest = CampaignManifest.create(
        id=campaign_id,
        session_id="placeholder",
        objective=objective,
        profile=CampaignProfile.RECOMMENDED,
        target=ReviewedTargetAdapter(
            id=target_id,
            interface="http_api",
            endpoint=target_origin + "message",
        ),
        provider_id="provider",
        provider_model="model",
        provider_effort="default",
        authority_ids=("target:reviewed",),
        catalogue=(reviewed_document, goat_guidance),
        skill_documents=(),
        tool_ids=("target_request",),
        budget=AssignmentBudget(max_seconds=30),
        specialist_composition="autonomous",
    )
    context = AssignmentContext(
        target_profile_id=target_id,
        techniques=(
            AssignmentTechnique(
                id=technique_id,
                objective=objective,
                tags=frozenset({"prompt-injection", "text", "multi", "conversational"}),
                guidance_ids=("attack-catalogue:goat",),
                semantic_id=contract.semantic_technique_id,
                language="en",
                domain_id="prompt-injection",
                oracle_digest=oracle_digest,
                strategy_id="goat",
                strategy_contract=tuple(sorted(contract_values.items())),
            ),
        ),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_seconds=30),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        provider_model="model",
    )
    compiled = compile_attack_campaign(manifest, context)
    agenda = compile_execution_agenda(manifest, context, compiled)
    assert len(agenda.assignments) == 2
    assignment = next(item for item in agenda.assignments if item.strategy_id == "goat")
    assert assignment.strategy_id == "goat"
    assert assignment.comparison_supported is True
    assignment_id = assignment.id
    provider = Provider()
    report_manifest: CampaignManifest | None = None
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("joined GOAT restart")
            report_manifest = CampaignManifest.create(
                id=manifest.id,
                session_id=str(session.id),
                objective=manifest.objective,
                profile=manifest.profile,
                target=manifest.target,
                provider_id=manifest.provider_id,
                provider_model=manifest.provider_model,
                provider_effort=manifest.provider_effort,
                authority_ids=manifest.authority_ids,
                catalogue=manifest.catalogue,
                skill_documents=manifest.skill_documents,
                tool_ids=manifest.tool_ids,
                budget=manifest.budget,
                specialist_composition="autonomous",
            )
            CampaignStore(connection).prepare(
                campaign_id,
                str(session.id),
                {
                    "manifest": _json_value(asdict(report_manifest)),
                    "assignment_context": _json_value(asdict(context)),
                },
                report_manifest.digest,
            )
            scheduler = FaultAfterPendingCheckpointStore(connection)
            async with httpx.AsyncClient() as client:
                target = ReviewedApiTargetTool(
                    CampaignApiSessionRepository(connection),
                    lambda _campaign_id: adapter,
                    {},
                    client=client,
                )
                broker = ToolBroker(
                    (target,),
                    confirmer=allow,
                    effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
                )
                service = DurableAgentService(
                    provider,
                    broker,
                    scheduler,
                    model="model",
                    workspace_root=str(tmp_path),
                    lease_seconds=0.1,
                )
                live = await service.submit(
                    str(session.id),
                    (assignment,),
                    max_workers=1,
                    minimum_workers=1,
                )
                CampaignStore(connection).add_wave(campaign_id, 0, "initial", live.id)
                assert live.execution is not None
                persisted = await asyncio.to_thread(scheduler.pending_persisted.wait, 5)
                assert persisted
                first_attempt_id = scheduler.faulted_attempt_id
                assert first_attempt_id is not None
                effects = ToolEffectRepository(connection).list_for_attempt(first_attempt_id)
                assert len(effects) == 1
                assert effects[0].state is ToolEffectState.ACKNOWLEDGED
                assert provider.calls == 2
                assert http_invocations.count("message") == 1
                live.execution.cancel()
                scheduler.release_fault.set()
                await asyncio.gather(live.execution, return_exceptions=True)
                node_id = f"worker:{assignment_id}"
                message_id = str(uuid5(NAMESPACE_URL, f"joined-integration:{live.id}"))
                message, created = scheduler.send_message_once(
                    message_id,
                    live.id,
                    "campaign-lead",
                    node_id,
                    "merlin.worker-coordination.v1",
                    {"message": "Continue from the acknowledged durable checkpoint."},
                )
                assert created is True
                scheduler.claim_message(
                    first_attempt_id,
                    1,
                    node_id,
                    str(message.id),
                )
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

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            scheduler = DurableSchedulerStore(connection)
            restored_before_resume = scheduler.restore(schedule_id)
            mailbox = restored_before_resume["mailbox"]
            assert len(mailbox) == 1
            assert str(mailbox[0]["id"]) == message_id
            assert mailbox[0]["acknowledged_at"] is not None
            _, duplicate_created = scheduler.send_message_once(
                message_id,
                schedule_id,
                "campaign-lead",
                f"worker:{assignment_id}",
                "merlin.worker-coordination.v1",
                {"message": "Continue from the acknowledged durable checkpoint."},
            )
            assert duplicate_created is False
            scheduler.pause_schedule(schedule_id, reason="joined-restart-boundary")
            async with httpx.AsyncClient() as client:
                target = ReviewedApiTargetTool(
                    CampaignApiSessionRepository(connection),
                    lambda _campaign_id: adapter,
                    {},
                    client=client,
                )
                broker = ToolBroker(
                    (target,),
                    confirmer=allow,
                    effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
                )
                replacement = DurableAgentService(
                    provider,
                    broker,
                    scheduler,
                    model="model",
                    workspace_root=str(tmp_path),
                    lease_seconds=0.1,
                    campaign_cleanup=target.cleanup_assignment,
                )
                staged = await replacement.reopen(schedule_id, resume_execution=False)
                assert staged.execution is None
                reopened = await replacement.resume_schedule(schedule_id)
                assert reopened.execution is not None
                await asyncio.wait_for(reopened.execution, timeout=10)

            worker_attempts = [
                row
                for row in scheduler.list_attempts(schedule_id)
                if row["node_id"] == f"worker:{assignment_id}"
            ]
            assert len(worker_attempts) == 2
            assert worker_attempts[0]["state"] == "expired"
            assert worker_attempts[1]["state"] == "completed", scheduler.list_results(schedule_id)
            assert str(worker_attempts[1]["id"]) != first_attempt_id
            restored = scheduler.restore(schedule_id)
            assert len(restored["mailbox"]) == 1
            assert restored["mailbox"][0]["acknowledged_at"] is not None
            assert [event["type"] for event in restored["events"]].count("schedule_paused") == 1
            assert [event["type"] for event in restored["events"]].count("schedule_resumed") == 1
            assert provider.calls == 2
            assert http_invocations.count("start") == 1
            assert http_invocations.count("message") == 2
            assert http_invocations.count("cleanup") == 1
            effects = [
                row
                for attempt in worker_attempts
                for row in ToolEffectRepository(connection).list_for_attempt(str(attempt["id"]))
            ]
            assert len(effects) == 2
            assert all(row.state is ToolEffectState.ACKNOWLEDGED for row in effects)
            gate = scheduler.goat_gate(schedule_id)
            assert gate["positive"] == ()
            assert gate["ambiguous"] == (assignment_id,)
            receipt = gate["strategies"][assignment_id]
            assert receipt["execution_complete"] is True
            assert receipt["evidence_verdict"] == "ambiguous"
            assert receipt["steps"][1]["objective_matched"] is True
            assert len(receipt["steps"]) == 2
            assert len({row["effect_id"] for row in receipt["steps"]}) == 2
            assert len({row["exchange_id"] for row in receipt["steps"]}) == 2
            assert str(receipt["steps"][0]["scheduler_attempt_id"]) == first_attempt_id
            assert str(receipt["steps"][1]["scheduler_attempt_id"]) == str(worker_attempts[1]["id"])

        assert report_manifest is not None
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "model",
                    },
                    "permission_mode": "auto",
                },
            ),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
        backend = build_backend(tmp_path / "report-workspace")
        try:
            await backend._campaigns._finish_campaign(  # type: ignore[attr-defined]
                campaign_id, "completed", "capped"
            )
            await backend._campaigns._report_runner(  # type: ignore[attr-defined]
                campaign_id, report_manifest, "capped"
            )
            assert len(backend._report_repository.list(campaign_id)) == 1  # type: ignore[attr-defined]
            revision = backend._report_repository.list(campaign_id)[-1]  # type: ignore[attr-defined]
            internal = backend._report_repository.get(  # type: ignore[attr-defined]
                revision.revision_id
            )
            assert internal.stop_reason == "budget_exhausted"
            assert internal.report_state == "incomplete"
            assert internal.coverage["confirmed"] == 0
            technique = next(row for row in internal.techniques if row["strategy_id"] == "goat")
            assert technique["goat_receipt"] == receipt["receipt_digest"]
            lineage = [row for row in internal.lineage if row.get("entry") == "merlin.goat.v1"]
            assert len(lineage) == 2
            assert {row["attempt"] for row in lineage} == {
                first_attempt_id,
                str(worker_attempts[1]["id"]),
            }
            for row, step in zip(
                sorted(lineage, key=lambda item: item["step_index"]),
                receipt["steps"],
                strict=True,
            ):
                assert row["step_index"] == step["step_index"]
                assert row["attempt"] == str(step["scheduler_attempt_id"])
                assert row["effect_receipt"] == step["effect_id"]
                assert row["exchange"] == step["exchange_id"]
                assert row["oracle"] == step["objective_observation_digest"]
                assert row["stage_receipt"] == step["receipt_digest"]
            customer = json.loads(
                DurableReportService(backend._report_repository).export(  # type: ignore[attr-defined]
                    revision.revision_id, "json", "customer"
                )
            )
            serialized = json.dumps(customer, sort_keys=True)
            for secret in (
                first_attempt_id,
                str(worker_attempts[1]["id"]),
                *[str(row["call_id"]) for row in receipt["steps"]],
                *[str(row["effect_id"]) for row in receipt["steps"]],
                *[str(row["receipt_digest"]) for row in receipt["steps"]],
            ):
                assert secret not in serialized
            for visible in ("reviewed response 0", "reviewed response 1"):
                assert visible in serialized
            for redacted_wire_body in ("target reply 1", "target reply 2"):
                assert redacted_wire_body not in serialized
            interaction_exchanges = [
                row for row in internal.exchanges if row.get("interaction") is True
            ]
            assert [row["oracle_observation"]["matched"] for row in interaction_exchanges] == [
                False,
                True,
            ]
            assert all("response" not in row for row in interaction_exchanges)
            assert all(
                row["response_summary"] == {"kind": "str", "utf8_bytes": 16}
                for row in interaction_exchanges
            )
            customer_lineage = [
                row
                for row in customer["lineage"]
                if row.get("effect_receipt") == "[redacted]" and row.get("exchange") is not None
            ]
            assert len(customer_lineage) == 2
            assert all(row["attempt"] == "[redacted]" for row in customer_lineage)
            assert all(row["effect_receipt"] == "[redacted]" for row in customer_lineage)
            customer_exchange_ids = {row["id"] for row in customer["exchanges"]}
            assert all(row["exchange"] in customer_exchange_ids for row in customer_lineage)
            terminal_lineage = [
                row
                for row in customer["lineage"]
                if row.get("effect_receipt") == "[redacted]" and row.get("exchange") is None
            ]
            assert len(terminal_lineage) == 1
            assert terminal_lineage[0]["attempt"] == "[redacted]"
        finally:
            await backend.close()
            target_server.close()
            await target_server.wait_closed()


@pytest.mark.asyncio
async def test_joined_goat_old_intent_restart_is_ambiguous_without_provider_or_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoProvider:
        capabilities = SimpleNamespace(vision=False, context_window_tokens=32_000)

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, _request):
            self.calls += 1
            raise AssertionError("old GOAT INTENT must short-circuit before provider execution")
            yield  # pragma: no cover

    async def allow(*_args) -> bool:
        return True

    data_dir = tmp_path / "postgres-joined-goat-intent"
    campaign_id = "campaign/joined-goat-intent"
    assignment_id = f"{campaign_id}/goat"
    target_id = "target-reviewed-api"
    oracle_digest = _digest("intent-objective-oracle")
    adapter = {
        "adapter_identity_version": EXECUTABLE_ADAPTER_IDENTITY_VERSION,
        "interface": "http_api",
        "endpoint": "https://target.test/api/",
        "api_steps": [
            {
                "step_id": "interaction",
                "endpoint": "message",
                "method": "POST",
                "request_template": {"message": "{{input}}"},
                "response_selector": "/reply",
                "expected_status": 200,
                "success_witness": "reply",
                "cleanup": False,
                "interaction": True,
                "captures": {},
                "repeat": 1,
                "idempotency_key": None,
                "replay_after_ambiguous_result": False,
                "oracle_predicate": {
                    "purpose": "objective",
                    "oracle_digest": oracle_digest,
                    "pointer": "/matched",
                    "match_values": [True],
                    "modality": "text",
                    "turn": "multi",
                    "language": "en",
                },
            }
        ],
    }
    contract = GoatContract(
        version="merlin.goat.v1",
        semantic_technique_id="semantic:goat:intent",
        goal_digest=_digest("intent objective"),
        toolbox=trusted_goat_toolbox(),
        toolbox_catalog_digest=GOAT_TOOLBOX_CATALOG_DIGEST,
        terminal_oracle_digest=oracle_digest,
        reviewed_adapter_digest=executable_adapter_digest(adapter),
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
    assignment = WorkerAssignment(
        id=assignment_id,
        objective="intent objective",
        profile_id="injection",
        target_profile_id=target_id,
        technique_ids=("case:goat:intent",),
        guidance_ids=(),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_seconds=30),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        provider_model="model",
        provider_capabilities=("streaming", "tools"),
        strategy_id="goat",
        semantic_technique_id=contract.semantic_technique_id,
        strategy_contract=tuple(
            sorted(
                {
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
                }.items()
            )
        ),
    )
    http_calls = 0
    report_manifest: CampaignManifest | None = None

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        raise AssertionError("old GOAT INTENT must short-circuit before HTTP execution")

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("joined GOAT INTENT")
            reviewed_document = ContextDocument.create(
                "target:reviewed-interaction-contract",
                "Reviewed interaction contract\n"
                + json.dumps(adapter, sort_keys=True, separators=(",", ":")),
            )
            report_manifest = CampaignManifest.create(
                id=campaign_id,
                session_id=str(session.id),
                objective="intent objective",
                profile=CampaignProfile.RECOMMENDED,
                target=ReviewedTargetAdapter(
                    id=target_id,
                    interface="http_api",
                    endpoint="https://target.test/api/message",
                ),
                provider_id="provider",
                provider_model="model",
                provider_effort="default",
                authority_ids=("target:reviewed",),
                catalogue=(reviewed_document,),
                skill_documents=(),
                tool_ids=("target_request",),
                budget=AssignmentBudget(max_seconds=30),
            )
            report_context = AssignmentContext(
                target_profile_id=target_id,
                techniques=(
                    AssignmentTechnique(
                        id="case:goat:intent",
                        objective="intent objective",
                        tags=frozenset({"prompt-injection", "text", "multi", "conversational"}),
                        guidance_ids=(),
                        semantic_id=contract.semantic_technique_id,
                        language="en",
                        domain_id="prompt-injection",
                        oracle_digest=oracle_digest,
                        strategy_id="goat",
                        strategy_contract=assignment.strategy_contract,
                    ),
                ),
                tool_ids=("target_request",),
                skill_ids=(),
                provider_id="provider",
                authority_ids=("target:reviewed",),
                budget=AssignmentBudget(max_seconds=30),
                input_schema='{"type":"object"}',
                output_schema='{"type":"object"}',
                provider_model="model",
            )
            CampaignStore(connection).prepare(
                campaign_id,
                str(session.id),
                {
                    "manifest": _json_value(asdict(report_manifest)),
                    "assignment_context": _json_value(asdict(report_context)),
                },
                report_manifest.digest,
            )
            scheduler = DurableSchedulerStore(connection)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                target = ReviewedApiTargetTool(
                    CampaignApiSessionRepository(connection),
                    lambda _campaign_id: adapter,
                    {},
                    client=client,
                )
                service = DurableAgentService(
                    NoProvider(),
                    ToolBroker(
                        (target,),
                        confirmer=allow,
                        effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
                    ),
                    scheduler,
                    model="model",
                    workspace_root=str(tmp_path),
                    lease_seconds=0.1,
                )
                live = await service.submit(
                    str(session.id),
                    (assignment,),
                    max_workers=1,
                    minimum_workers=1,
                    start_immediately=False,
                )
                start_handle = live.scheduler.claim("start", "scheduler", 0)
                start_lease = await service._claim(live, start_handle)
                live.scheduler.complete(start_handle, "start", 1)
                await service._commit(live, start_lease, {"started": True}, "schema/envelope")
                await service._ready(live, live.scheduler.ready_nodes())
                CampaignStore(connection).add_wave(campaign_id, 0, "initial", live.id)
            node_id = f"worker:{assignment_id}"
            first = scheduler.claim(
                live.id,
                node_id,
                node_id,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            arguments = {
                "input": "must not resend",
                "strategy_plan": {
                    "observation": "o",
                    "thought": "t",
                    "strategy": ["hypothetical"],
                    "response": "must not resend",
                },
                "strategy_turn_index": 0,
            }
            effect, created = ToolEffectRepository(connection).begin(
                ToolEffectRecord(
                    idempotency_key=sha256(b"joined-goat-intent").hexdigest(),
                    session_id=session.id,
                    call_id="goat-intent-call",
                    agent_id=assignment_id,
                    attempt_id=first.id,
                    campaign_id=campaign_id,
                    target_id=target_id,
                    tool_name="target_request",
                    effect="target_network",
                    arguments_digest=sha256(
                        json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    arguments_payload=arguments,
                    state=ToolEffectState.INTENT,
                )
            )
            assert created and effect.state is ToolEffectState.INTENT
            connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET lease_expires_at = now() - interval '1 second' WHERE id = %s""",
                (first.id,),
            )
            connection.commit()
            schedule_id = live.id
            first_attempt_id = first.id

    provider = NoProvider()
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            scheduler = DurableSchedulerStore(connection)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                target = ReviewedApiTargetTool(
                    CampaignApiSessionRepository(connection),
                    lambda _campaign_id: adapter,
                    {},
                    client=client,
                )
                replacement = DurableAgentService(
                    provider,
                    ToolBroker(
                        (target,),
                        confirmer=allow,
                        effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
                    ),
                    scheduler,
                    model="model",
                    workspace_root=str(tmp_path),
                    lease_seconds=0.1,
                    campaign_cleanup=target.cleanup_assignment,
                )
                reopened = await replacement.reopen(schedule_id)
                assert reopened.execution is not None
                await asyncio.wait_for(reopened.execution, timeout=10)

            assert provider.calls == 0
            assert http_calls == 0
            worker_attempts = [
                row
                for row in scheduler.list_attempts(schedule_id)
                if row["node_id"] == f"worker:{assignment_id}"
            ]
            assert len(worker_attempts) == 2
            assert worker_attempts[0]["state"] == "expired"
            assert worker_attempts[1]["state"] == "completed"
            assert str(worker_attempts[0]["id"]) == first_attempt_id
            assert str(worker_attempts[1]["id"]) != first_attempt_id
            committed_worker_results = [
                row
                for row in scheduler.list_results(schedule_id)
                if row["node_id"] == f"worker:{assignment_id}"
                and row["schema_id"] == "schema/result"
            ]
            assert len(committed_worker_results) == 1
            effects = ToolEffectRepository(connection).list_for_attempt(first_attempt_id)
            assert len(effects) == 1
            assert effects[0].state is ToolEffectState.INTENT
            gate = scheduler.goat_gate(schedule_id)
            assert gate["complete"] == ()
            assert gate["ambiguous"] == (assignment_id,)
            receipt = gate["strategies"][assignment_id]
            assert receipt["execution_complete"] is False
            assert receipt["evidence_verdict"] == "ambiguous"
            assert receipt["steps"] == ()

        assert report_manifest is not None
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "model",
                    },
                    "permission_mode": "auto",
                },
            ),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
        backend = build_backend(tmp_path / "intent-report-workspace")
        try:
            await backend._campaigns._finish_campaign(  # type: ignore[attr-defined]
                campaign_id, "failed", "interrupted"
            )
            await backend._campaigns._report_runner(  # type: ignore[attr-defined]
                campaign_id, report_manifest, "interrupted"
            )
            revision = backend._report_repository.list(campaign_id)[-1]  # type: ignore[attr-defined]
            internal = backend._report_repository.get(  # type: ignore[attr-defined]
                revision.revision_id
            )
            assert internal.stop_reason == "infrastructure_blocked_after_retry_policy"
            assert internal.report_state == "incomplete"
            technique = next(row for row in internal.techniques if row["strategy_id"] == "goat")
            assert technique["outcome"] in {"unclear", "blocked"}
            assert internal.coverage["unclear"] + internal.coverage["blocked"] == 1
            goat_lineage = [row for row in internal.lineage if row.get("entry") == "merlin.goat.v1"]
            assert goat_lineage == []
            customer = json.loads(
                DurableReportService(backend._report_repository).export(  # type: ignore[attr-defined]
                    revision.revision_id, "json", "customer"
                )
            )
            serialized = json.dumps(customer, sort_keys=True)
            for secret in (
                first_attempt_id,
                str(worker_attempts[1]["id"]),
                "goat-intent-call",
                effect.idempotency_key,
                "must not resend",
            ):
                assert secret not in serialized
            assert not any(row.get("entry") == "merlin.goat.v1" for row in customer["lineage"])
        finally:
            await backend.close()
