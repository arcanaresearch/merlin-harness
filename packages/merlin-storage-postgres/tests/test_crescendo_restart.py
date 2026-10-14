import asyncio
import json
import threading
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
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
    DurableAgentService,
    ReviewedTargetAdapter,
    WorkerAssignment,
)
from merlin_agents.campaign_service import _json_value
from merlin_agents.crescendo import CrescendoContract, CrescendoState
from merlin_assessments import ReviewedApiTargetTool
from merlin_config import Config, CredentialStore
from merlin_contracts import (
    EXECUTABLE_ADAPTER_IDENTITY_VERSION,
    ModelCompleted,
    TextDelta,
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


def digest(value: object) -> str:
    return (
        "sha256:"
        + sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )


def test_crescendo_receipt_and_state_survive_postgres_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "postgres-crescendo"
    adapter_digest = digest("adapter")
    oracle_digests = {
        purpose: digest(purpose)
        for purpose in ("objective", "refusal", "progress", "invalid-premise")
    }
    contract = {
        "version": "merlin.crescendo.v1",
        "semantic_technique_id": "semantic:crescendo",
        "objective_oracle_digest": oracle_digests["objective"],
        "refusal_oracle_digest": oracle_digests["refusal"],
        "progress_oracle_digest": oracle_digests["progress"],
        "invalid_premise_oracle_digest": oracle_digests["invalid-premise"],
        "reviewed_adapter_digest": adapter_digest,
        "modality": "text",
        "language": "en",
        "max_target_turns": "5",
        "plateau_turns": "2",
        "initial_state_equivalence_digest": digest("equivalence"),
    }
    assignment_id = "campaign/fixture-id/crescendo/one"
    assignment = {
        "id": assignment_id,
        "strategy_id": "crescendo",
        "strategy_contract": sorted(contract.items()),
        "technique_ids": ["case:crescendo"],
        "target_profile_id": "target-1",
    }
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("crescendo restart")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"assignments": [assignment]},
                {"completed": []},
                [f"worker:{assignment_id}"],
                ready_nodes=[f"worker:{assignment_id}"],
            )
            lease = store.claim(
                schedule_id,
                f"worker:{assignment_id}",
                "agent-one",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            observations = []
            for purpose, oracle_digest in oracle_digests.items():
                binding = {
                    "adapter_digest": adapter_digest,
                    "step_id": "interaction",
                    "oracle_digest": oracle_digest,
                    "pointer": "/result",
                    "match_values": [purpose],
                    "modality": "text",
                    "turn": "multi",
                    "language": "en",
                    "purpose": purpose,
                }
                observation = {
                    "schema": "merlin.api-oracle-observation.v2",
                    **binding,
                    "binding_digest": digest(binding),
                    "selected_digest": digest(purpose),
                    "matched": purpose == "objective",
                }
                observation["observation_digest"] = digest(observation)
                observations.append(observation)
            exchange = {
                "id": "interaction",
                "interaction": True,
                "cleanup": False,
                "response": "objective met",
                "oracle_observations": observations,
            }
            repository = ToolEffectRepository(connection)
            started, created = repository.begin(
                ToolEffectRecord(
                    idempotency_key=sha256(b"crescendo-effect").hexdigest(),
                    session_id=session.id,
                    call_id="call-one",
                    agent_id=assignment_id,
                    attempt_id=lease.id,
                    campaign_id="campaign/fixture-id",
                    target_id="target-1",
                    tool_name="target_request",
                    effect="target_network",
                    arguments_digest=sha256(b"prompt").hexdigest(),
                    state=ToolEffectState.INTENT,
                )
            )
            assert created
            repository.finish(
                started.model_copy(
                    update={
                        "state": ToolEffectState.ACKNOWLEDGED,
                        "result_content": "objective met",
                        "result_is_error": False,
                        "result_metadata": {
                            "campaign_id": "campaign/fixture-id",
                            "target_profile_id": "target-1",
                            "target_session_id": "api:campaign",
                            "target_exchanges": [exchange],
                        },
                    }
                )
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            restored = DurableSchedulerStore(connection)
            gate = restored.crescendo_gate(schedule_id)
            assert gate["positive"] == ()
            assert gate["ambiguous"] == (assignment_id,)
            strategy = gate["strategies"][assignment_id]
            assert strategy["verdict"] == "ambiguous"
            assert strategy["recorded_verdict"] is None
            assert strategy["steps"][0]["exchange_id"] == "call-one:interaction"
            assert strategy["steps"][0]["decision"] == "success"
            state = restored.crescendo_state(schedule_id, assignment_id, lease.id)
            assert state is not None
            assert state["terminal_reason"] is None
            assert state["attempt_id"] == lease.id
            reviewed = CrescendoContract(
                version="merlin.crescendo.v1",
                semantic_technique_id="semantic:crescendo",
                objective_oracle_digest=oracle_digests["objective"],
                refusal_oracle_digest=oracle_digests["refusal"],
                progress_oracle_digest=oracle_digests["progress"],
                invalid_premise_oracle_digest=oracle_digests["invalid-premise"],
                reviewed_adapter_digest=adapter_digest,
                modality="text",
                language="en",
                max_target_turns=5,
                plateau_turns=2,
            )
            assert CrescendoState.restore(state, reviewed, lease.id).terminal_reason is None


def test_blocked_crescendo_cell_is_durable_without_target_effect(tmp_path: Path) -> None:
    assignment_id = "campaign/blocked/crescendo/two"
    data_dir = tmp_path / "postgres-blocked"
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("blocked Crescendo")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {
                    "assignments": [
                        {
                            "id": assignment_id,
                            "strategy_id": "crescendo",
                            "strategy_contract": [],
                            "technique_ids": ["case:crescendo:two"],
                            "comparison_supported": False,
                            "comparison_block_reason": (
                                "campaign-global API session already owns one Crescendo chain; "
                                "additional cells require reviewed session isolation"
                            ),
                            "target_profile_id": "target-1",
                        }
                    ]
                },
                {"completed": []},
                [f"worker:{assignment_id}"],
                ready_nodes=[f"worker:{assignment_id}"],
            )

            gate = store.crescendo_gate(schedule_id)

            assert gate["execution_complete"] == ()
            assert gate["blocked"] == ()
            assert gate["complete"] == ()
            assert gate["strategies"][assignment_id]["steps"] == ()
            assert (
                connection.execute(
                    "SELECT count(*) AS count FROM merlin_storage.tool_effects"
                ).fetchone()["count"]
                == 0
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            restored = DurableSchedulerStore(connection)
            first = restored.crescendo_gate(schedule_id)
            second = restored.crescendo_gate(schedule_id)
            assert first["strategies"][assignment_id]["steps"] == ()
            assert second["strategies"][assignment_id]["steps"] == ()
            assert (
                connection.execute(
                    """SELECT count(*) AS count
                   FROM merlin_storage.crescendo_terminal_receipts
                   WHERE schedule_id = %s AND assignment_id = %s""",
                    (schedule_id, assignment_id),
                ).fetchone()["count"]
                == 0
            )


@pytest.mark.parametrize(
    ("terminal_kind", "expected_verdict"),
    (
        ("cancelled", "cancelled"),
        ("failed", "ambiguous"),
        ("quarantined", "ambiguous"),
        ("intent", "ambiguous"),
    ),
)
def test_zero_step_crescendo_terminal_is_restored_without_resend(
    tmp_path: Path, terminal_kind: str, expected_verdict: str
) -> None:
    adapter_digest = digest("zero-step-adapter")
    oracle_digests = {
        purpose: digest(f"zero-step:{purpose}")
        for purpose in ("objective", "refusal", "progress", "invalid-premise")
    }
    contract = {
        "version": "merlin.crescendo.v1",
        "semantic_technique_id": "semantic:crescendo:zero-step",
        "objective_oracle_digest": oracle_digests["objective"],
        "refusal_oracle_digest": oracle_digests["refusal"],
        "progress_oracle_digest": oracle_digests["progress"],
        "invalid_premise_oracle_digest": oracle_digests["invalid-premise"],
        "reviewed_adapter_digest": adapter_digest,
        "modality": "text",
        "language": "en",
        "max_target_turns": "5",
        "plateau_turns": "2",
    }
    assignment_id = f"campaign/zero-step/crescendo/{terminal_kind}"
    with Database(DatabaseConfig(data_dir=tmp_path / f"postgres-zero-{terminal_kind}")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("zero-step Crescendo")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {
                    "assignments": [
                        {
                            "id": assignment_id,
                            "strategy_id": "crescendo",
                            "strategy_contract": sorted(contract.items()),
                            "technique_ids": ["case:crescendo:zero-step"],
                            "target_profile_id": "target-1",
                        }
                    ]
                },
                {"completed": []},
                [f"worker:{assignment_id}"],
                ready_nodes=[f"worker:{assignment_id}"],
            )
            lease = store.claim(
                schedule_id,
                f"worker:{assignment_id}",
                assignment_id,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            if terminal_kind == "cancelled":
                assert store.cancel_node(
                    schedule_id,
                    f"worker:{assignment_id}",
                    reason="operator cancelled before first effect",
                    checkpoint={"cancelled": True},
                )
            elif terminal_kind == "intent":
                ToolEffectRepository(connection).begin(
                    ToolEffectRecord(
                        idempotency_key=sha256(b"unknown-intent").hexdigest(),
                        session_id=session.id,
                        call_id="unknown-call",
                        agent_id=assignment_id,
                        attempt_id=lease.id,
                        campaign_id="campaign/zero-step",
                        target_id="target-1",
                        tool_name="target_request",
                        effect="target_network",
                        arguments_digest=sha256(b"unknown-prompt").hexdigest(),
                        state=ToolEffectState.INTENT,
                    )
                )
            else:
                connection.execute(
                    """UPDATE merlin_storage.scheduler_attempts
                       SET state = %s, completed_at = now() WHERE id = %s""",
                    (terminal_kind, lease.id),
                )
                connection.execute(
                    """UPDATE merlin_storage.scheduler_nodes
                       SET state = %s WHERE schedule_id = %s AND node_id = %s""",
                    (terminal_kind, schedule_id, f"worker:{assignment_id}"),
                )
                connection.commit()

            gate = store.crescendo_gate(schedule_id)
            receipt = gate["strategies"][assignment_id]
            assert gate["execution_complete"] == ()
            assert gate["complete"] == ()
            assert receipt["verdict"] == "ambiguous"
            assert receipt["recorded_verdict"] == "ambiguous"
            assert receipt["steps"] == ()
            state = store.crescendo_state(schedule_id, assignment_id, "replacement-attempt")
            assert state is not None
            assert state["terminal_reason"] == expected_verdict
            assert state["attempt_id"] == "replacement-attempt"


@pytest.mark.asyncio
async def test_joined_crescendo_replacement_adopts_acknowledged_api_effect_without_resend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash after target acknowledgement, then prove production replacement adoption."""

    class Provider:
        capabilities = SimpleNamespace(vision=False, context_window_tokens=32_000)

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if any(
                message.role == "system" and "Strategy is terminal: positive" in message.content
                for message in request.messages
            ):
                yield TextDelta(text="{}")
                yield ModelCompleted(finish_reason="stop")
                return
            yield ToolCall(
                call_id=f"crescendo-call-{self.calls}",
                name="target_request",
                arguments={"input": "reviewed escalating prompt"},
            )
            yield ModelCompleted(finish_reason="tool_calls")

    class FaultAfterAcknowledgementStore(DurableSchedulerStore):
        def __init__(self, connection) -> None:
            super().__init__(connection)
            self.acknowledged = threading.Event()
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
            checkpoint = result.get("worker_checkpoint")
            strategy = checkpoint.get("strategy_state") if isinstance(checkpoint, dict) else None
            steps = strategy.get("steps") if isinstance(strategy, dict) else None
            if self.faulted_attempt_id is None and isinstance(steps, (list, tuple)) and steps:
                effects = ToolEffectRepository(self.connection).list_for_attempt(attempt_id)
                assert len(effects) == 1
                assert effects[0].state is ToolEffectState.ACKNOWLEDGED
                self.faulted_attempt_id = attempt_id
                self.acknowledged.set()
                assert self.release_fault.wait(timeout=5)
                # The daemon disappears before this strategy checkpoint is durable.
                return "sha256:fault-injected-before-strategy-checkpoint"
            return super().commit_partial(
                attempt_id,
                generation,
                result=result,
                applied_message_ids=applied_message_ids,
            )

    async def allow(*_args) -> bool:
        return True

    data_dir = tmp_path / "postgres-joined-crescendo"
    campaign_id = "campaign/joined-crescendo"
    target_id = "target-reviewed-api"
    oracle_digests = {
        purpose: digest(f"oracle:{purpose}")
        for purpose in ("objective", "refusal", "progress", "invalid-premise")
    }
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
                "idempotency_key": "start-joined-crescendo",
                "replay_after_ambiguous_result": True,
            },
            {
                "step_id": "message",
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
                "oracle_predicates": [
                    {
                        "purpose": purpose,
                        "oracle_digest": oracle_digest,
                        "pointer": f"/signals/{purpose}",
                        "match_values": [True],
                        "modality": "text",
                        "turn": "multi",
                        "language": "en",
                    }
                    for purpose, oracle_digest in oracle_digests.items()
                ],
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
                "idempotency_key": "cleanup-joined-crescendo",
                "replay_after_ambiguous_result": True,
            },
        ],
    }
    adapter_digest = executable_adapter_digest(adapter)
    contract = {
        "version": "merlin.crescendo.v1",
        "semantic_technique_id": "semantic:crescendo:joined",
        "objective_oracle_digest": oracle_digests["objective"],
        "refusal_oracle_digest": oracle_digests["refusal"],
        "progress_oracle_digest": oracle_digests["progress"],
        "invalid_premise_oracle_digest": oracle_digests["invalid-premise"],
        "reviewed_adapter_digest": adapter_digest,
        "modality": "text",
        "language": "en",
        "max_target_turns": "5",
        "plateau_turns": "2",
    }
    assignment_id = f"{campaign_id}/crescendo"
    assignment = WorkerAssignment(
        id=assignment_id,
        objective="Run the reviewed Crescendo strategy",
        profile_id="injection",
        target_profile_id=target_id,
        technique_ids=("case:crescendo",),
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
        strategy_id="crescendo",
        semantic_technique_id=contract["semantic_technique_id"],
        strategy_contract=tuple(sorted(contract.items())),
    )
    blocked_contract = {
        **contract,
        "semantic_technique_id": "semantic:crescendo:joined:blocked",
    }
    blocked_assignment_id = f"{campaign_id}/crescendo-blocked"
    blocked_assignment = WorkerAssignment(
        id=blocked_assignment_id,
        objective="Blocked additional Crescendo strategy",
        profile_id="injection",
        target_profile_id=target_id,
        technique_ids=("case:crescendo:blocked",),
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
        strategy_id="crescendo",
        semantic_technique_id=blocked_contract["semantic_technique_id"],
        strategy_contract=tuple(sorted(blocked_contract.items())),
        comparison_supported=False,
        comparison_block_reason=(
            "campaign-global API session already owns one Crescendo chain; additional cells "
            "require reviewed session isolation"
        ),
    )
    api_invocations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        api_invocations.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "target-session-one"})
        if request.url.path.endswith("/message"):
            return httpx.Response(
                200,
                json={
                    "reply": "objective reached",
                    "signals": {
                        "objective": True,
                        "refusal": False,
                        "progress": False,
                        "invalid-premise": False,
                    },
                },
            )
        return httpx.Response(200, json={"closed": True})

    provider = Provider()
    report_manifest: CampaignManifest | None = None
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("joined Crescendo restart")
            report_manifest = CampaignManifest.create(
                id=campaign_id,
                session_id=str(session.id),
                objective="Run the reviewed Crescendo strategy",
                profile=CampaignProfile.RECOMMENDED,
                target=ReviewedTargetAdapter(
                    id=target_id,
                    interface="api",
                    endpoint="https://target.test/api/message",
                ),
                provider_id="provider",
                provider_model="model",
                provider_effort="default",
                authority_ids=("target:reviewed",),
                catalogue=(),
                skill_documents=(),
                tool_ids=("target_request",),
                budget=AssignmentBudget(max_seconds=30),
            )
            report_context = AssignmentContext(
                target_profile_id=target_id,
                techniques=(
                    AssignmentTechnique(
                        id="case:crescendo",
                        objective="Run the reviewed Crescendo strategy",
                        tags=frozenset({"prompt-injection", "text", "multi"}),
                        guidance_ids=("attack-catalogue:crescendo",),
                        semantic_id=contract["semantic_technique_id"],
                        language="en",
                        domain_id="prompt-injection",
                        strategy_id="crescendo",
                        strategy_contract=tuple(sorted(contract.items())),
                    ),
                    AssignmentTechnique(
                        id="case:crescendo:blocked",
                        objective="Blocked additional Crescendo strategy",
                        tags=frozenset({"prompt-injection", "text", "multi"}),
                        guidance_ids=("attack-catalogue:crescendo",),
                        semantic_id=blocked_contract["semantic_technique_id"],
                        language="en",
                        domain_id="prompt-injection",
                        strategy_id="crescendo",
                        comparison_supported=False,
                        comparison_block_reason=(
                            "campaign-global API session already owns one Crescendo chain; "
                            "additional cells require reviewed session isolation"
                        ),
                        strategy_contract=tuple(sorted(blocked_contract.items())),
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
            scheduler = FaultAfterAcknowledgementStore(connection)
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
                )
                live = await service.submit(
                    str(session.id),
                    (assignment, blocked_assignment),
                    max_workers=1,
                    minimum_workers=1,
                )
                CampaignStore(connection).add_wave(campaign_id, 0, "initial", live.id)
                assert live.execution is not None
                acknowledged = await asyncio.to_thread(scheduler.acknowledged.wait, 5)
                assert acknowledged, (
                    api_invocations,
                    None if not live.execution.done() else live.execution.exception(),
                    scheduler.list_results(live.id),
                )
                first_attempt_id = scheduler.faulted_attempt_id
                assert first_attempt_id is not None
                live.execution.cancel()
                scheduler.release_fault.set()
                await asyncio.gather(live.execution, return_exceptions=True)
                connection.execute(
                    """UPDATE merlin_storage.scheduler_attempts
                       SET lease_expires_at = now() - interval '1 second' WHERE id = %s""",
                    (first_attempt_id,),
                )
                connection.commit()
                schedule_id = live.id
                session_id = str(session.id)

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
                reopened = await replacement.reopen(schedule_id)
                assert reopened.execution is not None
                await asyncio.wait_for(reopened.execution, timeout=10)

            attempts = scheduler.list_attempts(schedule_id)
            worker_attempts = [
                item for item in attempts if item["node_id"] == f"worker:{assignment_id}"
            ]
            assert len(worker_attempts) >= 2
            assert worker_attempts[0]["state"] == "expired"
            assert worker_attempts[-1]["state"] == "completed"
            assert worker_attempts[-1]["id"] != first_attempt_id
            # Sealed-adapter resume (e901dc7) adopts the acknowledged effect and
            # completes the fresh session in a single turn: one model call and one
            # target message fewer than the pre-sealing replay.
            assert provider.calls == 3
            assert api_invocations.count("message") == 2
            assert api_invocations.count("cleanup") == 2
            gate = scheduler.crescendo_gate(schedule_id)
            assert gate["positive"] == ()
            assert gate["ambiguous"] == (assignment_id, blocked_assignment_id)
            assert gate["blocked"] == ()
            receipt = gate["strategies"][assignment_id]
            second_receipt = gate["strategies"][blocked_assignment_id]
            assert second_receipt["steps"]
            assert second_receipt["terminal_reason"] == "ambiguous"
            assert receipt["terminal_reason"] is None
            assert "receipt_digest" not in receipt
            assert str(receipt["steps"][0]["scheduler_attempt_id"]) == first_attempt_id
            assert receipt["steps"][0]["decision"] == "success"
            assert receipt["steps"][0]["target_session_id"] == (
                f"api:{campaign_id}:{target_id}:scope:{assignment_id}:generation:1"
            )
            assert session_id == str(scheduler.load_schedule(schedule_id)["session_id"])

        assert report_manifest is not None
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {"base_url": "http://127.0.0.1:9/v1", "model": "model"},
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
            revision = backend._report_repository.list(campaign_id)[-1]  # type: ignore[attr-defined]
            internal = backend._report_repository.get(revision.revision_id)  # type: ignore[attr-defined]
            assert internal.stop_reason == "budget_exhausted"
            assert internal.report_state == "incomplete"
            assert internal.coverage["confirmed"] == 0
            assert internal.coverage["blocked"] == 0
            assert internal.coverage["unclear"] == 2
            lineage = [row for row in internal.lineage if row.get("pack") == "adaptive-strategy"]
            assert len(lineage) == 2
            assert lineage[0]["attempt"] == first_attempt_id
            assert lineage[0]["effect_receipt"] == receipt["steps"][0]["effect_id"]
            assert lineage[0]["exchange"] == receipt["steps"][0]["exchange_id"]
            assert lineage[0]["stage_receipt"] == receipt["steps"][0]["receipt_digest"]
            assert [row["id"] for row in internal.exchanges].count(lineage[0]["exchange"]) == 1
            technique = next(row for row in internal.techniques if row["id"] == "case:crescendo")
            assert technique["outcome"] == "unclear"
            assert "crescendo_receipt" not in technique
            blocked_technique = next(
                row for row in internal.techniques if row["id"] == "case:crescendo:blocked"
            )
            assert blocked_technique["outcome"] == "unclear"
            assert "ambiguous" in blocked_technique["rationale"]

            customer = json.loads(
                DurableReportService(backend._report_repository).export(  # type: ignore[attr-defined]
                    revision.revision_id, "json", "customer"
                )
            )
            serialized = json.dumps(customer, sort_keys=True)
            for secret in (
                first_attempt_id,
                str(worker_attempts[1]["id"]),
                str(receipt["steps"][0]["effect_id"]),
                str(receipt["steps"][0]["call_id"]),
                str(receipt["steps"][0]["receipt_digest"]),
            ):
                assert secret not in serialized
            assert "reviewed escalating prompt" in serialized
            assert "objective reached" not in serialized
            interaction_exchanges = [
                row for row in internal.exchanges if row.get("interaction") is True
            ]
            assert interaction_exchanges
            assert all("response" not in row for row in interaction_exchanges)
            assert all(
                row["response_summary"] == {"kind": "str", "utf8_bytes": 19}
                for row in interaction_exchanges
            )
            assert all(
                row["response"] == ""
                for row in customer["exchanges"]
                if row["request"] == "reviewed escalating prompt"
            )
            customer_lineage = [row for row in customer["lineage"] if row.get("stage") == "probe"]
            assert len(customer_lineage) == 2
            assert all(row["attempt"] == "[redacted]" for row in customer_lineage)
            assert all(row["effect_receipt"] == "[redacted]" for row in customer_lineage)
            assert all(row["stage_receipt"] == "[redacted]" for row in customer_lineage)
            assert all(
                [row["id"] for row in customer["exchanges"]].count(lineage_row["exchange"]) == 1
                for lineage_row in customer_lineage
            )
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_joined_crescendo_intent_only_restart_is_ambiguous_without_resend(
    tmp_path: Path,
) -> None:
    """An INTENT-only crash terminalizes provisionally without touching the target."""

    class InitialProvider:
        capabilities = SimpleNamespace(vision=False, context_window_tokens=32_000)

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, _request):
            self.calls += 1
            yield ToolCall(
                call_id="intent-only-call",
                name="target_request",
                arguments={"input": "prompt that must never reach the target"},
            )
            yield ModelCompleted(finish_reason="tool_calls")

    class ReplacementProvider:
        capabilities = SimpleNamespace(vision=False, context_window_tokens=32_000)

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, _request):
            self.calls += 1
            raise AssertionError("terminal INTENT recovery must not invoke the provider")
            yield  # pragma: no cover - preserve async-generator shape

    class BlockingIntentStore(PostgresToolEffectStore):
        def __init__(self, repository: ToolEffectRepository) -> None:
            super().__init__(repository)
            self.persisted = asyncio.Event()
            self.release = asyncio.Event()

        async def begin(self, record):
            result = await super().begin(record)
            self.persisted.set()
            await self.release.wait()
            return result

    async def allow(*_args) -> bool:
        return True

    data_dir = tmp_path / "postgres-intent-only-crescendo"
    campaign_id = "campaign/intent-only-crescendo"
    assignment_id = f"{campaign_id}/crescendo"
    target_id = "target-reviewed-api"
    oracle_digests = {
        purpose: digest(f"intent-oracle:{purpose}")
        for purpose in ("objective", "refusal", "progress", "invalid-premise")
    }
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
                "idempotency_key": "start-intent-only",
                "replay_after_ambiguous_result": True,
            },
            {
                "step_id": "message",
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
                "idempotency_key": "cleanup-intent-only",
                "replay_after_ambiguous_result": True,
            },
        ],
    }
    contract = {
        "version": "merlin.crescendo.v1",
        "semantic_technique_id": "semantic:crescendo:intent-only",
        "objective_oracle_digest": oracle_digests["objective"],
        "refusal_oracle_digest": oracle_digests["refusal"],
        "progress_oracle_digest": oracle_digests["progress"],
        "invalid_premise_oracle_digest": oracle_digests["invalid-premise"],
        "reviewed_adapter_digest": executable_adapter_digest(adapter),
        "modality": "text",
        "language": "en",
        "max_target_turns": "5",
        "plateau_turns": "2",
    }
    assignment = WorkerAssignment(
        id=assignment_id,
        objective="Run the reviewed Crescendo strategy",
        profile_id="injection",
        target_profile_id=target_id,
        technique_ids=("case:crescendo",),
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
        strategy_id="crescendo",
        semantic_technique_id=contract["semantic_technique_id"],
        strategy_contract=tuple(sorted(contract.items())),
    )
    http_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        raise AssertionError("INTENT-only recovery must never invoke the target HTTP adapter")

    initial_provider = InitialProvider()
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("INTENT-only Crescendo restart")
            CampaignStore(connection).prepare(
                campaign_id,
                str(session.id),
                {"manifest": {"id": campaign_id}, "assignment_context": {}},
                "sha256:reviewed",
            )
            scheduler = DurableSchedulerStore(connection)
            blocking_effects = BlockingIntentStore(ToolEffectRepository(connection))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                target = ReviewedApiTargetTool(
                    CampaignApiSessionRepository(connection),
                    lambda _campaign_id: adapter,
                    {},
                    client=client,
                )
                broker = ToolBroker((target,), confirmer=allow, effect_store=blocking_effects)
                service = DurableAgentService(
                    initial_provider,
                    broker,
                    scheduler,
                    model="model",
                    workspace_root=str(tmp_path),
                    lease_seconds=0.1,
                )
                live = await service.submit(
                    str(session.id), (assignment,), max_workers=1, minimum_workers=1
                )
                assert live.execution is not None
                await asyncio.wait_for(blocking_effects.persisted.wait(), timeout=5)
                attempts = scheduler.list_attempts(live.id)
                first_attempt = next(
                    item for item in attempts if item["node_id"] == f"worker:{assignment_id}"
                )
                first_attempt_id = str(first_attempt["id"])
                live.execution.cancel()
                await asyncio.gather(live.execution, return_exceptions=True)
                connection.execute(
                    """UPDATE merlin_storage.scheduler_attempts
                       SET lease_expires_at = now() - interval '1 second' WHERE id = %s""",
                    (first_attempt_id,),
                )
                connection.commit()
                effects = ToolEffectRepository(connection).list_for_attempt(first_attempt_id)
                assert len(effects) == 1
                assert effects[0].state is ToolEffectState.INTENT
                schedule_id = live.id

    replacement_provider = ReplacementProvider()
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
                broker = ToolBroker(
                    (target,),
                    confirmer=allow,
                    effect_store=PostgresToolEffectStore(ToolEffectRepository(connection)),
                )
                replacement = DurableAgentService(
                    replacement_provider,
                    broker,
                    scheduler,
                    model="model",
                    workspace_root=str(tmp_path),
                    lease_seconds=0.1,
                    campaign_cleanup=target.cleanup_assignment,
                )
                reopened = await replacement.reopen(schedule_id)
                assert reopened.execution is not None
                await asyncio.wait_for(reopened.execution, timeout=10)

            worker_attempts = [
                item
                for item in scheduler.list_attempts(schedule_id)
                if item["node_id"] == f"worker:{assignment_id}"
            ]
            assert len(worker_attempts) == 2
            assert worker_attempts[0]["state"] == "expired"
            assert worker_attempts[1]["state"] == "completed"
            assert str(worker_attempts[1]["id"]) != first_attempt_id
            assert initial_provider.calls == 1
            assert replacement_provider.calls == 0
            assert http_calls == 0
            effects = ToolEffectRepository(connection).list_for_attempt(first_attempt_id)
            assert len(effects) == 1
            assert effects[0].state is ToolEffectState.INTENT
            gate = scheduler.crescendo_gate(schedule_id)
            assert gate["complete"] == ()
            assert gate["ambiguous"] == (assignment_id,)
            receipt = gate["strategies"][assignment_id]
            assert receipt["terminal_reason"] == "ambiguous"
            assert receipt["verdict"] == "ambiguous"
            assert receipt["target_session_id"] is None
            assert receipt["steps"] == ()
            assert receipt["step_receipt_digests"] == []
