import asyncio
import json
import threading
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

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
    compile_attack_campaign,
)
from merlin_agents.campaign import compile_execution_agenda
from merlin_agents.campaign_service import _json_value
from merlin_agents.hydra_select import HydraSelectContract
from merlin_assessments import ReviewedApiTargetTool
from merlin_config import Config, CredentialStore
from merlin_contracts import (
    EXECUTABLE_ADAPTER_IDENTITY_VERSION,
    HYDRA_SELECT_CATALOG_DIGEST,
    ModelCompleted,
    TextDelta,
    ToolEffectState,
    executable_adapter_digest,
    trusted_hydra_select_mechanisms,
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
@pytest.mark.parametrize(
    ("first_match", "malformed_tail"),
    ((False, False), (True, False), (False, True)),
)
async def test_joined_hydra_ack_prefix_replacement_never_regenerates_or_resends(
    tmp_path: Path,
    first_match: bool,
    malformed_tail: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        capabilities = SimpleNamespace(vision=False, context_window_tokens=32_000)

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            assert request.tools == []
            proposal = {
                "candidates": [
                    {
                        "prompt": f"joined Hydra candidate {index}",
                        "rationale": f"reviewed slot {index}",
                    }
                    for index in range(6)
                ]
            }
            yield TextDelta(text=json.dumps(proposal))
            yield ModelCompleted(finish_reason="stop")

    class FaultAfterAckPrefixStore(DurableSchedulerStore):
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

    campaign_id = "campaign/joined-hydra"
    target_id = "target-reviewed-api"
    objective = "Execute the reviewed bounded Hydra catalogue sample"
    oracle_digest = _digest("joined-hydra-objective")
    adapter: dict[str, object] = {
        "adapter_identity_version": EXECUTABLE_ADAPTER_IDENTITY_VERSION,
        "interface": "http_api",
        "endpoint": "https://target.test/api/",
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
                "idempotency_key": "start-joined-hydra",
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
                "cleanup": True,
                "interaction": False,
                "captures": {},
                "repeat": 1,
                "idempotency_key": "cleanup-joined-hydra",
                "replay_after_ambiguous_result": True,
                "cleanup_acknowledgement": {
                    "pointer": "/closed",
                    "match_values": [True],
                    "status_codes": [200],
                },
            },
        ],
    }
    mechanisms = trusted_hydra_select_mechanisms()
    contract = HydraSelectContract(
        version="merlin.hydra-select.v1",
        semantic_technique_id="semantic:hydra:joined",
        goal_digest=_digest(objective),
        objective_oracle_digest=oracle_digest,
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
        mechanisms=mechanisms,
        mechanism_catalog_digest=HYDRA_SELECT_CATALOG_DIGEST,
        proposed_count=6,
        selected_count=3,
        max_candidate_chars=4096,
        stop_on_positive=True,
    )
    values = {
        "version": contract.version,
        "semantic_technique_id": contract.semantic_technique_id,
        "goal_digest": contract.goal_digest,
        "objective_oracle_digest": contract.objective_oracle_digest,
        "reviewed_adapter_digest": contract.reviewed_adapter_digest,
        "attacker_route_digest": contract.attacker_route_digest,
        "attacker_tool_transport": contract.attacker_tool_transport,
        "target_profile_id": contract.target_profile_id,
        "session_policy": contract.session_policy,
        "modality": contract.modality,
        "language": contract.language,
        "mechanisms_json": json.dumps(mechanisms, separators=(",", ":")),
        "mechanism_catalog_digest": contract.mechanism_catalog_digest,
        "proposed_count": "6",
        "selected_count": "3",
        "max_candidate_chars": "4096",
        "stop_on_positive": "true",
    }
    reviewed_document = ContextDocument.create(
        "target:reviewed-interaction-contract",
        "Reviewed interaction contract\n"
        + json.dumps(adapter, sort_keys=True, separators=(",", ":")),
    )
    manifest = CampaignManifest.create(
        id=campaign_id,
        session_id="placeholder",
        objective=objective,
        profile=CampaignProfile.RECOMMENDED,
        target=ReviewedTargetAdapter(target_id, "http_api", "https://target.test/api/message"),
        provider_id="provider",
        provider_model="model",
        provider_effort="default",
        authority_ids=("target:reviewed",),
        catalogue=(reviewed_document,),
        skill_documents=(),
        tool_ids=("target_request",),
        budget=AssignmentBudget(max_turns=8, max_seconds=30),
        specialist_composition="autonomous",
    )
    context = AssignmentContext(
        target_profile_id=target_id,
        techniques=(
            AssignmentTechnique(
                id="case:hydra:joined",
                objective=objective,
                tags=frozenset({"prompt-injection", "text", "multi", "conversational"}),
                guidance_ids=(),
                semantic_id=contract.semantic_technique_id,
                language="en",
                domain_id="prompt-injection",
                oracle_digest=oracle_digest,
                strategy_id="hydra-select",
                strategy_contract=tuple(sorted(values.items())),
            ),
        ),
        tool_ids=("target_request",),
        skill_ids=(),
        provider_id="provider",
        authority_ids=("target:reviewed",),
        budget=AssignmentBudget(max_turns=8, max_seconds=30),
        input_schema='{"type":"object"}',
        output_schema='{"type":"object"}',
        provider_model="model",
    )
    agenda = compile_execution_agenda(manifest, context, compile_attack_campaign(manifest, context))
    assert len(agenda.assignments) == 2
    assignment = next(item for item in agenda.assignments if item.strategy_id == "hydra-select")
    assert assignment.strategy_id == "hydra-select"
    assert assignment.comparison_supported is True
    assignment_id = assignment.id
    http_invocations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit("/", 1)[-1]
        http_invocations.append(operation)
        if operation == "start":
            return httpx.Response(200, json={"sessionId": "target-session-one"})
        if operation == "message":
            turn = http_invocations.count("message")
            if malformed_tail and turn == 2:
                return httpx.Response(200, json={"reply": "missing reviewed oracle value"})
            return httpx.Response(
                200,
                json={
                    "reply": f"target reply {turn}",
                    "matched": first_match and turn == 1,
                },
            )
        return httpx.Response(200, json={"closed": True})

    provider = Provider()
    data_dir = tmp_path / "postgres-joined-hydra"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("joined Hydra restart")
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
            campaign_store = CampaignStore(connection)

            async def reserve_budget(
                requested_campaign_id: str,
                reservation_id: str,
                actor_kind: str,
                tokens: int,
                turns: int,
                cost_usd: float,
            ) -> int | None:
                row = campaign_store.reserve_budget(
                    requested_campaign_id,
                    reservation_id,
                    actor_kind=actor_kind,
                    tokens=tokens,
                    turns=turns,
                    cost_usd=cost_usd,
                )
                return int(row["reserved_tokens"]) if row["state"] == "active" else None

            async def settle_budget(
                requested_campaign_id: str,
                reservation_id: str,
                tokens: int,
                turns: int,
                cost_usd: float,
            ) -> None:
                campaign_store.settle_budget(
                    requested_campaign_id,
                    reservation_id,
                    tokens=tokens,
                    turns=turns,
                    cost_usd=cost_usd,
                )

            async def release_budget(requested_campaign_id: str, reservation_id: str) -> None:
                campaign_store.release_budget(requested_campaign_id, reservation_id)

            scheduler = FaultAfterAckPrefixStore(connection)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
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
                    campaign_budget_reserver=reserve_budget,
                    campaign_budget_settler=settle_budget,
                    campaign_budget_releaser=release_budget,
                )
                live = await service.submit(
                    str(session.id), (assignment,), max_workers=1, minimum_workers=1
                )
                CampaignStore(connection).add_wave(campaign_id, 0, "initial", live.id)
                assert live.execution is not None
                if not await asyncio.to_thread(scheduler.pending_persisted.wait, 5):
                    await live.execution
                    raise AssertionError(
                        "Hydra execution completed before the injected ACK-prefix fault"
                    )
                first_attempt_id = scheduler.faulted_attempt_id
                assert first_attempt_id is not None
                assert provider.calls == 1
                assert http_invocations.count("message") == 1
                live.execution.cancel()
                scheduler.release_fault.set()
                await asyncio.gather(live.execution, return_exceptions=True)
                connection.execute(
                    "UPDATE merlin_storage.scheduler_attempts "
                    "SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                    (first_attempt_id,),
                )
                connection.commit()
                schedule_id = live.id

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            scheduler = DurableSchedulerStore(connection)
            campaign_store = CampaignStore(connection)

            async def reserve_budget(
                requested_campaign_id: str,
                reservation_id: str,
                actor_kind: str,
                tokens: int,
                turns: int,
                cost_usd: float,
            ) -> int | None:
                row = campaign_store.reserve_budget(
                    requested_campaign_id,
                    reservation_id,
                    actor_kind=actor_kind,
                    tokens=tokens,
                    turns=turns,
                    cost_usd=cost_usd,
                )
                return int(row["reserved_tokens"]) if row["state"] == "active" else None

            async def settle_budget(
                requested_campaign_id: str,
                reservation_id: str,
                tokens: int,
                turns: int,
                cost_usd: float,
            ) -> None:
                campaign_store.settle_budget(
                    requested_campaign_id,
                    reservation_id,
                    tokens=tokens,
                    turns=turns,
                    cost_usd=cost_usd,
                )

            async def release_budget(requested_campaign_id: str, reservation_id: str) -> None:
                campaign_store.release_budget(requested_campaign_id, reservation_id)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
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
                    campaign_budget_reserver=reserve_budget,
                    campaign_budget_settler=settle_budget,
                    campaign_budget_releaser=release_budget,
                )
                reopened = await replacement.reopen(schedule_id)
                assert reopened.execution is not None
                await asyncio.wait_for(reopened.execution, timeout=10)

            attempts = [
                row
                for row in scheduler.list_attempts(schedule_id)
                if row["node_id"] == f"worker:{assignment_id}"
            ]
            assert len(attempts) == 2
            assert attempts[0]["state"] == "expired"
            assert attempts[1]["state"] == "completed"
            assert provider.calls == 1
            assert http_invocations.count("start") == 1
            expected_effects = 1 if first_match else 3
            assert http_invocations.count("message") == expected_effects
            assert http_invocations.count("cleanup") == 1
            effects = [
                effect
                for attempt in attempts
                for effect in ToolEffectRepository(connection).list_for_attempt(str(attempt["id"]))
            ]
            assert len(effects) == expected_effects
            assert all(effect.state is ToolEffectState.ACKNOWLEDGED for effect in effects)
            gate = scheduler.hydra_select_gate(schedule_id)
            assert gate["positive"] == ()
            assert gate["negative"] == ()
            assert gate["blocked"] == ()
            assert gate["ambiguous"] == (assignment_id,)
            receipt = gate["strategies"][assignment_id]
            assert receipt["execution_complete"] is (not first_match)
            assert len(receipt["steps"]) == expected_effects
            assert receipt["steps"][0]["scheduler_attempt_id"] == first_attempt_id
            if not first_match:
                assert {item["scheduler_attempt_id"] for item in receipt["steps"][1:]} == {
                    str(attempts[1]["id"])
                }
            if malformed_tail:
                assert receipt["evaluation_uncertainties"]
                assert any(
                    "objective" in str(item.get("reason", ""))
                    for item in receipt["evaluation_uncertainties"]
                )
            action_reservations = connection.execute(
                """SELECT * FROM merlin_storage.campaign_budget_reservations
                   WHERE campaign_id = %s AND reservation_id LIKE %s""",
                (campaign_id, "%/hydra-target-actions"),
            ).fetchall()
            assert len(action_reservations) == 1
            assert action_reservations[0]["state"] == "settled"
            assert action_reservations[0]["reserved_turns"] == 3
            assert action_reservations[0]["settled_turns"] == expected_effects
            replayed_gate = scheduler.hydra_select_gate(schedule_id)
            assert replayed_gate["ambiguous"] == (assignment_id,)
            assert replayed_gate["positive"] == ()
            assert replayed_gate["negative"] == ()
            singleton_counts = connection.execute(
                """SELECT
                       (SELECT count(*) FROM merlin_storage.scheduler_results
                         WHERE schedule_id = %s AND node_id = %s
                           AND schema_id = 'schema/result') AS terminal_results,
                       (SELECT count(*) FROM merlin_storage.hydra_select_plan_receipts
                         WHERE schedule_id = %s AND assignment_id = %s) AS plans,
                       (SELECT count(*) FROM merlin_storage.hydra_select_step_receipts
                         WHERE schedule_id = %s AND assignment_id = %s) AS steps,
                       (SELECT count(*) FROM merlin_storage.hydra_select_terminal_receipts
                         WHERE schedule_id = %s AND assignment_id = %s) AS terminals""",
                (
                    schedule_id,
                    f"worker:{assignment_id}",
                    schedule_id,
                    assignment_id,
                    schedule_id,
                    assignment_id,
                    schedule_id,
                    assignment_id,
                ),
            ).fetchone()
            assert dict(singleton_counts) == {
                "terminal_results": 1,
                "plans": 1,
                "steps": expected_effects,
                "terminals": 0,
            }

        if first_match:
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
            backend = build_backend(tmp_path / "hydra-positive-report")
            try:
                await backend._campaigns._finish_campaign(  # type: ignore[attr-defined]
                    campaign_id, "completed", "capped"
                )
                await backend._campaigns._report_runner(  # type: ignore[attr-defined]
                    campaign_id, report_manifest, "capped"
                )
                revision = backend._report_repository.list(campaign_id)[-1]  # type: ignore[attr-defined]
                internal = backend._report_repository.get(  # type: ignore[attr-defined]
                    revision.revision_id
                )
                technique = next(
                    row for row in internal.techniques if row["strategy_id"] == "hydra-select"
                )
                assert technique["outcome"] == "unclear"
                assert internal.stop_reason == "budget_exhausted"
                assert internal.report_state == "incomplete"
                assert (
                    sum(row.get("strategy_id") == "hydra-select" for row in internal.techniques)
                    == 1
                )
                dispositions = technique["hydra_select_candidate_dispositions"]
                assert sum(row["disposition"] == "executed" for row in dispositions) == 1
                assert (
                    sum(row["disposition"].startswith("not-executed-") for row in dispositions) == 2
                ), dispositions
                assert sum(row["disposition"] == "not-selected" for row in dispositions) == 3
                lineage = [
                    row for row in internal.lineage if row.get("entry") == "merlin.hydra-select.v1"
                ]
                assert len(lineage) == 1
                customer = json.loads(
                    DurableReportService(backend._report_repository).export(  # type: ignore[attr-defined]
                        revision.revision_id, "json", "customer"
                    )
                )
                serialized_customer = json.dumps(customer, sort_keys=True)
                assert "candidate-" not in serialized_customer
                assert "reviewed slot" not in serialized_customer
                assert "joined Hydra candidate" in serialized_customer
                assert "receipt_digest" not in serialized_customer
            finally:
                await backend.close()
