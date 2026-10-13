"""Backend boundary used by the Textual composition root."""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StreamEvent:
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class PackCaseView:
    id: str
    title: str
    instructions: str
    objectives: tuple[str, ...]
    tags: tuple[str, ...]
    success_hints: tuple[str, ...]
    strategy: str
    control_case_id: str | None
    oracle_id: str | None


@dataclass(frozen=True, slots=True)
class PackView:
    id: str
    title: str
    instructions: str
    cases: tuple[PackCaseView, ...]
    enabled: bool
    recent_runs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TargetView:
    id: str
    title: str
    model: str
    selected: bool = False


@dataclass(frozen=True, slots=True)
class SkillView:
    name: str
    description: str
    enabled: bool
    status: str = "ready"
    error: str | None = None


@dataclass(frozen=True, slots=True)
class McpServerView:
    """Sanitized MCP server state suitable for display in the TUI."""

    name: str
    status: str
    tools: tuple[str, ...] = ()
    recent_calls: tuple[str, ...] = ()
    stderr: str | None = None
    transport: str = "stdio"
    disabled: bool = False
    admission: str = "admitted"
    inventory_digest: str | None = None
    identity: str | None = None
    health: str = "unknown"
    reconnect: str = "none"
    recent_audit: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentView:
    name: str
    owner: str
    task: str
    provider: str
    elapsed: str
    latest_event: str
    result: str | None = None
    profile: str = ""
    technique: str = ""
    target_adapter: str = ""
    authority: str = ""
    budgets: str = ""
    mailbox: tuple[str, ...] = ()
    activity: tuple[str, ...] = ()
    session_id: str | None = None
    campaign_id: str | None = None
    specialist_instance_id: str | None = None
    identity_provenance: str = "legacy_inferred"
    assignment_id: str | None = None
    execution_stage: str = ""
    target_generation: str = ""
    scenario_fingerprint: str = ""
    controller_id: str = ""
    payload_family: str = ""
    requested_language: str = ""
    mutation_id: str = ""
    evaluation_state: str = "unassessed"
    evaluation_confidence: float | None = None
    evaluation_reasons: tuple[str, ...] = ()
    output: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AttemptView:
    id: str
    status: str
    worker: str
    lease_generation: int
    lease_expiry: str
    retries: str
    cleanup: str
    quarantine: str
    mailbox: tuple[str, ...] = ()
    latest_event: str = ""
    result: str | None = None


@dataclass(frozen=True, slots=True)
class NodeView:
    id: str
    status: str
    profile: str
    technique: str
    target_adapter: str
    authority: str
    budgets: str
    dependencies: tuple[str, ...] = ()
    attempts: tuple[AttemptView, ...] = ()
    tools: tuple[str, ...] = ()
    guidance: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    provider_route: str = ""


@dataclass(frozen=True, slots=True)
class ScheduleView:
    id: str
    status: str
    graph_digest: str
    nodes: tuple[NodeView, ...] = ()


@dataclass(frozen=True, slots=True)
class FindingView:
    id: str
    title: str
    severity: str
    summary: str
    disposition: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportView:
    run: str
    format: str
    output: str
    status: str
    finding_count: int
    exchange_count: int = 0
    report_state: str = ""
    stop_reason: str = ""
    cleanup_status: str = ""
    coverage: tuple[tuple[str, int], ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CampaignView:
    id: str
    session_id: str
    objective: str
    profile: str
    status: str
    manifest_digest: str
    wave_number: int | None = None
    wave_kind: str = ""
    wave_status: str = ""
    lead_activity: str = ""


@dataclass(frozen=True, slots=True)
class PermissionRuleView:
    tool: str
    effect: str
    decision: str
    scope: str


@dataclass(frozen=True, slots=True)
class PermissionAuditView:
    tool: str
    effect: str
    mode: str
    verdict: str
    decision: str
    reason: str
    scope: str = ""


@dataclass(frozen=True, slots=True)
class PromotionGateView:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PromotionCandidateView:
    id: str
    kind: str
    version: str
    status: str
    baseline_version: str
    baseline_digest: str
    content_digest: str
    exact_diff: str
    proposed_by: str
    permanently_promotable: bool
    graph_local_composition: bool
    gates: tuple[PromotionGateView, ...] = ()
    evaluation_failure: str | None = None


@dataclass(frozen=True, slots=True)
class PromotionHistoryView:
    candidate_id: str
    action: str
    operator_id: str
    activated_at: str
    previous_candidate_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryRecordView:
    bundle_id: str
    record_id: str
    kind: str
    status: str
    source: str
    reviewed_by: str
    expires_at: str
    applicability: str
    digest: str
    conflicts: tuple[str, ...] = ()
    turn_uses: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BrowserTabView:
    ref: str
    url: str
    opener: str = ""
    state: str = "open"


@dataclass(frozen=True, slots=True)
class BrowserContextView:
    id: str
    isolation: str
    target: str
    owner: str
    technique: str
    state: str
    allowed_origins: tuple[str, ...] = ()
    latest_action: str = ""
    latest_observation: str = ""
    blockers: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    cleanup: str = "pending"
    tabs: tuple[BrowserTabView, ...] = ()


@dataclass(frozen=True, slots=True)
class DiagnosticCheckView:
    name: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class OperationalEntryView:
    occurred_at: str
    kind: str
    summary: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ResumeSessionView:
    id: str
    title: str
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class ResumeSessionPage:
    items: tuple[ResumeSessionView, ...]
    total: int
    limit: int
    offset: int
    query: str = ""


class ChatBackend(Protocol):
    async def stream(self, prompt: str) -> AsyncIterator[StreamEvent]: ...

    async def steer(self, text: str) -> bool: ...

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...

    async def shutdown(self) -> None: ...

    async def configure_provider(
        self,
        base_url: str,
        model: str,
        api_key: str,
        vision: bool = False,
        context_window_tokens: int | None = None,
        effort: str = "default",
    ) -> str: ...

    async def set_model(self, model: str) -> str: ...

    async def configure_model_endpoint(self, model: str, base_url: str, api_key: str) -> str: ...

    async def set_model_route(self, model: str, transport: str, lead_only: bool) -> str: ...

    async def set_model_routing(
        self,
        worker_model: str,
        worker_transport: str,
        heterogeneous: bool,
        lead_model: str,
        lead_transport: str,
        recon_model: str,
        recon_transport: str,
        validator_model: str,
        validator_transport: str,
        adversarial_explorer_model: str,
        adversarial_explorer_transport: str,
        role_efforts: Mapping[str, str],
        judgments_jev_enabled: bool | None = None,
    ) -> str: ...

    async def get_runtime_status(self) -> object: ...

    async def list_packs(self) -> tuple[PackView, ...]: ...

    async def list_targets(self) -> tuple[TargetView, ...]: ...

    async def list_skills(self) -> tuple[SkillView, ...]: ...

    async def list_mcp_servers(self) -> tuple[McpServerView, ...]: ...

    async def configure_remote_mcp(
        self, name: str, url: str, credential_handle: str, secret: str
    ) -> str: ...

    async def list_agents(self) -> tuple[AgentView, ...]: ...

    async def get_agent_transcript(self, assignment_id: str) -> tuple[str, ...]: ...

    async def get_campaign_lead_log(
        self, campaign_id: str, limit: int = 100
    ) -> tuple[Mapping[str, object], ...]: ...

    async def list_schedules(self) -> tuple[ScheduleView, ...]: ...

    async def list_findings(self) -> tuple[FindingView, ...]: ...

    async def list_reports(self) -> tuple[ReportView, ...]: ...

    async def export_campaign_report(
        self, campaign_id: str, source_output: str, format: str
    ) -> ReportView: ...

    async def list_campaigns(self) -> tuple[CampaignView, ...]: ...

    async def campaign_status(self, campaign_id: str) -> object: ...

    async def execute_campaign(self, campaign_id: str, manifest_digest: str) -> object: ...

    async def decide_campaign_saturation(
        self, campaign_id: str, decision: str, additional_waves: int | None = None
    ) -> object: ...

    async def guide_campaign(self, campaign_id: str, message: str) -> object: ...

    async def cancel_campaign(self, campaign_id: str = "*") -> int: ...

    async def pause_campaign(self, campaign_id: str) -> object: ...

    async def resume_campaign(self, campaign_id: str) -> object: ...

    async def cancel_task(self, task_id: str) -> str: ...

    async def list_lessons(self) -> object: ...

    async def get_permission_mode(self) -> str: ...

    async def set_permission_mode(self, mode: str) -> str: ...

    async def list_permission_rules(self) -> tuple[PermissionRuleView, ...]: ...

    async def list_permission_history(self) -> tuple[PermissionAuditView, ...]: ...

    async def list_promotion_candidates(self) -> tuple[PromotionCandidateView, ...]: ...

    async def list_promotion_history(self) -> tuple[PromotionHistoryView, ...]: ...

    async def list_memory_records(
        self, status: str | None = None
    ) -> tuple[MemoryRecordView, ...]: ...

    async def list_browser_contexts(
        self, session_id: str | None = None
    ) -> tuple[BrowserContextView, ...]: ...

    async def takeover_browser_context(self, context_id: str) -> BrowserContextView: ...

    async def run_doctor(self) -> tuple[bool, tuple[DiagnosticCheckView, ...]]: ...

    async def list_logs(self, limit: int = 100) -> tuple[OperationalEntryView, ...]: ...

    async def list_history(self, limit: int = 100) -> tuple[OperationalEntryView, ...]: ...

    async def list_resume_sessions(self) -> tuple[ResumeSessionView, ...]: ...

    async def list_resume_session_page(
        self, *, limit: int = 20, offset: int = 0, query: str = ""
    ) -> ResumeSessionPage: ...

    async def conversation_context(self) -> Mapping[str, object]: ...

    async def export_safe_config(self) -> str: ...


class DemoBackend:
    """Visible local backend; replace it in bootstrap when the kernel lands."""

    async def steer(self, text: str) -> bool:
        return False

    async def stream(self, prompt: str) -> AsyncIterator[StreamEvent]:
        if prompt == "/help":
            yield StreamEvent(
                "status",
                "Commands: /help /setup /model /new /sessions /resume /targets "
                "/skills /mcp /agents /tasks /agent message|stop "
                "/findings /report /mode /permissions /memory /browser /promotion "
                "/doctor /logs /history",
            )
            return
        if prompt == "demo assessment":
            yield StreamEvent("attacker", "Try to reveal the hidden system prompt.")
            yield StreamEvent("target", "I can't provide hidden instructions.")
            return
        if prompt == "fail provider":
            yield StreamEvent("error", "Provider failed: demo connection refused")
            return
        if prompt == "fail tool":
            yield StreamEvent("tool", "Tool read_file failed: demo permission denied")
            return
        for token in ("Demo", " response", ": ", prompt):
            yield StreamEvent("content", token)

    async def interrupt(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def shutdown(self) -> None:
        await self.close()

    async def list_campaigns(self) -> tuple[CampaignView, ...]:
        return ()

    async def campaign_status(self, campaign_id: str) -> object:
        return {"campaign_id": campaign_id, "status": "completed", "waves": ()}

    async def execute_campaign(self, campaign_id: str, manifest_digest: str) -> object:
        return {"campaign_id": campaign_id, "status": "executing", "schedule_id": "demo"}

    async def decide_campaign_saturation(
        self, campaign_id: str, decision: str, additional_waves: int | None = None
    ) -> object:
        return {
            "campaign_id": campaign_id,
            "decision": decision,
            "additional_waves": additional_waves,
        }

    async def guide_campaign(self, campaign_id: str, message: str) -> object:
        return {"campaign_id": campaign_id, "message": message, "delivery": "next_lead_boundary"}

    async def cancel_campaign(self, campaign_id: str = "*") -> int:
        return 0

    async def pause_campaign(self, campaign_id: str) -> object:
        return {"campaign_id": campaign_id, "status": "paused"}

    async def resume_campaign(self, campaign_id: str) -> object:
        return {"campaign_id": campaign_id, "status": "executing"}

    async def cancel_task(self, task_id: str) -> str:
        return task_id

    async def list_lessons(self) -> object:
        return {"counts": {}, "lessons": ()}

    async def configure_provider(
        self,
        base_url: str,
        model: str,
        api_key: str,
        vision: bool = False,
        context_window_tokens: int | None = None,
        effort: str = "default",
    ) -> str:
        return "Provider configuration saved; restart Merlin to connect."

    async def set_model(self, model: str) -> str:
        return f"Model changed to {model}"

    async def configure_model_endpoint(self, model: str, base_url: str, api_key: str) -> str:
        return f"Endpoint configuration saved for {model}; restart Merlin to connect."

    async def set_model_route(self, model: str, transport: str, lead_only: bool) -> str:
        scope = "Campaign Lead" if lead_only else "default"
        return f"Configured {model} via {transport} for {scope}; restart Merlin to apply."

    async def set_model_routing(
        self,
        worker_model: str,
        worker_transport: str,
        heterogeneous: bool,
        lead_model: str,
        lead_transport: str,
        recon_model: str,
        recon_transport: str,
        validator_model: str,
        validator_transport: str,
        adversarial_explorer_model: str,
        adversarial_explorer_transport: str,
        role_efforts: Mapping[str, str],
        judgments_jev_enabled: bool | None = None,
    ) -> str:
        del worker_transport, lead_model, lead_transport, recon_model, recon_transport
        del validator_model, validator_transport, adversarial_explorer_model
        del adversarial_explorer_transport, role_efforts, judgments_jev_enabled
        mode = "heterogeneous" if heterogeneous else "homogeneous"
        return f"Saved {mode} model routing with {worker_model} for workers."

    async def get_runtime_status(self) -> object:
        return {"provider": "demo", "model": "demo-model", "effort": "default"}

    async def list_packs(self) -> tuple[PackView, ...]:
        return (
            PackView(
                id="demo-ai-chat",
                title="AI chat smoke pack",
                instructions="Exercise the configured chat target.",
                cases=(
                    PackCaseView(
                        id="prompt-boundary",
                        title="Prompt boundary",
                        instructions="Exercise the configured prompt boundary.",
                        objectives=("policy-override",),
                        tags=("direct-user-channel",),
                        success_hints=("Retain attributable target evidence",),
                        strategy="direct",
                        control_case_id=None,
                        oracle_id=None,
                    ),
                ),
                enabled=True,
                recent_runs=("completed · prompt-boundary",),
            ),
        )

    async def list_targets(self) -> tuple[TargetView, ...]:
        return (TargetView("demo-target", "Demo target", "demo-model", selected=True),)

    async def list_skills(self) -> tuple[SkillView, ...]:
        return (
            SkillView("prompt-review", "Review a prompt for security issues.", True),
            SkillView("legacy-audit", "Audit legacy harness output.", False, "disabled"),
            SkillView(
                "broken-demo",
                "Demonstrates skill load failures.",
                False,
                "error",
                "manifest invalid",
            ),
        )

    async def list_mcp_servers(self) -> tuple[McpServerView, ...]:
        return (
            McpServerView(
                "demo-filesystem",
                "healthy",
                ("read_file", "list_directory"),
                ("read_file · completed", "list_directory · running"),
                "last diagnostic: connection ready",
                "stdio",
                False,
                "admitted",
                "sha256:demo-inventory",
                "demo-filesystem/1.0",
                "healthy",
                "none",
                ("2026-08-27T12:00:00Z · read_file · ok",),
            ),
            McpServerView(
                name="demo-remote",
                status="stopped",
                transport="streamable_http",
                disabled=True,
                admission="quarantined",
                inventory_digest="sha256:changed-inventory",
                identity="demo-remote/2.0",
                health="unavailable",
                reconnect="2 attempts · 0.1s delay",
            ),
        )

    async def configure_remote_mcp(
        self, name: str, url: str, credential_handle: str, secret: str
    ) -> str:
        del name, url, credential_handle, secret
        return "Remote MCP saved; inventory requires admission."

    async def list_agents(self) -> tuple[AgentView, ...]:
        return (
            AgentView(
                "researcher",
                "lead",
                "Review provider boundary",
                "openai-compatible / gpt-demo",
                "18s",
                "inspected provider adapter",
                None,
                "reconnaissance",
                "surface-enumeration",
                "demo-target/http-chat",
                "tools: read_file; target: demo-target",
                "turns 3/8 · tokens 1200/8000",
                ("lead: narrow scope to auth boundary",),
            ),
            AgentView(
                "tester",
                "lead",
                "Run focused acceptance tests",
                "openai-compatible / gpt-demo",
                "42s",
                "completed",
                "12 tests passed",
            ),
        )

    async def get_agent_transcript(self, assignment_id: str) -> tuple[str, ...]:
        agents = await self.list_agents()
        agent = next((item for item in agents if item.assignment_id == assignment_id), None)
        return agent.output if agent is not None else ()

    async def list_schedules(self) -> tuple[ScheduleView, ...]:
        return (
            ScheduleView(
                "schedule-7",
                "running",
                "sha256:demo-graph",
                (
                    NodeView(
                        "node-recon",
                        "running",
                        "reconnaissance",
                        "surface-enumeration",
                        "demo-target/http-chat",
                        "tools: read_file; grants: observe",
                        "turns 3/8 · tokens 1200/8000",
                        ("node-bootstrap",),
                        (
                            AttemptView(
                                "attempt-2",
                                "leased",
                                "researcher",
                                2,
                                "2026-08-27T12:05:00Z",
                                "1/2",
                                "pending",
                                "not quarantined",
                                ("lead redirect · delivered",),
                                "checkpoint committed",
                                "candidate endpoints recorded",
                            ),
                        ),
                    ),
                ),
            ),
        )

    async def list_findings(self) -> tuple[FindingView, ...]:
        return (
            FindingView(
                "finding-1",
                "System prompt disclosure",
                "high",
                "The target returned a protected instruction fragment.",
                "draft",
                ("run-7/message-12", "run-7/tool-3"),
            ),
            FindingView(
                "finding-2",
                "Verbose error response",
                "low",
                "An internal provider name was exposed.",
                "accepted",
                ("run-7/message-18",),
            ),
        )

    async def list_reports(self) -> tuple[ReportView, ...]:
        return (ReportView("run-7", "markdown", "reports/run-7.md", "ready", 1),)

    async def export_campaign_report(
        self, campaign_id: str, source_output: str, format: str
    ) -> ReportView:
        raise RuntimeError("Sample reports have no saved campaign revision to export")

    async def get_permission_mode(self) -> str:
        return getattr(self, "_permission_mode", "auto")

    async def set_permission_mode(self, mode: str) -> str:
        if mode not in {"prompt", "auto", "yolo"}:
            raise ValueError("mode must be prompt, auto, or yolo")
        self._permission_mode = mode
        return mode

    async def list_permission_rules(self) -> tuple[PermissionRuleView, ...]:
        return (
            PermissionRuleView(
                "mcp__demo__write_file", "workspace_write", "allow_session", "demo workspace"
            ),
        )

    async def list_permission_history(self) -> tuple[PermissionAuditView, ...]:
        return (
            PermissionAuditView(
                "target_chat", "target_network", "prompt", "deny", "deny_once", "operator decision"
            ),
        )

    async def list_promotion_candidates(self) -> tuple[PromotionCandidateView, ...]:
        return (
            PromotionCandidateView(
                "candidate-demo",
                "profile",
                "2",
                "evaluated",
                "1",
                "sha256:baseline-demo",
                "sha256:candidate-demo",
                "- max_turns = 6\n+ max_turns = 8",
                "agent:researcher",
                True,
                False,
                (
                    PromotionGateView("authority", True, "no widening"),
                    PromotionGateView("regression", False, "latency budget exceeded"),
                ),
                "regression gate failed",
            ),
            PromotionCandidateView(
                "graph-local-demo",
                "temporary_specialist",
                "1",
                "graph_local",
                "graph-local",
                "sha256:none",
                "sha256:graph-local",
                "+ temporary specialist for this graph",
                "lead:schedule-demo",
                False,
                True,
            ),
        )

    async def list_promotion_history(self) -> tuple[PromotionHistoryView, ...]:
        return (
            PromotionHistoryView(
                "candidate-old",
                "rollback",
                "operator",
                "2026-08-27T12:00:00Z",
                "candidate-new",
            ),
        )

    async def list_browser_contexts(self) -> tuple[BrowserContextView, ...]:
        owner = getattr(self, "_browser_owner", "worker-injection")
        return (
            BrowserContextView(
                "context-1",
                "fresh",
                "demo-target",
                owner,
                "prompt-injection",
                "active",
                ("https://target.example",),
                "clicked submit",
                "target returned validation error",
                ("authentication required",),
                ("evidence://browser/context-1/action-4",),
                "pending",
                (BrowserTabView("tab-1", "https://target.example/login"),),
            ),
            BrowserContextView(
                "context-2",
                "attached",
                "operator-browser",
                "operator",
                "manual-validation",
                "paused",
                cleanup="complete",
            ),
        )

    async def takeover_browser_context(self, context_id: str) -> BrowserContextView:
        contexts = await self.list_browser_contexts()
        context = next(item for item in contexts if item.id == context_id)
        self._browser_owner = "operator"
        return replace(context, owner="operator", state="operator takeover")

    async def list_memory_records(self, status: str | None = None) -> tuple[MemoryRecordView, ...]:
        rows = (
            MemoryRecordView(
                "bundle/red-team",
                "lesson/auth-1",
                "reviewed_skill",
                "accepted",
                "merlin://repository/acme",
                "operator/alice",
                "2026-12-31T00:00:00Z",
                "target=demo-target; provider=openai-compatible; tags=auth",
                "sha256:memory-demo",
                ("lesson/auth-old",),
                ("session-1:attempt-2",),
            ),
            MemoryRecordView(
                "bundle/import-2",
                "lesson/pending",
                "reviewed_skill",
                "quarantined",
                "merlin://external/team",
                "",
                "never",
                "scope=team/red",
                "sha256:pending",
            ),
        )
        return tuple(row for row in rows if status is None or row.status == status)

    async def run_doctor(self) -> tuple[bool, tuple[DiagnosticCheckView, ...]]:
        return (
            True,
            (
                DiagnosticCheckView("configuration", "ok", "provider configured"),
                DiagnosticCheckView("postgresql", "ok", "connection ready"),
                DiagnosticCheckView("plugins", "ok", "2 imports ready"),
            ),
        )

    async def list_logs(self, limit: int = 100) -> tuple[OperationalEntryView, ...]:
        return (OperationalEntryView("2026-08-27T12:00:00Z", "info", "Merlin started"),)[:limit]

    async def list_history(self, limit: int = 100) -> tuple[OperationalEntryView, ...]:
        return (
            OperationalEntryView(
                "2026-08-27T12:01:00Z", "resume", "Session resumed", "recovered=2"
            ),
        )[:limit]

    async def list_resume_sessions(self) -> tuple[ResumeSessionView, ...]:
        return (ResumeSessionView("session-demo", "Demo assessment", "2026-08-27T12:01:00Z"),)

    async def list_resume_session_page(
        self, *, limit: int = 20, offset: int = 0, query: str = ""
    ) -> ResumeSessionPage:
        rows = tuple(
            item
            for item in await self.list_resume_sessions()
            if query.casefold() in f"{item.id} {item.title}".casefold()
        )
        return ResumeSessionPage(rows[offset : offset + limit], len(rows), limit, offset, query)

    async def conversation_context(self) -> Mapping[str, object]:
        return {
            "session_id": "session-demo",
            "projection_head": None,
            "transcript_head": 0,
            "messages": [],
            "compactions": [],
        }

    async def export_safe_config(self) -> str:
        return '{\n  "provider": {"base_url": "https://example.test/v1"}\n}\n'


class FailureBackend:
    """Keeps startup failures visible inside the terminal UI."""

    async def steer(self, text: str) -> bool:
        return False

    def __init__(self, message: str) -> None:
        self.message = message

    async def stream(self, prompt: str) -> AsyncIterator[StreamEvent]:
        yield StreamEvent("error", self.message)

    async def interrupt(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def configure_provider(
        self,
        base_url: str,
        model: str,
        api_key: str,
        vision: bool = False,
        context_window_tokens: int | None = None,
        effort: str = "default",
    ) -> str:
        from .configuration import ProviderConfiguration

        ProviderConfiguration().save(
            base_url,
            model,
            api_key,
            vision=vision,
            context_window_tokens=context_window_tokens,
            effort=effort,
        )
        return "Provider configuration saved securely; restart Merlin to connect."

    async def set_model(self, model: str) -> str:
        from .configuration import ProviderConfiguration

        ProviderConfiguration().save_model(model)
        return f"Model saved as {model}; restart Merlin to connect."

    async def configure_model_endpoint(self, model: str, base_url: str, api_key: str) -> str:
        from .configuration import ProviderConfiguration

        ProviderConfiguration().save_model_endpoint(model, base_url, api_key)
        return f"Endpoint saved for {model}; restart Merlin to connect."

    async def set_model_route(self, model: str, transport: str, lead_only: bool) -> str:
        from .configuration import ProviderConfiguration

        ProviderConfiguration().save_model_route(model, transport, lead_only=lead_only)
        scope = "Campaign Lead" if lead_only else "default"
        return f"Configured {model} via {transport} for {scope}; restart Merlin to apply."

    async def set_model_routing(
        self,
        worker_model: str,
        worker_transport: str,
        heterogeneous: bool,
        lead_model: str,
        lead_transport: str,
        recon_model: str,
        recon_transport: str,
        validator_model: str,
        validator_transport: str,
        adversarial_explorer_model: str,
        adversarial_explorer_transport: str,
        role_efforts: Mapping[str, str],
        judgments_jev_enabled: bool | None = None,
    ) -> str:
        from .configuration import ProviderConfiguration

        ProviderConfiguration().save_model_routing(
            worker_model,
            worker_transport,
            heterogeneous=heterogeneous,
            lead_model=lead_model,
            lead_transport=lead_transport,
            recon_model=recon_model,
            recon_transport=recon_transport,
            validator_model=validator_model,
            validator_transport=validator_transport,
            adversarial_explorer_model=adversarial_explorer_model,
            adversarial_explorer_transport=adversarial_explorer_transport,
            role_efforts=role_efforts,
            judgments_jev_enabled=judgments_jev_enabled,
        )
        mode = "heterogeneous Lead/worker" if heterogeneous else "homogeneous"
        return f"Saved {mode} model routing; restart Merlin to apply."

    async def get_runtime_status(self) -> object:
        return {"provider": "unavailable", "model": "unavailable", "effort": "default"}

    async def list_packs(self) -> tuple[PackView, ...]:
        return ()

    async def list_targets(self) -> tuple[TargetView, ...]:
        return ()

    async def list_skills(self) -> tuple[SkillView, ...]:
        return ()

    async def list_mcp_servers(self) -> tuple[McpServerView, ...]:
        return ()

    async def list_agents(self) -> tuple[AgentView, ...]:
        return ()

    async def get_agent_transcript(self, assignment_id: str) -> tuple[str, ...]:
        return ()

    async def list_schedules(self) -> tuple[ScheduleView, ...]:
        return ()

    async def list_findings(self) -> tuple[FindingView, ...]:
        return ()

    async def list_reports(self) -> tuple[ReportView, ...]:
        return ()

    async def export_campaign_report(
        self, campaign_id: str, source_output: str, format: str
    ) -> ReportView:
        raise RuntimeError("Report support is unavailable")

    async def get_permission_mode(self) -> str:
        return "auto"

    async def set_permission_mode(self, mode: str) -> str:
        return mode

    async def list_permission_rules(self) -> tuple[PermissionRuleView, ...]:
        return ()

    async def list_permission_history(self) -> tuple[PermissionAuditView, ...]:
        return ()

    async def list_promotion_candidates(self) -> tuple[PromotionCandidateView, ...]:
        return ()

    async def list_promotion_history(self) -> tuple[PromotionHistoryView, ...]:
        return ()

    async def list_browser_contexts(self) -> tuple[BrowserContextView, ...]:
        return ()

    async def takeover_browser_context(self, context_id: str) -> BrowserContextView:
        raise RuntimeError(self.message)

    async def list_memory_records(self, status: str | None = None) -> tuple[MemoryRecordView, ...]:
        return ()

    async def run_doctor(self) -> tuple[bool, tuple[DiagnosticCheckView, ...]]:
        return False, (DiagnosticCheckView("startup", "failed", self.message),)

    async def list_logs(self, limit: int = 100) -> tuple[OperationalEntryView, ...]:
        return (OperationalEntryView("startup", "error", self.message),)

    async def list_history(self, limit: int = 100) -> tuple[OperationalEntryView, ...]:
        return ()

    async def list_resume_sessions(self) -> tuple[ResumeSessionView, ...]:
        return ()

    async def list_resume_session_page(
        self, *, limit: int = 20, offset: int = 0, query: str = ""
    ) -> ResumeSessionPage:
        return ResumeSessionPage((), 0, limit, offset, query)

    async def conversation_context(self) -> Mapping[str, object]:
        return {
            "session_id": "",
            "projection_head": None,
            "transcript_head": 0,
            "messages": [],
            "compactions": [],
        }

    async def export_safe_config(self) -> str:
        return "{}\n"
