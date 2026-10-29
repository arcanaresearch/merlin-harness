"""Foreground chat controls backed by the existing command and campaign paths."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import shlex
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from merlin_contracts import ChatMessage, ToolContext, ToolResult, ToolSpec
from merlin_contracts.campaign_coordination import (
    has_url_candidate,
    is_explicit_campaign_launch,
    is_recon_handback_coordination,
)

MessageLoader = Callable[[str], Awaitable[Sequence[ChatMessage]]]
PROFILES = ("quickscan", "recommended", "exhaustive", "exploration")
MODES = ("api", "browser", "hybrid")
_URL_RE = re.compile(r"https?://(?:\[[0-9a-f:.]+\]|[^\s<>\]\[(){}\"']+)+", re.IGNORECASE)
_CLI_CAMPAIGN_SUBMIT_MARKER = "MERLIN_CAMPAIGN_SUBMIT_REQUEST\n"
_CLI_CAMPAIGN_SUBMIT_LEGACY_SCHEMA = "merlin.cli-campaign-submit.v1"
_CLI_CAMPAIGN_SUBMIT_SCHEMA = "merlin.cli-campaign-submit.v2"
_CAMPAIGN_REQUEST_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def new_campaign_request_id() -> str:
    """Create one durable identity for a client-owned campaign submission."""

    return str(uuid.uuid4())


def _valid_campaign_request_id(value: object) -> bool:
    return isinstance(value, str) and _CAMPAIGN_REQUEST_ID_RE.fullmatch(value) is not None


def campaign_submit_envelope(objective: str, *, campaign_request_id: str) -> str:
    """Encode the operator-authoritative objective boundary for every campaign client."""

    if not _valid_campaign_request_id(campaign_request_id):
        raise ValueError("campaign_request_id must be a canonical lowercase UUID")
    payload = json.dumps(
        {
            "campaign_request_id": campaign_request_id,
            "schema_version": _CLI_CAMPAIGN_SUBMIT_SCHEMA,
            "operator_objective": objective,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return _CLI_CAMPAIGN_SUBMIT_MARKER + payload


def _campaign_submit_payload(content: str) -> Mapping[str, object] | None:
    marker_index = content.rfind(_CLI_CAMPAIGN_SUBMIT_MARKER)
    if marker_index < 0:
        return None
    encoded = content[marker_index + len(_CLI_CAMPAIGN_SUBMIT_MARKER) :]
    try:
        envelope, _end = json.JSONDecoder().raw_decode(encoded)
    except (ValueError, TypeError):
        return None
    if not isinstance(envelope, Mapping):
        return None
    schema = envelope.get("schema_version")
    if schema == _CLI_CAMPAIGN_SUBMIT_SCHEMA:
        if set(envelope) != {
            "campaign_request_id",
            "operator_objective",
            "schema_version",
        } or not _valid_campaign_request_id(envelope.get("campaign_request_id")):
            return None
    elif schema == _CLI_CAMPAIGN_SUBMIT_LEGACY_SCHEMA:
        if set(envelope) != {"operator_objective", "schema_version"}:
            return None
    else:
        return None
    if (
        not isinstance(envelope.get("operator_objective"), str)
        or not str(envelope["operator_objective"]).strip()
    ):
        return None
    return envelope


def _is_internal_campaign_continuation(content: str) -> bool:
    normalized = " ".join(content.casefold().split())
    return (
        re.match(r"^(?:please\s+)?(?:resume|retry|continue)\b", normalized) is not None
        or normalized.startswith("the previous prepared campaign was cancelled")
        or normalized.startswith("reuse the sealed reconnaissance")
    )


def _is_recon_handback_continuation(content: str) -> bool:
    return is_recon_handback_coordination(content)


def _primary_target(content: str) -> str | None:
    match = _URL_RE.search(content)
    if match is None:
        return None
    raw_url = match.group(0).rstrip(".,;:!?")
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname.casefold()
    host = f"[{hostname}]" if ":" in hostname else hostname
    authority = host if port is None else f"{host}:{port}"
    return urlunsplit((scheme, authority, parsed.path or "/", "", ""))


def _has_url_candidate(content: str) -> bool:
    return has_url_candidate(content)


def _configured_objective(messages: Sequence[ChatMessage]) -> str | None:
    receipt = _configured_campaign_receipt(messages)
    settings = receipt.get("settings")
    if not isinstance(settings, Mapping):
        return None
    objective = settings.get("objective")
    return str(objective).strip() if isinstance(objective, str) and objective.strip() else None


@dataclass(frozen=True)
class _CampaignBoundary:
    state: str
    request_id: str | None = None
    objective_digest: str | None = None
    objective: str | None = None
    message_index: int | None = None


def _looks_like_campaign_request(content: str) -> bool:
    normalized = " ".join(content.casefold().split())
    return is_explicit_campaign_launch(content) and (
        _primary_target(content) is not None or "target" in normalized
    )


def _latest_campaign_request_scope(messages: Sequence[ChatMessage]) -> _CampaignBoundary:
    scoped_messages = _messages_after_latest_configuration(messages)
    active_objective = _configured_objective(messages)
    configured_receipt = _configured_campaign_receipt(messages)
    configured_request_id = configured_receipt.get("campaign_request_id")
    request_objectives: dict[str, str] = {}
    invalid_request_ids: set[str] = set()
    if _valid_campaign_request_id(configured_request_id) and active_objective is not None:
        request_objectives[str(configured_request_id)] = (
            "sha256:" + hashlib.sha256(active_objective.encode()).hexdigest()
        )
    boundary = _CampaignBoundary("none")
    for index, message in enumerate(scoped_messages):
        if message.role != "user" or message.content.startswith("MERLIN_USER_INPUT_RESPONSE\n"):
            continue
        envelope = _campaign_submit_payload(message.content)
        if _CLI_CAMPAIGN_SUBMIT_MARKER in message.content and envelope is None:
            boundary = _CampaignBoundary("invalid", message_index=index)
            active_objective = None
            continue
        if envelope is not None:
            objective = str(envelope["operator_objective"]).strip()
            active_objective = objective
            if envelope.get("schema_version") == _CLI_CAMPAIGN_SUBMIT_SCHEMA:
                request_id = str(envelope["campaign_request_id"])
                objective_digest = "sha256:" + hashlib.sha256(objective.encode()).hexdigest()
                if request_id in invalid_request_ids:
                    boundary = _CampaignBoundary("conflict", message_index=index)
                    active_objective = None
                    continue
                prior_digest = request_objectives.get(request_id)
                if prior_digest is not None and prior_digest != objective_digest:
                    invalid_request_ids.add(request_id)
                    boundary = _CampaignBoundary("conflict", message_index=index)
                    active_objective = None
                    continue
                request_objectives[request_id] = objective_digest
                epoch_start = (
                    boundary.message_index
                    if boundary.state == "v2"
                    and boundary.request_id == request_id
                    and boundary.objective_digest == objective_digest
                    else index
                )
                boundary = _CampaignBoundary(
                    "v2",
                    request_id,
                    objective_digest,
                    objective,
                    epoch_start,
                )
            else:
                boundary = _CampaignBoundary("unbound", objective=objective, message_index=index)
            continue
        if _looks_like_campaign_request(message.content):
            active_objective = message.content.strip()
            boundary = _CampaignBoundary("unbound", objective=active_objective, message_index=index)
            continue
        if _is_internal_campaign_continuation(message.content):
            target = _primary_target(message.content)
            if target is None and not _has_url_candidate(message.content):
                continue
            if active_objective is not None and target == _primary_target(active_objective):
                continue
            active_objective = message.content.strip()
            boundary = _CampaignBoundary("unbound", objective=active_objective, message_index=index)
            continue
        if _is_recon_handback_continuation(message.content):
            target = _primary_target(message.content)
            if not _has_url_candidate(message.content) or (
                active_objective is not None and target == _primary_target(active_objective)
            ):
                continue
            active_objective = message.content.strip()
            boundary = _CampaignBoundary("unbound", objective=active_objective, message_index=index)
            continue
        active_objective = None
        boundary = _CampaignBoundary("unbound", message_index=index)
    return boundary


def _messages_after_latest_configuration(
    messages: Sequence[ChatMessage],
) -> Sequence[ChatMessage]:
    """Return messages newer than the latest durable configured receipt."""

    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role != "tool" or message.name != "campaign_configure":
            continue
        if _payload(message).get("status") == "configured":
            return messages[index + 1 :]
    return messages


def _active_campaign_epoch(messages: Sequence[ChatMessage]) -> Sequence[ChatMessage]:
    """Keep the newest substantive request and its subsequent structured answers."""

    scoped_messages = _messages_after_latest_configuration(messages)
    boundary = _latest_campaign_request_scope(messages)
    if boundary.message_index is not None:
        return scoped_messages[boundary.message_index :]
    return scoped_messages


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


def _configured_campaign_receipt(messages: Sequence[ChatMessage]) -> dict[str, Any]:
    for message in reversed(messages):
        if message.role == "tool" and message.name == "campaign_configure":
            payload = _payload(message)
            if payload.get("status") == "configured":
                return payload
    return {}


def _configured_campaign_tool_call_id(messages: Sequence[ChatMessage]) -> str | None:
    for message in reversed(messages):
        if message.role != "tool" or message.name != "campaign_configure":
            continue
        if _payload(message).get("status") != "configured":
            continue
        tool_call_id = message.tool_call_id
        if isinstance(tool_call_id, str) and tool_call_id.strip():
            return tool_call_id.strip()
        return None
    return None


def _settings(messages: Sequence[ChatMessage]) -> dict[str, Any]:
    payload = _configured_campaign_receipt(messages)
    settings = payload.get("settings")
    if isinstance(settings, Mapping):
        return dict(settings)
    return {}


def _latest_prepared_campaign(messages: Sequence[ChatMessage]) -> tuple[str, str] | None:
    for message in reversed(messages):
        if message.role != "tool" or message.name != "assessment_prepare":
            continue
        payload = _payload(message)
        campaign_id = str(payload.get("campaign_id") or "").strip()
        manifest_digest = str(payload.get("manifest_digest") or "").strip()
        if campaign_id and manifest_digest:
            return campaign_id, manifest_digest
    return None


def _operator_campaign_request(
    messages: Sequence[ChatMessage],
    proposed_objective: object,
    *,
    allow_same_target_join: bool = False,
) -> str | None:
    """Recover the exact operator request that contains the proposed target.

    The foreground model may summarize the request before calling
    ``campaign_configure``.  Target URLs give us a precise provenance join back to
    the operator-authored message without treating later model prose as authority.
    """

    boundary = _latest_campaign_request_scope(messages)
    if boundary.state in {"v2", "unbound"}:
        if boundary.objective is not None:
            return boundary.objective
        if allow_same_target_join and boundary.message_index is not None:
            scoped_messages = _messages_after_latest_configuration(messages)
            content = scoped_messages[boundary.message_index].content.strip()
            proposed = str(proposed_objective or "").strip()
            if _primary_target(content) is not None and _primary_target(content) == _primary_target(
                proposed
            ):
                return content
    return None


def _operator_named_profile(messages: Sequence[ChatMessage], objective: str) -> str | None:
    """Return a profile only when operator-authored text explicitly selects it."""

    scoped_messages = _active_campaign_epoch(messages)
    active_request_id: str | None = None
    active_request_start = 0
    for index, message in enumerate(scoped_messages):
        if message.role != "user":
            continue
        envelope = _campaign_submit_payload(message.content)
        if envelope is None or envelope.get("schema_version") != _CLI_CAMPAIGN_SUBMIT_SCHEMA:
            continue
        request_id = envelope.get("campaign_request_id")
        if not _valid_campaign_request_id(request_id):
            continue
        if request_id != active_request_id:
            active_request_id = str(request_id)
            active_request_start = index
    if active_request_id is not None:
        scoped_messages = scoped_messages[active_request_start:]

    for message in reversed(scoped_messages):
        if message.role != "user" or not message.content.startswith("MERLIN_USER_INPUT_RESPONSE\n"):
            continue
        try:
            response = json.loads(message.content.split("\n", 1)[1])
        except (ValueError, TypeError, IndexError):
            continue
        answers = response.get("answers", {}) if isinstance(response, Mapping) else {}
        if not isinstance(answers, Mapping):
            continue
        for answer_id, answer in answers.items():
            if "profile" not in str(answer_id).casefold() or not isinstance(answer, Mapping):
                continue
            option_ids = answer.get("option_ids", ())
            if not isinstance(option_ids, Sequence) or isinstance(option_ids, (str, bytes)):
                continue
            selected = [str(value).casefold() for value in option_ids if str(value) in PROFILES]
            if len(selected) == 1:
                return selected[0]

    annotation_pattern = re.compile(
        r"the operator explicitly selected campaign profile:\s*"
        r"(quickscan|recommended|exhaustive|exploration)\s*;\s*"
        r"do not ask for the profile again\.",
        re.IGNORECASE,
    )
    for message in reversed(scoped_messages):
        if message.role != "user":
            continue
        annotation_text = message.content
        envelope = _campaign_submit_payload(message.content)
        marker_index = message.content.rfind(_CLI_CAMPAIGN_SUBMIT_MARKER)
        if envelope is not None and marker_index >= 0:
            encoded = message.content[marker_index + len(_CLI_CAMPAIGN_SUBMIT_MARKER) :]
            _decoded, envelope_end = json.JSONDecoder().raw_decode(encoded)
            annotation_text = message.content[:marker_index] + encoded[envelope_end:]
            objective = str(envelope["operator_objective"])
            if annotation_text.startswith(objective):
                annotation_text = annotation_text[len(objective) :]
        selections = annotation_pattern.findall(annotation_text)
        if selections:
            return selections[-1].casefold()

    values = [objective]
    values.extend(message.content for message in scoped_messages if message.role == "user")
    for value in reversed(values):
        normalized = " ".join(value.lower().split())
        for profile in PROFILES:
            if re.search(
                rf"\b{re.escape(profile)}\b\s+(?:campaign\s+)?profile\b"
                rf"|\bcampaign\s+profile\b[^.\n]{{0,40}}\b{re.escape(profile)}\b",
                normalized,
            ):
                return profile
        if normalized in PROFILES:
            return normalized
    return None


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
                "When the operator does not name a profile, omit campaign_profile and Merlin "
                "will use Recommended. "
                "For profile, composition, execution-mode, or budget changes, update only the "
                "requested fields; omitted fields retain the selection. Null clears an optional "
                "setting. For a new campaign, omit execution_mode unless the operator explicitly "
                "forces one; omission lets reconnaissance select browser, API, or hybrid from "
                "observed target capabilities. Do not ask for an execution mode by default. "
                "Settings survive resume and are applied to assessment_prepare. Changes "
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
            messages = await self._load(context.session_id)
            configured_receipt = _configured_campaign_receipt(messages)
            campaign_boundary = _latest_campaign_request_scope(messages)
            if campaign_boundary.state == "conflict":
                raise ValueError("campaign_request_id is already bound to a different objective")
            if campaign_boundary.state == "invalid":
                raise ValueError("latest campaign request envelope is malformed")
            configured_request_id = configured_receipt.get("campaign_request_id")
            configured_settings = _settings(messages)
            configured_objective = str(configured_settings.get("objective") or "").strip()
            if (
                campaign_boundary.state == "v2"
                and configured_request_id == campaign_boundary.request_id
                and configured_objective
                and campaign_boundary.objective_digest
                != "sha256:" + hashlib.sha256(configured_objective.encode()).hexdigest()
            ):
                raise ValueError("campaign_request_id is already bound to a different objective")
            fresh_v2_campaign = (
                campaign_boundary.state == "v2"
                and configured_request_id != campaign_boundary.request_id
            )
            explicit_new_campaign = arguments.get("new_campaign") is True
            has_configured_receipt = bool(configured_receipt)
            operator_objective = _operator_campaign_request(
                messages,
                arguments.get("objective"),
                allow_same_target_join=explicit_new_campaign,
            )
            newer_untyped_campaign = (
                has_configured_receipt
                and campaign_boundary.state == "unbound"
                and operator_objective is not None
            )
            starts_new_campaign = (
                not has_configured_receipt or fresh_v2_campaign or newer_untyped_campaign
            )
            settings = {} if starts_new_campaign else configured_settings
            allowed = set(self.describe().input_schema["properties"])
            if set(arguments) - allowed:
                raise ValueError("unknown campaign setting")
            if "new_campaign" in arguments and not isinstance(arguments["new_campaign"], bool):
                raise ValueError("new_campaign must be boolean")
            for key, value in arguments.items():
                if key == "new_campaign":
                    continue
                if key == "objective" and explicit_new_campaign and not starts_new_campaign:
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
            if starts_new_campaign:
                operator_objective = _operator_campaign_request(
                    messages,
                    settings.get("objective"),
                    allow_same_target_join=explicit_new_campaign,
                )
                if operator_objective is not None:
                    settings["objective"] = operator_objective
                    settings["campaign_profile"] = (
                        _operator_named_profile(messages, operator_objective) or "recommended"
                    )
                else:
                    settings.setdefault("campaign_profile", "recommended")
            if starts_new_campaign and not settings.get("objective"):
                raise ValueError("a new campaign requires its exact objective and target")
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
            bound_request_id = (
                campaign_boundary.request_id
                if campaign_boundary.state == "v2"
                else str(configured_request_id)
                if campaign_boundary.state == "none"
                and not newer_untyped_campaign
                and _valid_campaign_request_id(configured_request_id)
                else None
            )
            if bound_request_id is not None:
                result["campaign_request_id"] = bound_request_id
            metadata = dict(result)
            if starts_new_campaign:
                metadata["preferred_next_tool"] = "browser_recall_reconnaissance"
                metadata["preferred_next_tool_mode"] = "after_one_admitted_action"
            return ToolResult(content=json.dumps(result, sort_keys=True), metadata=metadata)
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
        prepare_scope_digest: str | None = None
        prepare_scope_request_id: str | None = None
        prepare_cache_scope: str | None = None
        values = dict(arguments)
        if self.describe().name == "assessment_prepare":
            configured_receipt = _configured_campaign_receipt(messages)
            configured_settings = configured_receipt.get("settings")
            settings = dict(configured_settings) if isinstance(configured_settings, Mapping) else {}
            if not settings.get("objective") or not settings.get("campaign_profile"):
                return ToolResult(
                    content="Record the operator's target, objective and selected profile with "
                    "campaign_configure before preparing the campaign.",
                    is_error=True,
                )
            values.update({k: v for k, v in settings.items() if k != "execution_mode"})
            prepare_scope_digest = (
                "sha256:" + hashlib.sha256(str(settings["objective"]).strip().encode()).hexdigest()
            )
            newer_campaign_boundary = _latest_campaign_request_scope(messages)
            configured_request_id = configured_receipt.get("campaign_request_id")
            if newer_campaign_boundary.state in {"unbound", "invalid", "conflict"}:
                return ToolResult(
                    content=(
                        "A newer operator campaign request requires campaign_configure "
                        "before preparation."
                    ),
                    is_error=True,
                )
            if newer_campaign_boundary.state == "v2" and (
                configured_request_id != newer_campaign_boundary.request_id
                or newer_campaign_boundary.objective_digest != prepare_scope_digest
            ):
                return ToolResult(
                    content=(
                        "The latest campaign request does not match the configured operator "
                        "request. Call campaign_configure for that request before preparing."
                    ),
                    is_error=True,
                )
            prepare_scope_request_id = (
                newer_campaign_boundary.request_id
                if newer_campaign_boundary.state == "v2"
                else str(configured_request_id)
                if _valid_campaign_request_id(configured_request_id)
                else None
            )
            revision = configured_receipt.get("revision")
            if prepare_scope_request_id is not None:
                prepare_cache_scope = f"request:{prepare_scope_request_id}"
            elif isinstance(revision, str) and revision.strip():
                prepare_cache_scope = f"configure:{revision.strip()}"
            else:
                receipt_call_id = _configured_campaign_tool_call_id(messages)
                prepare_cache_scope = (
                    f"configure-call:{receipt_call_id}"
                    if receipt_call_id is not None
                    else f"uncached:{uuid.uuid4()}"
                )
            if settings.get("execution_mode"):
                values["adapter"] = {
                    **values.get("adapter", {}),
                    "execution_mode": settings["execution_mode"],
                }
        elif self.describe().name == "assessment_execute":
            latest = _latest_prepared_campaign(messages)
            requested = (
                str(values.get("campaign_id") or "").strip(),
                str(values.get("manifest_digest") or "").strip(),
            )
            if latest is not None and requested != latest:
                return ToolResult(
                    content=(
                        f"That campaign approval is superseded by {latest[0]} ({latest[1]}). "
                        "Display the latest prepared review and stop this turn; only the Merlin "
                        "client can approve and execute that exact manifest."
                    ),
                    is_error=True,
                )
            return ToolResult(
                content=(
                    "Campaign execution requires the operator's explicit approval in the "
                    "Merlin client. Display the prepared review and stop this turn; the client "
                    "will execute the exact reviewed manifest after approval."
                ),
                is_error=True,
            )
        scoped_prepare = getattr(self._tool, "invoke_for_configured_scope", None)
        if prepare_cache_scope is not None and callable(scoped_prepare):
            result = await scoped_prepare(
                context,
                values,
                cache_scope=prepare_cache_scope,
            )
        else:
            result = await self._tool.invoke(context, values)
        if self.describe().name == "assessment_prepare":
            result = result.model_copy(
                update={
                    "metadata": {
                        **result.metadata,
                        **(
                            {"repair_scope_campaign_request_id": (prepare_scope_request_id)}
                            if prepare_scope_request_id is not None
                            else {}
                        ),
                        "repair_scope_objective_digest": prepare_scope_digest,
                    }
                }
            )
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
                        "objective_contract",
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
