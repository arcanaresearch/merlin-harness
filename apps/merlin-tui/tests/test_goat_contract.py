import hashlib
import json
from dataclasses import replace

import pytest
from merlin_agents import (
    AssessmentPrepareTool,
    AssignmentBudget,
    AssignmentContext,
    AssignmentTechnique,
    CampaignManifest,
    CampaignPreparation,
    CampaignProfile,
    CampaignService,
    ContextDocument,
    DynamicSpecialistProposal,
    ReviewedTargetAdapter,
    compile_attack_campaign,
    compile_dynamic_specialist,
    compile_worker_context,
    template_by_id,
    template_technique_routing,
)
from merlin_agents.campaign import (
    CampaignCompilation,
    compile_execution_agenda,
    profile_grant,
    reviewed_adapter_supports_multi_turn,
)
from merlin_contracts import ToolContext
from merlin_tui.bootstrap import (
    _crescendo_contract_for,
    _goat_contract_for,
    _hydra_select_contract_for,
    _hydra_select_preview_support,
    _with_adaptive_oracle_observations,
)


def _production_adaptive_agenda(manifest, context):
    """Compile the adaptive agenda the way campaign execute does.

    There is no fixed category-worker roster: the adaptive cell is owned by the
    admitted wave-0 specialist the template routing selects, merged into the
    attack compilation before agenda expansion.
    """

    attack = compile_attack_campaign(manifest, context)
    template = template_by_id("merlin-core-specialists", 1)
    routing = template_technique_routing(template, context.techniques)
    ceiling = CampaignService._dynamic_authority_ceiling(manifest, context)  # noqa: SLF001
    assignments = list(attack.assignments)
    worker_contexts = list(attack.worker_contexts)
    for index, (profile_id, technique_ids) in enumerate(sorted(routing.items())):
        if profile_id == "adversarial-explorer":
            continue
        proposal = DynamicSpecialistProposal(
            campaign_id=manifest.id,
            label=f"dynamic-{profile_id}",
            objective=f"Cover the {profile_id} specialty",
            source_task_ids=("recon-1",),
            evidence_refs=("receipt:recon-1",),
            evidence_expectation="paired receipts",
            technique_ids=technique_ids,
            requested_model=context.model_for(profile_id),
        )
        record = compile_dynamic_specialist(
            proposal,
            parent=ceiling,
            context=context,
            graph_id=manifest.id,
            wave=0,
            active_count=index,
            campaign_count=index,
        )
        assignments.append(record.assignment)
        worker_contexts.append(
            compile_worker_context(
                manifest,
                record.assignment,
                record.profile,
                capability_grant=profile_grant(
                    "adversarial-explorer",
                    target_scope=context.target_profile_id,
                ),
            )
        )
    merged = CampaignCompilation(tuple(assignments), tuple(worker_contexts))
    return compile_execution_agenda(manifest, context, merged)


def digest(value: object) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def adapter(*, repeat: object = 1) -> dict[str, object]:
    oracle = digest("oracle")
    return {
        "interface": "http_api",
        "api_steps": [
            {
                "id": "interaction",
                "interaction": True,
                "cleanup": False,
                "repeat": repeat,
                "oracle_predicates": [
                    {
                        "purpose": "objective",
                        "oracle_digest": oracle,
                        "modality": "text",
                        "turn": "multi",
                        "language": "en",
                    }
                ],
            }
        ],
    }


def bind(value: dict[str, object], *, objective: str = "Assess exact objective"):
    return _goat_contract_for(
        value,
        technique_id="case:goat",
        semantic_technique_id="semantic:goat",
        objective=objective,
        attacker_provider_id="provider-1",
        attacker_model="model-1",
        attacker_transport=None,
        target_profile_id="target-1",
        expected_oracle_digest=digest("oracle"),
    )


def test_goat_contract_binds_goal_route_definitions_and_repeat() -> None:
    assert bind(adapter())
    assert bind(adapter(repeat=True)) == ()
    assert bind(adapter(repeat=2)) == ()
    assert bind(adapter(), objective="drifted objective")
    extra = adapter()
    extra["api_steps"][0]["oracle_predicates"].append(
        {
            "purpose": "progress",
            "oracle_digest": digest("progress"),
            "modality": "text",
            "turn": "multi",
            "language": "en",
        }
    )
    assert bind(extra)
    duplicate = adapter()
    duplicate["api_steps"][0]["oracle_predicates"].append(
        {
            "purpose": "objective",
            "oracle_digest": digest("other"),
            "modality": "text",
            "turn": "multi",
            "language": "en",
        }
    )
    duplicate_contract = bind(duplicate)
    assert duplicate_contract
    assert json.loads(dict(duplicate_contract)["evaluation_uncertainties_json"])


def test_singular_objective_oracle_stays_exact_and_crescendo_uses_validator_fallback() -> None:
    source = _reviewed_adapter(singular_objective=True)

    adapted = _with_adaptive_oracle_observations(source)
    assert adapted is source
    assert "oracle_predicates" not in adapted["api_steps"][1]
    contract = _crescendo_contract_for(
        adapted,
        technique_id="case:crescendo",
        semantic_technique_id="semantic:crescendo",
    )
    assert contract
    uncertainties = json.loads(dict(contract)["evaluation_uncertainties_json"])
    assert len(uncertainties) == 3
    assert all(item["kind"] == "evaluation_uncertain" for item in uncertainties)


def test_missing_objective_oracle_keeps_controllers_without_fabricated_predicates() -> None:
    source = _reviewed_adapter()
    source["interface"] = "http_api"
    source["api_steps"][1].pop("oracle_predicates")

    adapted = _with_adaptive_oracle_observations(source)
    assert adapted is source
    assert "oracle_predicate" not in adapted["api_steps"][1]
    assert "oracle_predicates" not in adapted["api_steps"][1]
    crescendo = _crescendo_contract_for(
        adapted,
        technique_id="case:crescendo",
        semantic_technique_id="semantic:crescendo",
    )
    goat = bind(adapted)
    hydra = _hydra_select_contract_for(
        adapted,
        semantic_technique_id="semantic:hydra",
        objective="Assess exact Hydra objective",
        attacker_provider_id="provider-1",
        attacker_model="worker-model",
        attacker_transport=None,
        target_profile_id="target-1",
        expected_oracle_digest=digest("oracle:objective"),
    )
    for contract in (crescendo, goat, hydra):
        assert contract
        uncertainties = json.loads(dict(contract)["evaluation_uncertainties_json"])
        assert uncertainties
        assert all(item["kind"] == "evaluation_uncertain" for item in uncertainties)

    supported, reason = _hydra_select_preview_support(
        {
            "tags": ("text", "multi", "conversational"),
            "transform_id": "baseline",
            "language": "am",
        },
        hydra,
        8,
    )
    assert supported is True
    assert reason is None


@pytest.mark.asyncio
async def test_non_und_hydra_compiles_without_a_native_objective_predicate() -> None:
    reviewed = _reviewed_adapter()
    reviewed["api_steps"][1].pop("oracle_predicates")
    factory, captured = _preparation_factory("hydra-select")
    await factory(
        "session-1",
        "Run reviewed Hydra in Amharic",
        CampaignProfile.RECOMMENDED,
        reviewed,
    )
    prepared = captured[-1]
    technique = replace(prepared.assignment_context.techniques[0], language="am")
    context = replace(prepared.assignment_context, techniques=(technique,))

    agenda = _production_adaptive_agenda(prepared.manifest, context)
    adaptive = next(item for item in agenda.assignments if item.technique_ids == (technique.id,))

    assert adaptive.strategy_id == "hydra-select"
    assert dict(adaptive.strategy_contract)["language"] == "am"
    uncertainties = json.loads(dict(adaptive.strategy_contract)["evaluation_uncertainties_json"])
    assert any("validator" in item["reason"] for item in uncertainties)


@pytest.mark.parametrize(
    ("item_patch", "max_turns", "expected_reason"),
    (
        ({}, 3, "exceed budget"),
        ({"transform_id": "unicode"}, 8, "baseline transform"),
        ({"language": "fr"}, 8, "oracle language differ"),
        ({"tags": ("text", "multi")}, 8, "conversational text turns"),
    ),
)
def test_hydra_preview_support_matches_compiler_visible_invariants(
    item_patch: dict[str, object], max_turns: int, expected_reason: str
) -> None:
    objective = "Assess exact Hydra objective"
    reviewed = _reviewed_adapter(singular_objective=True)
    contract = _hydra_select_contract_for(
        reviewed,
        semantic_technique_id="semantic:hydra",
        objective=objective,
        attacker_provider_id="provider-1",
        attacker_model="worker-model",
        attacker_transport=None,
        target_profile_id="target-1",
        expected_oracle_digest=digest("oracle:objective"),
    )
    item = {
        "tags": ("text", "multi", "conversational"),
        "transform_id": "baseline",
        "language": "en",
        **item_patch,
    }

    supported, reason = _hydra_select_preview_support(item, contract, max_turns)

    assert supported is False
    assert reason is not None and expected_reason in reason


def test_hydra_preview_rejects_no_tool_cli_transport() -> None:
    contract = _hydra_select_contract_for(
        _reviewed_adapter(singular_objective=True),
        semantic_technique_id="semantic:hydra",
        objective="Assess exact Hydra objective",
        attacker_provider_id="provider-1",
        attacker_model="cli-model",
        attacker_transport="kimi_acp",
        target_profile_id="target-1",
        expected_oracle_digest=digest("oracle:objective"),
    )
    supported, _ = _hydra_select_preview_support(
        {
            "tags": ("text", "multi", "conversational"),
            "transform_id": "baseline",
            "language": "en",
        },
        contract,
        8,
    )
    assert supported is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("technique_changes", "budget", "uncertainty_reason"),
    (
        (
            {"transform_id": "unicode"},
            AssignmentBudget(8, None, None),
            "transformed technique cell",
        ),
        (
            {"language": "fr"},
            AssignmentBudget(8, None, None),
            "requested language differed",
        ),
        (
            {"tags": frozenset({"text", "multi"})},
            AssignmentBudget(8, None, None),
            "modality, turn, or delivery tags differed",
        ),
        ({}, AssignmentBudget(3, None, None), None),
    ),
)
async def test_hydra_preview_uncertainty_compiles_direct_fallback(
    technique_changes: dict[str, object],
    budget: AssignmentBudget,
    uncertainty_reason: str | None,
) -> None:
    factory, captured = _preparation_factory("hydra-select")
    await factory(
        "session-1",
        "Run reviewed Hydra",
        CampaignProfile.RECOMMENDED,
        _reviewed_adapter(singular_objective=True),
        budget,
    )
    prepared = captured[-1]
    original = prepared.assignment_context.techniques[0]
    technique = replace(original, comparison_supported=True, **technique_changes)
    item = {
        "tags": tuple(technique.tags),
        "transform_id": technique.transform_id,
        "language": technique.language,
    }
    preview_supported, _ = _hydra_select_preview_support(
        item, technique.strategy_contract, budget.max_turns
    )
    context = replace(
        prepared.assignment_context,
        techniques=(technique,),
        budget=budget,
    )
    manifest = replace(prepared.manifest, budget=budget)
    agenda = _production_adaptive_agenda(manifest, context)
    adaptive = next(item for item in agenda.assignments if item.technique_ids == (technique.id,))

    assert preview_supported is False
    assert adaptive.strategy_id == "hydra-select"
    assert adaptive.comparison_supported is True
    assert adaptive.comparison_block_reason is None
    uncertainties = json.loads(
        dict(adaptive.strategy_contract).get("evaluation_uncertainties_json", "[]")
    )
    if uncertainty_reason is None:
        assert uncertainties == []
    else:
        assert "EVALUATION UNCERTAIN" in adaptive.objective
        assert any(uncertainty_reason in item["reason"] for item in uncertainties)


@pytest.mark.asyncio
async def test_dynamic_baseline_preserves_exact_delegated_grant() -> None:
    factory, captured = _preparation_factory("goat")
    await factory(
        "session-1",
        "Run reviewed direct observation",
        CampaignProfile.RECOMMENDED,
        _reviewed_adapter(singular_objective=True),
    )
    prepared = captured[-1]
    technique = replace(
        prepared.assignment_context.techniques[0],
        strategy_id="direct",
        strategy_contract=(),
        execution_role="direct",
    )
    context = replace(prepared.assignment_context, techniques=(technique,))

    agenda = _production_adaptive_agenda(prepared.manifest, context)

    direct = next(item for item in agenda.assignments if item.technique_ids == (technique.id,))
    assert direct.strategy_id == "direct"
    assert direct.profile_id.startswith("dynamic-")
    assert direct.grant_id == "profile:adversarial-explorer"
    assert any(item.assignment_id == direct.id for item in agenda.worker_contexts)


class _PreparationStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}

    def prepare(
        self,
        campaign_id: str,
        session_id: str,
        manifest: dict[str, object],
        digest_value: str,
    ) -> dict[str, object]:
        row = {
            "id": campaign_id,
            "session_id": session_id,
            "manifest": manifest,
            "manifest_digest": digest_value,
            "status": "prepared",
        }
        self.rows[campaign_id] = row
        return row


def _reviewed_adapter(*, singular_objective: bool = False) -> dict[str, object]:
    purposes = ("objective", "refusal", "progress", "invalid-premise")
    interaction = {
        "step_id": "message",
        "endpoint": "https://target.test/v1/chat/completions",
        "method": "POST",
        "request_template": {
            "sessionId": "{{capture:session_id}}",
            "message": "{{input}}",
        },
        "response_selector": "/message",
        "success_witness": "response body contains message",
        "expected_status": 200,
        "idempotency_key": None,
        "replay_after_ambiguous_result": False,
        "cleanup": False,
        "interaction": True,
        "captures": {},
        "repeat": 1,
    }
    predicates = [
        {
            "purpose": purpose,
            "oracle_digest": digest(f"oracle:{purpose}"),
            "pointer": "/verdict",
            "match_values": [purpose],
            "modality": "text",
            "turn": "multi",
            "language": "en",
        }
        for purpose in purposes
    ]
    if singular_objective:
        interaction["oracle_predicate"] = {
            key: value for key, value in predicates[0].items() if key != "purpose"
        }
    else:
        interaction["oracle_predicates"] = predicates
    return {
        "interface": "open_ai_compatible",
        "endpoint": "https://target.test/v1/chat/completions",
        "authentication": "reviewed bearer credential",
        "observed_evidence": ["recon-response-1"],
        "session_constraint": "one reviewed API session per campaign",
        "api_steps": [
            {
                "step_id": "setup",
                "endpoint": "https://target.test/v1/sessions",
                "method": "POST",
                "request_template": {},
                "response_selector": "/sessionId",
                "success_witness": "response body contains sessionId",
                "expected_status": 200,
                "idempotency_key": None,
                "replay_after_ambiguous_result": False,
                "cleanup": False,
                "interaction": False,
                "captures": {"session_id": "/sessionId"},
                "repeat": 1,
            },
            interaction,
            {
                "step_id": "cleanup",
                "endpoint": "https://target.test/v1/sessions/current",
                "method": "DELETE",
                "request_template": {},
                "response_selector": "",
                "success_witness": "reviewed session removed",
                "expected_status": 204,
                "idempotency_key": "reviewed-session-cleanup",
                "replay_after_ambiguous_result": True,
                "cleanup": True,
                "interaction": False,
                "captures": {},
                "repeat": 1,
            },
        ],
    }


def _preparation_factory(
    strategy_id: str,
    *,
    lead_model: str = "lead-model",
    lead_transport: str | None = None,
    worker_model: str = "worker-model",
    worker_transport: str | None = None,
    taxonomy_oracle_digest: str | None = None,
):
    captured: list[CampaignPreparation] = []

    async def prepare_campaign(
        session_id: str,
        objective: str,
        profile: CampaignProfile,
        reviewed_adapter: dict[str, object],
        budget_override: AssignmentBudget | None = None,
        **_kwargs: object,
    ) -> CampaignPreparation:
        oracle_digest = taxonomy_oracle_digest or digest("oracle:objective")
        semantic_id = f"semantic:{strategy_id}"
        technique_id = f"case:{strategy_id}"
        contract = (
            _goat_contract_for(
                reviewed_adapter,
                technique_id=technique_id,
                semantic_technique_id=semantic_id,
                objective=objective,
                attacker_provider_id="provider-1",
                attacker_model=worker_model,
                attacker_transport=worker_transport,
                target_profile_id="target-1",
                expected_oracle_digest=oracle_digest,
            )
            if strategy_id == "goat"
            else _hydra_select_contract_for(
                reviewed_adapter,
                semantic_technique_id=semantic_id,
                objective=objective,
                attacker_provider_id="provider-1",
                attacker_model=worker_model,
                attacker_transport=worker_transport,
                target_profile_id="target-1",
                expected_oracle_digest=oracle_digest,
            )
            if strategy_id == "hydra-select"
            else _crescendo_contract_for(
                reviewed_adapter,
                technique_id=technique_id,
                semantic_technique_id=semantic_id,
            )
        )
        supported = bool(contract)
        budget = budget_override or AssignmentBudget(64, None, None)
        adapter_document = ContextDocument.create(
            "target:reviewed-interaction-contract",
            "Reviewed target interaction contract.\n"
            + json.dumps(reviewed_adapter, sort_keys=True, separators=(",", ":")),
        )
        manifest = CampaignManifest.create(
            id=f"campaign-{strategy_id}",
            session_id=session_id,
            objective=objective,
            profile=profile,
            target=ReviewedTargetAdapter(
                "target-1",
                "http_api",
                "https://target.test/v1/chat/completions",
                "target-model",
            ),
            provider_id="provider-1",
            provider_model=lead_model,
            provider_effort="default",
            authority_ids=(f"session:{session_id}", "target:target-1"),
            catalogue=(adapter_document,),
            skill_documents=(),
            tool_ids=("target_request",),
            budget=budget,
        )
        context = AssignmentContext(
            target_profile_id="target-1",
            techniques=(
                AssignmentTechnique(
                    id=technique_id,
                    objective=objective,
                    tags=frozenset({"prompt", "injection", "text", "multi", "conversational"}),
                    guidance_ids=(),
                    semantic_id=semantic_id,
                    strategy_id=strategy_id,
                    language="en",
                    oracle_digest=oracle_digest,
                    comparison_supported=supported,
                    comparison_block_reason=(
                        None
                        if supported
                        else "GOAT v1 requires a buffered Merlin-tool-capable worker route"
                        if strategy_id == "goat"
                        and worker_transport
                        in {
                            "codex_app_server",
                            "claude_cli",
                            "kimi_acp",
                            "kiro_acp",
                            "opencode_acp",
                        }
                        else f"reviewed API {strategy_id} contract is unavailable"
                    ),
                    strategy_contract=contract,
                ),
            ),
            tool_ids=("target_request",),
            skill_ids=(),
            provider_id="provider-1",
            authority_ids=manifest.authority_ids,
            budget=budget,
            input_schema='{"type":"object"}',
            output_schema='{"type":"object"}',
            provider_model=lead_model,
            provider_role_models=(("lead", lead_model), ("routine", worker_model)),
            provider_model_pricing=(
                (lead_model, (1.0, 2.0)),
                (worker_model, (1.0, 2.0)),
            ),
            provider_model_transports=tuple(
                item
                for item in (
                    ((lead_model, lead_transport) if lead_transport is not None else None),
                    ((worker_model, worker_transport) if worker_transport is not None else None),
                )
                if item is not None
            ),
        )
        prepared = CampaignPreparation(manifest, context)
        captured.append(prepared)
        return prepared

    return prepare_campaign, captured


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_id", ("goat", "crescendo", "hydra-select"))
async def test_assessment_prepare_reaches_supported_production_adaptive_agenda(
    strategy_id: str,
) -> None:
    factory, captured = _preparation_factory(strategy_id)
    service = CampaignService(_PreparationStore(), object(), factory)  # type: ignore[arg-type]
    reviewed_adapter = _reviewed_adapter(singular_objective=strategy_id == "goat")
    assert reviewed_adapter_supports_multi_turn(reviewed_adapter) is True
    result = await AssessmentPrepareTool(service).invoke(
        ToolContext(session_id="session-1", workspace_root="/workspace"),
        {
            "objective": f"Run reviewed {strategy_id}",
            "campaign_profile": "recommended",
            "adapter": reviewed_adapter,
        },
    )

    assert result.is_error is False
    prepared = captured[-1]
    agenda = _production_adaptive_agenda(prepared.manifest, prepared.assignment_context)
    assert len(agenda.assignments) == 2
    adaptive = next(item for item in agenda.assignments if item.strategy_id == strategy_id)
    explorer = next(
        item for item in agenda.assignments if item.profile_id == "adversarial-explorer"
    )
    assert adaptive.comparison_supported is True
    assert adaptive.profile_id.startswith("dynamic-")
    assert adaptive.grant_id == "profile:adversarial-explorer"
    assert any(item.assignment_id == adaptive.id for item in agenda.worker_contexts)
    assert explorer.technique_ids == ()
    if strategy_id == "goat":
        assert result.metadata["review"]["target_contract"]["adaptive_strategies"] == (
            {
                "technique_id": "semantic:goat",
                "strategy_id": "goat",
                "target_turns": 5,
                "proposed_candidates": None,
                "selected_candidates": None,
                "max_target_calls": 5,
                "stop_on_positive": None,
                "session_policy": "same-reviewed-session",
                "tool_transport": "buffered",
                "supported": True,
                "block_reason": None,
            },
        )
    if strategy_id == "hydra-select":
        assert result.metadata["review"]["target_contract"]["adaptive_strategies"] == (
            {
                "technique_id": "semantic:hydra-select",
                "strategy_id": "hydra-select",
                "target_turns": 3,
                "proposed_candidates": 6,
                "selected_candidates": 3,
                "max_target_calls": 3,
                "stop_on_positive": True,
                "session_policy": "same-reviewed-session",
                "tool_transport": "buffered",
                "supported": True,
                "block_reason": None,
            },
        )


@pytest.mark.asyncio
async def test_goat_preview_route_digest_matches_heterogeneous_compiled_assignment() -> None:
    factory, captured = _preparation_factory("goat", worker_model="worker-model")
    service = CampaignService(_PreparationStore(), object(), factory)  # type: ignore[arg-type]
    result = await AssessmentPrepareTool(service).invoke(
        ToolContext(session_id="session-1", workspace_root="/workspace"),
        {
            "objective": "Run reviewed goat",
            "campaign_profile": "recommended",
            "adapter": _reviewed_adapter(singular_objective=True),
        },
    )

    assert result.is_error is False
    assert result.metadata["review"]["model_routes"]["adversarial-explorer"] == "worker-model"
    prepared = captured[-1]
    agenda = _production_adaptive_agenda(prepared.manifest, prepared.assignment_context)
    assignment = next(item for item in agenda.assignments if item.strategy_id == "goat")
    contract = dict(assignment.strategy_contract)
    assert assignment.provider_model == "worker-model"
    assert contract["attacker_route_digest"] == digest(
        {
            "provider_id": assignment.provider_id,
            "model": assignment.provider_model,
            "role": "worker",
            "tool_transport": "buffered",
        }
    )


@pytest.mark.asyncio
async def test_hydra_preview_route_digest_matches_heterogeneous_compiled_assignment() -> None:
    factory, captured = _preparation_factory("hydra-select", worker_model="worker-model")
    service = CampaignService(_PreparationStore(), object(), factory)  # type: ignore[arg-type]
    result = await AssessmentPrepareTool(service).invoke(
        ToolContext(session_id="session-1", workspace_root="/workspace"),
        {
            "objective": "Run reviewed Hydra",
            "campaign_profile": "recommended",
            "adapter": _reviewed_adapter(singular_objective=True),
        },
    )

    assert result.is_error is False
    assert result.metadata["review"]["model_routes"]["adversarial-explorer"] == "worker-model"
    prepared = captured[-1]
    agenda = _production_adaptive_agenda(prepared.manifest, prepared.assignment_context)
    assignment = next(item for item in agenda.assignments if item.strategy_id == "hydra-select")
    contract = dict(assignment.strategy_contract)
    assert assignment.provider_model == "worker-model"
    assert contract["attacker_route_digest"] == digest(
        {
            "provider_id": assignment.provider_id,
            "model": assignment.provider_model,
            "role": "worker",
            "tool_transport": "buffered",
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ("claude_cli", "kimi_acp", "kiro_acp", "opencode_acp"))
async def test_assessment_prepare_accepts_tool_capable_agent_routes(transport: str) -> None:
    factory, captured = _preparation_factory(
        "goat", worker_model=f"{transport}-model", worker_transport=transport
    )
    service = CampaignService(_PreparationStore(), object(), factory)  # type: ignore[arg-type]
    result = await AssessmentPrepareTool(service).invoke(
        ToolContext(session_id="session-1", workspace_root="/workspace"),
        {
            "objective": "Run reviewed goat",
            "campaign_profile": "recommended",
            "adapter": _reviewed_adapter(singular_objective=True),
        },
    )
    assert result.is_error is False
    assert captured[-1].assignment_context.techniques[0].comparison_supported is True


@pytest.mark.asyncio
async def test_low_worker_turn_budget_compiles_goat_direct_fallback() -> None:
    factory, captured = _preparation_factory("goat")
    service = CampaignService(_PreparationStore(), object(), factory)  # type: ignore[arg-type]
    result = await AssessmentPrepareTool(service).invoke(
        ToolContext(session_id="session-1", workspace_root="/workspace"),
        {
            "objective": "Run reviewed goat",
            "campaign_profile": "recommended",
            "budget": {"max_turns": 1},
            "adapter": _reviewed_adapter(singular_objective=True),
        },
    )

    assert result.is_error is False
    prepared = captured[-1]
    agenda = _production_adaptive_agenda(prepared.manifest, prepared.assignment_context)
    adaptive = next(item for item in agenda.assignments if item.technique_ids == ("case:goat",))
    assert adaptive.strategy_id == "goat"
    assert adaptive.comparison_supported is True
    assert adaptive.comparison_block_reason is None


@pytest.mark.asyncio
async def test_goat_binds_reviewed_target_oracle_not_generic_taxonomy_oracle() -> None:
    factory, captured = _preparation_factory(
        "goat", taxonomy_oracle_digest=digest("taxonomy-drift")
    )
    store = _PreparationStore()
    service = CampaignService(store, object(), factory)  # type: ignore[arg-type]
    result = await AssessmentPrepareTool(service).invoke(
        ToolContext(session_id="session-1", workspace_root="/workspace"),
        {
            "objective": "Run reviewed goat",
            "campaign_profile": "recommended",
            "adapter": _reviewed_adapter(singular_objective=True),
        },
    )

    assert result.is_error is False
    prepared = captured[-1]
    technique = prepared.assignment_context.techniques[0]
    assert technique.comparison_supported is True
    assert dict(technique.strategy_contract)["terminal_oracle_digest"] == digest("oracle:objective")
    agenda = _production_adaptive_agenda(prepared.manifest, prepared.assignment_context)
    adaptive = next(item for item in agenda.assignments if item.strategy_id == "goat")
    assert adaptive.comparison_supported is True
    assert store.rows["campaign-goat"]["status"] == "prepared"


@pytest.mark.asyncio
async def test_codex_worker_route_compiles_goat_direct_fallback() -> None:
    factory, captured = _preparation_factory(
        "goat", worker_model="codex-worker", worker_transport="codex_app_server"
    )
    service = CampaignService(_PreparationStore(), object(), factory)  # type: ignore[arg-type]
    result = await AssessmentPrepareTool(service).invoke(
        ToolContext(session_id="session-1", workspace_root="/workspace"),
        {
            "objective": "Run reviewed goat",
            "campaign_profile": "recommended",
            "adapter": _reviewed_adapter(singular_objective=True),
        },
    )

    assert result.is_error is False
    prepared = captured[-1]
    agenda = _production_adaptive_agenda(prepared.manifest, prepared.assignment_context)
    adaptive = next(item for item in agenda.assignments if item.technique_ids == ("case:goat",))
    assert adaptive.strategy_id == "goat"
    assert adaptive.comparison_supported is True
    assert adaptive.comparison_block_reason is None
    assert adaptive.objective.startswith("GOAT DEGRADED; DIRECT OBSERVATION ONLY.")
    contract = dict(adaptive.strategy_contract)
    assert contract["strategy_execution_mode"] == "degraded"
    assert contract["attacker_tool_transport"] == "buffered"
    assert contract["provider_route_transport"] == "codex_app_server"
    assert "provider route uses codex_app_server" in contract["strategy_execution_reason"]


@pytest.mark.asyncio
async def test_codex_campaign_lead_with_buffered_goat_worker_is_supported() -> None:
    factory, captured = _preparation_factory(
        "goat",
        lead_model="codex-lead",
        lead_transport="codex_app_server",
        worker_model="buffered-worker",
    )
    service = CampaignService(_PreparationStore(), object(), factory)  # type: ignore[arg-type]
    result = await AssessmentPrepareTool(service).invoke(
        ToolContext(session_id="session-1", workspace_root="/workspace"),
        {
            "objective": "Run reviewed goat",
            "campaign_profile": "recommended",
            "adapter": _reviewed_adapter(singular_objective=True),
        },
    )

    assert result.is_error is False
    assert result.metadata["review"]["model_routes"] == {
        "adversarial-explorer": "buffered-worker",
        "campaign-lead": "codex-lead",
        "recon": "codex-lead",
    }
    prepared = captured[-1]
    agenda = _production_adaptive_agenda(prepared.manifest, prepared.assignment_context)
    adaptive = next(item for item in agenda.assignments if item.strategy_id == "goat")
    assert adaptive.comparison_supported is True
