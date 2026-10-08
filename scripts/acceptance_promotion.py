"""Deterministic Phase 11 acceptance over a real swarm and PostgreSQL authority."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from merlin_agents import CORE_WORKER_PROFILES, AgentOrchestrator
from merlin_contracts import (
    ChatRequest,
    EvaluationMetrics,
    ModelCompleted,
    ModelEvent,
    PromotionCandidate,
    TextDelta,
    promotion_digest,
)
from merlin_promotion import (
    CandidateProposal,
    CommittedSwarmEvidence,
    EvaluationCase,
    FrozenEvaluationSuite,
    KernelPromotionService,
    PromotionService,
    frozen_dataset_digest,
)
from merlin_storage_postgres import (
    Database,
    DatabaseConfig,
    DurableSchedulerStore,
    MigrationRunner,
    PromotionRepository,
    SessionRepository,
)

ROOT = Path(__file__).resolve().parents[1]


class ImprovementSwarmProvider:
    """Frozen provider fixture; orchestration remains the production swarm implementation."""

    def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        async def events() -> AsyncIterator[ModelEvent]:
            objective = request.messages[-1].content
            if "independently validate" in objective:
                payload = {"validation": "bounded", "evidence": "result://worker/proposal"}
            else:
                payload = {
                    "guidance": "always re-check tenant ownership",
                    "profile": {
                        "profile_id": "object-authorization",
                        "instructions": (
                            "Reproduce object-boundary claims with attributable evidence."
                        ),
                        "scope": ["selected-target", "admitted-tools"],
                    },
                    "evidence": ["result://worker/authz-1", "result://worker/authz-2"],
                }
            yield TextDelta(text=json.dumps(payload, sort_keys=True))
            yield ModelCompleted(finish_reason="stop")

        return events()


def metrics(success: float) -> EvaluationMetrics:
    return EvaluationMetrics(
        task_success_rate=success,
        false_positive_rate=0.02 if success >= 0.8 else 0.2,
        evidence_attribution_rate=1 if success >= 0.8 else 0.5,
        authority_violations=0,
        mean_cost=1,
        p95_latency_ms=100,
        cancellation_recovery_rate=1,
    )


def suite(candidate_kind="profile") -> FrozenEvaluationSuite:
    cases = (
        EvaluationCase("authz-held-1", "fixture://authz/one", "held-out-a", "sha256:" + "1" * 64),
        EvaluationCase("authz-held-2", "fixture://authz/two", "held-out-b", "sha256:" + "2" * 64),
    )
    return FrozenEvaluationSuite(
        id=f"frozen-held-out-authz-v1-{candidate_kind}",
        candidate_kind=candidate_kind,
        cases=cases,
        provider="deterministic-provider-v1",
        model="frozen-model-v1",
        route="evaluation-only",
        budget={"turns": 4, "tokens": 2_000},
        dataset_provenance="operator-frozen/phase-11",
        dataset_digest=frozen_dataset_digest(cases),
        held_out=True,
        source_disjoint=True,
        baseline_metrics=metrics(0.8),
    )


async def run_acceptance(data_dir: Path) -> dict[str, object]:
    swarm = AgentOrchestrator(
        ImprovementSwarmProvider(), model="frozen-swarm-model", max_workers=2, max_tasks=2
    )
    proposer = swarm.create_task("propose one bounded authorization improvement")
    validator = swarm.create_task("independently validate the bounded proposal")
    swarm_result = await swarm.run_all()
    assert swarm_result.successful
    proposed = json.loads(proposer.result or "{}")
    assert json.loads(validator.result or "{}")["validation"] == "bounded"

    with Database(DatabaseConfig(data_dir=data_dir)) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("phase 11 acceptance")
            scheduler = DurableSchedulerStore(connection)
            schedule_id = scheduler.create_schedule(
                session.id,
                {"kind": "improvement-swarm", "max_workers": 2},
                {},
                ["proposal"],
                ready_nodes=["proposal"],
            )
            lease = scheduler.claim(
                schedule_id,
                "proposal",
                proposer.id,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            )
            result_digest = scheduler.commit_reducer(
                lease.id,
                lease.generation,
                schema_id="promotion-proposal/v1",
                result=proposed,
                checkpoint={"proposal": "committed"},
            )
            evidence = CommittedSwarmEvidence(
                schedule_id=schedule_id,
                node_id="proposal",
                attempt_id=lease.id,
                result_digest=result_digest,
                evidence_references=tuple(proposed["evidence"]),
                agent_id=proposer.id,
            )
            repository = PromotionRepository(connection)

            async def evaluator(candidate, case, binding):
                assert binding.suite_id == suite(candidate.kind).id
                assert binding.dataset_digest == suite(candidate.kind).dataset_digest
                assert case.source_group.startswith("held-out-")
                return metrics(0.5 if candidate.id == "regressing-guidance" else 0.9)

            def verify(item: CommittedSwarmEvidence) -> bool:
                rows = scheduler.list_results(item.schedule_id)
                return any(
                    row["attempt_id"] == item.attempt_id
                    and row["payload_digest"] == item.result_digest
                    for row in rows
                )

            service = PromotionService(repository, evaluator, verify)
            kernel_promotions = KernelPromotionService(
                service,
                repository,
                {
                    suite("profile").id: suite("profile"),
                    suite("guidance").id: suite("guidance"),
                },
                operator_id="operator:acceptance",
            )
            baseline = _baseline()
            repository.propose(baseline)
            baseline_eval = await service.evaluate(baseline.id, suite())
            assert baseline_eval.passed
            service.accept(
                baseline.id, operator_id="operator:acceptance", reason="seed reviewed baseline"
            )
            service.activate(baseline.id, operator_id="operator:acceptance")

            regression = service.propose_from_evidence(
                CandidateProposal(
                    id="regressing-guidance",
                    kind="guidance",
                    version="2.0.0",
                    baseline_version="1.0.0",
                    baseline_digest="sha256:guidance-v1",
                    content={"instructions": proposed["guidance"], "scope": "authorization"},
                ),
                evidence,
            )
            regression_record = await kernel_promotions.evaluate(
                "session-promotion", regression.id, suite("guidance").id
            )
            regression_passed = all(gate["passed"] for gate in regression_record["gates"])
            assert not regression_passed
            await kernel_promotions.reject(regression.id, "held-out regression")

            profile = proposed["profile"]
            ninth = service.propose_from_evidence(
                CandidateProposal(
                    id="ninth-profile",
                    kind="profile",
                    version="9.0.0",
                    baseline_version=baseline.version,
                    baseline_digest=baseline.content_digest,
                    content=dict(profile),
                    profile_id=str(profile["profile_id"]),
                    profile_operation="add",
                ),
                evidence,
            )
            ninth_record = await kernel_promotions.evaluate(
                "session-promotion", ninth.id, suite("profile").id
            )
            ninth_passed = all(gate["passed"] for gate in ninth_record["gates"])
            assert ninth_passed and len(CORE_WORKER_PROFILES) + 1 == 10

            old_work = service.snapshot_for_new_work("profile")
            _assert_model_cannot_promote(service, ninth.id)
            await kernel_promotions.accept(ninth.id, "held-out suite passed")
            await kernel_promotions.activate(ninth.id)
            new_work = service.snapshot_for_new_work("profile")
            assert old_work.candidate and old_work.candidate.id == "baseline-eight"
            assert new_work.candidate and new_work.candidate.id == "ninth-profile"
            assert old_work.candidate.id == "baseline-eight"
            await kernel_promotions.rollback("profile")
            rolled_back = service.snapshot_for_new_work("profile")
            assert rolled_back.candidate and rolled_back.candidate.id == "baseline-eight"

    return {
        "swarm_tasks": len(swarm_result.tasks),
        "evidence_digest": result_digest,
        "regression_rejected": not regression_passed,
        "ninth_profile_accepted": ninth_passed,
        "old_work": old_work.candidate.id,
        "new_work": new_work.candidate.id,
        "rolled_back": rolled_back.candidate.id,
        "model_self_promotion": "denied",
    }


async def real_provider_textual_swarm() -> dict[str, object]:
    """Produce and persist a proposal through configured-provider production composition."""
    from merlin_providers import TargetProviderFactory
    from merlin_tui.app import MerlinApp
    from merlin_tui.bootstrap import build_backend

    async def confirm(*_arguments: object) -> bool:
        return True

    backend = build_backend(ROOT, max_workers=2, tool_confirmer=confirm)
    close_backend = backend.close

    async def defer_backend_close() -> None:
        """Keep production resources alive until ephemeral target cleanup finishes."""

    backend.close = defer_backend_close
    app = MerlinApp(backend=backend)
    target_id = f"promotion-acceptance-{uuid4().hex[:8]}"
    try:
        assessments = backend._assessments  # noqa: SLF001 - production acceptance seam
        config = assessments._attacker_config  # noqa: SLF001
        credentials = dict(assessments._target_factory._credentials)  # noqa: SLF001
        credentials[f"target_{target_id}"] = config.api_key
        assessments._target_factory = TargetProviderFactory(credentials)  # noqa: SLF001
        await assessments.add_target(
            (target_id, "Promotion Acceptance", config.base_url, config.model)
        )
        await assessments.select_target(target_id)
        await assessments.enable_pack("merlin-injection-taxonomy")
        async with app.run_test() as pilot:
            await _send(
                app,
                pilot,
                "/swarm start Use the selected target and committed case evidence to propose "
                "one bounded worker-profile improvement. Return only a JSON object: "
                '{"proposal":{"profile_id":"object-authorization","summary":"short",'
                '"instructions":"specific evidence-based instructions",'
                '"specialty_tags":["authorization","object-boundary"]}}. '
                "Do not request tools, grants, providers, targets, or broader authority.",
            )
            session_id = backend.kernel.session_id
            assert session_id is not None
            schedules = await backend._agents.list_schedules(session_id)  # noqa: SLF001
            if not schedules:
                raise AssertionError("configured-provider swarm produced no durable schedule")
            schedule_id = schedules[0].schedule_id
            store = backend._agents._store  # noqa: SLF001
            rows = await asyncio.to_thread(store.list_results, schedule_id)
            selected: tuple[dict[str, object], dict[str, object]] | None = None
            for row in rows:
                payload = row["payload"]
                structured = payload.get("structured") if isinstance(payload, dict) else None
                proposal = structured.get("proposal") if isinstance(structured, dict) else None
                if isinstance(proposal, dict):
                    selected = (row, proposal)
                    break
            if selected is None:
                raise AssertionError(
                    "configured-provider workers did not return the bounded proposal schema"
                )
            row, proposal = selected
            required = ("profile_id", "summary", "instructions", "specialty_tags")
            if any(key not in proposal for key in required):
                raise AssertionError("configured-provider proposal is incomplete")
            node_id = str(row["node_id"])
            attempt_id = str(row["attempt_id"])
            digest = str(row["payload_digest"])
            candidate_token = uuid4().hex
            candidate_id = f"real-swarm-{candidate_token}"
            payload = {
                "candidate": {
                    "id": candidate_id,
                    "kind": "profile",
                    "version": f"9.0.0-real-{candidate_token}",
                    "baseline_version": "8.0.0",
                    "baseline_digest": "sha256:phase11-baseline-eight",
                    "content": {
                        "profile_id": str(proposal["profile_id"]),
                        "summary": str(proposal["summary"]),
                        "instructions": str(proposal["instructions"]),
                        "specialty_tags": list(proposal["specialty_tags"]),
                        "scope": ["selected-target", "admitted-tools"],
                        "exact_diff": "add one bounded specialist profile",
                    },
                    "profile_id": str(proposal["profile_id"]),
                    "profile_operation": "add",
                },
                "evidence": {
                    "schedule_id": schedule_id,
                    "node_id": node_id,
                    "attempt_id": attempt_id,
                    "result_digest": digest,
                    "evidence_references": [
                        f"merlin-result://schedule/{schedule_id}/node/{node_id}"
                    ],
                    "agent_id": node_id,
                    "committed": True,
                },
            }
            persisted = await backend._promotions.propose(  # noqa: SLF001
                session_id, (json.dumps(payload, sort_keys=True),)
            )
            assert persisted["id"] == candidate_id
            return {
                "candidate_id": candidate_id,
                "schedule_id": schedule_id,
                "node_id": node_id,
                "attempt_id": attempt_id,
                "result_digest": digest,
            }
    finally:
        try:
            await backend._assessments.remove_target(target_id)  # noqa: SLF001
        finally:
            await close_backend()


async def _send(app: object, pilot: object, command: str) -> None:
    app.query_one("#composer").text = command
    await app.action_send()
    if app._stream_task is not None:  # noqa: SLF001 - acceptance waits for production stream
        await app._stream_task  # noqa: SLF001
    await pilot.pause()


def _baseline() -> PromotionCandidate:
    content = {"profile_id": "adversarial-explorer", "instructions": "baseline eight"}
    return PromotionCandidate(
        id="baseline-eight",
        kind="profile",
        version="8.0.0",
        baseline_version="7.0.0",
        baseline_digest="sha256:baseline-seven",
        content=content,
        content_digest=promotion_digest(content),
        proposed_by="operator:acceptance",
        profile_id="adversarial-explorer",
        profile_operation="update",
    )


def _assert_model_cannot_promote(service: PromotionService, candidate_id: str) -> None:
    try:
        service.model_decide(candidate_id, "accepted")
    except PermissionError:
        pass
    else:
        raise AssertionError("model path approved its own promotion")
    try:
        service.activate(candidate_id, operator_id="agent:lead")
    except PermissionError:
        return
    raise AssertionError("model path activated its own promotion")


async def main() -> None:
    real = None
    if os.environ.get("MERLIN_SKIP_REAL_PROVIDER_ACCEPTANCE") != "1":
        real = await real_provider_textual_swarm()
    with TemporaryDirectory(prefix="merlin-promotion-") as directory:
        result = await run_acceptance(Path(directory) / "postgres")
    print("PROMOTION_ACCEPTANCE_OK")
    if real is not None:
        print("configured_provider_swarm=" + json.dumps(real, sort_keys=True))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
