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
from merlin_agents.campaign import scenario_fingerprint_for
from merlin_agents.campaign_service import _json_value
from merlin_config import Config, CredentialStore
from merlin_storage_postgres import (
    CampaignStore,
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
)
from merlin_storage_postgres.arm_store import CampaignArmStore
from merlin_tui.bootstrap import build_backend


def _contains_datetime(value: object) -> bool:
    if isinstance(value, datetime):
        return True
    if isinstance(value, dict):
        return any(_contains_datetime(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_datetime(item) for item in value)
    return False


@pytest.mark.asyncio
async def test_report_runner_survives_projection_datetimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a cancelled campaign whose status() completion document carries
    raw datetimes (ARM rows select created_at/updated_at) crashed the report
    runner with ``TypeError: Object of type datetime is not JSON serializable``
    when the terminal-authority guard dumped the live projection without
    normalizing.  The sealed receipt survives because every seal path routes
    through ``_json_value``; the guard must too.
    """
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
            campaign_id = "campaign/report-datetime"
            target_id = "target-report-datetime"
            technique_id = "technique-report-datetime"
            manifest = CampaignManifest.create(
                id=campaign_id,
                session_id=session_id,
                objective="Report runner datetime regression",
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
                        objective="Attempt one technique.",
                        tags=frozenset({"prompt-injection", "text"}),
                        guidance_ids=(),
                        language="en",
                        domain_id="prompt-injection",
                        strategy_id="direct",
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
            assignment_id = f"{campaign_id}/direct"
            node_id = f"worker:{assignment_id}"
            with database.connect() as connection:
                campaigns = CampaignStore(connection)
                scheduler = DurableSchedulerStore(connection)
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
                    "objective": "Attempt one technique.",
                    "profile_id": "injection",
                    "target_profile_id": target_id,
                    "technique_ids": [technique_id],
                    "guidance_ids": [],
                    "tool_ids": ["target_request"],
                    "skill_ids": [],
                    "provider_id": "test",
                    "provider_model": "test",
                    "authority_ids": [],
                    "validation_finding_ids": [],
                    "budget": {},
                    "input_schema": "worker-input/v1",
                    "output_schema": "worker-output/v1",
                    "strategy_id": "direct",
                }
                graph_spec = build_swarm_graph(
                    "report-datetime",
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
                scheduler.commit_reducer(
                    lease.id,
                    lease.generation,
                    schema_id="worker-result/v1",
                    result={
                        "structured": {"attempted_techniques": []},
                        "worker_checkpoint": {"messages": []},
                        "target_session_id": f"api:{campaign_id}",
                    },
                    checkpoint={"completed": [node_id]},
                    target_session_id=f"api:{campaign_id}",
                )
                # The failing production campaign had admitted ARM rows; their
                # created_at/updated_at columns load as raw datetimes and flow
                # into the status() completion document through the frontier
                # execution_spec.
                CampaignArmStore(connection).record_arm(
                    {
                        "id": "arm/report-datetime",
                        "target_id": target_id,
                        "scenario_fingerprint": scenario_fingerprint_for(
                            manifest.target, manifest.catalogue
                        ),
                        "surface_id": "surface:chat",
                        "semantic_id": "semantic:report-datetime",
                        "strategy_id": "direct",
                        "technique_id": technique_id,
                        "controller_id": "direct",
                        "mutation_id": "baseline",
                        "actual_language": "en",
                        "state_id": "state:initial",
                        "objective": "Attempt one technique.",
                        "source": "seed",
                        "rank_components": {"novelty": 1.0},
                        "eligibility_reasons": ["seeded for regression coverage"],
                        "decision_reason": "seed",
                    }
                )
            projection = await backend._campaigns.status(campaign_id)  # noqa: SLF001
            completion_document = projection["completion_document"]
            assert _contains_datetime(completion_document), (
                "fixture must place a raw datetime inside the live completion document"
            )
            await backend._campaigns._finish_campaign(  # noqa: SLF001
                campaign_id, "cancelled", "operator_stopped"
            )
            await backend._campaigns._report_runner(  # noqa: SLF001
                campaign_id, manifest, "operator_stopped"
            )
            revisions = backend._report_repository.list(campaign_id)  # noqa: SLF001
            assert len(revisions) == 1
            assert revisions[0].source_seal_state == "sealed"
        finally:
            await backend.close()
