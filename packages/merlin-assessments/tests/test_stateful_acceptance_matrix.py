from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import httpx
import pytest
from merlin_agents.arm_mutation import generate_bounded_child_candidate
from merlin_assessments import ReviewedApiTargetTool, StatefulTargetFixtureServer
from merlin_config import ConfigPaths, CredentialCheckpointVault
from merlin_contracts import ToolContext
from merlin_contracts.state_scope import STATE_SCOPE_SCHEMA_VERSION
from merlin_contracts.target_adapter_compiler import (
    compile_target_adapter,
    compiled_target_adapter_digest,
)
from merlin_storage_postgres import (
    CampaignApiSessionRepository,
    CampaignStore,
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    SessionRepository,
)

CORPUS_PATH = Path(__file__).parent / "fixtures" / "stateful_target_regressions.json"


@pytest.fixture(autouse=True)
def _isolated_pg0_port(monkeypatch: pytest.MonkeyPatch) -> None:
    import pg0

    real_pg0 = pg0.Pg0
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    def construct_pg0(*args: object, **kwargs: object) -> object:
        return real_pg0(*args, port=port, **kwargs)

    monkeypatch.setattr(pg0, "Pg0", construct_pg0)


def _corpus() -> dict[str, object]:
    return json.loads(CORPUS_PATH.read_text())


def _prepare(connection, campaign_id: str) -> CampaignApiSessionRepository:
    session = SessionRepository(connection).create("Generic stateful target acceptance")
    CampaignStore(connection).prepare(
        campaign_id,
        str(session.id),
        {"manifest": {"id": campaign_id}, "assignment_context": {}},
        "sha256:reviewed",
    )
    return CampaignApiSessionRepository(connection)


def _tool(
    sessions: CampaignApiSessionRepository,
    adapter: dict[str, object],
    tmp_path: Path,
    client: httpx.AsyncClient,
) -> ReviewedApiTargetTool:
    return ReviewedApiTargetTool(
        sessions,
        lambda _: adapter,
        {},
        checkpoint_vault=CredentialCheckpointVault(ConfigPaths(tmp_path / "credentials")),
        client=client,
    )


def _context(
    campaign_id: str, adapter: dict[str, object], *, agent: str = "candidate-1", call: str = "1"
) -> ToolContext:
    compilation = compile_target_adapter(adapter)
    return ToolContext(
        session_id="00000000-0000-0000-0000-000000000001",
        workspace_root="/workspace",
        call_id=f"call-{call}",
        agent_id=f"{campaign_id}/{agent}",
        campaign_id=campaign_id,
        target_id="generic-stateful-target",
        compiled_adapter=json.dumps(
            compilation.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ),
        compiled_adapter_digest=compiled_target_adapter_digest(compilation),
    )


@pytest.mark.asyncio
async def test_preserved_failure_shape_has_sanitized_reproducible_metrics() -> None:
    corpus = _corpus()
    source_path = CORPUS_PATH.with_name(str(corpus["source_snapshot"]))
    source = json.loads(source_path.read_text(encoding="utf-8"))
    durable = source["durable_export"]
    preserved = corpus["cases"][0]
    assert preserved["id"] == "preserved-shared-state-fanout"
    assert "target" not in preserved["observed_shape"]
    assert "prompt" not in preserved["observed_shape"]
    assert source["provenance"]["kind"] == corpus["source_kind"]
    assert len(durable["technique_attempts"]) == preserved["observed_shape"]["logical_attempts"]

    async with StatefulTargetFixtureServer(maximum_parallel_states=2, input_limit=5) as target:
        async with httpx.AsyncClient(base_url=target.origin) as client:
            starts = await asyncio.gather(*(client.post("/api/start", json={}) for _ in range(16)))
            leases = [response.json()["leaseId"] for response in starts]
            accepted = await asyncio.gather(
                *(
                    client.post(
                        "/api/message",
                        json={"leaseId": leases[index % len(leases)], "message": "short"},
                    )
                    for index in range(14)
                )
            )
            rejected = await asyncio.gather(
                *(
                    client.post(
                        "/api/message",
                        json={"leaseId": leases[index], "message": "too-long"},
                    )
                    for index in range(4)
                )
            )
        metrics = target.metrics()

    assert all(response.status_code == 200 for response in accepted)
    assert all(response.status_code == 422 for response in rejected)
    assert metrics.logical_leases == preserved["observed_shape"]["logical_attempts"]
    assert metrics.physical_states == preserved["observed_shape"]["physical_states"]
    assert metrics.identity_collisions == preserved["expected"]["identity_collisions"]
    assert metrics.accepted_interactions == preserved["observed_shape"]["accepted_requests"]
    assert metrics.rejected_interactions == preserved["observed_shape"]["rejected_requests"]


@pytest.mark.asyncio
async def test_empty_state_executes_without_inventing_an_authoritative_outcome(
    tmp_path: Path,
) -> None:
    async with StatefulTargetFixtureServer(include_state_capabilities=False) as target:
        adapter = target.adapter()
        campaign_id = "campaign/acceptance-empty-state"
        with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                sessions = _prepare(connection, campaign_id)
                async with httpx.AsyncClient() as client:
                    result = await _tool(sessions, adapter, tmp_path, client).invoke(
                        _context(campaign_id, adapter), {"input": "hello"}
                    )
                envelopes = [
                    dict(row["envelope"])
                    for row in connection.execute(
                        """SELECT envelope FROM merlin_storage.scope_envelopes
                           ORDER BY scope_id, revision"""
                    ).fetchall()
                ]

    assert result.is_error is False
    assert "target_state_transition" not in result.metadata
    assert result.metadata.get("outcome_authority") != "target"
    assert envelopes
    final = envelopes[-1]
    assert final["before"] == {
        "availability": "empty",
        "evidence_refs": [],
        "item_count": 0,
        "missing": None,
    }
    assert final["after"] == final["before"]
    assert final["action"]["availability"] == "present"
    assert final["responses"]["availability"] == "present"


@pytest.mark.asyncio
async def test_stateful_chat_uses_target_authored_before_after_state(tmp_path: Path) -> None:
    async with StatefulTargetFixtureServer(state_mode="fresh") as target:
        adapter = target.adapter()
        campaign_id = "campaign/acceptance-stateful-chat"
        with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                sessions = _prepare(connection, campaign_id)
                async with httpx.AsyncClient() as client:
                    result = await _tool(sessions, adapter, tmp_path, client).invoke(
                        _context(campaign_id, adapter), {"input": "advance"}
                    )
                envelopes = [
                    dict(row["envelope"])
                    for row in connection.execute(
                        """SELECT envelope FROM merlin_storage.scope_envelopes
                           ORDER BY scope_id, revision"""
                    ).fetchall()
                ]

    assert result.is_error is False
    assert result.metadata["attempt_disposition"] == "valid"
    assert result.metadata["state_identity_match"] is True
    assert result.metadata["target_state_transition"]["outcome"] == "progress"
    assert result.metadata["target_state_transition"]["authority"] == "target"
    assert result.metadata["target_state_transition"]["identity_equivalence_proven"] is True
    assert envelopes
    final = envelopes[-1]
    assert all(
        final[slot]["availability"] in {"present", "empty", "missing"}
        and (final[slot]["availability"] != "missing" or final[slot]["missing"]["reason"])
        for slot in ("before", "action", "responses", "after")
    )
    message_request = next(
        item["request"] for item in target.http_log if item["operation"] == "message"
    )
    lease_id = str(message_request["leaseId"])
    physical_id = target.physical_state_for(lease_id)
    assert physical_id is not None
    durable_and_public = json.dumps(
        {"result": result.model_dump(mode="json"), "envelopes": envelopes},
        sort_keys=True,
    )
    assert lease_id not in durable_and_public
    assert physical_id not in durable_and_public
    for exchange in result.metadata["current_target_exchanges"]:
        assert "request" not in exchange
        assert "response" not in exchange
        assert "response_body" not in exchange
        assert "request_summary" in exchange
        assert "response_body_summary" in exchange


@pytest.mark.asyncio
async def test_ordered_workflow_runs_checkpoint_restore_before_interaction(
    tmp_path: Path,
) -> None:
    async with StatefulTargetFixtureServer(
        maximum_parallel_states=1, state_mode="checkpoint"
    ) as target:
        adapter = target.adapter()
        campaign_id = "campaign/acceptance-ordered-workflow"
        with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                sessions = _prepare(connection, campaign_id)
                async with httpx.AsyncClient() as client:
                    result = await _tool(sessions, adapter, tmp_path, client).invoke(
                        _context(campaign_id, adapter), {"input": "advance"}
                    )

        operations = [item["operation"] for item in target.http_log]

    assert result.is_error is False
    assert operations[:3] == ["start", "restore", "message"]
    assert result.metadata["state_identity_match"] is True


@pytest.mark.asyncio
async def test_shared_state_is_serialized_with_exact_transition_attribution(
    tmp_path: Path,
) -> None:
    async with StatefulTargetFixtureServer(
        maximum_parallel_states=1, interaction_delay_seconds=0.05
    ) as target:
        adapter = target.adapter()
        campaign_id = "campaign/acceptance-shared-state"
        with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                sessions = _prepare(connection, campaign_id)
                async with httpx.AsyncClient() as client:
                    tool = _tool(sessions, adapter, tmp_path, client)
                    first, second = await asyncio.gather(
                        tool.invoke(_context(campaign_id, adapter), {"input": "first"}),
                        tool.invoke(
                            _context(campaign_id, adapter, agent="candidate-2", call="2"),
                            {"input": "second"},
                        ),
                    )
        metrics = target.metrics()

    assert first.metadata["attempt_disposition"] == "valid"
    assert second.is_error is False
    assert second.metadata["attempt_disposition"] == "valid"
    assert second.metadata["target_state_transition"]["outcome"] == "progress"
    assert second.metadata["current_target_exchanges"][0]["physical_state_sequential_reuse"] is True
    assert metrics.starts == 2
    assert metrics.interactions == 2
    assert metrics.identity_collisions == 1


@pytest.mark.asyncio
async def test_rotating_identity_cannot_produce_clean_learning(tmp_path: Path) -> None:
    async with StatefulTargetFixtureServer(
        state_mode="fresh", rotate_identity_on_message=True
    ) as target:
        adapter = target.adapter()
        campaign_id = "campaign/acceptance-rotating-id"
        with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                sessions = _prepare(connection, campaign_id)
                async with httpx.AsyncClient() as client:
                    result = await _tool(sessions, adapter, tmp_path, client).invoke(
                        _context(campaign_id, adapter), {"input": "rotate"}
                    )

    assert result.metadata["state_identity_match"] is False
    assert result.metadata["attempt_disposition"] == "contaminated"
    assert result.metadata["target_state_transition"]["outcome"] == "unclear"
    assert result.metadata["acknowledged_capability_evidence"] is False


@pytest.mark.asyncio
async def test_hostile_response_claim_cannot_override_measured_state(tmp_path: Path) -> None:
    async with StatefulTargetFixtureServer(state_mode="fresh", hostile_oracle=True) as target:
        adapter = target.adapter()
        campaign_id = "campaign/acceptance-hostile-oracle"
        with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                sessions = _prepare(connection, campaign_id)
                async with httpx.AsyncClient() as client:
                    result = await _tool(sessions, adapter, tmp_path, client).invoke(
                        _context(campaign_id, adapter), {"input": "claim victory"}
                    )

    interaction = result.metadata["current_target_exchanges"][-1]
    assert "untrusted_claim" in interaction["response_body_summary"]["fields"]
    assert result.metadata["target_state_transition"]["outcome"] == "progress"
    assert result.metadata.get("target_authoritative_outcome") != "confirmed"


@pytest.mark.asyncio
async def test_quota_and_multi_message_behavior_remain_observable(tmp_path: Path) -> None:
    async with StatefulTargetFixtureServer(
        maximum_parallel_states=1, interaction_quota=1, multi_message=True
    ) as target:
        adapter = target.adapter()
        campaign_id = "campaign/acceptance-multi-message"
        with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                sessions = _prepare(connection, campaign_id)
                async with httpx.AsyncClient() as client:
                    first = await _tool(sessions, adapter, tmp_path, client).invoke(
                        _context(campaign_id, adapter), {"input": "one"}
                    )
        async with httpx.AsyncClient(base_url=target.origin) as client:
            lease = (await client.post("/api/start", json={})).json()["leaseId"]
            exhausted = await client.post("/api/message", json={"leaseId": lease, "message": "two"})
        metrics = target.metrics()

    assert first.is_error is False
    assert json.loads(first.content) == [
        "first response",
        "second response",
        "third response",
    ]
    assert exhausted.status_code == 429
    assert exhausted.json()["error"]["code"] == "quota_exhausted"
    assert metrics.response_messages == 3
    assert metrics.accepted_interactions == 1
    assert metrics.rejected_interactions == 1


@pytest.mark.asyncio
async def test_durable_resource_exhaustion_blocks_retries_without_target_calls(
    tmp_path: Path,
) -> None:
    async with StatefulTargetFixtureServer(
        maximum_parallel_states=1, interaction_quota=1
    ) as target:
        adapter = target.adapter()
        campaign_id = "campaign/acceptance-durable-resource-block"
        with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                sessions = _prepare(connection, campaign_id)
                async with httpx.AsyncClient() as client:
                    tool = ReviewedApiTargetTool(
                        sessions,
                        lambda _: adapter,
                        {},
                        checkpoint_vault=CredentialCheckpointVault(
                            ConfigPaths(tmp_path / "credentials")
                        ),
                        client=client,
                    )
                    accepted = await tool.invoke(_context(campaign_id, adapter), {"input": "one"})
                    calls_after_acceptance = target.metrics().requests
                    first_retry = await tool.invoke(
                        _context(campaign_id, adapter, agent="candidate-2", call="2"),
                        {"input": "two"},
                    )
                    second_retry = await tool.invoke(
                        _context(campaign_id, adapter, agent="candidate-3", call="3"),
                        {"input": "three"},
                    )
        metrics = target.metrics()

    assert accepted.is_error is False
    for blocked in (first_retry, second_retry):
        assert blocked.is_error is True
        assert blocked.metadata["failure_code"] == "target_resource_blocked"
        assert blocked.metadata["attempt_disposition"] == "infrastructure_failed"
        assert blocked.metadata["target_effect_attempted"] is False
        assert blocked.metadata["target_call_budget_charge"] == 0
        assert blocked.metadata["no_target_effect"] is True
    assert metrics.requests == calls_after_acceptance
    assert metrics.accepted_interactions == 1
    assert metrics.rejected_interactions == 0


@pytest.mark.asyncio
async def test_restart_reuses_durable_target_session_and_physical_identity(
    tmp_path: Path,
) -> None:
    async with StatefulTargetFixtureServer(maximum_parallel_states=1) as target:
        adapter = target.adapter()
        campaign_id = "campaign/acceptance-restart"
        with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                sessions = _prepare(connection, campaign_id)
                async with httpx.AsyncClient() as client:
                    first_tool = _tool(sessions, adapter, tmp_path, client)
                    first = await first_tool.invoke(
                        _context(campaign_id, adapter), {"input": "before restart"}
                    )
                    restarted_tool = _tool(sessions, adapter, tmp_path, client)
                    second = await restarted_tool.invoke(
                        _context(campaign_id, adapter, call="2"),
                        {"input": "after restart"},
                    )
        metrics = target.metrics()

    assert first.is_error is False and second.is_error is False
    assert metrics.starts == 1
    assert metrics.interactions == 2
    assert first.metadata["physical_state_identity"] == second.metadata["physical_state_identity"]
    assert first.metadata["physical_state_identity"]["kind"] == "keyed_digest"


@pytest.mark.asyncio
async def test_universal_state_scope_combines_generic_campaign_safety_features(
    tmp_path: Path,
) -> None:
    """Prove the generic stateful defaults together through PostgreSQL seams."""

    postgres_dir = tmp_path / "postgres-combined-stateful-acceptance"

    # Unknown empty-state evidence still receives a universal envelope while
    # independent physical sessions retain safe concurrent delivery.
    async with StatefulTargetFixtureServer(
        include_state_capabilities=False,
        state_mode="fresh",
        interaction_delay_seconds=0.05,
    ) as empty_target:
        empty_adapter = empty_target.adapter()
        with Database(DatabaseConfig(data_dir=postgres_dir)) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                empty_campaign = "campaign/acceptance-combined-empty"
                empty_sessions = _prepare(connection, empty_campaign)
                async with httpx.AsyncClient() as client:
                    empty_tool = _tool(
                        empty_sessions, empty_adapter, tmp_path / "empty-vault", client
                    )
                    empty_results = await asyncio.gather(
                        empty_tool.invoke(
                            _context(empty_campaign, empty_adapter, agent="empty-a", call="a"),
                            {"input": "first"},
                        ),
                        empty_tool.invoke(
                            _context(empty_campaign, empty_adapter, agent="empty-b", call="b"),
                            {"input": "second"},
                        ),
                    )
                empty_scopes = connection.execute(
                    """SELECT status, format_version FROM merlin_storage.state_scopes
                       WHERE campaign_id = %s ORDER BY id""",
                    (empty_campaign,),
                ).fetchall()

        assert all(not result.is_error for result in empty_results)
        assert empty_target.metrics().interactions == 2
        assert empty_target.metrics().physical_states == 2
        assert empty_target.metrics().identity_collisions == 0
        assert len(empty_scopes) == 2
        assert {str(row["status"]) for row in empty_scopes} == {"settled"}
        assert {str(row["format_version"]) for row in empty_scopes} == {STATE_SCOPE_SCHEMA_VERSION}

    campaign_id = "campaign/acceptance-combined-stateful"
    parent_id = "technique/generic-stateful@lang=en@variant=native"
    async with StatefulTargetFixtureServer(
        maximum_parallel_states=1,
        input_limit=7,
        advertise_input_limit=False,
        interaction_quota=2,
        multi_message=True,
    ) as target:
        adapter = target.adapter()
        state_capabilities = adapter["state_capabilities"]
        assert isinstance(state_capabilities, dict)
        resource_controls = state_capabilities["resource_controls"]
        assert isinstance(resource_controls, list)
        resource_control = resource_controls[0]
        assert isinstance(resource_control, dict)
        # The fixture target owns the authoritative quota. Keep the local
        # reservation ceiling above it so the structured 429 can establish a
        # durable wake time before later retries are stopped locally.
        resource_control["limit"] = 4
        with Database(DatabaseConfig(data_dir=postgres_dir)) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                session = SessionRepository(connection).create("Combined stateful acceptance")
                store = CampaignStore(connection)
                store.prepare_with_frontier(
                    campaign_id,
                    str(session.id),
                    {"manifest": {"id": campaign_id}, "assignment_context": {}},
                    "sha256:combined-stateful",
                    [
                        {
                            "id": "technique/generic-stateful",
                            "document": {
                                "id": "technique/generic-stateful",
                                "objective": "exercise a generic stateful target",
                            },
                        }
                    ],
                    [
                        {
                            "id": parent_id,
                            "family_id": "technique/generic-stateful",
                            "ordinal": 0,
                            "requested_language": "en",
                            "variant_id": "native",
                            "variant": {"language": "en", "variant": "native"},
                        },
                        {
                            "id": "technique/generic-stateful@lang=en@variant=followup-a",
                            "family_id": "technique/generic-stateful",
                            "ordinal": 1,
                            "requested_language": "en",
                            "variant_id": "followup-a",
                            "variant": {"language": "en", "variant": "followup-a"},
                        },
                        {
                            "id": "technique/generic-stateful@lang=en@variant=followup-b",
                            "family_id": "technique/generic-stateful",
                            "ordinal": 2,
                            "requested_language": "en",
                            "variant_id": "followup-b",
                            "variant": {"language": "en", "variant": "followup-b"},
                        },
                    ],
                )
                sessions = CampaignApiSessionRepository(connection)
                async with httpx.AsyncClient() as client:
                    tool = _tool(sessions, adapter, tmp_path / "stateful-vault", client)
                    rejected = await tool.invoke(
                        _context(campaign_id, adapter, agent="parent", call="reject"),
                        {"input": "twelve chars"},
                    )
                    observation = rejected.metadata["request_constraint_observations"][0]
                    child = generate_bounded_child_candidate(
                        parent_candidate_id=parent_id,
                        parent_identity={
                            "technique_id": parent_id,
                            "requested_language": "en",
                            "payload_family": "native",
                            "mutation_id": "baseline",
                        },
                        rejected_input="twelve chars",
                        constraint=observation,
                        generator=lambda _prompt: (
                            {
                                "input_text": "short",
                                "rationale": "preserve intent within the observed bound",
                            },
                        ),
                    )
                    assert child is not None
                    child_variant = child["variant"]
                    assert isinstance(child_variant, dict)
                    registered_child = store.register_frontier_child(
                        campaign_id,
                        parent_id,
                        str(child["id"]),
                        str(child["variant_id"]),
                        child_variant,
                        reason=str(child["reason"]),
                    )
                    accepted = await tool.invoke(
                        _context(campaign_id, adapter, agent="child", call="accepted"),
                        {"input": child_variant["input_text"]},
                    )

                first_batch = store.claim_frontier_batch_exact(
                    campaign_id, 1, "batch/combined-1", [str(registered_child["id"])]
                )
                scheduler = DurableSchedulerStore(connection)
                first_schedule = scheduler.create_schedule(
                    str(session.id),
                    {"assignments": []},
                    {"states": {}},
                    ["lead"],
                    ready_nodes=["lead"],
                )
                store.bind_frontier_batch(campaign_id, str(first_batch["id"]), first_schedule)
                store.settle_frontier_batch(
                    campaign_id,
                    str(first_batch["id"]),
                    {str(registered_child["id"]): "terminal"},
                )
                preferred = "technique/generic-stateful@lang=en@variant=followup-b"
                unscored = "technique/generic-stateful@lang=en@variant=followup-a"
                assert accepted.metadata["target_state_transition"]["outcome"] == "progress"
                store.update_frontier_priorities(campaign_id, {preferred: 0.9, unscored: None})

        calls_before_restart = target.metrics().requests
        with Database(DatabaseConfig(data_dir=postgres_dir)) as restarted_database:
            with restarted_database.connect() as connection:
                MigrationRunner(connection).apply()
                restarted_store = CampaignStore(connection)
                second_batch = restarted_store.claim_frontier_batch(
                    campaign_id, 2, "batch/combined-2", 1
                )
                assert tuple(item["id"] for item in second_batch["candidates"]) == (preferred,)
                assert restarted_store.frontier_counts(campaign_id)["queued"] == 2

                restarted_sessions = CampaignApiSessionRepository(connection)
                async with httpx.AsyncClient() as client:
                    restarted_tool = _tool(
                        restarted_sessions, adapter, tmp_path / "stateful-vault", client
                    )
                    after_restart = await restarted_tool.invoke(
                        _context(campaign_id, adapter, agent="followup", call="restart"),
                        {"input": "again"},
                    )
                    structured_quota = await restarted_tool.invoke(
                        _context(campaign_id, adapter, agent="deferred", call="quota"),
                        {"input": "third"},
                    )
                    calls_after_quota = target.metrics().requests
                    quota_blocked = await restarted_tool.invoke(
                        _context(campaign_id, adapter, agent="deferred", call="quota-retry"),
                        {"input": "fourth"},
                    )
                scope_rows = connection.execute(
                    """SELECT status, format_version FROM merlin_storage.state_scopes
                       WHERE campaign_id = %s ORDER BY created_at""",
                    (campaign_id,),
                ).fetchall()

    assert rejected.is_error is True
    assert rejected.metadata["attempt_disposition"] == "protocol_invalid"
    assert accepted.is_error is False and after_restart.is_error is False
    assert json.loads(accepted.content) == [
        "first response",
        "second response",
        "third response",
    ]
    assert target.metrics().starts == 4
    assert target.metrics().physical_states == 1
    assert target.metrics().identity_collisions == 3
    assert target.metrics().quota_rejections == 1
    assert (
        after_restart.metadata["current_target_exchanges"][0]["physical_state_sequential_reuse"]
        is True
    )
    assert target.metrics().response_messages == 6
    assert target.metrics().requests > calls_before_restart
    assert structured_quota.is_error is True
    assert structured_quota.metadata["failure_code"] == "target_resource_blocked"
    assert structured_quota.metadata["target_effect_attempted"] is True
    assert structured_quota.metadata["resource_block_evidence"]["status_code"] == 429
    assert quota_blocked.is_error is True
    assert quota_blocked.metadata["failure_code"] == "target_resource_blocked"
    assert quota_blocked.metadata["target_effect_attempted"] is False
    assert quota_blocked.metadata["target_call_budget_charge"] == 0
    assert target.metrics().requests == calls_after_quota
    assert len(scope_rows) == 5
    assert {str(row["format_version"]) for row in scope_rows} == {STATE_SCOPE_SCHEMA_VERSION}
