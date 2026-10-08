from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest
from merlin_agents.automation import (
    ARTIFACT_KIND_RUNTIMES,
    SUPPORTED_ARTIFACT_KINDS,
    ArtifactAdmission,
    ArtifactExecutor,
    ArtifactFixtureRunner,
    ArtifactState,
    AutomationArtifact,
    AutomationAudit,
    AutomationCoordinator,
    AutomationError,
    AutomationPipeline,
    AutomationRequest,
    AutomationStep,
    MemoryAutomationJournal,
    RunState,
)
from merlin_agents.specialists import AssignmentBudget, WorkerAssignment


def artifact() -> AutomationArtifact:
    return AutomationArtifact(
        request_id="automation-request-1",
        owning_attempt_id="technique-attempt-1",
        author_identity="author-agent-1",
        author_model="author-model",
        origin="https://reviewed.example",
        max_steps=2,
        steps=(
            AutomationStep(
                step_id="exchange",
                operation="browser_chat_exchange",
                arguments={"prompt": "bounded fixture value"},
                evidence_keys=("request", "response"),
            ),
            AutomationStep(
                step_id="cleanup",
                operation="browser_cleanup",
                arguments={"mode": "reviewed"},
                cleanup=True,
            ),
        ),
    )


def accepted_pipeline():
    journal = MemoryAutomationJournal()
    pipeline = AutomationPipeline(journal)
    authored = pipeline.author(artifact())
    audit = AutomationAudit(
        artifact_digest=authored.digest,
        auditor_identity="auditor-agent-1",
        auditor_model="auditor-model",
        decision="accept",
        reasons=("bounded declarative flow matches the request",),
        static_check_refs=("static-check:1",),
        fixture_evidence_refs=("fixture:1",),
    )
    audited = pipeline.audit(authored, audit)
    admission = ArtifactAdmission(
        artifact_digest=audited.digest,
        owning_attempt_id=audited.owning_attempt_id,
        origin=audited.origin,
        allowed_operations=frozenset({"browser_chat_exchange", "browser_cleanup"}),
        environment_digest="sha256:" + "a" * 64,
    )
    admitted = pipeline.admit(audited, audit, admission)
    return journal, admitted, admission


def test_automation_roles_are_offline_and_cannot_own_target_authority() -> None:
    with pytest.raises(AutomationError, match="canonical JSON"):
        AutomationStep(step_id="bad", operation="browser_click", arguments={"value": {1}})

    with pytest.raises(ValueError, match="cannot own techniques or authority"):
        WorkerAssignment(
            id="assignment-1",
            objective="author a flow",
            profile_id="automation-author",
            target_profile_id="offline",
            technique_ids=("technique-1",),
            guidance_ids=(),
            tool_ids=("automation_artifact_write",),
            skill_ids=(),
            provider_id="provider",
            authority_ids=(),
            budget=AssignmentBudget(max_turns=1),
            input_schema="automation-request.v1",
            output_schema="automation-artifact.v1",
        )


def test_artifact_requires_independent_digest_bound_audit_and_admission() -> None:
    journal = MemoryAutomationJournal()
    pipeline = AutomationPipeline(journal)
    authored = pipeline.author(artifact())
    self_audit = AutomationAudit(
        artifact_digest=authored.digest,
        auditor_identity=authored.author_identity,
        auditor_model="auditor-model",
        decision="accept",
        reasons=("looks good",),
        static_check_refs=("static-check:1",),
        fixture_evidence_refs=("fixture:1",),
    )
    with pytest.raises(AutomationError, match="cannot audit their own"):
        pipeline.audit(authored, self_audit)

    wrong_owner = ArtifactAdmission(
        artifact_digest=authored.digest,
        owning_attempt_id="another-attempt",
        origin=authored.origin,
        allowed_operations=frozenset({"browser_chat_exchange"}),
        environment_digest="sha256:" + "b" * 64,
    )
    independent = AutomationAudit(
        artifact_digest=authored.digest,
        auditor_identity="auditor-agent-1",
        auditor_model="auditor-model",
        decision="accept",
        reasons=("bounded",),
        static_check_refs=("static-check:1",),
        fixture_evidence_refs=("fixture:1",),
    )
    audited = pipeline.audit(authored, independent)
    assert audited.state is ArtifactState.AUDITOR_ACCEPTED
    with pytest.raises(AutomationError, match="cannot rebind"):
        pipeline.admit(audited, independent, wrong_owner)


@pytest.mark.asyncio
async def test_executor_links_every_event_and_evidence_to_owning_attempt() -> None:
    journal, admitted, admission = accepted_pipeline()

    async def dispatch(step: AutomationStep):
        return {
            "status": "completed",
            "evidence": {key: f"evidence:{step.step_id}:{key}" for key in step.evidence_keys},
            "evidence_refs": (() if step.evidence_keys else (f"evidence:{step.step_id}",)),
            "effect_state": "committed" if not step.cleanup else "none",
        }

    result = await ArtifactExecutor(journal, dispatch).execute(
        "artifact-run-1", admitted, admission
    )

    assert result.state is RunState.COMPLETED
    assert result.owning_attempt_id == "technique-attempt-1"
    assert result.artifact_digest == admitted.digest
    assert result.evidence_refs == (
        "evidence:exchange:request",
        "evidence:exchange:response",
        "evidence:cleanup",
    )
    assert all(event.status == "completed" for event in result.events)
    assert journal.runs[result.run_id] == result


@pytest.mark.asyncio
async def test_executor_stops_after_ambiguous_effect_and_records_terminal_run() -> None:
    journal, admitted, admission = accepted_pipeline()
    called: list[str] = []

    def dispatch(step: AutomationStep):
        called.append(step.step_id)
        return {
            "status": "completed",
            "evidence": {key: f"evidence:ambiguous:{key}" for key in step.evidence_keys},
            "evidence_refs": (() if step.evidence_keys else ("evidence:cleanup",)),
            "effect_state": "ambiguous",
        }

    result = await ArtifactExecutor(journal, dispatch).execute(
        "artifact-run-2", admitted, admission
    )

    assert result.state is RunState.AMBIGUOUS
    assert called == ["exchange", "cleanup"]
    assert journal.runs[result.run_id].state is RunState.AMBIGUOUS


@pytest.mark.asyncio
async def test_coordinator_runs_separated_offline_roles_and_deterministic_executor() -> None:
    journal = MemoryAutomationJournal()
    request = AutomationRequest(
        request_id="request-joined",
        owning_attempt_id="attempt-joined",
        objective="repeat one treatment and capture its response",
        origin="https://reviewed.example",
        allowed_operations=frozenset({"browser_chat_exchange", "browser_cleanup"}),
        evidence_expectations=("response",),
    )
    roles: list[str] = []

    def author(value: AutomationRequest) -> AutomationArtifact:
        roles.append("author")
        return AutomationArtifact(
            request_id=value.request_id,
            owning_attempt_id=value.owning_attempt_id,
            author_identity="author-agent",
            author_model="author-model",
            origin=value.origin,
            max_steps=2,
            steps=(
                AutomationStep(
                    step_id="exchange",
                    operation="browser_chat_exchange",
                    arguments={"prompt": "bounded"},
                    evidence_keys=("response",),
                ),
                AutomationStep(
                    step_id="cleanup",
                    operation="browser_cleanup",
                    arguments={},
                    cleanup=True,
                ),
            ),
        )

    def auditor(
        value: AutomationArtifact,
        static_refs: tuple[str, ...],
        fixture_refs: tuple[str, ...],
    ) -> AutomationAudit:
        roles.append("auditor")
        return AutomationAudit(
            artifact_digest=value.digest,
            auditor_identity="auditor-agent",
            auditor_model="auditor-model",
            decision="accept",
            reasons=("independent review accepted the bounded flow",),
            static_check_refs=static_refs,
            fixture_evidence_refs=fixture_refs,
        )

    def dispatch(step: AutomationStep):
        roles.append("executor")
        return {
            "status": "completed",
            "evidence": ({"response": "evidence:response"} if step.evidence_keys else {}),
        }

    coordinator = AutomationCoordinator(
        pipeline=AutomationPipeline(journal),
        author=author,
        auditor=auditor,
        fixture_runner=lambda _artifact: ("fixture:passed",),
        executor=ArtifactExecutor(journal, dispatch),
        environment_digest="sha256:" + "f" * 64,
    )
    outcome = await coordinator.execute(request, run_id="joined-run")

    assert outcome.state == "completed"
    assert outcome.owning_attempt_id == request.owning_attempt_id
    assert roles == ["author", "auditor", "executor", "executor"]
    assert outcome.run is not None
    assert outcome.run.evidence_refs == ("evidence:response",)


@pytest.mark.asyncio
async def test_coordinator_surfaces_static_check_failure_without_target_execution() -> None:
    journal = MemoryAutomationJournal()
    request = AutomationRequest(
        request_id="request-bad",
        owning_attempt_id="attempt-bad",
        objective="capture response",
        origin="https://reviewed.example",
        allowed_operations=frozenset({"browser_chat_exchange", "browser_cleanup"}),
        evidence_expectations=("response",),
    )
    target_calls: list[str] = []
    coordinator = AutomationCoordinator(
        pipeline=AutomationPipeline(journal),
        author=lambda value: AutomationArtifact(
            request_id=value.request_id,
            owning_attempt_id=value.owning_attempt_id,
            author_identity="author-agent",
            author_model="author-model",
            origin=value.origin,
            max_steps=2,
            steps=(
                AutomationStep(
                    step_id="overbroad",
                    operation="shell_command",
                    arguments={"command": "false"},
                ),
                AutomationStep(
                    step_id="cleanup",
                    operation="browser_cleanup",
                    arguments={},
                    cleanup=True,
                ),
            ),
        ),
        auditor=lambda *_args: pytest.fail("auditor must not receive unsafe artifact"),
        fixture_runner=lambda _artifact: pytest.fail("fixture must not run unsafe artifact"),
        executor=ArtifactExecutor(journal, lambda step: target_calls.append(step.step_id)),
        environment_digest="sha256:" + "e" * 64,
    )
    outcome = await coordinator.execute(request, run_id="bad-run")

    assert outcome.state == "failed"
    assert outcome.stage == "static_check"
    assert "exceeds requested operations" in (outcome.error or "")
    assert target_calls == []


@pytest.mark.asyncio
async def test_coordinator_rejects_artifact_arguments_for_an_unreviewed_origin() -> None:
    journal = MemoryAutomationJournal()
    request = AutomationRequest(
        request_id="request-origin",
        owning_attempt_id="attempt-origin",
        objective="capture one response",
        origin="https://reviewed.example/chat",
        allowed_operations=frozenset({"target_request"}),
        evidence_expectations=("response",),
        cleanup_required=False,
    )
    coordinator = AutomationCoordinator(
        pipeline=AutomationPipeline(journal),
        author=lambda value: AutomationArtifact(
            request_id=value.request_id,
            owning_attempt_id=value.owning_attempt_id,
            author_identity="author",
            author_model="model",
            origin=value.origin,
            max_steps=1,
            steps=(
                AutomationStep(
                    step_id="escape",
                    operation="target_request",
                    arguments={"url": "https://other.example/collect"},
                    evidence_keys=("response",),
                ),
            ),
        ),
        auditor=lambda *_args: pytest.fail("auditor must not see rejected artifacts"),
        fixture_runner=lambda *_args: pytest.fail("fixture must not see rejected artifacts"),
        executor=ArtifactExecutor(journal, lambda *_args: pytest.fail("must not execute")),
        environment_digest="sha256:" + "d" * 64,
    )

    outcome = await coordinator.execute(request, run_id="run-origin")

    assert outcome.stage == "static_check"
    assert outcome.state == "failed"
    assert "unreviewed origin" in (outcome.error or "")


def test_artifact_kind_is_carried_into_the_digest() -> None:
    base = artifact()
    assert base.artifact_kind == "declarative_browser_flow"
    assert SUPPORTED_ARTIFACT_KINDS == frozenset(ARTIFACT_KIND_RUNTIMES)

    for unsupported_kind in ("bounded_procedure", "custom_code"):
        with pytest.raises(AutomationError, match="artifact kind is unsupported"):
            AutomationArtifact(
                request_id=base.request_id,
                owning_attempt_id=base.owning_attempt_id,
                author_identity=base.author_identity,
                author_model=base.author_model,
                origin=base.origin,
                steps=base.steps,
                max_steps=base.max_steps,
                artifact_kind=unsupported_kind,
            )

    with pytest.raises(AutomationError, match="current digest schema"):
        AutomationArtifact(
            request_id=base.request_id,
            owning_attempt_id=base.owning_attempt_id,
            author_identity=base.author_identity,
            author_model=base.author_model,
            origin=base.origin,
            steps=base.steps,
            max_steps=base.max_steps,
            schema="merlin.declarative-flow.v1",
        )

    for unsupported_kind in ("bounded_procedure", "custom_code"):
        with pytest.raises(AutomationError, match="unsupported artifact kind"):
            AutomationRequest(
                request_id="request-kind",
                owning_attempt_id="attempt-kind",
                objective="capture response",
                origin="https://reviewed.example",
                allowed_operations=frozenset({"browser_cleanup"}),
                evidence_expectations=("response",),
                artifact_kind=unsupported_kind,
            )


def test_bounded_procedure_is_rejected_until_a_runtime_exists() -> None:
    # bounded_procedure is deliberately unimplemented: it must be refused at
    # the request layer, not discovered later by the automation coordinator.
    with pytest.raises(AutomationError, match="unsupported artifact kind"):
        AutomationRequest(
            request_id="request-bounded",
            owning_attempt_id="attempt-bounded",
            objective="capture response",
            origin="https://reviewed.example",
            allowed_operations=frozenset({"browser_chat_exchange", "browser_cleanup"}),
            evidence_expectations=("response",),
            artifact_kind="bounded_procedure",
        )


@pytest.mark.asyncio
async def test_fixture_runner_executes_flow_and_binds_deterministic_receipts() -> None:
    authored = artifact()
    calls: list[str] = []

    def dispatch(step: AutomationStep):
        calls.append(step.step_id)
        return {
            "status": "completed",
            "evidence": {key: f"fixture:{step.step_id}:{key}" for key in step.evidence_keys},
            "effect_state": "committed" if not step.cleanup else "none",
        }

    refs = await ArtifactFixtureRunner(dispatch, trials=3).run(authored)

    assert len(refs) == 3
    assert [ref.split(":")[1] for ref in refs] == ["0", "1", "2"]
    assert len({ref.rsplit(":", 1)[1] for ref in refs}) == 1
    assert calls == ["exchange", "cleanup"] * 3


@pytest.mark.asyncio
async def test_fixture_runner_fails_on_missing_evidence_kinds() -> None:
    def dispatch(step: AutomationStep):
        return {"status": "completed", "evidence": {}}

    with pytest.raises(AutomationError, match="omitted required evidence kinds"):
        await ArtifactFixtureRunner(dispatch).run(artifact())


@pytest.mark.asyncio
async def test_fixture_runner_fails_on_nondeterministic_trials() -> None:
    counter = {"value": 0}

    def dispatch(step: AutomationStep):
        counter["value"] += 1
        return {
            "status": "completed",
            "evidence": {key: f"fixture:{counter['value']}:{key}" for key in step.evidence_keys},
        }

    with pytest.raises(AutomationError, match="non-deterministic"):
        await ArtifactFixtureRunner(dispatch, trials=2).run(artifact())


@pytest.mark.asyncio
async def test_coordinator_binds_fixture_run_receipts_into_admission() -> None:
    journal = MemoryAutomationJournal()
    request = AutomationRequest(
        request_id="automation-request-1",
        owning_attempt_id="technique-attempt-1",
        objective="repeat one treatment and capture its response",
        origin="https://reviewed.example",
        allowed_operations=frozenset({"browser_chat_exchange", "browser_cleanup"}),
        evidence_expectations=("response",),
    )
    authored = artifact()

    def fixture_dispatch(step: AutomationStep):
        return {
            "status": "completed",
            "evidence": {key: f"fixture:{step.step_id}:{key}" for key in step.evidence_keys},
        }

    def dispatch(step: AutomationStep):
        return {
            "status": "completed",
            "evidence": {key: f"evidence:{key}" for key in step.evidence_keys},
        }

    coordinator = AutomationCoordinator(
        pipeline=AutomationPipeline(journal),
        author=lambda value: authored,
        auditor=lambda value, static_refs, fixture_refs: AutomationAudit(
            artifact_digest=value.digest,
            auditor_identity="auditor-agent",
            auditor_model="auditor-model",
            decision="accept",
            reasons=("fixture execution produced the expected evidence",),
            static_check_refs=static_refs,
            fixture_evidence_refs=fixture_refs,
        ),
        fixture_runner=ArtifactFixtureRunner(fixture_dispatch, trials=2).run,
        executor=ArtifactExecutor(journal, dispatch),
        environment_digest="sha256:" + "b" * 64,
    )
    outcome = await coordinator.execute(request, run_id="run-fixture")

    assert outcome.state == "completed"
    admission = journal.admissions[authored.digest]
    assert len(admission.fixture_evidence_refs) == 2
    assert all(ref.startswith("fixture-run:") for ref in admission.fixture_evidence_refs)
    audit = journal.audits[authored.digest]
    assert audit.fixture_evidence_refs == admission.fixture_evidence_refs


def _sleep_artifact(arguments: dict) -> AutomationArtifact:
    return AutomationArtifact(
        request_id="automation-request-sleep",
        owning_attempt_id="technique-attempt-sleep",
        author_identity="author-agent-1",
        author_model="author-model",
        origin="https://reviewed.example",
        max_steps=2,
        steps=(
            AutomationStep(
                step_id="settle",
                operation="browser_sleep",
                arguments=arguments,
            ),
            AutomationStep(
                step_id="cleanup",
                operation="browser_cleanup",
                arguments={"mode": "reviewed"},
                cleanup=True,
            ),
        ),
    )


def _flow_artifact(selector: object) -> AutomationArtifact:
    return AutomationArtifact(
        request_id="automation-request-flow",
        owning_attempt_id="technique-attempt-flow",
        author_identity="author-agent-1",
        author_model="author-model",
        origin="https://reviewed.example",
        max_steps=2,
        steps=(
            AutomationStep(
                step_id="fill",
                operation="browser_fill",
                arguments={"selector": selector, "value": "bounded"},
                evidence_keys=("request",),
            ),
            AutomationStep(
                step_id="cleanup",
                operation="browser_cleanup",
                arguments={"mode": "reviewed"},
                cleanup=True,
            ),
        ),
    )


def _admit(flow: AutomationArtifact, operations: frozenset[str]) -> None:
    journal = MemoryAutomationJournal()
    pipeline = AutomationPipeline(journal)
    authored = pipeline.author(flow)
    audit = AutomationAudit(
        artifact_digest=authored.digest,
        auditor_identity="auditor-agent-1",
        auditor_model="auditor-model",
        decision="accept",
        reasons=("bounded flow accepted",),
        static_check_refs=("static-check:1",),
        fixture_evidence_refs=("fixture:1",),
    )
    audited = pipeline.audit(authored, audit)
    pipeline.admit(
        audited,
        audit,
        ArtifactAdmission(
            artifact_digest=audited.digest,
            owning_attempt_id=audited.owning_attempt_id,
            origin=audited.origin,
            allowed_operations=operations,
            environment_digest="sha256:" + "c" * 64,
        ),
    )


def test_admission_rejects_unconditional_fixed_sleep() -> None:
    operations = frozenset({"browser_sleep", "browser_cleanup"})
    with pytest.raises(AutomationError, match="unconditional fixed sleep"):
        _admit(_sleep_artifact({"seconds": 5}), operations)
    with pytest.raises(AutomationError, match="unconditional fixed sleep"):
        _admit(_sleep_artifact({"until": "  "}), operations)

    _admit(
        _sleep_artifact({"seconds": 5, "until": {"selector": "#ready"}}),
        operations,
    )


def test_admission_rejects_fill_click_without_selector_fallback() -> None:
    operations = frozenset({"browser_fill", "browser_cleanup"})
    with pytest.raises(AutomationError, match="reviewed selector with a fallback"):
        _admit(_flow_artifact("#message"), operations)
    with pytest.raises(AutomationError, match="reviewed selector with a fallback"):
        _admit(_flow_artifact({"primary": "#message"}), operations)
    with pytest.raises(AutomationError, match="reviewed selector with a fallback"):
        _admit(_flow_artifact(("#message",)), operations)

    _admit(_flow_artifact({"primary": "#message", "fallbacks": ("textarea",)}), operations)
    _admit(_flow_artifact(("#message", "textarea[name=message]")), operations)


@pytest.mark.asyncio
async def test_coordinator_rejects_blind_sleep_before_fixture_or_execution() -> None:
    journal = MemoryAutomationJournal()
    request = AutomationRequest(
        request_id="request-sleep",
        owning_attempt_id="attempt-sleep",
        objective="settle then capture response",
        origin="https://reviewed.example",
        allowed_operations=frozenset({"browser_sleep", "browser_cleanup"}),
        evidence_expectations=("settled",),
    )
    target_calls: list[str] = []
    coordinator = AutomationCoordinator(
        pipeline=AutomationPipeline(journal),
        author=lambda value: AutomationArtifact(
            request_id=value.request_id,
            owning_attempt_id=value.owning_attempt_id,
            author_identity="author-agent",
            author_model="author-model",
            origin=value.origin,
            max_steps=2,
            steps=(
                AutomationStep(
                    step_id="blind",
                    operation="browser_sleep",
                    arguments={"seconds": 30},
                    evidence_keys=("settled",),
                ),
                AutomationStep(
                    step_id="cleanup",
                    operation="browser_cleanup",
                    arguments={},
                    cleanup=True,
                ),
            ),
        ),
        auditor=lambda *_args: pytest.fail("auditor must not receive unsafe artifact"),
        fixture_runner=lambda _artifact: pytest.fail("fixture must not run blind sleeps"),
        executor=ArtifactExecutor(journal, lambda step: target_calls.append(step.step_id)),
        environment_digest="sha256:" + "d" * 64,
    )
    outcome = await coordinator.execute(request, run_id="sleep-run")

    assert outcome.state == "failed"
    assert outcome.stage == "static_check"
    assert "unconditional fixed sleep" in (outcome.error or "")
    assert target_calls == []


def _fault_dispatch(faults: dict[str, BaseException | dict]):
    def dispatch(step: AutomationStep):
        fault = faults.get(step.step_id)
        if isinstance(fault, BaseException):
            raise fault
        if isinstance(fault, dict):
            return fault
        return {
            "status": "completed",
            "evidence": {key: f"evidence:{step.step_id}:{key}" for key in step.evidence_keys},
        }

    return dispatch


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("faults", "expected_state", "error_match"),
    (
        (
            {"exchange": ConnectionError("adapter transport refused")},
            RunState.FAILED,
            "ConnectionError",
        ),
        (
            {"exchange": TimeoutError("adapter timed out")},
            RunState.FAILED,
            "TimeoutError",
        ),
        (
            {"exchange": {"status": "completed", "evidence": "not-a-mapping"}},
            RunState.FAILED,
            "evidence must be a mapping",
        ),
        (
            {"exchange": {"status": "completed", "evidence": {"request": "only-request"}}},
            RunState.FAILED,
            "omitted required evidence",
        ),
        (
            {
                "exchange": {
                    "status": "failed",
                    "evidence": {"request": "r", "response": "target-refused"},
                }
            },
            RunState.FAILED,
            "step failed",
        ),
        (
            {"cleanup": ConnectionError("cleanup transport lost")},
            RunState.FAILED,
            "cleanup ConnectionError",
        ),
    ),
    ids=(
        "adapter-connection-error",
        "adapter-timeout",
        "malformed-evidence-payload",
        "missing-evidence-keys",
        "failed-step-status",
        "cleanup-transport-fault",
    ),
)
async def test_executor_fault_injection_degrades_gracefully(
    faults: dict[str, BaseException | dict],
    expected_state: RunState,
    error_match: str,
) -> None:
    journal, admitted, admission = accepted_pipeline()

    result = await ArtifactExecutor(journal, _fault_dispatch(faults)).execute(
        "artifact-run-fault", admitted, admission
    )

    assert result.state is expected_state
    assert error_match in (result.error or "")
    assert journal.runs[result.run_id] == result
    cleanup_events = [event for event in result.events if event.step_id == "cleanup"]
    assert cleanup_events, "cleanup must still run under injected faults"


@pytest.mark.asyncio
async def test_executor_cancellation_is_terminal_and_propagates() -> None:
    journal, admitted, admission = accepted_pipeline()

    async def dispatch(step: AutomationStep):
        if step.step_id == "exchange":
            raise asyncio.CancelledError
        return {"status": "completed", "evidence": {}}

    with pytest.raises(asyncio.CancelledError):
        await ArtifactExecutor(journal, dispatch).execute(
            "artifact-run-cancelled", admitted, admission
        )

    recorded = journal.runs["artifact-run-cancelled"]
    assert recorded.state is RunState.CANCELLED
    assert any(event.step_id == "cleanup" for event in recorded.events)


@pytest.mark.asyncio
async def test_fixture_runner_selector_flow_is_stable_across_replays() -> None:
    authored = _flow_artifact({"primary": "#message", "fallbacks": ("textarea",)})
    calls: list[str] = []

    def dispatch(step: AutomationStep):
        calls.append(step.step_id)
        selector = step.arguments.get("selector")
        if isinstance(selector, Mapping):
            chosen = selector["primary"]
        else:
            chosen = "cleanup"
        return {
            "status": "completed",
            "evidence": {key: f"fixture:{chosen}:{key}" for key in step.evidence_keys},
        }

    refs = await ArtifactFixtureRunner(dispatch, trials=3).run(authored)

    assert len(refs) == 3
    assert len({ref.rsplit(":", 1)[1] for ref in refs}) == 1
    assert calls == ["fill", "cleanup"] * 3
