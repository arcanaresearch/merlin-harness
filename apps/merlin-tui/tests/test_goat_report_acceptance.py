import asyncio
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
    GraphAuthority,
    ReviewedTargetAdapter,
    SchemaSpec,
    SwarmTaskSpec,
    admit_graph,
    build_swarm_graph,
)
from merlin_agents.campaign_service import _json_value
from merlin_agents.goat import (
    GOAT_TOOLBOX_CATALOG_DIGEST,
    GoatContract,
    trusted_goat_toolbox,
)
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


def digest(value: object) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def observation(review: GoatContract, matched: bool) -> dict[str, object]:
    binding = {
        "adapter_digest": review.reviewed_adapter_digest,
        "step_id": "interaction",
        "oracle_digest": review.terminal_oracle_digest,
        "pointer": "/result",
        "match_values": ["matched"],
        "modality": "text",
        "turn": "multi",
        "language": "en",
        "purpose": "objective",
    }
    row = {
        "schema": "merlin.api-oracle-observation.v2",
        **binding,
        "binding_digest": digest(binding),
        "selected_digest": digest("reply"),
        "matched": matched,
    }
    row["observation_digest"] = digest(row)
    return row


@pytest.mark.asyncio
async def test_goat_report_preserves_lineage_and_customer_redacts(
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
            session_id = await backend.kernel.start()
            campaign_id = "campaign/goat-report"
            target_id = "target-goat"
            technique_id = "technique-goat"
            definitions = trusted_goat_toolbox()
            review = GoatContract(
                version="merlin.goat.v1",
                semantic_technique_id="semantic:goat",
                goal_digest=digest("Use fixed-K GOAT."),
                toolbox=definitions,
                toolbox_catalog_digest=GOAT_TOOLBOX_CATALOG_DIGEST,
                terminal_oracle_digest=digest("oracle"),
                reviewed_adapter_digest=digest("adapter"),
                attacker_route_digest=digest(
                    {
                        "provider_id": "test",
                        "model": "test",
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
            values = {
                "version": review.version,
                "semantic_technique_id": review.semantic_technique_id,
                "goal_digest": review.goal_digest,
                "toolbox_json": json.dumps(review.toolbox, separators=(",", ":")),
                "toolbox_catalog_digest": review.toolbox_catalog_digest,
                "terminal_oracle_digest": review.terminal_oracle_digest,
                "reviewed_adapter_digest": review.reviewed_adapter_digest,
                "attacker_route_digest": review.attacker_route_digest,
                "attacker_tool_transport": review.attacker_tool_transport,
                "target_profile_id": target_id,
                "session_policy": review.session_policy,
                "modality": "text",
                "language": "en",
                "target_turns": "2",
            }
            manifest = CampaignManifest.create(
                id=campaign_id,
                session_id=session_id,
                objective="Reviewed GOAT report",
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
                tool_ids=("target_request",),
                budget=AssignmentBudget(),
            )
            context = AssignmentContext(
                target_profile_id=target_id,
                techniques=(
                    AssignmentTechnique(
                        id=technique_id,
                        objective="Use fixed-K GOAT.",
                        tags=frozenset({"prompt-injection", "text", "multi"}),
                        guidance_ids=("attack-catalogue:goat",),
                        semantic_id=review.semantic_technique_id,
                        language="en",
                        domain_id="prompt-injection",
                        strategy_id="goat",
                        strategy_contract=tuple(sorted(values.items())),
                    ),
                ),
                tool_ids=("target_request",),
                skill_ids=(),
                provider_id="test",
                authority_ids=(),
                budget=AssignmentBudget(),
                input_schema="worker-input/v1",
                output_schema="worker-output/v1",
                provider_model="test",
            )
            assignment_id = f"{campaign_id}/goat"
            node_id = f"worker:{assignment_id}"
            with database.connect() as connection:
                campaigns = CampaignStore(connection)
                scheduler = DurableSchedulerStore(connection)
                # The reviewed target profile is origin-level while the reviewed
                # adapter drives a path-level endpoint; the report must name the
                # canonical profile identity and keep the endpoint as detail.
                connection.execute(
                    """INSERT INTO merlin_storage.target_profiles (id, title, base_url, model)
                       VALUES (%s, %s, %s, %s)""",
                    (target_id, "Target", "https://target.invalid/", "chat"),
                )
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
                    "objective": "Use fixed-K GOAT.",
                    "profile_id": "injection",
                    "target_profile_id": target_id,
                    "technique_ids": [technique_id],
                    "guidance_ids": ["attack-catalogue:goat"],
                    "tool_ids": ["target_request"],
                    "skill_ids": [],
                    "provider_id": "test",
                    "provider_model": "test",
                    "authority_ids": [],
                    "validation_finding_ids": [],
                    "budget": {},
                    "input_schema": "worker-input/v1",
                    "output_schema": "worker-output/v1",
                    "strategy_id": "goat",
                    "semantic_technique_id": review.semantic_technique_id,
                    "strategy_contract": sorted(values.items()),
                    "comparison_supported": True,
                }
                graph_spec = build_swarm_graph(
                    "goat-report",
                    (
                        SwarmTaskSpec(
                            id=assignment_id,
                            input_schema="worker-input/v1",
                            result_schema="worker-output/v1",
                            tools={"target_request"},
                        ),
                    ),
                    (
                        SchemaSpec(id="worker-input/v1"),
                        SchemaSpec(id="worker-output/v1"),
                    ),
                    envelope_schema="worker-input/v1",
                    reduced_schema="worker-output/v1",
                )
                admitted = admit_graph(
                    graph_spec,
                    GraphAuthority(authority_id="report-test", tools={"target_request"}),
                )
                schedule_id = scheduler.create_schedule(
                    session_id,
                    {
                        "assignments": [assignment],
                        "admitted": admitted.model_dump(mode="json"),
                    },
                    {"completed": []},
                    [node_id],
                    ready_nodes=[node_id],
                )
                campaigns.add_wave(campaign_id, 0, "initial", schedule_id)
                lease = scheduler.claim(
                    schedule_id,
                    node_id,
                    node_id,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
                repository = ToolEffectRepository(connection)
                effect_ids = []
                call_ids = []
                for index, matched in enumerate((True, False)):
                    plan = {
                        "observation": f"o{index}",
                        "thought": f"t{index}",
                        "strategy": ["hypothetical"],
                        "response": f"raw prompt {index}",
                    }
                    arguments = {
                        "input": plan["response"],
                        "strategy_plan": plan,
                        "strategy_turn_index": index,
                    }
                    effect_id = hashlib.sha256(f"effect-{index}".encode()).hexdigest()
                    call_id = f"call-{index}"
                    effect_ids.append(effect_id)
                    call_ids.append(call_id)
                    effect, created = repository.begin(
                        ToolEffectRecord(
                            idempotency_key=effect_id,
                            session_id=session_id,
                            call_id=call_id,
                            agent_id=assignment_id,
                            attempt_id=lease.id,
                            campaign_id=campaign_id,
                            target_id=target_id,
                            tool_name="target_request",
                            effect="target_network",
                            arguments_digest=hashlib.sha256(
                                json.dumps(
                                    arguments, sort_keys=True, separators=(",", ":")
                                ).encode()
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
                                "result_content": f"raw reply {index}",
                                "result_is_error": False,
                                "result_metadata": {
                                    "campaign_id": campaign_id,
                                    "target_profile_id": target_id,
                                    "target_session_id": f"api:{campaign_id}",
                                    "target_exchanges": [
                                        {
                                            "id": f"exchange-{index}",
                                            "interaction": True,
                                            "cleanup": False,
                                            "response": f"raw reply {index}",
                                            "oracle_observations": [observation(review, matched)],
                                        }
                                    ],
                                },
                            }
                        )
                    )
                scheduler.commit_reducer(
                    lease.id,
                    lease.generation,
                    schema_id="worker-result/v1",
                    result={
                        "structured": {"attempted_techniques": []},
                        "worker_checkpoint": {
                            "messages": [{"tool_call_id": value} for value in call_ids]
                        },
                        "target_session_id": f"api:{campaign_id}",
                    },
                    checkpoint={"completed": [node_id]},
                    target_session_id=f"api:{campaign_id}",
                )
            gate = await backend._agents.goat_gate(schedule_id)
            receipt = gate["strategies"][assignment_id]
            await backend._campaigns._finish_campaign(  # type: ignore[attr-defined]  # noqa: SLF001
                campaign_id, "completed", "capped"
            )
            await backend._campaigns._report_runner(campaign_id, manifest, "capped")
            revision = backend._report_repository.list(campaign_id)[-1]
            internal = backend._report_repository.get(revision.revision_id)
            assert internal.metadata["target"] == "https://target.invalid"
            assert internal.metadata["target_endpoint"] == "https://target.invalid/chat"
            technique = next(row for row in internal.techniques if row["strategy_id"] == "goat")
            assert technique["outcome"] == "unclear"
            assert technique["goat_receipt"] == receipt["receipt_digest"]
            assert any(
                uncertainty.get("kind") == "evaluation_uncertain"
                and uncertainty.get("source") == "goat.scheduler_observation"
                for uncertainty in technique["evaluation_uncertainties"]
            )
            assert internal.report_state == "incomplete"
            assert internal.metadata["confidence"] == "low"
            assert "Scan ended with incomplete coverage" in internal.metadata["verdict"]
            assert "No security conclusion can be drawn" in internal.metadata["verdict"]
            assert "None" not in internal.metadata["verdict"]
            lineage = [row for row in internal.lineage if row.get("entry") == "merlin.goat.v1"]
            assert len(lineage) == 2
            # A rerun of the report runner against an existing sealed revision is a
            # journaled no-op, never a divergent republish.
            await backend._campaigns._report_runner(campaign_id, manifest, "capped")
            assert len(backend._report_repository.list(campaign_id)) == 1
            lead_events = await asyncio.to_thread(
                backend._campaigns._store.list_lead,
                campaign_id,  # type: ignore[attr-defined]  # noqa: SLF001
            )
            assert any(
                isinstance(event.get("payload"), dict)
                and event["payload"].get("report_skipped")
                == "sealed report revision already published"
                for event in lead_events
            )
            customer = json.loads(
                DurableReportService(backend._report_repository).export(
                    revision.revision_id, "json", "customer"
                )
            )
            serialized = json.dumps(customer, sort_keys=True)
            for secret in (*effect_ids, *call_ids, lease.id):
                assert secret not in serialized
            assert "raw prompt 0" in serialized
            assert "raw reply 0" in serialized
            await asyncio.to_thread(
                backend._campaigns._store.add_wave,  # type: ignore[attr-defined]  # noqa: SLF001
                campaign_id,
                1,
                "followup",
                schedule_id,
            )
            with pytest.raises(RuntimeError, match="evidence changed after completion was sealed"):
                await backend._campaigns._report_runner(campaign_id, manifest, "capped")
        finally:
            await backend.close()
