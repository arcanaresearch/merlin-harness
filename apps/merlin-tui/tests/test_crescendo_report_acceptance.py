import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
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
from merlin_config import Config, CredentialStore
from merlin_contracts import ToolEffectRecord, ToolEffectState
from merlin_findings import DurableReportService
from merlin_storage_postgres import (
    CampaignStore,
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    ToolEffectRepository,
)
from merlin_tui.bootstrap import build_backend


def _digest(value: object) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def _contract(adapter_digest: str) -> dict[str, str]:
    return {
        "version": "merlin.crescendo.v1",
        "semantic_technique_id": "semantic:crescendo-report",
        "objective_oracle_digest": _digest("objective"),
        "refusal_oracle_digest": _digest("refusal"),
        "progress_oracle_digest": _digest("progress"),
        "invalid_premise_oracle_digest": _digest("invalid-premise"),
        "reviewed_adapter_digest": adapter_digest,
        "modality": "text",
        "language": "en",
        "max_target_turns": "5",
        "plateau_turns": "2",
    }


def _observations(
    contract: dict[str, str],
    *,
    valid: bool = True,
    objective_match: bool = True,
    target_authoritative: bool = False,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for purpose in ("objective", "refusal", "progress", "invalid-premise"):
        binding = {
            "adapter_digest": contract["reviewed_adapter_digest"],
            "step_id": "interaction",
            "oracle_digest": contract[f"{purpose.replace('-', '_')}_oracle_digest"],
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
            "binding_digest": _digest(binding),
            "selected_digest": _digest(purpose),
            "matched": purpose == "objective" and objective_match,
        }
        if purpose == "objective" and target_authoritative:
            observation["target_authoritative"] = True
        observation["observation_digest"] = _digest(observation)
        rows.append(observation)
    if not valid:
        rows.pop()
    return rows


async def _campaign_with_effect(
    backend: object,
    database: Database,
    tmp_path: Path,
    *,
    campaign_id: str,
    valid_receipt: bool,
    terminal_kind: str | None = None,
    objective_match: bool = True,
    target_authoritative: bool = False,
    context_strategy_id: str = "crescendo",
    create_target_effect: bool = True,
    complete_cleanup: bool = False,
    target_tool_name: str = "target_request",
) -> tuple[CampaignManifest, str, str, str, str]:
    session_id = await backend.kernel.start()  # type: ignore[attr-defined]
    target_id = f"target-{campaign_id.rsplit('/', 1)[-1]}"
    technique_id = f"technique-{campaign_id.rsplit('/', 1)[-1]}"
    adapter_digest = _digest(f"adapter:{campaign_id}")
    contract = _contract(adapter_digest)
    manifest = CampaignManifest.create(
        id=campaign_id,
        session_id=session_id,
        objective="Exercise a reviewed Crescendo strategy",
        profile=CampaignProfile.RECOMMENDED,
        target=ReviewedTargetAdapter(
            id=target_id, interface="api", endpoint="https://target.invalid/chat"
        ),
        provider_id="test",
        provider_model="test",
        provider_effort="default",
        authority_ids=(),
        catalogue=(),
        skill_documents=(),
        tool_ids=(target_tool_name,),
        budget=AssignmentBudget(),
    )
    context = AssignmentContext(
        target_profile_id=target_id,
        techniques=(
            AssignmentTechnique(
                id=technique_id,
                objective="Escalate within one reviewed target session.",
                tags=frozenset({"prompt-injection", "text", "multi"}),
                guidance_ids=("attack-catalogue:crescendo",),
                semantic_id=contract["semantic_technique_id"],
                language="en",
                domain_id="prompt-injection",
                strategy_id=context_strategy_id,
                strategy_contract=(
                    tuple(sorted(contract.items())) if context_strategy_id == "crescendo" else ()
                ),
            ),
        ),
        tool_ids=(target_tool_name,),
        skill_ids=(),
        provider_id="test",
        authority_ids=(),
        budget=AssignmentBudget(),
        input_schema="worker-input/v1",
        output_schema="worker-output/v1",
        provider_model="test",
    )
    assignment_id = f"{campaign_id}/crescendo"
    node_id = f"worker:{assignment_id}"
    call_id = f"call-{campaign_id.rsplit('/', 1)[-1]}"
    with database.connect() as connection:
        campaigns = CampaignStore(connection)
        scheduler = DurableSchedulerStore(connection)
        campaigns.prepare(
            campaign_id,
            session_id,
            {
                "manifest": _json_value(asdict(manifest)),
                "assignment_context": _json_value(asdict(context)),
            },
            manifest.digest,
        )
        assignment = {
            "id": assignment_id,
            "objective": "Escalate within one reviewed target session.",
            "profile_id": "injection",
            "target_profile_id": target_id,
            "technique_ids": [technique_id],
            "guidance_ids": ["attack-catalogue:crescendo"],
            "tool_ids": [target_tool_name],
            "skill_ids": [],
            "provider_id": "test",
            "authority_ids": [],
            "validation_finding_ids": [],
            "budget": {},
            "input_schema": "worker-input/v1",
            "output_schema": "worker-output/v1",
            "strategy_id": context_strategy_id,
            "strategy_contract": (
                sorted(contract.items()) if context_strategy_id == "crescendo" else []
            ),
        }
        schedule_id = scheduler.create_schedule(
            session_id,
            {
                "kind": "adaptive",
                "admitted": {"topology_digest": _digest(f"topology:{campaign_id}")},
                "assignments": [assignment],
            },
            {"completed": []},
            ([node_id] if not complete_cleanup else [node_id, f"cleanup:{assignment_id}"]),
            ready_nodes=[node_id],
        )
        campaigns.add_wave(campaign_id, 0, "initial", schedule_id)
        lease = scheduler.claim(
            schedule_id,
            node_id,
            node_id,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        )
        exchange = {
            "id": "interaction",
            "interaction": True,
            "cleanup": False,
            "request": "raw escalating prompt",
            "response": "raw target response",
            "oracle_observations": _observations(
                contract,
                valid=valid_receipt,
                objective_match=objective_match,
                target_authoritative=target_authoritative,
            ),
        }
        repository = ToolEffectRepository(connection)
        effect_id = hashlib.sha256(f"effect:{campaign_id}".encode()).hexdigest()
        if valid_receipt and create_target_effect:
            effect, created = repository.begin(
                ToolEffectRecord(
                    idempotency_key=effect_id,
                    session_id=session_id,
                    call_id=call_id,
                    agent_id=assignment_id,
                    attempt_id=lease.id,
                    campaign_id=campaign_id,
                    target_id=target_id,
                    tool_name=target_tool_name,
                    effect="target_network",
                    arguments_digest=hashlib.sha256(b"raw escalating prompt").hexdigest(),
                    state=ToolEffectState.INTENT,
                )
            )
            assert created
            saved = repository.finish(
                effect.model_copy(
                    update={
                        "state": ToolEffectState.ACKNOWLEDGED,
                        "result_content": "raw target response",
                        "result_is_error": False,
                        "result_metadata": {
                            "campaign_id": campaign_id,
                            "target_profile_id": target_id,
                            "target_session_id": f"api:{campaign_id}",
                            "target_exchanges": [exchange],
                        },
                    }
                )
            )
            effect_id = saved.idempotency_key
        if valid_receipt:
            scheduler.commit_reducer(
                lease.id,
                lease.generation,
                schema_id="worker-result/v1",
                result={
                    "structured": {
                        "attempted_techniques": (
                            []
                            if context_strategy_id == "crescendo"
                            else [
                                {
                                    "technique_id": technique_id,
                                    "outcome": "refuted",
                                    "evidence_refs": [f"effect:{effect_id}"],
                                }
                            ]
                        )
                    },
                    "worker_checkpoint": {
                        "messages": ([{"tool_call_id": call_id}] if create_target_effect else [])
                    },
                    "target_session_id": (f"api:{campaign_id}" if create_target_effect else None),
                },
                checkpoint={"completed": [node_id]},
                target_session_id=(f"api:{campaign_id}" if create_target_effect else None),
            )
            if complete_cleanup:
                cleanup_node_id = f"cleanup:{assignment_id}"
                connection.execute(
                    """UPDATE merlin_storage.scheduler_schedules
                       SET status = 'running' WHERE id = %s""",
                    (schedule_id,),
                )
                connection.execute(
                    """UPDATE merlin_storage.scheduler_nodes
                       SET state = 'ready' WHERE schedule_id = %s AND node_id = %s""",
                    (schedule_id, cleanup_node_id),
                )
                connection.commit()
                assert (
                    connection.execute(
                        """SELECT state FROM merlin_storage.scheduler_nodes
                       WHERE schedule_id = %s AND node_id = %s""",
                        (schedule_id, cleanup_node_id),
                    ).fetchone()["state"]
                    == "ready"
                )
                cleanup_lease = scheduler.claim(
                    schedule_id,
                    cleanup_node_id,
                    cleanup_node_id,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
                scheduler.commit_reducer(
                    cleanup_lease.id,
                    cleanup_lease.generation,
                    schema_id="cleanup-result/v1",
                    result={"cleanup": "completed"},
                    checkpoint={"cleanup": "completed"},
                )
        elif terminal_kind == "cancelled":
            scheduler.cancel_node(
                schedule_id,
                node_id,
                reason="operator cancelled adaptive cell",
                checkpoint={"cancelled": True},
            )
        elif terminal_kind in {"failed", "quarantined"}:
            connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET state = %s, completed_at = now() WHERE id = %s""",
                (terminal_kind, lease.id),
            )
            connection.execute(
                """UPDATE merlin_storage.scheduler_nodes
                   SET state = %s WHERE schedule_id = %s AND node_id = %s""",
                (terminal_kind, schedule_id, node_id),
            )
            connection.commit()
    return manifest, schedule_id, lease.id, effect_id, call_id


async def _seal_and_report(backend, campaign_id, manifest, stop_reason="capped") -> None:
    await backend._campaigns._finish_campaign(  # type: ignore[attr-defined]  # noqa: SLF001
        campaign_id,
        "completed" if stop_reason in {"capped", "saturated"} else "failed",
        stop_reason,
    )
    await backend._campaigns._report_runner(  # type: ignore[attr-defined]
        campaign_id, manifest, stop_reason
    )


@pytest.mark.asyncio
async def test_ordinary_saturated_report_is_complete_and_confident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-complete")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {"base_url": "http://127.0.0.1:9/v1", "model": "test"},
                    "permission_mode": "auto",
                },
            ),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
        backend = build_backend(tmp_path / "workspace-complete")
        try:
            campaign_id = "campaign/ordinary-saturated"
            manifest, _schedule_id, *_ = await _campaign_with_effect(
                backend,
                database,
                tmp_path,
                campaign_id=campaign_id,
                valid_receipt=True,
                objective_match=False,
                context_strategy_id="direct",
                complete_cleanup=True,
            )
            precompletion = await backend._campaigns.status(campaign_id)
            saturation = precompletion["completion_document"]["saturation"]
            assert saturation["ready"] is False
            assert saturation["hypothesis_cells_required"] == 0
            assert saturation["hypothesis_cells_unsearched"] == 0
            original_status = backend._campaigns.status

            async def sealed_negative_status(requested_campaign_id: str):
                projection = dict(await original_status(requested_campaign_id))
                document = dict(projection["completion_document"])
                document["cells"] = tuple(
                    (cell[0], cell[1], "refuted") for cell in document["cells"]
                )
                saturation = dict(document["saturation"])
                saturation.update(
                    {
                        "ready": True,
                    }
                )
                document["saturation"] = saturation
                projection["completion_document"] = document
                projection["completion_digest"] = _digest(document)
                return projection

            monkeypatch.setattr(backend._campaigns, "status", sealed_negative_status)
            await _seal_and_report(backend, campaign_id, manifest, "saturated")
            revision = backend._report_repository.list(campaign_id)[-1]
            report = backend._report_repository.get(revision.revision_id)
            assert report.report_state == "complete"
            assert report.metadata["confidence"] == "high"
            assert "Assessment completed" in report.metadata["verdict"]
            assert report.coverage["negative"] == 1
            assert report.cleanup_status == "completed"
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_injection_exchange_is_included_in_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-injection")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {"base_url": "http://127.0.0.1:9/v1", "model": "test"},
                    "permission_mode": "auto",
                },
            ),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
        backend = build_backend(tmp_path / "workspace-injection")
        try:
            campaign_id = "campaign/injection-report"
            manifest, schedule_id, _attempt_id, _effect_id, call_id = (
                await _campaign_with_effect(
                    backend,
                    database,
                    tmp_path,
                    campaign_id=campaign_id,
                    valid_receipt=True,
                    objective_match=False,
                    context_strategy_id="direct",
                    complete_cleanup=True,
                    target_tool_name="target_inject",
                )
            )
            # This focused fixture does not run the scheduler daemon that normally
            # reconciles an all-terminal node set into the terminal schedule state.
            with database.connect() as connection:
                connection.execute(
                    "UPDATE merlin_storage.scheduler_schedules SET status = 'completed' "
                    "WHERE id = %s",
                    (schedule_id,),
                )
                connection.commit()
            await _seal_and_report(backend, campaign_id, manifest)
            revision = backend._report_repository.list(campaign_id)[-1]  # type: ignore[attr-defined]
            report = backend._report_repository.get(revision.revision_id)  # type: ignore[attr-defined]

            assert [exchange["id"] for exchange in report.exchanges] == [
                f"{call_id}:interaction"
            ]
            assert report.exchanges[0]["request"] == "raw escalating prompt"
            assert report.exchanges[0]["response"] == "raw target response"
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_crescendo_receipt_joins_internal_report_and_customer_redacts_opaque_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {"base_url": "http://127.0.0.1:9/v1", "model": "test"},
                    "permission_mode": "auto",
                },
            ),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
        backend = build_backend(tmp_path / "workspace")
        try:
            campaign_id = "campaign/crescendo-report"
            manifest, schedule_id, attempt_id, effect_id, call_id = await _campaign_with_effect(
                backend,
                database,
                tmp_path,
                campaign_id=campaign_id,
                valid_receipt=True,
                target_authoritative=True,
            )
            gate = await backend._agents.crescendo_gate(schedule_id)  # type: ignore[attr-defined]
            receipt = next(iter(gate["strategies"].values()))
            await _seal_and_report(backend, campaign_id, manifest)
            revision = backend._report_repository.list(campaign_id)[-1]  # type: ignore[attr-defined]
            internal = backend._report_repository.get(revision.revision_id)  # type: ignore[attr-defined]
            exported_path = tmp_path / "workspace/target/reports/campaign-crescendo-report.html"
            assert internal.metadata["export_path"] == str(exported_path)
            assert exported_path.is_file()
            assert "<!doctype html>" in exported_path.read_text(encoding="utf-8")
            lineage = [row for row in internal.lineage if row.get("pack") == "adaptive-strategy"]
            assert len(lineage) == 1
            assert lineage[0]["attempt"] == attempt_id
            assert lineage[0]["effect_receipt"] == effect_id
            assert lineage[0]["exchange"] == f"{call_id}:interaction"
            assert lineage[0]["stage_receipt"] == receipt["steps"][0]["receipt_digest"]
            assert [row["id"] for row in internal.exchanges].count(lineage[0]["exchange"]) == 1
            technique = next(
                row for row in internal.techniques if row["strategy_id"] == "crescendo"
            )
            assert technique["outcome"] == "confirmed"
            assert "crescendo_receipt" not in technique
            assert not technique["evaluation_uncertainties"]

            customer = json.loads(
                DurableReportService(backend._report_repository).export(  # type: ignore[attr-defined]
                    revision.revision_id, "json", "customer"
                )
            )
            customer_lineage = [row for row in customer["lineage"] if row.get("stage") == "probe"]
            assert len(customer_lineage) == 1
            assert customer_lineage[0]["attempt"] == "[redacted]"
            assert customer_lineage[0]["stage_receipt"] == "[redacted]"
            assert customer_lineage[0]["effect_receipt"] == "[redacted]"
            assert [row["id"] for row in customer["exchanges"]].count(
                customer_lineage[0]["exchange"]
            ) == 1
            serialized = json.dumps(customer, sort_keys=True)
            for secret in (
                attempt_id,
                effect_id,
                call_id,
                receipt["steps"][0]["receipt_digest"],
            ):
                assert secret not in serialized
            assert "raw escalating prompt" in serialized
            assert "raw target response" in serialized
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_crescendo_report_fails_closed_when_terminal_receipt_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-missing")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {"base_url": "http://127.0.0.1:9/v1", "model": "test"},
                    "permission_mode": "auto",
                },
            ),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
        backend = build_backend(tmp_path / "workspace-missing")
        try:
            campaign_id = "campaign/crescendo-missing"
            manifest, schedule_id, *_opaque = await _campaign_with_effect(
                backend,
                database,
                tmp_path,
                campaign_id=campaign_id,
                valid_receipt=False,
            )
            gate = await backend._agents.crescendo_gate(schedule_id)  # type: ignore[attr-defined]
            observation = gate["strategies"][f"{campaign_id}/crescendo"]
            assert observation["verdict"] == "ambiguous"
            assert observation["evaluation_uncertain"] is True
            assert observation["steps"] == ()
            await _seal_and_report(backend, campaign_id, manifest)
            revision = backend._report_repository.list(campaign_id)[-1]  # type: ignore[attr-defined]
            report = backend._report_repository.get(revision.revision_id)  # type: ignore[attr-defined]
            technique = next(row for row in report.techniques if row["strategy_id"] == "crescendo")
            assert technique["outcome"] == "unclear"
            assert "crescendo_receipt" not in technique
            assert "scheduler content interpretation is advisory" in technique["rationale"]
            assert not [row for row in report.lineage if row.get("pack") == "adaptive-strategy"]
        finally:
            await backend.close()


@pytest.mark.parametrize(
    "terminal_kind",
    ("cancelled", "failed", "quarantined"),
)
@pytest.mark.asyncio
async def test_crescendo_zero_step_terminal_report_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_kind: str,
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / f"postgres-{terminal_kind}")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {"base_url": "http://127.0.0.1:9/v1", "model": "test"},
                    "permission_mode": "auto",
                },
            ),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
        backend = build_backend(tmp_path / f"workspace-{terminal_kind}")
        try:
            campaign_id = f"campaign/crescendo-{terminal_kind}"
            manifest, schedule_id, *_ = await _campaign_with_effect(
                backend,
                database,
                tmp_path,
                campaign_id=campaign_id,
                valid_receipt=False,
                terminal_kind=terminal_kind,
            )
            gate = await backend._agents.crescendo_gate(schedule_id)  # type: ignore[attr-defined]
            assert not gate["execution_complete"]
            assert gate["strategies"][f"{campaign_id}/crescendo"]["evaluation_uncertain"] is True
            await _seal_and_report(backend, campaign_id, manifest)
            revision = backend._report_repository.list(campaign_id)[-1]  # type: ignore[attr-defined]
            report = backend._report_repository.get(revision.revision_id)  # type: ignore[attr-defined]
            technique = next(row for row in report.techniques if row["strategy_id"] == "crescendo")
            assert technique["outcome"] == "unclear"
            assert report.report_state == "incomplete"
            assert report.stop_reason == "budget_exhausted"
            assert not [row for row in report.lineage if row.get("pack") == "adaptive-strategy"]
        finally:
            await backend.close()
