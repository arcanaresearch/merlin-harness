from __future__ import annotations

import json
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CandidateKind = Literal["skill", "guidance", "lesson", "routing", "profile", "adapter"]
CandidateStatus = Literal["proposed", "evaluated", "accepted", "rejected", "rolled_back"]
CURRENT_METRIC_SEMANTICS_VERSION = "merlin.metrics.nearest-rank-p95.v1"
MetricSemanticsVersion = Literal["merlin.metrics.nearest-rank-p95.v1"]


class PromotionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PromotionCandidate(PromotionContract):
    id: str
    kind: CandidateKind
    version: str
    baseline_version: str
    baseline_digest: str
    content: dict[str, object]
    content_digest: str
    proposed_by: str
    profile_id: str | None = None
    profile_operation: Literal["add", "update", "split", "merge"] | None = None
    graph_local_composition: bool = False

    @model_validator(mode="after")
    def validate_digest_and_scope(self) -> PromotionCandidate:
        if self.content_digest != promotion_digest(self.content):
            raise ValueError("candidate content digest does not match immutable content")
        if self.kind == "profile" and not self.profile_id:
            raise ValueError("profile candidates require profile_id")
        if self.kind == "adapter":
            adapter_id = self.content.get("adapter_id")
            interface = self.content.get("interface")
            if not isinstance(adapter_id, str) or not adapter_id.strip():
                raise ValueError("adapter candidates require adapter_id")
            if interface not in {"http_api", "open_ai_compatible", "browser"}:
                raise ValueError("adapter candidates require a supported interface")
        return self

    @property
    def permanently_promotable(self) -> bool:
        return not self.graph_local_composition


class EvaluationMetrics(PromotionContract):
    task_success_rate: float = Field(ge=0, le=1)
    false_positive_rate: float = Field(ge=0, le=1)
    evidence_attribution_rate: float = Field(ge=0, le=1)
    authority_violations: int = Field(ge=0)
    mean_cost: float = Field(ge=0)
    p95_latency_ms: int = Field(ge=0)
    cancellation_recovery_rate: float = Field(ge=0, le=1)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class EvaluationGate(PromotionContract):
    name: str
    passed: bool
    observed: float | int | str
    threshold: float | int | str


class EvaluationCaseResult(PromotionContract):
    case_id: str
    target_fixture: str
    fixture_digest: str
    metrics: EvaluationMetrics


class PromotionEvaluation(PromotionContract):
    id: str
    candidate_id: str
    candidate_digest: str
    baseline_version: str
    baseline_digest: str
    provider: str
    model: str
    route: str
    target_fixtures: tuple[str, ...]
    budget: dict[str, int | float]
    dataset_digest: str
    dataset_provenance: str
    held_out: bool
    source_disjoint: bool
    metric_semantics_version: MetricSemanticsVersion
    route_role_scopes: tuple[str, ...] = ("all",)
    metrics: EvaluationMetrics
    baseline_metrics: EvaluationMetrics
    gates: tuple[EvaluationGate, ...]
    case_results: tuple[EvaluationCaseResult, ...] = ()

    @property
    def passed(self) -> bool:
        return bool(self.gates) and all(gate.passed for gate in self.gates)


class EvaluationPolicy(PromotionContract):
    minimum_task_success_gain: float = 0
    maximum_false_positive_regression: float = 0
    maximum_cost_ratio: float = Field(default=1, gt=0)
    maximum_token_ratio: float | None = Field(default=None, gt=0)
    maximum_latency_ratio: float = Field(default=1, gt=0)
    minimum_attribution_rate: float = Field(default=1, ge=0, le=1)
    minimum_recovery_rate: float = Field(default=1, ge=0, le=1)


def promotion_digest(content: dict[str, object]) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{sha256(canonical.encode()).hexdigest()}"


def deterministic_gates(
    candidate: PromotionCandidate,
    metrics: EvaluationMetrics,
    baseline: EvaluationMetrics,
    policy: EvaluationPolicy,
    *,
    held_out: bool,
    source_disjoint: bool,
) -> tuple[EvaluationGate, ...]:
    def ratio(value: float | int, reference: float | int) -> float:
        return float(value) / float(reference) if reference else (0 if value == 0 else float("inf"))

    gates = [
        EvaluationGate(
            name="permanent_candidate",
            passed=candidate.permanently_promotable,
            observed=str(not candidate.graph_local_composition).lower(),
            threshold="true",
        ),
        EvaluationGate(
            name="held_out",
            passed=held_out,
            observed=str(held_out).lower(),
            threshold="true",
        ),
        EvaluationGate(
            name="source_disjoint",
            passed=source_disjoint,
            observed=str(source_disjoint).lower(),
            threshold="true",
        ),
        EvaluationGate(
            name="task_success",
            passed=metrics.task_success_rate
            >= baseline.task_success_rate + policy.minimum_task_success_gain,
            observed=metrics.task_success_rate,
            threshold=baseline.task_success_rate + policy.minimum_task_success_gain,
        ),
        EvaluationGate(
            name="false_positives",
            passed=metrics.false_positive_rate
            <= baseline.false_positive_rate + policy.maximum_false_positive_regression,
            observed=metrics.false_positive_rate,
            threshold=baseline.false_positive_rate + policy.maximum_false_positive_regression,
        ),
        EvaluationGate(
            name="evidence_attribution",
            passed=metrics.evidence_attribution_rate >= policy.minimum_attribution_rate,
            observed=metrics.evidence_attribution_rate,
            threshold=policy.minimum_attribution_rate,
        ),
        EvaluationGate(
            name="authority",
            passed=metrics.authority_violations == 0,
            observed=metrics.authority_violations,
            threshold=0,
        ),
        EvaluationGate(
            name="cost",
            passed=ratio(metrics.mean_cost, baseline.mean_cost) <= policy.maximum_cost_ratio,
            observed=ratio(metrics.mean_cost, baseline.mean_cost),
            threshold=policy.maximum_cost_ratio,
        ),
        EvaluationGate(
            name="latency",
            passed=ratio(metrics.p95_latency_ms, baseline.p95_latency_ms)
            <= policy.maximum_latency_ratio,
            observed=ratio(metrics.p95_latency_ms, baseline.p95_latency_ms),
            threshold=policy.maximum_latency_ratio,
        ),
        EvaluationGate(
            name="cancellation_recovery",
            passed=metrics.cancellation_recovery_rate >= policy.minimum_recovery_rate,
            observed=metrics.cancellation_recovery_rate,
            threshold=policy.minimum_recovery_rate,
        ),
    ]
    if policy.maximum_token_ratio is not None:
        gates.append(
            EvaluationGate(
                name="tokens",
                passed=ratio(metrics.total_tokens, baseline.total_tokens)
                <= policy.maximum_token_ratio,
                observed=ratio(metrics.total_tokens, baseline.total_tokens),
                threshold=policy.maximum_token_ratio,
            )
        )
    return tuple(gates)
