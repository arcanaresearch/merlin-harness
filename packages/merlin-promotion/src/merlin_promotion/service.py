from __future__ import annotations

import json
import os
import stat
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol
from uuid import uuid4

from merlin_contracts import (
    CandidateKind,
    EvaluationCaseResult,
    EvaluationMetrics,
    EvaluationPolicy,
    PromotionCandidate,
    PromotionEvaluation,
    deterministic_gates,
    promotion_digest,
)
from merlin_contracts.promotion import CURRENT_METRIC_SEMANTICS_VERSION

ROUTE_ROLE_SCOPES = frozenset({"all", "lead", "recon", "routine", "validator"})


@dataclass(frozen=True, slots=True)
class CommittedSwarmEvidence:
    schedule_id: str
    node_id: str
    attempt_id: str
    result_digest: str
    evidence_references: tuple[str, ...]
    agent_id: str
    committed: bool = True


@dataclass(frozen=True, slots=True)
class CandidateProposal:
    id: str
    kind: CandidateKind
    version: str
    baseline_version: str
    baseline_digest: str
    content: dict[str, object]
    profile_id: str | None = None
    profile_operation: Literal["add", "update", "split", "merge"] | None = None
    graph_local_composition: bool = False


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    id: str
    target_fixture: str
    source_group: str
    fixture_digest: str
    resolved_fixture_path: Path | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class FrozenEvaluationSuite:
    id: str
    candidate_kind: CandidateKind
    cases: tuple[EvaluationCase, ...]
    provider: str
    model: str
    route: str
    budget: Mapping[str, int | float]
    dataset_provenance: str
    dataset_digest: str
    held_out: bool
    source_disjoint: bool
    baseline_metrics: EvaluationMetrics
    route_role_scopes: tuple[str, ...] = ("all",)
    policy: EvaluationPolicy = EvaluationPolicy()

    def __post_init__(self) -> None:
        if not self.cases or not self.id.strip():
            raise ValueError("frozen evaluation suite requires cases and an id")
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("frozen evaluation suite case ids must be unique")
        if any(not _is_sha256_digest(case.fixture_digest) for case in self.cases):
            raise ValueError("frozen evaluation suite cases require SHA-256 fixture digests")
        content = [
            {
                "id": case.id,
                "target_fixture": case.target_fixture,
                "source_group": case.source_group,
                "fixture_digest": case.fixture_digest,
            }
            for case in self.cases
        ]
        actual = f"sha256:{sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()}"
        if actual != self.dataset_digest:
            raise ValueError("evaluation suite dataset digest mismatch")
        _validate_route_role_scopes(self.route_role_scopes)
        object.__setattr__(self, "budget", MappingProxyType(dict(self.budget)))


def load_frozen_evaluation_suite(
    path: Path, *, max_bytes: int = 1_048_576
) -> FrozenEvaluationSuite:
    """Load one operator-installed suite without trusting mutable or oversized input."""

    if path.is_symlink():
        raise ValueError("evaluation suite file must not be a symlink")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise ValueError("evaluation suite file is unavailable") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("evaluation suite path must be a regular file")
        if details.st_uid != os.getuid():
            raise PermissionError("evaluation suite file must be owned by the current user")
        if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError("evaluation suite file must not be group/world writable")
        if details.st_size > max_bytes:
            raise ValueError("evaluation suite file exceeds the configured size bound")
        chunks = bytearray()
        while len(chunks) <= max_bytes:
            chunk = os.read(descriptor, min(65_536, max_bytes + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        payload = bytes(chunks)
        if len(payload) > max_bytes:
            raise ValueError("evaluation suite file exceeds the configured size bound")
    finally:
        os.close(descriptor)
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("evaluation suite file is not valid UTF-8 JSON") from error
    if not isinstance(raw, dict):
        raise TypeError("evaluation suite document must be an object")
    expected = {
        "id",
        "candidate_kind",
        "cases",
        "provider",
        "model",
        "route",
        "budget",
        "dataset_provenance",
        "dataset_digest",
        "held_out",
        "source_disjoint",
        "baseline_metrics",
        "policy",
    }
    if set(raw) not in {frozenset(expected), frozenset((*expected, "route_role_scopes"))}:
        raise ValueError("evaluation suite document has an unexpected schema")
    cases_raw = raw["cases"]
    if not isinstance(cases_raw, list):
        raise TypeError("evaluation suite cases must be an array")
    cases: list[EvaluationCase] = []
    for item in cases_raw:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "target_fixture",
            "source_group",
            "fixture_digest",
        }:
            raise ValueError("evaluation suite case has an unexpected schema")
        if any(not isinstance(item[key], str) or not item[key].strip() for key in item):
            raise TypeError("evaluation suite case values must be non-empty strings")
        target_fixture = item["target_fixture"]
        resolved_fixture_path: Path | None = None
        if ":" not in target_fixture:
            relative_fixture = Path(target_fixture)
            if relative_fixture.is_absolute():
                raise ValueError("evaluation suite relative fixture path must not be absolute")
            suite_root = path.parent.resolve()
            resolved_fixture_path = Path(os.path.abspath(suite_root / relative_fixture))
            try:
                resolved_fixture_path.relative_to(suite_root)
            except ValueError as error:
                raise ValueError(
                    "evaluation suite relative fixture path must stay within the suite directory"
                ) from error
        cases.append(EvaluationCase(**item, resolved_fixture_path=resolved_fixture_path))
    budget = raw["budget"]
    if not isinstance(budget, dict) or any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
        for value in budget.values()
    ):
        raise TypeError("evaluation suite budget must contain non-negative numeric values")
    if not isinstance(raw["held_out"], bool) or not isinstance(raw["source_disjoint"], bool):
        raise TypeError("evaluation suite held_out/source_disjoint values must be booleans")
    role_scopes = raw.get("route_role_scopes", ["all"])
    if not isinstance(role_scopes, list):
        raise TypeError("evaluation suite route_role_scopes must be a string array")
    _validate_route_role_scopes(tuple(role_scopes))
    for key in ("id", "provider", "model", "route", "dataset_provenance", "dataset_digest"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise TypeError(f"evaluation suite {key} must be a non-empty string")
    if raw["candidate_kind"] not in {
        "skill",
        "guidance",
        "lesson",
        "routing",
        "profile",
        "adapter",
    }:
        raise ValueError("evaluation suite candidate_kind is unsupported")
    return FrozenEvaluationSuite(
        id=raw["id"],
        candidate_kind=raw["candidate_kind"],
        cases=tuple(cases),
        provider=raw["provider"],
        model=raw["model"],
        route=raw["route"],
        budget=budget,
        dataset_provenance=raw["dataset_provenance"],
        dataset_digest=raw["dataset_digest"],
        held_out=raw["held_out"],
        source_disjoint=raw["source_disjoint"],
        baseline_metrics=EvaluationMetrics.model_validate(raw["baseline_metrics"]),
        route_role_scopes=tuple(role_scopes),
        policy=EvaluationPolicy.model_validate(raw["policy"]),
    )


@dataclass(frozen=True, slots=True)
class EvaluationBinding:
    suite_id: str
    provider: str
    model: str
    route: str
    budget: Mapping[str, int | float]
    dataset_digest: str
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class ActivePromotionSnapshot:
    kind: CandidateKind
    candidate: PromotionCandidate | None


class PromotionStore(Protocol):
    def propose(self, candidate: PromotionCandidate) -> PromotionCandidate: ...
    def record_evaluation(self, evaluation: PromotionEvaluation) -> PromotionEvaluation: ...
    def operator_decide(
        self,
        candidate_id: str,
        decision: str,
        *,
        operator_id: str,
        reason: str,
        actor_kind: str = "operator",
    ) -> None: ...
    def activate(self, candidate_id: str, *, operator_id: str) -> int: ...
    def rollback(self, kind: str, *, operator_id: str) -> int: ...
    def active(self, kind: str) -> PromotionCandidate | None: ...
    def get(self, candidate_id: str) -> PromotionCandidate | None: ...
    def list_candidates(self, *, limit: int = 100) -> list[PromotionCandidate]: ...
    def evaluations(self, candidate_id: str) -> list[PromotionEvaluation]: ...
    def history(self, kind: str) -> list[dict[str, object]]: ...
    def status(self, candidate_id: str) -> str: ...


CaseEvaluator = Callable[
    [PromotionCandidate, EvaluationCase, EvaluationBinding], Awaitable[EvaluationMetrics]
]
EvidenceVerifier = Callable[[CommittedSwarmEvidence], bool]


class PromotionService:
    def __init__(
        self,
        store: PromotionStore,
        evaluator: CaseEvaluator,
        evidence_verifier: EvidenceVerifier,
    ) -> None:
        self._store = store
        self._evaluator = evaluator
        self._evidence_verifier = evidence_verifier

    def propose_from_evidence(
        self, proposal: CandidateProposal, evidence: CommittedSwarmEvidence
    ) -> PromotionCandidate:
        if (
            not evidence.committed
            or not evidence.result_digest.strip()
            or not self._evidence_verifier(evidence)
        ):
            raise ValueError("candidate proposals require committed swarm evidence")
        if not evidence.evidence_references or any(
            not reference.strip() for reference in evidence.evidence_references
        ):
            raise ValueError("candidate proposals require attributable evidence references")
        content = dict(proposal.content)
        content["proposal_evidence"] = {
            "schedule_id": evidence.schedule_id,
            "node_id": evidence.node_id,
            "attempt_id": evidence.attempt_id,
            "result_digest": evidence.result_digest,
            "references": list(evidence.evidence_references),
        }
        candidate = PromotionCandidate(
            id=proposal.id,
            kind=proposal.kind,
            version=proposal.version,
            baseline_version=proposal.baseline_version,
            baseline_digest=proposal.baseline_digest,
            content=content,
            content_digest=promotion_digest(content),
            proposed_by=f"agent:{evidence.agent_id}",
            profile_id=proposal.profile_id,
            profile_operation=proposal.profile_operation,
            graph_local_composition=proposal.graph_local_composition,
        )
        return self._store.propose(candidate)

    async def evaluate(
        self,
        candidate_id: str,
        suite: FrozenEvaluationSuite,
        *,
        session_id: str | None = None,
    ) -> PromotionEvaluation:
        candidate = self._store.get(candidate_id)
        if candidate is None:
            raise ValueError("candidate not found")
        if candidate.kind != suite.candidate_kind:
            raise ValueError("evaluation suite candidate kind does not match candidate")
        binding = EvaluationBinding(
            suite_id=suite.id,
            provider=suite.provider,
            model=suite.model,
            route=suite.route,
            budget=MappingProxyType(dict(suite.budget)),
            dataset_digest=suite.dataset_digest,
            session_id=session_id,
        )
        results = [await self._evaluator(candidate, case, binding) for case in suite.cases]
        metrics = aggregate_evaluation_metrics(results)
        gates = deterministic_gates(
            candidate,
            metrics,
            suite.baseline_metrics,
            suite.policy,
            held_out=suite.held_out,
            source_disjoint=suite.source_disjoint,
        )
        evaluation = PromotionEvaluation(
            id=str(uuid4()),
            candidate_id=candidate.id,
            candidate_digest=candidate.content_digest,
            baseline_version=candidate.baseline_version,
            baseline_digest=candidate.baseline_digest,
            provider=suite.provider,
            model=suite.model,
            route=suite.route,
            target_fixtures=tuple(case.target_fixture for case in suite.cases),
            budget=dict(suite.budget),
            dataset_digest=suite.dataset_digest,
            dataset_provenance=suite.dataset_provenance,
            held_out=suite.held_out,
            source_disjoint=suite.source_disjoint,
            metric_semantics_version=CURRENT_METRIC_SEMANTICS_VERSION,
            route_role_scopes=suite.route_role_scopes,
            metrics=metrics,
            baseline_metrics=suite.baseline_metrics,
            gates=gates,
            case_results=tuple(
                EvaluationCaseResult(
                    case_id=case.id,
                    target_fixture=case.target_fixture,
                    fixture_digest=case.fixture_digest,
                    metrics=result,
                )
                for case, result in zip(suite.cases, results, strict=True)
            ),
        )
        return self._store.record_evaluation(evaluation)

    def accept(self, candidate_id: str, *, operator_id: str, reason: str) -> None:
        _require_operator(operator_id)
        self._store.operator_decide(
            candidate_id, "accepted", operator_id=operator_id, reason=reason, actor_kind="operator"
        )

    def reject(self, candidate_id: str, *, operator_id: str, reason: str) -> None:
        _require_operator(operator_id)
        self._store.operator_decide(
            candidate_id, "rejected", operator_id=operator_id, reason=reason, actor_kind="operator"
        )

    def model_decide(self, *_: object, **__: object) -> None:
        raise PermissionError("model paths cannot approve or reject promotion candidates")

    def activate(self, candidate_id: str, *, operator_id: str) -> int:
        _require_operator(operator_id)
        evaluations = self._store.evaluations(candidate_id)
        if not evaluations or (
            evaluations[-1].metric_semantics_version != CURRENT_METRIC_SEMANTICS_VERSION
        ):
            raise ValueError("candidate requires reevaluation under current metric semantics")
        return self._store.activate(candidate_id, operator_id=operator_id)

    def rollback(self, kind: CandidateKind, *, operator_id: str) -> int:
        _require_operator(operator_id)
        return self._store.rollback(kind, operator_id=operator_id)

    def snapshot_for_new_work(self, kind: CandidateKind) -> ActivePromotionSnapshot:
        return ActivePromotionSnapshot(kind=kind, candidate=self._store.active(kind))


def frozen_dataset_digest(cases: Sequence[EvaluationCase]) -> str:
    content = [
        {
            "id": case.id,
            "target_fixture": case.target_fixture,
            "source_group": case.source_group,
            "fixture_digest": case.fixture_digest,
        }
        for case in cases
    ]
    return f"sha256:{sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()}"


def _validate_route_role_scopes(scopes: tuple[object, ...]) -> None:
    if (
        not scopes
        or any(not isinstance(scope, str) or scope not in ROUTE_ROLE_SCOPES for scope in scopes)
        or len(set(scopes)) != len(scopes)
        or ("all" in scopes and len(scopes) != 1)
    ):
        raise ValueError(
            "route role scopes must be unique lead/recon/routine/validator values or only all"
        )


def _candidate_route_role_scopes(candidate: PromotionCandidate) -> tuple[str, ...]:
    raw = candidate.content.get("role_scopes", ["all"])
    if not isinstance(raw, (list, tuple)):
        raise TypeError("routing candidate role_scopes must be a string array")
    scopes = tuple(raw)
    _validate_route_role_scopes(scopes)
    return scopes  # type: ignore[return-value]


def _is_sha256_digest(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != len("sha256:") + 64:
        return False
    try:
        int(value.removeprefix("sha256:"), 16)
    except ValueError:
        return False
    return True


def aggregate_evaluation_metrics(results: Sequence[EvaluationMetrics]) -> EvaluationMetrics:
    if not results:
        raise ValueError("evaluation produced no case results")
    count = len(results)
    ordered_latencies = sorted(item.p95_latency_ms for item in results)
    percentile_index = ((95 * count + 99) // 100) - 1
    return EvaluationMetrics(
        task_success_rate=sum(item.task_success_rate for item in results) / count,
        false_positive_rate=sum(item.false_positive_rate for item in results) / count,
        evidence_attribution_rate=sum(item.evidence_attribution_rate for item in results) / count,
        authority_violations=sum(item.authority_violations for item in results),
        mean_cost=sum(item.mean_cost for item in results) / count,
        p95_latency_ms=ordered_latencies[percentile_index],
        cancellation_recovery_rate=sum(item.cancellation_recovery_rate for item in results) / count,
        input_tokens=sum(item.input_tokens for item in results),
        output_tokens=sum(item.output_tokens for item in results),
        total_tokens=sum(item.total_tokens for item in results),
    )


def _require_operator(operator_id: str) -> None:
    if not operator_id.startswith("operator:") or not operator_id.removeprefix("operator:").strip():
        raise PermissionError("promotion lifecycle actions require an explicit operator identity")
