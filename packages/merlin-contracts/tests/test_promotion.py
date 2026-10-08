import pytest
from merlin_contracts import (
    EvaluationMetrics,
    EvaluationPolicy,
    PromotionCandidate,
    deterministic_gates,
    promotion_digest,
)
from pydantic import ValidationError


def candidate(*, graph_local: bool = False) -> PromotionCandidate:
    content = {"profile_id": "authorization", "instructions": "Test object boundaries."}
    return PromotionCandidate(
        id="candidate-9",
        kind="profile",
        version="9.0.0",
        baseline_version="8.0.0",
        baseline_digest="sha256:baseline-eight",
        content=content,
        content_digest=promotion_digest(content),
        proposed_by="agent:lead",
        profile_id="authorization",
        profile_operation="add",
        graph_local_composition=graph_local,
    )


def metrics(**changes: object) -> EvaluationMetrics:
    values = {
        "task_success_rate": 0.9,
        "false_positive_rate": 0.05,
        "evidence_attribution_rate": 1.0,
        "authority_violations": 0,
        "mean_cost": 1.0,
        "p95_latency_ms": 100,
        "cancellation_recovery_rate": 1.0,
    }
    values.update(changes)
    return EvaluationMetrics(**values)  # type: ignore[arg-type]


def test_ninth_profile_digest_and_deterministic_gates() -> None:
    gates = deterministic_gates(
        candidate(),
        metrics(),
        metrics(task_success_rate=0.8),
        EvaluationPolicy(),
        held_out=True,
        source_disjoint=True,
    )
    assert all(gate.passed for gate in gates)
    assert {gate.name for gate in gates} >= {"authority", "cost", "latency", "held_out"}


def test_optional_token_efficiency_gate_uses_aggregate_usage() -> None:
    gates = deterministic_gates(
        candidate(),
        metrics(total_tokens=150),
        metrics(total_tokens=100),
        EvaluationPolicy(maximum_token_ratio=1.25),
        held_out=True,
        source_disjoint=True,
    )

    token_gate = next(gate for gate in gates if gate.name == "tokens")
    assert token_gate.passed is False
    assert token_gate.observed == 1.5


def test_graph_local_composition_is_never_permanently_promotable() -> None:
    graph_local = candidate(graph_local=True)
    gates = deterministic_gates(
        graph_local,
        metrics(),
        metrics(task_success_rate=0.8),
        EvaluationPolicy(),
        held_out=True,
        source_disjoint=True,
    )
    assert not graph_local.permanently_promotable
    assert next(gate for gate in gates if gate.name == "permanent_candidate").passed is False


def test_candidate_content_is_digest_bound() -> None:
    values = candidate().model_dump()
    values["content_digest"] = "sha256:forged"
    with pytest.raises(ValidationError, match="digest"):
        PromotionCandidate(**values)


def test_adapter_candidate_requires_identity_and_supported_interface() -> None:
    content: dict[str, object] = {
        "adapter_id": "target-api-v1",
        "interface": "http_api",
    }
    adapter = PromotionCandidate(
        id="candidate-adapter",
        kind="adapter",
        version="1",
        baseline_version="0",
        baseline_digest="sha256:baseline",
        content=content,
        content_digest=promotion_digest(content),
        proposed_by="operator:test",
    )
    assert adapter.kind == "adapter"

    invalid = dict(content)
    invalid["interface"] = "unknown"
    with pytest.raises(ValidationError, match="supported interface"):
        PromotionCandidate(
            id="candidate-invalid-adapter",
            kind="adapter",
            version="1",
            baseline_version="0",
            baseline_digest="sha256:baseline",
            content=invalid,
            content_digest=promotion_digest(invalid),
            proposed_by="operator:test",
        )
