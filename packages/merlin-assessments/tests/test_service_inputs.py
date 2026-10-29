import hashlib
from pathlib import Path

import pytest
from merlin_assessments import AssessmentService
from merlin_contracts import (
    ModelCompleted,
    TargetProfile,
    TextDelta,
    ToolContext,
    ToolEffectState,
)
from merlin_contracts.target_adapter_compiler import (
    compile_target_adapter,
    compiled_target_adapter_digest,
)
from merlin_packs import PackCatalog
from merlin_providers import ProviderConfig, TargetProviderFactory, TargetSessionIdentity
from merlin_storage_postgres import (
    CampaignApiSessionRepository,
    CampaignStore,
    Database,
    DatabaseConfig,
    MigrationRunner,
    PostgresToolEffectStore,
    SessionRepository,
    ToolEffectRepository,
)
from merlin_tools import ToolBroker


class Store:
    def __init__(self, target: TargetProfile) -> None:
        self.target = target

    async def get_target(self, target_id: str) -> TargetProfile | None:
        return self.target if target_id == self.target.id else None

    async def list_targets(self):
        return (self.target,)


class StateScopeStore:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.envelopes: list[dict[str, object]] = []

    def create_state_scope(self, *_args, **_kwargs):
        self.events.append("created")
        self.envelopes.append(dict(_args[2]))
        return {}

    def claim_state_scope_lease(self, *_args, **_kwargs):
        self.events.append("claimed")
        return "claim:test"

    def renew_state_scope_lease(self, *_args, **_kwargs):
        return True

    def release_state_scope_lease(self, *_args, **_kwargs):
        self.events.append("released")
        return True

    def revise_state_scope(self, *_args, **_kwargs):
        self.events.append("revised")
        self.envelopes.append(dict(_args[2]))
        return {}

    def acknowledge_state_scope(self, *_args, **_kwargs):
        self.events.append("acknowledged")
        return {}

    def settle_state_scope(self, *_args, **_kwargs):
        self.events.append("settled")
        self.envelopes.append(dict(_args[2]))
        return {}

    def pause_state_scope(self, *_args, **_kwargs):
        self.events.append("paused")
        self.envelopes.append(dict(_args[2]))
        return {}

    def pause_unclaimed_state_scope(self, *_args, **_kwargs):
        self.events.append("paused_unclaimed")
        return {}


class FailingStateScopeStore(StateScopeStore):
    def create_state_scope(self, *_args, **_kwargs):
        self.events.append("create_failed")
        raise RuntimeError("persistence unavailable")


class TargetProvider:
    def __init__(self, state_scopes: StateScopeStore | None = None) -> None:
        self._state_scopes = state_scopes

    async def stream(self, request):
        if self._state_scopes is not None:
            assert self._state_scopes.events[:2] == ["created", "claimed"]
        yield TextDelta(text=f"target replied to: {request.messages[0].content}")
        yield ModelCompleted()


class IncompleteTargetProvider:
    async def stream(self, _request):
        yield TextDelta(text="partial target reply")


class FailingTargetProvider:
    async def stream(self, _request):
        if False:
            yield None
        raise RuntimeError("transport failed before response")


class TargetFactory:
    def __init__(self, state_scopes: StateScopeStore | None = None) -> None:
        self.profiles = []
        self.credential_handles = []
        self.state_scopes = state_scopes

    def create(self, profile, secret_lookup_key=None):
        self.profiles.append(profile)
        self.credential_handles.append(secret_lookup_key)
        return TargetProvider(self.state_scopes)

    async def preflight(self, profile):
        self.profiles.append(profile)
        return TargetSessionIdentity(profile.id, "target_handle", True, "user-7", 200)


class IncompleteTargetFactory(TargetFactory):
    def create(self, profile, secret_lookup_key=None):
        self.profiles.append(profile)
        self.credential_handles.append(secret_lookup_key)
        return IncompleteTargetProvider()


class FailingTargetFactory(TargetFactory):
    def create(self, profile, secret_lookup_key=None):
        self.profiles.append(profile)
        self.credential_handles.append(secret_lookup_key)
        return FailingTargetProvider()


async def test_resource_risk_campaign_receives_executable_experiment_contracts() -> None:
    target = TargetProfile(
        id="target-resource-risk",
        title="Resource Risk Chat",
        base_url="https://target.example/v1",
        model="target-model",
    )
    catalog = PackCatalog()
    catalog.enable("merlin-injection-taxonomy")
    service = AssessmentService(
        catalog=catalog,
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=TargetProviderFactory({"target_target-resource-risk": "secret"}),
    )
    await service.select_target("target-resource-risk")

    inputs = await service.campaign_inputs(
        "test derailment poison soup input output token amplification recursive expansion "
        "context exhaustion latency denial of wallet truncation recovery"
    )
    resource_techniques = [
        item for item in inputs["techniques"] if "resource-exhaustion" in item["tags"]
    ]

    assert resource_techniques
    for technique in resource_techniques:
        assert "Execute this bounded experiment contract exactly" in technique["objective"]
        assert "control_procedure" in technique["objective"]
        assert "attack_procedure" in technique["objective"]
        assert "confirm_when" in technique["objective"]
        assert technique["comparison_supported"] is True
        assert technique["control_technique_id"].endswith(
            "/cat/multi-turn/benign-multiturn-clarify"
        )


async def test_agent_inputs_bind_selected_target_and_enabled_pack_cases() -> None:
    target = TargetProfile(
        id="target-local",
        title="Local",
        base_url="https://target.example/v1",
        model="target-model",
    )
    catalog = PackCatalog()
    catalog.enable("merlin-injection-taxonomy")
    service = AssessmentService(
        catalog=catalog,
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=TargetProviderFactory({"target_target-local": "secret"}),
    )
    await service.select_target("target-local")

    inputs = await service.agent_assignment_inputs()

    assert inputs["target_profile_id"] == "target-local"
    assert inputs["target_model"] == "target-model"
    enabled_pack = catalog.load("merlin-injection-taxonomy")
    assert {item["id"] for item in inputs["techniques"]} == {
        f"{enabled_pack.id}/{case.id}"
        for case in enabled_pack.cases
        if case.execution_role != "control"
    }
    technique = next(
        item
        for item in inputs["techniques"]
        if item["id"] == "merlin-injection-taxonomy/cat/agent-to-agent/agent-authority-relay"
    )
    assert technique["id"].startswith("merlin-injection-taxonomy/cat/")
    assert "taxonomy cell" in technique["objective"]
    assert "authority-impersonation" in technique["tags"]
    assert technique["guidance_ids"] == (
        "pack:merlin-injection-taxonomy",
        "case:merlin-injection-taxonomy/cat/agent-to-agent/agent-authority-relay",
    )
    assert technique["control_guidance_ids"] == (f"case:{technique['control_technique_id']}",)
    assert technique["comparison_supported"] is True
    assert technique["comparison_block_reason"] is None
    assert technique["evaluation_uncertainties"] == (
        {
            "kind": "evaluation_uncertain",
            "source": "catalogue.comparison",
            "reason": "control oracle differs",
            "confidence": 0.0,
            "affects": ["ranking", "evaluation"],
        },
    )


async def test_select_target_does_not_require_an_execution_credential() -> None:
    target = TargetProfile(
        id="web-local",
        title="Local browser target",
        base_url="http://localhost:3100/",
        model="web-chat",
    )
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=TargetProviderFactory({}),
    )

    selected = await service.select_target(target.id)

    assert selected == target.model_dump(mode="json") | {"selected": True}
    assert service.selected_target_id == target.id
    with pytest.raises(ValueError, match="credential 'target_web-local' is not configured"):
        await service.preflight_target()


async def test_resolve_configured_target_does_not_require_an_execution_credential() -> None:
    target = TargetProfile(
        id="web-local",
        title="Local browser target",
        base_url="http://localhost:3100/",
        model="web-chat",
    )
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=TargetProviderFactory({}),
    )

    resolved = await service.resolve_target("continue the assessment for web-local")

    assert resolved == target.model_dump(mode="json") | {"selected": True}
    assert service.selected_target_id == target.id


async def test_campaign_inputs_keep_all_comparison_differences_advisory() -> None:
    target = TargetProfile(
        id="target-advisory-comparison",
        title="Advisory comparison",
        base_url="https://target.example/v1",
        model="target-model",
    )
    catalog = PackCatalog()
    service = AssessmentService(
        catalog=catalog,
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=TargetFactory(),  # type: ignore[arg-type]
    )
    await service.select_target(target.id)

    inputs = await service.campaign_inputs("Assess target-advisory-comparison")
    technique = next(
        item
        for item in inputs["techniques"]
        if item["id"].endswith("/cat/agent-to-agent/agent-goal-handoff")
    )

    assert technique["comparison_supported"] is True
    assert technique["comparison_block_reason"] is None
    assert technique["evaluation_uncertainties"] == (
        {
            "kind": "evaluation_uncertain",
            "source": "catalogue.comparison",
            "reason": "control turn and oracle differ",
            "confidence": 0.0,
            "affects": ["ranking", "evaluation"],
        },
    )
    assert technique["oracle_digest"] != technique["control_oracle_digest"]


async def test_campaign_inputs_admit_unpaired_case_as_direct_observation(tmp_path) -> None:
    pack = tmp_path / "unpaired"
    (pack / "cases").mkdir(parents=True)
    (pack / "PACK.md").write_text(
        """+++
id = "unpaired"
version = "1.0"
title = "Unpaired"
target_kind = "ai-chat"
cases = ["cases/direct.md"]
+++
Exercise a direct observation.
""",
        encoding="utf-8",
    )
    (pack / "cases" / "direct.md").write_text(
        """+++
id = "direct"
title = "Direct"
+++
Send one authorized target request and retain its response.
""",
        encoding="utf-8",
    )
    target = TargetProfile(
        id="target-unpaired",
        title="Unpaired target",
        base_url="https://target.example/v1",
        model="target-model",
    )
    service = AssessmentService(
        catalog=PackCatalog([tmp_path], include_builtin=False),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=TargetFactory(),  # type: ignore[arg-type]
    )
    await service.select_target(target.id)

    inputs = await service.campaign_inputs("Assess target-unpaired")

    technique = inputs["techniques"][0]
    assert technique["id"] == "unpaired/direct"
    assert technique["execution_role"] == "direct"
    assert technique["control_technique_id"] is None
    assert technique["comparison_block_reason"] is None
    assert technique["evaluation_uncertainties"][0]["reason"] == (
        "no structurally valid benign control was supplied"
    )


async def test_campaign_inputs_preserve_reviewed_pack_relationships(tmp_path) -> None:
    pack = tmp_path / "related"
    (pack / "cases").mkdir(parents=True)
    (pack / "PACK.md").write_text(
        """+++
id = "related"
version = "1.0"
title = "Related"
target_kind = "ai-chat"
        cases = ["cases/control.md", "cases/base.md", "cases/child.md"]
+++
Review explicit relationships.
""",
        encoding="utf-8",
    )
    (pack / "cases" / "control.md").write_text(
        '+++\nid = "control"\ntitle = "Control"\n'
        'execution_role = "control"\noracle_id = "reviewed-oracle"\n+++\nBenign control.',
        encoding="utf-8",
    )
    (pack / "cases" / "base.md").write_text(
        '+++\nid = "base"\ntitle = "Base"\nexecution_role = "direct"\n'
        'control_case_id = "control"\noracle_id = "reviewed-oracle"\n+++\nBase technique.',
        encoding="utf-8",
    )
    (pack / "cases" / "child.md").write_text(
        '+++\nid = "child"\ntitle = "Child"\nderived_from = ["base"]\n'
        'execution_role = "direct"\ncontrol_case_id = "control"\n'
        'oracle_id = "reviewed-oracle"\n+++\nChild technique.',
        encoding="utf-8",
    )
    target = TargetProfile(
        id="target-related",
        title="Related",
        base_url="https://related.example/v1",
        model="related-model",
    )
    service = AssessmentService(
        catalog=PackCatalog([tmp_path], include_builtin=False),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=TargetFactory(),  # type: ignore[arg-type]
    )
    await service.select_target("target-related")

    inputs = await service.campaign_inputs("Assess target-related child behavior")
    child = next(item for item in inputs["techniques"] if item["id"] == "related/child")

    assert child["derived_from_ids"] == ("related/base",)
    assert child["combination_source_ids"] == ()
    assert child["semantic_id"] == "related/child"
    assert child["strategy_id"] == "direct"
    assert child["transform_id"] == "baseline"
    assert child["language"] == "und"
    assert child["domain_id"] == "related"
    assert child["hypothesis_id"] is None
    assert child["equivalence_key"].startswith("sha256:")


async def test_target_tool_routes_to_exact_selected_profile() -> None:
    target = TargetProfile(
        id="target-exact",
        title="Exact",
        base_url="https://exact.example/v1",
        model="exact-model",
    )
    state_scopes = StateScopeStore()
    factory = TargetFactory(state_scopes)
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=factory,  # type: ignore[arg-type]
        state_scope_store=state_scopes,  # type: ignore[arg-type]
    )
    await service.select_target("target-exact")

    tool = service.target_tool()
    result = await tool.invoke(
        ToolContext(
            session_id="session-1",
            workspace_root=".",
            campaign_id="campaign:target-exact",
            call_id="call:target-exact",
        ),
        {"prompt": "hello"},
    )

    assert tool.effect == "target_network"
    assert result.content == "target replied to: hello"
    assert result.metadata["target_profile_id"] == "target-exact"
    assert result.metadata["target_model"] == "exact-model"
    assert result.metadata["compiled_adapter_digest"] == compiled_target_adapter_digest(
        compile_target_adapter(
            {
                "interface": "open_ai_compatible",
                "endpoint": target.base_url,
                "credential_handle": TargetProviderFactory.credential_key(target),
            }
        )
    )
    assert factory.profiles[-1] == target
    assert state_scopes.events == [
        "created",
        "claimed",
        "revised",
        "acknowledged",
        "settled",
        "released",
    ]
    settled = state_scopes.envelopes[-1]
    assert settled["before"] == {
        "availability": "empty",
        "evidence_refs": [],
        "item_count": 0,
    }
    assert settled["after"] == {
        "availability": "empty",
        "evidence_refs": [],
        "item_count": 0,
    }
    assert settled["responses"]["availability"] == "present"
    assert settled["responses"]["item_count"] == 2


async def test_target_tool_never_delivers_before_state_scope_persists() -> None:
    target = TargetProfile(
        id="target-no-scope",
        title="No Scope",
        base_url="https://no-scope.example/v1",
        model="no-scope-model",
    )
    state_scopes = FailingStateScopeStore()
    factory = TargetFactory(state_scopes)
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=factory,  # type: ignore[arg-type]
        state_scope_store=state_scopes,  # type: ignore[arg-type]
    )
    await service.select_target(target.id)
    provider_constructions = len(factory.profiles)

    result = await service.target_tool().invoke(
        ToolContext(
            session_id="session-1",
            workspace_root=".",
            campaign_id="campaign:no-scope",
            call_id="call:no-scope",
        ),
        {"prompt": "must not be sent"},
    )

    assert result.is_error is True
    assert result.metadata["failure_code"] == "state_scope_unavailable"
    assert result.metadata["target_effect_attempted"] is False
    assert result.metadata["target_call_budget_charge"] == 0
    assert len(factory.profiles) == provider_constructions
    assert state_scopes.events == ["create_failed"]


async def test_target_tool_persists_scope_before_real_postgres_delivery(tmp_path: Path) -> None:
    target = TargetProfile(
        id="target-postgres-scope",
        title="Postgres Scope",
        base_url="https://postgres-scope.example/v1",
        model="postgres-scope-model",
    )
    factory = TargetFactory()
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("target chat state scope")
            campaign_id = "campaign/target-chat-state-scope"
            CampaignStore(connection).prepare(
                campaign_id,
                str(session.id),
                {"manifest": {}},
                "sha256:" + "4" * 64,
            )
            state_scope_store = CampaignApiSessionRepository(connection)
            service = AssessmentService(
                catalog=PackCatalog(),
                store=Store(target),  # type: ignore[arg-type]
                attacker=object(),
                attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
                target_factory=factory,  # type: ignore[arg-type]
                state_scope_store=state_scope_store,
            )
            await service.select_target(target.id)

            result = await service.target_tool().invoke(
                ToolContext(
                    session_id=str(session.id),
                    workspace_root=".",
                    campaign_id=campaign_id,
                    call_id="call:postgres-target-chat",
                ),
                {"prompt": "hello through a durable scope"},
            )
            scope_id = (
                "state-scope:"
                + hashlib.sha256(f"{campaign_id}\0call:postgres-target-chat".encode()).hexdigest()
            )
            scope = state_scope_store.load_state_scope(scope_id)

    assert result.is_error is False
    assert scope is not None
    assert scope["status"] == "settled"
    assert scope["isolation_disposition"] == "shared_serialized"


async def test_legacy_run_pack_never_calls_target_without_prepared_campaign() -> None:
    target = TargetProfile(
        id="target-legacy-run",
        title="Legacy Run",
        base_url="https://legacy-run.example/v1",
        model="legacy-run-model",
    )
    catalog = PackCatalog()
    catalog.enable("merlin-injection-taxonomy")
    factory = TargetFactory()
    service = AssessmentService(
        catalog=catalog,
        store=Store(target),  # type: ignore[arg-type]
        attacker=TargetProvider(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=factory,  # type: ignore[arg-type]
    )
    await service.select_target(target.id)
    provider_constructions = len(factory.profiles)

    with pytest.raises(RuntimeError, match="prepare, approve, and execute a reviewed campaign"):
        _ = [item async for item in service.run_pack(None, "session-legacy-run")]

    assert len(factory.profiles) == provider_constructions


async def test_worker_target_context_survives_process_selection_loss() -> None:
    target = TargetProfile(
        id="target-durable",
        title="Durable",
        base_url="https://durable.example/v1",
        model="durable-model",
    )
    state_scopes = StateScopeStore()
    factory = TargetFactory(state_scopes)
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=factory,  # type: ignore[arg-type]
        state_scope_store=state_scopes,  # type: ignore[arg-type]
    )

    result = await service.target_tool().invoke(
        ToolContext(
            session_id="session-1",
            workspace_root=".",
            campaign_id="campaign:target-durable",
            call_id="call:target-durable",
            target_id="target-durable",
        ),
        {"prompt": "hello after restart"},
    )

    assert result.content == "target replied to: hello after restart"
    assert result.metadata["target_profile_id"] == "target-durable"
    assert result.metadata["target_model"] == "durable-model"
    assert result.metadata["compiled_adapter_digest"] == compiled_target_adapter_digest(
        compile_target_adapter(
            {
                "interface": "open_ai_compatible",
                "endpoint": target.base_url,
                "credential_handle": TargetProviderFactory.credential_key(target),
            }
        )
    )


async def test_target_tool_executes_the_canonical_selected_provider_adapter() -> None:
    target = TargetProfile(
        id="target-canonical",
        title="Canonical",
        base_url="HTTPS://Canonical.Example/v1",
        model="canonical-model",
    )
    state_scopes = StateScopeStore()
    factory = TargetFactory(state_scopes)
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=factory,  # type: ignore[arg-type]
        state_scope_store=state_scopes,  # type: ignore[arg-type]
    )
    await service.select_target(target.id)

    compilation = compile_target_adapter(
        {
            "interface": "api",
            "endpoint": target.base_url,
            "credential_handle": "credential:reviewed-target",
        }
    )
    compiled_digest = compiled_target_adapter_digest(compilation)

    result = await service.target_tool().invoke(
        ToolContext(
            session_id="session-1",
            workspace_root=".",
            campaign_id="campaign:target-canonical",
            call_id="call:target-canonical",
            target_id=target.id,
            compiled_adapter=compilation.model_dump_json(),
            compiled_adapter_digest=compiled_digest,
        ),
        {"prompt": "hello canonical target"},
    )

    assert result.is_error is False
    assert factory.profiles[-1].base_url == compilation.executable.endpoint
    assert factory.credential_handles[-1] == "credential:reviewed-target"
    assert result.metadata == {
        "target_profile_id": target.id,
        "target_model": target.model,
        "compiled_adapter_digest": compiled_digest,
        "response_boundary_complete": True,
        "response_completion_count": 1,
    }


async def test_target_tool_rejects_provider_stream_without_completion_boundary() -> None:
    target = TargetProfile(
        id="target-incomplete",
        title="Incomplete",
        base_url="https://incomplete.example/v1",
        model="incomplete-model",
    )
    state_scopes = StateScopeStore()
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=IncompleteTargetFactory(state_scopes),  # type: ignore[arg-type]
        state_scope_store=state_scopes,  # type: ignore[arg-type]
    )
    await service.select_target(target.id)

    result = await service.target_tool().invoke(
        ToolContext(
            session_id="session-1",
            workspace_root=".",
            campaign_id="campaign:target-incomplete",
            call_id="call:target-incomplete",
        ),
        {"prompt": "hello"},
    )

    assert result.is_error is True
    assert result.content == "partial target reply"
    assert result.metadata["failure_code"] == "incomplete_response_boundary"
    assert result.metadata["attempt_disposition"] == "infrastructure_failed"
    assert result.metadata["response_boundary_complete"] is False
    assert state_scopes.events[-2:] == ["settled", "released"]


async def test_target_tool_records_typed_missing_evidence_when_transport_fails() -> None:
    target = TargetProfile(
        id="target-transport-failure",
        title="Transport failure",
        base_url="https://transport-failure.example/v1",
        model="transport-failure-model",
    )
    state_scopes = StateScopeStore()
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=FailingTargetFactory(state_scopes),  # type: ignore[arg-type]
        state_scope_store=state_scopes,  # type: ignore[arg-type]
    )
    await service.select_target(target.id)

    with pytest.raises(RuntimeError, match="transport failed before response"):
        await service.target_tool().invoke(
            ToolContext(
                session_id="session-1",
                workspace_root=".",
                campaign_id="campaign:target-transport-failure",
                call_id="call:target-transport-failure",
            ),
            {"prompt": "hello"},
        )

    paused = state_scopes.envelopes[-1]
    assert paused["responses"] == {
        "availability": "missing",
        "evidence_refs": [],
        "item_count": 0,
        "missing": {"reason": "transport_failed", "retryable": True},
    }
    assert paused["after"] == {
        "availability": "missing",
        "evidence_refs": [],
        "item_count": 0,
        "missing": {"reason": "transport_failed", "retryable": True},
    }
    assert state_scopes.events[-2:] == ["paused", "released"]


async def test_target_tool_rejects_compiled_adapter_drift_before_target_execution() -> None:
    target = TargetProfile(
        id="target-bound",
        title="Bound",
        base_url="https://bound.example/v1",
        model="bound-model",
    )
    factory = TargetFactory()
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=factory,  # type: ignore[arg-type]
    )
    await service.select_target(target.id)
    calls_after_selection = len(factory.profiles)
    drifted = compile_target_adapter(
        {
            "interface": "open_ai_compatible",
            "endpoint": "https://other.example/v1",
        }
    )

    result = await service.target_tool().invoke(
        ToolContext(
            session_id="session-1",
            workspace_root=".",
            target_id=target.id,
            compiled_adapter=drifted.model_dump_json(),
            compiled_adapter_digest=compiled_target_adapter_digest(drifted),
        ),
        {"prompt": "must not be sent"},
    )

    assert result.is_error is True
    assert result.metadata["failure_code"] == "adapter_state_integrity"
    assert result.metadata["retryable"] is False
    assert len(factory.profiles) == calls_after_selection


async def test_target_tool_rejects_tampered_compiled_adapter_digest() -> None:
    target = TargetProfile(
        id="target-digest-bound",
        title="Digest bound",
        base_url="https://digest-bound.example/v1",
        model="digest-bound-model",
    )
    factory = TargetFactory()
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=factory,  # type: ignore[arg-type]
    )
    await service.select_target(target.id)
    calls_after_selection = len(factory.profiles)
    compilation = compile_target_adapter(
        {
            "interface": "open_ai_compatible",
            "endpoint": target.base_url,
        }
    )

    result = await service.target_tool().invoke(
        ToolContext(
            session_id="session-1",
            workspace_root=".",
            target_id=target.id,
            compiled_adapter=compilation.model_dump_json(),
            compiled_adapter_digest="sha256:" + "0" * 64,
        ),
        {"prompt": "must not be sent"},
    )

    assert result.is_error is True
    assert result.metadata["failure_code"] == "adapter_state_integrity"
    assert result.metadata["retryable"] is False
    assert len(factory.profiles) == calls_after_selection


async def test_broker_records_provider_seal_mismatch_as_clean_no_effect(
    tmp_path: Path,
) -> None:
    target = TargetProfile(
        id="target-broker-bound",
        title="Broker bound",
        base_url="https://broker-bound.example/v1",
        model="broker-bound-model",
    )
    factory = TargetFactory()
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=factory,  # type: ignore[arg-type]
    )
    await service.select_target(target.id)
    provider_constructions_after_selection = len(factory.profiles)
    compilation = compile_target_adapter(
        {
            "interface": "open_ai_compatible",
            "endpoint": target.base_url,
        }
    )

    async def allow(*_args) -> bool:
        return True

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("provider seal mismatch")
            effects = PostgresToolEffectStore(ToolEffectRepository(connection))
            broker = ToolBroker(
                (service.target_tool(),),
                confirmer=allow,
                effect_store=effects,
            )
            result = await broker.invoke(
                "target_chat",
                ToolContext(
                    session_id=str(session.id),
                    workspace_root=str(tmp_path),
                    call_id="call-provider-seal-mismatch",
                    agent_id="campaign/provider-seal/worker",
                    target_id=target.id,
                    compiled_adapter=compilation.model_dump_json(),
                    compiled_adapter_digest="sha256:" + "0" * 64,
                ),
                {"prompt": "must not be sent"},
            )
            records = await effects.list_for_session(str(session.id), tool_name="target_chat")

    assert result.is_error is True
    assert result.metadata["failure_code"] == "adapter_state_integrity"
    assert result.metadata["infrastructure_failure_category"] == "adapter_state_integrity"
    assert result.metadata["retryable"] is False
    assert "effect_ambiguous" not in result.metadata
    assert len(records) == 1
    assert records[0].state is ToolEffectState.ACKNOWLEDGED
    assert records[0].result_is_error is True
    assert records[0].result_metadata["failure_code"] == "adapter_state_integrity"
    assert len(factory.profiles) == provider_constructions_after_selection


async def test_target_preflight_retains_non_secret_session_identity() -> None:
    target = TargetProfile(
        id="target-exact",
        title="Exact",
        base_url="https://exact.example/v1",
        model="exact-model",
    )
    factory = TargetFactory()
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=factory,  # type: ignore[arg-type]
    )
    await service.select_target(target.id)

    identity = await service.preflight_target()

    assert identity == {
        "target_id": target.id,
        "credential_handle": "target_handle",
        "authenticated": True,
        "server_identity": "user-7",
        "status_code": 200,
    }


async def test_target_tool_resolver_requires_matching_authenticated_preflight() -> None:
    target = TargetProfile(
        id="target-session-bound",
        title="Session Bound",
        base_url="https://session-bound.example/v1",
        model="session-bound-model",
    )
    factory = TargetFactory()
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=factory,  # type: ignore[arg-type]
    )
    await service.select_target(target.id)
    tool = service.target_tool()
    context = ToolContext(
        session_id="session-1",
        workspace_root=".",
        campaign_id="campaign/session-bound",
        target_id=target.id,
        call_id="call/session-bound",
        agent_id="agent/session-bound",
    )

    assert tool.resolve_target_session_id(context) is None
    await service.preflight_target()
    resolved = tool.resolve_target_session_id(context)
    assert resolved is not None

    service._target_session_identity = {  # noqa: SLF001 - pre-dispatch drift regression
        "target_id": target.id,
        "credential_handle": "target_handle",
        "authenticated": True,
        "server_identity": "user-8",
        "status_code": 200,
    }
    provider_constructions = len(factory.profiles)
    result = await tool.invoke(
        context.model_copy(update={"expected_target_session_id": resolved}),
        {"prompt": "must not dispatch after preflight drift"},
    )

    assert result.is_error is True
    assert result.metadata["failure_code"] == "target_session_identity_mismatch"
    assert result.metadata["target_effect_attempted"] is False
    assert len(factory.profiles) == provider_constructions


async def test_campaign_exact_target_id_wins_over_shared_base_url() -> None:
    exact = TargetProfile(
        id="target-exact",
        title="Exact",
        base_url="https://shared.example/v1",
        model="exact-model",
    )
    other = exact.model_copy(update={"id": "target-other", "title": "Other"})

    class MultiStore:
        async def list_targets(self):
            return (exact, other)

        async def get_target(self, target_id):
            return {exact.id: exact, other.id: other}.get(target_id)

    service = AssessmentService(
        catalog=PackCatalog(),
        store=MultiStore(),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=TargetFactory(),  # type: ignore[arg-type]
    )

    inputs = await service.campaign_inputs("Assess target-exact at https://shared.example/v1")

    assert inputs["target"]["id"] == "target-exact"


async def test_campaign_registers_https_target_from_natural_objective_for_review() -> None:
    class EmptyStore:
        def __init__(self, *targets) -> None:
            self.targets = {target.id: target for target in targets}

        async def list_targets(self):
            return tuple(self.targets.values())

        async def get_target(self, target_id):
            return self.targets.get(target_id)

        async def save_target(self, profile):
            self.targets[profile.id] = profile
            return profile

    store = EmptyStore()
    factory = TargetFactory()
    service = AssessmentService(
        catalog=PackCatalog(),
        store=store,  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=factory,  # type: ignore[arg-type]
    )

    inputs = await service.campaign_inputs(
        "Run a merlin scan on https://Plunder.me, policy refund trap L1"
    )

    assert inputs["target"]["base_url"] == "https://plunder.me/"
    assert inputs["target"]["model"] == "web-chat"
    assert inputs["target"]["id"].startswith("web-plunder-me-")
    assert tuple(store.targets) == (inputs["target"]["id"],)
    # Preparing the review is non-operative. Provider creation and network
    # preflight remain behind the later operator-approved execution boundary.
    assert factory.profiles == []


async def test_objective_url_does_not_silently_select_unrelated_only_target() -> None:
    configured = TargetProfile(
        id="configured",
        title="Different target",
        base_url="https://different.example/v1",
        model="different-model",
    )

    class StoreWithSave(Store):
        def __init__(self) -> None:
            super().__init__(configured)
            self.saved = []

        async def save_target(self, profile):
            self.saved.append(profile)
            return profile

        async def get_target(self, target_id):
            if self.saved and self.saved[-1].id == target_id:
                return self.saved[-1]
            return await super().get_target(target_id)

    store = StoreWithSave()
    factory = TargetFactory()
    service = AssessmentService(
        catalog=PackCatalog(),
        store=store,  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=factory,  # type: ignore[arg-type]
    )

    inputs = await service.campaign_inputs("Assess https://new.example/chat")

    assert inputs["target"]["base_url"] == "https://new.example/chat"
    assert factory.profiles == []


async def test_campaign_preserves_policy_inventory_and_matching_guidance() -> None:
    target = TargetProfile(
        id="target-policy",
        title="Policy chatbot",
        base_url="https://policy.example/v1",
        model="policy-model",
    )
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=TargetFactory(),  # type: ignore[arg-type]
    )

    inputs = await service.campaign_inputs("Assess target-policy for the policy refund trap L1")

    assert inputs["techniques"]
    assert len(inputs["techniques"]) > 1
    assert all(
        item["id"].startswith("merlin-injection-taxonomy/cat/") for item in inputs["techniques"]
    )
    assert any(
        item["id"].endswith("/cat/direct/override-baseline") for item in inputs["techniques"]
    )
    document_ids = [item["id"] for item in inputs["documents"]]
    assert "pack:merlin-injection-taxonomy" in document_ids
    assert any(item.startswith("knowledge:com.merlin.") for item in document_ids)
    assert len(inputs["knowledge_catalogue"]) == 8
    assert inputs["selected_knowledge"]


async def test_recommended_campaign_preserves_complete_pre_recon_inventory() -> None:
    target = TargetProfile(
        id="target-recommended",
        title="Policy chatbot",
        base_url="https://policy.example/v1",
        model="policy-model",
    )
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=TargetFactory(),  # type: ignore[arg-type]
    )

    inputs = await service.campaign_inputs(
        "Run a scan for the policy refund trap", profile="recommended"
    )

    exhaustive = await service.campaign_inputs(
        "Run a scan for the policy refund trap", profile="exhaustive"
    )

    assert {item["id"] for item in inputs["techniques"]} == {
        item["id"] for item in exhaustive["techniques"]
    }
    assert {item["strategy_id"] for item in inputs["techniques"]} >= {
        "crescendo",
        "hydra-select",
        "goat",
    }


async def test_exhaustive_campaign_keeps_complete_catalogue() -> None:
    target = TargetProfile(
        id="target-exhaustive",
        title="Policy chatbot",
        base_url="https://policy.example/v1",
        model="policy-model",
    )
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=TargetFactory(),  # type: ignore[arg-type]
    )

    recommended = await service.campaign_inputs(
        "Run a scan for the policy refund trap", profile="recommended"
    )
    exhaustive = await service.campaign_inputs(
        "Run a scan for the policy refund trap", profile="exhaustive"
    )

    assert {item["id"] for item in exhaustive["techniques"]} == {
        item["id"] for item in recommended["techniques"]
    }


async def test_campaign_inputs_preserve_explicit_catalogue_languages() -> None:
    target = TargetProfile(
        id="target-languages",
        title="Multilingual chatbot",
        base_url="https://languages.example/v1",
        model="target-model",
    )
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=TargetFactory(),  # type: ignore[arg-type]
    )

    inputs = await service.campaign_inputs("Assess multilingual behavior", profile="exhaustive")
    by_id = {str(item["id"]): item for item in inputs["techniques"]}

    malay = by_id["merlin-injection-taxonomy/cat/direct/multilingual-pivot-ms"]
    assert malay["language"] == "ms"
    assert malay["transform_id"] == "native"

    mixed = by_id["merlin-injection-taxonomy/cat/direct/multilingual-pivot-zh-en"]
    assert mixed["language"] == "zh-en"
    assert mixed["transform_id"] == "mixed"


async def test_exploration_campaign_preserves_complete_pre_recon_inventory() -> None:
    target = TargetProfile(
        id="target-exploration",
        title="Policy chatbot",
        base_url="https://policy.example/v1",
        model="policy-model",
    )
    service = AssessmentService(
        catalog=PackCatalog(),
        store=Store(target),  # type: ignore[arg-type]
        attacker=object(),
        attacker_config=ProviderConfig("https://attacker.example/v1", "", "attacker"),
        target_factory=TargetFactory(),  # type: ignore[arg-type]
    )

    exploration = await service.campaign_inputs(
        "Run a scan for the policy refund trap", profile="exploration"
    )
    exhaustive = await service.campaign_inputs(
        "Run a scan for the policy refund trap", profile="exhaustive"
    )

    assert {item["id"] for item in exploration["techniques"]} == {
        item["id"] for item in exhaustive["techniques"]
    }
