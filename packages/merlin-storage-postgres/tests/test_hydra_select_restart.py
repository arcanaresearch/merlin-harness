import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from merlin_agents import (
    AssignmentBudget,
    AssignmentContext,
    AssignmentTechnique,
    CampaignManifest,
    CampaignProfile,
    ReviewedTargetAdapter,
)
from merlin_agents.campaign_service import _json_value
from merlin_agents.hydra_select import HydraSelectContract, HydraSelectState
from merlin_config import Config, CredentialStore
from merlin_contracts import (
    HYDRA_SELECT_CATALOG_DIGEST,
    ToolEffectRecord,
    ToolEffectState,
    trusted_hydra_select_mechanisms,
)
from merlin_findings import DurableReportService
from merlin_storage_postgres import (
    CampaignStore,
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    SessionRepository,
    ToolEffectRepository,
)
from merlin_tui.bootstrap import build_backend


def _digest(value: object) -> str:
    return (
        "sha256:"
        + sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )


def _contract() -> tuple[HydraSelectContract, dict[str, str]]:
    mechanisms = trusted_hydra_select_mechanisms()
    review = HydraSelectContract(
        version="merlin.hydra-select.v1",
        semantic_technique_id="semantic:hydra:restart",
        goal_digest=_digest("goal"),
        objective_oracle_digest=_digest("oracle"),
        reviewed_adapter_digest=_digest("adapter"),
        attacker_route_digest=_digest(
            {
                "provider_id": "test",
                "model": "test",
                "role": "worker",
                "tool_transport": "buffered",
            }
        ),
        attacker_tool_transport="buffered",
        target_profile_id="target-1",
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
        "version": review.version,
        "semantic_technique_id": review.semantic_technique_id,
        "goal_digest": review.goal_digest,
        "objective_oracle_digest": review.objective_oracle_digest,
        "reviewed_adapter_digest": review.reviewed_adapter_digest,
        "attacker_route_digest": review.attacker_route_digest,
        "attacker_tool_transport": review.attacker_tool_transport,
        "target_profile_id": review.target_profile_id,
        "session_policy": review.session_policy,
        "modality": review.modality,
        "language": review.language,
        "mechanisms_json": json.dumps(mechanisms, separators=(",", ":")),
        "mechanism_catalog_digest": review.mechanism_catalog_digest,
        "proposed_count": str(review.proposed_count),
        "selected_count": str(review.selected_count),
        "max_candidate_chars": str(review.max_candidate_chars),
        "stop_on_positive": "true",
    }
    return review, values


def _observation(review: HydraSelectContract, matched: bool) -> dict[str, object]:
    binding = {
        "adapter_digest": review.reviewed_adapter_digest,
        "step_id": "interaction",
        "oracle_digest": review.objective_oracle_digest,
        "pointer": "/matched",
        "match_values": [True],
        "modality": "text",
        "turn": "multi",
        "language": "en",
        "purpose": "objective",
    }
    observation = {
        "schema": "merlin.api-oracle-observation.v2",
        **binding,
        "binding_digest": _digest(binding),
        "selected_digest": _digest({"matched": matched}),
        "matched": matched,
    }
    observation["observation_digest"] = _digest(observation)
    return observation


def _acknowledge(
    repository: ToolEffectRepository,
    *,
    session_id: str,
    attempt_id: str,
    assignment_id: str,
    campaign_id: str,
    call_id: str,
    arguments: dict[str, object],
    exchange_id: str,
    review: HydraSelectContract,
    matched: bool,
) -> None:
    effect, created = repository.begin(
        ToolEffectRecord(
            idempotency_key=sha256(f"effect:{call_id}".encode()).hexdigest(),
            session_id=session_id,
            call_id=call_id,
            agent_id=assignment_id,
            attempt_id=attempt_id,
            campaign_id=campaign_id,
            target_id="target-1",
            tool_name="target_request",
            effect="target_network",
            arguments_digest=sha256(
                json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            arguments_payload=arguments,
            state=ToolEffectState.INTENT,
        )
    )
    assert created
    repository.finish(
        effect.model_copy(
            update={
                "state": ToolEffectState.ACKNOWLEDGED,
                "result_content": f"reply {call_id}",
                "result_is_error": False,
                "result_metadata": {
                    "campaign_id": campaign_id,
                    "target_profile_id": "target-1",
                    "target_session_id": "api:hydra-session",
                    "target_exchanges": [
                        {
                            "id": exchange_id,
                            "interaction": True,
                            "cleanup": False,
                            "request": arguments,
                            "response": {"matched": matched},
                            "oracle_observations": [_observation(review, matched)],
                        }
                    ],
                },
            }
        )
    )


def test_hydra_plan_is_sealed_before_intent_and_old_intent_is_never_replayed(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-hydra-intent"
    review, values = _contract()
    campaign_id = "campaign/hydra-restart"
    assignment_id = f"{campaign_id}/hydra/one"
    node_id = f"worker:{assignment_id}"
    assignment = {
        "id": assignment_id,
        "objective": "Exercise the reviewed Hydra semantic cell",
        "profile_id": "adversarial-explorer",
        "strategy_id": "hydra-select",
        "strategy_contract": sorted(values.items()),
        "semantic_technique_id": review.semantic_technique_id,
        "technique_ids": ["case:hydra"],
        "target_profile_id": "target-1",
        "comparison_supported": True,
        "provider_id": "test",
        "provider_model": "test",
        "guidance_ids": [],
        "tool_ids": ["target_request"],
        "skill_ids": [],
        "authority_ids": ["target:target-1"],
        "budget": asdict(AssignmentBudget(8, 8_000, 120)),
        "input_schema": "merlin.worker-input/v1",
        "output_schema": "merlin.worker-result/v1",
    }
    proposal = {
        "candidates": [
            {"prompt": "候选提示", "rationale": "rationale 0"},
            {"prompt": "candidate 1", "rationale": "rationale 1"},
        ]
    }

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("Hydra INTENT restart")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {
                    "assignments": [assignment],
                    "admitted": {"topology_digest": "sha256:fixture"},
                },
                {"completed": []},
                [node_id],
                ready_nodes=[node_id],
            )
            first = store.claim(
                schedule_id,
                node_id,
                "agent-one",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            state = HydraSelectState.start(review, first.id).with_plan(review, proposal)
            state = state.with_pending("hydra-call-0")
            checkpoint = {"strategy_state": state.dump(), "messages": []}
            store.commit_partial(
                first.id,
                first.generation,
                result={"worker_checkpoint": checkpoint},
            )
            assert (
                connection.execute(
                    "SELECT count(*) AS count FROM merlin_storage.hydra_select_plan_receipts"
                ).fetchone()["count"]
                == 1
            )
            plan_receipt = connection.execute(
                "SELECT receipt FROM merlin_storage.hydra_select_plan_receipts"
            ).fetchone()["receipt"]
            assert len(plan_receipt["candidates"]) == 2
            assert len(plan_receipt["selected_candidate_ids"]) == 2
            assert plan_receipt["evaluation_uncertainties"]
            assert (
                connection.execute(
                    "SELECT count(*) AS count FROM merlin_storage.tool_effects"
                ).fetchone()["count"]
                == 0
            )

            arguments = state.pending_arguments
            assert arguments is not None
            effect_id = sha256(b"hydra-old-intent").hexdigest()
            _, created = ToolEffectRepository(connection).begin(
                ToolEffectRecord(
                    idempotency_key=effect_id,
                    session_id=session.id,
                    call_id="hydra-call-0",
                    agent_id=assignment_id,
                    attempt_id=first.id,
                    campaign_id=campaign_id,
                    target_id="target-1",
                    tool_name="target_request",
                    effect="target_network",
                    arguments_digest=sha256(
                        json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    arguments_payload=arguments,
                    state=ToolEffectState.INTENT,
                )
            )
            assert created
            connection.execute(
                "UPDATE merlin_storage.scheduler_attempts "
                "SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                (first.id,),
            )
            connection.commit()
            store.expire(first.id, first.generation)
            replacement = store.claim(
                schedule_id,
                node_id,
                "agent-two",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            recovered = store.hydra_select_state(
                schedule_id,
                assignment_id,
                replacement.id,
                state.dump(),
            )
            assert recovered is not None
            assert recovered["terminal_reason"] == "ambiguous"
            assert recovered["blocking_effect_id"] == effect_id
            assert recovered["blocking_effect_attempt_id"] == first.id
            assert recovered["recovery"]["decision"] == "ambiguous-intent-no-resend"
            store.commit_partial(
                replacement.id,
                replacement.generation,
                result={
                    "worker_checkpoint": {
                        "strategy_state": recovered,
                        "messages": [],
                    }
                },
            )
            store.settle_attempt(
                replacement.id,
                replacement.generation,
                "failed",
                reason="effect replay forbidden",
                checkpoint={"completed": []},
            )
            gate = store.hydra_select_gate(schedule_id)
            assert gate["execution_complete"] == ()
            assert gate["ambiguous"] == (assignment_id,)
            receipt = gate["strategies"][assignment_id]
            assert receipt["terminal_attempt_id"] == replacement.id
            assert receipt["blocking_effect_attempt_id"] == first.id
            assert receipt["blocking_effect_id"] == effect_id
            assert receipt["steps"] == ()
            assert (
                connection.execute(
                    "SELECT count(*) AS count FROM merlin_storage.tool_effects"
                ).fetchone()["count"]
                == 1
            )

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            gate = DurableSchedulerStore(connection).hydra_select_gate(schedule_id)
            assert gate["execution_complete"] == ()
            assert gate["ambiguous"] == (assignment_id,)
            assert gate["strategies"][assignment_id]["blocking_effect_id"] == effect_id
            assert (
                connection.execute(
                    "SELECT count(*) AS count FROM merlin_storage.hydra_select_terminal_receipts"
                ).fetchone()["count"]
                == 1
            )


@pytest.mark.parametrize("extra_intent", (False, True))
@pytest.mark.asyncio
async def test_hydra_acknowledged_prefix_restores_cursor_and_exact_exchange_receipts(
    tmp_path: Path, extra_intent: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / f"postgres-hydra-ack-prefix-{extra_intent}"
    review, values = _contract()
    campaign_id = "campaign/hydra-ack"
    assignment_id = f"{campaign_id}/hydra/one"
    node_id = f"worker:{assignment_id}"
    assignment = {
        "id": assignment_id,
        "objective": "Exercise the reviewed Hydra semantic cell",
        "profile_id": "adversarial-explorer",
        "strategy_id": "hydra-select",
        "strategy_contract": sorted(values.items()),
        "semantic_technique_id": review.semantic_technique_id,
        "technique_ids": ["case:hydra"],
        "target_profile_id": "target-1",
        "comparison_supported": True,
        "provider_id": "test",
        "provider_model": "test",
        "guidance_ids": [],
        "tool_ids": ["target_request"],
        "skill_ids": [],
        "authority_ids": ["target:target-1"],
        "budget": asdict(AssignmentBudget(8, 8_000, 120)),
        "input_schema": "merlin.worker-input/v1",
        "output_schema": "merlin.worker-result/v1",
    }
    proposal = {
        "candidates": [
            {"prompt": f"candidate {index}", "rationale": f"rationale {index}"}
            for index in range(6)
        ]
    }
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("Hydra ACK prefix")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {
                    "assignments": [assignment],
                    "admitted": {"topology_digest": "sha256:fixture"},
                },
                {"completed": []},
                [node_id],
                ready_nodes=[node_id],
            )
            manifest = CampaignManifest.create(
                id=campaign_id,
                session_id=str(session.id),
                objective="Exercise the reviewed Hydra semantic cell",
                profile=CampaignProfile.EXHAUSTIVE,
                target=ReviewedTargetAdapter(
                    "target-1", "http_api", "https://target.test/api", "target-model"
                ),
                provider_id="test",
                provider_model="test",
                provider_effort="default",
                authority_ids=(f"session:{session.id}", "target:target-1"),
                catalogue=(),
                skill_documents=(),
                tool_ids=("target_request",),
                budget=AssignmentBudget(8, 8_000, 120),
            )
            context = AssignmentContext(
                target_profile_id="target-1",
                techniques=(
                    AssignmentTechnique(
                        id="case:hydra",
                        objective="Exercise the reviewed Hydra semantic cell",
                        tags=frozenset({"text", "multi", "conversational"}),
                        guidance_ids=(),
                        semantic_id=review.semantic_technique_id,
                        strategy_id="hydra-select",
                        strategy_contract=tuple(sorted(values.items())),
                    ),
                ),
                tool_ids=("target_request",),
                skill_ids=(),
                provider_id="test",
                authority_ids=manifest.authority_ids,
                budget=manifest.budget,
                input_schema="merlin.worker-input/v1",
                output_schema="merlin.worker-result/v1",
            )
            campaigns = CampaignStore(connection)
            campaigns.prepare(
                campaign_id,
                str(session.id),
                {
                    "manifest": _json_value(asdict(manifest)),
                    "assignment_context": _json_value(asdict(context)),
                },
                manifest.digest,
            )
            campaigns.add_wave(campaign_id, 0, "initial", schedule_id)
            first = store.claim(
                schedule_id,
                node_id,
                "agent-one",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            original = HydraSelectState.start(review, first.id).with_plan(review, proposal)
            original = original.with_pending("hydra-00")
            store.commit_partial(
                first.id,
                first.generation,
                result={
                    "worker_checkpoint": {
                        "strategy_state": original.dump(),
                        "messages": [],
                    }
                },
            )
            repository = ToolEffectRepository(connection)
            assert original.pending_arguments is not None
            _acknowledge(
                repository,
                session_id=session.id,
                attempt_id=first.id,
                assignment_id=assignment_id,
                campaign_id=campaign_id,
                call_id="hydra-00",
                arguments=original.pending_arguments,
                exchange_id="message",
                review=review,
                matched=False,
            )
            connection.execute(
                "UPDATE merlin_storage.scheduler_attempts "
                "SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                (first.id,),
            )
            connection.commit()
            store.expire(first.id, first.generation)
            replacement = store.claim(
                schedule_id,
                node_id,
                "agent-two",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            recovered = store.hydra_select_state(
                schedule_id, assignment_id, replacement.id, original.dump()
            )
            assert recovered is not None
            assert len(recovered["steps"]) == 1
            assert recovered["steps"][0]["exchange_id"] == "hydra-00:message"
            state = HydraSelectState.restore(recovered, review, replacement.id)
            for index in (1, 2):
                state = state.with_pending(f"hydra-{index:02d}")
                assert state.pending_arguments is not None
                _acknowledge(
                    repository,
                    session_id=session.id,
                    attempt_id=replacement.id,
                    assignment_id=assignment_id,
                    campaign_id=campaign_id,
                    call_id=f"hydra-{index:02d}",
                    arguments=state.pending_arguments,
                    exchange_id="message",
                    review=review,
                    matched=False,
                )
                recovered = store.hydra_select_state(
                    schedule_id, assignment_id, replacement.id, state.dump()
                )
                assert recovered is not None
                state = HydraSelectState.restore(recovered, review, replacement.id)
            assert state.terminal_reason is None
            assert tuple(step.exchange_id for step in state.steps) == (
                "hydra-00:message",
                "hydra-01:message",
                "hydra-02:message",
            )
            if extra_intent:
                tail_arguments = {"input": "must never be replayed"}
                _, created = repository.begin(
                    ToolEffectRecord(
                        idempotency_key=sha256(b"hydra-false-tail-intent").hexdigest(),
                        session_id=session.id,
                        call_id="hydra-99",
                        agent_id=assignment_id,
                        attempt_id=replacement.id,
                        campaign_id=campaign_id,
                        target_id="target-1",
                        tool_name="target_request",
                        effect="target_network",
                        arguments_digest=sha256(
                            json.dumps(
                                tail_arguments, sort_keys=True, separators=(",", ":")
                            ).encode()
                        ).hexdigest(),
                        arguments_payload=tail_arguments,
                        state=ToolEffectState.INTENT,
                    )
                )
                assert created
                recovered = store.hydra_select_state(
                    schedule_id, assignment_id, replacement.id, state.dump()
                )
                assert recovered is not None
                state = HydraSelectState.restore(recovered, review, replacement.id)
                assert state.terminal_reason == "ambiguous"
            terminal_result = {
                "worker_checkpoint": {"strategy_state": state.dump(), "messages": []}
            }
            store.commit_reducer(
                replacement.id,
                replacement.generation,
                schema_id="schema/worker-result",
                result=terminal_result,
                checkpoint={"completed": [assignment_id]},
                target_session_id="api:hydra-session",
            )
            gate = store.hydra_select_gate(schedule_id)
            assert gate["negative"] == ()
            strategy = gate["strategies"][assignment_id]
            assert strategy["execution_complete"] is (not extra_intent)
            assert strategy["evidence_verdict"] == "ambiguous"
            assert len(strategy["steps"]) == 3
            assert all(step["receipt_digest"].startswith("sha256:") for step in strategy["steps"])
            assert tuple(step["exchange_id"] for step in strategy["steps"]) == (
                "hydra-00:message",
                "hydra-01:message",
                "hydra-02:message",
            )
            assert connection.execute(
                "SELECT count(*) AS count FROM merlin_storage.tool_effects"
            ).fetchone()["count"] == (4 if extra_intent else 3)

        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "test",
                    },
                    "permission_mode": "auto",
                },
            ),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
        backend = build_backend(tmp_path / f"hydra-report-{extra_intent}")
        try:
            report_stop_reason = "interrupted" if extra_intent else "capped"
            await backend._campaigns._finish_campaign(  # type: ignore[attr-defined]
                campaign_id,
                "failed" if extra_intent else "completed",
                report_stop_reason,
            )
            await backend._campaigns._report_runner(  # type: ignore[attr-defined]
                campaign_id,
                manifest,
                report_stop_reason,
            )
            revision = backend._report_repository.list(campaign_id)[-1]  # type: ignore[attr-defined]
            internal = backend._report_repository.get(  # type: ignore[attr-defined]
                revision.revision_id
            )
            technique = next(
                row for row in internal.techniques if row["strategy_id"] == "hydra-select"
            )
            lineage = [
                row for row in internal.lineage if row.get("entry") == "merlin.hydra-select.v1"
            ]
            assert technique["outcome"] == "unclear"
            assert internal.stop_reason == report_stop_reason
            assert internal.report_state == "incomplete"
            assert len(lineage) == 3
            assert all(row["stage_receipt"].startswith("sha256:") for row in lineage)
            internal_exchange_ids = {row["id"] for row in internal.exchanges}
            assert all(row["exchange"] in internal_exchange_ids for row in lineage)
            customer = json.loads(
                DurableReportService(backend._report_repository).export(  # type: ignore[attr-defined]
                    revision.revision_id, "json", "customer"
                )
            )
            customer_lineage = [
                row for row in customer["lineage"] if row.get("effect_receipt") == "[redacted]"
            ]
            assert len(customer_lineage) == 3
            assert all(row["attempt"] == "[redacted]" for row in customer_lineage)
            customer_exchange_ids = {row["id"] for row in customer["exchanges"]}
            assert all(row["exchange"] in customer_exchange_ids for row in customer_lineage)
        finally:
            await backend.close()


def test_hydra_positive_prefix_with_later_intent_is_positive_but_incomplete(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "postgres-hydra-positive-intent"
    review, values = _contract()
    campaign_id = "campaign/hydra-positive-intent"
    assignment_id = f"{campaign_id}/hydra/one"
    node_id = f"worker:{assignment_id}"
    assignment = {
        "id": assignment_id,
        "strategy_id": "hydra-select",
        "strategy_contract": sorted(values.items()),
        "semantic_technique_id": review.semantic_technique_id,
        "technique_ids": ["case:hydra"],
        "target_profile_id": "target-1",
        "comparison_supported": True,
    }
    proposal = {
        "candidates": [
            {"prompt": f"candidate {index}", "rationale": f"rationale {index}"}
            for index in range(6)
        ]
    }
    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("Hydra positive then INTENT")
            store = DurableSchedulerStore(connection)
            schedule_id = store.create_schedule(
                session.id,
                {"assignments": [assignment]},
                {"completed": []},
                [node_id],
                ready_nodes=[node_id],
            )
            attempt = store.claim(
                schedule_id,
                node_id,
                "agent-one",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            state = HydraSelectState.start(review, attempt.id).with_plan(review, proposal)
            state = state.with_pending("hydra-00")
            store.commit_partial(
                attempt.id,
                attempt.generation,
                result={
                    "worker_checkpoint": {
                        "strategy_state": state.dump(),
                        "messages": [],
                    }
                },
            )
            repository = ToolEffectRepository(connection)
            assert state.pending_arguments is not None
            _acknowledge(
                repository,
                session_id=session.id,
                attempt_id=attempt.id,
                assignment_id=assignment_id,
                campaign_id=campaign_id,
                call_id="hydra-00",
                arguments=state.pending_arguments,
                exchange_id="message",
                review=review,
                matched=True,
            )
            next_candidate = state.plan.selected()[1]
            tail_arguments = {"input": next_candidate.prompt}
            _, created = repository.begin(
                ToolEffectRecord(
                    idempotency_key=sha256(b"hydra-positive-tail-intent").hexdigest(),
                    session_id=session.id,
                    call_id="hydra-01",
                    agent_id=assignment_id,
                    attempt_id=attempt.id,
                    campaign_id=campaign_id,
                    target_id="target-1",
                    tool_name="target_request",
                    effect="target_network",
                    arguments_digest=sha256(
                        json.dumps(tail_arguments, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    arguments_payload=tail_arguments,
                    state=ToolEffectState.INTENT,
                )
            )
            assert created
            recovered = store.hydra_select_state(
                schedule_id, assignment_id, attempt.id, state.dump()
            )
            assert recovered is not None
            assert recovered["terminal_reason"] == "ambiguous"
            assert len(recovered["steps"]) == 1
            store.commit_partial(
                attempt.id,
                attempt.generation,
                result={
                    "worker_checkpoint": {
                        "strategy_state": recovered,
                        "messages": [],
                    }
                },
            )
            store.settle_attempt(
                attempt.id,
                attempt.generation,
                "failed",
                reason="unexpected effect tail",
                checkpoint={"completed": []},
                target_session_id="api:hydra-session",
            )
            gate = store.hydra_select_gate(schedule_id)
            strategy = gate["strategies"][assignment_id]
            assert strategy["evidence_verdict"] == "ambiguous"
            assert strategy["steps"][0]["objective_matched"] is True
            assert strategy["execution_complete"] is False
            assert gate["complete"] == ()
            assert gate["positive"] == ()
            assert gate["execution_complete"] == ()
