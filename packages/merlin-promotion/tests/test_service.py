import json

import pytest
from merlin_contracts import (
    CandidateKind,
    EvaluationMetrics,
    EvaluationPolicy,
    PromotionCandidate,
    promotion_digest,
)
from merlin_contracts.promotion import CURRENT_METRIC_SEMANTICS_VERSION
from merlin_promotion import (
    CandidateProposal,
    CommittedSwarmEvidence,
    EvaluationCase,
    FrozenEvaluationSuite,
    KernelPromotionService,
    PromotionService,
    aggregate_evaluation_metrics,
    frozen_dataset_digest,
    load_frozen_evaluation_suite,
)


def test_aggregate_evaluation_metrics_computes_nearest_rank_p95() -> None:
    observations = [
        metrics().model_copy(update={"p95_latency_ms": latency}) for latency in range(1, 21)
    ]

    aggregate = aggregate_evaluation_metrics(observations)

    assert aggregate.p95_latency_ms == 19


def metrics(success: float = 0.9, *, tokens: int = 0, cost: float = 1) -> EvaluationMetrics:
    return EvaluationMetrics(
        task_success_rate=success,
        false_positive_rate=0.05,
        evidence_attribution_rate=1,
        authority_violations=0,
        mean_cost=cost,
        p95_latency_ms=100,
        cancellation_recovery_rate=1,
        input_tokens=tokens,
        output_tokens=tokens,
        total_tokens=tokens * 2,
    )


class Store:
    def __init__(self, active=None):
        self.candidates = {active.id: active} if active else {}
        self.active_candidate = active
        self.evaluations_by_candidate = {}
        self.decisions = {}
        self.history = []

    def propose(self, candidate):
        self.candidates[candidate.id] = candidate
        return candidate

    def get(self, candidate_id):
        return self.candidates.get(candidate_id)

    def record_evaluation(self, evaluation):
        self.evaluations_by_candidate.setdefault(evaluation.candidate_id, []).append(evaluation)
        return evaluation

    def operator_decide(self, candidate_id, decision, **kwargs):
        assert kwargs["actor_kind"] == "operator"
        self.decisions[candidate_id] = decision

    def activate(self, candidate_id, *, operator_id):
        evaluation = self.evaluations_by_candidate[candidate_id][-1]
        if not evaluation.passed or self.decisions.get(candidate_id) != "accepted":
            raise ValueError("candidate requires passing evaluation and operator approval")
        self.history.append(self.active_candidate)
        self.active_candidate = self.candidates[candidate_id]
        return len(self.history)

    def rollback(self, kind, *, operator_id):
        self.active_candidate = self.history.pop()
        return len(self.history) + 1

    def active(self, kind):
        return self.active_candidate

    def evaluations(self, candidate_id):
        value = self.evaluations_by_candidate.get(candidate_id, [])
        return list(value)

    def status(self, candidate_id):
        return self.decisions.get(candidate_id, "proposed")


def baseline_candidate() -> PromotionCandidate:
    content = {"profile_id": "explorer", "instructions": "baseline"}
    return PromotionCandidate(
        id="baseline-eight",
        kind="profile",
        version="8.0.0",
        baseline_version="7.0.0",
        baseline_digest="sha256:seven",
        content=content,
        content_digest=promotion_digest(content),
        proposed_by="operator:review",
        profile_id="explorer",
        profile_operation="update",
    )


def evidence(committed=True) -> CommittedSwarmEvidence:
    return CommittedSwarmEvidence(
        schedule_id="schedule-1",
        node_id="node-1",
        attempt_id="attempt-1",
        result_digest="sha256:committed",
        evidence_references=("evidence://result/1",),
        agent_id="lead-1",
        committed=committed,
    )


def proposal(candidate_id="ninth") -> CandidateProposal:
    return CandidateProposal(
        id=candidate_id,
        kind="profile",
        version="9.0.0",
        baseline_version="8.0.0",
        baseline_digest="sha256:eight",
        content={"profile_id": "authorization", "instructions": "check object boundaries"},
        profile_id="authorization",
        profile_operation="add",
    )


def routing_proposal(
    candidate_id="route-strong", *, role_scopes: tuple[str, ...] = ("all",)
) -> CandidateProposal:
    return CandidateProposal(
        id=candidate_id,
        kind="routing",
        version="1.0.0",
        baseline_version="0.9.0",
        baseline_digest="sha256:route-baseline",
        content={
            "provider": "openai-compatible",
            "model": "frozen-model",
            "route": "evaluation-only",
            "role_scopes": list(role_scopes),
        },
    )


def suite(
    candidate_kind: CandidateKind = "profile",
    *,
    baseline_metrics: EvaluationMetrics | None = None,
    policy: EvaluationPolicy | None = None,
    route_role_scopes: tuple[str, ...] = ("all",),
) -> FrozenEvaluationSuite:
    cases = (
        EvaluationCase("held-1", "fixture://authz/1", "heldout-a", "sha256:" + "1" * 64),
        EvaluationCase("held-2", "fixture://authz/2", "heldout-b", "sha256:" + "2" * 64),
    )
    return FrozenEvaluationSuite(
        id="frozen-authz-v1",
        candidate_kind=candidate_kind,
        cases=cases,
        provider="openai-compatible",
        model="frozen-model",
        route="evaluation-only",
        budget={"turns": 4, "tokens": 8000},
        dataset_provenance="operator-reviewed-v1",
        dataset_digest=frozen_dataset_digest(cases),
        held_out=True,
        source_disjoint=True,
        route_role_scopes=route_role_scopes,
        baseline_metrics=baseline_metrics or metrics(0.8),
        policy=policy or EvaluationPolicy(),
    )


def test_operator_suite_loader_validates_file_and_dataset_integrity(tmp_path) -> None:
    expected = suite()
    document = {
        "id": expected.id,
        "candidate_kind": expected.candidate_kind,
        "cases": [
            {
                "id": case.id,
                "target_fixture": case.target_fixture,
                "source_group": case.source_group,
                "fixture_digest": case.fixture_digest,
            }
            for case in expected.cases
        ],
        "provider": expected.provider,
        "model": expected.model,
        "route": expected.route,
        "budget": dict(expected.budget),
        "dataset_provenance": expected.dataset_provenance,
        "dataset_digest": expected.dataset_digest,
        "held_out": expected.held_out,
        "source_disjoint": expected.source_disjoint,
        "route_role_scopes": list(expected.route_role_scopes),
        "baseline_metrics": expected.baseline_metrics.model_dump(mode="json"),
        "policy": expected.policy.model_dump(mode="json"),
    }
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)

    loaded = load_frozen_evaluation_suite(path)
    assert loaded == expected

    document["dataset_digest"] = "sha256:tampered"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="dataset digest mismatch"):
        load_frozen_evaluation_suite(path)

    link = tmp_path / "suite-link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        load_frozen_evaluation_suite(link)

    document["dataset_digest"] = expected.dataset_digest
    document["route_role_scopes"] = ["all", "lead"]
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="route role scopes"):
        load_frozen_evaluation_suite(path)


def test_operator_suite_loader_resolves_relative_fixture_within_suite_directory(
    tmp_path,
) -> None:
    fixture = tmp_path / "case.json"
    fixture.write_text("{}", encoding="utf-8")
    fixture.chmod(0o600)
    case = EvaluationCase("case", "case.json", "group", "sha256:" + "1" * 64)
    expected = suite()
    document = {
        "id": expected.id,
        "candidate_kind": expected.candidate_kind,
        "cases": [
            {
                "id": case.id,
                "target_fixture": case.target_fixture,
                "source_group": case.source_group,
                "fixture_digest": case.fixture_digest,
            }
        ],
        "provider": expected.provider,
        "model": expected.model,
        "route": expected.route,
        "budget": dict(expected.budget),
        "dataset_provenance": expected.dataset_provenance,
        "dataset_digest": frozen_dataset_digest((case,)),
        "held_out": expected.held_out,
        "source_disjoint": expected.source_disjoint,
        "route_role_scopes": list(expected.route_role_scopes),
        "baseline_metrics": expected.baseline_metrics.model_dump(mode="json"),
        "policy": expected.policy.model_dump(mode="json"),
    }
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)

    loaded = load_frozen_evaluation_suite(path)

    assert loaded.cases[0].target_fixture == "case.json"
    assert loaded.cases[0].resolved_fixture_path == fixture

    document["cases"][0]["target_fixture"] = "../outside.json"
    escaped = EvaluationCase("case", "../outside.json", "group", "sha256:" + "1" * 64)
    document["dataset_digest"] = frozen_dataset_digest((escaped,))
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="stay within"):
        load_frozen_evaluation_suite(path)


def test_dataset_identity_binds_fixture_content_digest() -> None:
    first = EvaluationCase("case", "fixture://one", "group", "sha256:" + "1" * 64)
    changed = EvaluationCase("case", "fixture://one", "group", "sha256:" + "2" * 64)
    assert frozen_dataset_digest((first,)) != frozen_dataset_digest((changed,))

    with pytest.raises(ValueError, match="fixture digests"):
        FrozenEvaluationSuite(
            id="invalid",
            candidate_kind="profile",
            cases=(EvaluationCase("case", "fixture://one", "group", "sha256:not-hex"),),
            provider="provider",
            model="model",
            route="route",
            budget={},
            dataset_provenance="operator",
            dataset_digest="sha256:irrelevant",
            held_out=True,
            source_disjoint=True,
            baseline_metrics=metrics(),
        )


@pytest.mark.asyncio
async def test_passing_ninth_profile_affects_only_new_work() -> None:
    store = Store(baseline_candidate())

    async def evaluator(candidate, case, binding):
        assert binding.model == "frozen-model"
        assert binding.dataset_digest == suite().dataset_digest
        return metrics(0.9, tokens=7)

    service = PromotionService(store, evaluator, lambda item: item.committed)
    old_work = service.snapshot_for_new_work("profile")
    candidate = service.propose_from_evidence(proposal(), evidence())
    assert candidate.content["proposal_evidence"]["result_digest"] == "sha256:committed"
    evaluation = await service.evaluate(candidate.id, suite())
    assert evaluation.passed
    assert evaluation.metrics.input_tokens == 14
    assert evaluation.metrics.output_tokens == 14
    assert evaluation.metrics.total_tokens == 28
    assert evaluation.metric_semantics_version == CURRENT_METRIC_SEMANTICS_VERSION
    service.accept(candidate.id, operator_id="operator:lucas", reason="reviewed passing suite")
    service.activate(candidate.id, operator_id="operator:lucas")
    new_work = service.snapshot_for_new_work("profile")

    assert old_work.candidate.id == "baseline-eight"
    assert new_work.candidate.id == "ninth"
    assert old_work.candidate.id == "baseline-eight"
    service.rollback("profile", operator_id="operator:lucas")
    assert service.snapshot_for_new_work("profile").candidate.id == "baseline-eight"


@pytest.mark.asyncio
async def test_regressing_candidate_is_rejected_and_model_cannot_approve() -> None:
    store = Store(baseline_candidate())

    async def evaluator(candidate, case, binding):
        return metrics(0.5)

    service = PromotionService(store, evaluator, lambda item: item.committed)
    candidate = service.propose_from_evidence(proposal("regressing"), evidence())
    evaluation = await service.evaluate(candidate.id, suite())
    assert not evaluation.passed
    with pytest.raises(PermissionError, match="model paths"):
        service.model_decide(candidate.id, "accepted")
    with pytest.raises(PermissionError, match="operator identity"):
        service.accept(candidate.id, operator_id="agent:lead", reason="self approve")
    service.reject(candidate.id, operator_id="operator:lucas", reason="task regression")
    with pytest.raises(ValueError, match="passing evaluation"):
        service.activate(candidate.id, operator_id="operator:lucas")


def test_uncommitted_or_unattributed_proposals_are_refused() -> None:
    async def evaluator(candidate, case, binding):
        return metrics()

    service = PromotionService(Store(), evaluator, lambda item: item.committed)
    with pytest.raises(ValueError, match="committed"):
        service.propose_from_evidence(proposal(), evidence(committed=False))
    with pytest.raises(ValueError, match="attributable"):
        service.propose_from_evidence(
            proposal(),
            CommittedSwarmEvidence(
                schedule_id="s",
                node_id="n",
                attempt_id="a",
                result_digest="sha256:r",
                evidence_references=(),
                agent_id="lead",
            ),
        )


@pytest.mark.asyncio
async def test_evaluation_rejects_wrong_candidate_domain_before_executor() -> None:
    store = Store()
    called = False

    async def evaluator(candidate, case, binding):
        nonlocal called
        called = True
        return metrics()

    service = PromotionService(store, evaluator, lambda item: item.committed)
    candidate = service.propose_from_evidence(routing_proposal(), evidence())
    with pytest.raises(ValueError, match="candidate kind"):
        await service.evaluate(candidate.id, suite("adapter"))
    assert called is False


@pytest.mark.asyncio
async def test_kernel_selects_first_suite_for_exact_candidate_domain() -> None:
    store = Store()

    async def evaluator(candidate, case, binding):
        assert candidate.kind == "routing"
        assert binding.suite_id == suite("routing").id
        assert binding.session_id == "session-test"
        return metrics()

    service = PromotionService(store, evaluator, lambda item: item.committed)
    candidate = service.propose_from_evidence(routing_proposal(), evidence())
    kernel = KernelPromotionService(
        service,
        store,
        {
            suite("profile").id: suite("profile"),
            suite("routing").id: suite("routing"),
        },
        operator_id="operator:test",
        evaluation_authorizer=lambda session_id, candidate, selected: _allow_evaluation(
            session_id, candidate, selected
        ),
    )
    result = await kernel.evaluate("session-test", candidate.id, None)
    assert result["candidate_id"] == candidate.id


@pytest.mark.asyncio
async def test_kernel_refuses_network_evaluation_without_session_authority() -> None:
    store = Store()
    called = False

    async def evaluator(candidate, case, binding):
        nonlocal called
        called = True
        return metrics()

    service = PromotionService(store, evaluator, lambda item: item.committed)
    candidate = service.propose_from_evidence(routing_proposal(), evidence())
    kernel = KernelPromotionService(
        service,
        store,
        {suite("routing").id: suite("routing")},
        operator_id="operator:test",
    )
    with pytest.raises(PermissionError, match="session-bound authorizer"):
        await kernel.evaluate("session-test", candidate.id, None)
    assert called is False


async def _allow_evaluation(session_id, candidate, suite):
    del candidate, suite
    return session_id == "session-test"
