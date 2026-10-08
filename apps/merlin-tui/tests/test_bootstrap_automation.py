"""Integration coverage for the run_automation_request bootstrap closure."""

import json
from pathlib import Path

import pytest
from merlin_agents import (
    AssignmentBudget,
    CampaignManifest,
    CampaignProfile,
    LeadAutomationRequest,
    ReviewedTargetAdapter,
)
from merlin_config import Config, CredentialStore
from merlin_contracts import (
    TechniqueAttempt,
    TechniqueAttemptControlKind,
    TextDelta,
    ToolResult,
    ToolSpec,
)
from merlin_storage_postgres import (
    Database,
    DatabaseConfig,
    TechniqueAttemptRepository,
)
from merlin_tui.bootstrap import build_backend

AUTHOR_RESPONSE = json.dumps(
    {
        "max_steps": 2,
        "steps": [
            {
                "step_id": "exchange",
                "operation": "browser_chat_exchange",
                "arguments": {"prompt": "bounded fixture value"},
                "evidence_keys": ["request", "response"],
            },
            {
                "step_id": "cleanup",
                "operation": "browser_cleanup",
                "arguments": {"mode": "reviewed"},
                "cleanup": True,
            },
        ],
    }
)
AUDITOR_RESPONSE = json.dumps(
    {
        "decision": "accept",
        "reasons": ["bounded declarative flow matches the request"],
    }
)


class _StubProvider:
    """Answer the offline author and auditor prompts with deterministic JSON."""

    roles: list[str] = []
    author_prompts: list[str] = []

    def __init__(self, config) -> None:
        self.config = config

    async def stream(self, request):
        system = request.messages[0].content
        if "automation author" in system:
            type(self).roles.append("author")
            type(self).author_prompts.append(system)
            yield TextDelta(text=AUTHOR_RESPONSE)
        elif "automation auditor" in system:
            type(self).roles.append("auditor")
            yield TextDelta(text=AUDITOR_RESPONSE)
        else:  # pragma: no cover - the closure only prompts these two roles
            raise AssertionError(f"unexpected automation prompt: {system}")


class _ReviewedExchangeTool:
    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="browser_chat_exchange",
            description="Send one reviewed chat exchange",
            input_schema={"type": "object", "properties": {"prompt": {"type": "string"}}},
        )

    async def invoke(self, context, arguments) -> ToolResult:
        return ToolResult(
            content="exchange",
            metadata={
                "request": f"evidence:request:{context.call_id}",
                "response": f"evidence:response:{context.call_id}",
                "effect_state": "none",
            },
        )


class _ReviewedCleanupTool:
    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="browser_cleanup",
            description="Restore the reviewed browser session",
            input_schema={"type": "object", "properties": {"mode": {"type": "string"}}},
        )

    async def invoke(self, context, arguments) -> ToolResult:
        return ToolResult(content="cleanup", metadata={"effect_state": "none"})


class _InternalOwnedSessionCleanupTool:
    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="browser_end_owned_session",
            description="Internal reconnaissance cleanup only",
            input_schema={"type": "object", "properties": {}},
        )

    async def invoke(self, context, arguments) -> ToolResult:
        raise AssertionError("campaign automation must never invoke the internal cleanup tool")


@pytest.mark.asyncio
async def test_bootstrap_automation_closure_runs_author_fixture_audit_admission_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _StubProvider.roles = []
    _StubProvider.author_prompts = []

    def browser_tool_stubs(*_args, **_kwargs):
        return (
            _ReviewedExchangeTool(),
            _ReviewedCleanupTool(),
            _InternalOwnedSessionCleanupTool(),
        )

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "permission_mode": "yolo",
                    "provider": {
                        "base_url": "http://provider.test/v1",
                        "model": "routine-model",
                    },
                },
            ),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda self: {"api_key": "secret"})
        monkeypatch.setattr("merlin_providers.OpenAICompatibleProvider", _StubProvider)
        monkeypatch.setattr("merlin_providers.CodexAppServerProvider", _StubProvider)
        monkeypatch.setattr("merlin_browser.browser_tools", browser_tool_stubs)
        backend = build_backend(tmp_path / "workspace")
        try:
            session_id = await backend.kernel.start()
            campaign_id = "campaign/automation-closure"
            attempt_id = "11111111-1111-4111-8111-111111111111"
            with database.connect() as connection:
                TechniqueAttemptRepository(connection).begin(
                    TechniqueAttempt(
                        id=attempt_id,
                        campaign_id=campaign_id,
                        wave=0,
                        task_id="worker:source-task",
                        technique_id="technique-automation",
                        strategy_id="direct",
                        control_kind=TechniqueAttemptControlKind.ATTACK,
                        oracle_digest="sha256:" + "a" * 64,
                    )
                )
            runner = backend._campaigns._automation_runner  # noqa: SLF001
            manifest = CampaignManifest.create(
                id=campaign_id,
                session_id=session_id,
                objective="Exercise the bootstrap automation closure",
                profile=CampaignProfile.RECOMMENDED,
                target=ReviewedTargetAdapter(
                    id="target-automation",
                    interface="browser",
                    endpoint="https://reviewed.example/chat",
                ),
                provider_id="test",
                provider_model="test",
                provider_effort="default",
                authority_ids=(),
                catalogue=(),
                skill_documents=(),
                tool_ids=("browser_chat_exchange", "browser_cleanup"),
                budget=AssignmentBudget(),
            )
            request = LeadAutomationRequest(
                request_id="automation-request-closure",
                owning_attempt_id=attempt_id,
                objective="Send one bounded exchange and restore the session",
                artifact_kind="declarative_browser_flow",
                allowed_operations=("browser_chat_exchange", "browser_cleanup"),
                evidence_expectations=("response",),
                cleanup_expectations=("session restored",),
                source_task_ids=("worker:source-task",),
            )

            outcome = await runner(manifest, request)
            with pytest.raises(ValueError, match="unknown operations: browser_end_owned_session"):
                await runner(
                    manifest,
                    LeadAutomationRequest(
                        request_id="automation-request-internal-cleanup",
                        owning_attempt_id=attempt_id,
                        objective="Attempt to obtain internal cleanup authority",
                        artifact_kind="declarative_browser_flow",
                        allowed_operations=("browser_end_owned_session",),
                        evidence_expectations=("cleanup acknowledgement",),
                        cleanup_expectations=("session ended",),
                        source_task_ids=("worker:source-task",),
                    ),
                )
        finally:
            await backend.close()

    assert _StubProvider.roles == ["author", "auditor"]
    (author_prompt,) = _StubProvider.author_prompts
    assert "sleep/wait/delay/pause" in author_prompt
    assert "until, wait_for, condition, observe, selector, or" in author_prompt
    assert "fill/click" in author_prompt
    assert "{primary,fallbacks}" in author_prompt
    assert "at least two selectors" in author_prompt
    assert "browser_end_owned_session" not in author_prompt
    assert outcome.state == "completed", outcome.error
    assert outcome.stage == "execution"
    assert outcome.artifact_digest is not None
    assert outcome.artifact_digest.startswith("sha256:")
    assert outcome.run is not None
    assert [event.step_id for event in outcome.run.events] == ["exchange", "cleanup"]
    assert all(event.status == "completed" for event in outcome.run.events)
    assert any(ref.startswith("evidence:response:automation:") for ref in outcome.run.evidence_refs)
