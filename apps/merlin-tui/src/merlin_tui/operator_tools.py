"""Foreground chat controls backed by the existing command and campaign paths."""

from __future__ import annotations

import inspect
import json
import shlex
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any

from merlin_contracts import ChatMessage, ToolContext, ToolResult, ToolSpec

MessageLoader = Callable[[str], Awaitable[Sequence[ChatMessage]]]
PROFILES = ("quickscan", "recommended", "exhaustive", "exploration")
MODES = ("api", "browser", "hybrid")
READ_OPERATIONS = (
    "get_runtime_status",
    "list_campaigns",
    "campaign_status",
    "list_agents",
    "get_agent_transcript",
    "list_schedules",
    "list_active_schedules",
    "list_reports",
    "list_findings",
    "get_permission_mode",
    "list_permission_rules",
    "list_permission_history",
    "list_targets",
    "list_skills",
    "list_mcp_servers",
    "list_packs",
    "list_memory_records",
    "list_browser_contexts",
    "list_resume_sessions",
    "list_resume_session_page",
    "list_logs",
    "list_history",
    "conversation_context",
    "export_safe_config",
    "list_promotion_candidates",
    "list_promotion_history",
    "list_lessons",
    "run_doctor",
)
WRITE_OPERATIONS = (
    "set_model",
    "set_model_route",
    "set_model_routing",
    "set_permission_mode",
    "pause_campaign",
    "resume_campaign",
    "cancel_campaign",
    "cancel_task",
    "guide_campaign",
    "decide_campaign_saturation",
    "export_campaign_report",
    "takeover_browser_context",
    "resume_browser_context",
)


def _foreground(context: ToolContext) -> None:
    if context.agent_id is not None or context.campaign_id is not None:
        raise ValueError("operator controls are available only to the foreground model")


def _payload(message: ChatMessage) -> dict[str, Any]:
    try:
        # Kernel preparation receipts append a typed recon-continuation marker
        # after the JSON document. Decode the document, not that journal suffix.
        value, _end = json.JSONDecoder().raw_decode(message.content.lstrip())
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _settings(messages: Sequence[ChatMessage]) -> dict[str, Any]:
    for message in reversed(messages):
        if message.role == "tool" and message.name == "campaign_configure":
            payload = _payload(message)
            if payload.get("status") == "configured":
                return dict(payload["settings"])
    return {}


class CampaignConfigureTool:
    effect = "workspace_write"

    def __init__(self, load: MessageLoader, preflight=None, activate=None) -> None:
        self._load = load
        self._preflight = preflight
        self._activate = activate

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="campaign_configure",
            description=(
                "Record the operator's campaign intent BEFORE reconnaissance or preparation. "
                "Interpret natural language: discovery/novel-hypothesis scanning means "
                "exploration; "
                "quick, recommended, exhaustive, and exploration are distinct profiles. Set "
                "new_campaign=true for a new scan with its exact target/scenario and objective. "
                "For profile, composition, execution-mode, or budget changes, update only the "
                "requested fields; omitted fields retain the selection. Null clears an optional "
                "setting. Settings survive resume and are applied to assessment_prepare. Changes "
                "require a new review; this never changes an executing manifest or approves it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "new_campaign": {"type": "boolean"},
                    "objective": {"type": "string", "minLength": 1},
                    "campaign_profile": {"enum": list(PROFILES)},
                    "specialist_composition": {"enum": ["autonomous", "opinionated", None]},
                    "execution_mode": {"enum": [*MODES, None]},
                    "budget": {"type": ["object", "null"]},
                },
                "additionalProperties": False,
            },
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        try:
            _foreground(context)
            settings = (
                {}
                if arguments.get("new_campaign") is True
                else _settings(await self._load(context.session_id))
            )
            allowed = set(self.describe().input_schema["properties"])
            if set(arguments) - allowed:
                raise ValueError("unknown campaign setting")
            if "new_campaign" in arguments and not isinstance(arguments["new_campaign"], bool):
                raise ValueError("new_campaign must be boolean")
            for key, value in arguments.items():
                if key == "new_campaign":
                    continue
                if value is None and key in {"execution_mode", "specialist_composition", "budget"}:
                    settings.pop(key, None)
                    continue
                choices = {
                    "campaign_profile": PROFILES,
                    "specialist_composition": ("autonomous", "opinionated"),
                    "execution_mode": MODES,
                }
                if key in choices and value not in choices[key]:
                    raise ValueError(f"invalid {key}")
                if key == "objective" and (not isinstance(value, str) or not value.strip()):
                    raise ValueError(
                        "objective must contain the requested target and scan objective"
                    )
                if key == "budget" and not isinstance(value, Mapping):
                    raise ValueError("budget must be an object or null")
                settings[key] = value
            if arguments.get("new_campaign") is True and not settings.get("objective"):
                raise ValueError("a new campaign requires its exact objective and target")
            if arguments.get("new_campaign") is True and not settings.get("campaign_profile"):
                raise ValueError(
                    "a new campaign requires its requested campaign_profile; "
                    "ask the operator if the desired effort is unclear"
                )
            profile = settings.get("campaign_profile")
            if profile and self._preflight is not None:
                ready, report = await self._preflight(profile)
                if not ready:
                    return ToolResult(
                        content=json.dumps({"status": "not_configured", "error": report}),
                        is_error=True,
                    )
            if profile and self._activate is not None:
                self._activate()
            result = {
                "status": "configured",
                "settings": settings,
                "revision": context.call_id,
                "requires_review": True,
            }
            return ToolResult(content=json.dumps(result, sort_keys=True), metadata=result)
        except (ValueError, TypeError) as error:
            return ToolResult(
                content=json.dumps({"status": "not_configured", "error": str(error)}), is_error=True
            )


class ConfiguredCampaignTool:
    """Apply durable operator settings and reject execution of a superseded review."""

    def __init__(self, tool, load: MessageLoader) -> None:
        self._tool = tool
        self._load = load
        self.effect = tool.effect

    def describe(self) -> ToolSpec:
        return self._tool.describe()

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        # Worker/internal protocol calls retain their existing admitted contract.
        if context.agent_id is not None or context.campaign_id is not None:
            return await self._tool.invoke(context, arguments)
        messages = await self._load(context.session_id)
        values = dict(arguments)
        if self.describe().name == "assessment_prepare":
            settings = _settings(messages)
            if not settings.get("objective") or not settings.get("campaign_profile"):
                return ToolResult(
                    content="Record the operator's target, objective and selected profile with "
                    "campaign_configure before preparing the campaign.",
                    is_error=True,
                )
            values.update({k: v for k, v in settings.items() if k != "execution_mode"})
            if settings.get("execution_mode"):
                values["adapter"] = {
                    **values.get("adapter", {}),
                    "execution_mode": settings["execution_mode"],
                }
        elif self.describe().name == "assessment_execute":
            for message in reversed(messages):
                if message.role != "tool":
                    continue
                payload = _payload(message)
                if message.name == "campaign_configure" and payload.get("status") == "configured":
                    return ToolResult(
                        content="Campaign settings changed. Prepare and display a new "
                        "review before requesting approval.",
                        is_error=True,
                    )
                if message.name == "assessment_prepare" and payload.get("campaign_id"):
                    if any(
                        values.get(k) != payload.get(k) for k in ("campaign_id", "manifest_digest")
                    ):
                        return ToolResult(
                            content="This approval refers to a superseded campaign "
                            "review. Display the intended campaign review first.",
                            is_error=True,
                        )
                    break
        result = await self._tool.invoke(context, values)
        if self.describe().name == "assessment_prepare" and not result.is_error:
            # Keep the binding parseable in durable tool history even when the
            # full manifest exceeds the broker's model-facing result limit.
            payload = result.metadata
            if payload.get("campaign_id") and payload.get("manifest_digest"):
                receipt = {
                    k: payload[k]
                    for k in (
                        "campaign_id",
                        "manifest_digest",
                        "status",
                        "approval_required",
                        "profile",
                        "objective",
                        "specialist_composition",
                    )
                    if k in payload
                }
                receipt["operator_settings"] = _settings(messages)
                receipt["review_requires_client_display"] = True
                return result.model_copy(update={"content": json.dumps(receipt, sort_keys=True)})
        return result


class OperatorCommandTool:
    effect = "workspace_write"

    def __init__(self, commands: Sequence[str], hints: Mapping[str, str]) -> None:
        self._commands = commands
        self._hints = hints
        self._roots = {c.split()[0] for c in commands}

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="operator_command",
            description=(
                "Use an existing Merlin interface/command operation on the operator's request. "
                "Translate their natural language into the documented command, resolve exact IDs "
                "with read tools, and never guess ambiguous recipients. This queues the SAME TUI "
                "command path after this response; menus and confirmations remain interactive. "
                "The receipt means queued, not executed. Do not claim completion yet. Browser "
                "resume must use operator_runtime resume_browser_context instead, so its result "
                "returns in this turn and you can continue the pending work. Never use "
                "this for shell commands. For campaign profiles/modes use campaign_configure, "
                "then assessment_prepare and a fresh review. Available commands:\n"
                + "\n".join(f"{c}: {self._hints.get(c, '')}" for c in self._commands)
            ),
            input_schema={
                "type": "object",
                "properties": {"command": {"type": "string", "minLength": 1}},
                "required": ["command"],
                "additionalProperties": False,
            },
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        try:
            _foreground(context)
            command = arguments.get("command")
            if not isinstance(command, str) or "\n" in command or "\r" in command:
                raise ValueError("provide exactly one Merlin command")
            parts = shlex.split(command)
            if not parts or parts[0] not in self._roots:
                raise ValueError("unknown Merlin command; consult the tool's available commands")
            if parts[:2] == ["/browser", "resume"]:
                raise ValueError(
                    "Use operator_runtime with operation=resume_browser_context and "
                    "arguments={context_id: CONTEXT}; await its result, then continue "
                    "the pending work. Browser resume cannot be queued after this turn."
                )
            if not context.call_id:
                raise ValueError("a durable request identity is required")
            result = {"status": "queued", "command": command.strip(), "request_id": context.call_id}
            return ToolResult(content=json.dumps(result, sort_keys=True), metadata=result)
        except (ValueError, TypeError) as error:
            return ToolResult(
                content=json.dumps({"status": "not_queued", "error": str(error)}), is_error=True
            )


def _json_value(value):
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"unsupported control result: {type(value).__name__}")


class OperatorRuntimeTool:
    """Direct chat access to the same backend methods used by UI controls."""

    def __init__(self, backend, *, read_only: bool) -> None:
        self._backend = backend
        self._operations = READ_OPERATIONS if read_only else WRITE_OPERATIONS
        self._name = "operator_inspect" if read_only else "operator_runtime"
        self.effect = "workspace_read" if read_only else "workspace_write"

    def describe(self) -> ToolSpec:
        signatures = "\n".join(
            f"{name}{inspect.signature(getattr(self._backend, name))}" for name in self._operations
        )
        return ToolSpec(
            name=self._name,
            description=(
                "Use Merlin's existing backend control operations for natural-language requests. "
                "Pass named parameters in arguments. Inspect inventory before selecting exact "
                "IDs; ask if multiple campaigns, agents, sessions or models match. Do not invent "
                "IDs or change scope. Provider credentials must use operator_command /setup, "
                "which opens secure input. Profile/mode changes use campaign_configure and a new "
                "review. Session switching, new session, and other interface operations use "
                "operator_command. For browser hand-back use resume_browser_context: await "
                "the returned result, then verify the target session and continue the pending "
                "work in this turn. If resume fails, explain the error and retain operator "
                "control; never claim it succeeded. Report only observed results. "
                "Available signatures:\n" + signatures
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {"enum": list(self._operations)},
                    "arguments": {"type": "object"},
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        try:
            _foreground(context)
            if context.session_id != self._backend.kernel.session_id:
                raise ValueError("the foreground session changed; refresh before acting")
            operation = arguments.get("operation")
            if operation not in self._operations:
                raise ValueError("unsupported control operation")
            parameters = arguments.get("arguments", {})
            if not isinstance(parameters, Mapping):
                raise ValueError("arguments must be an object")
            method = getattr(self._backend, operation)
            inspect.signature(method).bind(**parameters)
            result = await method(**parameters)
            return ToolResult(
                content=json.dumps(
                    {"status": "completed", "operation": operation, "result": result},
                    default=_json_value,
                    sort_keys=True,
                )
            )
        except (ValueError, TypeError, KeyError, RuntimeError) as error:
            return ToolResult(
                content=json.dumps({"status": "failed", "error": str(error)}), is_error=True
            )
