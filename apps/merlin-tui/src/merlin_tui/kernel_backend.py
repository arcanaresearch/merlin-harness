from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from merlin_agents.swarm_snapshot import SwarmSnapshot
from merlin_config import Config
from merlin_contracts import AppEvent
from merlin_contracts.user_input import ANSWER_PREFIX, InputRequest, InputResponse, pending_input
from merlin_kernel import Kernel
from merlin_readiness import ResourceCloser
from merlin_readiness import sanitize as sanitize_readiness

from .backend import (
    AgentView,
    AttemptView,
    BrowserContextView,
    BrowserTabView,
    CampaignView,
    DiagnosticCheckView,
    FindingView,
    McpServerView,
    MemoryRecordView,
    NodeView,
    OperationalEntryView,
    PackCaseView,
    PackView,
    PermissionAuditView,
    PermissionRuleView,
    PromotionCandidateView,
    PromotionGateView,
    PromotionHistoryView,
    ReportView,
    ResumeSessionPage,
    ResumeSessionView,
    ScheduleView,
    SkillView,
    StreamEvent,
    TargetView,
)
from .configuration import ProviderConfiguration

_CAMPAIGN_PROFILE_PROMPT = (
    "Choose the campaign profile:\n"
    "1. Recommended [default]\n"
    "   Representative applicable coverage · usually 1–3 hours\n"
    "2. Quickscan\n"
    "   Fast smoke pass · bounded hypotheses, review-only validation · usually under an hour\n"
    "3. Exhaustive\n"
    "   Every technically executable technique · often several hours\n"
    "4. Exploration / learning\n"
    "   Discovery-led hypotheses and adaptive testing · duration varies\n"
    "Times are estimates; target limits, model speed, and retries can extend a scan.\n"
    "Press Enter or 1 for Recommended · 2 for Quickscan · 3 for Exhaustive · 4 for Exploration"
)

_ROUTE_PREFLIGHT_ACTIONS = (
    "Campaign route preflight failed.\n1. Configure routes (/model)\n2. Retry preflight\n3. Cancel"
)

RoutePreflight = Callable[[str], Awaitable[tuple[bool, str]]]


def _campaign_choice_composition(prompt: str) -> str | None:
    """Recognize the composition annotation emitted by the numbered TUI choice."""
    _profile, separator, annotation = prompt.strip().partition("\n")
    if not separator:
        return ""
    for composition in ("opinionated", "autonomous"):
        expected = (
            f"The operator explicitly selected specialist composition: {composition} "
            "(orthogonal to the coverage profile); pass "
            f"specialist_composition={composition!r} to assessment_prepare and do not "
            "ask again."
        )
        if annotation.strip() == expected:
            return "\n" + expected
    return None


def _campaign_profile_selection(prompt: str) -> str | None:
    if _campaign_choice_composition(prompt) is None:
        return None
    normalized = " ".join(prompt.strip().split("\n", 1)[0].lower().split()).rstrip(".")
    if normalized in {"", "1", "recommended"}:
        return "recommended"
    if normalized in {"3", "exhaustive"}:
        return "exhaustive"
    if normalized in {"4", "exploration", "exploration / learning", "learning"}:
        return "exploration"
    if normalized in {"2", "quickscan", "quick scan", "quick"}:
        return "quickscan"
    return None


def _needs_campaign_profile(prompt: str) -> bool:
    normalized = " ".join(prompt.lower().split())
    if normalized.startswith("/") or "campaign profile" in normalized:
        return False
    requests_assessment = any(
        marker in normalized
        for marker in (
            "merlin scan",
            "red pen scan",
            "redpen scan",
            "red-team scan",
            "red team scan",
            "red-team assessment",
            "red team assessment",
            "security assessment",
            "security evaluation",
        )
    )
    return requests_assessment and any(
        marker in normalized for marker in ("http://", "https://", "target")
    )


class KernelBackend:
    """Adapts the kernel's event stream to the small UI rendering contract."""

    def __init__(
        self,
        kernel: Kernel,
        resources: Sequence[Any] = (),
        *,
        settings: KernelSettings | None = None,
        configuration: ProviderConfiguration | None = None,
        assessments: Any | None = None,
        skills: Any | None = None,
        mcp: Any | None = None,
        agents: Any | None = None,
        agent_store: Any | None = None,
        findings: Any | None = None,
        readiness: Any | None = None,
        permissions: Any | None = None,
        promotions: Any | None = None,
        browser: Any | None = None,
        memory: Any | None = None,
        campaigns: Any | None = None,
        lifecycle_recovery: Any | None = None,
        report_repository: Any | None = None,
        route_preflight: RoutePreflight | None = None,
        operator_controls: bool = False,
    ) -> None:
        self.kernel = kernel
        self._resources = ResourceCloser(
            *(resource for resource in resources if resource is not mcp)
        )
        self._closed = False
        self._settings = settings
        self._configuration = configuration or ProviderConfiguration()
        self._assessments = assessments
        self._skills = skills
        self._mcp = mcp
        self._mcp_started = False
        self._mcp_closed = False
        self._agents = agents
        self._agent_store = agent_store
        self._agents_recovered = False
        self._campaigns_recovered = False
        self._findings = findings
        self._readiness = readiness
        self._report_history: list[ReportView] = []
        self._permissions = permissions
        self._promotions = promotions
        self._browser = browser
        self._memory = memory
        self._campaigns = campaigns
        self._lifecycle_recovery = lifecycle_recovery
        self._report_repository = report_repository
        self._route_preflight = route_preflight
        self._operator_controls = operator_controls
        self._pending_campaign_objective: str | None = None
        self._pending_route_preflight: tuple[str, str, str] | None = None
        self._pending_user_input: InputRequest | None = None
        self._config_reload_status: Mapping[str, object] = {
            "applied": (),
            "restart_required": (),
        }

    def _composition_instruction(self) -> str:
        """Chat-path specialist-composition selection from operator configuration."""
        if self._settings is None:
            return ""
        composition = self._settings.config.specialist_composition
        if composition == "opinionated":
            return ""
        return (
            f"\nThe operator explicitly selected specialist composition: {composition} "
            "(orthogonal to the coverage profile); pass "
            f"specialist_composition={composition!r} to assessment_prepare; do not ask again."
        )

    async def reload_configuration(self, newer: Config) -> Mapping[str, object]:
        """Apply only declared reload-safe settings and retain restart-required drift."""
        if self._settings is None:
            return self._config_reload_status
        current = self._settings.config
        reloadable, restart = current.changed_settings(newer)
        if "permission_mode" in reloadable and self._permissions is not None:
            await self.set_permission_mode(str(newer.values.get("permission_mode", "auto")))
        self._settings.apply_reload(newer, reloadable)
        self._config_reload_status = {
            "applied": tuple(sorted(reloadable)),
            "restart_required": tuple(sorted(restart)),
        }
        return self._config_reload_status

    def configuration_reload_failed(self, error: BaseException) -> None:
        """Record a redacted failure while retaining the last valid configuration."""
        self._config_reload_status = {
            "applied": (),
            "restart_required": self._config_reload_status.get("restart_required", ()),
            "error": type(error).__name__,
        }

    @property
    def config_reload_status(self) -> Mapping[str, object]:
        return self._config_reload_status

    async def stream(self, prompt: str) -> AsyncIterator[StreamEvent]:
        new_session = prompt.strip().lower() == "/new"
        if prompt.strip().lower() == "/interrupt":
            await self.interrupt()
            yield StreamEvent(kind="status", text="Foreground interruption acknowledged.")
            return
        durable_pending = None
        if hasattr(self.kernel, "_store") and self.kernel.session_id:
            durable_pending = pending_input(
                await self.kernel._store.load_messages(self.kernel.session_id)
            )
        pending = durable_pending or self._pending_user_input
        if prompt.startswith(ANSWER_PREFIX):
            if pending is None:
                raise ValueError("This question is no longer pending; refresh before answering")
            response = InputResponse.model_validate_json(prompt[len(ANSWER_PREFIX) :])
            response.validate_for(pending)
        campaign_selection = False
        if prompt.strip() == "/new":
            self._pending_campaign_objective = None
            self._pending_route_preflight = None
            self._pending_user_input = None
            if self._settings is not None:
                self._settings.reset_runtime_model()
        elif self._pending_route_preflight is not None:
            objective, pending_profile, composition_instruction = self._pending_route_preflight
            choice = " ".join(prompt.lower().split()).rstrip(".")
            if choice in {"1", "configure", "configure routes"}:
                yield StreamEvent(
                    kind="content",
                    text=(
                        "Open /model, save Lead and Worker routes together, then choose 2 to retry."
                    ),
                )
                return
            if choice in {"3", "cancel"}:
                self._pending_route_preflight = None
                self._pending_campaign_objective = None
                yield StreamEvent(
                    kind="content",
                    text="Campaign start cancelled before reconnaissance.",
                )
                return
            if choice not in {"2", "retry", "retry preflight"}:
                yield StreamEvent(kind="content", text=_ROUTE_PREFLIGHT_ACTIONS)
                return
            assert self._route_preflight is not None
            ready, report = await self._route_preflight(pending_profile)
            yield StreamEvent(kind="content", text=report)
            if not ready:
                yield StreamEvent(kind="content", text=_ROUTE_PREFLIGHT_ACTIONS)
                return
            campaign_selection = True
            if self._settings is not None:
                self._settings.use_role_model("campaign-lead", "lead")
            prompt = (
                f"{objective}\nThe operator explicitly selected campaign profile: "
                f"{pending_profile}; "
                "do not ask for the profile again."
                f"{composition_instruction}"
                " Pass the operator objective text verbatim as the assessment_prepare "
                "objective: do not reword, paraphrase, or substitute the target line; "
                "Merlin anchors the reviewed target itself."
            )
        elif self._pending_campaign_objective is not None and (
            _campaign_profile_selection(prompt) is not None or not self._operator_controls
        ):
            profile = _campaign_profile_selection(prompt)
            if profile is None:
                yield StreamEvent(kind="content", text=_CAMPAIGN_PROFILE_PROMPT)
                return
            composition_instruction = (
                _campaign_choice_composition(prompt) or self._composition_instruction()
            )
            objective = self._pending_campaign_objective
            campaign_selection = True
            if self._route_preflight is not None:
                ready, report = await self._route_preflight(profile)
                yield StreamEvent(kind="content", text=report)
                if not ready:
                    self._pending_route_preflight = (objective, profile, composition_instruction)
                    yield StreamEvent(kind="content", text=_ROUTE_PREFLIGHT_ACTIONS)
                    return
            if self._settings is not None:
                self._settings.use_role_model("campaign-lead", "lead")
            prompt = (
                f"{objective}\nThe operator explicitly selected campaign profile: {profile}; "
                "do not ask for the profile again."
                f"{composition_instruction}"
                " Pass the operator objective text verbatim as the assessment_prepare "
                "objective: do not reword, paraphrase, or substitute the target line; "
                "Merlin anchors the reviewed target itself."
            )
        elif _needs_campaign_profile(prompt) and not self._operator_controls:
            self._pending_campaign_objective = prompt
            yield StreamEvent(kind="content", text=_CAMPAIGN_PROFILE_PROMPT)
            return
        if (
            self._operator_controls
            and self._pending_campaign_objective is not None
            and not campaign_selection
        ):
            # A free-form correction belongs to the model, not a numeric gate.
            prompt = f"Pending assessment objective: {self._pending_campaign_objective}\n{prompt}"
            self._pending_campaign_objective = None
        if not new_session:
            await self._recover_agents()
            if self._mcp is not None and not self._mcp_started:
                await self._mcp.start_configured()
                self._mcp_started = True
        if campaign_selection and self.kernel.turn_active:
            yield StreamEvent(
                kind="status",
                text=(
                    "Waiting for the active response to finish; "
                    "your request and choices are retained."
                ),
            )
            await self.kernel.wait_for_turn()
        await self.kernel.submit(prompt)
        if pending is not None and not prompt.startswith("/"):
            self._pending_user_input = None
            yield StreamEvent("user_input_resolved", pending.request_id)
        if campaign_selection:
            self._pending_campaign_objective = None
            self._pending_route_preflight = None
        review_recovery_requested = False
        preparation_failed = False
        review_seen = False
        while True:
            try:
                async with asyncio.timeout(0.25 if not self.kernel.turn_active else None):
                    event = await self.kernel.next_event()
            except TimeoutError:
                break
            rendered = event_to_stream(event)
            if event.type == "tool":
                tool_name = event.payload.get("name")
                if tool_name in {"assessment_prepare", "assessment_execute"}:
                    review_recovery_requested = True
                if tool_name == "assessment_prepare":
                    preparation_failed |= event.payload.get("state") == "failed"
            if rendered is not None and rendered.kind == "campaign_review":
                review_seen = True
            if rendered is not None and rendered.kind == "user_input":
                self._pending_user_input = InputRequest.model_validate_json(rendered.text)
            if event.type == "report" and event.payload.get("state") == "completed":
                self._remember_report(event)
            if rendered is not None:
                yield rendered
            await asyncio.sleep(0)
        if review_recovery_requested and not review_seen:
            # The model-facing preparation receipt intentionally omits the full
            # review, and some provider event paths do not retain the tool
            # metadata that normally carries it. Recover only a sealed review in
            # this session; never infer approval from prose or execute here.
            for campaign in await self.list_campaigns():
                if campaign.status != "prepared" or campaign.session_id != self.kernel.session_id:
                    continue
                review = await self._saved_campaign_review(campaign.id, campaign.manifest_digest)
                if review is not None:
                    if preparation_failed:
                        yield StreamEvent(
                            "status",
                            "Preparation timed out, but its saved review was recovered. "
                            "Execution has not started; review and approve below.",
                        )
                    yield StreamEvent("campaign_review", json.dumps(review))
                    break
        values = prompt.strip().split(maxsplit=1)
        if (
            len(values) == 2
            and values[0].lower() == "/resume"
            and self.kernel.session_id == values[1]
        ):
            yield StreamEvent("status", f"Resumed session: {values[1]}")
            campaigns = await self.list_campaigns()
            if not campaigns:
                yield StreamEvent("status", "No campaign created in this session yet.")
            for campaign in campaigns:
                state = (
                    "Prepared · awaiting campaign confirmation"
                    if campaign.status == "prepared"
                    else campaign.status
                )
                yield StreamEvent(
                    "status", f"Campaign: {campaign.objective}\n{campaign.id} · {state}"
                )
                if campaign.status == "prepared":
                    review = await self._saved_campaign_review(
                        campaign.id, campaign.manifest_digest
                    )
                    if review is not None:
                        yield StreamEvent(
                            "status",
                            "Stage: reconnaissance complete; plan ready for review. "
                            "Execution has not started. Choose Approve below to start, "
                            "Request other changes to revise, or Cancel.",
                        )
                        yield StreamEvent("campaign_review", json.dumps(review))
                    else:
                        yield StreamEvent(
                            "status",
                            "Execution has not started. The saved review could "
                            "not be restored. Type: show the saved campaign review "
                            f"for {campaign.id}. Do not approve until it is displayed.",
                        )
            resumed, resume_failures = await self._resume_paused_session_campaigns(values[1])
            if resumed:
                yield StreamEvent(
                    kind="status",
                    text=(
                        f"Resumed {resumed} paused campaign"
                        f"{'s' if resumed != 1 else ''} from durable checkpoints."
                    ),
                )
            for campaign_id, error in resume_failures:
                yield StreamEvent(
                    kind="status",
                    text=f"Could not resume {campaign_id}: {error}",
                )

        if prompt.strip().startswith("/resume ") and hasattr(self.kernel, "_store"):
            pending = pending_input(await self.kernel._store.load_messages(self.kernel.session_id))
            if pending is not None:
                self._pending_user_input = pending
                yield StreamEvent("user_input", pending.model_dump_json())

    async def _saved_campaign_review(
        self, campaign_id: str, manifest_digest: str
    ) -> Mapping[str, object] | None:
        """Restore a review, falling back to the verified sealed campaign plan."""
        store = getattr(self.kernel, "_store", None)
        messages = (
            await store.load_messages(self.kernel.session_id)
            if hasattr(store, "load_messages")
            else ()
        )
        for message in reversed(messages):
            if message.role != "tool":
                continue
            try:
                # Automatic reconnaissance appends a durable continuation marker
                # after the tool's JSON. Restore the JSON document itself.
                payload, _ = json.JSONDecoder().raw_decode((message.content or "").lstrip())
            except (TypeError, ValueError):
                continue
            if (
                isinstance(payload, Mapping)
                and payload.get("campaign_id") == campaign_id
                and payload.get("manifest_digest") == manifest_digest
                and isinstance(payload.get("review"), Mapping)
            ):
                return payload
        # New preparations retain a structured review independently of the
        # broker-bounded tool transcript. Match both sealed identities on resume.
        if hasattr(store, "list_events"):
            for event in reversed(await store.list_events(self.kernel.session_id)):
                event_payload = event.get("payload", {})
                payload = (
                    event_payload.get("campaign_review")
                    if isinstance(event_payload, Mapping)
                    else None
                )
                if (
                    event.get("type") == "tool"
                    and isinstance(event_payload, Mapping)
                    and event_payload.get("name") == "assessment_prepare"
                    and event_payload.get("state") == "completed"
                    and isinstance(payload, Mapping)
                    and payload.get("campaign_id") == campaign_id
                    and payload.get("manifest_digest") == manifest_digest
                    and isinstance(payload.get("review"), Mapping)
                ):
                    return payload
        campaign_store = getattr(self._campaigns, "_store", None)
        if campaign_store is not None:
            from merlin_agents.campaign_review import recover_campaign_review

            row = await asyncio.to_thread(campaign_store.get, campaign_id)
            if row is not None:
                try:
                    return recover_campaign_review(
                        row,
                        session_id=self.kernel.session_id,
                        campaign_id=campaign_id,
                        manifest_digest=manifest_digest,
                    )
                except (KeyError, TypeError, ValueError):
                    # An unverifiable plan must never install approval controls.
                    return None
        return None

    async def steer(self, text: str) -> bool:
        """Queue operator input without replacing the active event stream."""
        if text.startswith(ANSWER_PREFIX):
            raise ValueError("This answer cannot be sent as guidance; wait for the current turn")
        return await self.kernel.steer(text)

    async def interrupt(self) -> None:
        await self.kernel.interrupt()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        try:
            await self.kernel.interrupt()
        except BaseException as exc:
            errors.append(exc)
        if self._campaigns is not None:
            try:
                rows = await self._campaigns.list_all()
            except BaseException as exc:
                errors.append(exc)
                rows = ()
            for row in rows:
                if (
                    str(row.get("session_id", "")) != self.kernel.session_id
                    or str(row["status"]) != "executing"
                ):
                    continue
                try:
                    await self._campaigns.pause(str(row["campaign_id"]))
                except ValueError:
                    # The supervisor may have reached a terminal state after
                    # the snapshot but before shutdown acquired the campaign.
                    continue
                except BaseException as exc:
                    # One failed durable transition must not leave the other
                    # locally owned supervisors and workers running.
                    errors.append(exc)
            detach_campaigns = getattr(self._campaigns, "detach", None)
            if callable(detach_campaigns):
                try:
                    await detach_campaigns()
                except BaseException as exc:
                    errors.append(exc)
        if self._agents is not None:
            detach_agents = getattr(self._agents, "detach", None)
            if callable(detach_agents):
                try:
                    await detach_agents()
                except BaseException as exc:
                    errors.append(exc)
        try:
            await self._close_mcp()
        except BaseException as exc:
            errors.append(exc)
        try:
            await self._resources.close()
        except ExceptionGroup as exc:
            errors.extend(exc.exceptions)
        if errors:
            raise ExceptionGroup("backend shutdown failed", errors)

    async def _close_mcp(self) -> None:
        if self._mcp is None or self._mcp_closed:
            return
        self._mcp_closed = True
        timeout = float(
            getattr(
                getattr(self._mcp, "tuning", None),
                "shutdown_timeout_seconds",
                10.0,
            )
        )
        async with asyncio.timeout(max(0.1, timeout)):
            await self._mcp.close()

    async def shutdown(self) -> None:
        """Orderly owner shutdown: interrupt work and close every owned resource."""
        await self.close()

    async def _resume_paused_session_campaigns(
        self, session_id: str
    ) -> tuple[int, tuple[tuple[str, str], ...]]:
        if self._campaigns is None:
            return 0, ()
        resumed = 0
        failures: list[tuple[str, str]] = []
        for row in await self._campaigns.list_all():
            if str(row["session_id"]) != session_id or str(row["status"]) != "paused":
                continue
            campaign_id = str(row["campaign_id"])
            try:
                await self._campaigns.resume(campaign_id)
            except (RuntimeError, ValueError) as error:
                failures.append((campaign_id, str(error)))
                continue
            resumed += 1
        return resumed, tuple(failures)

    async def detach(self) -> None:
        """Stop this daemon process without changing durable campaign state."""
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        if self._campaigns is not None:
            try:
                await self._campaigns.detach()
            except BaseException as exc:
                errors.append(exc)
        if self._agents is not None:
            try:
                await self._agents.detach()
            except BaseException as exc:
                errors.append(exc)
        try:
            await self._close_mcp()
        except BaseException as exc:
            errors.append(exc)
        try:
            await self._resources.close()
        except ExceptionGroup as exc:
            errors.extend(exc.exceptions)
        if errors:
            raise ExceptionGroup("backend detach failed", errors)

    async def configure_provider(
        self,
        base_url: str,
        model: str,
        api_key: str,
        vision: bool = False,
        context_window_tokens: int | None = None,
        effort: str = "default",
    ) -> str:
        await asyncio.to_thread(
            self._configuration.save,
            base_url,
            model,
            api_key,
            vision=vision,
            context_window_tokens=context_window_tokens,
            effort=effort,
        )
        return "Provider configuration saved securely; restart Merlin to reconnect."

    async def set_model(self, model: str) -> str:
        if not model.strip():
            raise ValueError("model is required")
        if self._settings is not None:
            await self._settings.set_model(model)
        else:
            await asyncio.to_thread(self._configuration.save_model, model)
        return f"Model changed to {model}"

    async def set_model_route(self, model: str, transport: str, lead_only: bool) -> str:
        await asyncio.to_thread(
            self._configuration.save_model_route, model, transport, lead_only=lead_only
        )
        scope = "Campaign Lead" if lead_only else "default"
        return f"Configured {model} via {transport} for {scope}; applies to the next turn."

    async def configure_model_endpoint(self, model: str, base_url: str, api_key: str) -> str:
        await asyncio.to_thread(self._configuration.save_model_endpoint, model, base_url, api_key)
        return f"Endpoint saved for {model}; applies to the next turn."

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
        await asyncio.to_thread(
            self._configuration.save_model_routing,
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
        return f"Saved {mode} model routing; applies to the next turn."

    async def get_runtime_status(self) -> Mapping[str, Any]:
        if self._settings is None:
            status: dict[str, Any] = {
                "provider": "unconfigured",
                "model": "unconfigured",
                "effort": "default",
            }
        else:
            status = dict(await self._settings.status())
        status["config_reload"] = dict(self._config_reload_status)
        return status

    async def list_packs(self) -> tuple[PackView, ...]:
        if self._assessments is None:
            return ()
        runs = (
            await self._assessments.list_runs(self.kernel.session_id)
            if self.kernel.session_id is not None
            else ()
        )
        views = []
        for item in await self._assessments.list_packs():
            detail = await self._assessments.inspect_pack(str(item["id"]))
            views.append(
                PackView(
                    id=str(item["id"]),
                    title=str(item["title"]),
                    instructions=str(detail["instructions"]),
                    cases=tuple(
                        PackCaseView(
                            id=str(case["id"]),
                            title=str(case["title"]),
                            instructions=str(case["instructions"]),
                            objectives=tuple(str(value) for value in case.get("objectives", ())),
                            tags=tuple(str(value) for value in case.get("tags", ())),
                            success_hints=tuple(
                                str(value) for value in case.get("success_hints", ())
                            ),
                            strategy=str(case.get("strategy_id", "direct")),
                            control_case_id=(
                                str(case["control_case_id"])
                                if case.get("control_case_id") is not None
                                else None
                            ),
                            oracle_id=(
                                str(case["oracle_id"])
                                if case.get("oracle_id") is not None
                                else None
                            ),
                        )
                        for case in detail["cases"]
                    ),
                    enabled=bool(item["enabled"]),
                    recent_runs=tuple(
                        f"{run['status']} · {run['case_id']}"
                        for run in runs
                        if run["pack_id"] == item["id"]
                    ),
                )
            )
        return tuple(views)

    async def list_targets(self) -> tuple[TargetView, ...]:
        if self._assessments is None:
            return ()
        return tuple(
            TargetView(
                id=str(item["id"]),
                title=str(item["title"]),
                model=str(item["model"]),
                selected=bool(item["selected"]),
            )
            for item in await self._assessments.list_targets()
        )

    async def list_skills(self) -> tuple[SkillView, ...]:
        if self._skills is None:
            return ()
        session_id = self.kernel.session_id or ""
        return tuple(
            SkillView(
                name=str(item["name"]),
                description=str(item["description"]),
                enabled=bool(item["enabled"]),
            )
            for item in await self._skills.list_skills(session_id)
        )

    async def list_mcp_servers(self) -> tuple[McpServerView, ...]:
        if self._mcp is None:
            return ()
        return tuple(
            McpServerView(
                name=str(item["name"]),
                status=str(item.get("status", "unknown")),
                tools=tuple(str(tool) for tool in item.get("tools", ())),
                recent_calls=tuple(str(call) for call in item.get("recent_calls", ())),
                stderr=str(item["stderr"]) if item.get("stderr") else None,
                transport=str(item.get("transport", "stdio")),
                disabled=bool(item.get("disabled", False)),
                admission=str(item.get("admission", "admitted")),
                inventory_digest=(
                    str(item["inventory_digest"]) if item.get("inventory_digest") else None
                ),
                identity=str(item["identity"]) if item.get("identity") else None,
                health=str(item.get("health", "unknown")),
                reconnect=_render_reconnect(item.get("reconnect")),
                recent_audit=tuple(
                    _render_mcp_audit(record)
                    for record in item.get("recent_call_audit", ())
                    if isinstance(record, Mapping)
                ),
            )
            for item in await self._mcp.list_servers()
        )

    async def configure_remote_mcp(
        self, name: str, url: str, credential_handle: str, secret: str
    ) -> str:
        if self._mcp is None:
            raise RuntimeError("MCP support is not configured")
        await self._mcp.add_remote_with_secret(name, url, credential_handle, secret)
        return "Remote MCP saved securely; review and admit its inventory digest."

    async def list_agents(self) -> tuple[AgentView, ...]:
        if self._agents is None:
            return ()
        session_id = self.kernel.session_id or ""
        snapshot_loader = getattr(self._agents, "list_swarm_snapshots", None)
        if callable(snapshot_loader):
            snapshots = tuple(await snapshot_loader(session_id))
            return await self._snapshot_agent_views(snapshots)
        return await self._legacy_agent_views(session_id)

    async def get_agent_transcript(self, assignment_id: str) -> tuple[str, ...]:
        loader = getattr(self._agents, "get_assignment_transcript", None)
        if not callable(loader):
            return ()
        return tuple(await loader(self.kernel.session_id or "", assignment_id))

    async def get_campaign_lead_log(
        self, campaign_id: str, limit: int = 100
    ) -> tuple[Mapping[str, object], ...]:
        if self._campaigns is None:
            raise RuntimeError("campaign support is not configured")
        await self._current_session_campaign(campaign_id)
        return tuple(await self._campaigns.lead_journal(campaign_id, limit=limit))

    async def _snapshot_agent_views(
        self, snapshots: Sequence[SwarmSnapshot]
    ) -> tuple[AgentView, ...]:
        return tuple(
            AgentView(
                name=(f"{specialist.specialist_instance_id}/{assignment.execution_stage}"),
                owner=(
                    assignment.current_attempt.owner
                    if assignment.current_attempt is not None
                    else "unassigned"
                ),
                task=assignment.objective or assignment.assignment_id,
                activity=assignment.activity,
                output=assignment.output,
                provider=assignment.provider_model,
                elapsed="durable history",
                latest_event=(
                    assignment.current_attempt.lifecycle.value
                    if assignment.current_attempt is not None
                    else assignment.lifecycle.value
                ),
                result=(
                    assignment.failure_reason
                    if assignment.current_attempt is not None
                    and assignment.current_attempt.lifecycle.value == "failed"
                    else assignment.successful_result.summary
                    if assignment.successful_result is not None
                    else None
                ),
                profile=specialist.display_name or "",
                technique=assignment.candidate.technique_id,
                target_adapter=assignment.candidate.target_id,
                authority=(
                    f"campaign={snapshot.campaign_id}"
                    if snapshot.campaign_id is not None
                    else "legacy-unscoped"
                ),
                budgets=f"schedule={assignment.schedule_id}",
                session_id=snapshot.session_id,
                campaign_id=snapshot.campaign_id,
                specialist_instance_id=specialist.specialist_instance_id,
                identity_provenance=specialist.identity_provenance.value,
                assignment_id=assignment.assignment_id,
                execution_stage=assignment.execution_stage,
                target_generation=assignment.candidate.target_generation,
                scenario_fingerprint=assignment.candidate.scenario_fingerprint,
                controller_id=assignment.candidate.controller_id,
                payload_family=assignment.candidate.payload_family,
                requested_language=assignment.candidate.requested_language,
                mutation_id=assignment.candidate.mutation_id,
                failure_kind=(
                    assignment.current_attempt.failure_kind
                    if assignment.current_attempt is not None
                    else None
                ),
                infrastructure_failure_category=(
                    assignment.current_attempt.infrastructure_failure_category
                    if assignment.current_attempt is not None
                    else None
                ),
                schedule_updated_at=assignment.schedule_updated_at,
                campaign_wave=assignment.campaign_wave,
                evaluation_state=assignment.candidate_evaluation.state.value,
                evaluation_confidence=(assignment.candidate_evaluation.evaluation_confidence),
                evaluation_reasons=assignment.candidate_evaluation.evaluation_reasons,
            )
            for snapshot in snapshots
            for specialist in snapshot.specialists
            for assignment in specialist.assignments
        )

    async def _legacy_agent_views(self, session_id: str) -> tuple[AgentView, ...]:
        """Compatibility path for non-durable test doubles and old plugins."""
        schedules = await self._agents.list_schedules(session_id)
        nodes = {
            node.node_id: (schedule, node) for schedule in schedules for node in schedule.nodes
        }
        attempts = {
            attempt.attempt_id: (schedule, attempt)
            for schedule in schedules
            for attempt in schedule.attempts
        }
        schedule_activity_loader = getattr(self._agents, "list_schedule_activity", None)
        schedule_mailbox_loader = getattr(self._agents, "list_schedule_mailbox", None)
        directive_loader = getattr(self._campaigns, "list_schedule_directives", None)
        activity: dict[tuple[str, str], tuple[str, ...]] = {}
        mailbox: dict[tuple[str, str], tuple[str, ...]] = {}
        if callable(schedule_activity_loader):

            async def empty_rows() -> Mapping[str, tuple[str, ...]]:
                return {}

            async def load_schedule_metadata(
                schedule: Any,
            ) -> tuple[
                Any,
                Mapping[str, tuple[str, ...]],
                Mapping[str, tuple[str, ...]],
                Mapping[str, tuple[str, ...]],
            ]:
                schedule_activity, schedule_mailbox, schedule_directives = await asyncio.gather(
                    schedule_activity_loader(schedule.schedule_id),
                    schedule_mailbox_loader(schedule.schedule_id)
                    if callable(schedule_mailbox_loader)
                    else empty_rows(),
                    directive_loader(schedule.schedule_id)
                    if callable(directive_loader)
                    else empty_rows(),
                )
                return schedule, schedule_activity, schedule_mailbox, schedule_directives

            metadata = await asyncio.gather(
                *(load_schedule_metadata(schedule) for schedule in schedules)
            )
            for schedule, schedule_activity, schedule_mailbox, schedule_directives in metadata:
                for node_id, rows in schedule_activity.items():
                    activity[(schedule.schedule_id, node_id)] = tuple(rows)
                for recipient, rows in schedule_mailbox.items():
                    mailbox[(schedule.schedule_id, recipient)] = tuple(rows)
                for node_id, rows in schedule_directives.items():
                    exact_node_id = node_id
                    if not any(node.node_id == exact_node_id for node in schedule.nodes):
                        matches = [
                            node.node_id
                            for node in schedule.nodes
                            if node.node_id.rsplit("/", 1)[-1] == node_id.removeprefix("worker:")
                        ]
                        if len(matches) == 1:
                            exact_node_id = matches[0]
                    key = (schedule.schedule_id, exact_node_id)
                    activity[key] = (*activity.get(key, ()), *tuple(rows))
        else:
            activity_loader = getattr(self._agents, "list_activity", None)
            active_activity_loader = getattr(self._agents, "list_active_activity", None)
            legacy_activity = (
                await active_activity_loader()
                if callable(active_activity_loader)
                else await activity_loader(session_id)
                if callable(activity_loader)
                else {}
            )
            for schedule in schedules:
                for node_id, rows in legacy_activity.items():
                    activity[(schedule.schedule_id, node_id)] = tuple(rows)
        provider = await self._settings.get_model() if self._settings is not None else ""
        views = []
        agents = await self._agents.list_agents(session_id)
        for agent in agents:
            schedule = None
            node = None
            attempt = None
            if agent.attempt_id in attempts:
                schedule, attempt = attempts[agent.attempt_id]
                node = nodes.get(attempt.node_id, (None, None))[1]
            elif agent.node_id in nodes:
                schedule, node = nodes[agent.node_id]
            views.append(
                AgentView(
                    name=agent.name,
                    owner=attempt.owner_id if attempt and attempt.owner_id else "lead",
                    task=node.objective if node else "unassigned",
                    provider=provider,
                    elapsed="current session",
                    latest_event=attempt.status if attempt else agent.status,
                    result=attempt.result if attempt else None,
                    profile=agent.profile_id or "",
                    technique=", ".join(node.technique_ids) if node else "",
                    target_adapter=node.target_profile_id if node else "",
                    authority=f"schedule={schedule.schedule_id}" if schedule else "",
                    budgets=node.budget_summary if node else "",
                    mailbox=tuple(
                        mailbox.get((schedule.schedule_id, agent.node_id or ""), ())
                        if schedule
                        else ()
                    ),
                    activity=tuple(
                        activity.get((schedule.schedule_id, agent.node_id or ""), ())
                        if schedule
                        else ()
                    ),
                )
            )
        return tuple(views)

    async def list_schedules(self) -> tuple[ScheduleView, ...]:
        if self._agents is None:
            return ()
        await self._recover_agents()
        session_id = self.kernel.session_id or ""
        schedules = await self._agents.list_schedules(session_id)
        return self._schedule_views(schedules)

    async def list_active_schedules(self) -> tuple[ScheduleView, ...]:
        """Project active schedules owned by the selected session."""
        return tuple(
            schedule
            for schedule in await self.list_schedules()
            if schedule.status not in {"completed", "failed", "cancelled", "quarantined"}
        )

    @staticmethod
    def _schedule_views(schedules: Sequence[Any]) -> tuple[ScheduleView, ...]:
        return tuple(
            ScheduleView(
                id=schedule.schedule_id,
                status=schedule.status,
                graph_digest=schedule.topology_digest,
                nodes=tuple(
                    NodeView(
                        id=node.node_id,
                        status=node.status,
                        profile=node.profile_id or "",
                        technique=", ".join(node.technique_ids),
                        target_adapter=node.target_profile_id or "",
                        authority=", ".join(node.authority_ids),
                        budgets=node.budget_summary,
                        tools=node.tool_names,
                        guidance=node.guidance_ids,
                        skills=node.skill_ids,
                        provider_route=node.provider_route or "",
                        dependencies=node.dependency_node_ids,
                        attempts=tuple(
                            AttemptView(
                                id=attempt.attempt_id,
                                status=attempt.status,
                                worker=attempt.owner_id or "unassigned",
                                lease_generation=attempt.lease_generation,
                                lease_expiry=(
                                    str(node.lease.expires_at_tick)
                                    if node.lease and node.lease.attempt_id == attempt.attempt_id
                                    else ""
                                ),
                                retries=str(attempt.retry_number),
                                cleanup="required" if node.cleanup_required else "clear",
                                quarantine=(
                                    "quarantined" if attempt.status == "quarantined" else "clear"
                                ),
                                latest_event=attempt.status,
                                result=attempt.result,
                            )
                            for attempt in schedule.attempts
                            if attempt.node_id == node.node_id
                        ),
                    )
                    for node in schedule.nodes
                ),
            )
            for schedule in schedules
        )

    async def list_findings(self) -> tuple[FindingView, ...]:
        if self._findings is None:
            return ()
        session_id = self.kernel.session_id or ""
        return tuple(_finding_view(item) for item in await self._findings.list_findings(session_id))

    async def list_reports(self, *, session_only: bool = False) -> tuple[ReportView, ...]:
        durable: list[ReportView] = []
        if self._report_repository is not None and self._campaigns is not None:
            # Reports are durable campaign artifacts, not console-chat artifacts.
            # A daemon restart creates a new console session, but must not hide the
            # completed campaign's report from acceptance or the operator.
            campaigns = (
                await self._campaigns.list(self.kernel.session_id or "", limit=None)
                if session_only
                else await self._campaigns.list_all()
            )
            for campaign in campaigns:
                revisions = await asyncio.to_thread(
                    self._report_repository.list, str(campaign["campaign_id"])
                )
                durable.extend(
                    ReportView(
                        run=revision.campaign_id,
                        format="durable-revision",
                        output=str(revision.metadata.get("export_path") or revision.revision_id),
                        status=(
                            f"ready · {revision.stop_reason} · cleanup {revision.cleanup_status}"
                        ),
                        finding_count=len(revision.findings),
                        exchange_count=len(revision.exchanges),
                        report_state=str(getattr(revision, "report_state", "")),
                        stop_reason=revision.stop_reason,
                        cleanup_status=revision.cleanup_status,
                        coverage=tuple(
                            (str(key), int(value))
                            for key, value in (getattr(revision, "coverage", {}) or {}).items()
                        ),
                        limitations=tuple(getattr(revision, "limitations", ())),
                        target_progress=(
                            dict(revision.metadata["target_progress"])
                            if isinstance(revision.metadata.get("target_progress"), Mapping)
                            else None
                        ),
                    )
                    for revision in revisions
                )
        return tuple((*self._report_history, *durable))

    async def export_campaign_report(
        self, campaign_id: str, source_output: str, format: str
    ) -> ReportView:
        """Export the selected saved campaign, without changing the console session."""
        if format not in {"markdown", "json", "csv"}:
            raise ValueError("Choose Markdown, JSON or CSV")
        if self._report_repository is None:
            raise RuntimeError("Durable report support is not configured")
        from merlin_findings.projection import DurableReportService

        revisions = await asyncio.to_thread(self._report_repository.list, campaign_id)
        revision = next(
            (
                item
                for item in reversed(revisions)
                if str(item.metadata.get("export_path") or item.revision_id) == source_output
            ),
            None,
        )
        if revision is None:
            raise ValueError("The selected saved report no longer exists")
        suffix = {"markdown": ".md", "json": ".json", "csv": ".zip"}[format]
        source = await asyncio.to_thread(lambda: Path(source_output).expanduser())
        if source.suffix.lower() != ".html":
            raise ValueError("This saved revision has no HTML export location")
        output = source.with_suffix(suffix)
        frontier_snapshot_provider = getattr(self._campaigns, "_store", None)
        content = await asyncio.to_thread(
            DurableReportService(
                self._report_repository,
                frontier_snapshot_provider=frontier_snapshot_provider,
            ).export,
            revision.revision_id,
            format,
        )
        if isinstance(content, bytes):
            await asyncio.to_thread(output.write_bytes, content)
        else:
            await asyncio.to_thread(output.write_text, content, encoding="utf-8")
        view = ReportView(
            campaign_id,
            format,
            str(output),
            "ready",
            len(revision.findings),
            report_state=str(getattr(revision, "report_state", "")),
            stop_reason=revision.stop_reason,
            cleanup_status=revision.cleanup_status,
            target_progress=(
                dict(revision.metadata["target_progress"])
                if isinstance(revision.metadata.get("target_progress"), Mapping)
                else None
            ),
        )
        self._report_history.append(view)
        return view

    async def get_permission_mode(self) -> str:
        return await self._permissions.get_mode() if self._permissions is not None else "auto"

    async def set_permission_mode(self, mode: str) -> str:
        if self._permissions is None:
            return mode
        session_id = self.kernel.session_id or await self.kernel.start()
        return str(await self._permissions.set_mode(mode, session_id=session_id))

    async def list_permission_rules(self) -> tuple[PermissionRuleView, ...]:
        if self._permissions is None:
            return ()
        rows = await self._permissions.list_permissions(self.kernel.session_id or "")
        return tuple(
            PermissionRuleView(
                tool=str(row["tool"]),
                effect=str(row["effect"]),
                decision=str(row["decision"]),
                scope=str(row["scope"]),
            )
            for row in rows
            if row.get("decision") in {"allow_session", "allow_command_session", "deny_session"}
        )

    async def list_permission_history(self) -> tuple[PermissionAuditView, ...]:
        if self._permissions is None:
            return ()
        rows = await self._permissions.list_permissions(self.kernel.session_id or "")
        return tuple(
            PermissionAuditView(
                tool=str(row["tool"]),
                effect=str(row["effect"]),
                mode=str(row["mode"]),
                verdict=str(row["verdict"]),
                decision=str(row["decision"] or ""),
                reason=str(row["reason"]),
                scope=str(row["scope"]),
            )
            for row in rows
        )

    async def list_promotion_candidates(self) -> tuple[PromotionCandidateView, ...]:
        if self._promotions is None:
            return ()
        views = []
        for summary in await self._promotions.list_candidates():
            candidate_id = str(summary["id"])
            detail = await self._promotions.show_candidate(candidate_id)
            evaluations = detail.get("evaluations", ())
            latest = evaluations[-1] if evaluations else None
            gates = (
                tuple(
                    PromotionGateView(
                        str(gate["name"]),
                        bool(gate["passed"]),
                        f"observed {gate['observed']} · threshold {gate['threshold']}",
                    )
                    for gate in latest.get("gates", ())
                )
                if isinstance(latest, Mapping)
                else ()
            )
            failed = tuple(gate.name for gate in gates if not gate.passed)
            views.append(
                PromotionCandidateView(
                    id=candidate_id,
                    kind=str(detail["kind"]),
                    version=str(detail["version"]),
                    status=str(detail.get("status", "proposed")),
                    baseline_version=str(detail["baseline_version"]),
                    baseline_digest=str(detail["baseline_digest"]),
                    content_digest=str(detail["content_digest"]),
                    exact_diff=str(detail.get("exact_diff") or "No exact diff recorded."),
                    proposed_by=str(detail["proposed_by"]),
                    permanently_promotable=bool(detail["permanently_promotable"]),
                    graph_local_composition=bool(detail.get("graph_local_composition", False)),
                    gates=gates,
                    evaluation_failure=(f"Failed gates: {', '.join(failed)}" if failed else None),
                )
            )
        return tuple(views)

    async def list_promotion_history(self) -> tuple[PromotionHistoryView, ...]:
        if self._promotions is None:
            return ()
        return tuple(
            PromotionHistoryView(
                candidate_id=str(item["candidate_id"]),
                action=str(item["action"]),
                operator_id=str(item["operator_id"]),
                activated_at=str(item["activated_at"]),
                previous_candidate_id=(
                    str(item["previous_candidate_id"])
                    if item.get("previous_candidate_id")
                    else None
                ),
            )
            for item in await self._promotions.history(None)
        )

    async def list_browser_contexts(
        self, session_id: str | None = None
    ) -> tuple[BrowserContextView, ...]:
        if self._browser is None:
            return ()
        selected_session_id = session_id or self.kernel.session_id or ""
        views = []
        for context in await self._browser.list_contexts(selected_session_id):
            context_id = str(context["context_id"])
            tabs = await self._browser.list_tabs(selected_session_id, context_id)
            views.append(_browser_context_view(context, tabs))
        return tuple(views)

    async def takeover_browser_context(self, context_id: str) -> BrowserContextView:
        if self._browser is None:
            raise RuntimeError("browser support is not configured")
        session_id = self.kernel.session_id or ""
        context = await self._browser.takeover(session_id, context_id)
        tabs = await self._browser.list_tabs(session_id, context_id)
        return _browser_context_view(context, tabs)

    async def resume_browser_context(self, context_id: str) -> BrowserContextView:
        """Hand control back within the tool turn so the model sees the outcome."""
        if self._browser is None:
            raise RuntimeError("browser support is not configured")
        session_id = self.kernel.session_id or ""
        context = await self._browser.resume(session_id, context_id)
        tabs = await self._browser.list_tabs(session_id, context_id)
        return _browser_context_view(context, tabs)

    async def list_memory_records(self, status: str | None = None) -> tuple[MemoryRecordView, ...]:
        if self._memory is None:
            return ()
        return tuple(
            MemoryRecordView(
                bundle_id=str(item["bundle_id"]),
                record_id=str(item["record_id"]),
                kind=str(item["kind"]),
                status=str(item["status"]),
                source=str(item.get("source", "unknown")),
                reviewed_by=str(item.get("reviewed_by") or ""),
                expires_at=str(item.get("expires_at") or "never"),
                applicability=_compact_mapping(item.get("applicability")),
                digest=str(item.get("digest", "")),
                conflicts=tuple(str(value) for value in item.get("conflicts", ())),
                turn_uses=tuple(str(value) for value in item.get("turn_uses", ())),
            )
            for item in await self._memory.list_memory(status)
        )

    async def run_doctor(self) -> tuple[bool, tuple[DiagnosticCheckView, ...]]:
        if self._readiness is None:
            return False, (
                DiagnosticCheckView("readiness", "failed", "readiness service is not configured"),
            )
        report = sanitize_readiness(await self._readiness.doctor())
        checks = report.get("checks", ()) if isinstance(report, Mapping) else ()
        return bool(report.get("healthy", False)), tuple(
            DiagnosticCheckView(
                str(item.get("name", "unknown")),
                str(item.get("status", "failed")),
                str(item.get("detail", "no detail")),
            )
            for item in checks
            if isinstance(item, Mapping)
        )

    async def list_logs(self, limit: int = 100) -> tuple[OperationalEntryView, ...]:
        if self._readiness is None:
            return ()
        return _operational_views(await self._readiness.logs(limit=limit))

    async def list_history(self, limit: int = 100) -> tuple[OperationalEntryView, ...]:
        if self._readiness is None:
            return ()
        return _operational_views(
            await self._readiness.history(self.kernel.session_id, limit=limit)
        )

    async def list_resume_sessions(self) -> tuple[ResumeSessionView, ...]:
        if self._readiness is None or not hasattr(self._readiness, "sessions"):
            return ()
        rows = sanitize_readiness(await self._readiness.sessions())
        return tuple(
            ResumeSessionView(
                str(item.get("id", item.get("session_id", ""))),
                str(item.get("title") or item.get("name") or "Untitled session"),
                str(item.get("updated_at") or item.get("created_at") or ""),
            )
            for item in rows
            if isinstance(item, Mapping) and (item.get("id") or item.get("session_id"))
        )

    async def list_resume_session_page(
        self, *, limit: int = 20, offset: int = 0, query: str = ""
    ) -> ResumeSessionPage:
        if self._readiness is None or not hasattr(self._readiness, "session_page"):
            rows = await self.list_resume_sessions()
            return ResumeSessionPage(rows[offset : offset + limit], len(rows), limit, offset, query)
        page = sanitize_readiness(
            await self._readiness.session_page(limit=limit, offset=offset, query=query)
        )
        items = page.get("items", ()) if isinstance(page, Mapping) else ()
        return ResumeSessionPage(
            tuple(
                ResumeSessionView(
                    str(item.get("id", item.get("session_id", ""))),
                    str(item.get("title") or item.get("name") or "Untitled session"),
                    str(item.get("updated_at") or item.get("created_at") or ""),
                )
                for item in items
                if isinstance(item, Mapping) and (item.get("id") or item.get("session_id"))
            ),
            int(page.get("total", len(items))),
            int(page.get("limit", limit)),
            int(page.get("offset", offset)),
            str(page.get("query", query)),
        )

    async def conversation_history(
        self,
        *,
        limit: int = 50,
        before: int | None = None,
        session_id: str | None = None,
        exclude_current_turn: bool = False,
    ) -> Mapping[str, object]:
        active = self.kernel.session_id or ""
        if session_id is not None and session_id != active:
            raise ValueError("The active session changed; reload its conversation")
        loader = getattr(getattr(self.kernel, "_store", None), "load_history_page", None)
        if loader is None or not active:
            return {"session_id": active, "messages": [], "before": None, "has_more": False}
        value = await loader(
            active, limit=limit, before=before, exclude_current_turn=exclude_current_turn
        )
        if (self.kernel.session_id or "") != active:
            raise ValueError("The active session changed; reload its conversation")
        return dict(sanitize_readiness(value))

    async def conversation_context(self) -> Mapping[str, object]:
        if self._readiness is None or not hasattr(self._readiness, "context"):
            return {
                "session_id": self.kernel.session_id or "",
                "projection_head": None,
                "transcript_head": 0,
                "messages": [],
                "compactions": [],
            }
        value = sanitize_readiness(await self._readiness.context(self.kernel.session_id))
        return dict(value) if isinstance(value, Mapping) else {}

    async def export_safe_config(self) -> str:
        if self._readiness is None:
            raise RuntimeError("readiness service is not configured")
        exported = await self._readiness.export_config()
        try:
            decoded = json.loads(exported)
        except (TypeError, ValueError):
            return str(sanitize_readiness(exported))
        return json.dumps(sanitize_readiness(decoded), indent=2, sort_keys=True) + "\n"

    def _remember_report(self, event: AppEvent) -> None:
        payload = event.payload
        view = ReportView(
            run=self.kernel.session_id or "current session",
            format=str(payload.get("format", "unknown")),
            output=str(payload.get("output", "")),
            status="ready",
            finding_count=int(payload.get("finding_count", 0)),
        )
        if view not in self._report_history:
            self._report_history.append(view)

    async def _recover_agents(self) -> None:
        if not self._campaigns_recovered or not self._agents_recovered:
            await self.recover_background()

    async def recover_background(self) -> None:
        if self.kernel.session_id is None:
            await self.kernel.start()
        # Executing campaigns already carry durable operator authorization.
        # Reattach them when the daemon is replaced so a read-only client or a
        # transient daemon restart cannot silently turn active work into a pause.
        # Campaigns explicitly paused by an operator are absent from
        # ``list_executing`` and remain paused.
        if self._campaigns is not None:
            await self._campaigns.recover(resume_execution=True)
        self._campaigns_recovered = True
        if not self._agents_recovered:
            if self._agent_store is not None:
                await self._agent_store.recover()
            if self._agents is not None:
                recover = getattr(self._agents, "recover", None)
                if callable(recover):
                    await recover(resume_execution=False)
            self._agents_recovered = True

    async def has_active_background_work(self) -> bool:
        """Report whether durable campaign execution still needs this daemon."""
        if self._campaigns is None:
            return False
        return any(
            str(campaign.get("status", "")) == "executing"
            for campaign in await self._campaigns.list_all()
        )

    async def campaign_status(self, campaign_id: str) -> Mapping[str, object]:
        if self._campaigns is None:
            raise RuntimeError("campaign support is not configured")
        await self._campaign_by_id(campaign_id)
        return await self._campaigns.status(campaign_id)

    async def execute_campaign(
        self, campaign_id: str, manifest_digest: str
    ) -> Mapping[str, object]:
        """Deterministic execution trigger after a verified durable review.

        Approval remains an operator decision, but once given, starting the
        campaign must not depend on a model turn choosing to call
        assessment_execute (run15/run16 ended the approval turn without it and
        stranded a prepared campaign).
        """
        if self._campaigns is None or self.kernel.session_id is None:
            raise RuntimeError("campaign support is not configured")
        await self._current_session_campaign(campaign_id)
        return await self._campaigns.execute(self.kernel.session_id, campaign_id, manifest_digest)

    async def decide_campaign_saturation(
        self, campaign_id: str, decision: str, additional_waves: int | None = None
    ) -> Mapping[str, object]:
        if self._campaigns is None or self.kernel.session_id is None:
            raise RuntimeError("campaign support is not configured")
        await self._current_session_campaign(campaign_id)
        return await self._campaigns.decide_saturation(
            self.kernel.session_id,
            campaign_id,
            decision,
            additional_waves=additional_waves,
        )

    async def guide_campaign(self, campaign_id: str, message: str) -> Mapping[str, object]:
        if self._campaigns is None or self.kernel.session_id is None:
            raise RuntimeError("campaign support is not configured")
        return await self._campaigns.guide(self.kernel.session_id, campaign_id, message)

    async def list_campaigns(self) -> tuple[CampaignView, ...]:
        if self._campaigns is None or self.kernel.session_id is None:
            return ()
        campaigns = []
        progress_reader = getattr(self._campaigns, "wave_progress", None)
        for item in await self._campaigns.list(self.kernel.session_id, limit=None):
            if str(item["session_id"]) != self.kernel.session_id:
                continue
            progress = {}
            if callable(progress_reader) and str(item["status"]) in {"executing", "paused"}:
                try:
                    progress = await progress_reader(str(item["campaign_id"]))
                except Exception:
                    # Optional progress must not make the campaign inventory unavailable.
                    progress = {}
            campaigns.append(
                CampaignView(
                    id=str(item["campaign_id"]),
                    session_id=str(item["session_id"]),
                    objective=str(item["objective"]),
                    profile=str(item["profile"]),
                    status=str(item["status"]),
                    manifest_digest=str(item["manifest_digest"]),
                    wave_number=progress.get("wave_number"),
                    wave_kind=str(progress.get("wave_kind", "")),
                    wave_status=str(progress.get("wave_status", "")),
                    lead_activity=str(progress.get("lead_activity", "")),
                    stop_reason=str(item.get("stop_reason") or ""),
                    target_progress=(
                        dict(progress["target_progress"])
                        if isinstance(progress.get("target_progress"), Mapping)
                        else None
                    ),
                    review_progress=(
                        dict(progress["review_progress"])
                        if isinstance(progress.get("review_progress"), Mapping)
                        else None
                    ),
                    saturation_decision=(
                        dict(progress["saturation_decision"])
                        if isinstance(progress.get("saturation_decision"), Mapping)
                        else None
                    ),
                    elapsed_seconds=(
                        float(item["elapsed_seconds"])
                        if isinstance(item.get("elapsed_seconds"), (int, float))
                        else None
                    ),
                )
            )
        return tuple(campaigns)

    async def cancel_campaign(self, campaign_id: str = "*") -> int:
        if self._campaigns is None or self.kernel.session_id is None:
            return 0
        return await self._campaigns.cancel(self.kernel.session_id, campaign_id)

    async def pause_campaign(self, campaign_id: str) -> object:
        if self._campaigns is None or self.kernel.session_id is None:
            raise RuntimeError("campaign support is not configured")
        await self._current_session_campaign(campaign_id)
        return await self._campaigns.pause(campaign_id)

    async def resume_campaign(self, campaign_id: str) -> object:
        if self._campaigns is None or self.kernel.session_id is None:
            raise RuntimeError("campaign support is not configured")
        row = await self._campaign_by_id(campaign_id)
        owning_session_id = str(row["session_id"])
        if owning_session_id != self.kernel.session_id:
            if self.kernel.turn_active:
                await self.kernel.interrupt()
            await self.kernel.start(owning_session_id)
        return await self._campaigns.resume(campaign_id)

    async def _campaign_by_id(self, campaign_id: str) -> Mapping[str, object]:
        if self._campaigns is None:
            raise RuntimeError("campaign support is not configured")
        for row in await self._campaigns.list_all():
            if str(row["campaign_id"]) == campaign_id:
                return row
        raise ValueError(f"campaign does not exist: {campaign_id}")

    async def _current_session_campaign(self, campaign_id: str) -> Mapping[str, object]:
        if self._campaigns is None or self.kernel.session_id is None:
            raise RuntimeError("campaign support is not configured")
        for row in await self._campaigns.list_all():
            if str(row["campaign_id"]) != campaign_id:
                continue
            if str(row["session_id"]) == self.kernel.session_id:
                return row
            raise ValueError("campaign belongs to a different session; resume that session first")
        raise ValueError(f"campaign does not exist: {campaign_id}")

    async def cancel_task(self, task_id: str) -> str:
        if self._agents is None or self.kernel.session_id is None:
            raise RuntimeError("agent support is not configured")
        await self._recover_agents()
        return await self._agents.cancel_task(self.kernel.session_id, task_id)

    async def list_lessons(self) -> object:
        if self._campaigns is None or self.kernel.session_id is None:
            return {"counts": {}, "lessons": ()}
        return await self._campaigns.lessons(self.kernel.session_id)


def _render_reconnect(value: object) -> str:
    if not isinstance(value, Mapping):
        return "none"
    return f"{value.get('attempts', 0)} attempts · {value.get('delay_seconds', 0)}s delay"


def _operational_views(rows: Sequence[Mapping[str, Any]]) -> tuple[OperationalEntryView, ...]:
    sanitized = sanitize_readiness(rows)
    views = []
    for item in sanitized:
        if not isinstance(item, Mapping):
            continue
        occurred_at = item.get("occurred_at") or item.get("created_at") or item.get("at") or ""
        kind = item.get("kind") or item.get("type") or item.get("level") or "event"
        summary = item.get("summary") or item.get("message") or item.get("action") or "updated"
        excluded = {
            "occurred_at",
            "created_at",
            "at",
            "kind",
            "type",
            "level",
            "summary",
            "message",
            "action",
        }
        detail = ", ".join(f"{key}={value}" for key, value in item.items() if key not in excluded)
        views.append(OperationalEntryView(str(occurred_at), str(kind), str(summary), detail))
    return tuple(views)


def _render_mcp_audit(record: Mapping[str, object]) -> str:
    return " · ".join(str(record.get(field, "unknown")) for field in ("at", "tool", "status"))


def event_to_stream(event: AppEvent) -> StreamEvent | None:
    payload = event.payload
    text = str(payload.get("text", ""))
    if event.type == "stream":
        return StreamEvent("content", text)
    if event.type == "usage":
        total = int(payload.get("total_tokens", 0) or 0)
        incoming = int(payload.get("input_tokens", 0) or 0)
        outgoing = int(payload.get("output_tokens", 0) or 0)
        cost = payload.get("estimated_cost_usd")
        summary = f"{total:,} tokens ({incoming:,} in · {outgoing:,} out)"
        if isinstance(cost, (int, float)) and cost > 0:
            summary += f" · ${cost:.4f}"
        return StreamEvent("usage", summary)
    if event.type == "thinking":
        return StreamEvent("thinking", text)
    if event.type in {"attacker", "target"}:
        return StreamEvent(event.type, text)
    if event.type == "assessment" and payload.get("role") in {"attacker", "target"}:
        return StreamEvent(str(payload["role"]), text)
    if event.type == "error":
        return StreamEvent("error", text or "Unknown kernel failure")
    if event.type == "tool":
        name = str(payload.get("name", "tool"))
        state = str(payload.get("state", "updated"))
        if state == "completed" and name == "request_user_input":
            try:
                receipt = json.loads(text)
                if receipt.get("status") == "awaiting_input":
                    return StreamEvent("user_input", json.dumps(receipt["request"]))
            except (ValueError, TypeError, KeyError, AttributeError):
                pass
        if state == "completed" and name in {"operator_command", "campaign_configure"}:
            try:
                control = json.loads(text)
            except (ValueError, TypeError):
                control = None
            expected = "queued" if name == "operator_command" else "configured"
            if isinstance(control, Mapping) and control.get("status") == expected:
                return StreamEvent(name, text)
        if name == "assessment_prepare" and state == "completed":
            prepared = payload.get("campaign_review")
            if isinstance(prepared, Mapping) and isinstance(prepared.get("review"), Mapping):
                return StreamEvent("campaign_review", json.dumps(prepared))
            try:
                prepared = json.loads(text)
            except json.JSONDecodeError:
                prepared = None
            if isinstance(prepared, Mapping) and isinstance(prepared.get("review"), Mapping):
                return StreamEvent("campaign_review", text)
        invocation = payload.get("invocation")
        if isinstance(invocation, Mapping):
            command = invocation.get("command")
            working_directory = invocation.get("working_directory")
            heading = f"{name} {state}"
            if isinstance(command, str) and command:
                heading += f": {command}"
            lines = [heading]
            if isinstance(working_directory, str) and working_directory:
                lines.append(f"  Working directory: {working_directory}")
            if text:
                label = "Error" if state in {"failed", "interrupted"} else "Result"
                lines.append(f"  {label}: {text}")
            kind = "skill" if name == "skill_load" else "tool"
            return StreamEvent(kind, "\n".join(lines))
        detail = f": {text}" if text else ""
        kind = "skill" if name == "skill_load" else "tool"
        return StreamEvent(kind, f"{name} {state}{detail}")
    if event.type == "finding":
        action = str(payload.get("action", "updated"))
        finding = payload.get("finding", {})
        finding_id = finding.get("id") if isinstance(finding, Mapping) else None
        suffix = f" {finding_id}" if finding_id else ""
        return StreamEvent("status", f"Finding{suffix} {action}ed")
    if event.type == "report":
        state = str(payload.get("state", "updated"))
        output = payload.get("output")
        count = payload.get("finding_count")
        detail = f" → {output}" if output else ""
        if count is not None:
            detail += f" · {count} accepted findings"
        if text:
            detail += f": {text}"
        kind = "error" if state == "failed" else "status"
        return StreamEvent(kind, f"Report {state}{detail}")
    if event.type == "memory":
        action = str(payload.get("action", payload.get("state", "updated")))
        record_ids = payload.get("record_ids") or payload.get("record_id")
        if isinstance(record_ids, (list, tuple)):
            identity = ", ".join(str(value) for value in record_ids)
        else:
            identity = str(record_ids or payload.get("bundle_id", ""))
        suffix = f" · {identity}" if identity else ""
        turn = f" · turn {payload['turn_id']}" if payload.get("turn_id") else ""
        return StreamEvent("status", f"Memory {action}{suffix}{turn}")
    if event.type in {"agent_progress", "swarm"}:
        kind = str(payload.get("kind", event.type))
        state = str(payload.get("state", "updated"))
        identifiers = [
            f"{label}={payload[label]}"
            for label in (
                "schedule_id",
                "node_id",
                "attempt_id",
                "lease_generation",
                "mailbox_message_id",
                "directive_id",
            )
            if payload.get(label) is not None
        ]
        detail = f" — {text}" if text else ""
        suffix = f" ({', '.join(identifiers)})" if identifiers else ""
        presentation = "swarm" if event.type == "swarm" else "subagent"
        return StreamEvent(presentation, f"{kind} {state}{suffix}{detail}")
    if event.type in {"directive", "mailbox"}:
        state = str(payload.get("state", "updated"))
        identity = payload.get("directive_id") or payload.get("mailbox_message_id")
        suffix = f" {identity}" if identity else ""
        detail = f": {text}" if text else ""
        return StreamEvent("status", f"{event.type} {state}{suffix}{detail}")
    if event.type == "message":
        if payload.get("foreground_followup") is True and payload.get("role") == "user":
            return StreamEvent("user", text)
        if payload.get("resumed"):
            # Readiness carries recovery metadata, not a replayable transcript.
            # Present the aggregate below instead of flooding the conversation.
            return None
        # The user text is already echoed and assistant text arrived as deltas.
        return None
    if event.type == "command" and payload.get("command") == "resume":
        try:
            projection = json.loads(text)
        except (TypeError, ValueError):
            return StreamEvent("status", "Resume recovery completed")
        return StreamEvent(
            "status",
            "Resume recovery completed · "
            f"{projection.get('recovered', 0)} recovered operations · "
            f"{len(projection.get('messages', ()))} messages · "
            f"{len(projection.get('schedules', ()))} schedules",
        )
    if event.type == "quit":
        return StreamEvent("quit", text)
    return StreamEvent("status", text or str(dict(payload)))


def _finding_view(finding: Any) -> FindingView:
    raw_evidence = getattr(finding, "evidence_references", None)
    if raw_evidence is None:
        raw_evidence = getattr(finding, "evidence", ())
    evidence = tuple(str(getattr(reference, "ref", reference)) for reference in raw_evidence)
    return FindingView(
        id=str(finding.id),
        title=str(finding.title),
        severity=str(finding.severity),
        summary=str(finding.summary),
        disposition=str(getattr(finding, "status", "draft")),
        evidence=evidence,
    )


def _browser_context_view(
    context: Mapping[str, Any], tabs: Sequence[Mapping[str, Any]]
) -> BrowserContextView:
    return BrowserContextView(
        id=str(context["context_id"]),
        isolation=str(context.get("mode", "fresh")),
        target=str(context.get("target_id", "unknown")),
        owner=str(context.get("worker_id", "unassigned")),
        technique=str(context.get("technique_id", "unknown")),
        state=str(context.get("state", "unknown")),
        allowed_origins=tuple(str(value) for value in context.get("allowed_origins", ())),
        latest_action=str(context.get("latest_action", "")),
        latest_observation=str(context.get("latest_observation", "")),
        blockers=tuple(str(value) for value in context.get("blockers", ())),
        evidence=tuple(str(value) for value in context.get("evidence", ())),
        cleanup=str(context.get("cleanup_state", "pending")),
        tabs=tuple(
            BrowserTabView(
                ref=str(tab["tab_ref"]),
                url=str(tab.get("url", "")),
                opener=str(tab.get("opener_ref") or ""),
                state=str(tab.get("state", "unknown")),
            )
            for tab in tabs
        ),
    )


def _compact_mapping(value: object) -> str:
    if not isinstance(value, Mapping):
        return str(value or "none")
    return (
        "; ".join(f"{key}={_render_applicability_value(raw)}" for key, raw in value.items())
        or "none"
    )


def _render_applicability_value(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


class KernelSettings:
    """Kernel settings backed by already-validated application configuration."""

    def __init__(
        self,
        config: Config,
        model: str,
        configuration: ProviderConfiguration | None = None,
    ) -> None:
        self._config = config
        self._default_model = model
        self._model = model
        self._configuration = configuration or ProviderConfiguration()

    async def setup(self) -> Mapping[str, Any]:
        return {
            "configured": True,
            "hint": "Edit ~/.merlin/config.toml or use MERLIN_* environment variables",
        }

    @property
    def config(self) -> Config:
        return self._config

    def apply_reload(self, newer: Config, reloadable: frozenset[str]) -> None:
        values = dict(self._config.values)
        sources = dict(self._config.sources)
        if "permission_mode" in reloadable:
            values["permission_mode"] = newer.values.get("permission_mode", "auto")
            source = newer.sources.get("permission_mode")
            if source is None:
                sources.pop("permission_mode", None)
            else:
                sources["permission_mode"] = source
        if "specialist_composition" in reloadable:
            values["specialist_composition"] = newer.values.get(
                "specialist_composition", "opinionated"
            )
            source = newer.sources.get("specialist_composition")
            if source is None:
                sources.pop("specialist_composition", None)
            else:
                sources["specialist_composition"] = source
        provider_keys = {key.split(".", 1)[1] for key in reloadable if key.startswith("provider.")}
        if provider_keys:
            provider = dict(values.get("provider", {}))
            newer_provider = newer.values.get("provider", {})
            assert isinstance(newer_provider, Mapping)
            for key in provider_keys:
                if key in newer_provider:
                    provider[key] = newer_provider[key]
                else:
                    provider.pop(key, None)
                source = newer.sources.get(f"provider.{key}")
                if source is None:
                    sources.pop(f"provider.{key}", None)
                else:
                    sources[f"provider.{key}"] = source
            values["provider"] = provider
            if "model" in provider_keys:
                previous_default = self._default_model
                self._default_model = str(provider.get("model", previous_default))
                # Follow a saved default change only when no role route overrode
                # the model this session; an active role route re-resolves from
                # the reloaded role_models at the next campaign turn.
                if self._model == previous_default:
                    self._model = self._default_model
        self._config = Config(
            database_url=self._config.database_url,
            values=values,
            sources=sources,
        )

    async def get_model(self) -> str:
        return self._model

    def use_role_model(self, role: str, category: str) -> str:
        """Apply a validated role route to this backend without persisting a global default."""

        routes = self._config.provider_role_models
        self._model = routes.get(role) or routes.get(category) or self._default_model
        return self._model

    def reset_runtime_model(self) -> None:
        self._model = self._default_model

    async def set_model(self, model: str) -> None:
        if not model.strip():
            raise ValueError("model is required")
        await asyncio.to_thread(self._configuration.save_model, model)
        self._default_model = model
        self._model = model

    async def status(self) -> Mapping[str, Any]:
        provider = self._config.values.get("provider", {})
        base_url = provider.get("base_url") if isinstance(provider, Mapping) else None
        effort = provider.get("effort", "default") if isinstance(provider, Mapping) else "default"
        return {
            "model": self._model,
            "provider": base_url,
            "effort": effort,
            "database": "postgresql",
        }

    async def plugins(self) -> Sequence[Mapping[str, Any]]:
        return (
            {"name": "openai-compatible", "kind": "provider"},
            {"name": "workspace", "kind": "tools"},
        )
