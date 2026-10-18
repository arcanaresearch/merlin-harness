from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from merlin_agents.arm_mutation import generate_bounded_child_candidate
from merlin_agents.bounded_children import materialize_bounded_child_family
from merlin_agents.completion_frontier import CompletionCandidateIdentity
from merlin_agents.runtime import WorkerHandoff, _protocol_invalid_request
from merlin_agents.specialists import (
    AssignmentBudget,
    AssignmentTechnique,
    WorkerAssignment,
    WorkerProfile,
)
from merlin_assessments import ReviewedApiTargetTool, StatefulTargetFixtureServer
from merlin_assessments.api_target import (
    _augment_target_state_metadata,
    _infrastructure_failure_category,
    _ResponsePointerMissing,
    _reviewed_session_scope_id,
    _state_enumerations,
    _StateScopeLease,
    _structured_constraint_rejection_observations,
    _target_parallel_capacity,
    _target_state_transition,
    _TargetCleanupUnverified,
)
from merlin_config import ConfigPaths, CredentialCheckpointVault
from merlin_contracts import (
    InfrastructureFailureCategory,
    StateEnumerationCompleteness,
    StateEnumerationDeclaration,
    StateEnumerationShape,
    ToolContext,
    executable_adapter_digest,
)
from merlin_contracts.target_adapter_compiler import (
    compile_target_adapter,
    compiled_target_adapter_digest,
)
from merlin_contracts.target_capabilities import StateCapabilities, StateSelector
from merlin_storage_postgres import (
    CampaignApiSessionRepository,
    CampaignStore,
    Database,
    DatabaseConfig,
    MigrationRunner,
    SessionRepository,
    load_migrations,
)


def _adapter() -> dict[str, object]:
    common = {
        "expected_status": 200,
        "repeat": 1,
        "replay_after_ambiguous_result": False,
    }
    return {
        "interface": "http_api",
        "endpoint": "https://target.test/api/scanner",
        "credential_handle": "target-token",
        "api_steps": [
            {
                **common,
                "step_id": "start",
                "endpoint": "start",
                "method": "POST",
                "request_template": {"game": "refund", "levelId": 1},
                "response_selector": "/sessionId",
                "success_witness": "non-empty sessionId",
                "cleanup": False,
                "interaction": False,
                "captures": {"session_id": "/sessionId"},
                "idempotency_key": "start-one",
                "replay_after_ambiguous_result": True,
            },
            {
                **common,
                "step_id": "message",
                "endpoint": "message",
                "method": "POST",
                "request_template": {
                    "sessionId": "{{capture:session_id}}",
                    "userMessage": "{{input}}",
                },
                "response_selector": "/reply",
                "success_witness": "non-empty reply",
                "cleanup": False,
                "interaction": True,
                "captures": {},
                "idempotency_key": None,
            },
            {
                **common,
                "step_id": "abandon",
                "endpoint": "abandon",
                "method": "POST",
                "request_template": {"sessionId": "{{capture:session_id}}"},
                "response_selector": "/closed",
                "success_witness": "closed=true",
                "cleanup": True,
                "interaction": False,
                "captures": {},
                "idempotency_key": "abandon-one",
                "replay_after_ambiguous_result": True,
                "cleanup_acknowledgement": {
                    "pointer": "/closed",
                    "match_values": [True],
                    "status_codes": [200],
                },
            },
        ],
    }


@pytest.mark.asyncio
async def test_reviewed_api_tool_executes_selected_setup_step_without_interaction(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    adapter["state_capabilities"] = {
        "identity": {
            "baseline_selector": {
                "step_id": "start",
                "evaluation_root": "response_body",
                "pointer": "/sessionId",
            },
            "result_selector": {
                "step_id": "message",
                "evaluation_root": "response_body",
                "pointer": "/sessionId",
            },
            "evidence_refs": ["exchange/session"],
        },
        "observations": [
            {
                "name": "session_id",
                "role": "custom",
                "baseline_selector": {
                    "step_id": "start",
                    "evaluation_root": "response_body",
                    "pointer": "/sessionId",
                },
                "result_selector": {
                    "step_id": "message",
                    "evaluation_root": "response_body",
                    "pointer": "/sessionId",
                },
                "value_type": "string",
                "trust_class": "target_reported",
            }
        ],
        "maximum_parallel_states": 1,
    }
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "verified-session"})
        raise AssertionError("selected setup verification must not execute the interaction step")

    campaign_id = "campaign/selected-setup-step"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(
                    _sealed_context(campaign_id, adapter).model_copy(
                        update={
                            "objective_phase_gate_id": "auth-session",
                            "objective_phase_gate_contract": {
                                "id": "auth-session",
                                "success_predicate": {
                                    "observation_name": "session_id",
                                    "operator": "present",
                                    "expected_values": [],
                                },
                            },
                        }
                    ),
                    {"input": "verify state", "step_id": "start"},
                )

    assert result.is_error is False
    assert json.loads(result.content) == "[target-identity-redacted]"
    assert calls == ["/api/scanner/start"]
    assert result.metadata["limit_interaction_count"] == 0
    assert result.metadata["current_target_exchanges"][0]["id"] == "start"
    observations = result.metadata["state_observations"]
    assert {
        (item["name"], item["phase"])
        for item in observations
    } == {
        ("physical_state_identity", "baseline"),
        ("physical_state_identity", "result"),
        ("session_id", "baseline"),
        ("session_id", "result"),
    }
    transition = result.metadata["target_state_transition"]
    session_observation = next(
        item for item in transition["observations"] if item["name"] == "session_id"
    )
    assert session_observation["status"] == "unchanged"
    assert session_observation["validation_errors"] == []


@pytest.mark.asyncio
async def test_reviewed_api_tool_executes_normalized_compiled_steps_and_emits_identity(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    steps = adapter["api_steps"]
    assert isinstance(steps, list)
    steps.insert(
        0,
        {
            "step_id": "not-executable",
            "endpoint": "ignored",
            "method": "",
            "request_template": {},
        },
    )
    interaction = steps[2]
    assert isinstance(interaction, dict)
    interaction["method"] = " post "
    interaction["request_template"] = {
        "sessionId": "{{capture:session_id}}",
        "userMessage": "{{prompt}}",
    }
    interaction["repeat"] = 0

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "compiled-session"})
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "sessionId": "compiled-session",
            "userMessage": "compiled probe",
        }
        return httpx.Response(200, json={"reply": "compiled response"})

    campaign_id = "campaign/api-compiled-steps"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_sealed_context(campaign_id, adapter), {"input": "compiled probe"})
            state = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=f"{campaign_id}/injection",
            )

    assert result.is_error is False
    assert json.loads(result.content) == "compiled response"
    assert state is not None
    assert result.metadata["adapter_digest"] == executable_adapter_digest(adapter)
    compiled_digest = compiled_target_adapter_digest(compile_target_adapter(adapter))
    assert result.metadata["compiled_adapter_digest"] == compiled_digest
    assert "legacy_compiled_adapter_context" not in result.metadata
    assert state.adapter_digest == compiled_digest


@pytest.mark.asyncio
async def test_reviewed_api_tool_emits_compiled_identity_on_post_resolution_failure(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    adapter.pop("credential_handle")
    adapter["api_transport"] = "browser_context"
    campaign_id = "campaign/api-compiled-failure"

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            result = await ReviewedApiTargetTool(
                _prepare(connection, campaign_id),
                lambda _campaign_id: adapter,
                {},
            ).invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is True
    assert result.content == "reviewed browser-context API transport is unavailable"
    assert result.metadata["adapter_digest"] == executable_adapter_digest(adapter)
    assert result.metadata["compiled_adapter_digest"] == compiled_target_adapter_digest(
        compile_target_adapter(adapter)
    )


def test_infrastructure_failure_category_uses_typed_errors_not_prose() -> None:
    assert (
        _infrastructure_failure_category(
            _ResponsePointerMissing("message", "/reply", {"error": "missing"})
        )
        is InfrastructureFailureCategory.MISSING_RESPONSE_POINTER
    )


def test_runtime_constraint_discovery_does_not_parse_rejection_prose() -> None:
    assert (
        _structured_constraint_rejection_observations(
            SimpleNamespace(compiled_digest="sha256:adapter"),
            SimpleNamespace(id="message", structured_constraint_rejections=()),
            {"message": "too long"},
            422,
            {"error": "message must be at most 7 characters"},
        )
        == []
    )


def test_cleanup_unverified_exposes_typed_downstream_marker() -> None:
    error = _TargetCleanupUnverified("cleanup lifecycle was not established")

    assert error.cleanup_unresolved is True


@pytest.mark.parametrize(
    "assignment_id",
    [
        "campaign/x/direct",
        "campaign/x/pair-old/direct",
        "campaign/x/agenda-short/direct",
        "campaign/x/agenda-0123456789abcdeg/direct",
        "campaign/x/agenda-0123456789ABCDEF/direct",
        "campaign/x/agenda-0123456789abcdef/nested/direct",
        "campaign/x/agenda-0123456789abcdef/direct/extra",
        "campaign/x/agenda-old/unrelated/direct",
    ],
)
def test_noncanonical_pair_assignment_ids_remain_exactly_isolated(
    assignment_id: str,
) -> None:
    assert _reviewed_session_scope_id(assignment_id) == assignment_id


@pytest.mark.parametrize("stage", ["control", "direct", "replay"])
def test_canonical_pair_stage_keeps_exact_assignment_scope(stage: str) -> None:
    scope = "campaign/x/agenda-0123456789abcdef"
    assignment_id = f"{scope}/{stage}"

    assert _reviewed_session_scope_id(assignment_id) == assignment_id
    assert (
        _infrastructure_failure_category(
            RuntimeError("target session unavailable and response pointer missing")
        )
        is None
    )


def _prepare(connection, campaign_id: str) -> CampaignApiSessionRepository:
    session = SessionRepository(connection).create("API target acceptance")
    CampaignStore(connection).prepare(
        campaign_id,
        str(session.id),
        {"manifest": {"id": campaign_id}, "assignment_context": {}},
        "sha256:reviewed",
    )
    return CampaignApiSessionRepository(connection)


def _context(campaign_id: str) -> ToolContext:
    return ToolContext(
        session_id="00000000-0000-0000-0000-000000000001",
        workspace_root="/workspace",
        call_id="call-1",
        agent_id=f"{campaign_id}/injection",
        campaign_id=campaign_id,
        target_id="target-refund",
    )


def _sealed_context(campaign_id: str, adapter: dict[str, object]) -> ToolContext:
    compilation = compile_target_adapter(adapter)
    return _context(campaign_id).model_copy(
        update={
            "compiled_adapter": json.dumps(
                compilation.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ),
            "compiled_adapter_digest": compiled_target_adapter_digest(compilation),
        }
    )


@pytest.mark.asyncio
async def test_shared_principal_quota_blocks_campaigns_until_one_post_restart_resume(
    tmp_path: Path,
) -> None:
    """Exercise constraint, block, reset, and restart through the public target tool."""

    first_campaign = "campaign/shared-quota-a"
    second_campaign = "campaign/shared-quota-b"
    postgres_dir = tmp_path / "postgres-shared-quota"
    async with StatefulTargetFixtureServer(
        maximum_parallel_states=2,
        interaction_quota=3,
        input_limit=5,
    ) as target:
        adapter = target.adapter()
        with Database(DatabaseConfig(data_dir=postgres_dir)) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                sessions = _prepare(connection, first_campaign)
                _prepare(connection, second_campaign)

                def context(campaign_id: str, call_id: str) -> ToolContext:
                    return _sealed_context(campaign_id, adapter).model_copy(
                        update={"call_id": call_id}
                    )

                async with httpx.AsyncClient() as client:
                    tool = ReviewedApiTargetTool(
                        sessions,
                        lambda _: adapter,
                        {},
                        checkpoint_vault=CredentialCheckpointVault(
                            ConfigPaths(tmp_path / "shared-quota-credentials")
                        ),
                        client=client,
                    )
                    first = await tool.invoke(context(first_campaign, "seed-a"), {"input": "one"})
                    second = await tool.invoke(context(second_campaign, "seed-b"), {"input": "two"})
                    assert first.is_error is False, first.model_dump(mode="json")
                    assert second.is_error is False, second.model_dump(mode="json")

                    calls_before_oversize = target.metrics().requests
                    oversized = await tool.invoke(
                        context(second_campaign, "oversized"),
                        {"input": "sixxxx"},
                    )
                    assert oversized.is_error is True
                    assert oversized.metadata["failure_code"] == "request_constraint_violation"
                    assert oversized.metadata["target_effect_attempted"] is False
                    assert target.metrics().requests == calls_before_oversize

                    first_state = sessions.load(
                        first_campaign,
                        target_id="target-refund",
                        session_scope_id=f"{first_campaign}/injection",
                    )
                    assert first_state is not None
                    lease_id = first_state.variables["lease_id"]
                    assert isinstance(lease_id, str)
                    # Exhaust the target-side window independently so the next
                    # public tool call observes the authoritative structured 429.
                    for message in ("x", "y"):
                        response = await client.post(
                            target.origin + "/api/message",
                            json={"leaseId": lease_id, "message": message},
                        )
                        assert response.status_code == 200

                    authoritative_block = await tool.invoke(
                        context(first_campaign, "quota-signal"),
                        {"input": "three"},
                    )
                    assert authoritative_block.is_error is True, authoritative_block.model_dump(
                        mode="json"
                    )
                    assert "failure_code" in authoritative_block.metadata, (
                        authoritative_block.model_dump(mode="json")
                    )
                    assert authoritative_block.metadata["failure_code"] == "target_resource_blocked"
                    assert authoritative_block.metadata["target_effect_attempted"] is True
                    assert (
                        authoritative_block.metadata["resource_block_evidence"]["status_code"]
                        == 429
                    )

                    calls_after_block = target.metrics().requests
                    blocked_a = await tool.invoke(
                        context(first_campaign, "blocked-a"), {"input": "four"}
                    )
                    blocked_b = await tool.invoke(
                        context(second_campaign, "blocked-b"), {"input": "five"}
                    )
                    for blocked in (blocked_a, blocked_b):
                        assert blocked.is_error is True
                        assert blocked.metadata["failure_code"] == "target_resource_blocked"
                        assert blocked.metadata["target_effect_attempted"] is False
                    assert target.metrics().requests == calls_after_block

                    resource = connection.execute(
                        """SELECT target_authority, principal_key, resource_kind, window_key
                           FROM merlin_storage.principal_resources"""
                    ).fetchone()
                    assert resource is not None
                    connection.execute(
                        """UPDATE merlin_storage.principal_resources
                           SET capacity = 1, wake_at = %s
                           WHERE target_authority = %s AND principal_key = %s
                             AND resource_kind = %s AND window_key = %s""",
                        (
                            datetime.now(UTC) - timedelta(seconds=1),
                            resource["target_authority"],
                            resource["principal_key"],
                            resource["resource_kind"],
                            resource["window_key"],
                        ),
                    )
                    connection.commit()

                    restarted = ReviewedApiTargetTool(
                        CampaignApiSessionRepository(connection),
                        lambda _: adapter,
                        {},
                        checkpoint_vault=CredentialCheckpointVault(
                            ConfigPaths(tmp_path / "shared-quota-credentials")
                        ),
                        client=client,
                    )
                    resumed = await restarted.invoke(
                        context(second_campaign, "resumed"), {"input": "again"}
                    )
                    blocked_after_resume = await restarted.invoke(
                        context(first_campaign, "post-resume"), {"input": "later"}
                    )
                    resource_after_resume = connection.execute(
                        """SELECT capacity, consumed, state, generation
                           FROM merlin_storage.principal_resources
                           WHERE target_authority = %s AND principal_key = %s
                             AND resource_kind = %s AND window_key = %s""",
                        (
                            resource["target_authority"],
                            resource["principal_key"],
                            resource["resource_kind"],
                            resource["window_key"],
                        ),
                    ).fetchone()

        final_metrics = target.metrics()

    assert resumed.is_error is False
    assert blocked_after_resume.is_error is True
    assert blocked_after_resume.metadata["failure_code"] == "target_resource_blocked"
    assert blocked_after_resume.metadata["target_effect_attempted"] is False
    assert resource_after_resume is not None
    assert dict(resource_after_resume) == {
        "capacity": 1,
        "consumed": 1,
        "state": "exhausted",
        "generation": 2,
    }
    assert final_metrics.requests == calls_after_block + 1


@pytest.mark.asyncio
async def test_structured_runtime_limit_regenerates_child_for_next_batch(
    tmp_path: Path,
) -> None:
    """Persist a rejection across restart, preflight its parent, then run its child."""

    campaign_id = "campaign/runtime-discovered-limit"
    parent_id = "technique/runtime-limit@lang=en@variant=native"
    postgres_dir = tmp_path / "postgres-runtime-limit"
    async with StatefulTargetFixtureServer(input_limit=7, advertise_input_limit=False) as target:
        adapter = target.adapter()
        assert "request_constraints" not in adapter
        with Database(DatabaseConfig(data_dir=postgres_dir)) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                sessions = _prepare(connection, campaign_id)
                async with httpx.AsyncClient() as client:
                    tool = ReviewedApiTargetTool(
                        sessions,
                        lambda _campaign_id: adapter,
                        {},
                        client=client,
                    )
                    rejected = await tool.invoke(
                        _sealed_context(campaign_id, adapter),
                        {"input": "twelve chars"},
                    )
        initial_target_calls = len(target.http_log)
        identity = CompletionCandidateIdentity(
            campaign_id=campaign_id,
            target_id="target-refund",
            target_generation="generation-1",
            scenario_fingerprint="scenario-1",
            technique_id=parent_id,
            controller_id="direct",
            payload_family="native",
            requested_language="en",
            mutation_id="baseline",
        )
        assignment = WorkerAssignment(
            id=f"{campaign_id}/injection",
            objective="exercise the reviewed candidate",
            profile_id="injection",
            target_profile_id="target-refund",
            technique_ids=(parent_id,),
            guidance_ids=(),
            tool_ids=("target_request",),
            skill_ids=(),
            provider_id="provider",
            authority_ids=("target:reviewed",),
            budget=AssignmentBudget(max_seconds=60),
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
        )
        handoff = WorkerHandoff(
            session_id="00000000-0000-0000-0000-000000000001",
            workspace_root="/workspace",
            model="test",
            profile=WorkerProfile(
                id="injection",
                version=1,
                summary="test",
                instructions="test",
                specialty_tags=frozenset(),
            ),
            assignment=assignment,
            candidate_identity=identity,
        )
        protocol_request = _protocol_invalid_request(rejected.metadata, handoff)
        assert protocol_request is not None
        child = generate_bounded_child_candidate(
            parent_candidate_id=parent_id,
            parent_identity=protocol_request["parent_identity"],
            rejected_input=protocol_request["rendered_input"],
            constraint=protocol_request["constraint"],
            generator=lambda _prompt: (
                {
                    "input_text": "short",
                    "rationale": "preserves the bounded fixture probe",
                },
            ),
        )
        assert child is not None
        materialized = materialize_bounded_child_family(
            AssignmentTechnique(
                id="technique/runtime-limit",
                objective="exercise the reviewed candidate",
                tags=frozenset({"injection"}),
                guidance_ids=(),
                semantic_id="technique/runtime-limit",
                strategy_id="direct",
                transform_id="baseline",
                language="en",
            ),
            child,
        )

        # Reopen the same PostgreSQL data directory and use a new assignment
        # scope. The campaign-wide constraint must reject the immutable parent
        # before setup or interaction reaches the target.
        with Database(DatabaseConfig(data_dir=postgres_dir)) as restarted:
            with restarted.connect() as connection:
                MigrationRunner(connection).apply()
                sessions = CampaignApiSessionRepository(connection)
                async with httpx.AsyncClient() as client:
                    tool = ReviewedApiTargetTool(
                        sessions,
                        lambda _campaign_id: adapter,
                        {},
                        client=client,
                    )
                    retry_context = _sealed_context(campaign_id, adapter).model_copy(
                        update={
                            "call_id": "call-parent-retry",
                            "agent_id": f"{campaign_id}/retry",
                        }
                    )
                    parent_retry = await tool.invoke(
                        retry_context,
                        {"input": "twelve chars"},
                    )
                    assert len(target.http_log) == initial_target_calls
                    accepted = await tool.invoke(
                        retry_context.model_copy(update={"call_id": "call-child"}),
                        {"input": materialized.mutation_seed},
                    )

    assert rejected.is_error is True
    assert rejected.metadata["attempt_disposition"] == "protocol_invalid"
    observations = rejected.metadata["request_constraint_observations"]
    assert observations[0]["source"] == "structured_target_rejection"
    assert observations[0]["maximum"] == 7
    assert observations[0]["evidence_bindings"][0]["observed_maximum"] == 7
    assert protocol_request["rendered_input"] == "twelve chars"
    assert parent_retry.is_error is True
    assert parent_retry.metadata["attempt_disposition"] == "protocol_invalid"
    assert parent_retry.metadata["target_call_budget_charge"] == 0
    durable_observations = parent_retry.metadata["request_constraint_observations"]
    assert durable_observations[0]["preflight_source"] == ("durable_structured_target_rejection")
    assert child["variant"]["parent_identity"]["technique_id"] == parent_id
    assert materialized.mutation_seed == "short"
    assert accepted.is_error is False


@pytest.mark.asyncio
async def test_reviewed_api_tool_rejects_assignment_campaign_adapter_drift_before_effects() -> None:
    campaign_adapter = _adapter()
    assignment_adapter = _adapter()
    assignment_steps = assignment_adapter["api_steps"]
    assert isinstance(assignment_steps, list)
    assignment_interaction = assignment_steps[1]
    assert isinstance(assignment_interaction, dict)
    assignment_interaction["endpoint"] = "different-message"

    class NoSessionActions:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"session action {name} occurred before adapter verification")

    result = await ReviewedApiTargetTool(  # type: ignore[arg-type]
        NoSessionActions(),
        lambda _campaign_id: campaign_adapter,
        {"target-token": "opaque-secret"},
    ).invoke(_sealed_context("campaign/api-context-drift", assignment_adapter), {"input": "probe"})

    assert result.is_error is True
    assert result.metadata["failure_code"] == "adapter_state_integrity"
    assert result.metadata["no_target_effect"] is True
    assert result.metadata["target_effect_attempted"] is False
    assert result.metadata["target_call_budget_charge"] == 0
    assert result.metadata["limit_interaction_count"] == 0
    assert result.metadata["compiled_adapter_digest"] == compiled_target_adapter_digest(
        compile_target_adapter(campaign_adapter)
    )
    assert result.metadata["assignment_compiled_adapter_digest"] == (
        compiled_target_adapter_digest(compile_target_adapter(assignment_adapter))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("compiled_adapter", "compiled_digest"),
    (("{}", None), (None, "sha256:claimed"), ("not-json", "sha256:claimed")),
)
async def test_reviewed_api_tool_rejects_partial_or_malformed_assignment_seal_without_effects(
    compiled_adapter: str | None,
    compiled_digest: str | None,
) -> None:
    class NoSessionActions:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"session action {name} occurred before adapter verification")

    context = _context("campaign/api-invalid-context-seal").model_copy(
        update={
            "compiled_adapter": compiled_adapter,
            "compiled_adapter_digest": compiled_digest,
        }
    )
    result = await ReviewedApiTargetTool(  # type: ignore[arg-type]
        NoSessionActions(),
        lambda _campaign_id: _adapter(),
        {"target-token": "opaque-secret"},
    ).invoke(context, {"input": "probe"})

    assert result.is_error is True
    assert result.metadata["failure_code"] == "adapter_state_integrity"
    assert result.metadata["no_target_effect"] is True
    assert result.metadata["target_call_budget_charge"] == 0


@pytest.mark.asyncio
async def test_reviewed_api_tool_reuses_one_session_and_runs_idempotent_cleanup(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, object], str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path, body, request.headers.get("idempotency-key")))
        assert request.headers["authorization"] == "Bearer opaque-secret"
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "session-42"})
        assert body["sessionId"] == "session-42"
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json={"reply": f"seen:{body['userMessage']}"})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-one"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            peer_adapter = _adapter()
            peer_adapter["target_delivery"] = "inter-agent-message"
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: peer_adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                first = await tool.invoke(_context(campaign_id), {"input": "first"})
                second = await tool.invoke(_context(campaign_id), {"input": "second"})
                await tool.cleanup_assignment(f"{campaign_id}/injection")
                await tool.cleanup_assignment(f"{campaign_id}/validator")

            assert first.is_error is False
            assert json.loads(first.content) == "seen:first"
            assert first.metadata["session_reused"] is False
            assert first.metadata["campaign_id"] == campaign_id
            assert first.metadata["reviewed_target_delivery"] == "inter-agent-message"
            assert [row["id"] for row in first.metadata["target_exchanges"]] == [
                "start",
                "message",
            ]
            assert first.metadata["current_target_exchanges"] == first.metadata["target_exchanges"]
            assert first.metadata["target_exchanges"][1]["request_summary"]["fields"] == [
                "sessionId",
                "userMessage",
            ]
            assert first.metadata["target_exchanges"][1]["response_summary"]["kind"] == "str"
            assert [
                (row["interaction"], row["cleanup"]) for row in first.metadata["target_exchanges"]
            ] == [(False, False), (True, False)]
            assert all(
                isinstance(row["latency_ms"], int) and row["latency_ms"] >= 0
                for row in first.metadata["target_exchanges"]
            )
            assert second.is_error is False
            assert json.loads(second.content) == "seen:second"
            assert second.metadata["session_reused"] is True
            assert [path.rsplit("/", 1)[-1] for path, _, _ in calls] == [
                "start",
                "message",
                "message",
                "abandon",
            ]
            assert calls[0][2] == "start-one"
            assert calls[-1][2] == "abandon-one"
            restored = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=f"{campaign_id}/injection",
            )
            assert restored is not None
            assert restored.state == "cleaned"
            assert restored.cleanup_disposition == "cleanup_completed"
            assert restored.variables["session_id"] == "session-42"
            cleanup_exchange = restored.variables["__cleanup_exchange"]
            assert isinstance(cleanup_exchange["latency_ms"], int)
            assert cleanup_exchange["latency_ms"] >= 0
            trimmed = {key: value for key, value in cleanup_exchange.items() if key != "latency_ms"}
            assert trimmed == {
                "id": "abandon",
                "interaction": False,
                "cleanup": True,
                "request": {"sessionId": "session-42"},
                "response": True,
                "response_body": {"closed": True},
                "status_code": 200,
                "outcome": "acknowledged",
            }


@pytest.mark.asyncio
async def test_reviewed_api_tool_reuses_assignment_scope_after_tool_restart(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        body = json.loads(request.content)
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "session-restart"})
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json={"reply": body["userMessage"]})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-scope-restart"
    assignment_id = f"{campaign_id}/injection"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                first = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "first"})
                second_tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                second = await second_tool.invoke(_context(campaign_id), {"input": "second"})
                await second_tool.cleanup_assignment(assignment_id)
            state = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=assignment_id,
            )

    assert first.is_error is False and second.is_error is False
    assert second.metadata["session_reused"] is True
    assert first.metadata["target_session_id"] == second.metadata["target_session_id"]
    assert calls == ["start", "message", "message", "abandon"]
    assert state is not None and state.state == "cleaned"
    assert state.remote_session_id is None


@pytest.mark.asyncio
async def test_pair_stages_retry_bank_style_resumed_session_after_owner_cleanup(
    tmp_path: Path,
) -> None:
    starts = 0
    active_session: str | None = None
    calls: list[tuple[str, str | None, bool | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_session, starts
        step = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        if step == "start":
            resumed = active_session is not None
            if active_session is None:
                starts += 1
                active_session = f"session-{starts}"
            calls.append((step, active_session, resumed))
            return httpx.Response(
                200,
                json={"sessionId": active_session, "resumed": resumed},
            )
        if step == "message":
            calls.append((step, body.get("userMessage"), None))
            return httpx.Response(200, json={"reply": body["userMessage"]})
        calls.append((step, active_session, None))
        active_session = None
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-pair-scope"
    pair_scope = f"{campaign_id}/agenda-0123456789abcdef"
    control_id = f"{pair_scope}/control"
    direct_id = f"{pair_scope}/direct"
    control_context = _context(campaign_id).model_copy(update={"agent_id": control_id})
    direct_context = _context(campaign_id).model_copy(update={"agent_id": direct_id})
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                first_tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                control = await first_tool.invoke(control_context, {"input": "control"})
                restarted_tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                collided = await restarted_tool.invoke(direct_context, {"input": "direct"})
                states_after_collision = sessions.list_for_campaign(campaign_id)
                await restarted_tool.cleanup_assignment(control_id)
                direct = await restarted_tool.invoke(direct_context, {"input": "direct"})
                await restarted_tool.cleanup_assignment(direct_id)

    assert control.is_error is False
    assert collided.is_error is True
    assert collided.metadata["failure_code"] == "session_isolation_unavailable"
    assert collided.metadata["owner_session_scope_id"] == control_id
    assert collided.metadata["retryable"] is True
    assert direct.is_error is False
    assert direct.metadata["session_reused"] is False
    assert control.metadata["target_session_id"] != direct.metadata["target_session_id"]
    by_scope = {state.session_scope_id: state for state in states_after_collision}
    assert by_scope[control_id].state == "active"
    assert by_scope[control_id].remote_session_id == "session-1"
    assert by_scope[direct_id].state == "cleaned"
    assert by_scope[direct_id].cleanup_disposition == "cleanup_not_owned"
    assert calls == [
        ("start", "session-1", False),
        ("message", "control", None),
        ("start", "session-1", True),
        ("abandon", "session-1", None),
        ("start", "session-2", False),
        ("message", "direct", None),
        ("abandon", "session-2", None),
    ]


@pytest.mark.asyncio
async def test_reviewed_api_tool_rejects_shared_remote_identity_without_closing_owner(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        step = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        calls.append((step, body.get("userMessage")))
        if step == "start":
            return httpx.Response(200, json={"sessionId": "shared-physical-session"})
        if step == "message":
            return httpx.Response(200, json={"reply": body["userMessage"]})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-physical-collision"
    owner_id = f"{campaign_id}/owner"
    claimant_id = f"{campaign_id}/claimant"
    owner_context = _context(campaign_id).model_copy(update={"agent_id": owner_id})
    claimant_context = _context(campaign_id).model_copy(update={"agent_id": claimant_id})
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                owner = await tool.invoke(owner_context, {"input": "owner message"})
                claimant = await tool.invoke(claimant_context, {"input": "claimant message"})
                calls_before_owner_cleanup = list(calls)
                states_before_cleanup = sessions.list_for_campaign(campaign_id)
                await tool.cleanup_assignment(owner_id)
                states_after_cleanup = sessions.list_for_campaign(campaign_id)

    assert owner.is_error is False
    assert claimant.is_error is True
    assert claimant.metadata["failure_code"] == "session_isolation_unavailable"
    assert claimant.metadata["retryable"] is True
    assert claimant.metadata["session_health"] == "unknown"
    assert claimant.metadata["owner_campaign_id"] == campaign_id
    assert claimant.metadata["owner_session_scope_id"] == owner_id
    assert claimant.metadata["limit_interaction_count"] == 0
    assert claimant.metadata["target_call_budget_charge"] == 1
    assert claimant.metadata["acknowledged_capability_evidence"] is False
    assert calls_before_owner_cleanup == [
        ("start", None),
        ("message", "owner message"),
        ("start", None),
    ]
    by_scope = {state.session_scope_id: state for state in states_before_cleanup}
    assert claimant.metadata["compiled_adapter_digest"] == by_scope[claimant_id].adapter_digest
    assert by_scope[owner_id].state == "active"
    assert by_scope[owner_id].remote_session_id == "shared-physical-session"
    assert by_scope[claimant_id].state == "cleaned"
    assert by_scope[claimant_id].remote_session_id is None
    cleaned_by_scope = {state.session_scope_id: state for state in states_after_cleanup}
    assert cleaned_by_scope[owner_id].state == "cleaned"
    assert cleaned_by_scope[owner_id].variables["__cleanup_exchange"]["outcome"] == ("acknowledged")
    assert cleaned_by_scope[claimant_id].cleanup_disposition == "cleanup_not_owned"
    assert calls[-1] == ("abandon", None)


@pytest.mark.asyncio
async def test_retired_scope_claim_is_adopted_on_single_session_target(
    tmp_path: Path,
) -> None:
    """A quarantined terminal claim must not poison the shared physical session.

    On a single-physical-session target every setup resumes the same remote
    session; the prior scope's cleanup retires with the
    cleanup_requirement_unestablished limitation and keeps its claim
    quarantined. A later scope must adopt that claim and proceed with the
    uncertainty journaled instead of failing delivery.
    """
    adapter = _adapter()
    adapter["api_steps"] = adapter["api_steps"][:-1]  # type: ignore[index]
    adapter["cleanup_lifecycle"] = {"status": "not_established", "evidence_refs": []}
    calls: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        step = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        calls.append((step, body.get("userMessage")))
        if step == "start":
            return httpx.Response(200, json={"sessionId": "shared-physical-session"})
        return httpx.Response(200, json={"reply": body["userMessage"]})

    campaign_id = "campaign/api-single-session-adoption"
    owner_id = f"{campaign_id}/owner"
    claimant_id = f"{campaign_id}/claimant"
    owner_context = _context(campaign_id).model_copy(update={"agent_id": owner_id})
    claimant_context = _context(campaign_id).model_copy(update={"agent_id": claimant_id})
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                owner = await tool.invoke(owner_context, {"input": "owner message"})
                await tool.cleanup_assignment(owner_id)
                claimant = await tool.invoke(claimant_context, {"input": "claimant message"})
                by_scope = {
                    state.session_scope_id: state
                    for state in sessions.list_for_campaign(campaign_id)
                }

    assert owner.is_error is False
    assert by_scope[owner_id].state == "ambiguous"
    assert by_scope[owner_id].cleanup_disposition == "cleanup_requirement_unestablished"
    assert claimant.is_error is False
    assert json.loads(claimant.content) == "claimant message"
    assert claimant.metadata["evaluation_uncertain"] is True
    assert by_scope[claimant_id].remote_session_id == "shared-physical-session"
    # The adopted claim moved to the live scope; the retired scope keeps its
    # quarantine record without poisoning the physical session identity.
    assert by_scope[owner_id].remote_session_id is None
    assert calls == [
        ("start", None),
        ("message", "owner message"),
        ("start", None),
        ("message", "claimant message"),
    ]


@pytest.mark.asyncio
async def test_reviewed_api_tool_attacks_unclaimed_resumed_physical_session_advisory(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    adapter["session_creation_transition"] = {
        "source": "inferred_absent_then_active_transition",
        "precondition": {
            "method": "GET",
            "url": "https://target.test/api/scanner/active",
            "status_codes": [404],
            "pointer": "/error",
            "match_values": ["no_active_session"],
        },
        "postcondition": {
            "method": "GET",
            "url": "https://target.test/api/scanner/active",
            "status_codes": [200],
            "pointer": "/sessionId",
            "match_values": ["reviewed-recon-session"],
        },
        "remote_session_id": "reviewed-recon-session",
        "evidence_refs": ["setup-request", "setup-response"],
    }
    adapter["session_freshness_predicate"] = {
        "source": "authoritative_fresh_setup_response",
        "method": "POST",
        "url": "https://target.test/api/scanner/start",
        "status_codes": [200],
        "pointer": "/resumed",
        "match_values": [False],
        "reviewed_remote_session_id": "reviewed-recon-session",
        "evidence_refs": ["setup-action", "setup-request", "setup-response"],
    }
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        step = request.url.path.rsplit("/", 1)[-1]
        calls.append(step)
        if step == "start":
            return httpx.Response(
                200,
                json={"sessionId": "foreign-physical-session", "resumed": True},
            )
        if step == "message":
            return httpx.Response(200, json={"reply": "resumed session response"})
        raise AssertionError("an unproven session must never receive destructive cleanup")

    campaign_id = "campaign/api-unclaimed-resumed-session"
    assignment_id = f"{campaign_id}/injection"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await tool.invoke(
                    _context(campaign_id), {"input": "probe the resumed session"}
                )
                with pytest.raises(_TargetCleanupUnverified):
                    await tool.cleanup_assignment(assignment_id)
            state = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=assignment_id,
            )

    assert result.is_error is False
    assert result.metadata["session_freshness"] == ("authoritative_setup_predicate_mismatch")
    assert result.metadata["evaluation_uncertain"] is True
    assert any(
        item.get("source") == "api_target.fresh_setup_isolation"
        for item in result.metadata["evaluation_uncertainties"]
    )
    assert [exchange["id"] for exchange in result.metadata["target_exchanges"]] == [
        "start",
        "message",
    ]
    assert calls == ["start", "message"]
    assert state is not None
    assert state.state == "ambiguous"
    assert state.remote_session_id == "foreign-physical-session"
    assert state.cleanup_disposition == "cleanup_requirement_unestablished"


@pytest.mark.asyncio
async def test_reviewed_api_tool_does_not_treat_generic_resumed_field_as_lifecycle(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        step = request.url.path.rsplit("/", 1)[-1]
        calls.append(step)
        if step == "start":
            return httpx.Response(
                200,
                json={"sessionId": "ordinary-session", "resumed": True},
            )
        if step == "message":
            return httpx.Response(200, json={"reply": "ordinary domain response"})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-generic-resumed-field"
    assignment_id = f"{campaign_id}/injection"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await tool.invoke(_context(campaign_id), {"input": "probe"})
                await tool.cleanup_assignment(assignment_id)

    assert result.is_error is False
    assert json.loads(result.content) == "ordinary domain response"
    assert result.metadata["remote_target_session"]["kind"] == "opaque_reference"
    assert result.metadata["target_session_id"] != "ordinary-session"
    assert calls == ["start", "message", "abandon"]


@pytest.mark.asyncio
async def test_reviewed_recon_owned_resumed_session_is_attacked_then_cleaned(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    reviewed_session_id = "reviewed-recon-session"
    adapter["session_freshness_predicate"] = {
        "source": "authoritative_fresh_setup_response",
        "method": "POST",
        "url": "https://target.test/api/scanner/start",
        "status_codes": [200],
        "pointer": "/resumed",
        "match_values": [False],
        "reviewed_remote_session_id": reviewed_session_id,
        "evidence_refs": ["setup-request", "setup-response"],
    }
    active_session: str | None = reviewed_session_id
    calls: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_session
        step = request.url.path.rsplit("/", 1)[-1]
        if step == "start":
            resumed = active_session is not None
            if active_session is None:
                active_session = "fresh-worker-session"
            calls.append((step, active_session))
            return httpx.Response(
                200,
                json={"sessionId": active_session, "resumed": resumed},
            )
        if step == "abandon":
            calls.append((step, active_session))
            active_session = None
            return httpx.Response(200, json={"closed": True})
        calls.append((step, active_session))
        return httpx.Response(200, json={"reply": "fresh result"})

    campaign_id = "campaign/api-recon-owned-resumed-session"
    assignment_id = f"{campaign_id}/injection"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                resumed = await tool.invoke(
                    _context(campaign_id), {"input": "delivered on the resumed session"}
                )
                again = await tool.invoke(_context(campaign_id), {"input": "delivered"})
                await tool.cleanup_assignment(assignment_id)

    # The resumed but recon-owned session is attacked with the freshness
    # mismatch recorded as advisory; cleanup still runs because the physical
    # session matches the reviewed ownership evidence.
    assert resumed.is_error is False
    assert resumed.metadata["session_freshness"] == ("authoritative_setup_predicate_mismatch")
    assert resumed.metadata["evaluation_uncertain"] is True
    assert any(
        item.get("source") == "api_target.fresh_setup_isolation"
        for item in resumed.metadata["evaluation_uncertainties"]
    )
    assert resumed.metadata["remote_target_session"]["kind"] == "opaque_reference"
    assert again.is_error is False
    assert again.metadata["remote_target_session"] == resumed.metadata["remote_target_session"]
    assert calls == [
        ("start", reviewed_session_id),
        ("message", reviewed_session_id),
        ("message", reviewed_session_id),
        ("abandon", reviewed_session_id),
    ]


@pytest.mark.asyncio
async def test_reviewed_api_tool_does_not_infer_http_failure_from_json_body(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    adapter["api_steps"][1]["response_selector"] = ""  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "session-500"})
        return httpx.Response(
            200,
            json={
                "error": "Internal Server Error",
                "message": "deepseek_empty_content",
                "statusCode": 500,
            },
        )

    campaign_id = "campaign/api-root-server-error"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is False
    assert json.loads(result.content) == {
        "error": "Internal Server Error",
        "message": "deepseek_empty_content",
        "statusCode": 500,
    }
    assert result.metadata["target_exchanges"][0]["status_code"] == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_body", "selector", "expected"),
    (
        (
            {"bubbles": ["first reply", "second reply"]},
            "/bubbles/0",
            ["first reply", "second reply"],
        ),
        (
            {
                "messages": [
                    {"role": "assistant", "content": "first reply"},
                    {"role": "assistant", "content": "second reply"},
                ]
            },
            "/messages/0/content",
            ["first reply", "second reply"],
        ),
        ({"bubbles": ["only reply"]}, "/bubbles/0", "only reply"),
    ),
)
async def test_reviewed_api_tool_exposes_all_selected_text_siblings(
    tmp_path: Path,
    response_body: dict[str, object],
    selector: str,
    expected: object,
) -> None:
    adapter = _adapter()
    adapter["api_steps"][1]["response_selector"] = selector  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "session-multi-message"})
        return httpx.Response(200, json=response_body)

    campaign_id = "campaign/api-multi-message-response"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is False
    assert json.loads(result.content) == expected
    exchange = result.metadata["current_target_exchanges"][-1]
    assert exchange["response_summary"]["kind"] in {"array", "str"}
    assert exchange["response_body_summary"]["kind"] in {"array", "object"}


@pytest.mark.asyncio
async def test_reviewed_api_tool_preserves_actual_http_500_in_target_exchange(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    adapter["api_steps"][1]["response_selector"] = ""  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "session-http-500"})
        return httpx.Response(
            500,
            json={
                "error": "Internal Server Error",
                "message": "deepseek_empty_content",
                "statusCode": 500,
            },
        )

    campaign_id = "campaign/api-actual-http-500"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is True
    assert result.metadata["failure_code"] == "target_delivery_rejected"
    assert result.metadata["target_delivery_rejected"] is True
    assert result.metadata["session_health"] == "unknown"
    assert result.metadata["retryable"] is True
    assert result.metadata["infrastructure_failure_category"] == "delivery_failure"
    assert result.metadata["limit_interaction_count"] == 1
    assert result.metadata["target_call_budget_charge"] == 2
    assert result.metadata["acknowledged_capability_evidence"] is False
    interaction = next(
        item for item in result.metadata["target_exchanges"] if item["interaction"] is True
    )
    assert interaction["status_code"] == 500
    assert interaction["outcome"] == "target_delivery_rejected"
    assert interaction["response_body_summary"]["fields"] == [
        "error",
        "message",
        "statusCode",
    ]


@pytest.mark.asyncio
async def test_reviewed_api_tool_starts_fresh_session_after_assignment_cleanup(
    tmp_path: Path,
) -> None:
    starts = 0
    calls: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path.endswith("/start"):
            starts += 1
            return httpx.Response(200, json={"sessionId": f"session-{starts}"})
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json={"reply": f"seen:{body['userMessage']}"})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-assignment-reuse"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                first = await tool.invoke(_context(campaign_id), {"input": "first"})
                await tool.cleanup_assignment(f"{campaign_id}/injection")
                second = await tool.invoke(_context(campaign_id), {"input": "second"})
                await tool.cleanup_assignment(f"{campaign_id}/injection")

            assert first.is_error is False
            assert second.is_error is False
            assert first.metadata["target_session_generation"] == 1
            assert second.metadata["target_session_generation"] == 2
            assert first.metadata["target_session_id"] != second.metadata["target_session_id"]
            assert first.metadata["target_session_id"].startswith(
                f"api:{campaign_id}:target-refund:scope:{campaign_id}/injection:generation:"
            )
            assert second.metadata["session_reused"] is False
            assert [path.rsplit("/", 1)[-1] for path, _ in calls] == [
                "start",
                "message",
                "abandon",
                "start",
                "message",
                "abandon",
            ]
            assert calls[1][1]["sessionId"] == "session-1"
            assert calls[4][1]["sessionId"] == "session-2"
            restored = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=f"{campaign_id}/injection",
            )
            assert restored is not None
            assert restored.state == "cleaned"
            assert restored.variables["session_id"] == "session-2"


@pytest.mark.asyncio
async def test_reviewed_api_tool_runs_lifecycle_without_remote_cleanup(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    adapter["api_steps"] = adapter["api_steps"][:-1]  # type: ignore[index]
    adapter["cleanup_lifecycle"] = {
        "status": "not_required",
        "evidence_refs": ["reviewed:stateless-contract"],
    }
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "session-no-cleanup"})
        return httpx.Response(200, json={"reply": "observed"})

    campaign_id = "campaign/api-no-cleanup"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await tool.invoke(_context(campaign_id), {"input": "probe"})
                await tool.cleanup_assignment(f"{campaign_id}/injection")
            restored = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=f"{campaign_id}/injection",
            )

    assert result.is_error is False
    assert json.loads(result.content) == "observed"
    assert calls == ["/api/scanner/start", "/api/scanner/message"]
    assert restored is not None
    assert restored.state == "cleaned"
    assert restored.cleanup_disposition == "no_cleanup_obligation"


@pytest.mark.asyncio
async def test_unknown_cleanup_requirement_survives_restart_without_blocking_completion(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    adapter["api_steps"] = adapter["api_steps"][:-1]  # type: ignore[index]
    adapter["cleanup_lifecycle"] = {"status": "not_established", "evidence_refs": []}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "session-cleanup-unknown"})
        return httpx.Response(200, json={"reply": "observed"})

    campaign_id = "campaign/api-cleanup-unknown"
    assignment_id = f"{campaign_id}/injection"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                first = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "first"})
                restarted = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                second = await restarted.invoke(_context(campaign_id), {"input": "second"})
                # A reviewed adapter with no executable cleanup action must not
                # fail the cleanup node and park the campaign: the scope is
                # retired without a target call, the physical-session claim
                # stays quarantined, and completion records the limitation.
                await restarted.cleanup_assignment(assignment_id)
            restored = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=assignment_id,
            )

    assert first.is_error is False and second.is_error is False
    assert second.metadata["session_reused"] is True
    assert calls == [
        "/api/scanner/start",
        "/api/scanner/message",
        "/api/scanner/message",
    ]
    assert restored is not None
    assert restored.state == "ambiguous"
    assert restored.remote_session_id == "session-cleanup-unknown"
    assert restored.cleanup_disposition == "cleanup_requirement_unestablished"


@pytest.mark.asyncio
async def test_reviewed_api_tool_retries_no_response_transport_timeout(
    tmp_path: Path,
) -> None:
    attempts: list[str] = []
    adapter = _adapter()
    adapter["api_steps"][1]["replay_after_ambiguous_result"] = True  # type: ignore[index]
    adapter["api_steps"][1]["idempotency_key"] = "message-retry"  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "session-timeout"})
        if request.url.path.endswith("/message"):
            attempts.append("message")
            if len(attempts) == 1:
                raise httpx.ReadTimeout("no response within 30s", request=request)
            return httpx.Response(200, json={"reply": "recovered-after-timeout"})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-timeout-retry"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await tool.invoke(_context(campaign_id), {"input": "probe"})
                await tool.cleanup_assignment(f"{campaign_id}/injection")
            restored = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=f"{campaign_id}/injection",
            )

    assert result.is_error is False
    assert json.loads(result.content) == "recovered-after-timeout"
    assert attempts == ["message", "message"]
    assert result.metadata["target_call_budget_charge"] == 3
    assert result.metadata["limit_interaction_count"] == 1
    assert restored is not None
    assert restored.state == "cleaned"


@pytest.mark.asyncio
async def test_reviewed_api_tool_rejects_ambiguous_replay_without_idempotency_key() -> None:
    adapter = _adapter()
    adapter["api_steps"][1]["replay_after_ambiguous_result"] = True  # type: ignore[index]

    resolved = ReviewedApiTargetTool(  # type: ignore[arg-type]
        None,
        lambda _campaign_id: adapter,
        {"target-token": "opaque-secret"},
    )._adapter("campaign/api-unsafe-replay", "target-refund")

    assert resolved.steps[1].replay_after_ambiguous_result is False


@pytest.mark.asyncio
async def test_reviewed_api_tool_bounds_no_response_transport_retries(
    tmp_path: Path,
) -> None:
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "session-down"})
        if request.url.path.endswith("/message"):
            attempts.append("message")
            raise httpx.ReadTimeout("no response within 30s", request=request)
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-timeout-bound"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await tool.invoke(_context(campaign_id), {"input": "probe"})
            restored = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=f"{campaign_id}/injection",
            )

    assert result.is_error is True
    assert result.metadata["infrastructure_failure_category"] == "adapter_timeout"
    assert result.metadata["target_call_budget_charge"] == 2
    assert result.metadata["limit_interaction_count"] == 1
    assert len(result.metadata["current_target_exchanges"]) == 1
    assert result.metadata["current_target_exchanges"][0]["id"] == "start"
    assert attempts == ["message"]
    assert restored is not None
    assert restored.state == "ambiguous"


@pytest.mark.asyncio
async def test_reviewed_api_tool_types_missing_response_pointer(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"reply": "unused"})

    campaign_id = "campaign/api-missing-pointer"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is True
    assert result.metadata["infrastructure_failure_category"] == "missing_response_pointer"
    assert result.metadata["limit_interaction_count"] == 0
    assert result.metadata["target_call_budget_charge"] == 1
    assert result.metadata["current_target_exchanges"][0]["status_code"] == 200


@pytest.mark.asyncio
async def test_missing_required_interaction_capture_preserves_attempt_telemetry(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    adapter["api_steps"][1]["captures"] = {"receipt": "/receipt"}  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "session-pointer"})
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json={"reply": "delivered", "error": "prose only"})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-interaction-capture-missing"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is True
    assert result.metadata["infrastructure_failure_category"] == "missing_response_pointer"
    assert result.metadata["limit_interaction_count"] == 2
    assert result.metadata["target_call_budget_charge"] == 4
    assert "target_session_exhausted" not in result.metadata
    assert [
        exchange["outcome"]
        for exchange in result.metadata["current_target_exchanges"]
        if exchange["interaction"]
    ] == ["missing_response_pointer", "missing_response_pointer"]


@pytest.mark.asyncio
async def test_reviewed_api_tool_preserves_interaction_response_after_selector_miss(
    tmp_path: Path,
) -> None:
    starts = 0
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path.endswith("/start"):
            starts += 1
            return httpx.Response(200, json={"sessionId": f"session-{starts}"})
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json={"gameStatus": "ended", "riskScore": 45})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-pointer-rotation"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await tool.invoke(_context(campaign_id), {"input": "probe"})
                await tool.cleanup_assignment(f"{campaign_id}/injection")

    assert result.is_error is False
    response = {"gameStatus": "ended", "riskScore": 45}
    assert json.loads(result.content) == response
    assert result.metadata["evaluation_uncertain"] is True
    assert "pointer_miss_session_rotations" not in result.metadata
    assert result.metadata["target_session_generation"] == 1
    exchange = result.metadata["target_exchanges"][-1]
    assert exchange["response_summary"]["kind"] == "object"
    assert exchange["response_body_summary"]["fields"] == ["gameStatus", "riskScore"]
    assert exchange["outcome"] == "evaluation_uncertain"
    assert calls == ["start", "message", "abandon"]


@pytest.mark.asyncio
async def test_reviewed_api_tool_does_not_rotate_after_interaction_selector_miss(
    tmp_path: Path,
) -> None:
    starts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        if request.url.path.endswith("/start"):
            starts += 1
            return httpx.Response(200, json={"sessionId": f"session-{starts}"})
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json={"gameStatus": "ended"})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-pointer-bound"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is False
    assert json.loads(result.content) == {"gameStatus": "ended"}
    assert result.metadata["evaluation_uncertain"] is True
    assert result.metadata["target_session_id"].endswith(":generation:1")
    assert result.metadata["target_session_generation"] == 1
    assert starts == 1


@pytest.mark.asyncio
async def test_reviewed_api_tool_preserves_interaction_error_envelope(
    tmp_path: Path,
) -> None:
    starts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        if request.url.path.endswith("/start"):
            starts += 1
            return httpx.Response(200, json={"sessionId": f"session-{starts}"})
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json={"error": "domain warning", "data": {"ok": True}})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-pointer-exhausted"
    adapter = _adapter()
    # An arbitrary error-shaped field is domain data unless the reviewed adapter
    # explicitly declares a lifecycle-terminal predicate.
    adapter["api_steps"][1]["response_selector"] = ""
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is False
    assert json.loads(result.content) == {"data": {"ok": True}, "error": "domain warning"}
    assert result.metadata["evaluation_uncertain"] is True
    assert result.metadata["target_exchanges"][-1]["outcome"] == "evaluation_uncertain"
    assert result.metadata["target_session_generation"] == 1
    assert "target_session_recovered" not in result.metadata
    assert starts == 1


@pytest.mark.asyncio
async def test_reviewed_api_tool_preserves_applied_terminal_response_without_replay(
    tmp_path: Path,
) -> None:
    starts = messages = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal messages, starts
        if request.url.path.endswith("/start"):
            starts += 1
            return httpx.Response(200, json={"sessionId": f"session-{starts}"})
        if request.url.path.endswith("/message"):
            messages += 1
            if starts == 1:
                return httpx.Response(
                    200,
                    json={
                        "text": "terminal target reply",
                        "gameStatus": "lost",
                        "messagesRemaining": 0,
                    },
                )
            return httpx.Response(200, json={"text": "fresh distinct reply"})
        return httpx.Response(200, json={"closed": True})

    adapter = _adapter()
    adapter["api_steps"][1]["response_selector"] = "/text"  # type: ignore[index]
    adapter["api_steps"][1]["terminal_response_contract"] = {  # type: ignore[index]
        "conditions": [
            {
                "pointer": "/gameStatus",
                "match_values": ["lost"],
                "status_codes": [200],
                "effect_applied": True,
                "replay_safe": False,
            },
            {
                "pointer": "/error",
                "match_values": ["Game already ended"],
                "status_codes": [400],
                "effect_applied": False,
                "replay_safe": True,
            },
        ]
    }
    campaign_id = "campaign/api-applied-terminal-response"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                terminal = await tool.invoke(_context(campaign_id), {"input": "terminal probe"})
                terminal_state = sessions.load(
                    campaign_id,
                    target_id="target-refund",
                    session_scope_id=f"{campaign_id}/injection",
                )
                repeated = await tool.invoke(_context(campaign_id), {"input": "terminal probe"})
                distinct = await tool.invoke(_context(campaign_id), {"input": "distinct probe"})

    assert terminal.is_error is False
    assert json.loads(terminal.content) == "terminal target reply"
    interaction = terminal.metadata["current_target_exchanges"][-1]
    assert interaction["terminal_response_observation"]["matched"] is True
    assert interaction["evaluation_uncertain"] is True
    assert terminal_state is not None and terminal_state.pending_step is None
    assert "__terminal_exchanges" not in terminal_state.variables
    assert repeated.is_error is False
    assert distinct.is_error is False
    assert json.loads(distinct.content) == "terminal target reply"
    assert distinct.metadata["target_session_generation"] == 1
    assert starts == 1
    assert messages == 3


@pytest.mark.asyncio
async def test_reviewed_api_tool_preserves_applied_terminal_response_on_http_4xx(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "session-terminal-4xx"})
        if request.url.path.endswith("/message"):
            return httpx.Response(
                409,
                json={"reply": "final target reply", "gameStatus": "won"},
            )
        return httpx.Response(200, json={"closed": True})

    adapter = _adapter()
    adapter["api_steps"][1]["terminal_response_contract"] = {  # type: ignore[index]
        "pointer": "/gameStatus",
        "match_values": ["won"],
        "status_codes": [409],
        "effect_applied": True,
        "replay_safe": False,
    }
    campaign_id = "campaign/api-applied-terminal-4xx"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "terminal probe"})

    assert result.is_error is True
    assert result.metadata["failure_code"] == "target_delivery_rejected"
    interaction = result.metadata["current_target_exchanges"][-1]
    assert interaction["status_code"] == 409
    assert interaction["outcome"] == "target_delivery_rejected"
    assert interaction["response_body_summary"]["fields"] == ["gameStatus", "reply"]


@pytest.mark.asyncio
async def test_reviewed_api_tool_rotates_for_exact_declared_terminal_error(
    tmp_path: Path,
) -> None:
    starts = messages = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal messages, starts
        if request.url.path.endswith("/start"):
            starts += 1
            return httpx.Response(200, json={"sessionId": f"session-{starts}"})
        if request.url.path.endswith("/message"):
            messages += 1
            if starts == 1:
                return httpx.Response(400, json={"error": "Game already ended"})
            return httpx.Response(200, json={"reply": "replayed on fresh session"})
        return httpx.Response(200, json={"closed": True})

    adapter = _adapter()
    adapter["api_steps"][1]["terminal_response_contract"] = {  # type: ignore[index]
        "conditions": [
            {
                "pointer": "/gameStatus",
                "match_values": ["lost"],
                "status_codes": [200],
                "effect_applied": True,
                "replay_safe": False,
            },
            {
                "pointer": "/error",
                "match_values": ["Game already ended"],
                "status_codes": [400],
                "effect_applied": False,
                "replay_safe": True,
            },
        ]
    }
    campaign_id = "campaign/api-declared-terminal-error"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is True
    assert result.metadata["failure_code"] == "target_delivery_rejected"
    assert result.metadata["target_exchanges"][-1]["status_code"] == 400
    assert result.metadata["target_exchanges"][-1]["outcome"] == "target_delivery_rejected"
    assert result.metadata["target_exchanges"][-1]["response_body_summary"]["fields"] == ["error"]
    assert starts == 1
    assert messages == 1


@pytest.mark.asyncio
async def test_reviewed_api_tool_rotates_and_replays_only_with_reviewed_safe_contract(
    tmp_path: Path,
) -> None:
    starts = 0
    messages = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal messages, starts
        if request.url.path.endswith("/start"):
            starts += 1
            return httpx.Response(200, json={"sessionId": f"session-{starts}"})
        if request.url.path.endswith("/message"):
            messages += 1
            if starts == 1:
                return httpx.Response(200, json={"code": "SESSION_ENDED", "error": "ended"})
            return httpx.Response(200, json={"reply": "fresh session reply"})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-terminal-no-progress"
    adapter = _adapter()
    adapter["api_steps"][1]["terminal_response_contract"] = {
        "pointer": "/code",
        "match_values": ["SESSION_ENDED"],
        "effect_applied": False,
        "replay_safe": True,
    }
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})
            state = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=f"{campaign_id}/injection",
            )

    assert result.is_error is False
    assert result.metadata["target_session_generation"] == 1
    interaction = result.metadata["target_exchanges"][-1]
    assert interaction["terminal_response_observation"]["matched"] is True
    assert interaction["outcome"] == "evaluation_uncertain"
    assert starts == 1
    assert messages == 1
    assert state is not None and state.state == "active" and state.generation == 1


@pytest.mark.asyncio
async def test_reviewed_api_tool_does_not_replay_without_reviewed_safety_attestation(
    tmp_path: Path,
) -> None:
    starts = messages = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal messages, starts
        if request.url.path.endswith("/start"):
            starts += 1
            return httpx.Response(200, json={"sessionId": f"session-{starts}"})
        if request.url.path.endswith("/message"):
            messages += 1
            if json.loads(request.content)["userMessage"] == "distinct probe":
                return httpx.Response(200, json={"reply": "distinct effect completed"})
            return httpx.Response(200, json={"code": "SESSION_ENDED", "error": "ended"})
        return httpx.Response(200, json={"closed": True})

    adapter = _adapter()
    adapter["api_steps"][1]["terminal_response_contract"] = {
        "pointer": "/code",
        "match_values": ["SESSION_ENDED"],
        "effect_applied": False,
        "replay_safe": False,
    }
    campaign_id = "campaign/api-terminal-unsafe-replay"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await tool.invoke(_context(campaign_id), {"input": "probe"})
                repeated = await tool.invoke(_context(campaign_id), {"input": "probe"})
                distinct = await tool.invoke(_context(campaign_id), {"input": "distinct probe"})
            state = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=f"{campaign_id}/injection",
            )

    assert result.is_error is False
    assert repeated.is_error is False
    assert distinct.is_error is False
    assert json.loads(distinct.content) == "distinct effect completed"
    assert starts == 1
    assert messages == 3
    assert state is not None and state.state == "active" and state.generation == 1


@pytest.mark.asyncio
async def test_reviewed_api_tool_stops_after_one_safe_rotation_despite_changed_body_shape(
    tmp_path: Path,
) -> None:
    starts = messages = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal messages, starts
        if request.url.path.endswith("/start"):
            starts += 1
            return httpx.Response(200, json={"sessionId": f"session-{starts}"})
        if request.url.path.endswith("/message"):
            messages += 1
            request_body = json.loads(request.content)
            if request_body["userMessage"] == "distinct probe" and starts >= 4:
                return httpx.Response(200, json={"reply": "distinct effect completed"})
            body = (
                {"code": "SESSION_ENDED", "error": "first prose"}
                if starts in {1, 3}
                else {"code": "SESSION_ENDED", "detail": {"changed": "shape"}}
            )
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"closed": True})

    adapter = _adapter()
    adapter["api_steps"][1]["terminal_response_contract"] = {
        "pointer": "/code",
        "match_values": ["SESSION_ENDED"],
        "effect_applied": False,
        "replay_safe": True,
    }
    campaign_id = "campaign/api-terminal-no-progress"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await tool.invoke(_context(campaign_id), {"input": "probe"})
                repeated = await tool.invoke(_context(campaign_id), {"input": "probe"})
                distinct = await tool.invoke(_context(campaign_id), {"input": "distinct probe"})
            state = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=f"{campaign_id}/injection",
            )

    assert result.is_error is False
    assert repeated.is_error is False
    assert distinct.is_error is False
    assert result.metadata["target_exchanges"][-1]["terminal_response_observation"] == {
        "matched": True,
        "effect_applied": False,
        "replay_safe": True,
    }
    assert starts == 1
    assert messages == 3
    assert state is not None and state.state == "active" and state.generation == 1


@pytest.mark.asyncio
async def test_safe_rotation_disposition_survives_crash_after_generation_two_create(
    tmp_path: Path,
) -> None:
    starts = messages = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal messages, starts
        if request.url.path.endswith("/start"):
            starts += 1
            return httpx.Response(200, json={"sessionId": f"session-{starts}"})
        if request.url.path.endswith("/message"):
            messages += 1
            return httpx.Response(200, json={"code": "SESSION_ENDED", "shape": starts})
        return httpx.Response(200, json={"closed": True})

    adapter = _adapter()
    adapter["api_steps"][1]["terminal_response_contract"] = {
        "pointer": "/code",
        "match_values": ["SESSION_ENDED"],
        "effect_applied": False,
        "replay_safe": True,
    }
    campaign_id = "campaign/api-terminal-crash-after-rotate"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            original_create = sessions.create
            crashed = False

            def crash_after_second_create(*args, **kwargs):
                nonlocal crashed
                state = original_create(*args, **kwargs)
                if state.generation == 2 and not crashed:
                    crashed = True
                    raise RuntimeError("simulated process loss after generation-2 create")
                return state

            sessions.create = crash_after_second_create  # type: ignore[method-assign]
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                first_tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                first = await first_tool.invoke(_context(campaign_id), {"input": "probe"})
                persisted = sessions.load(
                    campaign_id,
                    target_id="target-refund",
                    session_scope_id=f"{campaign_id}/injection",
                )
                assert first.is_error is False
                assert persisted is not None and persisted.generation == 1
                assert persisted.state == "active" and persisted.pending_step is None
                assert "__terminal_replay_dispositions" not in persisted.variables

                sessions.create = original_create  # type: ignore[method-assign]
                restarted = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await restarted.invoke(_context(campaign_id), {"input": "probe"})
            state = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=f"{campaign_id}/injection",
            )

    assert result.is_error is False
    assert starts == 1
    assert messages == 2
    assert state is not None and state.generation == 1 and state.state == "active"


@pytest.mark.asyncio
async def test_terminal_exchange_survives_crash_before_cleanup_and_restart(
    tmp_path: Path,
) -> None:
    starts = messages = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal messages, starts
        if request.url.path.endswith("/start"):
            starts += 1
            return httpx.Response(200, json={"sessionId": f"session-{starts}"})
        if request.url.path.endswith("/message"):
            messages += 1
            if starts == 1:
                return httpx.Response(200, json={"code": "SESSION_ENDED"})
            return httpx.Response(200, json={"reply": "continued after recovery"})
        return httpx.Response(200, json={"closed": True})

    adapter = _adapter()
    adapter["api_steps"][1]["terminal_response_contract"] = {
        "pointer": "/code",
        "match_values": ["SESSION_ENDED"],
        "effect_applied": False,
        "replay_safe": True,
    }
    campaign_id = "campaign/api-terminal-crash-restart"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                first_tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                cleanup = first_tool._cleanup_locked

                async def crash_before_cleanup(
                    _campaign_id: str,
                    _target_id: str,
                    _session_scope_id: str,
                ) -> None:
                    raise asyncio.CancelledError

                first_tool._cleanup_locked = crash_before_cleanup  # type: ignore[method-assign]
                first = await first_tool.invoke(_context(campaign_id), {"input": "probe"})
                persisted = sessions.load(
                    campaign_id,
                    target_id="target-refund",
                    session_scope_id=f"{campaign_id}/injection",
                )
                assert first.is_error is False
                assert persisted is not None and persisted.state == "active"
                assert persisted.pending_step is None
                assert "__terminal_rotation_required" not in persisted.variables
                assert "__terminal_exchanges" not in persisted.variables
                first_tool._cleanup_locked = cleanup  # type: ignore[method-assign]

                restarted = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await restarted.invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is False
    assert json.loads(result.content) == {"code": "SESSION_ENDED"}
    assert result.metadata["target_session_generation"] == 1
    assert (
        result.metadata["target_exchanges"][-1]["terminal_response_observation"]["matched"] is True
    )
    assert starts == 1
    assert messages == 2


async def test_reviewed_api_tool_types_unhealthy_session_before_delivery() -> None:
    sessions = SimpleNamespace(
        create=lambda *_args, **_kwargs: SimpleNamespace(pending_step=None, state="cleaned"),
        load=lambda *_args, **_kwargs: None,
        list_for_campaign=lambda *_args, **_kwargs: (),
    )

    result = await ReviewedApiTargetTool(
        sessions,
        lambda _campaign_id: _adapter(),
        {"target-token": "opaque-secret"},
    ).invoke(_context("campaign/api-cleaned"), {"input": "probe"})

    assert result.is_error is True
    assert result.metadata["infrastructure_failure_category"] == "unhealthy_target_session"


@pytest.mark.asyncio
async def test_reviewed_api_tool_cleans_ambiguous_session_before_next_assignment(
    tmp_path: Path,
) -> None:
    messages = 0
    calls: list[tuple[str, dict[str, object], str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal messages
        calls.append(
            (
                request.url.path,
                json.loads(request.content),
                request.headers.get("idempotency-key"),
            )
        )
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "session-ambiguous"})
        if request.url.path.endswith("/message"):
            messages += 1
            if messages == 1:
                raise httpx.ReadError(
                    "response lost after target accepted request", request=request
                )
            return httpx.Response(200, json={"reply": "recovered"})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-ambiguous"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                failed = await tool.invoke(_context(campaign_id), {"input": "one"})
                recovered = await tool.invoke(_context(campaign_id), {"input": "two"})
                await tool.cleanup_assignment(f"{campaign_id}/injection")

            assert failed.is_error is True
            assert recovered.is_error is False
            assert json.loads(recovered.content) == "recovered"
            assert messages == 2
            assert [path.rsplit("/", 1)[-1] for path, _, _ in calls] == [
                "start",
                "message",
                "abandon",
                "start",
                "message",
                "abandon",
            ]
            restored = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=f"{campaign_id}/injection",
            )
            assert restored is not None
            assert restored.state == "cleaned"


@pytest.mark.asyncio
async def test_reviewed_api_tool_emits_daemon_derived_typed_oracle_observation(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    message = adapter["api_steps"][1]  # type: ignore[index]
    message["oracle_predicate"] = {  # type: ignore[index]
        "oracle_digest": "sha256:" + "7" * 64,
        "pointer": "/verdict",
        "match_values": ["violated"],
        "modality": "text",
        "turn": "single",
        "language": "en",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "session-oracle"})
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json={"reply": "ordinary prose", "verdict": "violated"})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-oracle"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})

    exchange = result.metadata["target_exchanges"][-1]
    assert isinstance(exchange["latency_ms"], int) and exchange["latency_ms"] >= 0
    observation = exchange["oracle_observation"]
    assert observation["matched"] is True
    assert observation["selected_digest"].startswith("sha256:")
    assert observation["binding_digest"].startswith("sha256:")
    assert observation["observation_digest"].startswith("sha256:")


@pytest.mark.asyncio
async def test_reviewed_api_tool_preserves_interaction_on_expected_status_mismatch(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "session-status"})
        return httpx.Response(
            409,
            json={"reply": "target rejected the premise", "reason": "policy"},
        )

    campaign_id = "campaign/api-status-mismatch"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is True
    assert result.metadata["failure_code"] == "target_delivery_rejected"
    assert result.metadata["acknowledged_capability_evidence"] is False
    assert result.metadata["limit_interaction_count"] == 1
    assert result.metadata["target_call_budget_charge"] == 2
    assert result.metadata["evaluation_uncertain"] is True
    exchange = result.metadata["target_exchanges"][-1]
    assert exchange["status_code"] == 409
    assert exchange["expected_status"] == 200
    assert exchange["response_body_summary"]["fields"] == ["reason", "reply"]
    assert exchange["outcome"] == "target_delivery_rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("oracle_digest", "match_values", "expected_error"),
    (
        ("sha256:" + "7" * 64, [float("inf")], "JSON scalars"),
        ("sha256:short", ["violated"], "SHA-256 oracle digest"),
    ),
)
async def test_reviewed_api_tool_records_uncertain_oracle_metadata_without_blocking(
    tmp_path: Path,
    oracle_digest: str,
    match_values: list[object],
    expected_error: str,
) -> None:
    adapter = _adapter()
    adapter["api_steps"][1]["oracle_predicate"] = {  # type: ignore[index]
        "oracle_digest": oracle_digest,
        "pointer": "/verdict",
        "match_values": match_values,
        "modality": "text",
        "turn": "single",
        "language": "und",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "session-invalid-oracle"})
        return httpx.Response(200, json={"reply": "target response preserved"})

    campaign_id = "campaign/invalid-oracle"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is False
    assert json.loads(result.content) == "target response preserved"
    assert result.metadata["evaluation_uncertain"] is True
    assert any(
        expected_error in reason["reason"] for reason in result.metadata["evaluation_uncertainties"]
    )
    exchange = result.metadata["target_exchanges"][-1]
    assert exchange["outcome"] == "evaluation_uncertain"
    assert exchange["response_body_summary"]["fields"] == ["reply"]


@pytest.mark.asyncio
async def test_reviewed_api_tool_preserves_setup_response_when_status_metadata_differs(
    tmp_path: Path,
) -> None:
    adapter = _adapter()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(201, json={"sessionId": "session-created"})
        return httpx.Response(200, json={"reply": "continued after setup"})

    campaign_id = "campaign/setup-status-mismatch"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is False
    assert json.loads(result.content) == "continued after setup"
    assert result.metadata["evaluation_uncertain"] is True
    setup = result.metadata["target_exchanges"][0]
    assert setup["status_code"] == 201
    assert setup["expected_status"] == 200
    assert setup["outcome"] == "evaluation_uncertain"
    assert setup["response_body_summary"]["fields"] == ["sessionId"]


@pytest.mark.asyncio
async def test_compiled_browser_api_uses_authenticated_context_transport(tmp_path: Path) -> None:
    adapter = _adapter()
    adapter.pop("credential_handle")
    adapter["api_transport"] = "browser_context"
    calls: list[tuple[str, str, object, dict[str, str]]] = []

    async def request(
        campaign_id: str,
        method: str,
        url: str,
        payload: object,
        headers: dict[str, str],
    ) -> tuple[int, object]:
        calls.append((campaign_id, url, payload, headers))
        if url.endswith("/start"):
            return 200, {"sessionId": "browser-authenticated"}
        if url.endswith("/message"):
            assert payload == {
                "sessionId": "browser-authenticated",
                "userMessage": "probe",
            }
            return 200, {"reply": "authenticated reply"}
        return 200, {"closed": True}

    campaign_id = "campaign/browser-context-api"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            tool = ReviewedApiTargetTool(
                sessions,
                lambda _campaign_id: adapter,
                {},
                browser_api_requester=request,
            )
            result = await tool.invoke(_context(campaign_id), {"input": "probe"})
            await tool.cleanup_assignment(f"{campaign_id}/injection")

    assert result.is_error is False
    assert json.loads(result.content) == "authenticated reply"
    assert [url.rsplit("/", 1)[-1] for _, url, _, _ in calls] == [
        "start",
        "message",
        "abandon",
    ]
    assert all(item[0] == campaign_id for item in calls)


@pytest.mark.asyncio
async def test_compiled_browser_api_fails_closed_without_context_transport(tmp_path: Path) -> None:
    adapter = _adapter()
    adapter.pop("credential_handle")
    adapter["api_transport"] = "browser_context"
    campaign_id = "campaign/missing-browser-context-api"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            result = await ReviewedApiTargetTool(
                _prepare(connection, campaign_id),
                lambda _campaign_id: adapter,
                {},
            ).invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is True
    assert result.content == "reviewed browser-context API transport is unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_step", ("start", "message"))
async def test_browser_context_transport_failure_marks_setup_or_interaction_ambiguous(
    tmp_path: Path, failed_step: str
) -> None:
    adapter = _adapter()
    adapter.pop("credential_handle")
    adapter["api_transport"] = "browser_context"

    async def request(
        _campaign_id: str,
        _method: str,
        url: str,
        _payload: object,
        _headers: dict[str, str],
    ) -> tuple[int, object]:
        step = url.rsplit("/", 1)[-1]
        if step == failed_step:
            raise RuntimeError("browser context closed")
        if step == "start":
            return 200, {"sessionId": "browser-session"}
        return 200, {"reply": "ok"}

    campaign_id = f"campaign/browser-{failed_step}-failure"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            result = await ReviewedApiTargetTool(
                sessions,
                lambda _campaign_id: adapter,
                {},
                browser_api_requester=request,
            ).invoke(_context(campaign_id), {"input": "probe"})
            restored = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=f"{campaign_id}/injection",
            )

    assert result.is_error is True
    assert result.content == "browser-context API transport failed: browser context closed"
    assert restored is not None
    assert restored.state == "ambiguous"
    assert restored.pending_step is not None
    assert restored.pending_step["id"] == failed_step


@pytest.mark.asyncio
async def test_browser_context_cleanup_failure_marks_session_ambiguous(tmp_path: Path) -> None:
    adapter = _adapter()
    adapter.pop("credential_handle")
    adapter["api_transport"] = "browser_context"

    async def request(
        _campaign_id: str,
        _method: str,
        url: str,
        _payload: object,
        _headers: dict[str, str],
    ) -> tuple[int, object]:
        step = url.rsplit("/", 1)[-1]
        if step == "start":
            return 200, {"sessionId": "browser-session"}
        if step == "message":
            return 200, {"reply": "ok"}
        raise RuntimeError("browser context closed during cleanup")

    campaign_id = "campaign/browser-cleanup-failure"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            tool = ReviewedApiTargetTool(
                sessions,
                lambda _campaign_id: adapter,
                {},
                browser_api_requester=request,
            )
            result = await tool.invoke(_context(campaign_id), {"input": "probe"})
            with pytest.raises(RuntimeError, match="browser-context API transport failed"):
                await tool.cleanup_assignment(f"{campaign_id}/injection")
            restored = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=f"{campaign_id}/injection",
            )

    assert result.is_error is False
    assert restored is not None
    assert restored.state == "ambiguous"
    assert restored.pending_step is not None
    assert restored.pending_step["id"] == "abandon"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body"),
    (
        (400, "plain-text rejection"),
        (503, "<html>temporarily unavailable</html>"),
    ),
)
async def test_browser_context_invalid_json_marks_interaction_ambiguous(
    tmp_path: Path,
    status_code: int,
    body: str,
) -> None:
    adapter = _adapter()
    adapter.pop("credential_handle")
    adapter["api_transport"] = "browser_context"

    async def request(
        _campaign_id: str,
        _method: str,
        url: str,
        _payload: object,
        _headers: dict[str, str],
    ) -> tuple[int, object]:
        if url.endswith("/start"):
            return 200, {"sessionId": "browser-session"}
        return status_code, body

    campaign_id = "campaign/browser-invalid-json"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            result = await ReviewedApiTargetTool(
                sessions,
                lambda _campaign_id: adapter,
                {},
                browser_api_requester=request,
            ).invoke(_context(campaign_id), {"input": "probe"})
            restored = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=f"{campaign_id}/injection",
            )

    assert result.is_error is True
    assert result.metadata["failure_code"] == "target_delivery_rejected"
    assert result.metadata["target_delivery_rejected"] is True
    assert result.metadata["session_health"] == "unknown"
    assert result.metadata["retryable"] is True
    assert result.metadata["limit_interaction_count"] == 1
    assert result.metadata["target_call_budget_charge"] == 2
    assert result.metadata["acknowledged_capability_evidence"] is False
    exchange = result.metadata["current_target_exchanges"][-1]
    assert exchange["id"] == "message"
    assert exchange["status_code"] == status_code
    assert exchange["response_summary"]["kind"] == "str"
    assert exchange["response_body_summary"]["kind"] == "str"
    assert exchange["outcome"] == "target_delivery_rejected"
    assert restored is not None
    assert restored.state == "active"
    assert restored.pending_step is None


@pytest.mark.asyncio
async def test_reviewed_api_tool_reconciles_migrated_legacy_session_before_scoped_work(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str | None]] = []
    adapter = _adapter()

    def handler(request: httpx.Request) -> httpx.Response:
        step = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        calls.append((step, body.get("sessionId")))
        if step == "start":
            return httpx.Response(200, json={"sessionId": "scoped-session"})
        if step == "message":
            return httpx.Response(200, json={"reply": "scoped reply"})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-migrated-legacy-runtime"
    migrations = load_migrations()
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            runner = MigrationRunner(connection)
            runner.apply(migrations[:65])
            sessions = _prepare(connection, campaign_id)
            connection.execute(
                """INSERT INTO merlin_storage.campaign_api_sessions
                       (campaign_id, target_id, adapter_digest, state, variables)
                   VALUES (%s, %s, %s, 'active', %s::jsonb)""",
                (
                    campaign_id,
                    "target-refund",
                    executable_adapter_digest(adapter),
                    json.dumps(
                        {
                            "session_id": "legacy-session",
                            "__completed_steps": ["start"],
                        }
                    ),
                ),
            )
            connection.commit()
            runner.apply(migrations[65:])

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await tool.invoke(_context(campaign_id), {"input": "probe"})
                await tool.cleanup_assignment(f"{campaign_id}/injection")
            states = {
                state.session_scope_id: state for state in sessions.list_for_campaign(campaign_id)
            }

    assert result.is_error is False
    assert calls == [
        ("abandon", "legacy-session"),
        ("start", None),
        ("message", "scoped-session"),
        ("abandon", "scoped-session"),
    ]
    assert states["legacy:campaign"].state == "cleaned"
    assert states[f"{campaign_id}/injection"].state == "cleaned"


@pytest.mark.asyncio
async def test_reviewed_api_tool_quarantines_incompatible_migrated_legacy_session(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    campaign_id = "campaign/api-incompatible-legacy"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            sessions.create(
                campaign_id,
                "target-refund",
                "sha256:incompatible",
            )
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: (
                        calls.append(request.url.path)
                        or httpx.Response(500, text="must not be called")
                    )
                )
            ) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})
            legacy = sessions.load(campaign_id)

    assert result.is_error is True
    assert result.metadata["failure_code"] == "legacy_session_reconciliation_unavailable"
    assert result.metadata["legacy_session_quarantined"] is True
    assert result.metadata["retryable"] is True
    assert result.metadata["limit_interaction_count"] == 0
    assert result.metadata["target_call_budget_charge"] == 0
    assert calls == []
    assert legacy is not None and legacy.state == "ambiguous"


@pytest.mark.asyncio
async def test_reviewed_api_tool_preserves_plain_text_http_failure_before_selectors(
    tmp_path: Path,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="<html>temporarily unavailable</html>")

    campaign_id = "campaign/api-plain-http-failure"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is True
    assert result.metadata["failure_code"] == "target_delivery_rejected"
    assert result.metadata["limit_interaction_count"] == 0
    assert result.metadata["target_call_budget_charge"] == 1
    exchange = result.metadata["current_target_exchanges"][0]
    assert exchange["id"] == "start"
    assert exchange["status_code"] == 503
    assert exchange["response_summary"]["kind"] == "str"
    assert exchange["response_body_summary"]["kind"] == "str"


@pytest.mark.asyncio
async def test_reviewed_api_tool_preserves_invalid_utf8_http_failure_before_selectors(
    tmp_path: Path,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"\xff\xfe\x00")

    campaign_id = "campaign/api-invalid-utf8-http-failure"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is True
    assert result.metadata["failure_code"] == "target_delivery_rejected"
    assert result.metadata["limit_interaction_count"] == 0
    assert result.metadata["target_call_budget_charge"] == 1
    exchange = result.metadata["current_target_exchanges"][0]
    assert exchange["id"] == "start"
    assert exchange["status_code"] == 502
    assert exchange["response_summary"]["kind"] == "str"
    assert exchange["response_body_summary"]["kind"] == "str"


@pytest.mark.asyncio
async def test_reviewed_api_tool_classifies_non_2xx_before_required_capture(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    adapter["api_steps"][1]["captures"] = {"receipt": "/missing"}  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "capture-session"})
        return httpx.Response(422, json={"error": "request rejected"})

    campaign_id = "campaign/api-non-2xx-missing-capture"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is True
    assert result.metadata["failure_code"] == "target_delivery_rejected"
    exchange = result.metadata["current_target_exchanges"][-1]
    assert exchange["status_code"] == 422
    assert exchange["response_body_summary"]["fields"] == ["error"]
    assert exchange["outcome"] == "target_delivery_rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    (
        httpx.Response(500, json={"error": "cleanup failed"}),
        httpx.Response(500, text="cleanup failed"),
    ),
)
async def test_reviewed_api_tool_does_not_release_claim_after_cleanup_http_failure(
    tmp_path: Path,
    response: httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "cleanup-session"})
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json={"reply": "ok"})
        return response

    campaign_id = "campaign/api-cleanup-http-failure"
    assignment_id = f"{campaign_id}/injection"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: _adapter(),
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await tool.invoke(_context(campaign_id), {"input": "probe"})
                with pytest.raises(RuntimeError, match="cleanup returned HTTP 500"):
                    await tool.cleanup_assignment(assignment_id)
                retry = await tool.invoke(_context(campaign_id), {"input": "retry"})
            state = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=assignment_id,
            )

    assert result.is_error is False
    assert retry.is_error is True
    assert retry.metadata["failure_code"] == "target_cleanup_unresolved"
    assert retry.metadata["retryable"] is True
    assert retry.metadata["target_call_budget_charge"] == 0
    assert state is not None
    assert state.state == "ambiguous"
    assert state.pending_step is None
    assert state.remote_session_id == "cleanup-session"
    assert state.variables["__cleanup_exchange"]["status_code"] == 500
    assert state.variables["__cleanup_exchange"]["outcome"] == "cleanup_delivery_rejected"


@pytest.mark.asyncio
async def test_reviewed_api_tool_quarantines_cleanup_after_adapter_rotation(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        step = request.url.path.rsplit("/", 1)[-1]
        calls.append(step)
        if step == "start":
            return httpx.Response(200, json={"sessionId": "rotation-owned-session"})
        if step == "message":
            return httpx.Response(200, json={"reply": "ok"})
        return httpx.Response(200, json={"closed": True})

    original = _adapter()
    current = {"adapter": original}
    campaign_id = "campaign/api-cleanup-adapter-rotation"
    assignment_id = f"{campaign_id}/injection"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: current["adapter"],
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await tool.invoke(_context(campaign_id), {"input": "probe"})
                rotated = json.loads(json.dumps(original))
                rotated["api_steps"] = rotated["api_steps"][:-1]
                current["adapter"] = rotated
                with pytest.raises(
                    RuntimeError,
                    match="created with a different reviewed adapter",
                ):
                    await tool.cleanup_assignment(assignment_id)
            state = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=assignment_id,
            )

    assert result.is_error is False
    assert calls == ["start", "message"]
    assert state is not None
    assert state.state == "ambiguous"
    assert state.remote_session_id == "rotation-owned-session"
    assert state.cleanup_disposition is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cleanup_body", "root_response_selector"),
    (
        ({"closed": False}, False),
        ({"status": "accepted"}, True),
    ),
)
async def test_reviewed_api_tool_requires_reviewed_cleanup_response_witness(
    tmp_path: Path,
    cleanup_body: object,
    root_response_selector: bool,
) -> None:
    adapter = _adapter()
    if root_response_selector:
        adapter["api_steps"][-1]["response_selector"] = ""  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "cleanup-witness-session"})
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json={"reply": "ok"})
        return httpx.Response(200, json=cleanup_body)

    campaign_id = "campaign/api-cleanup-witness-missing"
    assignment_id = f"{campaign_id}/injection"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await tool.invoke(_context(campaign_id), {"input": "probe"})
                await tool.cleanup_assignment(assignment_id)
            state = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=assignment_id,
            )

    assert result.is_error is False
    assert state is not None
    assert state.state == "cleaned"
    assert state.cleanup_disposition == "cleanup_completed"
    cleanup_exchange = state.variables["__cleanup_exchange"]
    assert cleanup_exchange["outcome"] == "acknowledged"
    assert cleanup_exchange["evaluation_uncertain"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("acknowledgement", "cleanup_body"),
    (
        (
            {"pointer": "/cleaned", "match_values": [True], "status_codes": [200]},
            {"cleaned": True},
        ),
        (
            {
                "pointer": "/lifecycle/state",
                "match_values": ["closed"],
                "status_codes": [200],
            },
            {"lifecycle": {"state": "closed"}, "message": "session retired"},
        ),
    ),
)
async def test_reviewed_api_tool_accepts_exact_typed_cleanup_acknowledgement(
    tmp_path: Path,
    acknowledgement: dict[str, object],
    cleanup_body: object,
) -> None:
    adapter = _adapter()
    adapter["api_steps"][-1]["response_selector"] = ""  # type: ignore[index]
    adapter["api_steps"][-1]["cleanup_acknowledgement"] = acknowledgement  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "typed-cleanup-session"})
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json={"reply": "ok"})
        return httpx.Response(200, json=cleanup_body)

    campaign_id = "campaign/api-typed-cleanup-witness"
    assignment_id = f"{campaign_id}/injection"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await tool.invoke(_context(campaign_id), {"input": "probe"})
                await tool.cleanup_assignment(assignment_id)
            state = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=assignment_id,
            )

    assert result.is_error is False
    assert state is not None
    assert state.state == "cleaned"
    assert state.cleanup_disposition == "cleanup_completed"
    assert state.variables["__cleanup_exchange"]["outcome"] == "acknowledged"


@pytest.mark.asyncio
async def test_invalid_cleanup_advisory_cannot_block_compiled_cleanup(tmp_path: Path) -> None:
    adapter = _adapter()
    adapter["api_steps"][-1]["cleanup_acknowledgement"] = "unsigned-invalid-hint"  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"sessionId": "cleanup-advisory-session"})
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json={"reply": "ok"})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-invalid-cleanup-advisory"
    assignment_id = f"{campaign_id}/injection"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await tool.invoke(_context(campaign_id), {"input": "probe"})
                await tool.cleanup_assignment(assignment_id)
            state = sessions.load(
                campaign_id,
                target_id="target-refund",
                session_scope_id=assignment_id,
            )

    assert result.is_error is False
    assert state is not None and state.state == "cleaned"
    cleanup_exchange = state.variables["__cleanup_exchange"]
    assert cleanup_exchange["outcome"] == "acknowledged"
    assert cleanup_exchange["evaluation_uncertain"] is True


def _adapter_with_session_expired_signal() -> dict[str, object]:
    adapter = _adapter()
    adapter["session_creation_transition"] = {
        "source": "inferred_absent_then_active_transition",
        "precondition": {
            "method": "GET",
            "url": "https://target.test/api/scanner/active",
            "status_codes": [404],
            "pointer": "/error",
            "match_values": ["no_active_session"],
        },
        "postcondition": {
            "method": "GET",
            "url": "https://target.test/api/scanner/active",
            "status_codes": [200],
            "pointer": "/sessionId",
            "match_values": ["reviewed-recon-session"],
        },
        "remote_session_id": "reviewed-recon-session",
        "evidence_refs": ["setup-request", "setup-response"],
    }
    return adapter


@pytest.mark.asyncio
async def test_reviewed_api_tool_restarts_declared_session_once_on_no_active_session(
    tmp_path: Path,
) -> None:
    adapter = _adapter_with_session_expired_signal()
    calls: list[tuple[str, object]] = []
    live_session: dict[str, str | None] = {"id": None}
    starts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        step = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        calls.append((step, body.get("sessionId")))
        if step == "start":
            starts += 1
            live_session["id"] = f"session-{starts}"
            return httpx.Response(200, json={"sessionId": live_session["id"]})
        if step == "message":
            if body.get("sessionId") != live_session["id"]:
                return httpx.Response(404, json={"error": "no_active_session"})
            return httpx.Response(200, json={"reply": "after restart"})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-session-restart-fallback"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                first = await tool.invoke(_context(campaign_id), {"input": "first probe"})
                # The server-side game session expires between attempts.
                live_session["id"] = None
                second = await tool.invoke(_context(campaign_id), {"input": "second probe"})

    assert first.is_error is False
    assert second.is_error is False
    assert json.loads(second.content) == "after restart"
    assert second.metadata["target_session_restarted"] is True
    assert calls == [
        ("start", None),
        ("message", "session-1"),
        ("message", "session-1"),
        ("start", None),
        ("message", "session-2"),
    ]
    restart_exchanges = [
        exchange
        for exchange in second.metadata["current_target_exchanges"]
        if exchange.get("infrastructure_receipt") == "session_restart"
    ]
    assert [exchange["id"] for exchange in restart_exchanges] == ["start"]


@pytest.mark.asyncio
async def test_reviewed_api_tool_start_fallback_is_bounded_to_one_retry(
    tmp_path: Path,
) -> None:
    adapter = _adapter_with_session_expired_signal()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        step = request.url.path.rsplit("/", 1)[-1]
        calls.append(step)
        if step == "start":
            return httpx.Response(200, json={"sessionId": f"session-{calls.count('start')}"})
        if step == "message":
            return httpx.Response(404, json={"error": "no_active_session"})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-session-restart-bounded"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                await tool.invoke(_context(campaign_id), {"input": "first probe"})
                # Expire the durable session, then keep rejecting even after restart.
                calls.clear()
                result = await tool.invoke(_context(campaign_id), {"input": "second probe"})

    assert result.is_error is True
    assert result.metadata["failure_code"] == "target_delivery_rejected"
    # One start fallback plus exactly one retried interaction, never a loop.
    assert calls == ["message", "start", "message"]
    restart_exchanges = [
        exchange
        for exchange in result.metadata["current_target_exchanges"]
        if exchange.get("infrastructure_receipt") == "session_restart"
    ]
    assert [exchange["id"] for exchange in restart_exchanges] == ["start"]


@pytest.mark.asyncio
async def test_reviewed_api_tool_without_declared_signal_keeps_delivery_rejected(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        step = request.url.path.rsplit("/", 1)[-1]
        calls.append(step)
        if step == "start":
            return httpx.Response(200, json={"sessionId": "session-1"})
        if step == "message":
            return httpx.Response(404, json={"error": "no_active_session"})
        return httpx.Response(200, json={"closed": True})

    campaign_id = "campaign/api-session-no-declared-fallback"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                result = await tool.invoke(_context(campaign_id), {"input": "probe"})

    assert result.is_error is True
    assert result.metadata["failure_code"] == "target_delivery_rejected"
    # No session_creation_transition declared: the setup step is never replayed.
    assert calls == ["start", "message"]


@pytest.mark.asyncio
async def test_generated_request_values_survive_retry_rejection_and_tool_restart(tmp_path: Path):
    from uuid import UUID

    adapter = _adapter()
    step = adapter["api_steps"][1]
    step["request_template"]["envelope"] = {"opaque": "captured-example"}
    step["request_bindings"] = {"/envelope/opaque": "uuid4"}
    step["idempotency_key"] = "reviewed-retry-safe"
    step["replay_after_ambiguous_result"] = True
    campaign_id = "campaign/generated-request-values"
    seen = []
    delivered = {}

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)

            def handler(request):
                if request.url.path.endswith("/start"):
                    return httpx.Response(200, json={"sessionId": "stable-session"})
                body = json.loads(request.content)
                pending = sessions.load(
                    campaign_id,
                    target_id="target-refund",
                    session_scope_id=f"{campaign_id}/injection",
                ).pending_step
                # The exact request, including the nonce, was committed before dispatch.
                assert pending["request"] == body
                seen.append(body)
                nonce = body["envelope"]["opaque"]
                if nonce in delivered and delivered[nonce] != body["userMessage"]:
                    return httpx.Response(409, json={"error": "duplicate ID, different payload"})
                delivered[nonce] = body["userMessage"]
                if len(seen) == 1:
                    raise httpx.ReadTimeout("response lost after delivery", request=request)
                if body["userMessage"] == "rejected":
                    return httpx.Response(409, json={"error": "known rejection"})
                return httpx.Response(200, json={"reply": "received"})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

                def make_tool():
                    return ReviewedApiTargetTool(
                        sessions,
                        lambda _: adapter,
                        {"target-token": "test"},
                        client=client,
                    )

                first = await make_tool().invoke(
                    _sealed_context(campaign_id, adapter), {"input": "first"}
                )
                rejected = await make_tool().invoke(
                    _sealed_context(campaign_id, adapter), {"input": "rejected"}
                )
                state = sessions.load(
                    campaign_id,
                    target_id="target-refund",
                    session_scope_id=f"{campaign_id}/injection",
                )
                assert state.state == "active" and state.pending_step is None
                third = await make_tool().invoke(
                    _sealed_context(campaign_id, adapter), {"input": "third"}
                )

    assert not first.is_error and not third.is_error
    assert rejected.is_error and rejected.metadata["target_profile_id"] == "target-refund"
    assert rejected.metadata["infrastructure_failure_category"] == "delivery_failure"
    assert len(seen) == 4
    assert seen[0] == seen[1]  # Same logical message, same ID on transport retry.
    assert len({item["envelope"]["opaque"] for item in seen}) == 3
    assert all(UUID(item["envelope"]["opaque"]).version == 4 for item in seen)
    assert all(item["sessionId"] == "stable-session" for item in seen)


@pytest.mark.asyncio
async def test_api_error_retains_target_identity_when_transport_is_unavailable(tmp_path: Path):
    adapter = _adapter()
    adapter.pop("credential_handle")
    adapter["api_transport"] = "browser_context"
    campaign_id = "campaign/error-target-identity"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            result = await ReviewedApiTargetTool(
                _prepare(connection, campaign_id),
                lambda _: adapter,
                {},
            ).invoke(_sealed_context(campaign_id, adapter), {"input": "probe"})
    assert result.is_error
    assert result.metadata["target_profile_id"] == "target-refund"
    assert result.metadata["compiled_adapter_digest"] == compiled_target_adapter_digest(
        compile_target_adapter(adapter)
    )


@pytest.mark.asyncio
async def test_reviewed_request_constraint_rejects_rendered_input_before_target_call(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    adapter["request_constraints"] = [
        {
            "step_id": "message",
            "evaluation_root": "request_body",
            "path": "/userMessage",
            "kind": "string_length",
            "maximum": 5,
            "unit": "unicode_characters",
            "source": "server_validation_response",
            "evidence_refs": ["exchange/limit"],
            "evidence_bindings": [{"evidence_ref": "exchange/limit", "observed_maximum": 5}],
        }
    ]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500, json={"error": "must not be called"})

    campaign_id = "campaign/request-constraint"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_sealed_context(campaign_id, adapter), {"input": "too long"})

    assert result.is_error is True
    assert calls == []
    assert result.metadata["attempt_disposition"] == "protocol_invalid"
    assert result.metadata["retryable"] is True
    assert result.metadata["acknowledged_capability_evidence"] is False
    assert result.metadata["target_effect_attempted"] is False
    assert result.metadata["target_call_budget_charge"] == 0
    observation = result.metadata["request_constraint_observations"][0]
    assert observation["step_id"] == "message"
    assert observation["observed"] == 8
    assert observation["satisfied"] is False


@pytest.mark.asyncio
async def test_reviewed_state_contract_emits_attributable_authoritative_transition(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    adapter["state_capabilities"] = {
        "identity": {
            "baseline_selector": {
                "step_id": "start",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            },
            "result_selector": {
                "step_id": "message",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            },
            "evidence_refs": ["exchange/run-id"],
        },
        "observations": [
            {
                "name": "score",
                "role": "score",
                "baseline_selector": {
                    "step_id": "start",
                    "evaluation_root": "response_body",
                    "pointer": "/run/score",
                },
                "result_selector": {
                    "step_id": "message",
                    "evaluation_root": "response_body",
                    "pointer": "/run/score",
                },
                "preferred_direction": "increase",
                "value_type": "integer",
                "minimum_value": 0,
                "monotonicity": "nondecreasing",
                "trust_class": "host_observed",
            }
        ],
        "freshness_rules": [
            {
                "selector": {
                    "step_id": "start",
                    "evaluation_root": "response_body",
                    "pointer": "/run/fresh",
                },
                "match_values": [True],
            }
        ],
        "affordances": [
            {
                "kind": "provision_fresh_state",
                "step_id": "start",
                "evaluation_root": "response_body",
            }
        ],
        "maximum_parallel_states": 4,
    }

    interaction_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal interaction_calls
        if request.url.path.endswith("/start"):
            return httpx.Response(
                200,
                json={
                    "sessionId": "logical-session",
                    "run": {"id": "physical-run", "score": 10, "fresh": True},
                },
            )
        interaction_calls += 1
        return httpx.Response(
            200,
            json={
                "reply": "advanced",
                "run": {"id": "physical-run", "score": 25},
            },
        )

    campaign_id = "campaign/state-transition"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    checkpoint_vault=CredentialCheckpointVault(
                        ConfigPaths(tmp_path / "state-transition-credentials")
                    ),
                    client=client,
                )
                result = await tool.invoke(
                    _sealed_context(campaign_id, adapter), {"input": "probe"}
                )
                aliased_result = await tool.invoke(
                    _sealed_context(campaign_id, adapter).model_copy(
                        update={
                            "agent_id": f"{campaign_id}/candidate-2",
                            "call_id": "call-2",
                        }
                    ),
                    {"input": "second probe"},
                )

    assert result.is_error is False
    assert result.metadata["attempt_disposition"] == "valid"
    assert result.metadata["state_identity_match"] is True
    assert result.metadata["physical_state_identity"]["kind"] == "keyed_digest"
    transition = result.metadata["target_state_transition"]
    assert transition["authority"] == "target"
    assert transition["outcome"] == "progress"
    assert transition["observations"] == [
        {
            "name": "score",
            "role": "score",
            "status": "progress",
            "before_digest": transition["observations"][0]["before_digest"],
            "after_digest": transition["observations"][0]["after_digest"],
            "before_value": 10,
            "after_value": 25,
            "preferred_direction": "increase",
            "monotonicity": "nondecreasing",
            "minimum_value": 0,
            "trust_class": "host_observed",
            "baseline_binding_digest": transition["observations"][0]["baseline_binding_digest"],
            "result_binding_digest": transition["observations"][0]["result_binding_digest"],
            "validation_errors": [],
        }
    ]
    assert aliased_result.is_error is True
    assert aliased_result.metadata["attempt_disposition"] == "contaminated"
    assert aliased_result.metadata["state_identity_match"] is False
    assert aliased_result.metadata["acknowledged_capability_evidence"] is False
    assert aliased_result.metadata["target_state_transition"]["outcome"] == "unclear"
    assert aliased_result.metadata["limit_interaction_count"] == 0
    assert interaction_calls == 1


def test_target_parallel_capacity_enforces_caps_and_reset_is_not_concurrent_isolation() -> None:
    capped = _adapter()
    capped["state_capabilities"] = {"maximum_parallel_states": 3}
    assert (
        _target_parallel_capacity(
            ReviewedApiTargetTool(  # type: ignore[arg-type]
                SimpleNamespace(), lambda _campaign_id: capped, {}
            )._adapter("campaign", "target-refund")
        )
        == 1
    )  # noqa: SLF001

    independently_capped = _adapter()
    independently_capped["state_capabilities"] = {
        "identity": {
            "baseline_selector": {
                "step_id": "start",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            },
            "result_selector": {
                "step_id": "message",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            },
        },
        "freshness_rules": [
            {
                "selector": {
                    "step_id": "start",
                    "evaluation_root": "response_body",
                    "pointer": "/run/fresh",
                },
                "match_values": [True],
            }
        ],
        "affordances": [
            {
                "kind": "provision_fresh_state",
                "step_id": "start",
                "evaluation_root": "response_body",
            }
        ],
        "maximum_parallel_states": 3,
    }
    independent_adapter = ReviewedApiTargetTool(
        SimpleNamespace(), lambda _campaign_id: independently_capped, {}
    )._adapter("campaign", "target-refund")  # noqa: SLF001
    assert _target_parallel_capacity(independent_adapter) == 3

    reset_only = _adapter()
    reset_only["state_capabilities"] = {
        "affordances": [
            {
                "kind": "reset_state",
                "step_id": "start",
                "evaluation_root": "response_body",
            }
        ],
    }
    reset_adapter = ReviewedApiTargetTool(
        SimpleNamespace(), lambda _campaign_id: reset_only, {}
    )._adapter("campaign", "target-refund")  # noqa: SLF001
    assert _target_parallel_capacity(reset_adapter) == 1

    rotating_identity = _adapter()
    rotating_identity["state_capabilities"] = {
        "identity": {
            "baseline_selector": {
                "step_id": "start",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            },
            "result_selector": {
                "step_id": "message",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            },
        },
        "freshness_rules": [
            {
                "selector": {
                    "step_id": "start",
                    "evaluation_root": "response_body",
                    "pointer": "/run/fresh",
                },
                "match_values": [True],
            }
        ],
        "affordances": [
            {
                "kind": "provision_fresh_state",
                "step_id": "start",
                "evaluation_root": "response_body",
            }
        ],
    }
    rotating_adapter = ReviewedApiTargetTool(
        SimpleNamespace(), lambda _campaign_id: rotating_identity, {}
    )._adapter("campaign", "target-refund")  # noqa: SLF001
    assert _target_parallel_capacity(rotating_adapter) == 1

    legacy_adapter = ReviewedApiTargetTool(
        SimpleNamespace(), lambda _campaign_id: _adapter(), {}
    )._adapter("campaign", "target-refund")  # noqa: SLF001
    assert _target_parallel_capacity(legacy_adapter) == 1


@pytest.mark.asyncio
async def test_failed_isolation_proof_never_falls_back_to_shared_delivery(tmp_path: Path) -> None:
    adapter = _adapter()
    adapter["state_capabilities"] = {
        "identity": {
            "baseline_selector": {
                "step_id": "start",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            },
            "result_selector": {
                "step_id": "message",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            },
        },
        "freshness_rules": [
            {
                "selector": {
                    "step_id": "start",
                    "evaluation_root": "response_body",
                    "pointer": "/run/fresh",
                },
                "match_values": [True],
            }
        ],
        "affordances": [
            {
                "kind": "provision_fresh_state",
                "step_id": "start",
                "evaluation_root": "response_body",
            }
        ],
        "maximum_parallel_states": 1,
    }
    interaction_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal interaction_calls
        if request.url.path.endswith("/start"):
            return httpx.Response(
                200,
                json={
                    "sessionId": "shared-session",
                    "run": {"id": "shared-state", "fresh": False},
                },
            )
        interaction_calls += 1
        return httpx.Response(
            200,
            json={"reply": "must not be delivered", "run": {"id": "shared-state"}},
        )

    campaign_id = "campaign/no-isolated-fallback"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                ).invoke(_sealed_context(campaign_id, adapter), {"input": "candidate"})

    assert result.is_error is True
    assert result.metadata["attempt_disposition"] == "contaminated"
    assert result.metadata["retryable"] is True
    assert result.metadata["limit_interaction_count"] == 0
    assert interaction_calls == 0


@pytest.mark.asyncio
async def test_unknown_state_is_globally_serialized_across_campaign_target_aliases(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    setup_calls = 0
    interaction_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal setup_calls, interaction_calls
        if request.url.path.endswith("/start"):
            setup_calls += 1
            return httpx.Response(200, json={"sessionId": f"session-{setup_calls}"})
        interaction_calls += 1
        first_entered.set()
        await release_first.wait()
        return httpx.Response(200, json={"reply": "observed"})

    first_campaign = "campaign/global-state-scope-a"
    second_campaign = "campaign/global-state-scope-b"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-global-scope")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, first_campaign)
            _prepare(connection, second_campaign)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    client=client,
                )
                first_context = _sealed_context(first_campaign, adapter).model_copy(
                    update={"target_id": "target-alias-a"}
                )
                second_context = _sealed_context(second_campaign, adapter).model_copy(
                    update={"target_id": "target-alias-b"}
                )
                first_task = asyncio.create_task(tool.invoke(first_context, {"input": "first"}))
                await asyncio.wait_for(first_entered.wait(), timeout=5)
                second = await tool.invoke(second_context, {"input": "second"})
                release_first.set()
                first = await asyncio.wait_for(first_task, timeout=5)
            scope_rows = connection.execute(
                "SELECT status FROM merlin_storage.state_scopes ORDER BY campaign_id"
            ).fetchall()

    assert first.is_error is False
    assert second.is_error is True
    assert second.metadata["attempt_disposition"] == "infrastructure_failed"
    assert second.metadata["acknowledged_capability_evidence"] is False
    assert second.metadata["limit_interaction_count"] == 0
    assert setup_calls == 1
    assert interaction_calls == 1
    assert [str(row["status"]) for row in scope_rows] == ["settled", "paused"]


@pytest.mark.asyncio
async def test_reviewed_physical_identity_is_globally_leased_across_target_aliases(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    adapter["state_capabilities"] = {
        "identity": {
            "baseline_selector": {
                "step_id": "start",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            },
            "result_selector": {
                "step_id": "message",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            },
        },
        "freshness_rules": [
            {
                "selector": {
                    "step_id": "start",
                    "evaluation_root": "response_body",
                    "pointer": "/run/fresh",
                },
                "match_values": [True],
            }
        ],
        "affordances": [
            {
                "kind": "provision_fresh_state",
                "step_id": "start",
                "evaluation_root": "response_body",
            }
        ],
        "maximum_parallel_states": 2,
    }
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    setup_calls = 0
    interaction_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal setup_calls, interaction_calls
        if request.url.path.endswith("/start"):
            setup_calls += 1
            return httpx.Response(
                200,
                json={
                    "sessionId": f"logical-{setup_calls}",
                    "run": {"id": "aliased-physical-state", "fresh": True},
                },
            )
        interaction_calls += 1
        first_entered.set()
        await release_first.wait()
        return httpx.Response(
            200,
            json={"reply": "observed", "run": {"id": "aliased-physical-state"}},
        )

    first_campaign = "campaign/global-physical-state-a"
    second_campaign = "campaign/global-physical-state-b"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres-global-physical")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, first_campaign)
            _prepare(connection, second_campaign)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    checkpoint_vault=CredentialCheckpointVault(
                        ConfigPaths(tmp_path / "global-physical-credentials")
                    ),
                    client=client,
                )
                first_context = _sealed_context(first_campaign, adapter).model_copy(
                    update={"target_id": "target-alias-a"}
                )
                second_context = _sealed_context(second_campaign, adapter).model_copy(
                    update={"target_id": "target-alias-b"}
                )
                first_task = asyncio.create_task(tool.invoke(first_context, {"input": "first"}))
                await asyncio.wait_for(first_entered.wait(), timeout=5)
                second = await tool.invoke(second_context, {"input": "second"})
                release_first.set()
                first = await asyncio.wait_for(first_task, timeout=5)

    assert first.is_error is False
    assert second.is_error is True
    assert second.metadata["acknowledged_capability_evidence"] is False
    assert second.metadata["limit_interaction_count"] == 0
    assert setup_calls == 2
    assert interaction_calls == 1


def test_target_state_transition_keeps_conflicting_or_missing_identity_unclear() -> None:
    conflicting = _target_state_transition(
        [
            {
                "name": "win",
                "role": "terminal_status",
                "phase": "baseline",
                "selected_digest": "before-win",
                "selected_value": "active",
                "success_values": ["won"],
            },
            {
                "name": "win",
                "role": "terminal_status",
                "phase": "result",
                "selected_digest": "after-win",
                "selected_value": "won",
                "success_values": ["won"],
            },
            {
                "name": "loss",
                "role": "terminal_status",
                "phase": "baseline",
                "selected_digest": "before-loss",
                "selected_value": "active",
                "failure_values": ["lost"],
            },
            {
                "name": "loss",
                "role": "terminal_status",
                "phase": "result",
                "selected_digest": "after-loss",
                "selected_value": "lost",
                "failure_values": ["lost"],
            },
        ]
    )
    assert conflicting["outcome"] == "unclear"

    no_identity = _target_state_transition(
        [
            {
                "name": "score",
                "role": "score",
                "phase": "baseline",
                "selected_digest": "before",
                "selected_value": 1,
                "preferred_direction": "increase",
            },
            {
                "name": "score",
                "role": "score",
                "phase": "result",
                "selected_digest": "after",
                "selected_value": 2,
                "preferred_direction": "increase",
            },
        ]
    )
    assert no_identity["identity_match"] is None
    assert no_identity["outcome"] == "unclear"

    missing_identity = _target_state_transition(
        [
            {
                "name": "physical_state_identity",
                "role": "state_identity",
                "phase": "baseline",
                "selected_digest": None,
            },
            {
                "name": "score",
                "role": "score",
                "phase": "baseline",
                "selected_digest": "before",
                "selected_value": 1,
                "preferred_direction": "increase",
            },
            {
                "name": "score",
                "role": "score",
                "phase": "result",
                "selected_digest": "after",
                "selected_value": 2,
                "preferred_direction": "increase",
            },
        ]
    )
    assert missing_identity["identity_match"] is None
    assert missing_identity["outcome"] == "unclear"


def test_state_metadata_without_physical_identity_is_never_authoritative() -> None:
    metadata: dict[str, object] = {
        "current_target_exchanges": [
            {
                "state_observations": [
                    {
                        "name": "score",
                        "role": "score",
                        "phase": "baseline",
                        "selected_digest": "before",
                        "selected_value": 1,
                        "preferred_direction": "increase",
                    },
                    {
                        "name": "score",
                        "role": "score",
                        "phase": "result",
                        "selected_digest": "after",
                        "selected_value": 2,
                        "preferred_direction": "increase",
                    },
                ]
            }
        ]
    }

    _augment_target_state_metadata(metadata)

    assert metadata["state_identity_match"] is None
    assert metadata["evaluation_uncertain"] is True
    transition = metadata["target_state_transition"]
    assert isinstance(transition, dict)
    assert transition["outcome"] == "unclear"


def test_reviewed_api_state_enumeration_reaches_target_metadata() -> None:
    declaration = StateEnumerationDeclaration(
        schema_version="merlin.state-enumeration-declaration.v1",
        name="language",
        selector=StateSelector(
            schema_version="merlin.state-selector.v1",
            step_id="message",
            evaluation_root="response_body",
            pointer="/available_languages",
            evidence_refs=("evidence:language-response",),
        ),
        shape=StateEnumerationShape.FLAT,
        completeness=StateEnumerationCompleteness.COMPLETE,
        evidence_refs=("evidence:reviewed-adapter",),
    )
    adapter = SimpleNamespace(
        state_capabilities=StateCapabilities(enumerations=(declaration,)),
        digest="sha256:" + "1" * 64,
        compiled_digest="sha256:" + "2" * 64,
        target_id="target-1",
    )

    enumerations = _state_enumerations(
        adapter,
        SimpleNamespace(id="message"),
        {"available_languages": ["en", "ms"]},
        {},
    )
    metadata: dict[str, object] = {
        "current_target_exchanges": [{"state_enumerations": enumerations}]
    }
    _augment_target_state_metadata(metadata)

    assert metadata["state_enumerations"] == enumerations
    assert enumerations[0]["members_by_scope"] == {"*": ["en", "ms"]}
    assert enumerations[0]["complete"] is True
    assert (
        _state_enumerations(
            adapter,
            SimpleNamespace(id="unreviewed-later-step"),
            {"available_languages": ["forged"]},
            {},
        )
        == []
    )


def test_reviewed_api_state_enumeration_rejects_shape_drift() -> None:
    declaration = StateEnumerationDeclaration(
        name="language",
        selector=StateSelector(
            step_id="message",
            evaluation_root="response_body",
            pointer="/available_languages",
            evidence_refs=("evidence:language-response",),
        ),
        shape=StateEnumerationShape.FLAT,
        completeness=StateEnumerationCompleteness.COMPLETE,
    )
    adapter = SimpleNamespace(
        state_capabilities=StateCapabilities(enumerations=(declaration,)),
        digest="sha256:" + "1" * 64,
        compiled_digest="sha256:" + "2" * 64,
        target_id="target-1",
    )

    assert (
        _state_enumerations(
            adapter,
            SimpleNamespace(id="message"),
            {"available_languages": "en"},
            {},
        )
        == []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reviewed_reset", [False, True], ids=["shared-state", "reset"])
async def test_serial_reviewed_reset_allows_learning_eligible_physical_state_reuse(
    tmp_path: Path,
    reviewed_reset: bool,
) -> None:
    adapter = _adapter()
    adapter["state_capabilities"] = {
        "identity": {
            "baseline_selector": {
                "step_id": "start",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            },
            "result_selector": {
                "step_id": "message",
                "evaluation_root": "response_body",
                "pointer": "/run/id",
            },
        },
        "observations": [
            {
                "name": "score",
                "role": "score",
                "baseline_selector": {
                    "step_id": "start",
                    "evaluation_root": "response_body",
                    "pointer": "/run/score",
                },
                "result_selector": {
                    "step_id": "message",
                    "evaluation_root": "response_body",
                    "pointer": "/run/score",
                },
                "preferred_direction": "increase",
                "value_type": "integer",
                "minimum_value": 0,
                "monotonicity": "nondecreasing",
                "trust_class": "host_observed",
            }
        ],
        "freshness_rules": [
            {
                "selector": {
                    "step_id": "start",
                    "evaluation_root": "response_body",
                    "pointer": "/run/restored",
                },
                "match_values": [True],
            }
        ],
        "affordances": [
            {
                "kind": "reset_state",
                "step_id": "start",
                "evaluation_root": "response_body",
            }
        ],
        "maximum_parallel_states": 1,
    }
    state_capabilities = adapter["state_capabilities"]
    assert isinstance(state_capabilities, dict)
    if not reviewed_reset:
        state_capabilities.pop("freshness_rules")
        state_capabilities.pop("affordances")
    interaction_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal interaction_calls
        if request.url.path.endswith("/start"):
            baseline_score = 10 if reviewed_reset else 10 + interaction_calls * 10
            return httpx.Response(
                200,
                json={
                    "sessionId": "logical-session",
                    "run": {
                        "id": "shared-physical-run",
                        "score": baseline_score,
                        "restored": True,
                    },
                },
            )
        interaction_calls += 1
        result_score = 20 if reviewed_reset else 10 + interaction_calls * 10
        return httpx.Response(
            200,
            json={
                "reply": "advanced",
                "run": {"id": "shared-physical-run", "score": result_score},
            },
        )

    campaign_id = f"campaign/serial-reuse-{reviewed_reset}"
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions = _prepare(connection, campaign_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {"target-token": "opaque-secret"},
                    checkpoint_vault=CredentialCheckpointVault(
                        ConfigPaths(tmp_path / "serial-reuse-credentials")
                    ),
                    client=client,
                )
                first = await tool.invoke(_sealed_context(campaign_id, adapter), {"input": "first"})
                second = await tool.invoke(
                    _sealed_context(campaign_id, adapter).model_copy(
                        update={
                            "agent_id": f"{campaign_id}/candidate-2",
                            "call_id": "call-2",
                        }
                    ),
                    {"input": "second"},
                )

    assert first.metadata["attempt_disposition"] == "valid"
    assert second.metadata["attempt_disposition"] == "valid"
    assert second.metadata["target_state_transition"]["outcome"] == "progress"
    assert second.metadata["acknowledged_capability_evidence"] is True
    assert second.metadata["session_freshness"] == "not_observed"
    assert second.metadata["current_target_exchanges"][0]["physical_state_sequential_reuse"] is True
    assert interaction_calls == 2


@pytest.mark.asyncio
async def test_target_execution_renewal_failure_cancels_the_owner() -> None:
    sessions = SimpleNamespace(renew_target_execution=lambda *_args: False)
    tool = ReviewedApiTargetTool(sessions, lambda _campaign_id: None, {})

    async def owner() -> None:
        await tool._renew_target_execution_lease(  # noqa: SLF001
            "claim-id",
            "claimant-id",
            1,
            asyncio.current_task(),
        )
        await asyncio.sleep(0)

    task = asyncio.create_task(owner())
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_state_scope_renewal_failure_cancels_the_owner() -> None:
    sessions = SimpleNamespace(
        renew_target_execution=lambda *_args: True,
        renew_state_scope_lease=lambda *_args: False,
    )
    tool = ReviewedApiTargetTool(sessions, lambda _campaign_id: None, {})
    state_scope_lease = _StateScopeLease(
        claimant_id="claimant-id",
        lease_seconds=1,
        lifecycle=SimpleNamespace(),  # type: ignore[arg-type]
        claim_id="state-scope-claim",
        equivalence_key="conservative-shared-state",
    )

    async def owner() -> None:
        await tool._renew_target_execution_lease(  # noqa: SLF001
            "target-claim",
            "claimant-id",
            1,
            asyncio.current_task(),
            state_scope_lease,
        )
        await asyncio.sleep(0)

    task = asyncio.create_task(owner())
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_objective_checkpoint_capture_and_restore_stay_inside_fenced_tool_path(
    tmp_path: Path,
) -> None:
    identity_vault = CredentialCheckpointVault(ConfigPaths(tmp_path / "identity-credentials"))

    class VaultEntry:
        digest = "a" * 64

        def opaque_reference(self) -> dict[str, str]:
            return {
                "kind": "opaque_reference",
                "namespace": "state",
                "reference_id": "checkpoint-test-entry",
            }

    class Vault:
        secret: str | None = None

        def store_checkpoint(
            self,
            checkpoint_secret: str,
            *,
            campaign_id: str,
            gate_id: str,
        ) -> VaultEntry:
            assert campaign_id == "campaign/objective-checkpoint"
            assert gate_id == "reach-stage-two"
            self.secret = checkpoint_secret
            return VaultEntry()

        def resolve_checkpoint(self, reference_id: str) -> str:
            assert reference_id == "checkpoint-test-entry"
            assert self.secret is not None
            return self.secret

        def digest_identity(self, value: object, *, purpose: str) -> dict[str, str]:
            return identity_vault.digest_identity(value, purpose=purpose)

    campaign_id = "campaign/objective-checkpoint"
    recipe_digest = "sha256:" + "1" * 64
    manifest_digest = "sha256:reviewed"
    vault = Vault()
    async with StatefulTargetFixtureServer(
        state_mode="checkpoint",
        maximum_parallel_states=1,
    ) as target:
        adapter = target.adapter()
        capabilities = adapter["state_capabilities"]
        assert isinstance(capabilities, dict)
        checkpoint_contract = capabilities["checkpoint"]
        assert isinstance(checkpoint_contract, dict)
        checkpoint_contract.update(
            {
                "capture_selector": {
                    "step_id": "interaction",
                    "evaluation_root": "response_body",
                    "pointer": "/leaseId",
                },
                "capture_timing": "post_success_response",
            }
        )
        identity = capabilities["identity"]
        assert isinstance(identity, dict)
        baseline = identity["baseline_selector"]
        assert isinstance(baseline, dict)
        baseline["step_id"] = "start"
        for observation in capabilities["observations"]:
            assert isinstance(observation, dict)
            selector = observation["baseline_selector"]
            assert isinstance(selector, dict)
            selector["step_id"] = "start"
        with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
            with database.connect() as connection:
                MigrationRunner(connection).apply()
                sessions = _prepare(connection, campaign_id)
                checkpoints = CampaignStore(connection)
                tool = ReviewedApiTargetTool(
                    sessions,
                    lambda _campaign_id: adapter,
                    {},
                    checkpoint_store=checkpoints,
                    checkpoint_vault=vault,
                )
                capture_context = _sealed_context(campaign_id, adapter).model_copy(
                    update={
                        "objective_phase_gate_id": "reach-stage-two",
                        "objective_phase_gate_recipe_digest": recipe_digest,
                        "objective_phase_manifest_digest": manifest_digest,
                        "objective_phase_checkpoint_intent": "capture",
                        "objective_phase_gate_contract": {
                            "id": "reach-stage-two",
                            "retain_success": "checkpoint",
                            "success_predicate": {
                                "observation_name": "host_score",
                                "operator": "increases",
                                "expected_values": [],
                            },
                        },
                    }
                )
                captured = await tool.invoke(capture_context, {"input": "advance"})
                receipt = captured.metadata["objective_phase_checkpoint"]
                assert isinstance(receipt, dict)
                reference = receipt["checkpoint_reference"]
                assert isinstance(reference, dict)
                assert (
                    checkpoints.load_objective_gate_checkpoint(campaign_id, "reach-stage-two")
                    is not None
                )

                restored = await tool.invoke(
                    _sealed_context(campaign_id, adapter).model_copy(
                        update={
                            "agent_id": f"{campaign_id}/candidate-after-gate",
                            "call_id": "call-after-gate",
                            "objective_phase_gate_id": "reach-stage-two",
                            "objective_phase_gate_recipe_digest": recipe_digest,
                            "objective_phase_manifest_digest": manifest_digest,
                            "objective_phase_checkpoint_intent": "restore",
                            "objective_phase_checkpoint_reference": reference,
                        }
                    ),
                    {"input": "continue"},
                )

    assert captured.is_error is False
    assert restored.is_error is False
    assert restored.metadata["objective_phase_checkpoint"]["restored"] is True
    assert vault.secret is not None
    assert vault.secret not in json.dumps(captured.model_dump(mode="json"), sort_keys=True)
    assert vault.secret not in json.dumps(restored.model_dump(mode="json"), sort_keys=True)
    restore_exchange = next(
        item for item in restored.metadata["current_target_exchanges"] if item["id"] == "restore"
    )
    assert restore_exchange["request_summary"]["fields"] == ["leaseId"]
    assert restore_exchange["response_body_summary"]["fields"] == [
        "hostState",
        "restored",
        "state",
    ]
