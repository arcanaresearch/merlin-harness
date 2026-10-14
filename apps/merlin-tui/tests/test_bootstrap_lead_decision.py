"""Lead decision parsing tolerance and retry feedback in the bootstrap closure."""

import asyncio
import json
from pathlib import Path

import pytest
from merlin_agents import (
    AssignmentBudget,
    CampaignManifest,
    CampaignProfile,
    FrontierHypothesis,
    LeadFollowUp,
    ReviewedTargetAdapter,
)
from merlin_agents.campaign_service import CampaignLeadDecisionError
from merlin_config import Config, CredentialStore
from merlin_contracts import AgentTask, ModelCompleted, ModelUsage, TextDelta
from merlin_storage_postgres import Database, DatabaseConfig
from merlin_tui.bootstrap import _dynamic_specialist_report_rows, build_backend

GOOD_REQUEST = {
    "request_id": "automation/good",
    "owning_attempt_id": "11111111-1111-4111-8111-111111111111",
    "objective": "Capture one bounded exchange.",
    "artifact_kind": "declarative_browser_flow",
    "allowed_operations": ["fill", "submit"],
    "evidence_expectations": ["response receipts"],
    "cleanup_expectations": ["restore session"],
    "source_task_ids": ["task-1"],
}
BAD_REQUEST = {
    "request_id": "automation/bad",
    "owning_attempt_id": "11111111-1111-4111-8111-111111111111",
    "objective": "Missing operations, evidence, and sources.",
    "artifact_kind": "declarative_browser_flow",
    "allowed_operations": [],
    "evidence_expectations": [],
    "cleanup_expectations": ["restore session"],
    "source_task_ids": [],
}


class _LeadProvider:
    requests = []

    def __init__(self, config) -> None:
        self.config = config

    async def stream(self, request):
        type(self).requests.append(request)
        yield ModelUsage(
            input_tokens=10,
            output_tokens=10,
            total_tokens=20,
        )
        yield TextDelta(
            text=json.dumps(
                {
                    "summary": "one good and one malformed automation request",
                    "coverage_complete": False,
                    "automation_requests": [GOOD_REQUEST, BAD_REQUEST],
                }
            )
        )
        yield ModelCompleted()


def _build_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    provider: type = _LeadProvider,
    model_pricing: dict | None = None,
):
    provider_values = {
        "base_url": "http://provider.test/v1",
        "model": "lead-model",
    }
    if model_pricing is not None:
        provider_values["model_pricing"] = model_pricing
    monkeypatch.setattr(
        "merlin_config.load_config",
        lambda: Config(
            database_url=database_url,
            values={"provider": provider_values},
        ),
    )
    monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
    monkeypatch.setattr("merlin_providers.OpenAICompatibleProvider", provider)
    return build_backend(tmp_path / "workspace")


def _prepare_campaign(backend, campaign_id: str, session_id: str) -> None:
    backend._campaigns._store.prepare(  # noqa: SLF001
        campaign_id,
        session_id,
        {
            "manifest": {
                "budget": {
                    "max_turns": 4,
                    "max_tokens": 100_000,
                    "max_seconds": None,
                    "max_cost_usd": None,
                }
            }
        },
        "sha256:lead-decision",
    )


def _campaign_manifest(campaign_id: str, session_id: str) -> CampaignManifest:
    return CampaignManifest.create(
        id=campaign_id,
        session_id=session_id,
        objective="Assess the reviewed target",
        profile=CampaignProfile.QUICKSCAN,
        target=ReviewedTargetAdapter(
            "target-1", "api", "https://target.invalid/chat", "target-model"
        ),
        provider_id="provider-1",
        provider_model="lead-model",
        provider_effort="default",
        authority_ids=(f"session:{session_id}", "target:target-1"),
        catalogue=(),
        skill_documents=(),
        tool_ids=(),
        budget=AssignmentBudget(4, 100_000, 600),
        specialist_composition="opinionated",
    )


class _StateChangingLeadProvider:
    changed = False

    def __init__(self, config) -> None:
        self.config = config

    async def stream(self, _request):
        yield ModelUsage(input_tokens=10, output_tokens=10, total_tokens=20)
        yield TextDelta(
            text=json.dumps(
                {
                    "summary": "decision from the original task snapshot",
                    "coverage_complete": False,
                }
            )
        )
        type(self).changed = True
        yield ModelCompleted()


@pytest.mark.asyncio
async def test_lead_decision_rejects_result_when_authoritative_tasks_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _StateChangingLeadProvider.changed = False
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(
            tmp_path,
            monkeypatch,
            database.url,
            _StateChangingLeadProvider,
        )
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-stale-snapshot"
            _prepare_campaign(backend, campaign_id, session_id)
            store = backend._campaigns._store  # noqa: SLF001
            agents = backend._campaigns._agents  # noqa: SLF001
            monkeypatch.setattr(
                store,
                "list_waves",
                lambda _campaign_id: [
                    {"wave": 4, "schedule_id": "schedule-4", "kind": "followup"}
                ],
            )

            async def list_schedule_tasks(_schedule_id):
                return (
                    AgentTask(
                        id="task-1",
                        objective="Assess",
                        status="running" if _StateChangingLeadProvider.changed else "completed",
                    ),
                )

            monkeypatch.setattr(agents, "list_schedule_tasks", list_schedule_tasks)

            with pytest.raises(CampaignLeadDecisionError, match="snapshot became stale"):
                await backend._campaigns._lead_runner(  # noqa: SLF001
                    campaign_id,
                    CampaignProfile.RECOMMENDED,
                    0,
                    (AgentTask(id="task-1", objective="Assess", status="completed"),),
                )

            events = [
                row["payload"]
                for row in store.list_lead(campaign_id)
                if row["payload"].get("kind") == "lead_review_stale"
            ]
            assert len(events) == 1
            assert events[0]["wave"] == 4
            assert "authoritative task state changed" in events[0]["reason"]
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_changed_control_projection_supersedes_in_flight_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class Provider:
        def __init__(self, config) -> None:
            self.config = config

        async def stream(self, request):
            prompt = "\n".join(message.content for message in request.messages)
            if "receipt-old" in prompt:
                first_started.set()
                await release_first.wait()
            yield ModelUsage(input_tokens=10, output_tokens=10, total_tokens=20)
            yield TextDelta(
                text=json.dumps(
                    {
                        "summary": "decision for sealed prompt inputs",
                        "coverage_complete": False,
                    }
                )
            )
            yield ModelCompleted()

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url, Provider)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-control-superseded"
            _prepare_campaign(backend, campaign_id, session_id)

            def tasks(receipt: str) -> tuple[AgentTask, ...]:
                return (
                    AgentTask(id="task-1", objective="Assess", status="completed"),
                    AgentTask(
                        id=f"{campaign_id}/lead-control/evidence_get",
                        objective="Project evidence",
                        status="completed",
                        result=json.dumps({"receipt": receipt}),
                    ),
                )

            first = asyncio.create_task(
                backend._campaigns._lead_runner(  # noqa: SLF001
                    campaign_id,
                    CampaignProfile.RECOMMENDED,
                    0,
                    tasks("receipt-old"),
                )
            )
            await first_started.wait()
            newer = await backend._campaigns._lead_runner(  # noqa: SLF001
                campaign_id,
                CampaignProfile.RECOMMENDED,
                0,
                tasks("receipt-new"),
            )
            assert newer.summary == "decision for sealed prompt inputs"
            release_first.set()
            with pytest.raises(CampaignLeadDecisionError, match="snapshot became stale"):
                await first

            stale = [
                row["payload"]
                for row in backend._campaigns._store.list_lead(campaign_id)  # noqa: SLF001
                if row["payload"].get("kind") == "lead_review_stale"
            ]
            assert len(stale) == 1
            assert "normalized Lead prompt inputs changed" in stale[0]["reason"]
        finally:
            release_first.set()
            await backend.close()


@pytest.mark.asyncio
async def test_single_review_rejects_durable_control_change_without_second_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changed = False

    class Provider:
        def __init__(self, config) -> None:
            self.config = config

        async def stream(self, _request):
            nonlocal changed
            yield ModelUsage(input_tokens=10, output_tokens=10, total_tokens=20)
            yield TextDelta(
                text=json.dumps(
                    {
                        "summary": "decision from old durable evidence",
                        "coverage_complete": False,
                    }
                )
            )
            changed = True
            yield ModelCompleted()

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url, Provider)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-control-mutated"
            _prepare_campaign(backend, campaign_id, session_id)

            def projected_tasks(receipt: str) -> tuple[AgentTask, ...]:
                return (
                    AgentTask(id="task-1", objective="Assess", status="completed"),
                    AgentTask(
                        id=f"{campaign_id}/lead-control/evidence_get",
                        objective="Project evidence",
                        status="completed",
                        result=json.dumps({"receipt": receipt}),
                    ),
                )

            async def refresh_prompt_tasks() -> tuple[AgentTask, ...]:
                return projected_tasks("receipt-new" if changed else "receipt-old")

            with pytest.raises(CampaignLeadDecisionError, match="snapshot became stale"):
                await backend._campaigns._lead_runner(  # noqa: SLF001
                    campaign_id,
                    CampaignProfile.RECOMMENDED,
                    0,
                    projected_tasks("receipt-old"),
                    refresh_prompt_tasks,
                )

            stale = [
                row["payload"]
                for row in backend._campaigns._store.list_lead(campaign_id)  # noqa: SLF001
                if row["payload"].get("kind") == "lead_review_stale"
            ]
            assert len(stale) == 1
            assert "durable Lead prompt inputs changed" in stale[0]["reason"]
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_production_lead_review_does_not_invalidate_its_own_journal_telemetry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Provider:
        def __init__(self, config) -> None:
            self.config = config

        async def stream(self, _request):
            yield ModelUsage(input_tokens=10, output_tokens=10, total_tokens=20)
            yield TextDelta(
                text=json.dumps(
                    {
                        "summary": "accepted production review",
                        "coverage_complete": False,
                    }
                )
            )
            yield ModelCompleted()

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url, Provider)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-production-self-journal"
            _prepare_campaign(backend, campaign_id, session_id)
            manifest = _campaign_manifest(campaign_id, session_id)

            result = await backend._campaigns._run_lead_with_policy(  # noqa: SLF001
                manifest,
                (AgentTask(id="task-1", objective="Assess", status="completed"),),
            )

            assert result.summary == "accepted production review"
            lead_rows = backend._campaigns._store.list_lead(campaign_id)  # noqa: SLF001
            assert not any(row["payload"].get("kind") == "lead_review_stale" for row in lead_rows)
            assert any(row.get("decision_id") is not None for row in lead_rows)
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_production_lead_review_rejects_external_worker_evidence_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def record_external_evidence() -> None:
        pass

    class Provider:
        def __init__(self, config) -> None:
            self.config = config

        async def stream(self, _request):
            yield ModelUsage(input_tokens=10, output_tokens=10, total_tokens=20)
            yield TextDelta(
                text=json.dumps(
                    {
                        "summary": "decision from the old evidence projection",
                        "coverage_complete": False,
                    }
                )
            )
            record_external_evidence()
            yield ModelCompleted()

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url, Provider)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-production-external-evidence"
            _prepare_campaign(backend, campaign_id, session_id)
            manifest = _campaign_manifest(campaign_id, session_id)
            store = backend._campaigns._store  # noqa: SLF001

            def record() -> None:
                store.record_worker_message(
                    {
                        "id": "external-evidence-1",
                        "campaign_id": campaign_id,
                        "wave": 0,
                        "source_task_id": "worker/recon",
                        "destination": "lead",
                        "content": "A new attributable target receipt arrived during review.",
                        "content_digest": "sha256:" + "a" * 64,
                        "kind": "discovery",
                    }
                )

            record_external_evidence = record

            with pytest.raises(CampaignLeadDecisionError, match="snapshot became stale"):
                await backend._campaigns._run_lead_with_policy(  # noqa: SLF001
                    manifest,
                    (AgentTask(id="task-1", objective="Assess", status="completed"),),
                )

            stale = [
                row["payload"]
                for row in store.list_lead(campaign_id)
                if row["payload"].get("kind") == "lead_review_stale"
            ]
            assert len(stale) == 1
            assert "durable Lead prompt inputs changed" in stale[0]["reason"]
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_lead_decision_drops_one_malformed_automation_request_and_journals_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _LeadProvider.requests = []
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-decision-drop"
            _prepare_campaign(backend, campaign_id, session_id)

            result = await backend._campaigns._lead_runner(  # noqa: SLF001
                campaign_id,
                CampaignProfile.RECOMMENDED,
                0,
                (AgentTask(id="task-1", objective="Assess", status="completed"),),
            )

            assert [request.request_id for request in result.automation_requests] == [
                "automation/good"
            ]
            assert result.usage["dropped_automation_requests"] == 1
            drops = [
                row["payload"]
                for row in backend._campaigns._store.list_lead(campaign_id)  # noqa: SLF001
                if row["payload"].get("kind") == "lead_automation_request_dropped"
            ]
            assert len(drops) == 1
            assert drops[0]["admitted"] == 1
            assert drops[0]["drops"] == [
                {
                    "request_id": "automation/bad",
                    "reason": "Lead automation requests require operations, evidence, and sources",
                }
            ]
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_lead_retry_prompt_quotes_the_daemon_rejection_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _LeadProvider.requests = []
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-decision-feedback"
            _prepare_campaign(backend, campaign_id, session_id)
            backend._campaigns._store.append_lead(  # noqa: SLF001
                campaign_id,
                0,
                {
                    "kind": "lead_decision_rejected",
                    "attempt": 0,
                    "reason": (
                        "Lead automation requests require operations, evidence, and sources"
                    ),
                    "retryable": True,
                },
            )

            await backend._campaigns._lead_runner(  # noqa: SLF001
                campaign_id,
                CampaignProfile.RECOMMENDED,
                1,
                (AgentTask(id="task-1", objective="Assess", status="completed"),),
            )

            feedback = [
                message.content
                for message in _LeadProvider.requests[0].messages
                if message.content.startswith("Daemon feedback:")
            ]
            assert len(feedback) == 1
            assert (
                "Lead automation requests require operations, evidence, and sources" in feedback[0]
            )
            assert "single JSON object only" in feedback[0]
            assert _LeadProvider.requests[0].reasoning == "disabled"
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_lead_retry_feedback_has_a_fixed_byte_ceiling_for_huge_id_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _LeadProvider.requests = []
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-feedback-bounded"
            _prepare_campaign(backend, campaign_id, session_id)
            identifiers = [f"candidate-{index:05d}" for index in range(12_765)]
            reason = f"orphaned={identifiers}; double_owned={identifiers}"
            backend._campaigns._store.append_lead(  # noqa: SLF001
                campaign_id,
                0,
                {
                    "kind": "lead_decision_rejected",
                    "attempt": 0,
                    "reason": reason,
                    "retryable": True,
                },
            )

            await backend._campaigns._lead_runner(  # noqa: SLF001
                campaign_id,
                CampaignProfile.RECOMMENDED,
                1,
                (AgentTask(id="task-1", objective="Assess", status="completed"),),
            )

            feedback = next(
                message.content
                for message in _LeadProvider.requests[0].messages
                if message.content.startswith("Daemon feedback:")
            )
            assert len(feedback.encode("utf-8")) <= 4_096
            assert "[truncated bytes=" in feedback
            assert "sha256:" in feedback
            assert identifiers[-1] not in feedback
        finally:
            await backend.close()


class _DynamicLeadProvider:
    """Lead that emits a single dynamic-specialist followup."""

    requests = []

    def __init__(self, config) -> None:
        self.config = config

    async def stream(self, request):
        type(self).requests.append(request)
        yield ModelUsage(
            input_tokens=10,
            output_tokens=10,
            total_tokens=20,
        )
        yield TextDelta(
            text=json.dumps(
                {
                    "summary": "one dynamic specialist followup",
                    "coverage_complete": False,
                    "followups": [
                        {
                            "profile_id": "dynamic-injection",
                            "objective": "Probe the refund-trap prompt injection surface.",
                            "source_task_ids": ["task-1"],
                            "evidence_refs": ["receipt:recon"],
                            "evidence_expectation": (
                                "A captured exchange where the prompt injection is attempted."
                            ),
                            "technique_ids": ["prompt-injection"],
                        }
                    ],
                }
            )
        )
        yield ModelCompleted()


@pytest.mark.asyncio
async def test_lead_dynamic_followup_carries_specialist_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _DynamicLeadProvider.requests = []
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url, _DynamicLeadProvider)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-dynamic-followup"
            _prepare_campaign(backend, campaign_id, session_id)

            result = await backend._campaigns._lead_runner(  # noqa: SLF001
                campaign_id,
                CampaignProfile.RECOMMENDED,
                0,
                (AgentTask(id="task-1", objective="Recon", status="completed"),),
            )

            assert len(result.followups) == 1
            specialist = result.followups[0].specialist
            assert specialist is not None
            assert specialist.label == "dynamic-injection"
            assert specialist.technique_ids == ("prompt-injection",)
            assert specialist.source_task_ids == ("task-1",)
            assert specialist.evidence_refs == ("receipt:recon",)
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_lead_decision_survives_uat_scale_terminal_task_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A campaign whose exact terminal identities exceed the old snapshot
    budget must still produce an accepted Lead decision (UAT run4 regression:
    deterministic lead_decision_rejected -> replan_required loop)."""
    _LeadProvider.requests = []
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-snapshot-growth"
            _prepare_campaign(backend, campaign_id, session_id)
            terminal_tasks = tuple(
                AgentTask(
                    id=(
                        f"{campaign_id}/wave-{1 + index // 100}/"
                        f"adversarial-explorer-{index % 40}/goat-{index:016x}"
                    ),
                    objective="Execute the reviewed technique with attributable evidence. " * 6,
                    status="completed" if index % 5 else "failed",
                    technique_ids=(
                        "merlin-injection-taxonomy/cat/multi-turn/"
                        f"technique-{index:03d}-long-identity",
                    ),
                    result="Observed target exchange with attributable receipts. " * 40,
                )
                for index in range(640)
            )
            tasks = (
                *terminal_tasks,
                AgentTask(id="task-1", objective="Assess", status="completed"),
            )

            result = await backend._campaigns._lead_runner(  # noqa: SLF001
                campaign_id,
                CampaignProfile.RECOMMENDED,
                0,
                tasks,
            )

            assert result.summary == "one good and one malformed automation request"
            user_payload = next(
                message.content
                for message in _LeadProvider.requests[0].messages
                if '"tasks"' in message.content
            )
            snapshot = json.loads(user_payload)
            manifest = snapshot["terminal_task_manifest"]
            assert manifest["omitted_terminal_task_count"] > 0
            projected_ids = {row["id"] for row in snapshot["tasks"]}
            assert "task-1" in projected_ids
            assert terminal_tasks[-1].id in projected_ids
            assert terminal_tasks[0].id not in projected_ids
            assert len(user_payload.encode("utf-8")) <= 65_536
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_lead_decision_compacts_oversized_control_projections_at_scale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A campaign whose control-plane projections alone strain the snapshot
    budget must still produce an accepted Lead decision: unbounded evidence
    rows compact to a bounded recent window, and as a final resort oversized
    control projections demote to digest stubs instead of looping
    lead_decision_rejected forever (Bank L1 wave-8 regression)."""
    _LeadProvider.requests = []
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-control-snapshot-growth"
            _prepare_campaign(backend, campaign_id, session_id)
            evidence_rows = [
                {
                    "task_id": f"{campaign_id}/wave-0/worker-{index:04d}",
                    "digest": "a" * 64,
                    "reference": f"merlin-result://schedule/x/node/worker-{index:04d}",
                    "target_call_ids": [f"target-call:call_{index:04d}_{'b' * 24}"],
                }
                for index in range(240)
            ]
            control_rows = (
                AgentTask(
                    id=f"{campaign_id}/lead-control/evidence_get",
                    objective="Read-only Campaign Lead control-plane projection: evidence_get",
                    status="completed",
                    result=json.dumps(evidence_rows),
                ),
                AgentTask(
                    id=f"{campaign_id}/lead-control/attack_graph_get",
                    objective="Read-only Campaign Lead control-plane projection: attack_graph_get",
                    status="completed",
                    result=json.dumps(
                        {
                            "prior_plan_updates": [
                                {"rationale": "x" * 512, "wave": index} for index in range(64)
                            ],
                            "automation_requests": [],
                            "technique_attempts": [],
                        }
                    ),
                ),
            )
            terminal_rows = tuple(
                AgentTask(
                    id=f"{campaign_id}/wave-1/adversarial-explorer-{index}/goat-{index:016x}",
                    objective="Execute the reviewed technique with attributable evidence. " * 6,
                    status="completed" if index % 5 else "failed",
                    technique_ids=(
                        f"merlin-injection-taxonomy/cat/direct/technique-{index:03d}-long-identity",
                    ),
                    result="Observed target exchange with attributable receipts. " * 40,
                )
                for index in range(320)
            )
            tasks = (
                *terminal_rows,
                *control_rows,
                AgentTask(id="task-1", objective="Assess", status="completed"),
            )

            result = await backend._campaigns._lead_runner(  # noqa: SLF001
                campaign_id,
                CampaignProfile.RECOMMENDED,
                0,
                tasks,
            )

            assert result.summary == "one good and one malformed automation request"
            user_payload = next(
                message.content
                for message in _LeadProvider.requests[0].messages
                if '"tasks"' in message.content
            )
            snapshot = json.loads(user_payload)
            assert len(user_payload.encode("utf-8")) <= 65_536
            rows_by_id = {row["id"]: row for row in snapshot["tasks"]}
            evidence_row = rows_by_id[f"{campaign_id}/lead-control/evidence_get"]
            evidence_result = evidence_row["result"]
            if isinstance(evidence_result, dict) and "evidence_rows" in evidence_result:
                # Bounded window, exact omission count.
                assert evidence_result["omitted_evidence_row_count"] > 0
                assert len(evidence_result["evidence_rows"]) <= 8
            else:
                # Or demoted entirely to a digest stub under heavier pressure.
                assert evidence_result["projection_demoted"] is True
        finally:
            await backend.close()


def test_dynamic_specialist_report_rows_strip_namespace_prefix() -> None:
    rows = [
        {
            "id": "dynamic-abc123",
            "state": "active",
            "admitted_wave": 2,
            "retired_wave": None,
            "retirement_reason": None,
            "record": {
                "label": "dynamic-injection",
            },
        },
        {
            "id": "dynamic-def456",
            "state": "retired",
            "admitted_wave": 1,
            "retired_wave": 3,
            "retirement_reason": "completed",
            "record": {
                "label": "dynamic-refund-abuse",
            },
        },
    ]
    projected = _dynamic_specialist_report_rows(rows)
    assert projected == [
        {
            "name": "injection",
            "state": "active",
            "admitted_wave": 2,
            "retired_wave": None,
            "retirement_reason": None,
        },
        {
            "name": "refund-abuse",
            "state": "retired",
            "admitted_wave": 1,
            "retired_wave": 3,
            "retirement_reason": "completed",
        },
    ]


def test_dynamic_specialist_report_rows_recover_label_from_contract() -> None:
    objective = "Probe the surface\n\nDynamic specialist contract: " + json.dumps(
        {"label": "dynamic-credential-stuffing"}, sort_keys=True
    )
    rows = [
        {
            "id": "dynamic-zzz",
            "state": "active",
            "admitted_wave": 0,
            "retired_wave": None,
            "retirement_reason": None,
            "record": {"assignment": {"objective": objective}},
        }
    ]
    (projected,) = _dynamic_specialist_report_rows(rows)
    assert projected["name"] == "credential-stuffing"


class _UnknownSourceLeadProvider:
    """Lead that cites a reconnaissance task this campaign never ran."""

    requests = []

    def __init__(self, config) -> None:
        self.config = config

    async def stream(self, request):
        type(self).requests.append(request)
        yield ModelUsage(
            input_tokens=10,
            output_tokens=10,
            total_tokens=20,
        )
        yield TextDelta(
            text=json.dumps(
                {
                    "summary": "composition followup citing a reused (nonexistent) recon task",
                    "coverage_complete": False,
                    "followups": [
                        {
                            "profile_id": "dynamic-injection",
                            "objective": "Probe the refund-trap prompt injection surface.",
                            "source_task_ids": ["campaign/lead-unknown-source/recon"],
                            "evidence_refs": ["receipt:recon"],
                            "evidence_expectation": "A captured exchange attempting injection.",
                            "technique_ids": ["prompt-injection"],
                        }
                    ],
                }
            )
        )
        yield ModelCompleted()


@pytest.mark.asyncio
async def test_lead_followup_citing_unknown_source_is_dropped_and_journaled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run18 root cause: in the recon-reuse path no recon task exists, so a
    specialist followup citing it must not vanish silently — the drop is
    journaled and replayed into the next attempt's feedback."""
    _UnknownSourceLeadProvider.requests = []
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url, _UnknownSourceLeadProvider)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-unknown-source"
            _prepare_campaign(backend, campaign_id, session_id)

            result = await backend._campaigns._lead_runner(  # noqa: SLF001
                campaign_id,
                CampaignProfile.RECOMMENDED,
                0,
                (AgentTask(id="task-1", objective="Assess", status="completed"),),
            )

            assert result.followups == ()
            assert result.usage["dropped_followups"] == 1
            drops = [
                row["payload"]
                for row in backend._campaigns._store.list_lead(campaign_id)  # noqa: SLF001
                if row["payload"].get("kind") == "lead_followup_dropped"
            ]
            assert len(drops) == 1
            assert drops[0]["drops"][0]["profile_id"] == "dynamic-injection"
            assert "campaign/lead-unknown-source/recon" in drops[0]["drops"][0]["reason"]

            await backend._campaigns._lead_runner(  # noqa: SLF001
                campaign_id,
                CampaignProfile.RECOMMENDED,
                1,
                (AgentTask(id="task-1", objective="Assess", status="completed"),),
            )

            feedback = [
                message.content
                for message in _UnknownSourceLeadProvider.requests[-1].messages
                if message.content.startswith("Daemon feedback:")
            ]
            assert len(feedback) == 1
            assert "dropped before admission" in feedback[0]
            assert "campaign/lead-unknown-source/recon" in feedback[0]
        finally:
            await backend.close()


class _PricedUsageLeadProvider:
    """Provider-reported tokens without a provider-reported cost."""

    def __init__(self, config) -> None:
        self.config = config

    async def stream(self, request):
        yield ModelUsage(
            input_tokens=1000,
            output_tokens=200,
            total_tokens=1200,
            uncached_input_tokens=600,
            cache_read_input_tokens=400,
        )
        yield TextDelta(text=json.dumps({"summary": "priced usage", "coverage_complete": True}))
        yield ModelCompleted()


@pytest.mark.asyncio
async def test_lead_usage_cost_falls_back_to_pinned_pricing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pricing = {
        "lead-model": {
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 2.0,
            "cache_read_input_cost_per_million": 0.1,
        }
    }
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(
            tmp_path, monkeypatch, database.url, _PricedUsageLeadProvider, pricing
        )
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-priced-usage"
            _prepare_campaign(backend, campaign_id, session_id)

            result = await backend._campaigns._lead_runner(  # noqa: SLF001
                campaign_id,
                CampaignProfile.RECOMMENDED,
                0,
                (AgentTask(id="task-1", objective="Assess", status="completed"),),
            )

            # (600 uncached + 0 cache-creation) * 1.0 + 400 cache-read * 0.1
            # + 200 output * 2.0, all per million.
            assert result.usage["estimated_cost_usd"] == pytest.approx(
                (600 * 1.0 + 400 * 0.1 + 200 * 2.0) / 1_000_000
            )
            assert result.usage["cache_read_input_tokens"] == 400
        finally:
            await backend.close()


class _EmptyDecisionLeadProvider:
    """Returns usage, then a completion with no decision text."""

    def __init__(self, config) -> None:
        self.config = config

    async def stream(self, request):
        yield ModelUsage(
            input_tokens=100,
            output_tokens=0,
            total_tokens=100,
            estimated_cost_usd=0.0005,
        )
        yield ModelCompleted(finish_reason="stop", disposition="completed")


@pytest.mark.asyncio
async def test_lead_uncommitted_decision_usage_is_journaled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url, _EmptyDecisionLeadProvider)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-uncommitted-usage"
            _prepare_campaign(backend, campaign_id, session_id)

            with pytest.raises(ValueError, match="empty decision"):
                await backend._campaigns._lead_runner(  # noqa: SLF001
                    campaign_id,
                    CampaignProfile.RECOMMENDED,
                    0,
                    (AgentTask(id="task-1", objective="Assess", status="completed"),),
                )

            journaled = [
                row["payload"]
                for row in backend._campaigns._store.list_lead(campaign_id)  # noqa: SLF001
                if row["payload"].get("kind") == "lead_decision_usage_uncommitted"
            ]
            assert len(journaled) == 1
            assert journaled[0]["usage"]["total_tokens"] == 100
            assert journaled[0]["usage"]["estimated_cost_usd"] == pytest.approx(0.0005)
        finally:
            await backend.close()


class _CriticProvider:
    """Exploration critic usage without cost; verdict rejects so no planner call."""

    def __init__(self, config) -> None:
        self.config = config

    async def stream(self, request):
        yield ModelUsage(
            input_tokens=500,
            output_tokens=100,
            total_tokens=600,
        )
        yield TextDelta(
            text=json.dumps(
                {
                    "verdict": "reject",
                    "evidence_refs": ["receipt:source"],
                    "closest_known_technique_id": "catalogue:known",
                    "meaningful_difference_claim": "none",
                    "alternative_explanation": "transient behavior",
                    "falsification_test": "compare both arms",
                    "rationale": "already covered",
                    "confidence": "medium",
                    "limitations": ["not executed"],
                    "causal_ambiguity": False,
                    "high_risk_promotion": False,
                }
            )
        )
        yield ModelCompleted(finish_reason="stop", disposition="completed")


@pytest.mark.asyncio
async def test_exploration_review_journals_inference_usage_with_pricing_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pricing = {
        "lead-model": {
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 2.0,
        }
    }
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url, _CriticProvider, pricing)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/exploration-review-usage"
            _prepare_campaign(backend, campaign_id, session_id)

            hypothesis = FrontierHypothesis("surface", "assertion", "oracle", "novelty")
            followup = LeadFollowUp("injection", "test hypothesis", ("source",), hypothesis)
            review_runner = backend._campaigns._exploration_review_runner  # noqa: SLF001
            assert review_runner is not None
            decision, plan = await review_runner(
                campaign_id,
                CampaignProfile.EXPLORATION,
                2,
                followup,
                {"source": {"reference": "receipt:source"}},
            )

            assert decision.verdict == "reject"
            assert plan is None
            journaled = [
                row["payload"]
                for row in backend._campaigns._store.list_lead(campaign_id)  # noqa: SLF001
                if row["payload"].get("kind") == "exploration_review_usage"
            ]
            assert len(journaled) == 1
            usage = journaled[0]["usage"]
            assert usage["total_tokens"] == 600
            assert usage["estimated_cost_usd"] == pytest.approx((500 * 1.0 + 100 * 2.0) / 1_000_000)
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_lead_retry_feedback_deduplicates_identical_rejection_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated identical rejections across attempts must be replayed once, not
    re-read line-per-attempt by the Lead."""
    _LeadProvider.requests = []
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-feedback-dedup"
            _prepare_campaign(backend, campaign_id, session_id)
            reason = "Lead automation requests require operations, evidence, and sources"
            for attempt in range(3):
                backend._campaigns._store.append_lead(  # noqa: SLF001
                    campaign_id,
                    attempt,
                    {
                        "kind": "lead_decision_rejected",
                        "attempt": attempt,
                        "reason": reason,
                        "retryable": True,
                    },
                )

            await backend._campaigns._lead_runner(  # noqa: SLF001
                campaign_id,
                CampaignProfile.RECOMMENDED,
                3,
                (AgentTask(id="task-1", objective="Assess", status="completed"),),
            )

            feedback = [
                message.content
                for message in _LeadProvider.requests[0].messages
                if message.content.startswith("Daemon feedback:")
            ]
            assert len(feedback) == 1
            assert feedback[0].count(reason) == 1
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_lead_retry_feedback_replays_dynamic_specialist_rejection_with_gate_criteria(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dynamic_specialist_rejected journal rows must reach the Lead prompt with
    the admission gate criteria so the Lead corrects instead of resubmitting."""
    _DynamicLeadProvider.requests = []
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url, _DynamicLeadProvider)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-specialist-feedback"
            _prepare_campaign(backend, campaign_id, session_id)
            backend._campaigns._store.append_lead(  # noqa: SLF001
                campaign_id,
                0,
                {
                    "kind": "dynamic_specialist_rejected",
                    "label": "dynamic-injection",
                    "proposal_digest": "sha256:deadbeef",
                    "reason": "source tasks are not completed in-campaign evidence",
                },
            )

            await backend._campaigns._lead_runner(  # noqa: SLF001
                campaign_id,
                CampaignProfile.RECOMMENDED,
                1,
                (AgentTask(id="task-1", objective="Assess", status="completed"),),
            )

            feedback = [
                message.content
                for message in _DynamicLeadProvider.requests[0].messages
                if message.content.startswith("Daemon feedback:")
            ]
            assert len(feedback) == 1
            assert "dynamic-injection" in feedback[0]
            assert "source tasks are not completed in-campaign evidence" in feedback[0]
            assert "Dynamic specialist admission gate:" in feedback[0]
            assert "Do not resubmit an identical proposal." in feedback[0]
        finally:
            await backend.close()


_FOLLOWUP = {
    "profile_id": "dynamic-injection",
    "objective": "Probe the refund-trap prompt injection surface.",
    "source_task_ids": ["task-1"],
    "evidence_refs": ["receipt:recon"],
    "evidence_expectation": "A captured exchange where the prompt injection is attempted.",
    "technique_ids": ["prompt-injection"],
}


class _TruncatedLeadProvider:
    """Lead whose reply is cut mid-followups-array by the output-token cap."""

    def __init__(self, config) -> None:
        self.config = config

    async def stream(self, request):
        yield ModelUsage(
            input_tokens=10,
            output_tokens=10,
            total_tokens=20,
        )
        truncated = json.dumps(
            {
                "coverage_complete": False,
                "followups": [_FOLLOWUP, {**_FOLLOWUP, "objective": "second probe"}],
            }
        )
        yield TextDelta(text=truncated[: len(truncated) - 40])
        yield ModelCompleted(finish_reason="length", disposition="completed")


@pytest.mark.asyncio
async def test_truncated_lead_decision_is_salvaged_and_journaled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url, _TruncatedLeadProvider)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-decision-salvaged"
            _prepare_campaign(backend, campaign_id, session_id)

            result = await backend._campaigns._lead_runner(  # noqa: SLF001
                campaign_id,
                CampaignProfile.RECOMMENDED,
                0,
                (AgentTask(id="task-1", objective="Assess", status="completed"),),
            )

            assert len(result.followups) == 1
            assert result.followups[0].objective == _FOLLOWUP["objective"]
            assert result.summary.startswith("Salvaged from truncated Lead decision")
            assert "finish_reason=length" in result.summary
            salvaged = [
                row["payload"]
                for row in backend._campaigns._store.list_lead(campaign_id)  # noqa: SLF001
                if row["payload"].get("kind") == "lead_decision_salvaged"
            ]
            assert len(salvaged) == 1
            assert salvaged[0]["finish_reason"] == "length"
            assert salvaged[0]["recovered"] == {"followups": 1}
        finally:
            await backend.close()


class _UnusableTruncatedLeadProvider:
    """Truncated before any complete decision-relevant field is emitted."""

    def __init__(self, config) -> None:
        self.config = config

    async def stream(self, request):
        yield ModelUsage(
            input_tokens=10,
            output_tokens=10,
            total_tokens=20,
        )
        yield TextDelta(text='{"summa')
        yield ModelCompleted(finish_reason="length", disposition="completed")


@pytest.mark.asyncio
async def test_truncated_lead_decision_without_usable_fields_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(
            tmp_path, monkeypatch, database.url, _UnusableTruncatedLeadProvider
        )
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-decision-salvage-empty"
            _prepare_campaign(backend, campaign_id, session_id)

            with pytest.raises(ValueError, match="invalid decision"):
                await backend._campaigns._lead_runner(  # noqa: SLF001
                    campaign_id,
                    CampaignProfile.RECOMMENDED,
                    0,
                    (AgentTask(id="task-1", objective="Assess", status="completed"),),
                )

            kinds = [
                row["payload"].get("kind")
                for row in backend._campaigns._store.list_lead(campaign_id)  # noqa: SLF001
            ]
            assert "lead_decision_salvaged" not in kinds
            assert "lead_decision_usage_uncommitted" in kinds
        finally:
            await backend.close()


class _GarbageLeadProvider:
    """A normal-completion reply that is not JSON at all must not salvage."""

    def __init__(self, config) -> None:
        self.config = config

    async def stream(self, request):
        yield ModelUsage(
            input_tokens=10,
            output_tokens=10,
            total_tokens=20,
        )
        yield TextDelta(text="I cannot help with that request.")
        yield ModelCompleted(finish_reason="stop", disposition="completed")


@pytest.mark.asyncio
async def test_non_truncation_garbage_lead_decision_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        backend = _build_backend(tmp_path, monkeypatch, database.url, _GarbageLeadProvider)
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/lead-decision-garbage"
            _prepare_campaign(backend, campaign_id, session_id)

            with pytest.raises(ValueError, match="invalid decision"):
                await backend._campaigns._lead_runner(  # noqa: SLF001
                    campaign_id,
                    CampaignProfile.RECOMMENDED,
                    0,
                    (AgentTask(id="task-1", objective="Assess", status="completed"),),
                )

            kinds = [
                row["payload"].get("kind")
                for row in backend._campaigns._store.list_lead(campaign_id)  # noqa: SLF001
            ]
            assert "lead_decision_salvaged" not in kinds
        finally:
            await backend.close()
