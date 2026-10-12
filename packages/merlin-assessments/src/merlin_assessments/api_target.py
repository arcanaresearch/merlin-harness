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
from merlin_contracts.target_capabilities import (
    RequestConstraint,
    StateCapabilities,
    StateObservation,
    StateSelector,
    StructuredConstraintRejection,
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


class _RequestConstraintViolation(ValueError):
    """A fully rendered request violates a reviewed target protocol constraint."""

    def __init__(
        self,
        step_id: str,
        request: object,
        observations: Sequence[Mapping[str, object]],
    ) -> None:
        self.step_id = step_id
        self.request = request
        self.observations = [dict(item) for item in observations]
        self.exchanges: list[dict[str, object]] = []
        super().__init__(
            f"rendered request violates reviewed constraints at step {step_id}; "
            "the target was not called"
        )

    def include_completed_exchanges(self, exchanges: Sequence[Mapping[str, object]]) -> None:
        self.exchanges[:0] = [dict(item) for item in exchanges]


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
    structured_constraint_rejections: tuple[StructuredConstraintRejection, ...] = ()
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
    request_constraints: tuple[RequestConstraint, ...] = ()
    state_capabilities: StateCapabilities | None = None


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
        parallel_capacity = _target_parallel_capacity(adapter)
        lock_scope = (
            f"target-state:{context.target_id}" if parallel_capacity == 1 else session_scope_id
        )
        lock = self._locks.setdefault((context.campaign_id, lock_scope), asyncio.Lock())
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
                except _RequestConstraintViolation as error:
                    return resolved(_request_constraint_violation_result(error))
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
        parallel_capacity = _target_parallel_capacity(adapter)
        if parallel_capacity is None:
            return await self._invoke_under_execution_lease(
                context,
                text,
                pointer_miss_retries,
                session_scope_id,
                adapter,
                state,
            )
        claimant_id = _target_execution_claimant_id(context.campaign_id, session_scope_id, text)
        lease_seconds = max(60, int(self._timeout * 4))
        claim_id = await asyncio.to_thread(
            self._sessions.claim_target_execution,
            context.campaign_id,
            target_id=adapter.target_id,
            session_scope_id=session_scope_id,
            claimant_id=claimant_id,
            lease_seconds=lease_seconds,
            maximum_parallel_states=parallel_capacity,
        )
        if claim_id is None:
            return _target_execution_busy_result(state)
        owner = asyncio.current_task()
        heartbeat = asyncio.create_task(
            self._renew_target_execution_lease(claim_id, claimant_id, lease_seconds, owner)
        )
        try:
            result = await self._invoke_under_execution_lease(
                context,
                text,
                pointer_miss_retries,
                session_scope_id,
                adapter,
                state,
            )
            still_owned = await asyncio.to_thread(
                self._sessions.validate_target_execution, claim_id, claimant_id
            )
            if not still_owned:
                return _target_execution_lease_lost_result(result)
            if _allows_serial_state_reuse(adapter):
                current_state = await asyncio.to_thread(
                    self._sessions.load,
                    context.campaign_id,
                    target_id=adapter.target_id,
                    session_scope_id=session_scope_id,
                )
                if (
                    current_state is not None
                    and current_state.remote_session_id is not None
                    and current_state.physical_state_identity == current_state.remote_session_id
                ):
                    await asyncio.to_thread(
                        self._sessions.release_remote_session,
                        context.campaign_id,
                        target_id=adapter.target_id,
                        session_scope_id=session_scope_id,
                        remote_session_id=current_state.remote_session_id,
                    )
            return result
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await asyncio.to_thread(self._sessions.release_target_execution, claim_id, claimant_id)

    async def _renew_target_execution_lease(
        self,
        claim_id: str,
        claimant_id: str,
        lease_seconds: int,
        owner: asyncio.Task[object] | None,
    ) -> None:
        while True:
            await asyncio.sleep(max(1, lease_seconds // 3))
            renewed = await asyncio.to_thread(
                self._sessions.renew_target_execution,
                claim_id,
                claimant_id,
                lease_seconds,
            )
            if not renewed:
                if owner is not None:
                    owner.cancel()
                return

    async def _invoke_under_execution_lease(
        self,
        context: ToolContext,
        text: str,
        pointer_miss_retries: int,
        session_scope_id: str,
        adapter: _ApiAdapter,
        state: CampaignApiSession,
    ) -> ToolResult:
        variables = dict(state.variables)
        list_constraints = getattr(self._sessions, "list_request_constraints", None)
        durable_constraints: Sequence[Mapping[str, object]] = ()
        if callable(list_constraints):
            durable_constraints = await asyncio.to_thread(
                list_constraints,
                context.campaign_id,
                target_id=adapter.target_id,
                adapter_digest=adapter.compiled_digest,
            )
        _preflight_interaction_constraints(
            adapter,
            variables,
            text,
            durable_constraints=durable_constraints,
        )
        isolation_failure = None
        if not _has_reviewed_physical_identity(adapter):
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
            isolation_failure = (
                await self._claim_reviewed_physical_state(
                    state,
                    adapter,
                    exchange,
                    exchanges=exchanges,
                )
                if _has_reviewed_physical_identity(adapter)
                else await self._claim_remote_session(
                    state,
                    variables,
                    exchanges=exchanges,
                )
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
                exchange["attempt_disposition"] = "contaminated"
                exchange["retryable"] = True
                exchange["acknowledged_capability_evidence"] = False
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
                except _RequestConstraintViolation as error:
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
                if (
                    session_reused
                    and _has_state_contract(adapter)
                    or _has_reviewed_physical_identity(adapter)
                    and state.remote_session_id is None
                ):
                    exchange["attempt_disposition"] = "contaminated"
                    exchange["retryable"] = True
                    exchange["acknowledged_capability_evidence"] = False
                    exchange.setdefault("evaluation_uncertainties", []).append(
                        _evaluation_uncertainty(
                            "api_target.reused_physical_state",
                            (
                                "the candidate executed on durable target state changed by an "
                                "earlier invocation; the transition remains attributable but "
                                "cannot count as clean independent capability evidence"
                            ),
                        )
                    )
                    exchange["evaluation_uncertain"] = True
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
            isolation_failure = (
                await self._claim_reviewed_physical_state(
                    state,
                    adapter,
                    exchange,
                    exchanges=[*exchanges, *restart_exchanges],
                )
                if _has_reviewed_physical_identity(adapter)
                else await self._claim_remote_session(
                    state,
                    variables,
                    exchanges=[*exchanges, *restart_exchanges],
                )
            )
            if isolation_failure is not None:
                return variables, restart_exchanges, isolation_failure
        return variables, restart_exchanges, None

    async def _claim_reviewed_physical_state(
        self,
        state: CampaignApiSession,
        adapter: _ApiAdapter,
        exchange: dict[str, object],
        *,
        exchanges: list[dict[str, object]],
    ) -> ToolResult | None:
        capabilities = adapter.state_capabilities
        identity_contract = capabilities.identity if capabilities is not None else None
        if identity_contract is None or identity_contract.baseline_selector.step_id != exchange.get(
            "id"
        ):
            return None
        identity = next(
            (
                item
                for item in _mapping_list(exchange.get("state_observations"))
                if item.get("role") == "state_identity"
                and item.get("phase") == "baseline"
                and item.get("selected_digest") is not None
            ),
            None,
        )
        if identity is None:
            exchange["attempt_disposition"] = "contaminated"
            exchange["retryable"] = True
            exchange["acknowledged_capability_evidence"] = False
            exchange["evaluation_uncertain"] = True
            exchange.setdefault("evaluation_uncertainties", []).append(
                _evaluation_uncertainty(
                    "api_target.physical_state_identity",
                    (
                        "the reviewed physical-state identity was not observed before "
                        "interaction; delivery may continue but cannot produce clean evidence"
                    ),
                )
            )
            return None
        claim_key = "physical-state:" + str(identity["selected_digest"]).removeprefix("sha256:")
        if state.physical_state_identity == claim_key:
            exchange["physical_state_claimed"] = True
            return None
        try:
            claim = await asyncio.to_thread(
                self._sessions.claim_physical_state,
                state.campaign_id,
                claim_key,
                target_id=state.target_id,
                session_scope_id=state.session_scope_id,
            )
        except RemoteSessionClaimConflict:
            exchange["attempt_disposition"] = "contaminated"
            exchange["retryable"] = True
            exchange["acknowledged_capability_evidence"] = False
            exchange["evaluation_uncertain"] = True
            exchange["physical_state_alias_detected"] = True
            exchange.setdefault("evaluation_uncertainties", []).append(
                _evaluation_uncertainty(
                    "api_target.physical_state_alias",
                    (
                        "the reviewed physical state was already assigned to another "
                        "scope; execution continues as contaminated evidence"
                    ),
                )
            )
            return _physical_state_unavailable_result(
                state,
                exchanges,
                reason="the reviewed physical state is still owned by another assignment",
            )
        exchange["physical_state_claimed"] = True
        if claim.previously_observed:
            if _reviewed_state_reuse_isolated(adapter, exchange):
                exchange["physical_state_sequential_reuse"] = True
                exchange["physical_state_reuse_basis"] = (
                    "exclusive_target_lease_with_paired_identity_observation"
                )
                return None
            exchange["attempt_disposition"] = "contaminated"
            exchange["retryable"] = True
            exchange["acknowledged_capability_evidence"] = False
            exchange["physical_state_alias_detected"] = True
            exchange["prior_physical_state_scope_count"] = len(claim.prior_session_scope_ids)
            exchange["prior_physical_state_cleanup_dispositions"] = [
                item for item in claim.prior_cleanup_dispositions
            ]
            await asyncio.to_thread(
                self._sessions.release_remote_session,
                state.campaign_id,
                target_id=state.target_id,
                session_scope_id=state.session_scope_id,
                remote_session_id=claim_key,
            )
            return _physical_state_unavailable_result(
                state,
                exchanges,
                reason=(
                    "the reviewed physical state was reused without an exclusive lease and "
                    "paired before/after identity attribution"
                ),
            )
        return None

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
                    exchanges[-1]["attempt_disposition"] = "contaminated"
                    exchanges[-1]["retryable"] = True
                    exchanges[-1]["acknowledged_capability_evidence"] = False
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
        request_constraint_observations = _request_constraint_observations(adapter, step, request)
        protocol_invalid = any(
            item.get("satisfied") is False for item in request_constraint_observations
        )
        if protocol_invalid:
            raise _RequestConstraintViolation(
                step.id,
                request,
                request_constraint_observations,
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
        response_state_observations = _state_observations(adapter, step, response_json, variables)
        freshness_observations = _freshness_observations(adapter, step, response_json, variables)
        freshness_unproven = any(item.get("matched") is not True for item in freshness_observations)
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
            discovered_constraints = _structured_constraint_rejection_observations(
                adapter,
                step,
                request,
                status_code,
                response_json,
            )
            if discovered_constraints:
                record_constraint = getattr(self._sessions, "record_request_constraint", None)
                if callable(record_constraint):
                    for constraint in discovered_constraints:
                        await asyncio.to_thread(
                            record_constraint,
                            campaign_id,
                            target_id=adapter.target_id,
                            adapter_digest=adapter.compiled_digest,
                            constraint=constraint,
                        )
                request_constraint_observations.extend(discovered_constraints)
                protocol_invalid = True
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
                    "outcome": (
                        "request_constraint_violation"
                        if protocol_invalid
                        else "target_delivery_rejected"
                    ),
                    "target_delivery_rejected": not protocol_invalid,
                    "session_health": "unknown",
                    "retryable": True,
                    **(
                        {"attempt_disposition": "protocol_invalid"}
                        if protocol_invalid
                        else {
                            "infrastructure_failure_category": (
                                InfrastructureFailureCategory.DELIVERY_FAILURE.value
                            )
                        }
                    ),
                    "latency_ms": latency_ms,
                    "target_call_budget_charge": attempt_count,
                    "evaluation_uncertain": True,
                    "evaluation_uncertainties": _dedupe_uncertainties(evaluation_uncertainties),
                    **(
                        {"state_observations": response_state_observations}
                        if response_state_observations
                        else {}
                    ),
                    **(
                        {"freshness_observations": freshness_observations}
                        if freshness_observations
                        else {}
                    ),
                    **(
                        {"request_constraint_observations": request_constraint_observations}
                        if request_constraint_observations
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
        freshness_observations = _freshness_observations(adapter, step, response_json, variables)
        freshness_unproven = any(item.get("matched") is not True for item in freshness_observations)
        oracle_observations = _oracle_observation(adapter, step, response_json)
        state_observations = _state_observations(adapter, step, response_json, variables)
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
                {"request_constraint_observations": request_constraint_observations}
                if request_constraint_observations
                else {}
            ),
            **({"state_observations": state_observations} if state_observations else {}),
            **(
                {"freshness_observations": freshness_observations} if freshness_observations else {}
            ),
            **(
                {
                    "attempt_disposition": "protocol_invalid",
                    "acknowledged_capability_evidence": False,
                }
                if protocol_invalid
                else {}
            ),
            **(
                {
                    "attempt_disposition": "contaminated",
                    "retryable": True,
                    "acknowledged_capability_evidence": False,
                }
                if freshness_unproven and not protocol_invalid
                else {}
            ),
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
                observed = (
                    _select_with_text_siblings(response_json, step.response_pointer, step.id)
                    if step.interaction
                    else _select(response_json, step.response_pointer, step.id)
                )
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
                observed = (
                    _select_with_text_siblings(response_json, step.response_pointer, step.id)
                    if step.interaction
                    else _select(response_json, step.response_pointer, step.id)
                )
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
            request_constraints=executable.request_constraints,
            state_capabilities=executable.state_capabilities,
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
            "structured_constraint_rejections": compiled.structured_constraint_rejections,
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
        structured_constraint_rejections=tuple(
            item
            for item in value.get("structured_constraint_rejections", ())
            if isinstance(item, StructuredConstraintRejection)
        ),
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


def _request_constraint_observations(
    adapter: _ApiAdapter,
    step: _ApiStep,
    request: object,
) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    for constraint in adapter.request_constraints:
        if constraint.step_id != step.id:
            continue
        binding = {
            "step_id": constraint.step_id,
            "evaluation_root": constraint.evaluation_root,
            "path": constraint.path,
            "kind": constraint.kind,
            "maximum": constraint.maximum,
            "unit": constraint.unit.value,
        }
        try:
            selected = _select(request, constraint.path, step.id)
        except _ResponsePointerMissing:
            observed: int | None = None
            satisfied: bool | None = None
            selected_digest: str | None = None
        else:
            selected_digest = _digest_json(selected)
            if isinstance(selected, str):
                observed = (
                    len(selected.encode("utf-8"))
                    if constraint.unit.value == "utf8_bytes"
                    else len(selected)
                )
                satisfied = observed <= constraint.maximum
            else:
                observed = None
                satisfied = False
        observation: dict[str, object] = {
            "schema": "merlin.request-constraint-observation.v1",
            **binding,
            "binding_digest": _digest_json(binding),
            "selected_digest": selected_digest,
            "observed": observed,
            "satisfied": satisfied,
            **({"source": constraint.source} if constraint.source is not None else {}),
            **(
                {"evidence_refs": list(constraint.evidence_refs)}
                if constraint.evidence_refs
                else {}
            ),
            "evidence_bindings": [
                item.model_dump(mode="json") for item in constraint.evidence_bindings
            ],
            **({"confidence": constraint.confidence} if constraint.confidence is not None else {}),
        }
        observations.append({**observation, "observation_digest": _digest_json(observation)})
    return observations


def _structured_constraint_rejection_observations(
    adapter: _ApiAdapter,
    step: _ApiStep,
    request: object,
    status_code: int,
    response_body: object,
) -> list[dict[str, object]]:
    """Promote only reviewed, structured rejection fields into runtime constraints."""

    observations: list[dict[str, object]] = []
    response_ref = "target-response:" + _digest_json(response_body)
    for rule in step.structured_constraint_rejections:
        if status_code not in rule.status_codes:
            continue
        try:
            maximum = _select(response_body, rule.maximum_pointer, step.id)
            selected = _select(request, rule.request_path, step.id)
        except _ResponsePointerMissing:
            continue
        if (
            isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or maximum < 1
            or not isinstance(selected, str)
        ):
            continue
        observed = (
            len(selected.encode("utf-8")) if rule.unit.value == "utf8_bytes" else len(selected)
        )
        # A status and pointer mapping is reviewed, but inconsistent target data is
        # not enough to invent a limit. Preserve it as ordinary rejection telemetry.
        if observed <= maximum:
            continue
        binding = {
            "compiled_adapter_digest": adapter.compiled_digest,
            "step_id": step.id,
            "evaluation_root": "request_body",
            "path": rule.request_path,
            "kind": rule.kind,
            "maximum": maximum,
            "unit": rule.unit.value,
            "status_code": status_code,
            "maximum_pointer": rule.maximum_pointer,
        }
        evidence_binding = {
            "evidence_ref": response_ref,
            "observed_maximum": maximum,
        }
        observation: dict[str, object] = {
            "schema": "merlin.request-constraint-observation.v1",
            "step_id": step.id,
            "evaluation_root": "request_body",
            "path": rule.request_path,
            "kind": rule.kind,
            "maximum": maximum,
            "unit": rule.unit.value,
            "binding_digest": _digest_json(binding),
            "selected_digest": _digest_json(selected),
            "observed": observed,
            "satisfied": False,
            "source": "structured_target_rejection",
            "evidence_refs": [response_ref],
            "evidence_bindings": [evidence_binding],
            "structured_rejection": {
                "status_code": status_code,
                "maximum_pointer": rule.maximum_pointer,
                "response_digest": _digest_json(response_body),
            },
        }
        observations.append({**observation, "observation_digest": _digest_json(observation)})
    return observations


def _preflight_interaction_constraints(
    adapter: _ApiAdapter,
    variables: Mapping[str, object],
    input_text: str,
    *,
    durable_constraints: Sequence[Mapping[str, object]] = (),
) -> None:
    preflight_variables = dict(variables)
    for lifecycle_step in adapter.steps:
        for capture_name in lifecycle_step.captures:
            preflight_variables.setdefault(capture_name, "")
    for step in adapter.steps:
        if not step.interaction:
            continue
        request = _render(step.request, preflight_variables, input_text)
        observations = _request_constraint_observations(adapter, step, request)
        observations.extend(
            _durable_request_constraint_observations(step, request, durable_constraints)
        )
        if any(item.get("satisfied") is False for item in observations):
            raise _RequestConstraintViolation(step.id, request, observations)


def _durable_request_constraint_observations(
    step: _ApiStep,
    request: object,
    constraints: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Re-evaluate persisted structured constraints against the rendered request."""
    observations: list[dict[str, object]] = []
    for constraint in constraints:
        if constraint.get("step_id") != step.id:
            continue
        path = constraint.get("path")
        maximum = constraint.get("maximum")
        unit = constraint.get("unit")
        if (
            not isinstance(path, str)
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or maximum < 1
            or unit not in {"unicode_characters", "utf8_bytes"}
        ):
            continue
        try:
            selected = _select(request, path, step.id)
        except _ResponsePointerMissing:
            selected_digest: str | None = None
            observed: int | None = None
            satisfied: bool | None = None
        else:
            selected_digest = _digest_json(selected)
            if isinstance(selected, str):
                observed = len(selected.encode("utf-8")) if unit == "utf8_bytes" else len(selected)
                satisfied = observed <= maximum
            else:
                observed = None
                satisfied = False
        observation = {
            **dict(constraint),
            "selected_digest": selected_digest,
            "observed": observed,
            "satisfied": satisfied,
            "preflight_source": "durable_structured_target_rejection",
        }
        observation.pop("observation_digest", None)
        observations.append({**observation, "observation_digest": _digest_json(observation)})
    return observations


def _state_observations(
    adapter: _ApiAdapter,
    step: _ApiStep,
    response_body: object,
    captures: Mapping[str, object],
) -> list[dict[str, object]]:
    capabilities = adapter.state_capabilities
    if capabilities is None:
        return []
    observations: list[dict[str, object]] = []
    if capabilities.identity is not None:
        observations.extend(
            _bound_state_observation(
                adapter,
                step,
                response_body,
                captures,
                name="physical_state_identity",
                role="state_identity",
                phase="baseline",
                selector=capabilities.identity.baseline_selector,
                evidence_refs=capabilities.identity.evidence_refs,
            )
        )
        if capabilities.identity.result_selector is not None:
            observations.extend(
                _bound_state_observation(
                    adapter,
                    step,
                    response_body,
                    captures,
                    name="physical_state_identity",
                    role="state_identity",
                    phase="result",
                    selector=capabilities.identity.result_selector,
                    evidence_refs=capabilities.identity.evidence_refs,
                )
            )
    for observation in capabilities.observations:
        observations.extend(
            _configured_state_observation(
                adapter,
                step,
                response_body,
                captures,
                observation,
            )
        )
    return observations


def _configured_state_observation(
    adapter: _ApiAdapter,
    step: _ApiStep,
    response_body: object,
    captures: Mapping[str, object],
    observation: StateObservation,
) -> list[dict[str, object]]:
    common = {
        "name": observation.name,
        "role": observation.role.value,
        "preferred_direction": (
            observation.preferred_direction.value
            if observation.preferred_direction is not None
            else None
        ),
        "success_values": observation.success_values,
        "failure_values": observation.failure_values,
        "evidence_refs": observation.evidence_refs,
    }
    return [
        *_bound_state_observation(
            adapter,
            step,
            response_body,
            captures,
            phase="baseline",
            selector=observation.baseline_selector,
            **common,
        ),
        *_bound_state_observation(
            adapter,
            step,
            response_body,
            captures,
            phase="result",
            selector=observation.result_selector,
            **common,
        ),
    ]


def _bound_state_observation(
    adapter: _ApiAdapter,
    step: _ApiStep,
    response_body: object,
    captures: Mapping[str, object],
    *,
    name: str,
    role: str,
    phase: str,
    selector: StateSelector,
    preferred_direction: str | None = None,
    success_values: Sequence[object] = (),
    failure_values: Sequence[object] = (),
    evidence_refs: Sequence[str] = (),
) -> list[dict[str, object]]:
    if selector.step_id != step.id:
        return []
    root = response_body if selector.evaluation_root == "response_body" else captures
    binding = {
        "adapter_digest": adapter.digest,
        "compiled_adapter_digest": adapter.compiled_digest,
        "step_id": selector.step_id,
        "evaluation_root": selector.evaluation_root,
        "pointer": selector.pointer,
        "phase": phase,
    }
    try:
        selected = _select(root, selector.pointer, step.id)
    except _ResponsePointerMissing:
        selected_digest: str | None = None
        selected_value: object = None
        selected = None
    else:
        selected_digest = _digest_json(selected)
        selected_value = selected if _json_scalar(selected) else None
    result: dict[str, object] = {
        "schema": "merlin.target-state-observation.v1",
        "authority": "target",
        "name": name,
        "role": role,
        "phase": phase,
        "step_id": selector.step_id,
        "evaluation_root": selector.evaluation_root,
        "pointer": selector.pointer,
        "binding_digest": _digest_json(binding),
        "selected_digest": selected_digest,
        **(
            {"selected_value": selected_value}
            if selected_digest is not None and _json_scalar(selected)
            else {}
        ),
        **({"preferred_direction": preferred_direction} if preferred_direction is not None else {}),
        **({"success_values": list(success_values)} if success_values else {}),
        **({"failure_values": list(failure_values)} if failure_values else {}),
        **({"evidence_refs": list(evidence_refs)} if evidence_refs else {}),
    }
    return [{**result, "observation_digest": _digest_json(result)}]


def _freshness_observations(
    adapter: _ApiAdapter,
    step: _ApiStep,
    response_body: object,
    captures: Mapping[str, object],
) -> list[dict[str, object]]:
    capabilities = adapter.state_capabilities
    if capabilities is None:
        return []
    result: list[dict[str, object]] = []
    for rule in capabilities.freshness_rules:
        selector = rule.selector
        if selector.step_id != step.id:
            continue
        root = response_body if selector.evaluation_root == "response_body" else captures
        try:
            selected = _select(root, selector.pointer, step.id)
        except _ResponsePointerMissing:
            selected_digest: str | None = None
            matched: bool | None = None
        else:
            selected_digest = _digest_json(selected)
            matched = any(_typed_equal(selected, expected) for expected in rule.match_values)
        binding = {
            "step_id": selector.step_id,
            "evaluation_root": selector.evaluation_root,
            "pointer": selector.pointer,
            "match_values": rule.match_values,
        }
        observation = {
            "schema": "merlin.target-state-freshness-observation.v1",
            "authority": "target",
            **binding,
            "binding_digest": _digest_json(binding),
            "selected_digest": selected_digest,
            "matched": matched,
            **({"evidence_refs": list(rule.evidence_refs)} if rule.evidence_refs else {}),
        }
        result.append({**observation, "observation_digest": _digest_json(observation)})
    return result


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
    _augment_target_state_metadata(metadata)
    disposition = _attempt_disposition(metadata, is_error=result.is_error)
    metadata["attempt_disposition"] = disposition
    metadata["acknowledged_capability_evidence"] = disposition == "valid"
    return result.model_copy(update={"metadata": metadata})


def _augment_target_state_metadata(metadata: dict[str, object]) -> None:
    exchanges_value = metadata.get("current_target_exchanges", metadata.get("target_exchanges", ()))
    exchanges = exchanges_value if isinstance(exchanges_value, list) else []
    state_observations: list[dict[str, object]] = []
    constraint_observations: list[dict[str, object]] = []
    contaminated = bool(metadata.get("session_reused"))
    for exchange in exchanges:
        if not isinstance(exchange, Mapping):
            continue
        state_observations.extend(_mapping_list(exchange.get("state_observations")))
        constraint_observations.extend(
            _mapping_list(exchange.get("request_constraint_observations"))
        )
        contaminated = contaminated or exchange.get("attempt_disposition") == "contaminated"
    if state_observations:
        metadata["state_observations"] = state_observations
        transition = _target_state_transition(state_observations)
        if contaminated:
            transition["identity_match"] = False
            transition["outcome"] = "unclear"
        metadata["target_state_transition"] = transition
        metadata["state_identity_match"] = transition["identity_match"]
        if transition["identity_match"] is not True:
            metadata["evaluation_uncertain"] = True
            metadata["evaluation_uncertainties"] = _dedupe_uncertainties(
                [
                    *_uncertainty_records(metadata.get("evaluation_uncertainties", ())),
                    _evaluation_uncertainty(
                        "api_target.physical_state_identity",
                        (
                            "the reviewed physical-state identity was absent from the "
                            "attributable before/after observation"
                        ),
                    ),
                ]
            )
        identities = [
            item
            for item in state_observations
            if item.get("role") == "state_identity" and item.get("selected_digest") is not None
        ]
        if identities:
            metadata["physical_state_identity"] = {
                key: identities[-1][key]
                for key in (
                    "selected_digest",
                    "selected_value",
                    "step_id",
                    "evaluation_root",
                    "pointer",
                    "binding_digest",
                )
                if key in identities[-1]
            }
    if constraint_observations:
        metadata["request_constraint_observations"] = constraint_observations


def _target_state_transition(
    observations: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    identity_baselines = [
        item
        for item in observations
        if item.get("role") == "state_identity" and item.get("phase") == "baseline"
    ]
    identity_results = [
        item
        for item in observations
        if item.get("role") == "state_identity" and item.get("phase") == "result"
    ]
    baseline_digest = identity_baselines[0].get("selected_digest") if identity_baselines else None
    result_digest = identity_results[-1].get("selected_digest") if identity_results else None
    if baseline_digest is None:
        identity_match: bool | None = None
    elif identity_results:
        identity_match = result_digest is not None and baseline_digest == result_digest
    else:
        # The reviewed baseline identity is claimed before interaction and its
        # lease spans the result observation; a second selector is optional.
        identity_match = True

    by_name: dict[str, dict[str, Mapping[str, object]]] = {}
    for item in observations:
        name = item.get("name")
        phase = item.get("phase")
        if item.get("role") == "state_identity" or not isinstance(name, str):
            continue
        if phase in {"baseline", "result"}:
            by_name.setdefault(name, {})[str(phase)] = item

    compared: list[dict[str, object]] = []
    for name, phases in by_name.items():
        baseline = phases.get("baseline")
        result = phases.get("result")
        compared.append(_compare_state_observation(name, baseline, result))
    statuses = {str(item["status"]) for item in compared}
    outcome = (
        "unclear"
        if {"success", "failure"} <= statuses or {"progress", "regression"} <= statuses
        else "confirmed"
        if "success" in statuses
        else "refuted"
        if "failure" in statuses
        else "progress"
        if "progress" in statuses
        else "regression"
        if "regression" in statuses
        else "unclear"
    )
    if identity_match is not True and compared:
        outcome = "unclear"
    return {
        "authority": "target",
        "identity_match": identity_match,
        "outcome": outcome,
        "observations": compared,
    }


def _compare_state_observation(
    name: str,
    baseline: Mapping[str, object] | None,
    result: Mapping[str, object] | None,
) -> dict[str, object]:
    exemplar = result or baseline or {}
    before_digest = baseline.get("selected_digest") if baseline is not None else None
    after_digest = result.get("selected_digest") if result is not None else None
    before_value = baseline.get("selected_value") if baseline is not None else None
    after_value = result.get("selected_value") if result is not None else None
    success_values = tuple(exemplar.get("success_values", ()))
    failure_values = tuple(exemplar.get("failure_values", ()))
    preferred_direction = exemplar.get("preferred_direction")
    if after_digest is None or before_digest is None:
        status = "unclear"
    elif any(_typed_equal(after_value, expected) for expected in success_values):
        status = "success"
    elif any(_typed_equal(after_value, expected) for expected in failure_values):
        status = "failure"
    elif _typed_equal(before_value, after_value):
        status = "unchanged"
    elif (
        preferred_direction in {"increase", "decrease"}
        and isinstance(before_value, (int, float))
        and not isinstance(before_value, bool)
        and isinstance(after_value, (int, float))
        and not isinstance(after_value, bool)
    ):
        moved_preferred = (
            after_value > before_value
            if preferred_direction == "increase"
            else after_value < before_value
        )
        status = "progress" if moved_preferred else "regression"
    else:
        status = "unclear"
    return {
        "name": name,
        "role": exemplar.get("role", "custom"),
        "status": status,
        "before_digest": before_digest,
        "after_digest": after_digest,
        **({"before_value": before_value} if before_digest is not None else {}),
        **({"after_value": after_value} if after_digest is not None else {}),
        **({"preferred_direction": preferred_direction} if preferred_direction is not None else {}),
        **(
            {"baseline_binding_digest": baseline.get("binding_digest")}
            if baseline is not None
            else {}
        ),
        **({"result_binding_digest": result.get("binding_digest")} if result is not None else {}),
    }


def _attempt_disposition(metadata: Mapping[str, object], *, is_error: bool) -> str:
    exchanges_value = metadata.get("current_target_exchanges", metadata.get("target_exchanges", ()))
    exchanges = exchanges_value if isinstance(exchanges_value, list) else []
    exchange_dispositions = {
        str(item.get("attempt_disposition"))
        for item in exchanges
        if isinstance(item, Mapping) and item.get("attempt_disposition") is not None
    }
    constraints = _mapping_list(metadata.get("request_constraint_observations"))
    if "protocol_invalid" in exchange_dispositions or any(
        item.get("satisfied") is False for item in constraints
    ):
        return "protocol_invalid"
    transition = metadata.get("target_state_transition")
    if (
        "contaminated" in exchange_dispositions
        or metadata.get("session_reused") is True
        or isinstance(transition, Mapping)
        and transition.get("identity_match") is False
    ):
        return "contaminated"
    if is_error or metadata.get("infrastructure_failure_category") is not None:
        return "infrastructure_failed"
    if metadata.get("evaluation_uncertain") is True:
        return "uncertain"
    return "valid"


def _mapping_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _has_state_contract(adapter: _ApiAdapter) -> bool:
    capabilities = adapter.state_capabilities
    return capabilities is not None and (
        capabilities.identity is not None or bool(capabilities.observations)
    )


def _has_reviewed_physical_identity(adapter: _ApiAdapter) -> bool:
    capabilities = adapter.state_capabilities
    return capabilities is not None and capabilities.identity is not None


def _target_parallel_capacity(adapter: _ApiAdapter) -> int | None:
    capabilities = adapter.state_capabilities
    if capabilities is None:
        return None
    if capabilities.maximum_parallel_states is not None:
        return capabilities.maximum_parallel_states
    affordances = {item.kind.value for item in capabilities.affordances}
    if affordances & {"restore_checkpoint", "reset_state"}:
        return 1
    can_isolate = "provision_fresh_state" in affordances
    return 1 if capabilities.identity is not None and not can_isolate else None


def _reviewed_state_reuse_isolated(
    adapter: _ApiAdapter,
    exchange: Mapping[str, object],
) -> bool:
    capabilities = adapter.state_capabilities
    return bool(
        capabilities is not None
        and capabilities.identity is not None
        and capabilities.identity.result_selector is not None
        and _target_parallel_capacity(adapter) == 1
        and exchange.get("physical_state_claimed") is True
    )


def _allows_serial_state_reuse(adapter: _ApiAdapter) -> bool:
    capabilities = adapter.state_capabilities
    return (
        capabilities is not None
        and capabilities.identity is not None
        and _target_parallel_capacity(adapter) == 1
    )


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


def _select_with_text_siblings(value: object, pointer: str, step_id: str) -> object:
    """Expose every text item when a selector points into a repeated response list.

    Recon often seals the first observed chat item, for example ``/bubbles/0``
    or ``/messages/0/content``. Later turns can contain several assistant
    messages in that same list. Preserve the exact selector for captures and
    oracle evaluation, but give the worker all homogeneous sibling texts so a
    multi-message response is not silently reduced to its first item.
    """

    selected = _select(value, pointer, step_id)
    if not isinstance(selected, str) or not pointer:
        return selected

    encoded_tokens = pointer[1:].split("/")
    for index in range(len(encoded_tokens) - 1, -1, -1):
        if not encoded_tokens[index].isdigit():
            continue
        parent_pointer = "" if index == 0 else "/" + "/".join(encoded_tokens[:index])
        try:
            siblings = _select(value, parent_pointer, step_id)
        except _ResponsePointerMissing:
            continue
        if not isinstance(siblings, list) or len(siblings) < 2:
            continue

        suffix_tokens = encoded_tokens[index + 1 :]
        suffix_pointer = "" if not suffix_tokens else "/" + "/".join(suffix_tokens)
        sibling_texts: list[str] = []
        for sibling in siblings:
            try:
                sibling_value = _select(sibling, suffix_pointer, step_id)
            except _ResponsePointerMissing:
                break
            if not isinstance(sibling_value, str):
                break
            sibling_texts.append(sibling_value)
        else:
            return sibling_texts

    return selected


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


def _request_constraint_violation_result(error: _RequestConstraintViolation) -> ToolResult:
    rejected_exchange = {
        "id": error.step_id,
        "interaction": True,
        "cleanup": False,
        "request": error.request,
        "outcome": "request_constraint_violation",
        "request_constraint_observations": error.observations,
        "attempt_disposition": "protocol_invalid",
        "retryable": True,
        "acknowledged_capability_evidence": False,
        "target_call_budget_charge": 0,
        "no_target_effect": True,
        "target_effect_attempted": False,
    }
    exchanges = [*error.exchanges, rejected_exchange]
    return ToolResult(
        content=str(error),
        is_error=True,
        metadata={
            "failure_code": "request_constraint_violation",
            "attempt_disposition": "protocol_invalid",
            "retryable": True,
            "acknowledged_capability_evidence": False,
            "no_target_effect": True,
            "target_effect_attempted": False,
            "target_call_budget_charge": 0,
            "limit_interaction_count": 0,
            "request_constraint_observations": error.observations,
            "target_exchanges": exchanges,
            "current_target_exchanges": exchanges,
        },
    )


def _target_execution_busy_result(state: CampaignApiSession) -> ToolResult:
    return ToolResult(
        content=(
            "reviewed target state is currently leased by another assignment; "
            "retry this candidate after that attributable execution completes"
        ),
        is_error=True,
        metadata={
            "campaign_id": state.campaign_id,
            "target_profile_id": state.target_id,
            "target_session_id": state.target_session_id,
            "target_session_generation": state.generation,
            "session_scope_id": state.session_scope_id,
            "failure_code": "target_execution_lease_unavailable",
            "attempt_disposition": "infrastructure_failed",
            "retryable": True,
            "acknowledged_capability_evidence": False,
            "infrastructure_failure_category": (
                InfrastructureFailureCategory.DELIVERY_FAILURE.value
            ),
            "no_target_effect": True,
            "target_effect_attempted": False,
            "target_call_budget_charge": 0,
            "limit_interaction_count": 0,
        },
    )


def _physical_state_unavailable_result(
    state: CampaignApiSession,
    exchanges: Sequence[Mapping[str, object]],
    *,
    reason: str,
) -> ToolResult:
    current_exchanges = [dict(item) for item in exchanges]
    target_call_budget_charge = sum(
        int(exchange.get("target_call_budget_charge", 0)) for exchange in current_exchanges
    )
    return ToolResult(
        content=f"{reason}; retry after isolated target state is available",
        is_error=True,
        metadata={
            "campaign_id": state.campaign_id,
            "target_profile_id": state.target_id,
            "target_session_id": state.target_session_id,
            "target_session_generation": state.generation,
            "session_scope_id": state.session_scope_id,
            "failure_code": "physical_state_unavailable",
            "attempt_disposition": "contaminated",
            "retryable": True,
            "acknowledged_capability_evidence": False,
            "target_effect_attempted": target_call_budget_charge > 0,
            "target_call_budget_charge": target_call_budget_charge,
            "limit_interaction_count": 0,
            "target_exchanges": current_exchanges,
            "current_target_exchanges": current_exchanges,
        },
    )


def _target_execution_lease_lost_result(result: ToolResult) -> ToolResult:
    metadata = dict(result.metadata)
    exchanges = _mapping_list(
        metadata.get("current_target_exchanges", metadata.get("target_exchanges", ()))
    )
    uncertainty = _evaluation_uncertainty(
        "api_target.target_execution_lease",
        (
            "the durable target execution lease was lost before the attributable "
            "interaction completed; retained telemetry is contaminated and retryable"
        ),
    )
    for exchange in exchanges:
        exchange["attempt_disposition"] = "contaminated"
        exchange["retryable"] = True
        exchange["acknowledged_capability_evidence"] = False
        exchange["evaluation_uncertain"] = True
        exchange.setdefault("evaluation_uncertainties", []).append(uncertainty)
    if "current_target_exchanges" in metadata:
        metadata["current_target_exchanges"] = exchanges
    if "target_exchanges" in metadata:
        metadata["target_exchanges"] = exchanges
    metadata.update(
        {
            "attempt_disposition": "contaminated",
            "retryable": True,
            "acknowledged_capability_evidence": False,
            "evaluation_uncertain": True,
            "evaluation_uncertainties": _dedupe_uncertainties(
                [*_uncertainty_records(metadata.get("evaluation_uncertainties", ())), uncertainty]
            ),
        }
    )
    return ToolResult(content=result.content, is_error=result.is_error, metadata=metadata)


def _target_execution_claimant_id(
    campaign_id: str,
    session_scope_id: str,
    input_text: str,
) -> str:
    nonce = f"{campaign_id}\0{session_scope_id}\0{input_text}\0{time.time_ns()}".encode()
    return "target-execution:" + hashlib.sha256(nonce).hexdigest()


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
    protocol_invalid = current.get("attempt_disposition") == "protocol_invalid"
    return ToolResult(
        content=(
            f"reviewed target delivery returned HTTP {status_code}; the structured response "
            "proved a request constraint violation"
            if protocol_invalid
            else f"reviewed target delivery returned HTTP {status_code}; the response was "
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
            "failure_code": (
                "request_constraint_violation" if protocol_invalid else "target_delivery_rejected"
            ),
            "target_delivery_rejected": not protocol_invalid,
            "session_health": "unknown",
            "retryable": True,
            **(
                {
                    "attempt_disposition": "protocol_invalid",
                    "disposition_reason": (
                        "structured target rejection proved the rendered request exceeded "
                        "a reviewed limit"
                    ),
                    "no_target_effect": True,
                }
                if protocol_invalid
                else {
                    "infrastructure_failure_category": (
                        InfrastructureFailureCategory.DELIVERY_FAILURE.value
                    )
                }
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
