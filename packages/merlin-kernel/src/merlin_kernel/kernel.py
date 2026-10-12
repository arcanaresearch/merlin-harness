from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from merlin_contracts import (
    AppCommand,
    AppEvent,
    ChatMessage,
    ChatRequest,
    DynamicToolOutput,
    InfrastructureFailureCategory,
    MailboxDeliveryRequest,
    ModelCompleted,
    ModelError,
    ModelUsage,
    ModelWarning,
    Provider,
    ReasoningDelta,
    ReportRequest,
    SwarmRequest,
    TextDelta,
    ToolCall,
    ToolContext,
    ToolResult,
)

from .commands import CommandError, help_text, parse_command
from .context import (
    CONTEXT_WINDOW_ASSUMPTION_TOKENS,
    MERLIN_IDENTITY,
    _compaction_summary_line,
    _tool_pairing_boundary,
    compact_projection,
    estimate_message_tokens,
    estimate_tool_schema_tokens,
    prune_superseded_tool_results,
    request_input_budget,
)
from .failures import failure_event_payload, provider_failure_category, tool_failure_category
from .protocols import (
    AgentService,
    BrowserService,
    FindingService,
    McpService,
    MemoryService,
    PackService,
    PermissionService,
    PromotionService,
    ReadinessService,
    SessionStore,
    Settings,
    SkillService,
    TargetService,
    ToolBroker,
)

_PROVIDER_OWNED_TRANSPORT_RETRIES = frozenset(
    {
        "connect_timeout",
        "read_timeout",
        "write_timeout",
        "pool_timeout",
        "timeout",
        "total_timeout",
        "transport_error",
        "rate_limited",
        "incomplete_stream",
    }
)
_AUTOMATIC_RECON_MARKER = "[merlin-automatic-recon]"
_OWNED_SESSION_CLEANUP_TOOL = "browser_end_owned_session"
_READ_ONLY_LIFECYCLE_DISCOVERY_TOOLS = (
    "browser_snapshot",
    "browser_network_events",
    "browser_compile_api_handoff",
    "assessment_prepare",
)
_OWNED_LIFECYCLE_DISCOVERY_TOOLS = (
    *_READ_ONLY_LIFECYCLE_DISCOVERY_TOOLS,
    _OWNED_SESSION_CLEANUP_TOOL,
)
_LIFECYCLE_DISCOVERY_TOOLS = frozenset(_OWNED_LIFECYCLE_DISCOVERY_TOOLS)
_AUTOMATIC_RECON_CONTINUATION_FIELDS = frozenset(
    {
        "kind",
        "status",
        "progress_key",
        "stage",
        "handoff_id",
        "session_ownership",
        "ownership_evidence_refs",
        "evidence_refs",
        "allowed_tools",
    }
)


def _validated_automatic_recon_continuation(
    value: object,
) -> dict[str, object] | None:
    """Validate the complete sealed cleanup-continuation envelope."""
    if not isinstance(value, Mapping) or set(value) != _AUTOMATIC_RECON_CONTINUATION_FIELDS:
        return None
    ownership = value.get("session_ownership")
    ownership_evidence_refs = value.get("ownership_evidence_refs")
    evidence_refs = value.get("evidence_refs")
    allowed_tools = value.get("allowed_tools")
    expected_tools = (
        _OWNED_LIFECYCLE_DISCOVERY_TOOLS
        if ownership == "owned"
        else _READ_ONLY_LIFECYCLE_DISCOVERY_TOOLS
    )
    if (
        value.get("kind") != "cleanup_discovery"
        or value.get("status") != "required"
        or value.get("stage") != "compile_superseding_lifecycle_handoff"
        or not isinstance(value.get("progress_key"), str)
        or not str(value["progress_key"]).strip()
        or not isinstance(value.get("handoff_id"), str)
        or not str(value["handoff_id"]).strip()
        or ownership not in {"owned", "unknown", "unowned"}
        or not isinstance(ownership_evidence_refs, list)
        or not all(isinstance(item, str) and item.strip() for item in ownership_evidence_refs)
        or (ownership == "owned" and not ownership_evidence_refs)
        or not isinstance(evidence_refs, list)
        or not all(isinstance(item, str) and item.strip() for item in evidence_refs)
        or not isinstance(allowed_tools, list)
        or tuple(allowed_tools) != expected_tools
    ):
        return None
    return dict(value)


def _campaign_review_event_payload(name: str, result: ToolResult) -> dict[str, object]:
    """Keep approval data intact when the broker bounds the model-facing text."""
    prepared = result.metadata
    if (
        name != "assessment_prepare"
        or result.is_error
        or not prepared.get("campaign_id")
        or not prepared.get("manifest_digest")
        or not isinstance(prepared.get("review"), Mapping)
    ):
        return {}
    review = dict(prepared["review"])
    learning = review.get("cross_campaign_learning")
    if isinstance(learning, Mapping):
        # Per-cell audit records stay in the tool result/manifest. The review
        # renders their aggregate counts, not this potentially megabyte-sized list.
        review["cross_campaign_learning"] = {
            key: value
            for key, value in learning.items()
            if key not in {"decisions", "partial_cells"}
        }
    return {
        "campaign_review": {
            key: value
            for key, value in prepared.items()
            if key not in {"review", "result_truncated", "original_bytes"}
        }
        | {"review": review}
    }


def _automatic_recon_continuation(result: ToolResult) -> dict[str, object] | None:
    """Accept only the narrow typed internal continuation contract."""
    if not result.is_error:
        return None
    return _validated_automatic_recon_continuation(
        result.metadata.get("automatic_recon_continuation")
    )


def _automatic_tool_progress_fingerprint(
    call: ToolCall, result: ToolResult, *, progress_key: str
) -> str:
    """Identify a durable recon stage/evidence transition, ignoring response prose noise."""
    evidence = result.metadata.get("evidence_refs")
    if not isinstance(evidence, (list, tuple)):
        evidence = []
    handoff = result.metadata.get("handoff_id")
    if handoff is None and result.metadata.get("compiled_adapter") is True:
        try:
            decoded = json.loads(result.content)
        except (TypeError, json.JSONDecodeError):
            decoded = None
        handoff = decoded.get("id") if isinstance(decoded, Mapping) else None
    return json.dumps(
        {
            "progress_key": progress_key,
            "stage": call.name,
            "evidence_refs": sorted(str(item) for item in evidence),
            "superseding_handoff": handoff if isinstance(handoff, str) else None,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _durable_automatic_recon_marker(
    tool_name: str, result: ToolResult, *, progress_key: str | None = None
) -> str | None:
    continuation = _automatic_recon_continuation(result)
    if continuation is not None:
        payload: dict[str, object] = {"state": "held", "continuation": continuation}
    elif tool_name == "assessment_prepare":
        payload = {"state": "prepared" if not result.is_error else "failed"}
    elif progress_key is not None and tool_name in _LIFECYCLE_DISCOVERY_TOOLS:
        payload = {
            "state": "progress",
            "progress": _automatic_tool_progress_fingerprint(
                ToolCall(call_id="durable", name=tool_name, arguments={}),
                result,
                progress_key=progress_key,
            ),
        }
    else:
        return None
    return _AUTOMATIC_RECON_MARKER + json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _tool_content_with_automatic_marker(
    tool_name: str, result: ToolResult, *, progress_key: str | None = None
) -> str:
    marker = _durable_automatic_recon_marker(tool_name, result, progress_key=progress_key)
    return result.content if marker is None else f"{result.content}\n{marker}"


def _restored_automatic_recon(
    transcript: list[ChatMessage],
) -> tuple[dict[str, object] | None, set[str]]:
    continuation: dict[str, object] | None = None
    progress: set[str] = set()
    trusted_calls = {
        str(call.get("id")): str(call["function"].get("name"))
        for message in transcript
        if message.role == "assistant"
        for call in message.tool_calls
        if isinstance(call, Mapping)
        and isinstance(call.get("function"), Mapping)
        and call["function"].get("name") in _LIFECYCLE_DISCOVERY_TOOLS
    }
    for message in transcript:
        if (
            message.role != "tool"
            or not message.tool_call_id
            or trusted_calls.get(message.tool_call_id) != message.name
            or _AUTOMATIC_RECON_MARKER not in message.content
        ):
            continue
        encoded = message.content.rsplit(_AUTOMATIC_RECON_MARKER, 1)[-1]
        try:
            payload = json.loads(encoded)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, Mapping):
            continue
        state = payload.get("state")
        if (
            message.name == "assessment_prepare"
            and state == "held"
            and isinstance(payload.get("continuation"), Mapping)
        ):
            candidate = _validated_automatic_recon_continuation(payload["continuation"])
            if candidate is not None:
                if candidate.get("session_ownership") == "owned":
                    handoff_id = str(candidate["handoff_id"])
                    candidate = {
                        **candidate,
                        "progress_key": f"{handoff_id}:session_not_owned",
                        "session_ownership": "unknown",
                        "ownership_evidence_refs": [],
                        "allowed_tools": list(_READ_ONLY_LIFECYCLE_DISCOVERY_TOOLS),
                    }
                prior_key = (
                    str(continuation.get("progress_key")) if continuation is not None else None
                )
                if prior_key != str(candidate["progress_key"]):
                    progress.clear()
                continuation = candidate
        elif message.name == "assessment_prepare" and state == "prepared":
            continuation = None
            progress.clear()
        elif state == "progress" and isinstance(payload.get("progress"), str):
            progress.add(str(payload["progress"]))
    return continuation, progress


def _sanitize_mcp_error(message: str) -> str:
    sanitized = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|bearer\s+|"
        r"token\s*[:=]\s*|api[-_ ]?key\s*[:=]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        message,
    )
    sanitized = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "?", sanitized)
    return sanitized[:1_000] or "MCP operation failed"


def _promises_immediate_tool_action(text: str) -> bool:
    """Recognize explicit near-term action commitments, not ordinary future discussion."""
    normalized = " ".join(text.lower().split())
    commitments = (
        "i'll continue",
        "i will continue",
        "i'm continuing",
        "i am continuing",
        "let me inspect",
        "let me check",
        "let me open",
        "let me capture",
        "let me run",
        "let me submit",
        "now i'll",
        "now i will",
        "proceeding with",
    )
    return any(commitment in normalized for commitment in commitments)


_logger = logging.getLogger(__name__)

_JEV_DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
_JEV_MODEL = "jev-latest"
_JEV_RETRYABLE_STATUS_CODES = {429, 529}
_JEV_ACTION_QUESTION = (
    "Does this message commit the assistant to performing a concrete action "
    "(tool call, request, submission) as its immediate next step, rather than "
    "merely discussing future possibilities?"
)
_JEV_ACTION_TRUE = "The message commits the assistant to a concrete immediate action"
_JEV_ACTION_FALSE = "The message only discusses possibilities or future actions"
_JEV_ACTION_THRESHOLD = 0.7


def _jev_post(
    url: str,
    token: str,
    payload: Mapping[str, object],
    timeout_seconds: float,
) -> Mapping[str, object]:
    """POST one Jev request with one retry on retryable status codes.

    Inline mirror of ``merlin_agents.jev.jev_post``; merlin-kernel must not
    depend on merlin-agents, so the minimal transport lives here.
    """
    for attempt in range(2):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                value = json.loads(response.read().decode())
            if not isinstance(value, Mapping):
                raise ValueError("jev returned a non-object response")
            return value
        except urllib.error.HTTPError as error:
            if error.code not in _JEV_RETRYABLE_STATUS_CODES or attempt:
                raise
            time.sleep(1.0)
    raise AssertionError("unreachable")


def _group_tool_responses(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Project mid-tool user messages after complete tool groups, without losing text."""
    projected: list[ChatMessage] = []
    deferred: list[ChatMessage] = []
    pending: set[str] = set()
    for message in messages:
        if pending and message.role == "user":
            deferred.append(message)
            continue
        projected.append(message)
        if message.role == "assistant" and message.tool_calls:
            pending.update(str(call["id"]) for call in message.tool_calls if "id" in call)
        elif message.role == "tool" and message.tool_call_id:
            pending.discard(message.tool_call_id)
            if not pending:
                projected.extend(deferred)
                deferred.clear()
    projected.extend(deferred)
    return projected


class _KernelJevClient:
    """Minimal advisory Jev noul client; every failure returns ``None``."""

    def __init__(self, token: str, url: str = _JEV_DEFAULT_URL, timeout: float = 10.0) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout
        self.input_tokens = 0
        self.output_tokens = 0

    async def noul(
        self,
        state: str,
        question: str,
        *,
        true_meaning: str,
        false_meaning: str,
    ) -> float | None:
        """Ask a yes/no question; return the probability of "yes" or ``None``."""
        payload: dict[str, object] = {
            "state": state,
            "model": _JEV_MODEL,
            "questions": {
                "question": {
                    "type": "noul",
                    "instructions": question,
                    "criteria": {"true": true_meaning, "false": false_meaning},
                }
            },
        }
        try:
            result = await asyncio.to_thread(
                _jev_post, self._url, self._token, payload, self._timeout
            )
        except Exception:  # noqa: BLE001 - decisions are advisory; never raise
            _logger.debug("jev decision call failed", exc_info=True)
            return None
        usage = result.get("usage")
        if isinstance(usage, Mapping):
            self.input_tokens += int(usage.get("input_tokens", 0))
            self.output_tokens += int(usage.get("output_tokens", 0))
        answers = result.get("answers")
        answer = answers.get("question") if isinstance(answers, Mapping) else None
        if not isinstance(answer, Mapping):
            return None
        try:
            return float(answer.get("noul"))
        except (TypeError, ValueError):
            return None


def _load_jev_client() -> _KernelJevClient | None:
    """Build the kernel Jev client from config; ``None`` when unavailable/disabled."""
    try:
        from merlin_config import load_config
    except ImportError:
        return None
    try:
        config = load_config()
    except Exception:  # noqa: BLE001 - advisory gate; never raise
        return None
    judgments = getattr(config, "judgments", None)
    if judgments is None or not getattr(judgments, "jev_decisions_enabled", False):
        return None
    token = getattr(judgments, "jev_token", None)
    if not token:
        return None
    return _KernelJevClient(
        token=str(token), url=str(getattr(judgments, "jev_url", _JEV_DEFAULT_URL))
    )


def _is_orphan_tool_result(message: ChatMessage, orphan_ids: frozenset[str]) -> bool:
    """True when a tool result has no matching call and must skip provider replay."""
    return (
        message.role == "tool"
        and message.tool_call_id is not None
        and message.tool_call_id in orphan_ids
    )


class _AwaitingUserInput(Exception):
    """A durable question suspends the foreground until the operator responds."""


class Kernel:
    """Owns one active conversation and exposes ordered events to the UI."""

    def __init__(
        self,
        provider: Provider,
        store: SessionStore,
        tools: ToolBroker,
        settings: Settings,
        workspace_root: Path | str = ".",
        *,
        packs: PackService | None = None,
        targets: TargetService | None = None,
        skills: SkillService | None = None,
        mcp: McpService | None = None,
        agents: AgentService | None = None,
        findings: FindingService | None = None,
        promotions: PromotionService | None = None,
        permissions: PermissionService | None = None,
        browser: BrowserService | None = None,
        memory: MemoryService | None = None,
        readiness: ReadinessService | None = None,
        campaign_context: Callable[[str], Awaitable[str | None]] | None = None,
        max_workers: int = 3,
        max_provider_retries: int = 3,
        max_output_tokens_per_request: int = 8_192,
        tool_timeout_seconds: float = 30,
        capability_registry: object | None = None,
    ) -> None:
        if not 1 <= max_workers <= 3:
            raise ValueError("max_workers must be between 1 and 3")
        if max_provider_retries < 0:
            raise ValueError("max_provider_retries must be non-negative")
        if max_output_tokens_per_request < 1:
            raise ValueError("max output tokens per request must be positive")
        if tool_timeout_seconds <= 0:
            raise ValueError("tool timeout seconds must be positive")
        self._provider, self._store, self._tools = provider, store, tools
        self._settings = settings
        self._workspace_root = str(Path(workspace_root).resolve())
        self._packs, self._targets, self._skills = packs, targets, skills
        self._mcp = mcp
        self._agents = agents
        self._findings = findings
        self._promotions = promotions
        self._permissions = permissions
        self._browser = browser
        self._memory = memory
        self._readiness = readiness
        self._campaign_context = campaign_context
        self._max_workers = max_workers
        self._max_provider_retries = max_provider_retries
        self._max_output_tokens_per_request = max_output_tokens_per_request
        self._tool_timeout_seconds = tool_timeout_seconds
        self._capability_registry = capability_registry
        self._session_id: str | None = None
        self._turn: asyncio.Task[None] | None = None
        self._steering_lock = asyncio.Lock()
        self._steering_version = 0
        self._assessment: asyncio.Task[None] | None = None
        self._swarm: asyncio.Task[None] | None = None
        self._events: asyncio.Queue[AppEvent] = asyncio.Queue()
        self._jev_client_checked = False
        self._jev_client: _KernelJevClient | None = None

    def _jev_action_client(self) -> _KernelJevClient | None:
        """Lazily build the Jev client; ``None`` when unavailable/disabled."""
        if self._jev_client_checked:
            return self._jev_client
        self._jev_client_checked = True
        self._jev_client = _load_jev_client()
        return self._jev_client

    async def _commits_immediate_tool_action(self, text: str) -> bool:
        """Phrase list first; Jev noul second opinion may only turn a no into a yes."""
        if _promises_immediate_tool_action(text):
            return True
        client = self._jev_action_client()
        if client is None:
            return False
        input_before, output_before = client.input_tokens, client.output_tokens
        started = time.monotonic()
        probability = await client.noul(
            text,
            _JEV_ACTION_QUESTION,
            true_meaning=_JEV_ACTION_TRUE,
            false_meaning=_JEV_ACTION_FALSE,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        if probability is None:
            return False
        committed = probability >= _JEV_ACTION_THRESHOLD
        await self._emit(
            self._event(
                "judgment",
                code="action_commitment",
                verdict="committed" if committed else "not_committed",
                confidence=probability if committed else 1.0 - probability,
                graded_by="jev",
                latency_ms=latency_ms,
                input_tokens=client.input_tokens - input_before,
                output_tokens=client.output_tokens - output_before,
            )
        )
        return committed

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def turn_active(self) -> bool:
        return any(
            task is not None and not task.done()
            for task in (self._turn, self._assessment, self._swarm)
        )

    async def start(self, session_id: str | None = None) -> str:
        if session_id and not await self._store.session_exists(session_id):
            raise ValueError(f"session not found: {session_id}")
        self._session_id = session_id or await self._store.create_session(
            model=await self._settings.get_model()
        )
        await self._emit(self._event("session", text="session ready"))
        return self._session_id

    async def submit(self, text: str) -> None:
        try:
            command = parse_command(text)
        except CommandError as exc:
            await self._emit(self._event("error", text=str(exc)))
            return
        if command:
            try:
                await self._route(command)
            except Exception as exc:
                detail = str(exc).strip() or type(exc).__name__
                await self._emit(
                    self._event(
                        "error",
                        text=f"/{command.name} failed: {detail[:500]}",
                        command=command.name,
                    )
                )
            return
        if self.turn_active:
            await self._emit(self._event("error", text="a turn is already active"))
            return
        session_id = await self._ensure_session()
        await self._store.append_message(session_id, ChatMessage(role="user", content=text))
        await self._emit(self._event("message", text=text, role="user"))
        self._turn = asyncio.create_task(self._run_turn(session_id, text))

    async def wait_for_turn(self) -> None:
        task = next(
            (
                task
                for task in (self._turn, self._assessment, self._swarm)
                if task is not None and not task.done()
            ),
            None,
        )
        if task:
            with suppress(asyncio.CancelledError):
                await task

    async def steer(self, text: str) -> bool:
        """Persist operator follow-up without cancelling or replacing the active turn."""
        if not text.strip() or text.lstrip().startswith("/"):
            return False
        async with self._steering_lock:
            if self._turn is None or self._turn.done() or self._session_id is None:
                return False
            await self._store.append_message(
                self._session_id, ChatMessage(role="user", content=text)
            )
            self._steering_version += 1
            await self._emit(
                self._event(
                    "message",
                    text=text,
                    role="user",
                    foreground_followup=True,
                )
            )
            return True

    async def interrupt(self) -> bool:
        if not self.turn_active:
            return False
        if (
            self._swarm is not None
            and not self._swarm.done()
            and self._agents is not None
            and self._session_id is not None
        ):
            await self._agents.stop_swarm(self._session_id)
        tasks = [
            task
            for task in (self._turn, self._assessment, self._swarm)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await self._emit(self._event("command", text="turn interrupted"))
        return True

    async def next_event(self) -> AppEvent:
        return await self._events.get()

    async def branch(self, sequence: int) -> str:
        """Fork the visible transcript without modifying the source session."""
        session_id = await self._ensure_session()
        brancher = getattr(self._store, "branch_session", None)
        if brancher is None:
            raise RuntimeError("conversation branching is not supported by this session store")
        branch_id = await brancher(session_id, sequence)
        self._session_id = branch_id
        await self._emit(self._event("session", text="conversation branched", source=session_id))
        return branch_id

    async def rewind(self, sequence: int) -> None:
        """Move the context projection head; durable transcript rows remain immutable."""
        session_id = await self._ensure_session()
        rewinder = getattr(self._store, "rewind_session", None)
        if rewinder is None:
            raise RuntimeError("conversation rewind is not supported by this session store")
        await rewinder(session_id, sequence)
        await self._emit(self._event("session", text="conversation rewound", sequence=sequence))

    async def restore(self) -> None:
        """Restore the context projection to the durable transcript head."""
        session_id = await self._ensure_session()
        restorer = getattr(self._store, "restore_session", None)
        if restorer is None:
            raise RuntimeError("conversation restoration is not supported by this session store")
        await restorer(session_id)
        await self._emit(self._event("session", text="conversation restored"))

    async def events(self) -> AsyncIterator[AppEvent]:
        while True:
            yield await self.next_event()

    async def _run_turn(self, session_id: str, user_text: str) -> None:
        assistant = ""
        reasoning = ""
        provider_failures = 0
        recovery_tool_name: str | None = None
        preferred_next_tool_name: str | None = None
        blocked_recovery_tools: set[str] = set()
        repair_messages: list[ChatMessage] = []
        follow_through_recoveries = 0
        preferred_tool_follow_through_failures = 0
        automatic_recon_continuation: dict[str, object] | None = None
        automatic_no_progress_key: str | None = None
        automatic_progress_fingerprint: str | None = None
        automatic_progress_seen: set[str] = set()
        automatic_progress_key: str | None = None
        automatic_recon_stalled = False
        automatic_state_restored = False
        owned_cleanup_failed = False
        owned_cleanup_evidence_failed = False
        owned_cleanup_completed = False
        owned_cleanup_attempted_keys: set[str] = set()
        consumed_steering_version = self._steering_version
        try:
            skill_messages: list[ChatMessage] = []
            if self._skills is not None:
                instructions = await self._skills.instructions_for(session_id, user_text)
                skill_messages = [
                    ChatMessage(role="system", content=instruction)
                    for instruction in instructions
                    if instruction.strip()
                ]

            async def handle_preferred_tool_miss(tool_name: str) -> str:
                nonlocal preferred_tool_follow_through_failures
                nonlocal repair_messages
                await self._emit(
                    self._event(
                        "warning",
                        code="evaluation_uncertain",
                        kind="evaluation_uncertain",
                        source="provider_tool_follow_through",
                        reason=(
                            "provider returned without invoking the preferred tool; "
                            "its prose or alternate tool choice is not evidence that the "
                            "preferred action is invalid"
                        ),
                        confidence=1.0,
                        preferred_tool=tool_name,
                        nonblocking=True,
                    )
                )
                if preferred_tool_follow_through_failures < self._max_provider_retries:
                    preferred_tool_follow_through_failures += 1
                    repair_messages = [
                        ChatMessage(
                            role="system",
                            content=(
                                "The previous provider response did not invoke the required "
                                f"`{tool_name}` tool. Durable tool evidence takes precedence "
                                "over prose, refusal, speculation, or an alternate tool call. "
                                "Call that exact tool now; its schema is the only tool schema "
                                "provided. Do not repeat or reinterpret the refusal."
                            ),
                        )
                    ]
                    return "retry"
                await self._emit(
                    self._event(
                        "error",
                        code="preferred_tool_not_called",
                        text=(
                            "provider repeatedly returned without invoking the preferred "
                            f"tool `{tool_name}`"
                        ),
                        retryable=True,
                        preferred_tool=tool_name,
                        **failure_event_payload(InfrastructureFailureCategory.DELIVERY_FAILURE),
                    )
                )
                return "exhausted"

            while True:
                request_steering_version = self._steering_version
                request_has_followup = request_steering_version != consumed_steering_version
                consumed_steering_version = request_steering_version
                # Recovery constraints apply to exactly one provider request. A
                # successful repaired tool call must not trap the rest of the
                # turn behind that tool's schema or repair instruction.
                request_recovery_tool_name = recovery_tool_name
                recovery_tool_name = None
                request_preferred_tool_name = preferred_next_tool_name
                request_repair_messages = repair_messages
                repair_messages = []
                if self._permissions is not None and not await self._permissions.authorize_provider(
                    session_id, self._workspace_root
                ):
                    await self._emit(
                        self._event("permission", state="denied", effect="provider_network")
                    )
                    return
                campaign_messages: list[ChatMessage] = []
                if self._campaign_context is not None:
                    campaign_context = await self._campaign_context(session_id)
                    if campaign_context:
                        campaign_messages.append(
                            ChatMessage(role="system", content=campaign_context)
                        )
                if not automatic_state_restored:
                    durable_messages = list(await self._store.load_messages(session_id))
                    restored_continuation, restored_progress = _restored_automatic_recon(
                        durable_messages
                    )
                    if restored_continuation is not None:
                        automatic_recon_continuation = restored_continuation
                        automatic_progress_key = str(restored_continuation.get("progress_key"))
                    automatic_progress_seen.update(restored_progress)
                    automatic_state_restored = True
                model = await self._settings.get_model() or ""
                transcript = await self._provider_transcript(session_id, model=model)
                messages = [
                    MERLIN_IDENTITY,
                    *campaign_messages,
                    *skill_messages,
                    *request_repair_messages,
                    *transcript,
                ]
                capabilities_for_model = getattr(self._provider, "capabilities_for_model", None)
                capabilities = (
                    capabilities_for_model(model)
                    if callable(capabilities_for_model)
                    else getattr(self._provider, "capabilities", None)
                )
                context_window = await self._context_window_tokens(capabilities, model=model)
                described_tools = tuple(
                    spec
                    for spec in self._tools.describe()
                    if spec.name not in blocked_recovery_tools
                )
                available_tools = tuple(
                    spec for spec in described_tools if spec.name != _OWNED_SESSION_CLEANUP_TOOL
                )
                if automatic_recon_continuation is not None:
                    continuation_tools = automatic_recon_continuation.get("allowed_tools", [])
                    continuation_source = (
                        described_tools
                        if automatic_recon_continuation.get("session_ownership") == "owned"
                        else available_tools
                    )
                    available_tools = tuple(
                        spec for spec in continuation_source if spec.name in continuation_tools
                    )
                    owned_cleanup = automatic_recon_continuation.get("session_ownership") == "owned"
                    messages.append(
                        ChatMessage(
                            role="system",
                            content=(
                                "Continue the internal reconnaissance automatically. Establish "
                                "whether the exact session-creation and cleanup evidence is "
                                "already present using only the admitted lifecycle browser "
                                "tools, then "
                                "call `assessment_prepare` again only with a superseding compiled "
                                "handoff. "
                                + (
                                    "For the exact owned reconnaissance session only, call the "
                                    "dedicated cleanup tool on its visible session-ending, cancel, "
                                    "or forfeit control; "
                                    "then observe the typed cleanup response and compile the "
                                    "superseding handoff. Do not send a message, upload content, "
                                    "or create another session. "
                                    if owned_cleanup
                                    else "Do not click, send, upload, or tear down a target "
                                    "session; remain safely held when evidence is absent. "
                                )
                                + "Do not ask "
                                "the operator "
                                "for permission or report success while preparation is held. "
                                "Preserve attack candidates offline."
                            ),
                        )
                    )
                if request_recovery_tool_name is not None:
                    available_tools = tuple(
                        spec for spec in available_tools if spec.name == request_recovery_tool_name
                    )
                elif request_preferred_tool_name is not None:
                    available_tools = tuple(
                        spec for spec in available_tools if spec.name == request_preferred_tool_name
                    )
                    if available_tools:
                        messages.append(
                            ChatMessage(
                                role="system",
                                content=(
                                    "Durable tool evidence takes precedence over provider prose, "
                                    "refusal, or speculation. "
                                    f"The preceding tool established the next bounded action. "
                                    f"Call `{request_preferred_tool_name}` now using the durable "
                                    "evidence already in the transcript. Do not perform more "
                                    "reconnaissance or optional lifecycle discovery first."
                                ),
                            )
                        )
                tool_schemas = [
                    {
                        "type": "function",
                        "function": {
                            "name": spec.name,
                            "description": spec.description,
                            "parameters": spec.input_schema,
                        },
                    }
                    for spec in available_tools
                ]
                advertised_tool_names = frozenset(spec.name for spec in available_tools)
                request = ChatRequest(
                    model=model,
                    messages=_compact_messages(
                        messages,
                        context_window_tokens=context_window,
                        reserved_output_tokens=self._max_output_tokens_per_request,
                        reserved_overhead_tokens=estimate_tool_schema_tokens(tool_schemas),
                    ),
                    tools=tool_schemas,
                    max_output_tokens=self._max_output_tokens_per_request,
                )
                used_tool = False
                tool_calls: list[ToolCall] = []
                completed: ModelCompleted | None = None
                reasoning = ""
                retry_provider = False
                preferred_tool_invoked_dynamically = False

                async def handle_dynamic_tool(
                    call: ToolCall,
                    _advertised_tool_names: frozenset[str] = advertised_tool_names,
                    _request_preferred_tool_name: str | None = request_preferred_tool_name,
                ) -> DynamicToolOutput:
                    nonlocal automatic_no_progress_key
                    nonlocal automatic_progress_fingerprint
                    nonlocal automatic_progress_key
                    nonlocal automatic_recon_continuation
                    nonlocal automatic_recon_stalled
                    nonlocal owned_cleanup_failed
                    nonlocal owned_cleanup_evidence_failed
                    nonlocal owned_cleanup_completed
                    nonlocal preferred_next_tool_name
                    nonlocal preferred_tool_follow_through_failures
                    nonlocal preferred_tool_invoked_dynamically
                    await self._store.append_message(
                        session_id,
                        ChatMessage(
                            role="assistant",
                            content="",
                            tool_calls=[
                                {
                                    "id": call.call_id,
                                    "type": "function",
                                    "function": {
                                        "name": call.name,
                                        "arguments": json.dumps(call.arguments),
                                    },
                                }
                            ],
                        ),
                    )
                    await self._emit(self._event("tool", name=call.name, state="started"))
                    owned_cleanup_key = (
                        str(automatic_recon_continuation.get("progress_key"))
                        if automatic_recon_continuation is not None
                        else ""
                    )
                    is_required_owned_cleanup = (
                        call.name == _request_preferred_tool_name == _OWNED_SESSION_CLEANUP_TOOL
                    )
                    duplicate_owned_cleanup = (
                        is_required_owned_cleanup
                        and owned_cleanup_key in owned_cleanup_attempted_keys
                    )
                    if call.name == _request_preferred_tool_name and not duplicate_owned_cleanup:
                        # Invocation discharges the obligation even when the tool returns a
                        # typed error. Tool integrity failures are evidence, not provider prose.
                        preferred_next_tool_name = None
                        preferred_tool_follow_through_failures = 0
                        preferred_tool_invoked_dynamically = True
                    context = ToolContext(
                        session_id=session_id,
                        workspace_root=self._workspace_root,
                        call_id=call.call_id,
                    )
                    tool_admitted = (
                        call.name in _advertised_tool_names
                        and (
                            call.name != _OWNED_SESSION_CLEANUP_TOOL
                            or (
                                automatic_recon_continuation is not None
                                and automatic_recon_continuation.get("session_ownership") == "owned"
                            )
                        )
                        and not duplicate_owned_cleanup
                    )
                    if is_required_owned_cleanup and not duplicate_owned_cleanup:
                        owned_cleanup_attempted_keys.add(owned_cleanup_key)
                    try:
                        if not tool_admitted:
                            raise PermissionError("tool is not admitted for this provider request")
                        async with asyncio.timeout(self._tool_timeout_seconds):
                            result = await self._tools.invoke(call.name, context, call.arguments)
                    except asyncio.CancelledError:
                        await self._append_tool_terminal(
                            session_id,
                            name=call.name,
                            call_id=call.call_id,
                            content="tool call interrupted before completion",
                        )
                        await self._emit(
                            self._event(
                                "tool",
                                name=call.name,
                                text="tool call interrupted before completion",
                                state="interrupted",
                            )
                        )
                        raise
                    except TimeoutError:
                        result = ToolResult(
                            content=(
                                f"{call.name} timed out after "
                                f"{self._tool_timeout_seconds:g} seconds"
                            ),
                            is_error=True,
                            metadata={
                                "infrastructure_failure_category": (
                                    InfrastructureFailureCategory.ADAPTER_TIMEOUT.value
                                )
                            },
                        )
                    except Exception as error:
                        result = ToolResult(
                            content=f"{call.name} failed: {error}",
                            is_error=True,
                            metadata={
                                "infrastructure_failure_category": (
                                    InfrastructureFailureCategory.DELIVERY_FAILURE.value
                                )
                            },
                        )
                    await self._append_tool_terminal(
                        session_id,
                        name=call.name,
                        call_id=call.call_id,
                        content=_tool_content_with_automatic_marker(
                            call.name,
                            result,
                            progress_key=(
                                str(automatic_recon_continuation.get("progress_key"))
                                if automatic_recon_continuation is not None
                                else None
                            ),
                        ),
                    )
                    failure_category = tool_failure_category(result)
                    await self._emit(
                        self._event(
                            "tool",
                            name=call.name,
                            text=result.content,
                            state="failed" if result.is_error else "completed",
                            **_campaign_review_event_payload(call.name, result),
                            **failure_event_payload(failure_category),
                        )
                    )
                    if call.name == "request_user_input" and not result.is_error:
                        raise _AwaitingUserInput
                    next_tool = result.metadata.get("preferred_next_tool")
                    if not result.is_error and isinstance(next_tool, str):
                        preferred_next_tool_name = next_tool
                        preferred_tool_follow_through_failures = 0
                    if is_required_owned_cleanup and not duplicate_owned_cleanup:
                        owned_cleanup_failed = (
                            result.is_error or next_tool != "browser_compile_api_handoff"
                        )
                        if not owned_cleanup_failed:
                            owned_cleanup_completed = True
                    continuation = _automatic_recon_continuation(result)
                    if continuation is not None:
                        next_key = str(continuation.get("progress_key"))
                        if automatic_progress_key != next_key:
                            automatic_progress_seen.clear()
                        automatic_progress_key = next_key
                        automatic_recon_continuation = continuation
                        automatic_no_progress_key = None
                        if continuation.get("session_ownership") == "owned":
                            if owned_cleanup_completed:
                                owned_cleanup_evidence_failed = True
                            else:
                                preferred_next_tool_name = _OWNED_SESSION_CLEANUP_TOOL
                                preferred_tool_follow_through_failures = 0
                    elif (
                        automatic_recon_continuation is not None
                        and call.name == "assessment_prepare"
                        and not result.is_error
                    ):
                        automatic_recon_continuation = None
                        automatic_no_progress_key = None
                        automatic_progress_seen.clear()
                        automatic_progress_key = None
                    elif (
                        automatic_recon_continuation is not None
                        and call.name.startswith("browser_")
                        and not duplicate_owned_cleanup
                    ):
                        fingerprint = _automatic_tool_progress_fingerprint(
                            call,
                            result,
                            progress_key=str(automatic_recon_continuation.get("progress_key")),
                        )
                        if fingerprint in automatic_progress_seen:
                            automatic_recon_stalled = True
                        else:
                            automatic_progress_fingerprint = fingerprint
                            automatic_progress_seen.add(fingerprint)
                            automatic_no_progress_key = None
                    return DynamicToolOutput(
                        content=result.content,
                        success=not result.is_error,
                    )

                interactive = getattr(self._provider, "stream_with_tools", None)
                provider_stream = (
                    interactive(request, handle_dynamic_tool)
                    if request.tools and callable(interactive)
                    else self._provider.stream(request)
                )
                async for event in provider_stream:
                    if isinstance(event, TextDelta):
                        assistant += event.text
                        await self._emit(self._event("stream", text=event.text))
                    elif isinstance(event, ToolCall):
                        used_tool = True
                        tool_calls.append(event)
                    elif isinstance(event, ReasoningDelta):
                        reasoning += event.text
                        await self._emit(self._event("thinking", text=event.text))
                    elif isinstance(event, ModelUsage):
                        await self._emit(self._event("usage", **event.model_dump(exclude={"type"})))
                    elif isinstance(event, ModelWarning):
                        await self._emit(
                            self._event("warning", code=event.code, text=event.message)
                        )
                    elif isinstance(event, ModelCompleted):
                        completed = event
                    elif isinstance(event, ModelError):
                        failure_category = provider_failure_category(event.code)
                        provider_already_retried_transport = (
                            not event.partial and event.code in _PROVIDER_OWNED_TRANSPORT_RETRIES
                        )
                        if (
                            event.retryable
                            and not provider_already_retried_transport
                            and provider_failures < self._max_provider_retries
                        ):
                            provider_failures += 1
                            malformed_tool_call = event.code in {
                                "malformed_tool_arguments",
                                "malformed_tool_call",
                            }
                            failed_tool_name = event.tool_name or request_recovery_tool_name
                            if (
                                malformed_tool_call
                                and provider_failures == 1
                                and failed_tool_name
                                and any(spec.name == failed_tool_name for spec in available_tools)
                            ):
                                recovery_tool_name = failed_tool_name
                            elif (
                                malformed_tool_call
                                and failed_tool_name
                                and failed_tool_name
                                in {spec.name for spec in self._tools.describe()}
                            ):
                                # A schema-constrained repair was already attempted. Do not
                                # hand the same repeatedly malformed tool back to the model
                                # during this turn: expose the remaining admitted tools so it
                                # can make progress through a simpler route.
                                blocked_recovery_tools.add(failed_tool_name)
                                recovery_tool_name = None
                            elif provider_failures > 1 or not malformed_tool_call:
                                # One schema-constrained retry can repair an isolated
                                # malformed call. Repeated failure must restore the full
                                # admitted tool set so the model can choose the simpler
                                # alternative requested below instead of being trapped on
                                # the same failing schema.
                                recovery_tool_name = None
                            if malformed_tool_call and provider_failures == 1:
                                recovery_strategy = (
                                    "Emit exactly one complete JSON object matching the tool "
                                    "schema."
                                )
                            elif not malformed_tool_call:
                                recovery_strategy = (
                                    "Continue from the durable transcript. Do not repeat completed "
                                    "tool calls; their results are already present. Retry only the "
                                    "next unfinished step."
                                )
                            else:
                                recovery_strategy = (
                                    "The repeatedly malformed tool is unavailable for the rest "
                                    "of this turn. Choose a simpler or "
                                    "purpose-built tool, split the operation into smaller calls, "
                                    "and keep each argument short. If no safe alternative exists, "
                                    "continue without that tool and explain the limitation."
                                )
                            repair_messages = [
                                ChatMessage(
                                    role="system",
                                    content=(
                                        "The previous provider attempt ended with a recoverable "
                                        f"transport or output failure ({event.code}). "
                                        + (
                                            f"Call only `{recovery_tool_name}`; its schema is the "
                                            "only tool schema provided. "
                                            if recovery_tool_name is not None
                                            else ""
                                        )
                                        + "If "
                                        f"calling a tool: {recovery_strategy} Do not repeat prior "
                                        "narration."
                                    ),
                                )
                            ]
                            await self._emit(
                                self._event(
                                    "warning",
                                    code="provider_retry",
                                    text=(
                                        "Provider attempt ended recoverably; resuming safely "
                                        f"({provider_failures}/{self._max_provider_retries})"
                                    ),
                                    **failure_event_payload(failure_category),
                                )
                            )
                            retry_provider = True
                            break
                        await self._emit(
                            self._event(
                                "error",
                                text=event.message,
                                code=event.code,
                                partial=event.partial,
                                **failure_event_payload(failure_category),
                            )
                        )
                        # The received bytes are authoritative even when a provider
                        # mislabels the error as non-partial.
                        if assistant or reasoning:
                            await self._save_assistant(
                                session_id,
                                assistant,
                                reasoning_content=reasoning or None,
                            )
                        return
                if retry_provider:
                    assistant = ""
                    reasoning = ""
                    continue
                if owned_cleanup_failed:
                    await self._emit(
                        self._event(
                            "error",
                            code="owned_session_cleanup_failed",
                            text=(
                                "Owned reconnaissance-session cleanup did not establish "
                                "the required next compile step; preparation remains held "
                                "and retryable."
                            ),
                            retryable=True,
                            **failure_event_payload(InfrastructureFailureCategory.DELIVERY_FAILURE),
                        )
                    )
                    return
                if owned_cleanup_evidence_failed:
                    await self._emit(
                        self._event(
                            "error",
                            code="owned_session_cleanup_evidence_incomplete",
                            text=(
                                "Preparation still reports missing cleanup evidence after the "
                                "owned reconnaissance session was ended; the superseding "
                                "handoff must be repaired without repeating cleanup."
                            ),
                            retryable=True,
                            **failure_event_payload(InfrastructureFailureCategory.DELIVERY_FAILURE),
                        )
                    )
                    return
                if automatic_recon_stalled:
                    await self._emit(
                        self._event(
                            "error",
                            code="automatic_recon_stalled",
                            text=(
                                "Automatic reconnaissance repeated the same action without "
                                "durable progress; preparation remains safely held and retryable."
                            ),
                            retryable=True,
                        )
                    )
                    return
                if used_tool:
                    (
                        next_preferred_tool_name,
                        tool_continuation,
                        tool_progress_fingerprint,
                        prepare_succeeded,
                    ) = await self._invoke_tools(
                        session_id,
                        tool_calls,
                        content=assistant,
                        reasoning_content=reasoning or None,
                        automatic_progress_key=(
                            str(automatic_recon_continuation.get("progress_key"))
                            if automatic_recon_continuation is not None
                            else None
                        ),
                        advertised_tool_names=advertised_tool_names,
                        automatic_recon_continuation=automatic_recon_continuation,
                        required_tool_name=request_preferred_tool_name,
                    )
                    preferred_tool_invoked = request_preferred_tool_name is not None and any(
                        call.name == request_preferred_tool_name for call in tool_calls
                    )
                    if preferred_tool_invoked:
                        preferred_next_tool_name = next_preferred_tool_name
                        preferred_tool_follow_through_failures = 0
                    elif (
                        request_preferred_tool_name is None and next_preferred_tool_name is not None
                    ):
                        preferred_next_tool_name = next_preferred_tool_name
                        preferred_tool_follow_through_failures = 0
                    if request_preferred_tool_name is not None and not preferred_tool_invoked:
                        disposition = await handle_preferred_tool_miss(request_preferred_tool_name)
                        if disposition == "retry":
                            assistant = ""
                            reasoning = ""
                            continue
                        return
                    if (
                        request_preferred_tool_name == _OWNED_SESSION_CLEANUP_TOOL
                        and next_preferred_tool_name != "browser_compile_api_handoff"
                    ):
                        await self._emit(
                            self._event(
                                "error",
                                code="owned_session_cleanup_failed",
                                text=(
                                    "Owned reconnaissance-session cleanup did not establish "
                                    "the required next compile step; preparation remains held "
                                    "and retryable."
                                ),
                                retryable=True,
                                **failure_event_payload(
                                    InfrastructureFailureCategory.DELIVERY_FAILURE
                                ),
                            )
                        )
                        return
                    if request_preferred_tool_name == _OWNED_SESSION_CLEANUP_TOOL:
                        owned_cleanup_completed = True
                    if tool_continuation is not None:
                        next_key = str(tool_continuation.get("progress_key"))
                        if automatic_progress_key != next_key:
                            automatic_progress_seen.clear()
                        automatic_progress_key = next_key
                        automatic_recon_continuation = tool_continuation
                        automatic_no_progress_key = None
                        if tool_continuation.get("session_ownership") == "owned":
                            if owned_cleanup_completed:
                                await self._emit(
                                    self._event(
                                        "error",
                                        code="owned_session_cleanup_evidence_incomplete",
                                        text=(
                                            "Preparation still reports missing cleanup evidence "
                                            "after the owned reconnaissance session was ended; "
                                            "the superseding handoff must be repaired without "
                                            "repeating cleanup."
                                        ),
                                        retryable=True,
                                        **failure_event_payload(
                                            InfrastructureFailureCategory.DELIVERY_FAILURE
                                        ),
                                    )
                                )
                                return
                            preferred_next_tool_name = _OWNED_SESSION_CLEANUP_TOOL
                            preferred_tool_follow_through_failures = 0
                    elif automatic_recon_continuation is not None and prepare_succeeded is True:
                        automatic_recon_continuation = None
                        automatic_no_progress_key = None
                        automatic_progress_seen.clear()
                        automatic_progress_key = None
                    elif (
                        automatic_recon_continuation is not None
                        and tool_progress_fingerprint is not None
                    ):
                        if tool_progress_fingerprint in automatic_progress_seen:
                            await self._emit(
                                self._event(
                                    "error",
                                    code="automatic_recon_stalled",
                                    text=(
                                        "Automatic reconnaissance repeated the same action "
                                        "without durable progress; preparation remains safely "
                                        "held and retryable."
                                    ),
                                    retryable=True,
                                )
                            )
                            return
                        automatic_progress_fingerprint = tool_progress_fingerprint
                        automatic_progress_seen.add(tool_progress_fingerprint)
                        automatic_no_progress_key = None
                    assistant = ""
                else:
                    if completed is None:
                        await self._emit(
                            self._event(
                                "error",
                                code="incomplete_stream",
                                text="provider stream ended without completion",
                                **failure_event_payload(
                                    InfrastructureFailureCategory.DELIVERY_FAILURE
                                ),
                            )
                        )
                        return
                    if (
                        request_preferred_tool_name is not None
                        and not preferred_tool_invoked_dynamically
                    ):
                        disposition = await handle_preferred_tool_miss(request_preferred_tool_name)
                        if disposition == "retry":
                            assistant = ""
                            reasoning = ""
                            continue
                        return
                    if preferred_next_tool_name is not None and (
                        request_preferred_tool_name is None or preferred_tool_invoked_dynamically
                    ):
                        # Dynamic providers execute tools inside the stream, so a tool result
                        # can establish the next obligation without yielding a ToolCall event.
                        assistant = ""
                        reasoning = ""
                        continue
                    if automatic_recon_continuation is not None:
                        progress_key = str(
                            automatic_recon_continuation.get("progress_key") or "unknown"
                        )
                        if automatic_no_progress_key == progress_key:
                            await self._emit(
                                self._event(
                                    "error",
                                    code="automatic_recon_stalled",
                                    text=(
                                        "Automatic reconnaissance made no durable progress; "
                                        "preparation remains safely held and retryable."
                                    ),
                                    retryable=True,
                                )
                            )
                            return
                        automatic_no_progress_key = progress_key
                        assistant = ""
                        reasoning = ""
                        continue
                    if not assistant:
                        if reasoning and provider_failures < self._max_provider_retries:
                            provider_failures += 1
                            assistant = ""
                            reasoning = ""
                            repair_messages = [
                                ChatMessage(
                                    role="system",
                                    content=(
                                        "The previous provider attempt ended after reasoning but "
                                        "returned neither user-visible content nor a tool call. "
                                        "Continue from the durable transcript and now either call "
                                        "one admitted tool or return a concise user-visible "
                                        "response. Do not repeat the hidden reasoning."
                                    ),
                                )
                            ]
                            await self._emit(
                                self._event(
                                    "warning",
                                    code="reasoning_only_retry",
                                    text=(
                                        "Provider ended without content or a tool call; "
                                        "resuming safely "
                                        f"({provider_failures}/{self._max_provider_retries})"
                                    ),
                                )
                            )
                            continue
                        await self._emit(
                            self._event(
                                "error",
                                code="empty_provider_response",
                                text=(
                                    "provider repeatedly ended without user-visible content "
                                    "or a tool call"
                                    if reasoning
                                    else "provider ended without content or a tool call"
                                ),
                                **failure_event_payload(
                                    InfrastructureFailureCategory.DELIVERY_FAILURE
                                ),
                            )
                        )
                        return
                    if completed.disposition != "completed":
                        await self._emit(
                            self._event(
                                "warning",
                                code=completed.disposition,
                                text=f"provider response {completed.disposition}",
                                partial=bool(assistant),
                            )
                        )
                    if (
                        assistant
                        and follow_through_recoveries < 2
                        and await self._commits_immediate_tool_action(assistant)
                        and available_tools
                    ):
                        await self._save_assistant(
                            session_id,
                            assistant,
                            reasoning_content=reasoning or None,
                        )
                        assistant = ""
                        reasoning = ""
                        follow_through_recoveries += 1
                        repair_messages = [
                            ChatMessage(
                                role="system",
                                content=(
                                    "You committed to an immediate next action but ended the "
                                    "response without performing it. Continue now using the "
                                    "admitted tool needed for that action. Do not ask for "
                                    "confirmation and do not repeat prior narration. If the "
                                    "action is impossible, state the concrete blocker instead "
                                    "of promising another action."
                                ),
                            )
                        ]
                        await self._emit(
                            self._event(
                                "warning",
                                code="tool_follow_through",
                                text=(
                                    "Model ended after promising an immediate action; "
                                    "continuing automatically"
                                ),
                            )
                        )
                        continue
                    # Serialize terminal commit with steering acceptance. A message
                    # arriving during generation must receive a provider step even
                    # when the previous response ended without a tool call.
                    async with self._steering_lock:
                        if assistant:
                            await self._save_assistant(
                                session_id, assistant, reasoning_content=reasoning or None
                            )
                        if self._steering_version != request_steering_version:
                            assistant = ""
                            reasoning = ""
                            continue
                        if request_has_followup:
                            repair_messages = [
                                ChatMessage(
                                    role="system",
                                    content=(
                                        "You have responded to the operator's follow-up. "
                                        "Now continue "
                                        "the original work from the retained state, unless their "
                                        "message explicitly cancelled or changed it. "
                                        "Do not restart "
                                        "reconnaissance or repeat completed tool actions. If the "
                                        "original work is already complete or waiting "
                                        "on the operator, "
                                        "state that briefly and finish. Original request: "
                                        + user_text
                                    ),
                                )
                            ]
                            assistant = ""
                            reasoning = ""
                            continue
                        break
        except _AwaitingUserInput:
            return
        except asyncio.CancelledError:
            if assistant:
                await self._save_assistant(
                    session_id, assistant, reasoning_content=reasoning or None
                )
            raise
        except Exception as exc:
            await self._emit(self._event("error", text=str(exc)))

    async def _invoke_tools(
        self,
        session_id: str,
        events: list[ToolCall],
        *,
        content: str = "",
        reasoning_content: str | None = None,
        automatic_progress_key: str | None = None,
        advertised_tool_names: frozenset[str] | None = None,
        automatic_recon_continuation: Mapping[str, object] | None = None,
        required_tool_name: str | None = None,
    ) -> tuple[str | None, dict[str, object] | None, str | None, bool | None]:
        if not events:
            return None, None, None, None
        preferred_next_tool: str | None = None
        automatic_continuation: dict[str, object] | None = None
        last_known_continuation: dict[str, object] | None = None
        automatic_progress_fingerprint: str | None = None
        prepare_succeeded: bool | None = None
        owned_cleanup_invoked = False
        signatures = getattr(self._provider, "tool_call_signatures", None)
        await self._store.append_message(
            session_id,
            ChatMessage(
                role="assistant",
                content=content,
                tool_calls=[
                    {
                        "id": event.call_id,
                        "type": "function",
                        "function": {
                            "name": event.name,
                            "arguments": json.dumps(event.arguments),
                        },
                        **(
                            {"thought_signature": signatures[event.call_id]}
                            if signatures and event.call_id in signatures
                            else {}
                        ),
                    }
                    for event in events
                ],
                reasoning_content=reasoning_content,
            ),
        )
        for position, event in enumerate(events):
            await self._emit(self._event("tool", name=event.name, state="started"))
            context = ToolContext(
                session_id=session_id,
                workspace_root=self._workspace_root,
                call_id=event.call_id,
            )
            duplicate_owned_cleanup = (
                event.name == _OWNED_SESSION_CLEANUP_TOOL and owned_cleanup_invoked
            )
            cleanup_admitted = event.name != _OWNED_SESSION_CLEANUP_TOOL or (
                advertised_tool_names is not None
                and event.name in advertised_tool_names
                and automatic_recon_continuation is not None
                and automatic_recon_continuation.get("session_ownership") == "owned"
                and not duplicate_owned_cleanup
            )
            tool_admitted = cleanup_admitted and (
                required_tool_name is None or event.name == required_tool_name
            )
            try:
                if not tool_admitted:
                    raise PermissionError("tool is not admitted for this provider request")
                if event.name == _OWNED_SESSION_CLEANUP_TOOL:
                    owned_cleanup_invoked = True
                async with asyncio.timeout(self._tool_timeout_seconds):
                    result = await self._tools.invoke(event.name, context, event.arguments)
            except asyncio.CancelledError:
                for interrupted in events[position:]:
                    await self._append_tool_terminal(
                        session_id,
                        name=interrupted.name,
                        call_id=interrupted.call_id,
                        content="tool call interrupted before completion",
                    )
                    await self._emit(
                        self._event(
                            "tool",
                            name=interrupted.name,
                            text="tool call interrupted before completion",
                            state="interrupted",
                        )
                    )
                raise
            except TimeoutError:
                result = ToolResult(
                    content=(
                        f"{event.name} timed out after {self._tool_timeout_seconds:g} seconds"
                    ),
                    is_error=True,
                    metadata={
                        "infrastructure_failure_category": (
                            InfrastructureFailureCategory.ADAPTER_TIMEOUT.value
                        )
                    },
                )
            except Exception as error:
                result = ToolResult(
                    content=f"{event.name} failed: {error}",
                    is_error=True,
                    metadata={
                        "infrastructure_failure_category": (
                            InfrastructureFailureCategory.DELIVERY_FAILURE.value
                        )
                    },
                )
            continuation = _automatic_recon_continuation(result)
            await self._append_tool_terminal(
                session_id,
                name=event.name,
                call_id=event.call_id,
                content=_tool_content_with_automatic_marker(
                    event.name, result, progress_key=automatic_progress_key
                ),
            )
            failure_category = tool_failure_category(result)
            await self._emit(
                self._event(
                    "tool",
                    name=event.name,
                    text=result.content,
                    state="failed" if result.is_error else "completed",
                    **_campaign_review_event_payload(event.name, result),
                    **failure_event_payload(failure_category),
                )
            )
            if event.name == "request_user_input" and not result.is_error:
                # Every call in the assistant batch must have a terminal receipt.
                # Calls after a question are deferred, never executed on guessed input.
                for deferred in events[position + 1 :]:
                    await self._append_tool_terminal(
                        session_id,
                        name=deferred.name,
                        call_id=deferred.call_id,
                        content="Not executed: waiting for the operator's structured answer.",
                    )
                raise _AwaitingUserInput
            next_tool = result.metadata.get("preferred_next_tool")
            if not result.is_error and isinstance(next_tool, str):
                preferred_next_tool = next_tool
            if continuation is not None:
                automatic_continuation = continuation
                last_known_continuation = continuation
            if event.name == "assessment_prepare":
                prepare_succeeded = not result.is_error
                if prepare_succeeded:
                    automatic_continuation = None
                elif continuation is None and last_known_continuation is not None:
                    automatic_continuation = last_known_continuation
            if (
                automatic_progress_key is not None
                and event.name in _LIFECYCLE_DISCOVERY_TOOLS
                and event.name != "assessment_prepare"
            ):
                automatic_progress_fingerprint = _automatic_tool_progress_fingerprint(
                    event, result, progress_key=automatic_progress_key
                )
        return (
            preferred_next_tool,
            automatic_continuation,
            automatic_progress_fingerprint,
            prepare_succeeded,
        )

    async def _save_assistant(
        self, session_id: str, text: str, *, reasoning_content: str | None = None
    ) -> None:
        await self._store.append_message(
            session_id,
            ChatMessage(role="assistant", content=text, reasoning_content=reasoning_content),
        )
        await self._emit(self._event("message", text=text, role="assistant"))

    async def _append_tool_terminal(
        self, session_id: str, *, name: str, call_id: str, content: str
    ) -> None:
        """Persist one terminal tool result even when the owning turn is cancelled."""
        writer = asyncio.create_task(
            self._store.append_message(
                session_id,
                ChatMessage(role="tool", content=content, name=name, tool_call_id=call_id),
            )
        )
        try:
            await asyncio.shield(writer)
        except asyncio.CancelledError:
            await writer
            raise

    async def _provider_transcript(self, session_id: str, *, model: str = "") -> list[ChatMessage]:
        loader = getattr(self._store, "load_context", None)
        if loader is None:
            messages = list(await self._store.load_messages(session_id))
            _, orphan_ids = await self._close_orphaned_tool_calls(session_id, messages)
            replayed = await self._store.load_messages(session_id)
            return _group_tool_responses(
                [message for message in replayed if not _is_orphan_tool_result(message, orphan_ids)]
            )
        projection = await loader(session_id, MERLIN_IDENTITY)
        closed, orphan_ids = await self._close_orphaned_tool_calls(session_id, projection.messages)
        if closed:
            projection = await loader(session_id, MERLIN_IDENTITY)
        resolver = getattr(self._provider, "capabilities_for_model", None)
        capabilities = (
            resolver(model) if callable(resolver) else getattr(self._provider, "capabilities", None)
        )
        projection = compact_projection(
            projection,
            context_window_tokens=await self._context_window_tokens(capabilities, model=model),
        )
        if projection.compaction is not None:
            saver = getattr(self._store, "save_compaction", None)
            if saver is not None:
                await saver(projection.compaction)
        return _group_tool_responses(
            [
                message
                for message in projection.messages
                if not _is_orphan_tool_result(message, orphan_ids)
            ]
        )

    async def _context_window_tokens(self, capabilities: object, *, model: str) -> int:
        """Resolve the routed model's context window, labelling unknowns loudly.

        Resolution order: the route's own provider capabilities (tier-1 operator
        configuration), then the capability registry (built-in catalogue and
        last-good snapshot), and only then the tier-7 conservative assumption
        (the same value as the ``agent_context_fallback_tokens`` default); the
        silence is what died — a warning event is emitted on every assumption.
        """
        value = getattr(capabilities, "context_window_tokens", None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        resolve = getattr(self._capability_registry, "resolve_model", None)
        if callable(resolve) and model:
            route = self._route_identity_for_model(model)
            resolved = resolve(
                model,
                transport=getattr(route, "transport", "openai_compatible"),
                endpoint=getattr(route, "endpoint", ""),
            ).context_window_tokens
            candidate = getattr(resolved, "value", None)
            if (
                isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and candidate > 0
                and not getattr(getattr(resolved, "provenance", None), "assumed", True)
            ):
                return candidate
        await self._emit(
            self._event(
                "warning",
                code="context_window_assumed",
                text=(
                    f"Context window for model '{model or 'default'}' is unknown; assuming "
                    f"{CONTEXT_WINDOW_ASSUMPTION_TOKENS} tokens (tier-7 conservative assumption)"
                ),
            )
        )
        return CONTEXT_WINDOW_ASSUMPTION_TOKENS

    def _route_identity_for_model(self, model: str) -> object | None:
        """The provider's exact RouteIdentity for this model, when it exposes one."""
        resolver = getattr(self._provider, "route_identity_for_model", None)
        if callable(resolver):
            identity = resolver(model)
            if identity is not None:
                return identity
        return getattr(self._provider, "route_identity", None)

    async def _close_orphaned_tool_calls(
        self, session_id: str, messages: tuple[ChatMessage, ...] | list[ChatMessage]
    ) -> tuple[bool, frozenset[str]]:
        """Balance tool pairing before provider replay.

        Returns ``(closed, orphan_result_ids)``: ``closed`` is true when terminal
        results were synthesized for calls missing outputs, and
        ``orphan_result_ids`` carries the symmetric sweep — tool-RESULT messages
        whose call is absent from the transcript. Orphan outputs stay durable
        but must be dropped from provider replay (codex ``remove_orphan_outputs``
        parity); Anthropic-class providers reject them.
        """
        pending: dict[str, str] = {}
        call_ids: set[str] = set()
        for message in messages:
            if message.role == "assistant" and message.tool_calls:
                for call in message.tool_calls:
                    if not isinstance(call, dict) or not isinstance(call.get("id"), str):
                        continue
                    call_ids.add(call["id"])
                    function = call.get("function")
                    name = function.get("name") if isinstance(function, dict) else "unknown"
                    pending[call["id"]] = str(name)
            elif message.role == "tool" and message.tool_call_id:
                pending.pop(message.tool_call_id, None)
        orphan_result_ids = frozenset(
            message.tool_call_id
            for message in messages
            if message.role == "tool"
            and message.tool_call_id
            and message.tool_call_id not in call_ids
        )
        for call_id, name in pending.items():
            await self._append_tool_terminal(
                session_id,
                name=name,
                call_id=call_id,
                content=(
                    "tool call interrupted before completion during process restart; "
                    "inspect durable effect receipts before retrying"
                ),
            )
        if orphan_result_ids:
            await self._emit(
                self._event(
                    "warning",
                    code="orphan_tool_results_dropped",
                    text=(
                        "dropping orphaned tool results from provider replay; "
                        "no matching tool call in the durable transcript: "
                        + ", ".join(sorted(orphan_result_ids))
                    ),
                )
            )
        return bool(pending), orphan_result_ids

    async def _route(self, command: AppCommand) -> None:
        name, argument = command.name, " ".join(command.arguments)
        if name == "help":
            await self._emit(self._event("command", text=help_text()))
        elif name == "setup":
            await self._result("setup", await self._settings.setup())
        elif name == "model":
            if argument:
                await self._settings.set_model(argument)
            await self._result("model", {"model": await self._settings.get_model()})
        elif name == "new":
            await self.interrupt()
            self._session_id = await self._store.create_session(
                model=await self._settings.get_model()
            )
            await self._emit(self._event("session", text="new session"))
        elif name == "sessions":
            await self._result("sessions", await self._store.list_sessions())
        elif name == "resume":
            if not argument:
                await self._emit(self._event("error", text="usage: /resume SESSION_ID"))
            elif self._readiness is not None:
                try:
                    projection = await self._readiness.resume(argument)
                except (KeyError, ValueError):
                    await self._emit(self._event("error", text="session not found"))
                else:
                    await self.interrupt()
                    self._session_id = argument
                    for message in projection.get("messages", []):
                        if isinstance(message, dict):
                            await self._emit(
                                self._event(
                                    "message",
                                    role=message.get("role", "assistant"),
                                    text=message.get("content", ""),
                                    resumed=True,
                                )
                            )
                    await self._result("resume", projection)
                    await self._emit(self._event("session", text="session resumed"))
            elif not await self._store.session_exists(argument):
                await self._emit(self._event("error", text="session not found"))
            else:
                await self.interrupt()
                self._session_id = argument
                await self._emit(self._event("session", text="session resumed"))
        elif name in {"branch", "rewind"}:
            if len(command.arguments) != 1 or not command.arguments[0].isdigit():
                await self._emit(self._event("error", text=f"usage: /{name} MESSAGE_SEQUENCE"))
            else:
                await self.interrupt()
                sequence = int(command.arguments[0])
                if name == "branch":
                    branch_id = await self.branch(sequence)
                    await self._result("branch", {"session_id": branch_id, "sequence": sequence})
                else:
                    await self.rewind(sequence)
                    await self._result(
                        "rewind", {"session_id": self._session_id, "sequence": sequence}
                    )
        elif name == "restore":
            if command.arguments:
                await self._emit(self._event("error", text="usage: /restore"))
            else:
                await self.interrupt()
                await self.restore()
                await self._result("restore", {"session_id": self._session_id})
        elif name == "status":
            value = dict(await self._settings.status())
            value.update(session_id=self._session_id, turn_active=self.turn_active)
            if self._permissions is not None:
                value["permission_mode"] = await self._permissions.get_mode()
            await self._result("status", value)
        elif name == "plugins":
            await self._result("plugins", await self._settings.plugins())
        elif name == "doctor":
            if self._readiness is None:
                await self._emit(self._event("error", text="readiness support is not configured"))
            elif command.arguments == ["config"]:
                await self._result("config", await self._readiness.export_config())
            elif command.arguments:
                await self._emit(self._event("error", text="usage: /doctor [config]"))
            else:
                await self._result("doctor", await self._readiness.doctor())
        elif name in {"logs", "history"}:
            await self._route_readiness_rows(name, command.arguments)
        elif name == "support":
            if self._readiness is None:
                await self._emit(self._event("error", text="readiness support is not configured"))
            elif len(command.arguments) != 1:
                await self._emit(self._event("error", text="usage: /support OUTPUT.zip"))
            else:
                try:
                    output = await self._readiness.support_bundle(
                        command.arguments[0], self._session_id
                    )
                except Exception as error:
                    await self._emit(self._event("error", text=str(error)[:1_000]))
                else:
                    await self._result("support", {"path": output})
        elif name == "mode":
            await self._route_mode(command.arguments)
        elif name == "permissions":
            await self._route_permissions(command.arguments)
        elif name == "targets":
            await self._route_targets(command.arguments)
        elif name == "skills":
            await self._route_skills(command.arguments)
        elif name == "mcp":
            await self._route_mcp(command.arguments)
        elif name == "agents":
            await self._route_agents(command.arguments)
        elif name == "tasks":
            await self._route_tasks(command.arguments)
        elif name == "agent":
            await self._route_agent(command.arguments)
        elif name == "findings":
            await self._route_findings(command.arguments)
        elif name == "report":
            await self._route_report(command.arguments)
        elif name == "memory":
            await self._route_memory(command.arguments)
        elif name == "browser":
            await self._route_browser(command.arguments)
        elif name == "promotion":
            await self._route_promotion(command.arguments)
        elif name == "clear":
            await self.interrupt()
            self._session_id = await self._store.create_session(
                model=await self._settings.get_model()
            )
            await self._emit(self._event("cleared", text="session cleared"))
        elif name == "interrupt":
            if not await self.interrupt():
                await self._emit(self._event("command", text="no active turn"))
        elif name == "quit":
            await self.interrupt()
            await self._emit(self._event("quit", text="quit"))

    async def _route_mode(self, arguments: list[str]) -> None:
        if self._permissions is None:
            await self._emit(self._event("error", text="permission support is not configured"))
        elif len(arguments) != 1 or arguments[0].lower() not in {"prompt", "auto", "yolo"}:
            await self._emit(self._event("error", text="usage: /mode prompt|auto|yolo"))
        else:
            session_id = await self._ensure_session()
            mode = await self._permissions.set_mode(arguments[0].lower(), session_id=session_id)
            await self._result("mode", {"mode": mode})

    async def _route_permissions(self, arguments: list[str]) -> None:
        if self._permissions is None:
            await self._emit(self._event("error", text="permission support is not configured"))
        elif arguments:
            await self._emit(self._event("error", text="usage: /permissions"))
        else:
            session_id = await self._ensure_session()
            await self._result("permissions", await self._permissions.list_permissions(session_id))

    async def _route_memory(self, arguments: list[str]) -> None:
        if self._memory is None:
            await self._emit(self._event("error", text="portable memory is not configured"))
            return
        action = arguments[0].lower() if arguments else ""
        if action == "list" and len(arguments) <= 2:
            status = arguments[1] if len(arguments) == 2 else None
            await self._result("memory", await self._memory.list_memory(status))
        elif action == "import" and len(arguments) == 2:
            await self._result("memory", await self._memory.import_file(arguments[1]))
        elif action == "export" and len(arguments) == 2:
            await self._result("memory", await self._memory.export_file(arguments[1]))
        elif action == "accept" and len(arguments) >= 3:
            await self._result("memory", await self._memory.accept(arguments[1], arguments[2:]))
        elif action in {"reject", "delete"} and len(arguments) >= 4:
            method = self._memory.reject if action == "reject" else self._memory.delete
            await self._result(
                "memory", await method(arguments[1], (arguments[2],), " ".join(arguments[3:]))
            )
        else:
            await self._emit(
                self._event(
                    "error",
                    text=(
                        "usage: /memory list [STATUS]|import PATH|export PATH|"
                        "accept BUNDLE RECORD...|reject BUNDLE RECORD REASON|"
                        "delete BUNDLE RECORD REASON"
                    ),
                )
            )

    async def _route_packs(self, arguments: list[str]) -> None:
        if self._packs is None:
            await self._emit(self._event("error", text="pack support is not configured"))
            return
        action = arguments[0].lower() if arguments else ""
        pack_id = arguments[1] if len(arguments) == 2 else None
        if action == "list" and len(arguments) == 1:
            await self._result("packs", await self._packs.list_packs())
        elif action == "inspect" and pack_id:
            await self._result("packs", await self._packs.inspect_pack(pack_id))
        elif action == "enable" and pack_id:
            await self._result("packs", await self._packs.enable_pack(pack_id))
        elif action == "disable" and pack_id:
            await self._result("packs", await self._packs.disable_pack(pack_id))
        elif action == "run" and len(arguments) <= 2:
            if self.turn_active:
                await self._emit(self._event("error", text="an activity is already running"))
            else:
                self._assessment = asyncio.create_task(self._run_pack(pack_id))
        else:
            await self._emit(
                self._event(
                    "error", text="usage: /packs list|inspect ID|enable ID|disable ID|run [ID]"
                )
            )

    async def _run_pack(self, pack_id: str | None) -> None:
        assert self._packs is not None
        session_id = await self._ensure_session()
        await self._emit(self._event("assessment", state="started", pack_id=pack_id))
        try:
            async for progress in self._packs.run_pack(pack_id, session_id):
                await self._emit(self._event("assessment", **dict(progress)))
        except Exception as exc:
            await self._emit(
                self._event("assessment", state="failed", pack_id=pack_id, text=str(exc))
            )
            return
        await self._emit(self._event("assessment", state="completed", pack_id=pack_id))

    async def _route_targets(self, arguments: list[str]) -> None:
        if self._targets is None:
            await self._emit(self._event("error", text="target support is not configured"))
            return
        action = arguments[0].lower() if arguments else ""
        if action == "list" and len(arguments) == 1:
            await self._result("targets", await self._targets.list_targets())
        elif action == "add" and len(arguments) > 1:
            await self._result("targets", await self._targets.add_target(arguments[1:]))
        elif action == "select" and len(arguments) == 2:
            await self._result("targets", await self._targets.select_target(arguments[1]))
        elif action == "remove" and len(arguments) == 2:
            await self._result("targets", await self._targets.remove_target(arguments[1]))
        else:
            await self._emit(
                self._event("error", text="usage: /targets list|add ARGS...|select ID|remove ID")
            )

    async def _route_skills(self, arguments: list[str]) -> None:
        if self._skills is None:
            await self._emit(self._event("error", text="skill support is not configured"))
            return
        action = arguments[0].lower() if arguments else ""
        name = arguments[1] if len(arguments) == 2 else None
        if action == "list" and len(arguments) == 1:
            session_id = await self._ensure_session()
            await self._result("skills", await self._skills.list_skills(session_id))
        elif action == "inspect" and name:
            await self._result("skills", await self._skills.inspect_skill(name))
        elif action in {"enable", "disable"} and name:
            session_id = await self._ensure_session()
            method = self._skills.enable if action == "enable" else self._skills.disable
            await self._result("skills", await method(session_id, name))
        else:
            await self._emit(
                self._event(
                    "error",
                    text="usage: /skills list|inspect NAME|enable NAME|disable NAME",
                )
            )

    async def _route_mcp(self, arguments: list[str]) -> None:
        if self._mcp is None:
            await self._emit(self._event("error", text="MCP support is not configured"))
            return
        action = arguments[0].lower() if arguments else ""
        name = arguments[1] if len(arguments) == 2 else None
        try:
            if action == "list" and len(arguments) <= 1:
                await self._result("mcp", await self._mcp.list_servers())
            elif action == "add" and len(arguments) > 1:
                # The MCP owner interprets configuration and secret-bearing arguments.
                await self._result("mcp", await self._mcp.add_server(arguments[1:]))
            elif action == "remove" and name:
                await self._result("mcp", await self._mcp.remove_server(name))
            elif action == "check" and name:
                await self._result("mcp", await self._mcp.check_server(name))
            elif action == "restart" and name:
                await self._result("mcp", await self._mcp.restart_server(name))
            elif action == "disable" and name:
                await self._result("mcp", await self._mcp.disable_server(name))
            elif action == "admit" and len(arguments) == 3:
                await self._result("mcp", await self._mcp.admit_server(arguments[1], arguments[2]))
            else:
                await self._emit(
                    self._event(
                        "error",
                        text=(
                            "usage: /mcp list|add NAME [local options] COMMAND [ARGS...]|"
                            "add NAME --remote HTTPS_URL --credential HANDLE "
                            "[--transport streamable_http|sse] [--reconnect 0..3]|"
                            "remove NAME|check NAME|restart NAME|disable NAME|"
                            "admit NAME INVENTORY_DIGEST"
                        ),
                    )
                )
        except Exception as exc:
            await self._emit(self._event("error", text=_sanitize_mcp_error(str(exc))))

    async def _route_agents(self, arguments: list[str]) -> None:
        if self._agents is None:
            await self._emit(self._event("error", text="agent support is not configured"))
        elif arguments:
            await self._emit(self._event("error", text="usage: /agents"))
        else:
            session_id = await self._ensure_session()
            agents = await self._agents.list_agents(session_id)
            await self._result("agents", [agent.model_dump(mode="json") for agent in agents])

    async def _route_tasks(self, arguments: list[str]) -> None:
        if self._agents is None:
            await self._emit(self._event("error", text="agent support is not configured"))
        elif arguments:
            await self._emit(self._event("error", text="usage: /tasks"))
        else:
            session_id = await self._ensure_session()
            tasks = await self._agents.list_tasks(session_id)
            await self._result("tasks", [task.model_dump(mode="json") for task in tasks])

    async def _route_swarm(self, arguments: list[str]) -> None:
        if self._agents is None:
            await self._emit(self._event("error", text="agent support is not configured"))
            return
        action = arguments[0].lower() if arguments else ""
        if action == "start" and len(arguments) > 1:
            if self.turn_active:
                await self._emit(self._event("error", text="an activity is already running"))
                return
            session_id = await self._ensure_session()
            request = SwarmRequest(
                session_id=session_id,
                objective=" ".join(arguments[1:]),
                max_workers=self._max_workers,
            )
            self._swarm = asyncio.create_task(self._run_swarm(request))
        elif action == "stop" and len(arguments) == 1:
            session_id = await self._ensure_session()
            await self._agents.stop_swarm(session_id)
            if self._swarm is not None and not self._swarm.done():
                self._swarm.cancel()
                with suppress(asyncio.CancelledError):
                    await self._swarm
            await self._emit(self._event("swarm", state="stopped"))
        else:
            await self._emit(self._event("error", text="usage: /swarm start OBJECTIVE|stop"))

    async def _run_swarm(self, request: SwarmRequest) -> None:
        assert self._agents is not None
        await self._emit(self._event("swarm", state="started", max_workers=request.max_workers))
        try:
            async for progress in self._agents.start_swarm(request):
                await self._emit(self._event("agent_progress", **progress.model_dump()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(self._event("swarm", state="failed", text=str(exc)))
            return
        await self._emit(self._event("swarm", state="completed"))

    async def _route_agent(self, arguments: list[str]) -> None:
        if self._agents is None:
            await self._emit(self._event("error", text="agent support is not configured"))
            return
        action = arguments[0].lower() if arguments else ""
        session_id = await self._ensure_session()
        if action == "message" and len(arguments) > 2:
            delivery = await self._agents.deliver_message(
                MailboxDeliveryRequest(
                    session_id=session_id,
                    recipient_agent_id=arguments[1],
                    message=" ".join(arguments[2:]),
                )
            )
            await self._emit(
                self._event(
                    "agent_progress",
                    kind="mailbox",
                    state=delivery.state,
                    agent_id=arguments[1],
                    schedule_id=delivery.schedule_id,
                    attempt_id=delivery.attempt_id,
                    lease_generation=delivery.lease_generation,
                    mailbox_message_id=delivery.message_id,
                )
            )
        elif action == "stop" and len(arguments) == 2:
            await self._agents.stop_agent(session_id, arguments[1])
            await self._emit(
                self._event(
                    "agent_progress",
                    kind="agent",
                    state="stop_requested",
                    agent_id=arguments[1],
                )
            )
        else:
            await self._emit(self._event("error", text="usage: /agent message ID TEXT|stop ID"))

    async def _route_findings(self, arguments: list[str]) -> None:
        if self._findings is None:
            await self._emit(self._event("error", text="finding support is not configured"))
            return
        action = arguments[0].lower() if arguments else "list"
        session_id = await self._ensure_session()
        if action == "list" and len(arguments) <= 1:
            findings = await self._findings.list_findings(session_id)
            await self._result(
                "findings", [finding.model_dump(mode="json") for finding in findings]
            )
        elif action in {"accept", "reject"} and len(arguments) == 2:
            method = (
                self._findings.accept_finding
                if action == "accept"
                else self._findings.reject_finding
            )
            finding = await method(session_id, arguments[1])
            await self._emit(
                self._event(
                    "finding",
                    action=action,
                    finding=finding.model_dump(mode="json"),
                )
            )
        else:
            await self._emit(
                self._event("error", text="usage: /findings [list]|accept ID|reject ID")
            )

    async def _route_report(self, arguments: list[str]) -> None:
        if self._findings is None:
            await self._emit(self._event("error", text="finding support is not configured"))
            return
        if arguments and arguments[0].lower() == "compare":
            if len(arguments) != 3:
                await self._report_usage()
                return
            try:
                rows = await self._findings.compare_reports(arguments[1], arguments[2])
            except Exception as exc:
                await self._emit(self._event("report", state="failed", text=str(exc)))
                return
            await self._result(
                "report_comparison",
                {"older": arguments[1], "newer": arguments[2], "changes": list(rows)},
            )
            return
        values = arguments[1:] if arguments and arguments[0].lower() == "run" else arguments
        options: dict[str, str] = {}
        index = 0
        while index < len(values):
            option = values[index]
            if option not in {"--format", "--audience", "--output"} or index + 1 >= len(values):
                await self._report_usage()
                return
            if option in options:
                await self._report_usage()
                return
            options[option] = values[index + 1]
            index += 2
        report_format = options.get("--format", "html")
        default_suffix = {"html": "html", "markdown": "md", "csv": "zip", "json": "json"}.get(
            report_format, "html"
        )
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_value = options.get(
            "--output", f"reports/{timestamp}-merlin-report.{default_suffix}"
        )
        if not output_value.strip() or "\x00" in output_value:
            await self._report_usage()
            return
        session_id = await self._ensure_session()
        try:
            request = ReportRequest(
                session_id=session_id,
                format=report_format,
                audience=options.get("--audience", "internal"),
                output=Path(output_value),
            )
        except ValueError:
            await self._report_usage()
            return
        await self._emit(self._event("report", state="started", **request.model_dump(mode="json")))
        try:
            result = await self._findings.generate_report(request)
        except Exception as exc:
            await self._emit(self._event("report", state="failed", text=str(exc)))
            return
        await self._emit(self._event("report", state="completed", **result.model_dump(mode="json")))

    async def _report_usage(self) -> None:
        await self._emit(
            self._event(
                "error",
                text=(
                    "usage: /report [run] [--format html|markdown|csv|json] "
                    "[--audience internal|customer] [--output PATH]"
                    " | /report compare OLDER_REVISION NEWER_REVISION"
                ),
            )
        )

    async def _route_browser(self, arguments: list[str]) -> None:
        if self._browser is None:
            await self._emit(self._event("error", text="browser support is not configured"))
            return
        action = arguments[0].lower() if arguments else ""
        context_id = arguments[1] if len(arguments) == 2 else None
        session_id = await self._ensure_session()
        try:
            if action == "contexts" and len(arguments) == 1:
                await self._result("browser", await self._browser.list_contexts(session_id))
            elif action == "tabs" and context_id:
                await self._result("browser", await self._browser.list_tabs(session_id, context_id))
            elif action in {"takeover", "cleanup", "resume"} and context_id:
                method = getattr(self._browser, action)
                value = await method(session_id, context_id)
                await self._emit(self._event("browser", action=action, **dict(value)))
            else:
                await self._browser_usage()
        except Exception as error:
            await self._emit(self._event("error", text=str(error)[:1_000]))

    async def _browser_usage(self) -> None:
        await self._emit(
            self._event(
                "error",
                text=(
                    "usage: /browser contexts|tabs CONTEXT|takeover CONTEXT|"
                    "cleanup CONTEXT|resume CONTEXT"
                ),
            )
        )

    async def _route_promotion(self, arguments: list[str]) -> None:
        if self._promotions is None:
            await self._emit(self._event("error", text="promotion support is not configured"))
            return
        action = arguments[0].lower() if arguments else "list"
        try:
            if action == "list" and len(arguments) == 1:
                await self._result("promotion", await self._promotions.list_candidates())
            elif action == "propose" and len(arguments) > 1:
                session_id = await self._ensure_session()
                await self._result(
                    "promotion", await self._promotions.propose(session_id, arguments[1:])
                )
            elif action == "show" and len(arguments) == 2:
                await self._result("promotion", await self._promotions.show_candidate(arguments[1]))
            elif action == "evaluate" and len(arguments) in {2, 3}:
                suite_id = arguments[2] if len(arguments) == 3 else None
                session_id = await self._ensure_session()
                await self._result(
                    "promotion",
                    await self._promotions.evaluate(session_id, arguments[1], suite_id),
                )
            elif action in {"accept", "reject"} and len(arguments) > 2:
                method = self._promotions.accept if action == "accept" else self._promotions.reject
                await self._result("promotion", await method(arguments[1], " ".join(arguments[2:])))
            elif action == "activate" and len(arguments) == 2:
                await self._result("promotion", await self._promotions.activate(arguments[1]))
            elif action == "history" and len(arguments) <= 2:
                kind = arguments[1] if len(arguments) == 2 else None
                await self._result("promotion", await self._promotions.history(kind))
            elif action == "rollback" and len(arguments) == 2:
                await self._result("promotion", await self._promotions.rollback(arguments[1]))
            else:
                raise ValueError(
                    "usage: /promotion propose ARGS...|list|show ID|evaluate ID [SUITE]|"
                    "accept ID REASON|reject ID REASON|"
                    "activate ID|history [KIND]|rollback KIND"
                )
        except Exception as exc:
            await self._emit(self._event("error", text=str(exc)))

    async def _route_readiness_rows(self, name: str, arguments: list[str]) -> None:
        if self._readiness is None:
            await self._emit(self._event("error", text="readiness support is not configured"))
            return
        if len(arguments) > 1 or (arguments and not arguments[0].isdigit()):
            await self._emit(self._event("error", text=f"usage: /{name} [LIMIT]"))
            return
        limit = int(arguments[0]) if arguments else 100
        if not 1 <= limit <= 1_000:
            await self._emit(self._event("error", text=f"usage: /{name} [LIMIT]"))
            return
        if name == "logs":
            rows = await self._readiness.logs(limit=limit)
        else:
            rows = await self._readiness.history(self._session_id, limit=limit)
        await self._result(name, rows)

    async def _result(self, name: str, value: object) -> None:
        await self._emit(self._event("command", command=name, text=json.dumps(value, default=str)))

    async def _ensure_session(self) -> str:
        if not self._session_id:
            await self.start()
        assert self._session_id
        return self._session_id

    def _event(self, type: str, **payload: object) -> AppEvent:
        return AppEvent(
            type=type, payload=payload, session_id=self._session_id, occurred_at=datetime.now(UTC)
        )

    async def _emit(self, event: AppEvent) -> None:
        if event.session_id:
            await self._store.append_event(event.session_id, event)
        await self._events.put(event)


def _compact_messages(
    messages: list[ChatMessage],
    *,
    context_window_tokens: int | None = None,
    reserved_output_tokens: int = 0,
    reserved_overhead_tokens: int = 0,
) -> list[ChatMessage]:
    """Bound provider context while leaving the durable transcript untouched."""
    if not context_window_tokens:
        warnings.warn(
            "context window unknown; assuming "
            f"{CONTEXT_WINDOW_ASSUMPTION_TOKENS} tokens (tier-7 conservative assumption)",
            stacklevel=2,
        )
    effective_window = context_window_tokens or CONTEXT_WINDOW_ASSUMPTION_TOKENS
    # Budget against this request's own routed route: the window minus explicit
    # per-request reservations (output tokens, tool schemas, continuation
    # metadata, safety margin), not a global or smallest-model window.
    budget = request_input_budget(
        effective_window,
        reserved_output_tokens=reserved_output_tokens,
        reserved_overhead_tokens=reserved_overhead_tokens,
    )
    token_limit_reached = sum(estimate_message_tokens(message) for message in messages) >= budget
    if not token_limit_reached:
        return messages
    static = 0
    while static < len(messages) and messages[static].role == "system":
        static += 1
    prune_before = max(static, len(messages) - 48)
    projected = prune_superseded_tool_results(messages, before=prune_before)
    if sum(estimate_message_tokens(message) for message in projected) < budget:
        return projected
    messages = projected
    boundary = _tool_pairing_boundary(messages, max(static, len(messages) - 48))
    removed = messages[static:boundary]
    if not removed:
        return messages
    lines = [f"Earlier durable conversation context compacted ({len(removed)} messages):"]
    for message in removed:
        line = _compaction_summary_line(message)
        lines.append(line)
        if sum(len(line) for line in lines) >= 16 * 1_024:
            break
    summary = ChatMessage(role="system", content="\n".join(lines)[: 16 * 1_024])
    return [*messages[:static], summary, *messages[boundary:]]
