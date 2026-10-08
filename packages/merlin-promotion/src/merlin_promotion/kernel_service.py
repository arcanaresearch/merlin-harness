from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from merlin_contracts import EvaluationMetrics, PromotionCandidate

from .service import (
    CandidateProposal,
    CommittedSwarmEvidence,
    EvaluationBinding,
    EvaluationCase,
    FrozenEvaluationSuite,
    PromotionService,
    PromotionStore,
)


class FrozenFixtureRunner:
    """Run immutable reviewed fixture results under an exact evaluation binding."""

    def __init__(self, results: Mapping[str, EvaluationMetrics]) -> None:
        self._results = dict(results)

    async def __call__(
        self, candidate: PromotionCandidate, case: EvaluationCase, binding: EvaluationBinding
    ) -> EvaluationMetrics:
        del candidate
        if case.id not in self._results:
            raise ValueError(f"frozen evaluation result is unavailable: {case.id}")
        if not all((binding.provider, binding.model, binding.route, binding.dataset_digest)):
            raise ValueError("evaluation binding is incomplete")
        return self._results[case.id]


class KernelPromotionService:
    """Command-friendly asynchronous adapter around the strict promotion domain."""

    def __init__(
        self,
        service: PromotionService,
        store: PromotionStore,
        suites: Mapping[str, FrozenEvaluationSuite],
        *,
        operator_id: str,
        evaluation_authorizer: Callable[
            [str, PromotionCandidate, FrozenEvaluationSuite], Awaitable[bool]
        ]
        | None = None,
    ) -> None:
        if not operator_id.startswith("operator:"):
            raise ValueError("configured promotion operator identity is required")
        self._service = service
        self._store = store
        self._suites = dict(suites)
        self._operator_id = operator_id
        self._evaluation_authorizer = evaluation_authorizer

    async def propose(self, session_id: str, arguments: Sequence[str]) -> Mapping[str, Any]:
        del session_id
        if len(arguments) != 1:
            raise ValueError("usage: /promotion propose JSON")
        payload = json.loads(arguments[0])
        if not isinstance(payload, dict):
            raise ValueError("promotion proposal must be a JSON object")
        proposal = CandidateProposal(**payload["candidate"])
        evidence = CommittedSwarmEvidence(**payload["evidence"])
        candidate = await asyncio.to_thread(self._service.propose_from_evidence, proposal, evidence)
        return await self._candidate(candidate)

    async def list_candidates(self) -> Sequence[Mapping[str, Any]]:
        candidates = await asyncio.to_thread(self._store.list_candidates)
        return [await self._candidate(candidate) for candidate in candidates]

    async def show_candidate(self, candidate_id: str) -> Mapping[str, Any]:
        candidate = await asyncio.to_thread(self._store.get, candidate_id)
        if candidate is None:
            raise ValueError("candidate not found")
        evaluations = await asyncio.to_thread(self._store.evaluations, candidate_id)
        value = dict(await self._candidate(candidate))
        value["exact_diff"] = candidate.content.get("exact_diff")
        value["evaluations"] = [evaluation.model_dump(mode="json") for evaluation in evaluations]
        return value

    async def evaluate(
        self, session_id: str, candidate_id: str, suite_id: str | None
    ) -> Mapping[str, Any]:
        candidate = await asyncio.to_thread(self._store.get, candidate_id)
        if candidate is None:
            raise ValueError("candidate not found")
        selected = suite_id or next(
            (key for key, suite in self._suites.items() if suite.candidate_kind == candidate.kind),
            None,
        )
        if selected is None or selected not in self._suites:
            raise ValueError("evaluation suite not found")
        if self._suites[selected].candidate_kind != candidate.kind:
            raise ValueError("evaluation suite candidate kind does not match candidate")
        if candidate.kind in {"routing", "adapter"}:
            if self._evaluation_authorizer is None:
                raise PermissionError("network evaluation requires a session-bound authorizer")
            if not await self._evaluation_authorizer(session_id, candidate, self._suites[selected]):
                raise PermissionError("network evaluation was not authorized")
        evaluation = await self._service.evaluate(
            candidate_id, self._suites[selected], session_id=session_id
        )
        return evaluation.model_dump(mode="json")

    async def accept(self, candidate_id: str, reason: str) -> Mapping[str, Any]:
        await asyncio.to_thread(
            self._service.accept,
            candidate_id,
            operator_id=self._operator_id,
            reason=reason,
        )
        return {"candidate_id": candidate_id, "status": "accepted"}

    async def reject(self, candidate_id: str, reason: str) -> Mapping[str, Any]:
        await asyncio.to_thread(
            self._service.reject,
            candidate_id,
            operator_id=self._operator_id,
            reason=reason,
        )
        return {"candidate_id": candidate_id, "status": "rejected"}

    async def activate(self, candidate_id: str) -> Mapping[str, Any]:
        activation_id = await asyncio.to_thread(
            self._service.activate, candidate_id, operator_id=self._operator_id
        )
        return {"candidate_id": candidate_id, "activation_id": activation_id, "status": "active"}

    async def history(self, kind: str | None) -> Sequence[Mapping[str, Any]]:
        if kind is None:
            rows = []
            for candidate_kind in (
                "skill",
                "guidance",
                "lesson",
                "routing",
                "profile",
                "adapter",
            ):
                rows.extend(await asyncio.to_thread(self._store.history, candidate_kind))
            return rows
        return await asyncio.to_thread(self._store.history, kind)

    async def rollback(self, kind: str) -> Mapping[str, Any]:
        activation_id = await asyncio.to_thread(
            self._service.rollback, kind, operator_id=self._operator_id
        )
        return {"kind": kind, "activation_id": activation_id, "status": "rolled_back"}

    async def _candidate(self, candidate: PromotionCandidate) -> Mapping[str, Any]:
        value = candidate.model_dump(mode="json", exclude={"content"})
        value["permanently_promotable"] = candidate.permanently_promotable
        value["status"] = await asyncio.to_thread(self._store.status, candidate.id)
        return value
