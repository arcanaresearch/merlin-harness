from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from merlin_contracts import PromotionCandidate, promotion_digest
from merlin_promotion import (
    CandidateCaseEvaluator,
    ControllerCaseEvaluator,
    EvaluationBinding,
    EvaluationCase,
    PromotionService,
    frozen_dataset_digest,
    load_controller_evaluation_fixture,
    load_frozen_evaluation_suite,
)

EVALUATIONS = Path(__file__).parent / "fixtures" / "controller_evaluations"
SUITE_IDS = ("controller-goat-v1", "controller-crescendo-v1", "controller-hydra-select-v1")


class Store:
    def __init__(self, candidate):
        self.candidates = {candidate.id: candidate}
        self.evaluations_by_candidate = {}

    def get(self, candidate_id):
        return self.candidates.get(candidate_id)

    def record_evaluation(self, evaluation):
        self.evaluations_by_candidate.setdefault(evaluation.candidate_id, []).append(evaluation)
        return evaluation


def candidate(strategy_id: str) -> PromotionCandidate:
    content = {
        "provider": "fixture-attacker",
        "model": "controller-fixture-model",
        "route": "controller_replay",
        "strategy_id": strategy_id,
        "role_scopes": ["lead"],
    }
    return PromotionCandidate(
        id=f"controller-eval-{strategy_id}",
        kind="routing",
        version="controller-fixture-v1",
        baseline_version="none",
        baseline_digest=f"sha256:{'0' * 64}",
        content=content,
        content_digest=promotion_digest(content),
        proposed_by="operator:review",
    )


@pytest.mark.parametrize("suite_id", SUITE_IDS)
def test_frozen_controller_suites_load_and_validate(suite_id: str) -> None:
    suite = load_frozen_evaluation_suite(EVALUATIONS / f"{suite_id}.json")

    assert suite.id == suite_id
    assert suite.candidate_kind == "routing"
    assert suite.route_role_scopes == ("lead",)
    assert suite.dataset_digest == frozen_dataset_digest(suite.cases)
    for case in suite.cases:
        fixture = load_controller_evaluation_fixture(case)
        assert fixture.strategy_id in {"goat", "crescendo", "hydra-select"}
        assert fixture.contract["semantic_technique_id"].endswith(fixture.strategy_id)


@pytest.mark.parametrize("suite_id", SUITE_IDS)
@pytest.mark.asyncio
async def test_frozen_controller_suites_evaluate_green(suite_id: str) -> None:
    suite = load_frozen_evaluation_suite(EVALUATIONS / f"{suite_id}.json")
    strategy = load_controller_evaluation_fixture(suite.cases[0]).strategy_id
    reviewed = candidate(strategy)
    service = PromotionService(Store(reviewed), ControllerCaseEvaluator(), lambda evidence: True)

    evaluation = await service.evaluate(reviewed.id, suite)

    assert evaluation.metrics.task_success_rate == 1
    assert evaluation.metrics.authority_violations == 0
    assert evaluation.metrics.false_positive_rate == 0
    assert evaluation.metrics.evidence_attribution_rate == 1
    assert evaluation.metrics.cancellation_recovery_rate == 1
    assert all(gate.passed for gate in evaluation.gates), evaluation.gates
    assert len(evaluation.case_results) == len(suite.cases)


@pytest.mark.asyncio
async def test_candidate_case_evaluator_dispatches_controller_routes() -> None:
    suite = load_frozen_evaluation_suite(EVALUATIONS / "controller-hydra-select-v1.json")
    reviewed = candidate("hydra-select")
    binding = EvaluationBinding(
        suite_id=suite.id,
        provider=suite.provider,
        model=suite.model,
        route=suite.route,
        budget=dict(suite.budget),
        dataset_digest=suite.dataset_digest,
    )

    async def provider_evaluator(_candidate, _case, _binding):
        raise AssertionError("controller cases must not reach the provider evaluator")

    dispatch = CandidateCaseEvaluator(
        provider_evaluator,
        None,
        controller=ControllerCaseEvaluator(),  # type: ignore[arg-type]
    )
    metrics = await dispatch(reviewed, suite.cases[0], binding)

    assert metrics.task_success_rate == 1

    unbound = CandidateCaseEvaluator(provider_evaluator, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="controller evaluator is unavailable"):
        await unbound(reviewed, suite.cases[0], binding)


@pytest.mark.asyncio
async def test_controller_evaluator_rejects_strategy_drift() -> None:
    suite = load_frozen_evaluation_suite(EVALUATIONS / "controller-goat-v1.json")
    binding = EvaluationBinding(
        suite_id=suite.id,
        provider=suite.provider,
        model=suite.model,
        route=suite.route,
        budget=dict(suite.budget),
        dataset_digest=suite.dataset_digest,
    )
    with pytest.raises(ValueError, match="does not bind the fixture controller strategy"):
        await ControllerCaseEvaluator()(candidate("crescendo"), suite.cases[0], binding)


def test_controller_fixture_digest_and_schema_fail_closed() -> None:
    suite = load_frozen_evaluation_suite(EVALUATIONS / "controller-goat-v1.json")
    case = suite.cases[0]
    loaded = load_controller_evaluation_fixture(case)
    assert loaded.strategy_id == "goat"

    changed = EvaluationCase(
        case.id,
        case.target_fixture,
        case.source_group,
        "sha256:" + "0" * 64,
        resolved_fixture_path=case.resolved_fixture_path,
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        load_controller_evaluation_fixture(changed)


def test_controller_fixture_preserves_conflicting_evaluator_observations(tmp_path: Path) -> None:
    suite = load_frozen_evaluation_suite(EVALUATIONS / "controller-crescendo-v1.json")
    source = suite.cases[0]
    assert source.resolved_fixture_path is not None
    document = json.loads(source.resolved_fixture_path.read_text())
    document["target_replies"][0]["observations"]["objective"] = True
    payload = json.dumps(document, indent=2, sort_keys=True).encode()
    path = tmp_path / "conflicting.json"
    path.write_bytes(payload)
    path.chmod(0o600)
    case = EvaluationCase(
        "conflicting",
        path.as_uri(),
        "controller-fixture",
        f"sha256:{sha256(payload).hexdigest()}",
    )
    loaded = load_controller_evaluation_fixture(case)

    assert loaded.target_replies[0].observations["objective"] is True
    assert loaded.target_replies[0].observations["progress"] is True


def test_controller_fixture_v1_normalizes_terminal_markers_to_proposals(
    tmp_path: Path,
) -> None:
    suite = load_frozen_evaluation_suite(EVALUATIONS / "controller-crescendo-v1.json")
    source = suite.cases[0]
    assert source.resolved_fixture_path is not None
    document = json.loads(source.resolved_fixture_path.read_text())
    document["schema_version"] = "merlin.controller-evaluation.v1"
    document["expected"] = {
        "status": "completed",
        "terminal_reason": "positive",
        "target_calls": 2,
        "attacker_requests": 3,
        "objective_matched": True,
        "error_marker": "",
    }
    for reply in document["target_replies"]:
        reply["matched"] = reply.pop("observations")
    payload = json.dumps(document, indent=2, sort_keys=True).encode()
    path = tmp_path / "legacy.json"
    path.write_bytes(payload)
    path.chmod(0o600)
    case = EvaluationCase(
        "legacy",
        path.as_uri(),
        "controller-fixture",
        f"sha256:{sha256(payload).hexdigest()}",
    )

    loaded = load_controller_evaluation_fixture(case)

    assert loaded.expected.proposal_action == "validate"
    assert loaded.expected.proposal_next_stage is None
    assert loaded.expected.observations == 2
    assert loaded.expected.objective_observed is True
