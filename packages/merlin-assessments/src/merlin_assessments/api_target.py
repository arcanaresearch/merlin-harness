from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urljoin

import httpx
from merlin_contracts import (
    InfrastructureFailureCategory,
    ToolContext,
    ToolInteractionRole,
    ToolResult,
    ToolSpec,
    executable_adapter_digest,
)
from merlin_contracts.request_bindings import RequestValueGenerator, bind_request_values
from merlin_contracts.target_adapter_compiler import (
    AdapterCompilation,
    AdapterCompileError,
    AdapterStepRole,
    HttpApi,
    HttpApiStep,
    compile_target_adapter,
    compiled_target_adapter_digest,
    read_target_adapter,
)
from merlin_storage_postgres import (
    LEGACY_API_SESSION_SCOPE_ID,
    CampaignApiSession,
    CampaignApiSessionRepository,
    RemoteSessionClaimConflict,
)


class ReviewedAdapterResolver(Protocol):
    def __call__(self, campaign_id: str) -> Mapping[str, object] | None: ...


class BrowserApiRequester(Protocol):
    async def __call__(
        self,
        campaign_id: str,
        method: str,
        url: str,
        payload: object,
        headers: Mapping[str, str],
    ) -> tuple[int, object]: ...


class BrowserApiTransportError(RuntimeError):
    """A browser-context request ended without a trustworthy target result."""


class _TargetTransportFailure(RuntimeError):
    """An outbound attempt ended without a response, with conservative call accounting."""

    def __init__(self, cause: BaseException, *, attempts: int, interaction: bool) -> None:
        self.exchanges: list[dict[str, object]] = []
        self.target_call_budget_charge = attempts
        self.limit_interaction_count = 1 if interaction and attempts else 0
        super().__init__(str(cause))
        self.__cause__ = cause

    def include_completed_exchanges(self, exchanges: Sequence[Mapping[str, object]]) -> None:
        self.exchanges[:0] = [dict(item) for item in exchanges]
        self.target_call_budget_charge += sum(
            int(item.get("target_call_budget_charge", 1))
            for item in exchanges
            if item.get("cleanup") is not True
        )
        self.limit_interaction_count += sum(
            1
            for item in exchanges
            if item.get("interaction") is True and item.get("cleanup") is not True
        )


class _TargetCleanupRejected(RuntimeError):
    """The target rejected cleanup, so ownership must remain quarantined."""

    infrastructure_failure_category = InfrastructureFailureCategory.DELIVERY_FAILURE

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"reviewed target cleanup returned HTTP {status_code}")


class _TargetCleanupUnverified(RuntimeError):
    """Cleanup returned success status without the reviewed response witness."""

    cleanup_unresolved = True
    infrastructure_failure_category = InfrastructureFailureCategory.UNHEALTHY_TARGET_SESSION


class _TargetSessionStateError(ValueError):
    """Durable session state prevents a trustworthy target interaction."""

    infrastructure_failure_category = InfrastructureFailureCategory.UNHEALTHY_TARGET_SESSION


class _ResponsePointerMissing(ValueError):
    """A reviewed response selector did not resolve against the live body.

    Carries the failing step plus a digest and top-level keys of the observed
    body so a miss is diagnosable without stored response payloads.
    """

    infrastructure_failure_category = InfrastructureFailureCategory.MISSING_RESPONSE_POINTER

    def __init__(self, step_id: str, pointer: str, body: object) -> None:
        self.step_id = step_id
        self.pointer = pointer
        self.interaction = False
        self.body_digest = _digest_json(body)
        self.exchanges: list[dict[str, object]] = []
        self.target_call_budget_charge = 0
        self.limit_interaction_count = 0
        keys = sorted(str(key) for key in body)[:8] if isinstance(body, Mapping) else []
        self.body_keys = tuple(keys)
        detail = f"response keys: {', '.join(keys)}" if keys else "response has no object keys"
        super().__init__(f"reviewed API response pointer missing at step {step_id} ({detail})")

    def include_completed_exchanges(self, exchanges: Sequence[Mapping[str, object]]) -> None:
        self.exchanges[:0] = [dict(item) for item in exchanges]
        self.target_call_budget_charge += sum(
            int(item.get("target_call_budget_charge", 1))
            for item in exchanges
            if item.get("cleanup") is not True
        )
        self.limit_interaction_count += sum(
            1
            for item in exchanges
            if item.get("interaction") is True and item.get("cleanup") is not True
        )


class _TargetSessionTerminal(RuntimeError):
    """A reviewed lifecycle predicate identified a terminal target session."""

    def __init__(
        self,
        *,
        step_id: str,
        response_pointer: str,
        response: object,
        variables: Mapping[str, object],
        exchange: Mapping[str, object],
        replay_safe: bool,
        effect_digest: str,
        input_digest: str,
    ) -> None:
        self.step_id = step_id
        self.response_pointer = response_pointer
        self.response_digest = _digest_json(response)
        self.variables = dict(variables)
        self.exchanges = [dict(exchange)]
        self.replay_safe = replay_safe
        self.effect_digest = effect_digest
        self.input_digest = input_digest
        super().__init__(
            "target returned an error envelope instead of the reviewed "
            f"interaction response at step {step_id}"
        )


def _infrastructure_failure_category(
    error: BaseException,
) -> InfrastructureFailureCategory | None:
    current: BaseException | None = error
    while current is not None:
        typed = getattr(current, "infrastructure_failure_category", None)
        if isinstance(typed, InfrastructureFailureCategory):
            return typed
        if isinstance(typed, str):
            try:
                return InfrastructureFailureCategory(typed)
            except ValueError:
                pass
        if isinstance(current, (TimeoutError, httpx.TimeoutException)):
            return InfrastructureFailureCategory.ADAPTER_TIMEOUT
        current = current.__cause__
    return None


@dataclass(frozen=True, slots=True)
class _ApiStep:
    id: str
    endpoint: str
    method: str
    request: object
    response_pointer: str
    expected_status: int | None
    cleanup: bool
    interaction: bool
    captures: Mapping[str, str]
    repeat: int
    idempotency_key: str | None
    replay_after_ambiguous_result: bool
    oracle_predicates: tuple[_OraclePredicate, ...]
    evaluation_uncertainties: tuple[Mapping[str, object], ...]
    terminal_response: tuple[_TerminalResponseContract, ...]
    cleanup_acknowledgement: _CleanupAcknowledgement | None
    request_bindings: Mapping[str, RequestValueGenerator] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _OraclePredicate:
    purpose: str
    oracle_digest: str
    pointer: str
    match_values: tuple[object, ...]
    modality: str
    turn: str
    language: str


@dataclass(frozen=True, slots=True)
class _TerminalResponseContract:
    pointer: str
    match_values: tuple[object, ...]
    effect_applied: bool
    replay_safe: bool
    status_codes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _CleanupAcknowledgement:
    pointer: str
    match_values: tuple[object, ...]
    status_codes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _FreshSetupResponsePredicate:
    method: str
    url: str
    status_codes: tuple[int, ...]
    pointer: str
    match_values: tuple[object, ...]
    reviewed_remote_session_id: str


@dataclass(frozen=True, slots=True)
class _SessionExpiredSignal:
    """The reviewed adapter's documented no-active-session response signal."""

    status_codes: tuple[int, ...]
    pointer: str
    match_values: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class _ApiAdapter:
    target_id: str
    endpoint: str
    credential_handle: str | None
    browser_context_transport: bool
    steps: tuple[_ApiStep, ...]
    digest: str
    compiled_adapter: str
    compiled_digest: str
    evaluation_uncertainties: tuple[Mapping[str, object], ...]
    target_delivery: str | None
    cleanup_lifecycle: str
    fresh_setup_response: _FreshSetupResponsePredicate | None
    session_expired_signal: _SessionExpiredSignal | None = None


class ReviewedApiTargetTool:
    """Execute one reviewed stateful HTTP adapter through a durable campaign session."""

    effect = "target_network"

    def __init__(
        self,
        sessions: CampaignApiSessionRepository,
        adapter_resolver: ReviewedAdapterResolver,
        credentials: Mapping[str, str],
        *,
        browser_api_requester: BrowserApiRequester | None = None,
        timeout_seconds: float = 30,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("API target timeout must be positive")
        self._sessions = sessions
        self._adapter_resolver = adapter_resolver
        self._credentials = credentials
        self._browser_api_requester = browser_api_requester
        self._timeout = timeout_seconds
        self._client = client
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="target_request",
            description=(
                "Send one input through the campaign's exact reviewed stateful HTTP API adapter. "
                "Merlin owns session start, captured values, serialization, and final cleanup."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "input": {"type": "string", "minLength": 1},
                    "strategy_plan": {
                        "type": "object",
                        "properties": {
                            "observation": {"type": "string", "minLength": 1},
                            "thought": {"type": "string", "minLength": 1},
                            "strategy": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                                "minItems": 1,
                                "uniqueItems": True,
                            },
                            "response": {"type": "string", "minLength": 1},
                        },
                        "required": ["observation", "thought", "strategy", "response"],
                        "additionalProperties": False,
                    },
                    "strategy_turn_index": {"type": "integer", "minimum": 0},
                },
                "required": ["input"],
                "additionalProperties": False,
            },
            interaction_role=ToolInteractionRole.TARGET_EXCHANGE,
            target_delivery_from_reviewed_adapter=True,
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        text = arguments.get("input")
        if not isinstance(text, str) or not text.strip():
            return ToolResult(content="input must be a non-empty string", is_error=True)
        if context.campaign_id is None or context.target_id is None:
            return ToolResult(
                content="target_request requires campaign target context", is_error=True
            )
        if context.agent_id is None:
            return ToolResult(
                content="target_request requires a stable assignment scope", is_error=True
            )
        session_scope_id = _reviewed_session_scope_id(context.agent_id)
        try:
            adapter = self._adapter(context.campaign_id, context.target_id)
        except ValueError as error:
            return ToolResult(content=str(error), is_error=True)

        def resolved(result: ToolResult) -> ToolResult:
            identified = _with_adapter_identity(result, adapter)
            if context.compiled_adapter is None and context.compiled_adapter_digest is None:
                metadata = dict(identified.metadata)
                metadata["legacy_compiled_adapter_context"] = True
                return identified.model_copy(update={"metadata": metadata})
            return identified

        identity_failure = _context_adapter_identity_failure(context, adapter)
        if identity_failure is not None:
            return resolved(identity_failure)

        legacy_failure = await self._reconcile_legacy_sessions(
            context.campaign_id,
            context.target_id,
            adapter,
        )
        if legacy_failure is not None:
            return resolved(legacy_failure)
        lock = self._locks.setdefault((context.campaign_id, session_scope_id), asyncio.Lock())
        async with lock:
            effect_digest = _terminal_effect_digest(adapter, text)
            state = await asyncio.to_thread(
                self._sessions.load,
                context.campaign_id,
                target_id=context.target_id,
                session_scope_id=session_scope_id,
            )
            disposition = _replay_disposition(
                state.variables if state is not None else {}, effect_digest
            )
            if disposition in {"no_replay", "rotation_exhausted"}:
                return resolved(_replay_refused_result(state, effect_digest, disposition))
            pointer_miss_retries = 0
            pointer_miss_exchanges: list[dict[str, object]] = []
            pointer_miss_target_call_budget_charge = 0
            pointer_miss_limit_interaction_count = 0
            terminal_attempt_exchanges: list[dict[str, object]] = []
            reviewed_safe_rotation_used = disposition == "safe_rotation_used"
            while True:
                try:
                    recovered_rotation = await self._recover_terminal_rotation_locked(
                        context.campaign_id,
                        context.target_id,
                        session_scope_id,
                        effect_digest,
                    )
                except (_TargetCleanupRejected, _TargetCleanupUnverified) as error:
                    return resolved(_cleanup_unresolved_result(error))
                reviewed_safe_rotation_used = recovered_rotation or reviewed_safe_rotation_used
                try:
                    return resolved(
                        await self._invoke_locked(
                            context,
                            text,
                            pointer_miss_retries,
                            session_scope_id,
                            adapter,
                        )
                    )
                except _TargetSessionTerminal as error:
                    terminal_attempt_exchanges.extend(error.exchanges)
                    error.exchanges[:] = terminal_attempt_exchanges
                    # Persist the terminal exchange and recovery marker in the
                    # same transaction that resolves the pending effect. Cleanup
                    # and rotation happen only after that durable boundary.
                    disposition = (
                        "no_replay"
                        if not error.replay_safe
                        else (
                            "rotation_exhausted"
                            if reviewed_safe_rotation_used
                            else "safe_rotation_used"
                        )
                    )
                    _set_replay_disposition(
                        error.variables,
                        effect_digest=error.effect_digest,
                        input_digest=error.input_digest,
                        disposition=disposition,
                    )
                    await asyncio.to_thread(
                        self._sessions.finish_step,
                        context.campaign_id,
                        error.variables,
                        target_id=context.target_id,
                        session_scope_id=session_scope_id,
                    )
                    try:
                        await self._cleanup_locked(
                            context.campaign_id,
                            context.target_id,
                            session_scope_id,
                        )
                    except (
                        ValueError,
                        httpx.HTTPError,
                        BrowserApiTransportError,
                        _TargetTransportFailure,
                        _TargetCleanupRejected,
                        _TargetCleanupUnverified,
                    ) as cleanup_error:
                        return resolved(
                            await self._terminal_result(
                                context.campaign_id,
                                error,
                                detail=f"reviewed cleanup failed: {cleanup_error}",
                                rotations=1 if reviewed_safe_rotation_used else 0,
                                target_id=context.target_id,
                                session_scope_id=session_scope_id,
                            )
                        )
                    if not error.replay_safe or reviewed_safe_rotation_used:
                        return resolved(
                            await self._terminal_result(
                                context.campaign_id,
                                error,
                                detail=(
                                    "reviewed contract does not authorize automatic replay"
                                    if not error.replay_safe
                                    else "one reviewed-safe rotation made no progress"
                                ),
                                rotations=1 if reviewed_safe_rotation_used else 0,
                                target_id=context.target_id,
                                session_scope_id=session_scope_id,
                            )
                        )
                    reviewed_safe_rotation_used = True
                except _ResponsePointerMissing as error:
                    # The interaction response selector is advisory and is handled
                    # without raising. Reaching this branch means a required capture
                    # is missing. Rotate one stateful interaction session once; a
                    # setup/cleanup capture miss cannot be recovered because later
                    # requests cannot be rendered from the incomplete lifecycle.
                    pointer_miss_retries += 1
                    pointer_miss_exchanges.extend(error.exchanges)
                    pointer_miss_target_call_budget_charge += error.target_call_budget_charge
                    pointer_miss_limit_interaction_count += error.limit_interaction_count
                    if not error.interaction or pointer_miss_retries > 1:
                        failure_category = _infrastructure_failure_category(error)
                        metadata: dict[str, object] = (
                            {
                                "infrastructure_failure_category": failure_category.value,
                                "response_pointer": error.pointer,
                                "response_digest": error.body_digest,
                                "target_exchanges": pointer_miss_exchanges,
                                "current_target_exchanges": pointer_miss_exchanges,
                                "target_call_budget_charge": (
                                    pointer_miss_target_call_budget_charge
                                ),
                                "limit_interaction_count": (pointer_miss_limit_interaction_count),
                            }
                            if failure_category is not None
                            else {}
                        )
                        if error.interaction and pointer_miss_retries > 1:
                            # The retry already rotated the durable API session.
                            # Report that intermediate identity even though its
                            # delivery failed, so a replacement worker observes
                            # the complete monotonic generation chain instead of
                            # mistaking a legitimate 2 -> 3 -> 4 recovery for a
                            # conflicting 2 -> 4 identity jump.
                            state = await asyncio.to_thread(
                                self._sessions.load,
                                context.campaign_id,
                                target_id=context.target_id,
                                session_scope_id=session_scope_id,
                            )
                            if state is not None:
                                metadata["target_session_id"] = state.target_session_id
                                metadata["target_session_generation"] = state.generation
                        return resolved(
                            ToolResult(
                                content=str(error),
                                is_error=True,
                                metadata=metadata,
                            )
                        )
                except (
                    ValueError,
                    httpx.HTTPError,
                    BrowserApiTransportError,
                    _TargetTransportFailure,
                    _TargetCleanupRejected,
                    _TargetCleanupUnverified,
                ) as error:
                    if isinstance(error, (_TargetCleanupRejected, _TargetCleanupUnverified)):
                        return resolved(_cleanup_unresolved_result(error))
                    failure_category = _infrastructure_failure_category(error)
                    transport_metadata = (
                        {
                            "target_call_budget_charge": error.target_call_budget_charge,
                            "limit_interaction_count": error.limit_interaction_count,
                            "target_exchanges": error.exchanges,
                            "current_target_exchanges": error.exchanges,
                        }
                        if isinstance(error, _TargetTransportFailure)
                        else {}
                    )
                    return resolved(
                        ToolResult(
                            content=str(error),
                            is_error=True,
                            metadata={
                                **transport_metadata,
                                **(
                                    {"infrastructure_failure_category": failure_category.value}
                                    if failure_category is not None
                                    else {}
                                ),
                            },
                        )
                    )

    async def _recover_terminal_rotation_locked(
        self,
        campaign_id: str,
        target_id: str,
        session_scope_id: str,
        effect_digest: str,
    ) -> bool:
        """Finish a persisted terminal-session rotation after interruption."""
        state = await asyncio.to_thread(
            self._sessions.load,
            campaign_id,
            target_id=target_id,
            session_scope_id=session_scope_id,
        )
        if state is None:
            return False
        marker = state.variables.get("__terminal_rotation_required")
        if not isinstance(marker, Mapping):
            return False
        if state.state != "cleaned":
            await self._cleanup_locked(campaign_id, target_id, session_scope_id)
        return marker.get("effect_digest") == effect_digest and marker.get("replay_safe") is True

    async def _reconcile_legacy_sessions(
        self,
        campaign_id: str,
        target_id: str,
        adapter: _ApiAdapter,
    ) -> ToolResult | None:
        """Clean a migrated campaign-wide session before creating scoped work."""
        lock = self._locks.setdefault((campaign_id, LEGACY_API_SESSION_SCOPE_ID), asyncio.Lock())
        async with lock:
            states = await asyncio.to_thread(
                self._sessions.list_for_campaign,
                campaign_id,
                include_cleaned=False,
            )
            legacy = [
                state for state in states if state.session_scope_id == LEGACY_API_SESSION_SCOPE_ID
            ]
            for state in legacy:
                if state.target_id != target_id or not _state_matches_adapter(state, adapter):
                    await asyncio.to_thread(
                        self._sessions.quarantine,
                        campaign_id,
                        target_id=state.target_id,
                        session_scope_id=LEGACY_API_SESSION_SCOPE_ID,
                    )
                    return _legacy_session_reconciliation_unavailable_result(
                        state,
                        "the migrated session does not match the current reviewed adapter",
                    )
                remote_session_id = state.variables.get("session_id")
                if (
                    isinstance(remote_session_id, str)
                    and remote_session_id
                    and state.remote_session_id != remote_session_id
                ):
                    try:
                        state = await asyncio.to_thread(
                            self._sessions.claim_remote_session,
                            campaign_id,
                            remote_session_id,
                            target_id=state.target_id,
                            session_scope_id=LEGACY_API_SESSION_SCOPE_ID,
                        )
                    except RemoteSessionClaimConflict:
                        await asyncio.to_thread(
                            self._sessions.quarantine,
                            campaign_id,
                            target_id=state.target_id,
                            session_scope_id=LEGACY_API_SESSION_SCOPE_ID,
                        )
                        return _legacy_session_reconciliation_unavailable_result(
                            state,
                            "the migrated physical session is owned by another active scope",
                        )
                try:
                    await self._cleanup_locked(
                        campaign_id,
                        state.target_id,
                        LEGACY_API_SESSION_SCOPE_ID,
                    )
                except (
                    ValueError,
                    httpx.HTTPError,
                    BrowserApiTransportError,
                    _TargetTransportFailure,
                    _TargetCleanupRejected,
                    _TargetCleanupUnverified,
                ) as error:
                    await asyncio.to_thread(
                        self._sessions.quarantine,
                        campaign_id,
                        target_id=state.target_id,
                        session_scope_id=LEGACY_API_SESSION_SCOPE_ID,
                    )
                    return _legacy_session_reconciliation_unavailable_result(
                        state,
                        f"the migrated session could not be safely cleaned: {error}",
                    )
        return None

    async def _terminal_result(
        self,
        campaign_id: str,
        error: _TargetSessionTerminal,
        *,
        detail: str,
        rotations: int,
        target_id: str,
        session_scope_id: str,
    ) -> ToolResult:
        state = await asyncio.to_thread(
            self._sessions.load,
            campaign_id,
            target_id=target_id,
            session_scope_id=session_scope_id,
        )
        exchanges = _terminal_exchanges(state.variables if state is not None else error.variables)
        current_exchanges = [dict(item) for item in error.exchanges]
        disposition = _replay_disposition(
            state.variables if state is not None else error.variables,
            error.effect_digest,
        )
        exact_replay_retryable = disposition == "safe_rotation_used"
        return ToolResult(
            content=f"{error}; {detail}",
            is_error=True,
            metadata={
                "infrastructure_failure_category": (
                    InfrastructureFailureCategory.UNHEALTHY_TARGET_SESSION.value
                ),
                "retryable": exact_replay_retryable,
                "exact_replay_retryable": exact_replay_retryable,
                "replay_disposition": disposition,
                "effect_digest": error.effect_digest,
                "input_digest": error.input_digest,
                "target_session_exhausted": True,
                "response_pointer": error.response_pointer,
                "response_digest": error.response_digest,
                "target_exchanges": exchanges,
                "current_target_exchanges": current_exchanges,
                "limit_interaction_count": sum(
                    1
                    for item in current_exchanges
                    if item.get("interaction") is True and item.get("cleanup") is not True
                ),
                "target_call_budget_charge": sum(
                    int(item.get("target_call_budget_charge", 1))
                    for item in current_exchanges
                    if item.get("cleanup") is not True
                ),
                "target_session_rotations": rotations,
                **(
                    {
                        "target_session_id": state.target_session_id,
                        "target_session_generation": state.generation,
                    }
                    if state is not None
                    else {}
                ),
            },
        )

    async def _invoke_locked(
        self,
        context: ToolContext,
        text: str,
        pointer_miss_retries: int,
        session_scope_id: str,
        adapter: _ApiAdapter,
    ) -> ToolResult:
        assert context.campaign_id is not None and context.target_id is not None
        state = await asyncio.to_thread(
            self._sessions.create,
            context.campaign_id,
            adapter.target_id,
            adapter.compiled_digest,
            session_scope_id=session_scope_id,
        )
        if state.pending_step is not None or state.state == "ambiguous":
            await self._cleanup_locked(context.campaign_id, adapter.target_id, session_scope_id)
            state = await asyncio.to_thread(
                self._sessions.create,
                context.campaign_id,
                adapter.target_id,
                adapter.compiled_digest,
                session_scope_id=session_scope_id,
            )
        if state.state == "cleaned":
            raise _TargetSessionStateError("campaign API session is already cleaned")
        variables = dict(state.variables)
        isolation_failure = await self._claim_remote_session(
            state,
            variables,
            exchanges=[],
        )
        if isolation_failure is not None:
            return isolation_failure
        durable_terminal_exchanges = _terminal_exchanges(variables)
        completed = set(_string_list(variables.get("__completed_steps", ())))
        session_reused = bool(completed)
        exchanges: list[dict[str, object]] = []
        for step in adapter.steps:
            if step.cleanup or step.interaction or step.id in completed:
                continue
            _, variables, exchange = await self._execute(
                context.campaign_id,
                adapter,
                step,
                variables,
                session_scope_id=session_scope_id,
                input_text=text,
            )
            exchanges.append(exchange)
            if not 200 <= int(exchange["status_code"]) <= 299:
                state = await asyncio.to_thread(
                    self._sessions.finish_step,
                    context.campaign_id,
                    variables,
                    target_id=adapter.target_id,
                    session_scope_id=session_scope_id,
                )
                return _target_delivery_rejected_result(
                    state,
                    adapter,
                    durable_terminal_exchanges + exchanges,
                    exchanges,
                )
            completed.add(step.id)
            variables["__completed_steps"] = sorted(completed)
            setup_isolated = _fresh_setup_response_matches(adapter, step, exchange)
            if not setup_isolated:
                remote_session_id = variables.get("session_id")
                predicate = adapter.fresh_setup_response
                assert predicate is not None
                variables["__fresh_setup_isolation"] = {
                    "status": "predicate_mismatch",
                    "cleanup_owned": (
                        isinstance(remote_session_id, str)
                        and remote_session_id == predicate.reviewed_remote_session_id
                    ),
                }
            state = await asyncio.to_thread(
                self._sessions.finish_step,
                context.campaign_id,
                variables,
                target_id=adapter.target_id,
                session_scope_id=session_scope_id,
            )
            variables = dict(state.variables)
            isolation_failure = await self._claim_remote_session(
                state,
                variables,
                exchanges=exchanges,
            )
            if isolation_failure is not None:
                return isolation_failure
            if not setup_isolated:
                # Advisory only: a resumed/unproven physical session never blocks
                # the attack. The __fresh_setup_isolation marker above keeps
                # cleanup conservative (finish_without_cleanup with a recorded
                # limitation); the exchange proceeds with the uncertainty on the
                # record.
                exchange["evaluation_uncertain"] = True
                exchange.setdefault("evaluation_uncertainties", []).append(
                    _evaluation_uncertainty(
                        "api_target.fresh_setup_isolation",
                        (
                            "the designated setup response did not prove fresh "
                            "physical target state; execution proceeds on the "
                            "resumed session and destructive cleanup stays skipped"
                        ),
                    )
                )
        observed: list[object] = []
        session_terminal = False
        session_restart_used = False
        for step in adapter.steps:
            if not step.interaction:
                continue
            repeat_index = 0
            while repeat_index < step.repeat:
                try:
                    item, variables, exchange = await self._execute(
                        context.campaign_id,
                        adapter,
                        step,
                        variables,
                        session_scope_id=session_scope_id,
                        input_text=text,
                    )
                except _TargetSessionTerminal as error:
                    error.exchanges[:0] = exchanges
                    raise
                except _TargetTransportFailure as error:
                    error.include_completed_exchanges(exchanges)
                    raise
                except _ResponsePointerMissing as error:
                    error.include_completed_exchanges(exchanges)
                    raise
                state = await asyncio.to_thread(
                    self._sessions.finish_step,
                    context.campaign_id,
                    variables,
                    target_id=adapter.target_id,
                    session_scope_id=session_scope_id,
                )
                variables = dict(state.variables)
                exchanges.append(exchange)
                if exchange.get("target_session_terminal") is True:
                    observed.append(item)
                    session_terminal = True
                    break
                if not 200 <= int(exchange["status_code"]) <= 299:
                    if (
                        not session_restart_used
                        and adapter.session_expired_signal is not None
                        and any(
                            not candidate.cleanup and not candidate.interaction
                            for candidate in adapter.steps
                        )
                        and _session_expired_signal_matches(
                            adapter.session_expired_signal, exchange
                        )
                    ):
                        # The target's documented no-active-session signal: run the
                        # reviewed start action once, then retry this exchange once.
                        session_restart_used = True
                        (
                            variables,
                            restart_exchanges,
                            restart_failure,
                        ) = await self._restart_session_locked(
                            context.campaign_id,
                            adapter,
                            variables,
                            session_scope_id=session_scope_id,
                            exchanges=exchanges,
                        )
                        exchanges.extend(restart_exchanges)
                        if restart_failure is not None:
                            return restart_failure
                        continue
                    observed.append(item)
                    return _target_delivery_rejected_result(
                        state,
                        adapter,
                        durable_terminal_exchanges + exchanges,
                        exchanges,
                    )
                observed.append(item)
                repeat_index += 1
            if session_terminal:
                break
        if not observed:
            raise ValueError("reviewed API adapter has no interaction step")
        payload: object = observed[0] if len(observed) == 1 else observed
        remote_target_session_id = variables.get("session_id")
        metadata: dict[str, object] = {
            "target_profile_id": adapter.target_id,
            "adapter_digest": adapter.digest,
            "campaign_id": context.campaign_id,
            "target_session_id": state.target_session_id,
            "target_session_generation": state.generation,
            "remote_target_session_id": (
                remote_target_session_id
                if isinstance(remote_target_session_id, str) and remote_target_session_id
                else None
            ),
            "session_reused": session_reused,
            "durable_setup_reused": session_reused,
            "remote_session_reused": (
                session_reused if adapter.fresh_setup_response is not None else None
            ),
            "session_freshness": (
                "authoritative_setup_predicate_mismatch"
                if adapter.fresh_setup_response is not None
                and isinstance(variables.get("__fresh_setup_isolation"), Mapping)
                and variables["__fresh_setup_isolation"].get("status") == "predicate_mismatch"
                else "durable_assignment_reuse"
                if adapter.fresh_setup_response is not None and session_reused
                else "authoritative_fresh_setup_response"
                if adapter.fresh_setup_response is not None
                else "not_observed"
            ),
            "target_exchanges": durable_terminal_exchanges + exchanges,
            # Keep archived lifecycle exchanges for audit, but identify the
            # exchanges attributable to this invocation so consumers never
            # mistake an earlier rotated session for the current effect.
            "current_target_exchanges": exchanges,
            **(
                {
                    "target_session_recovered": True,
                    "target_session_rotations": len(durable_terminal_exchanges),
                }
                if durable_terminal_exchanges
                else {}
            ),
            **(
                {"reviewed_target_delivery": adapter.target_delivery}
                if adapter.target_delivery is not None
                else {}
            ),
        }
        evaluation_uncertainties = list(adapter.evaluation_uncertainties)
        for exchange in exchanges:
            evaluation_uncertainties.extend(
                _uncertainty_records(exchange.get("evaluation_uncertainties", ()))
            )
        if evaluation_uncertainties:
            metadata["evaluation_uncertain"] = True
            metadata["evaluation_uncertainties"] = _dedupe_uncertainties(evaluation_uncertainties)
        if pointer_miss_retries:
            metadata["pointer_miss_session_rotations"] = pointer_miss_retries
        if session_restart_used:
            metadata["target_session_restarted"] = True
        if session_terminal:
            metadata["target_session_terminal"] = True
            metadata["exact_replay_retryable"] = False
        metadata["target_call_budget_charge"] = sum(
            int(exchange.get("target_call_budget_charge", 1))
            for exchange in exchanges
            if exchange.get("cleanup") is not True
        )
        metadata["limit_interaction_count"] = sum(
            1
            for exchange in exchanges
            if exchange.get("interaction") is True and exchange.get("cleanup") is not True
        )
        return ToolResult(content=json.dumps(payload, sort_keys=True), metadata=metadata)

    async def _restart_session_locked(
        self,
        campaign_id: str,
        adapter: _ApiAdapter,
        variables: dict[str, object],
        *,
        session_scope_id: str,
        exchanges: list[dict[str, object]],
    ) -> tuple[dict[str, object], list[dict[str, object]], ToolResult | None]:
        """Run the reviewed start action once after the no-active-session signal.

        The restart exchanges are infrastructure receipts, not attack telemetry:
        they re-establish the target session so the original exchange can be
        retried exactly once by the caller.
        """
        variables = dict(variables)
        # The expired physical session is dead server-side; drop its durable
        # identity so the reviewed setup steps re-capture a fresh one.
        variables.pop("session_id", None)
        variables.pop("__fresh_setup_isolation", None)
        completed: set[str] = set()
        variables["__completed_steps"] = []
        restart_exchanges: list[dict[str, object]] = []
        # The expired physical session is dead server-side (that is the reviewed
        # signal that admitted this fallback), so its claim has nothing left to
        # protect; release it before the fresh session is claimed.
        state = await asyncio.to_thread(
            self._sessions.load,
            campaign_id,
            target_id=adapter.target_id,
            session_scope_id=session_scope_id,
        )
        if state is not None and state.remote_session_id is not None:
            await asyncio.to_thread(
                self._sessions.release_remote_session,
                campaign_id,
                target_id=adapter.target_id,
                session_scope_id=session_scope_id,
            )
        for step in adapter.steps:
            if step.cleanup or step.interaction:
                continue
            _, variables, exchange = await self._execute(
                campaign_id,
                adapter,
                step,
                variables,
                session_scope_id=session_scope_id,
                input_text="",
            )
            exchange["infrastructure_receipt"] = "session_restart"
            restart_exchanges.append(exchange)
            if not 200 <= int(exchange["status_code"]) <= 299:
                state = await asyncio.to_thread(
                    self._sessions.finish_step,
                    campaign_id,
                    variables,
                    target_id=adapter.target_id,
                    session_scope_id=session_scope_id,
                )
                variables = dict(state.variables)
                return (
                    variables,
                    restart_exchanges,
                    _target_delivery_rejected_result(
                        state,
                        adapter,
                        exchanges + restart_exchanges,
                        exchanges + restart_exchanges,
                    ),
                )
            completed.add(step.id)
            variables["__completed_steps"] = sorted(completed)
            state = await asyncio.to_thread(
                self._sessions.finish_step,
                campaign_id,
                variables,
                target_id=adapter.target_id,
                session_scope_id=session_scope_id,
            )
            variables = dict(state.variables)
            isolation_failure = await self._claim_remote_session(
                state,
                variables,
                exchanges=[*exchanges, *restart_exchanges],
            )
            if isolation_failure is not None:
                return variables, restart_exchanges, isolation_failure
        return variables, restart_exchanges, None

    async def _claim_remote_session(
        self,
        state: CampaignApiSession,
        variables: Mapping[str, object],
        *,
        exchanges: list[dict[str, object]],
    ) -> ToolResult | None:
        remote_session_id = variables.get("session_id")
        if not isinstance(remote_session_id, str) or not remote_session_id:
            return None
        if state.remote_session_id == remote_session_id:
            return None
        try:
            await asyncio.to_thread(
                self._sessions.claim_remote_session,
                state.campaign_id,
                remote_session_id,
                target_id=state.target_id,
                session_scope_id=state.session_scope_id,
            )
        except RemoteSessionClaimConflict as error:
            adopted = await self._adopt_terminal_claim(state, remote_session_id)
            if adopted:
                if exchanges:
                    exchanges[-1]["evaluation_uncertain"] = True
                    exchanges[-1].setdefault("evaluation_uncertainties", []).append(
                        _evaluation_uncertainty(
                            "api_target.shared_physical_session_adoption",
                            (
                                "the physical target session is shared: a retired "
                                "scope's quarantined claim was adopted so delivery "
                                "proceeds; session isolation is unachievable on this "
                                "target and the uncertainty is journaled"
                            ),
                        )
                    )
                return None
            await asyncio.to_thread(
                self._sessions.finish_without_cleanup,
                state.campaign_id,
                variables,
                target_id=state.target_id,
                session_scope_id=state.session_scope_id,
                disposition="cleanup_not_owned",
            )
            return _session_isolation_unavailable_result(state, error, list(exchanges))
        return None

    async def _adopt_terminal_claim(
        self,
        state: CampaignApiSession,
        remote_session_id: str,
    ) -> bool:
        """Adopt the physical session when only terminal quarantined claims hold it.

        A still-active owner scope keeps the strict isolation failure; stores
        without adoption support keep the legacy cleanup_not_owned finish.
        """
        adopter = getattr(self._sessions, "adopt_remote_session", None)
        if not callable(adopter):
            return False
        try:
            await asyncio.to_thread(
                adopter,
                state.campaign_id,
                remote_session_id,
                target_id=state.target_id,
                session_scope_id=state.session_scope_id,
            )
        except (RemoteSessionClaimConflict, ValueError, KeyError):
            return False
        return True

    async def cleanup_assignment(self, assignment_id: str) -> None:
        campaign_id = _campaign_id(assignment_id)
        if campaign_id is None:
            return
        legacy_lock = self._locks.setdefault(
            (campaign_id, LEGACY_API_SESSION_SCOPE_ID), asyncio.Lock()
        )
        assignment_scope_id = _reviewed_session_scope_id(assignment_id)
        assignment_lock = self._locks.setdefault((campaign_id, assignment_scope_id), asyncio.Lock())

        async def cleanup() -> None:
            states = await asyncio.to_thread(
                self._sessions.list_for_campaign,
                campaign_id,
                include_cleaned=False,
            )
            for state in states:
                if state.session_scope_id in {
                    LEGACY_API_SESSION_SCOPE_ID,
                    assignment_id,
                    assignment_scope_id,
                }:
                    await self._cleanup_locked(
                        campaign_id,
                        state.target_id,
                        state.session_scope_id,
                    )

        async with legacy_lock:
            if assignment_lock is legacy_lock:
                await cleanup()
            else:
                async with assignment_lock:
                    await cleanup()

    async def _cleanup_locked(
        self, campaign_id: str, target_id: str, session_scope_id: str
    ) -> None:
        """Resolve one owned API session while the campaign lock is held."""
        state = await asyncio.to_thread(
            self._sessions.load,
            campaign_id,
            target_id=target_id,
            session_scope_id=session_scope_id,
        )
        if state is None or state.state == "cleaned":
            return
        adapter = self._adapter(campaign_id, state.target_id)
        if not _state_matches_adapter(state, adapter):
            await asyncio.to_thread(
                self._sessions.quarantine,
                campaign_id,
                target_id=target_id,
                session_scope_id=session_scope_id,
            )
            raise _TargetCleanupUnverified(
                "the owned target session was created with a different reviewed adapter"
            )
        freshness = state.variables.get("__fresh_setup_isolation")
        if (
            isinstance(freshness, Mapping)
            and freshness.get("status") == "predicate_mismatch"
            and freshness.get("cleanup_owned") is not True
        ):
            await asyncio.to_thread(
                self._sessions.finish_without_cleanup,
                campaign_id,
                target_id=target_id,
                session_scope_id=session_scope_id,
                disposition="cleanup_requirement_unestablished",
            )
            raise _TargetCleanupUnverified(
                "fresh-session setup returned an unowned physical target session"
            )
        step = adapter.steps[-1]
        if not step.cleanup:
            if adapter.cleanup_lifecycle != "not_required":
                # Unknown cleanup metadata is not proof that an owned remote
                # resource requires a destructive cleanup action, and a missing
                # cleanup capability can never be repaired by parking the
                # campaign. Retire this scope without a target call, keep its
                # exact physical-session claim quarantined against silent
                # reuse, and let the completion projection record the
                # evaluation-uncertain limitation. A reviewed cleanup step
                # below remains mandatory.
                await asyncio.to_thread(
                    self._sessions.finish_without_cleanup,
                    campaign_id,
                    target_id=target_id,
                    session_scope_id=session_scope_id,
                    disposition="cleanup_requirement_unestablished",
                )
                return
            await asyncio.to_thread(
                self._sessions.finish_without_cleanup,
                campaign_id,
                target_id=target_id,
                session_scope_id=session_scope_id,
                disposition="no_cleanup_obligation",
            )
            return
        variables = dict(state.variables)
        request = bind_request_values(_render(step.request, variables, ""), step.request_bindings)
        await asyncio.to_thread(
            self._sessions.begin_cleanup,
            campaign_id,
            _pending_step(step, request),
            target_id=target_id,
            session_scope_id=session_scope_id,
        )
        try:
            started = time.monotonic()
            observed, status_code, response_body, _attempt_count = await self._request(
                campaign_id, adapter, step, variables, input_text="", prepared_request=request
            )
        except (
            httpx.HTTPError,
            ValueError,
            BrowserApiTransportError,
            _TargetTransportFailure,
        ):
            await asyncio.to_thread(
                self._sessions.mark_ambiguous,
                campaign_id,
                target_id=target_id,
                session_scope_id=session_scope_id,
            )
            raise
        cleanup_exchange = {
            "id": step.id,
            "interaction": step.interaction,
            "cleanup": step.cleanup,
            "request": request,
            "response": observed,
            "response_body": response_body,
            "status_code": status_code,
            "outcome": (
                "acknowledged" if 200 <= status_code <= 299 else "cleanup_delivery_rejected"
            ),
            "latency_ms": max(0, int((time.monotonic() - started) * 1_000)),
        }
        variables["__cleanup_exchange"] = cleanup_exchange
        if not 200 <= status_code <= 299:
            await asyncio.to_thread(
                self._sessions.fail_cleanup,
                campaign_id,
                variables,
                target_id=target_id,
                session_scope_id=session_scope_id,
            )
            raise _TargetCleanupRejected(status_code)
        acknowledgement = step.cleanup_acknowledgement
        acknowledgement_error: str | None = None
        if acknowledgement is None:
            acknowledgement_error = "the reviewed adapter has no executable cleanup acknowledgement"
        elif status_code not in acknowledgement.status_codes:
            acknowledgement_error = (
                f"HTTP {status_code} is not an allowed cleanup acknowledgement status"
            )
        else:
            try:
                acknowledged_value = _select(response_body, acknowledgement.pointer, step.id)
            except _ResponsePointerMissing:
                acknowledgement_error = f"reviewed selector {acknowledgement.pointer} was absent"
            else:
                if not any(
                    _typed_equal(acknowledged_value, expected)
                    for expected in acknowledgement.match_values
                ):
                    acknowledgement_error = (
                        "the reviewed cleanup acknowledgement value did not match"
                    )
        if acknowledgement_error is not None:
            cleanup_exchange["evaluation_uncertain"] = True
            cleanup_exchange["evaluation_uncertainties"] = [
                _evaluation_uncertainty(
                    f"api_steps.{step.id}.cleanup_acknowledgement",
                    f"Cleanup returned HTTP {status_code}, but {acknowledgement_error}. "
                    "The compiled cleanup action still completed; this advisory metadata did "
                    "not retain or rotate the target session.",
                )
            ]
        await asyncio.to_thread(
            self._sessions.finish_cleanup,
            campaign_id,
            variables,
            target_id=target_id,
            session_scope_id=session_scope_id,
        )

    async def _execute(
        self,
        campaign_id: str,
        adapter: _ApiAdapter,
        step: _ApiStep,
        variables: dict[str, object],
        *,
        session_scope_id: str,
        input_text: str,
    ) -> tuple[object, dict[str, object], dict[str, object]]:
        request = bind_request_values(
            _render(step.request, variables, input_text), step.request_bindings
        )
        await asyncio.to_thread(
            self._sessions.begin_step,
            campaign_id,
            _pending_step(step, request),
            target_id=adapter.target_id,
            session_scope_id=session_scope_id,
        )
        started = time.monotonic()
        try:
            observed, status_code, response_json, attempt_count = await self._request(
                campaign_id,
                adapter,
                step,
                variables,
                input_text=input_text,
                prepared_request=request,
            )
        except (
            httpx.HTTPError,
            ValueError,
            BrowserApiTransportError,
            _TargetTransportFailure,
        ):
            await asyncio.to_thread(
                self._sessions.mark_ambiguous,
                campaign_id,
                target_id=adapter.target_id,
                session_scope_id=session_scope_id,
            )
            raise
        latency_ms = max(0, int((time.monotonic() - started) * 1_000))
        evaluation_uncertainties = list(step.evaluation_uncertainties)
        if step.expected_status is not None and status_code != step.expected_status:
            evaluation_uncertainties.append(
                _evaluation_uncertainty(
                    f"api_steps.{step.id}.expected_status",
                    f"Target returned HTTP {status_code}; reviewed metadata expected "
                    f"{step.expected_status}. The response and captures were preserved.",
                )
            )
        terminal_response = _terminal_response_match(step, status_code, response_json)
        if terminal_response is not None:
            evaluation_uncertainties.append(
                _evaluation_uncertainty(
                    f"api_steps.{step.id}.terminal_response_contract",
                    "Reviewed terminal-response metadata matched. It was recorded for "
                    "validator judgment and did not alter delivery, replay, cleanup, or "
                    "session rotation semantics.",
                )
            )
        if not 200 <= status_code <= 299:
            evaluation_uncertainties.append(
                _evaluation_uncertainty(
                    f"api_steps.{step.id}.http_status",
                    f"Target returned unexpected HTTP {status_code}; the raw response was "
                    "preserved before applying reviewed selectors or captures.",
                )
            )
            return (
                observed,
                variables,
                {
                    "id": step.id,
                    "interaction": step.interaction,
                    "cleanup": step.cleanup,
                    "request": request,
                    "response": observed,
                    "response_body": response_json,
                    "status_code": status_code,
                    "expected_status": step.expected_status,
                    "outcome": "target_delivery_rejected",
                    "target_delivery_rejected": True,
                    "session_health": "unknown",
                    "retryable": True,
                    "infrastructure_failure_category": (
                        InfrastructureFailureCategory.DELIVERY_FAILURE.value
                    ),
                    "latency_ms": latency_ms,
                    "target_call_budget_charge": attempt_count,
                    "evaluation_uncertain": True,
                    "evaluation_uncertainties": _dedupe_uncertainties(evaluation_uncertainties),
                    **(
                        {
                            "terminal_response_observation": {
                                "matched": True,
                                "effect_applied": terminal_response.effect_applied,
                                "replay_safe": terminal_response.replay_safe,
                            }
                        }
                        if terminal_response is not None
                        else {}
                    ),
                },
            )
        try:
            _select(response_json, step.response_pointer, step.id)
        except _ResponsePointerMissing:
            if step.interaction:
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        f"api_steps.{step.id}.response_selector",
                        "The response did not contain reviewed selector "
                        f"{step.response_pointer}; the complete response was preserved.",
                    )
                )
        for name, pointer in step.captures.items():
            try:
                variables[name] = _select(response_json, pointer, step.id)
            except _ResponsePointerMissing as error:
                error.interaction = step.interaction
                uncertainty = _evaluation_uncertainty(
                    f"api_steps.{step.id}.required_capture",
                    f"The delivered response omitted required capture {pointer}; the full "
                    "response was preserved as retryable infrastructure telemetry.",
                )
                error.exchanges.append(
                    {
                        "id": step.id,
                        "interaction": step.interaction,
                        "cleanup": step.cleanup,
                        "request": request,
                        "response": observed,
                        "response_body": response_json,
                        "status_code": status_code,
                        "expected_status": step.expected_status,
                        "outcome": "missing_response_pointer",
                        "latency_ms": latency_ms,
                        "target_call_budget_charge": attempt_count,
                        "evaluation_uncertain": True,
                        "evaluation_uncertainties": _dedupe_uncertainties(
                            [*evaluation_uncertainties, uncertainty]
                        ),
                    }
                )
                error.target_call_budget_charge += attempt_count
                error.limit_interaction_count += 1 if step.interaction else 0
                raise
        oracle_observations = _oracle_observation(adapter, step, response_json)
        if any(item.get("matched") is None for item in oracle_observations):
            evaluation_uncertainties.append(
                _evaluation_uncertainty(
                    f"api_steps.{step.id}.oracle",
                    "The response did not match its reviewed oracle response pointer.",
                )
            )
        if step.interaction and _contains_error_signal(response_json):
            evaluation_uncertainties.append(
                _evaluation_uncertainty(
                    f"api_steps.{step.id}.response_body",
                    "The target returned error-shaped domain data without a matching reviewed "
                    "terminal lifecycle contract. The complete response was preserved for "
                    "validator judgment.",
                )
            )
        exchange = {
            "id": step.id,
            "interaction": step.interaction,
            "cleanup": step.cleanup,
            "request": request,
            "response": observed,
            "response_body": response_json,
            "status_code": status_code,
            "expected_status": step.expected_status,
            "outcome": ("evaluation_uncertain" if evaluation_uncertainties else "acknowledged"),
            "latency_ms": latency_ms,
            "target_call_budget_charge": attempt_count,
            **(
                {
                    "evaluation_uncertain": True,
                    "evaluation_uncertainties": _dedupe_uncertainties(evaluation_uncertainties),
                }
                if evaluation_uncertainties
                else {}
            ),
            **(
                {
                    "terminal_response_observation": {
                        "matched": True,
                        "effect_applied": terminal_response.effect_applied,
                        "replay_safe": terminal_response.replay_safe,
                    }
                }
                if terminal_response is not None
                else {}
            ),
            **(
                {
                    "oracle_observations": oracle_observations,
                    # Preserve the PAIR v1 singleton projection while the
                    # multi-predicate list is the authoritative adaptive lane.
                    "oracle_observation": next(
                        (
                            item
                            for item in oracle_observations
                            if item.get("purpose") == "objective"
                        ),
                        oracle_observations[0],
                    ),
                }
                if oracle_observations
                else {}
            ),
        }
        return observed, variables, exchange

    async def _request(
        self,
        campaign_id: str,
        adapter: _ApiAdapter,
        step: _ApiStep,
        variables: Mapping[str, object],
        *,
        input_text: str,
        prepared_request: object | None = None,
    ) -> tuple[object, int, object, int]:
        request = prepared_request
        if request is None:
            request = bind_request_values(
                _render(step.request, variables, input_text), step.request_bindings
            )
        headers: dict[str, str] = {}
        if adapter.credential_handle is not None:
            secret = self._credentials.get(adapter.credential_handle)
            if not secret:
                raise ValueError("reviewed API credential handle is unavailable")
            headers["Authorization"] = f"Bearer {secret}"
        if step.idempotency_key is not None:
            headers["Idempotency-Key"] = step.idempotency_key
        url = urljoin(adapter.endpoint.rstrip("/") + "/", step.endpoint)
        if adapter.browser_context_transport:
            if self._browser_api_requester is None:
                raise ValueError("reviewed browser-context API transport is unavailable")
            try:
                status_code, response_json = await self._browser_api_requester(
                    campaign_id, step.method, url, request, headers
                )
            except Exception as error:
                transport_error = BrowserApiTransportError(
                    f"browser-context API transport failed: {error}"
                )
                raise _TargetTransportFailure(
                    transport_error,
                    attempts=1,
                    interaction=step.interaction,
                ) from error
            try:
                observed = _select(response_json, step.response_pointer, step.id)
            except _ResponsePointerMissing as error:
                error.interaction = step.interaction
                observed = response_json
            return observed, status_code, response_json, 1
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response: httpx.Response | None = None
            attempt_count = 0
            for attempt in range(3):
                attempt_count += 1
                try:
                    response = await client.request(
                        step.method,
                        url,
                        json=request,
                        headers=headers,
                    )
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                    # The target never produced an HTTP response at all: one
                    # transient timeout on a live target must not fail the call.
                    # HTTP error statuses and application-level refusals are
                    # never retried here.
                    if attempt >= 2:
                        raise _TargetTransportFailure(
                            error,
                            attempts=attempt_count,
                            interaction=step.interaction,
                        ) from error
                    await asyncio.sleep(0.5 * (attempt + 1))
                except (httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError) as error:
                    # The request may have been accepted before the response was
                    # lost; replay only steps reviewed as safe to replay.
                    if attempt >= 2 or not step.replay_after_ambiguous_result:
                        raise _TargetTransportFailure(
                            error,
                            attempts=attempt_count,
                            interaction=step.interaction,
                        ) from error
                    await asyncio.sleep(0.5 * (attempt + 1))
            assert response is not None
            try:
                response_json: object = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                response_json = response.text
            try:
                observed = _select(response_json, step.response_pointer, step.id)
            except _ResponsePointerMissing as error:
                error.interaction = step.interaction
                observed = response_json
            return observed, response.status_code, response_json, attempt_count
        finally:
            if owns_client:
                await client.aclose()

    def _adapter(self, campaign_id: str, target_id: str) -> _ApiAdapter:
        raw = self._adapter_resolver(campaign_id)
        if raw is None:
            raise ValueError("campaign has no reviewed HTTP API adapter")
        try:
            compilation = compile_target_adapter(raw)
        except AdapterCompileError as error:
            raise ValueError(
                f"reviewed API adapter is not executable: {error.kind.value} at {error.path}"
            ) from error
        durable_json = raw.get("compiled_adapter")
        durable_digest = raw.get("compiled_adapter_digest")
        if bool(durable_json) != bool(durable_digest):
            raise ValueError("reviewed API adapter compiled state is incomplete")
        canonical = compilation
        if durable_json is not None:
            if not isinstance(durable_json, str) or not isinstance(durable_digest, str):
                raise ValueError("reviewed API adapter compiled state is invalid")
            try:
                durable = read_target_adapter(durable_json)
            except AdapterCompileError as error:
                raise ValueError("reviewed API adapter compiled state is malformed") from error
            observed_digest = compiled_target_adapter_digest(durable)
            if observed_digest != durable_digest:
                raise ValueError("reviewed API adapter compiled digest is invalid")
            if observed_digest != compiled_target_adapter_digest(compilation):
                raise ValueError("reviewed API adapter compiled and compatibility states disagree")
            canonical = durable
        executable = canonical.executable
        if not isinstance(executable, HttpApi):
            raise ValueError("campaign has no reviewed HTTP API adapter")
        raw_steps = raw.get("api_steps")
        if not isinstance(raw_steps, (list, tuple)):
            raise ValueError("reviewed API adapter steps are invalid")
        source_steps = _compiled_step_sources(raw_steps, compilation)
        if len(source_steps) != len(executable.steps):
            raise ValueError("reviewed API adapter compiled step projection is invalid")
        steps = tuple(
            _step_from_compiled(compiled, source, index)
            for index, (compiled, source) in enumerate(
                zip(executable.steps, source_steps, strict=True)
            )
        )
        if not steps:
            raise ValueError("reviewed API adapter lifecycle is unavailable")
        fresh_setup_response = _fresh_setup_response_predicate(raw, executable.endpoint, steps)
        return _ApiAdapter(
            target_id=target_id,
            endpoint=executable.endpoint,
            credential_handle=executable.credential_handle,
            browser_context_transport=executable.api_transport == "browser_context",
            steps=steps,
            digest=executable_adapter_digest(raw),
            compiled_adapter=_canonical_compilation_json(canonical),
            compiled_digest=compiled_target_adapter_digest(canonical),
            evaluation_uncertainties=_uncertainty_records(raw.get("evaluation_uncertainties", ())),
            target_delivery=(
                executable.target_delivery.value if executable.target_delivery is not None else None
            ),
            cleanup_lifecycle=executable.cleanup_lifecycle.value,
            fresh_setup_response=fresh_setup_response,
            session_expired_signal=_session_expired_signal(raw),
        )


def _compiled_step_sources(
    raw_steps: Sequence[object], compilation: AdapterCompilation
) -> tuple[Mapping[str, object], ...]:
    omitted: set[int] = set()
    for advisory in getattr(compilation, "advisories", ()):
        path = str(getattr(advisory, "path", ""))
        message = str(getattr(advisory, "message", ""))
        if not path.startswith("api_steps[") or "was omitted" not in message:
            continue
        index_text, separator, _ = path[len("api_steps[") :].partition("]")
        if separator and index_text.isdigit():
            omitted.add(int(index_text))
    return tuple(
        item
        for index, item in enumerate(raw_steps)
        if index not in omitted and isinstance(item, Mapping)
    )


def _fresh_setup_response_predicate(
    raw: Mapping[str, object],
    adapter_endpoint: str,
    steps: Sequence[_ApiStep],
) -> _FreshSetupResponsePredicate | None:
    supplied = raw.get("session_freshness_predicate")
    transition = raw.get("session_creation_transition")
    if isinstance(supplied, Mapping):
        condition = supplied
        reviewed_remote_session_id = supplied.get("reviewed_remote_session_id")
    elif (
        isinstance(transition, Mapping)
        and transition.get("source") == "authoritative_fresh_setup_response"
        and isinstance(transition.get("precondition"), Mapping)
    ):
        condition = transition["precondition"]
        reviewed_remote_session_id = transition.get("remote_session_id")
    else:
        return None
    if condition.get("source") not in {None, "authoritative_fresh_setup_response"}:
        return None
    method = condition.get("method")
    url = condition.get("url")
    statuses = condition.get("status_codes")
    pointer = condition.get("pointer")
    match_values = condition.get("match_values")
    if (
        not isinstance(method, str)
        or method.upper() not in {"POST", "PUT", "PATCH"}
        or not isinstance(url, str)
        or not url
        or not isinstance(statuses, (list, tuple))
        or not statuses
        or any(
            isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 299
            for status in statuses
        )
        or not isinstance(pointer, str)
        or (pointer and not pointer.startswith("/"))
        or not isinstance(match_values, (list, tuple))
        or not match_values
        or any(not _json_scalar(value) for value in match_values)
        or not isinstance(reviewed_remote_session_id, str)
        or not reviewed_remote_session_id
    ):
        raise ValueError("authoritative fresh-session response predicate is invalid")
    method = method.upper()
    designated = tuple(
        step
        for step in steps
        if not step.cleanup
        and not step.interaction
        and step.method == method
        and urljoin(adapter_endpoint.rstrip("/") + "/", step.endpoint) == url
    )
    if len(designated) != 1:
        raise ValueError(
            "authoritative fresh-session response predicate does not designate one setup step"
        )
    return _FreshSetupResponsePredicate(
        method=method,
        url=url,
        status_codes=tuple(statuses),
        pointer=pointer,
        match_values=tuple(match_values),
        reviewed_remote_session_id=reviewed_remote_session_id,
    )


def _step_from_compiled(
    compiled: HttpApiStep,
    source: Mapping[str, object],
    index: int,
) -> _ApiStep:
    role = compiled.role
    value = dict(source)
    value.update(
        {
            "step_id": str(source.get("step_id") or f"{role.value}-{index + 1}"),
            "endpoint": compiled.endpoint,
            "method": compiled.method,
            "request_template": compiled.request_template,
            "request_bindings": compiled.request_bindings,
            "response_selector": (
                source.get("response_selector")
                if isinstance(source.get("response_selector"), str)
                else ""
            ),
            "cleanup": role is AdapterStepRole.CLEANUP,
            "interaction": role is AdapterStepRole.INTERACTION,
            "captures": compiled.captures,
            "repeat": compiled.repeat,
            "idempotency_key": compiled.idempotency_key,
            "replay_after_ambiguous_result": compiled.replay_after_ambiguous_result,
        }
    )
    return _step(value)


def _step(value: Mapping[str, object]) -> _ApiStep:
    cleanup = value["cleanup"] is True
    idempotency_key = None if value["idempotency_key"] is None else str(value["idempotency_key"])
    replay_after_ambiguous_result = value["replay_after_ambiguous_result"] is True
    if replay_after_ambiguous_result and idempotency_key is None:
        raise ValueError("replay_after_ambiguous_result requires a reviewed idempotency_key")
    try:
        oracle_predicates = _oracle_predicates(value)
    except (TypeError, ValueError) as error:
        oracle_predicates = ()
        evaluation_uncertainties = (
            _evaluation_uncertainty(
                "api_steps.oracle",
                f"Reviewed oracle metadata could not be applied: {error}",
            ),
        )
    else:
        evaluation_uncertainties = ()
    try:
        terminal_response = _terminal_response_contract(value.get("terminal_response_contract"))
    except (TypeError, ValueError) as error:
        terminal_response = ()
        evaluation_uncertainties += (
            _evaluation_uncertainty(
                "api_steps.terminal_response_contract",
                f"Invalid terminal lifecycle metadata was ignored: {error}",
            ),
        )
    try:
        cleanup_acknowledgement = _cleanup_acknowledgement(value.get("cleanup_acknowledgement"))
    except (TypeError, ValueError) as error:
        cleanup_acknowledgement = None
        evaluation_uncertainties += (
            _evaluation_uncertainty(
                "api_steps.cleanup_acknowledgement",
                f"Invalid cleanup acknowledgement metadata was ignored: {error}",
            ),
        )
    if cleanup_acknowledgement is not None and not cleanup:
        evaluation_uncertainties += (
            _evaluation_uncertainty(
                "api_steps.cleanup_acknowledgement",
                "Cleanup acknowledgement metadata on a non-cleanup step was ignored.",
            ),
        )
        cleanup_acknowledgement = None
    return _ApiStep(
        id=str(value["step_id"]),
        endpoint=str(value["endpoint"]),
        method=str(value["method"]).upper(),
        request=value["request_template"],
        request_bindings=value.get("request_bindings", {}),
        response_pointer=str(value["response_selector"]),
        expected_status=(
            int(value["expected_status"])
            if type(value.get("expected_status")) is int
            and 100 <= int(value["expected_status"]) <= 599
            else None
        ),
        cleanup=cleanup,
        interaction=value["interaction"] is True,
        captures=dict(value["captures"]),  # type: ignore[arg-type]
        repeat=int(value["repeat"]),
        idempotency_key=idempotency_key,
        replay_after_ambiguous_result=replay_after_ambiguous_result,
        oracle_predicates=oracle_predicates,
        evaluation_uncertainties=evaluation_uncertainties,
        terminal_response=terminal_response,
        cleanup_acknowledgement=cleanup_acknowledgement,
    )


def _oracle_predicates(value: Mapping[str, object]) -> tuple[_OraclePredicate, ...]:
    values = value.get("oracle_predicates")
    if values is None:
        legacy = _oracle_predicate(value.get("oracle_predicate"), default_purpose="objective")
        return () if legacy is None else (legacy,)
    if not isinstance(values, list) or not values:
        raise ValueError("reviewed API oracle_predicates must be a non-empty array")
    predicates = tuple(_oracle_predicate(item) for item in values)
    if any(item is None for item in predicates):
        raise ValueError("reviewed API oracle_predicates cannot contain null")
    typed = tuple(item for item in predicates if item is not None)
    purposes = tuple(item.purpose for item in typed)
    if (
        len(typed) != 4
        or len(set(purposes)) != 4
        or set(purposes)
        != {
            "objective",
            "refusal",
            "progress",
            "invalid-premise",
        }
    ):
        raise ValueError(
            "reviewed adaptive API predicates require the exact four reviewed purposes"
        )
    return typed


def _oracle_predicate(
    value: object, *, default_purpose: str | None = None
) -> _OraclePredicate | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("reviewed API oracle predicate must be an object")
    match_values = value.get("match_values")
    if not isinstance(match_values, list) or not match_values:
        raise ValueError("reviewed API oracle predicate requires finite match values")
    if any(not _json_scalar(item) for item in match_values):
        raise ValueError("reviewed API oracle match values must be JSON scalars")
    predicate = _OraclePredicate(
        purpose=str(value.get("purpose", default_purpose or "")),
        oracle_digest=str(value.get("oracle_digest", "")),
        pointer=str(value.get("pointer", "")),
        match_values=tuple(match_values),
        modality=str(value.get("modality", "")),
        turn=str(value.get("turn", "")),
        language=str(value.get("language", "")),
    )
    if not _sha256_digest(predicate.oracle_digest):
        raise ValueError("reviewed API oracle predicate requires a SHA-256 oracle digest")
    if predicate.pointer and not predicate.pointer.startswith("/"):
        raise ValueError("reviewed API oracle predicate pointer must be a JSON Pointer")
    if predicate.purpose and predicate.purpose not in {
        "objective",
        "refusal",
        "progress",
        "invalid-premise",
    }:
        raise ValueError("reviewed API oracle predicate purpose is invalid")
    if not all((predicate.modality, predicate.turn, predicate.language)):
        raise ValueError("reviewed API oracle predicate dimensions must not be blank")
    return predicate


def _oracle_observation(
    adapter: _ApiAdapter, step: _ApiStep, response: object
) -> list[dict[str, object]]:
    return [
        _one_oracle_observation(adapter, step, response, item) for item in step.oracle_predicates
    ]


def _one_oracle_observation(
    adapter: _ApiAdapter,
    step: _ApiStep,
    response: object,
    predicate: _OraclePredicate,
) -> dict[str, object]:
    try:
        selected = _select(response, predicate.pointer, step.id)
    except ValueError:
        matched: bool | None = None
        selected_digest = None
    else:
        matched = any(_typed_equal(selected, expected) for expected in predicate.match_values)
        selected_digest = _digest_json(selected)
    binding = {
        "adapter_digest": adapter.digest,
        "compiled_adapter_digest": adapter.compiled_digest,
        "step_id": step.id,
        "oracle_digest": predicate.oracle_digest,
        "pointer": predicate.pointer,
        "match_values": predicate.match_values,
        "modality": predicate.modality,
        "turn": predicate.turn,
        "language": predicate.language,
    }
    if predicate.purpose:
        binding["purpose"] = predicate.purpose
    observation = {
        "schema": (
            "merlin.api-oracle-observation.v2"
            if predicate.purpose
            else "merlin.api-oracle-observation.v1"
        ),
        **binding,
        "binding_digest": _digest_json(binding),
        "selected_digest": selected_digest,
        "matched": matched,
    }
    return {**observation, "observation_digest": _digest_json(observation)}


def _json_scalar(value: object) -> bool:
    return (
        value is None
        or isinstance(value, (str, int, bool))
        or (isinstance(value, float) and math.isfinite(value))
    )


def _sha256_digest(value: str) -> bool:
    if len(value) != 71 or not value.startswith("sha256:"):
        return False
    try:
        int(value[7:], 16)
    except ValueError:
        return False
    return True


def _typed_equal(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def _terminal_response_contract(value: object) -> tuple[_TerminalResponseContract, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise ValueError("terminal_response_contract must be an object")
    conditions = value.get("conditions")
    if conditions is not None:
        if not isinstance(conditions, list) or not conditions:
            raise ValueError("terminal response conditions must be a non-empty array")
        return tuple(_one_terminal_response_contract(item) for item in conditions)
    return (_one_terminal_response_contract(value),)


def _cleanup_acknowledgement(value: object) -> _CleanupAcknowledgement | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("cleanup acknowledgement must be an object")
    pointer = value.get("pointer")
    match_values = value.get("match_values")
    status_codes = value.get("status_codes")
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("cleanup acknowledgement pointer must be a JSON Pointer")
    if not isinstance(match_values, list) or not match_values:
        raise ValueError("cleanup acknowledgement requires finite match values")
    if any(not _json_scalar(item) for item in match_values):
        raise ValueError("cleanup acknowledgement match values must be JSON scalars")
    if (
        not isinstance(status_codes, list)
        or not status_codes
        or any(type(item) is not int or not 200 <= item <= 299 for item in status_codes)
    ):
        raise ValueError("cleanup acknowledgement status codes must be 2xx integers")
    return _CleanupAcknowledgement(
        pointer=pointer,
        match_values=tuple(match_values),
        status_codes=tuple(status_codes),
    )


def _one_terminal_response_contract(value: object) -> _TerminalResponseContract:
    if not isinstance(value, Mapping):
        raise ValueError("terminal response condition must be an object")
    pointer = value.get("pointer")
    match_values = value.get("match_values")
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("terminal response pointer must be a JSON Pointer")
    if not isinstance(match_values, list) or not match_values:
        raise ValueError("terminal response contract requires finite match values")
    if any(not _json_scalar(item) for item in match_values):
        raise ValueError("terminal response match values must be JSON scalars")
    effect_applied = value.get("effect_applied")
    replay_safe = value.get("replay_safe")
    if not isinstance(effect_applied, bool) or not isinstance(replay_safe, bool):
        raise ValueError("terminal response effect and replay attestations must be boolean")
    if effect_applied and replay_safe:
        raise ValueError("an applied terminal effect cannot be declared replay safe")
    raw_status_codes = value.get("status_codes", [])
    if not isinstance(raw_status_codes, list) or any(
        type(item) is not int or not 100 <= item <= 599 for item in raw_status_codes
    ):
        raise ValueError("terminal response status_codes must be finite HTTP status integers")
    return _TerminalResponseContract(
        pointer=pointer,
        match_values=tuple(match_values),
        effect_applied=effect_applied,
        replay_safe=replay_safe,
        status_codes=tuple(raw_status_codes),
    )


def _terminal_response_match(
    step: _ApiStep, status_code: int, response: object
) -> _TerminalResponseContract | None:
    for contract in step.terminal_response:
        if contract.status_codes and status_code not in contract.status_codes:
            continue
        try:
            selected = _select(response, contract.pointer, step.id)
        except _ResponsePointerMissing:
            continue
        if any(_typed_equal(selected, expected) for expected in contract.match_values):
            return contract
    return None


def _contains_error_signal(response: object) -> bool:
    if not isinstance(response, Mapping):
        return False
    return any(
        response.get(key) not in (None, False, "", (), [], {}) for key in ("error", "errors")
    )


def _terminal_exchanges(variables: Mapping[str, object]) -> list[dict[str, object]]:
    value = variables.get("__terminal_exchanges", ())
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _terminal_effect_digest(adapter: _ApiAdapter, input_text: str) -> str:
    return _digest_json(
        {
            "compiled_adapter_digest": adapter.compiled_digest,
            "interaction_steps": [step.id for step in adapter.steps if step.interaction],
            "input_digest": _digest_json(input_text),
        }
    )


def _replay_disposition(variables: Mapping[str, object], effect_digest: str) -> str | None:
    values = variables.get("__terminal_replay_dispositions")
    if not isinstance(values, Mapping):
        return None
    value = values.get(effect_digest)
    if not isinstance(value, Mapping):
        return None
    disposition = value.get("disposition")
    return (
        str(disposition)
        if disposition in {"safe_rotation_used", "no_replay", "rotation_exhausted"}
        else None
    )


def _set_replay_disposition(
    variables: dict[str, object],
    *,
    effect_digest: str,
    input_digest: str,
    disposition: str,
) -> None:
    existing = variables.get("__terminal_replay_dispositions")
    values = dict(existing) if isinstance(existing, Mapping) else {}
    values[effect_digest] = {
        "effect_digest": effect_digest,
        "input_digest": input_digest,
        "disposition": disposition,
    }
    variables["__terminal_replay_dispositions"] = values


def _replay_refused_result(state: Any, effect_digest: str, disposition: str) -> ToolResult:
    variables = state.variables if state is not None else {}
    values = variables.get("__terminal_replay_dispositions")
    record = values.get(effect_digest) if isinstance(values, Mapping) else None
    input_digest = record.get("input_digest") if isinstance(record, Mapping) else None
    return ToolResult(
        content=(
            "exact target effect replay refused because its reviewed terminal-session "
            f"disposition is {disposition}"
        ),
        is_error=True,
        metadata={
            "infrastructure_failure_category": (
                InfrastructureFailureCategory.UNHEALTHY_TARGET_SESSION.value
            ),
            "retryable": False,
            "exact_replay_retryable": False,
            "replay_disposition": disposition,
            "effect_digest": effect_digest,
            **({"input_digest": input_digest} if isinstance(input_digest, str) else {}),
            "target_exchanges": _terminal_exchanges(variables),
            **(
                {
                    "target_session_id": state.target_session_id,
                    "target_session_generation": state.generation,
                }
                if state is not None
                else {}
            ),
        },
    )


def _digest_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _canonical_compilation_json(compilation: AdapterCompilation) -> str:
    return json.dumps(
        compilation.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )


def _context_adapter_identity_failure(
    context: ToolContext,
    adapter: _ApiAdapter,
) -> ToolResult | None:
    serialized = context.compiled_adapter
    claimed_digest = context.compiled_adapter_digest
    if serialized is None and claimed_digest is None:
        # Assignments produced before the compiled identity seal existed remain
        # executable, but every result is labelled as legacy by invoke().
        return None
    if not isinstance(serialized, str) or not isinstance(claimed_digest, str):
        return _adapter_identity_failure(
            "assignment compiled adapter state is incomplete or invalid",
            claimed_digest=claimed_digest,
        )
    try:
        assignment = read_target_adapter(serialized)
    except AdapterCompileError:
        return _adapter_identity_failure(
            "assignment compiled adapter state is malformed",
            claimed_digest=claimed_digest,
        )
    observed_digest = compiled_target_adapter_digest(assignment)
    if observed_digest != claimed_digest:
        return _adapter_identity_failure(
            "assignment compiled adapter digest is invalid",
            claimed_digest=claimed_digest,
            observed_digest=observed_digest,
        )
    normalized = _canonical_compilation_json(assignment)
    if observed_digest != adapter.compiled_digest or normalized != adapter.compiled_adapter:
        return _adapter_identity_failure(
            "assignment and campaign compiled adapter identities disagree",
            claimed_digest=claimed_digest,
            observed_digest=observed_digest,
        )
    return None


def _adapter_identity_failure(
    message: str,
    *,
    claimed_digest: object = None,
    observed_digest: str | None = None,
) -> ToolResult:
    return ToolResult(
        content=message,
        is_error=True,
        metadata={
            "effect": "target_network",
            "failure_code": "adapter_state_integrity",
            "infrastructure_failure_category": "adapter_state_integrity",
            "adapter_state_integrity": True,
            "no_target_effect": True,
            "target_effect_attempted": False,
            "target_call_budget_charge": 0,
            "limit_interaction_count": 0,
            "retryable": False,
            **(
                {"assignment_compiled_adapter_digest": claimed_digest}
                if isinstance(claimed_digest, str)
                else {}
            ),
            **(
                {"observed_assignment_compiled_adapter_digest": observed_digest}
                if observed_digest is not None
                else {}
            ),
        },
    )


def _with_adapter_identity(result: ToolResult, adapter: _ApiAdapter) -> ToolResult:
    metadata = dict(result.metadata)
    metadata.setdefault("target_profile_id", adapter.target_id)
    metadata.setdefault("adapter_digest", adapter.digest)
    metadata["compiled_adapter_digest"] = adapter.compiled_digest
    return result.model_copy(update={"metadata": metadata})


def _state_matches_adapter(state: CampaignApiSession, adapter: _ApiAdapter) -> bool:
    return state.adapter_digest in {adapter.compiled_digest, adapter.digest}


def _pending_step(step: _ApiStep, request: object) -> dict[str, object]:
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    return {
        "id": step.id,
        "request_digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "request": request,
        "idempotency_key": step.idempotency_key,
        "replay_after_ambiguous_result": step.replay_after_ambiguous_result,
    }


def _render(value: object, variables: Mapping[str, object], input_text: str) -> object:
    if isinstance(value, Mapping):
        return {str(key): _render(item, variables, input_text) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, variables, input_text) for item in value]
    if not isinstance(value, str) or "{{" not in value:
        return value
    if value == "{{input}}":
        return input_text
    if value.startswith("{{capture:") and value.endswith("}}"):
        name = value[len("{{capture:") : -2]
        if name in variables:
            return variables[name]
    raise ValueError("reviewed API request references an unavailable placeholder")


def _select(value: object, pointer: str, step_id: str) -> object:
    if pointer == "":
        return value
    body = value
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
            value = value[int(token)]
        else:
            raise _ResponsePointerMissing(step_id, pointer, body)
    return value


def _campaign_id(assignment_id: str) -> str | None:
    if not assignment_id.startswith("campaign/"):
        return None
    parts = assignment_id.split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 else None


def _reviewed_session_scope_id(assignment_id: str) -> str:
    """Keep each logical assignment in an independent target-session scope."""
    return assignment_id


def _session_expired_signal(raw: Mapping[str, object]) -> _SessionExpiredSignal | None:
    """Project the reviewed session-creation precondition as the no-session signal.

    Absence or a malformed precondition keeps the legacy behavior: no automatic
    start fallback is declared by the reviewed adapter.
    """
    transition = raw.get("session_creation_transition")
    if not isinstance(transition, Mapping):
        return None
    precondition = transition.get("precondition")
    if not isinstance(precondition, Mapping):
        return None
    statuses = precondition.get("status_codes")
    pointer = precondition.get("pointer")
    match_values = precondition.get("match_values")
    if (
        not isinstance(statuses, (list, tuple))
        or not statuses
        or any(
            isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599
            for status in statuses
        )
        or not isinstance(pointer, str)
        or not pointer.startswith("/")
        or not isinstance(match_values, (list, tuple))
        or not match_values
        or any(not _json_scalar(value) for value in match_values)
    ):
        return None
    return _SessionExpiredSignal(
        status_codes=tuple(statuses),
        pointer=pointer,
        match_values=tuple(match_values),
    )


def _session_expired_signal_matches(
    signal: _SessionExpiredSignal, exchange: Mapping[str, object]
) -> bool:
    status = exchange.get("status_code")
    if isinstance(status, bool) or not isinstance(status, int) or status not in signal.status_codes:
        return False
    try:
        observed = _select(exchange.get("response_body"), signal.pointer, "session-expired")
    except _ResponsePointerMissing:
        return False
    return any(_typed_equal(observed, expected) for expected in signal.match_values)


def _fresh_setup_response_matches(
    adapter: _ApiAdapter,
    step: _ApiStep,
    exchange: Mapping[str, object],
) -> bool:
    predicate = adapter.fresh_setup_response
    if predicate is None:
        return True
    if (
        step.method != predicate.method
        or urljoin(adapter.endpoint.rstrip("/") + "/", step.endpoint) != predicate.url
    ):
        return True
    status = exchange.get("status_code")
    if isinstance(status, bool) or not isinstance(status, int):
        return False
    if status not in predicate.status_codes:
        return False
    try:
        observed = _select(exchange.get("response_body"), predicate.pointer, step.id)
    except _ResponsePointerMissing:
        return False
    return any(_typed_equal(observed, expected) for expected in predicate.match_values)


def _session_isolation_unavailable_result(
    state: CampaignApiSession,
    error: RemoteSessionClaimConflict,
    exchanges: list[dict[str, object]],
) -> ToolResult:
    owner_scope_id = error.owner_session_scope_id
    owner_campaign_id = _campaign_id(owner_scope_id or "")
    return ToolResult(
        content=(
            "reviewed target session isolation is unavailable; the physical session "
            "was already owned by another active assignment scope"
        ),
        is_error=True,
        metadata={
            "campaign_id": state.campaign_id,
            "target_profile_id": state.target_id,
            "compiled_adapter_digest": state.adapter_digest,
            "target_session_id": state.target_session_id,
            "target_session_generation": state.generation,
            "session_scope_id": state.session_scope_id,
            "owner_campaign_id": owner_campaign_id,
            "owner_session_scope_id": owner_scope_id,
            "failure_code": "session_isolation_unavailable",
            "session_isolation_unavailable": True,
            "session_health": "unknown",
            "retryable": True,
            "infrastructure_failure_category": (
                InfrastructureFailureCategory.DELIVERY_FAILURE.value
            ),
            "remote_session_id_sha256": hashlib.sha256(
                error.remote_session_id.encode("utf-8")
            ).hexdigest(),
            "target_exchanges": exchanges,
            "current_target_exchanges": exchanges,
            "limit_interaction_count": 0,
            "target_call_budget_charge": sum(
                int(exchange.get("target_call_budget_charge", 1))
                for exchange in exchanges
                if exchange.get("cleanup") is not True
            ),
            "acknowledged_capability_evidence": False,
        },
    )


def _legacy_session_reconciliation_unavailable_result(
    state: CampaignApiSession,
    detail: str,
) -> ToolResult:
    return ToolResult(
        content=(
            "a migrated campaign-wide target session could not be reconciled; "
            "new scoped target delivery was not started"
        ),
        is_error=True,
        metadata={
            "campaign_id": state.campaign_id,
            "target_profile_id": state.target_id,
            "target_session_id": state.target_session_id,
            "target_session_generation": state.generation,
            "session_scope_id": state.session_scope_id,
            "failure_code": "legacy_session_reconciliation_unavailable",
            "legacy_session_quarantined": True,
            "session_health": "unknown",
            "retryable": True,
            "infrastructure_failure_category": (
                InfrastructureFailureCategory.UNHEALTHY_TARGET_SESSION.value
            ),
            "detail": detail,
            "limit_interaction_count": 0,
            "target_call_budget_charge": 0,
            "acknowledged_capability_evidence": False,
        },
    )


def _cleanup_unresolved_result(error: BaseException) -> ToolResult:
    failure_category = _infrastructure_failure_category(error)
    return ToolResult(
        content=f"reviewed target cleanup remains unresolved: {error}",
        is_error=True,
        metadata={
            "failure_code": "target_cleanup_unresolved",
            "target_cleanup_unresolved": True,
            "session_health": "unknown",
            "retryable": True,
            "infrastructure_failure_category": (
                InfrastructureFailureCategory.UNHEALTHY_TARGET_SESSION.value
                if failure_category is None
                else failure_category.value
            ),
            "limit_interaction_count": 0,
            "target_call_budget_charge": 0,
            "acknowledged_capability_evidence": False,
        },
    )


def _target_delivery_rejected_result(
    state: CampaignApiSession,
    adapter: _ApiAdapter,
    target_exchanges: list[dict[str, object]],
    current_target_exchanges: list[dict[str, object]],
) -> ToolResult:
    current = current_target_exchanges[-1]
    status_code = int(current["status_code"])
    evaluation_uncertainties = list(adapter.evaluation_uncertainties)
    for exchange in current_target_exchanges:
        evaluation_uncertainties.extend(
            _uncertainty_records(exchange.get("evaluation_uncertainties", ()))
        )
    interaction_count = sum(
        1
        for exchange in current_target_exchanges
        if exchange.get("interaction") is True and exchange.get("cleanup") is not True
    )
    target_call_budget_charge = sum(
        int(exchange.get("target_call_budget_charge", 1))
        for exchange in current_target_exchanges
        if exchange.get("cleanup") is not True
    )
    return ToolResult(
        content=(
            f"reviewed target delivery returned HTTP {status_code}; the response was "
            "preserved as retryable infrastructure telemetry, not capability evidence"
        ),
        is_error=True,
        metadata={
            "campaign_id": state.campaign_id,
            "target_profile_id": adapter.target_id,
            "adapter_digest": adapter.digest,
            "target_session_id": state.target_session_id,
            "target_session_generation": state.generation,
            "session_scope_id": state.session_scope_id,
            "failure_code": "target_delivery_rejected",
            "target_delivery_rejected": True,
            "session_health": "unknown",
            "retryable": True,
            "infrastructure_failure_category": (
                InfrastructureFailureCategory.DELIVERY_FAILURE.value
            ),
            "target_exchanges": target_exchanges,
            "current_target_exchanges": current_target_exchanges,
            "limit_interaction_count": interaction_count,
            "target_call_budget_charge": target_call_budget_charge,
            "acknowledged_capability_evidence": False,
            "evaluation_uncertain": True,
            "evaluation_uncertainties": _dedupe_uncertainties(
                evaluation_uncertainties
                + [
                    _evaluation_uncertainty(
                        "target_delivery.http_status",
                        f"The target returned unexpected HTTP {status_code}; no reviewed "
                        "terminal lifecycle contract matched.",
                    )
                ]
            ),
            **(
                {"reviewed_target_delivery": adapter.target_delivery}
                if adapter.target_delivery is not None
                else {}
            ),
        },
    )


def _evaluation_uncertainty(
    source: str, reason: str, *, confidence: float = 0.0
) -> dict[str, object]:
    return {
        "kind": "evaluation_uncertain",
        "source": source,
        "reason": reason,
        "confidence": confidence,
        "affects": ["ranking", "evaluation"],
    }


def _uncertainty_records(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    records: list[Mapping[str, object]] = []
    for item in value:
        if isinstance(item, Mapping) and str(item.get("reason", "")).strip():
            records.append(dict(item))
        elif str(item).strip():
            records.append(_evaluation_uncertainty("adapter", str(item).strip()))
    return tuple(records)


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _dedupe_uncertainties(
    values: list[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    unique: dict[str, Mapping[str, object]] = {}
    for value in values:
        key = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        unique.setdefault(key, value)
    return list(unique.values())
