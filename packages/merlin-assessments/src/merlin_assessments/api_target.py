from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from merlin_contracts import (
    InfrastructureFailureCategory,
    ToolContext,
    ToolInteractionRole,
    ToolResult,
    ToolSpec,
    build_state_enumeration,
    build_state_observation,
    evaluate_objective_gate_predicate,
    evaluate_state_transition,
    executable_adapter_digest,
)
from merlin_contracts.request_bindings import RequestValueGenerator, bind_request_values
from merlin_contracts.state_isolation import resolve_state_isolation
from merlin_contracts.state_scope import StateScopeActionKind
from merlin_contracts.state_scope_runtime import (
    CheckpointVault,
    ObjectiveCheckpointStore,
    StateScopeLifecycle,
)
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
    ResourceControl,
    ResourceOwnership,
    StateCapabilities,
    StateCheckpoint,
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


class _TargetResourceBlocked(ValueError):
    """A shared principal resource denied delivery until its durable wake time."""

    def __init__(self, status: Mapping[str, object]) -> None:
        self.status = dict(status)
        self.exchanges: list[dict[str, object]] = []
        super().__init__("reviewed target resource is unavailable before delivery")

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


@dataclass(slots=True)
class _StateScopeLease:
    claimant_id: str
    lease_seconds: int
    lifecycle: StateScopeLifecycle
    claim_id: str | None = None
    equivalence_key: str | None = None
    principal_key: str | None = None
    checkpoint_restore_gate_id: str | None = None
    checkpoint_receipt: Mapping[str, object] | None = None
    pending_checkpoint_secret: str | None = None
    pending_checkpoint_exchange_ref: str | None = None
    checkpoint_restore_secret: str | None = None


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
        checkpoint_store: ObjectiveCheckpointStore | None = None,
        checkpoint_vault: CheckpointVault | None = None,
        timeout_seconds: float = 30,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("API target timeout must be positive")
        self._sessions = sessions
        self._adapter_resolver = adapter_resolver
        self._credentials = credentials
        self._browser_api_requester = browser_api_requester
        self._checkpoint_store = checkpoint_store
        self._checkpoint_vault = checkpoint_vault
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
                    "step_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Execute the named reviewed adapter step. Used by sealed objective "
                            "phase gates; omitted calls retain the ordinary interaction path."
                        ),
                    },
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
        requested_step_id = arguments.get("step_id")
        if requested_step_id is not None and (
            not isinstance(requested_step_id, str) or not requested_step_id.strip()
        ):
            return ToolResult(content="step_id must be a non-empty string", is_error=True)
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
        if requested_step_id is not None and requested_step_id not in {
            step.id for step in adapter.steps if not step.cleanup
        }:
            return resolved(
                ToolResult(
                    content=f"reviewed adapter step {requested_step_id!r} is unavailable",
                    is_error=True,
                    metadata={"infrastructure_failure_category": "configuration"},
                )
            )

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
                            requested_step_id,
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
                except _TargetResourceBlocked as error:
                    return resolved(
                        _target_resource_blocked_result(None, error.status, error.exchanges)
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
        requested_step_id: str | None,
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
        lifecycle = await StateScopeLifecycle.create(
            self._sessions,
            campaign_id=context.campaign_id,
            target_authority=_target_authority(adapter),
            attempt_id=session_scope_id,
            action_id=claimant_id,
            candidate_id=context.agent_id,
            experiment_id=str(
                context.technique_attempt_id
                or context.attempt_id
                or context.agent_id
                or session_scope_id
            ),
            action_kind=(
                StateScopeActionKind(context.state_action_kind)
                if context.state_action_kind is not None
                else StateScopeActionKind.CANDIDATE
            ),
        )
        state_scope_lease = _StateScopeLease(
            claimant_id=claimant_id,
            lease_seconds=lease_seconds,
            lifecycle=lifecycle,
        )
        scope_claimed = await self._claim_state_scope_lease(
            state_scope_lease,
            adapter,
            equivalence_key="conservative-shared-state",
        )
        if not scope_claimed:
            await asyncio.to_thread(self._sessions.release_target_execution, claim_id, claimant_id)
            await lifecycle.pause_unclaimed(
                isolation_disposition=("shared_serialized" if parallel_capacity == 1 else "unknown")
            )
            return _physical_state_unavailable_result(
                state,
                [],
                reason="the reviewed physical state is already owned by another execution",
            )
        owner = asyncio.current_task()
        heartbeat = asyncio.create_task(
            self._renew_target_execution_lease(
                claim_id,
                claimant_id,
                lease_seconds,
                owner,
                state_scope_lease,
            )
        )
        try:
            try:
                result = await self._invoke_under_execution_lease(
                    context,
                    text,
                    requested_step_id,
                    pointer_miss_retries,
                    session_scope_id,
                    adapter,
                    state,
                    state_scope_lease,
                )
            except BaseException:
                await self._release_checkpoint_restore(context, state_scope_lease)
                if lifecycle.acknowledged:
                    await lifecycle.settle(
                        before_refs=(),
                        action_refs=(),
                        response_refs=(),
                        after_refs=(),
                        transition_disposition="infrastructure_failure",
                        isolation_disposition=(
                            "shared_serialized" if parallel_capacity == 1 else "unknown"
                        ),
                        cleanup_disposition="pending",
                    )
                else:
                    await lifecycle.pause(
                        reason="transport_failed",
                        retryable=True,
                        isolation_disposition=(
                            "shared_serialized" if parallel_capacity == 1 else "unknown"
                        ),
                    )
                raise
            await self._release_checkpoint_restore(context, state_scope_lease)
            await _finish_state_scope_lifecycle(
                lifecycle,
                result,
                adapter=adapter,
                parallel_capacity=parallel_capacity,
            )
            still_owned = await asyncio.to_thread(
                self._sessions.validate_target_execution, claim_id, claimant_id
            )
            if state_scope_lease.claim_id is not None:
                still_owned = still_owned and await asyncio.to_thread(
                    self._sessions.validate_state_scope_lease,
                    state_scope_lease.claim_id,
                    claimant_id,
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
            if state_scope_lease.claim_id is not None:
                await asyncio.to_thread(
                    self._sessions.release_state_scope_lease,
                    state_scope_lease.claim_id,
                    claimant_id,
                )
            await asyncio.to_thread(self._sessions.release_target_execution, claim_id, claimant_id)

    async def _claim_state_scope_lease(
        self,
        lease: _StateScopeLease,
        adapter: _ApiAdapter,
        *,
        equivalence_key: str,
    ) -> bool:
        principal_key = lease.lifecycle.principal_key
        if (
            lease.claim_id is not None
            and lease.equivalence_key == equivalence_key
            and lease.principal_key == principal_key
        ):
            return True
        if lease.claim_id is not None:
            rekey = getattr(self._sessions, "rekey_state_scope_lease", None)
            if callable(rekey):
                changed = await asyncio.to_thread(
                    rekey,
                    lease.claim_id,
                    lease.claimant_id,
                    principal_key=principal_key,
                    equivalence_key=equivalence_key,
                )
                if not changed:
                    return False
                lease.equivalence_key = equivalence_key
                lease.principal_key = principal_key
                return True
            await asyncio.to_thread(
                self._sessions.release_state_scope_lease,
                lease.claim_id,
                lease.claimant_id,
            )
            lease.claim_id = None
            lease.equivalence_key = None
            lease.principal_key = None
            lease.lifecycle.unbind_lease()
        claim_id = await asyncio.to_thread(
            self._sessions.claim_state_scope_lease,
            lease.lifecycle.scope_id,
            target_authority=_target_authority(adapter),
            principal_key=principal_key,
            equivalence_key=equivalence_key,
            claimant_id=lease.claimant_id,
            lease_seconds=lease.lease_seconds,
        )
        if claim_id is None:
            await lease.lifecycle.pause_unclaimed(isolation_disposition="unknown")
            return False
        lease.claim_id = claim_id
        lease.equivalence_key = equivalence_key
        lease.principal_key = principal_key
        lease.lifecycle.bind_lease(claim_id, lease.claimant_id)
        return True

    async def _renew_target_execution_lease(
        self,
        claim_id: str,
        claimant_id: str,
        lease_seconds: int,
        owner: asyncio.Task[object] | None,
        state_scope_lease: _StateScopeLease | None = None,
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
            if state_scope_lease is not None and state_scope_lease.claim_id is not None:
                renewed = await asyncio.to_thread(
                    self._sessions.renew_state_scope_lease,
                    state_scope_lease.claim_id,
                    state_scope_lease.claimant_id,
                    state_scope_lease.lease_seconds,
                )
                if not renewed:
                    if owner is not None:
                        owner.cancel()
                    return

    async def _invoke_under_execution_lease(
        self,
        context: ToolContext,
        text: str,
        requested_step_id: str | None,
        pointer_miss_retries: int,
        session_scope_id: str,
        adapter: _ApiAdapter,
        state: CampaignApiSession,
        state_scope_lease: _StateScopeLease,
    ) -> ToolResult:
        variables = dict(state.variables)
        checkpoint = _objective_checkpoint_contract(context, adapter)
        if context.objective_phase_checkpoint_intent is not None:
            self._checkpoint_dependencies()
        if checkpoint is not None and context.objective_phase_checkpoint_intent == "restore":
            state_scope_lease.checkpoint_restore_secret = await self._claim_checkpoint_restore(
                context,
                state_scope_lease,
            )
            variables[checkpoint.binding_name] = state_scope_lease.checkpoint_restore_secret
        resource_block = await self._preflight_resource_block(adapter, variables, state_scope_lease)
        if resource_block is not None:
            return _target_resource_blocked_result(state, resource_block, ())
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
        isolation_plan = resolve_state_isolation(adapter.state_capabilities)
        isolation_affordance_steps = {
            item.step_id
            for item in (
                adapter.state_capabilities.affordances
                if adapter.state_capabilities is not None
                else ()
            )
            if item.kind.value in {"provision_fresh_state", "restore_checkpoint", "clone_state"}
        }
        selected_step = next(
            (step for step in adapter.steps if step.id == requested_step_id),
            None,
        )
        setup_observed: list[object] = []
        for step in adapter.steps:
            if (
                step.cleanup
                or step.interaction
                or step.id in completed
                and step.id != requested_step_id
            ):
                continue
            if (
                context.objective_phase_checkpoint_intent is None
                and isolation_plan.affordance_step_id is not None
                and step.id in isolation_affordance_steps
                and step.id != isolation_plan.affordance_step_id
            ):
                continue
            if checkpoint is not None and (
                step.id == checkpoint.restore_step_id
                and context.objective_phase_checkpoint_intent == "capture"
                or step.id == checkpoint.capture_selector.step_id
                and context.objective_phase_checkpoint_intent == "restore"
            ):
                continue
            item, variables, exchange = await self._execute(
                context.campaign_id,
                adapter,
                step,
                variables,
                session_scope_id=session_scope_id,
                input_text=text,
                state_scope_lease=state_scope_lease,
                secret_request_pointer=(
                    checkpoint.restore_request_pointer
                    if checkpoint is not None
                    and context.objective_phase_checkpoint_intent == "restore"
                    and step.id == checkpoint.restore_step_id
                    else None
                ),
            )
            if state_scope_lease.checkpoint_restore_secret is not None:
                _redact_checkpoint_capture(
                    exchange,
                    state_scope_lease.checkpoint_restore_secret,
                )
            exchanges.append(exchange)
            if selected_step is not None and step.id == selected_step.id:
                setup_observed.append(item)
            if 200 <= int(exchange["status_code"]) <= 299 and checkpoint is not None:
                if (
                    context.objective_phase_checkpoint_intent == "capture"
                    and isinstance(variables.get(checkpoint.binding_name), str)
                    and variables[checkpoint.binding_name]
                ):
                    secret = str(variables[checkpoint.binding_name])
                    state_scope_lease.pending_checkpoint_secret = secret
                    if (
                        checkpoint.capture_timing == "live_handle"
                        and step.id == checkpoint.capture_selector.step_id
                    ):
                        state_scope_lease.pending_checkpoint_exchange_ref = _target_exchange_ref(
                            exchange
                        )
                    _redact_checkpoint_capture(exchanges, secret)
                    variables.pop(checkpoint.binding_name, None)
                elif (
                    context.objective_phase_checkpoint_intent == "restore"
                    and step.id == checkpoint.restore_step_id
                ):
                    await state_scope_lease.lifecycle.acknowledge(
                        before_refs=(),
                        action_refs=(_target_exchange_ref(exchange),),
                    )
                    state_scope_lease.checkpoint_receipt = await self._complete_checkpoint_restore(
                        context,
                        exchange,
                        state_scope_lease,
                    )
                    variables.pop(checkpoint.binding_name, None)
            if (
                checkpoint is not None
                and context.objective_phase_checkpoint_intent == "restore"
                and step.id == checkpoint.restore_step_id
            ):
                variables.pop(checkpoint.binding_name, None)
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
                    state_scope_lease=state_scope_lease,
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
            if (
                selected_step is not None
                and not selected_step.interaction
                and step.id == selected_step.id
            ):
                break
        observed: list[object] = []
        session_terminal = False
        session_restart_used = False
        for step in adapter.steps:
            if not step.interaction:
                continue
            if selected_step is not None and step.id != selected_step.id:
                continue
            repeat_index = 0
            while repeat_index < step.repeat:
                if (
                    checkpoint is not None
                    and state_scope_lease.checkpoint_restore_secret is not None
                ):
                    variables[checkpoint.binding_name] = state_scope_lease.checkpoint_restore_secret
                elif (
                    checkpoint is not None
                    and context.objective_phase_checkpoint_intent == "capture"
                    and state_scope_lease.pending_checkpoint_secret is not None
                ):
                    variables[checkpoint.binding_name] = state_scope_lease.pending_checkpoint_secret
                await state_scope_lease.lifecycle.ensure_authoritative_before(
                    _state_observation_refs(exchanges, phase="baseline")
                )
                try:
                    item, variables, exchange = await self._execute(
                        context.campaign_id,
                        adapter,
                        step,
                        variables,
                        session_scope_id=session_scope_id,
                        input_text=text,
                        resource_attempt_id=context.call_id,
                        state_scope_lease=state_scope_lease,
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
                except _TargetResourceBlocked as error:
                    error.include_completed_exchanges(exchanges)
                    raise
                if state_scope_lease.checkpoint_restore_secret is not None:
                    _redact_checkpoint_capture(
                        exchange,
                        state_scope_lease.checkpoint_restore_secret,
                    )
                exchanges.append(exchange)
                if 200 <= int(exchange["status_code"]) <= 299:
                    await state_scope_lease.lifecycle.acknowledge(
                        before_refs=_state_observation_refs(exchanges, phase="baseline"),
                        action_refs=(_target_exchange_ref(exchange),),
                    )
                    if (
                        checkpoint is not None
                        and context.objective_phase_checkpoint_intent == "capture"
                        and step.id == checkpoint.capture_selector.step_id
                    ):
                        source = (
                            exchange.get("response_body")
                            if checkpoint.capture_selector.evaluation_root == "response_body"
                            else variables
                        )
                        secret = _select(
                            source,
                            checkpoint.capture_selector.pointer,
                            checkpoint.capture_selector.step_id,
                        )
                        if not isinstance(secret, str) or not secret:
                            raise ValueError(
                                "reviewed checkpoint selector did not yield a non-empty string"
                            )
                        state_scope_lease.pending_checkpoint_exchange_ref = _target_exchange_ref(
                            exchange
                        )
                        state_scope_lease.pending_checkpoint_secret = secret
                        _redact_checkpoint_capture(exchanges, secret)
                        variables.pop(checkpoint.binding_name, None)
                if checkpoint is not None:
                    variables.pop(checkpoint.binding_name, None)
                state = await asyncio.to_thread(
                    self._sessions.finish_step,
                    context.campaign_id,
                    variables,
                    target_id=adapter.target_id,
                    session_scope_id=session_scope_id,
                )
                variables = dict(state.variables)
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
                            state_scope_lease=state_scope_lease,
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
        if selected_step is not None and not selected_step.interaction:
            if context.objective_phase_gate_contract is not None:
                for exchange in exchanges:
                    if exchange.get("id") != selected_step.id:
                        continue
                    observations = _mapping_list(exchange.get("state_observations"))
                    result_names = {
                        str(item.get("name"))
                        for item in observations
                        if item.get("phase") == "result"
                    }
                    projected_results: list[dict[str, object]] = []
                    for observation in observations:
                        if (
                            observation.get("phase") != "baseline"
                            or str(observation.get("name")) in result_names
                        ):
                            continue
                        projected = {**observation, "phase": "result"}
                        projected.pop("observation_digest", None)
                        projected_results.append(
                            {
                                **projected,
                                "observation_digest": _digest_json(projected),
                            }
                        )
                    if projected_results:
                        exchange["state_observations"] = [
                            *observations,
                            *projected_results,
                        ]
            observed.extend(setup_observed)
        if not observed:
            raise ValueError(
                "reviewed API adapter did not execute the requested step"
                if requested_step_id is not None
                else "reviewed API adapter has no interaction step"
            )
        if state_scope_lease.pending_checkpoint_secret is not None:
            if _objective_checkpoint_gate_succeeded(
                context,
                adapter,
                exchanges,
                session_reused=session_reused,
            ):
                state_scope_lease.checkpoint_receipt = await self._capture_checkpoint(
                    context,
                    state_scope_lease.pending_checkpoint_secret,
                    state_scope_lease.pending_checkpoint_exchange_ref,
                    state_scope_lease,
                )
            state_scope_lease.pending_checkpoint_secret = None
            state_scope_lease.pending_checkpoint_exchange_ref = None
        state_scope_lease.checkpoint_restore_secret = None
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
        if state_scope_lease.checkpoint_receipt is not None:
            metadata["objective_phase_checkpoint"] = dict(state_scope_lease.checkpoint_receipt)
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

    async def _claim_checkpoint_restore(
        self,
        context: ToolContext,
        state_scope_lease: _StateScopeLease,
    ) -> str:
        store, vault = self._checkpoint_dependencies()
        assert context.campaign_id is not None
        assert context.objective_phase_gate_id is not None
        assert context.objective_phase_gate_recipe_digest is not None
        assert context.objective_phase_manifest_digest is not None
        assert context.objective_phase_checkpoint_reference is not None
        claim_id = state_scope_lease.claim_id
        if claim_id is None:
            raise RuntimeError("checkpoint restore requires an active state-scope lease")
        row = await asyncio.to_thread(
            store.claim_objective_gate_checkpoint_restore,
            context.campaign_id,
            context.objective_phase_gate_id,
            scope_id=state_scope_lease.lifecycle.scope_id,
            claim_id=claim_id,
            claimant_id=state_scope_lease.claimant_id,
        )
        durable_reference = row.get("checkpoint_handle")
        if (
            row.get("manifest_digest") != context.objective_phase_manifest_digest
            or row.get("recipe_digest") != context.objective_phase_gate_recipe_digest
            or durable_reference != context.objective_phase_checkpoint_reference
        ):
            await asyncio.to_thread(
                store.release_objective_gate_checkpoint_restore,
                context.campaign_id,
                context.objective_phase_gate_id,
                scope_id=state_scope_lease.lifecycle.scope_id,
                claim_id=claim_id,
                claimant_id=state_scope_lease.claimant_id,
            )
            raise ValueError("durable checkpoint does not match the sealed restore contract")
        reference_id = context.objective_phase_checkpoint_reference.get("reference_id")
        if not isinstance(reference_id, str) or not reference_id:
            raise ValueError("sealed checkpoint reference is invalid")
        state_scope_lease.checkpoint_restore_gate_id = context.objective_phase_gate_id
        return await asyncio.to_thread(vault.resolve_checkpoint, reference_id)

    async def _capture_checkpoint(
        self,
        context: ToolContext,
        secret: str,
        exchange_ref: str | None,
        state_scope_lease: _StateScopeLease,
    ) -> Mapping[str, object]:
        store, vault = self._checkpoint_dependencies()
        if exchange_ref is None:
            raise RuntimeError("checkpoint capture is missing its acknowledged exchange")
        assert context.campaign_id is not None
        assert context.objective_phase_gate_id is not None
        assert context.objective_phase_gate_recipe_digest is not None
        assert context.objective_phase_manifest_digest is not None
        entry = await asyncio.to_thread(
            vault.store_checkpoint,
            secret,
            campaign_id=context.campaign_id,
            gate_id=context.objective_phase_gate_id,
        )
        evidence_digest = _checkpoint_evidence_digest(exchange_ref, entry.digest)
        claim_id = state_scope_lease.claim_id
        if claim_id is None:
            raise RuntimeError("checkpoint capture requires an active state-scope lease")
        reference = dict(entry.opaque_reference())
        await asyncio.to_thread(
            store.record_objective_gate_checkpoint,
            context.campaign_id,
            context.objective_phase_gate_id,
            manifest_digest=context.objective_phase_manifest_digest,
            recipe_digest=context.objective_phase_gate_recipe_digest,
            checkpoint_handle=reference,
            evidence_digest=evidence_digest,
            scope_id=state_scope_lease.lifecycle.scope_id,
            claim_id=claim_id,
            claimant_id=state_scope_lease.claimant_id,
        )
        return {
            "schema": "merlin.objective-phase-checkpoint.v1",
            "gate_id": context.objective_phase_gate_id,
            "recipe_digest": context.objective_phase_gate_recipe_digest,
            "checkpoint_reference": reference,
            "evidence_digest": evidence_digest,
            "captured": True,
        }

    async def _complete_checkpoint_restore(
        self,
        context: ToolContext,
        exchange: Mapping[str, object],
        state_scope_lease: _StateScopeLease,
    ) -> Mapping[str, object]:
        store, _ = self._checkpoint_dependencies()
        assert context.campaign_id is not None
        assert context.objective_phase_gate_id is not None
        assert context.objective_phase_gate_recipe_digest is not None
        assert context.objective_phase_checkpoint_reference is not None
        claim_id = state_scope_lease.claim_id
        if claim_id is None:
            raise RuntimeError("checkpoint restore requires an active state-scope lease")
        evidence_digest = _checkpoint_evidence_digest(_target_exchange_ref(exchange), claim_id)
        await asyncio.to_thread(
            store.mark_objective_gate_checkpoint_restored,
            context.campaign_id,
            context.objective_phase_gate_id,
            scope_id=state_scope_lease.lifecycle.scope_id,
            claim_id=claim_id,
            claimant_id=state_scope_lease.claimant_id,
            evidence_digest=evidence_digest,
        )
        state_scope_lease.checkpoint_restore_gate_id = None
        return {
            "schema": "merlin.objective-phase-checkpoint.v1",
            "gate_id": context.objective_phase_gate_id,
            "recipe_digest": context.objective_phase_gate_recipe_digest,
            "checkpoint_reference": dict(context.objective_phase_checkpoint_reference),
            "evidence_digest": evidence_digest,
            "restored": True,
        }

    def _checkpoint_dependencies(self) -> tuple[ObjectiveCheckpointStore, CheckpointVault]:
        if self._checkpoint_store is None or self._checkpoint_vault is None:
            raise RuntimeError("objective checkpoint storage is unavailable")
        return self._checkpoint_store, self._checkpoint_vault

    async def _release_checkpoint_restore(
        self,
        context: ToolContext,
        state_scope_lease: _StateScopeLease,
    ) -> None:
        gate_id = state_scope_lease.checkpoint_restore_gate_id
        claim_id = state_scope_lease.claim_id
        if gate_id is None or claim_id is None or context.campaign_id is None:
            return
        store, _ = self._checkpoint_dependencies()
        await asyncio.to_thread(
            store.release_objective_gate_checkpoint_restore,
            context.campaign_id,
            gate_id,
            scope_id=state_scope_lease.lifecycle.scope_id,
            claim_id=claim_id,
            claimant_id=state_scope_lease.claimant_id,
        )
        state_scope_lease.checkpoint_restore_gate_id = None

    async def _preflight_resource_block(
        self,
        adapter: _ApiAdapter,
        variables: Mapping[str, object],
        state_scope_lease: _StateScopeLease,
    ) -> Mapping[str, object] | None:
        status_reader = getattr(self._sessions, "resource_status", None)
        if not callable(status_reader):
            return None
        for control, ownership in _resource_controls(adapter):
            retained = variables.get("__principal_identities")
            retained_identity = (
                retained.get(ownership.name) if isinstance(retained, Mapping) else None
            )
            if state_scope_lease.lifecycle.principal_identity is None and isinstance(
                retained_identity, Mapping
            ):
                state_scope_lease.lifecycle.bind_safe_identities(
                    principal_identity=retained_identity
                )
                equivalence_key = state_scope_lease.equivalence_key or "conservative-shared-state"
                if not await self._claim_state_scope_lease(
                    state_scope_lease,
                    adapter,
                    equivalence_key=equivalence_key,
                ):
                    return {
                        "state": "principal_lease_conflict",
                        "retryable": True,
                    }
            try:
                principal_key = _resource_principal_key(
                    ownership, variables, state_scope_lease.lifecycle
                )
            except _TargetResourceBlocked:
                principal_identity = await self._prior_resource_principal_identity(
                    adapter,
                    ownership,
                    campaign_id=state_scope_lease.lifecycle.campaign_id,
                )
                if principal_identity is None:
                    # A target with no prior reviewed binding still has to learn
                    # its principal from the exact setup selector. Reservation
                    # remains the authoritative pre-interaction check.
                    continue
                state_scope_lease.lifecycle.bind_safe_identities(
                    principal_identity=principal_identity
                )
                equivalence_key = state_scope_lease.equivalence_key or "conservative-shared-state"
                if not await self._claim_state_scope_lease(
                    state_scope_lease,
                    adapter,
                    equivalence_key=equivalence_key,
                ):
                    return {
                        "state": "principal_lease_conflict",
                        "retryable": True,
                    }
                principal_key = state_scope_lease.lifecycle.principal_key
            status = await asyncio.to_thread(
                status_reader,
                _target_authority(adapter),
                principal_key,
                ownership.resource_kind.value,
                control.window_key,
            )
            if isinstance(status, Mapping) and _resource_status_blocks(status):
                return status
        return None

    async def _prior_resource_principal_identity(
        self,
        adapter: _ApiAdapter,
        ownership: ResourceOwnership,
        *,
        campaign_id: str,
    ) -> Mapping[str, object] | None:
        """Reuse one unambiguous reviewed campaign principal before target setup."""

        list_sessions = getattr(self._sessions, "list_for_campaign", None)
        if not callable(list_sessions):
            return None
        sessions = await asyncio.to_thread(list_sessions, campaign_id)
        identities: dict[str, Mapping[str, object]] = {}
        for session in sessions:
            if (
                session.target_id != adapter.target_id
                or session.adapter_digest != adapter.compiled_digest
            ):
                continue
            retained = session.variables.get("__principal_identities")
            if not isinstance(retained, Mapping):
                continue
            identity = retained.get(ownership.name)
            if not isinstance(identity, Mapping):
                continue
            try:
                key = _secret_safe_identity_key(identity)
            except ValueError:
                continue
            identities[key] = dict(identity)
        if len(identities) != 1:
            return None
        return next(iter(identities.values()))

    async def _reserve_step_resources(
        self,
        campaign_id: str,
        adapter: _ApiAdapter,
        step: _ApiStep,
        *,
        session_scope_id: str,
        attempt_id: str,
        variables: Mapping[str, object],
        state_scope_lease: _StateScopeLease,
    ) -> list[tuple[str, ResourceControl, ResourceOwnership]]:
        reserve = getattr(self._sessions, "reserve_resource", None)
        release = getattr(self._sessions, "release_resource", None)
        if not callable(reserve):
            return []
        reservations: list[tuple[str, ResourceControl, ResourceOwnership]] = []
        for control, ownership in _resource_controls(adapter, step_id=step.id):
            reservation_id = _resource_reservation_id(
                campaign_id,
                session_scope_id,
                attempt_id,
                ownership.name,
                step.id,
            )
            status = await asyncio.to_thread(
                reserve,
                state_scope_lease.lifecycle.scope_id,
                reservation_id,
                target_authority=_target_authority(adapter),
                principal_key=_resource_principal_key(
                    ownership, variables, state_scope_lease.lifecycle
                ),
                resource_kind=ownership.resource_kind.value,
                amount=control.reservation_amount,
                limit=control.limit,
                window_key=control.window_key,
                claim_id=state_scope_lease.claim_id,
                claimant_id=state_scope_lease.claimant_id,
            )
            if not isinstance(status, Mapping) or status.get("admitted") is not True:
                if callable(release):
                    for active_id, _, _ in reservations:
                        await asyncio.to_thread(
                            release,
                            active_id,
                            claim_id=state_scope_lease.claim_id,
                            claimant_id=state_scope_lease.claimant_id,
                        )
                raise _TargetResourceBlocked(
                    status if isinstance(status, Mapping) else {"state": "unavailable"}
                )
            reservations.append((reservation_id, control, ownership))
        return reservations

    async def _settle_step_resources(
        self,
        reservations: Sequence[tuple[str, ResourceControl, ResourceOwnership]],
        *,
        actual_calls: int,
        blocks: Mapping[str, Mapping[str, object]],
        state_scope_lease: _StateScopeLease | None,
    ) -> None:
        settle = getattr(self._sessions, "settle_resource", None)
        if not callable(settle):
            return
        if reservations and state_scope_lease is None:
            raise RuntimeError("resource settlement requires an active state-scope lease")
        for reservation_id, control, ownership in reservations:
            assert state_scope_lease is not None
            block = blocks.get(ownership.name)
            await asyncio.to_thread(
                settle,
                reservation_id,
                actual_amount=actual_calls * control.reservation_amount,
                terminal_block=block is not None,
                wake_at=None if block is None else block.get("wake_at"),
                evidence=None if block is None else block.get("evidence"),
                claim_id=state_scope_lease.claim_id,
                claimant_id=state_scope_lease.claimant_id,
            )

    async def _acknowledge_step_resources(
        self,
        reservations: Sequence[tuple[str, ResourceControl, ResourceOwnership]],
        *,
        state_scope_lease: _StateScopeLease | None,
    ) -> None:
        acknowledge = getattr(self._sessions, "acknowledge_resource", None)
        if not callable(acknowledge):
            return
        if reservations and state_scope_lease is None:
            raise RuntimeError("resource acknowledgement requires an active state-scope lease")
        for reservation_id, _, _ in reservations:
            assert state_scope_lease is not None
            await asyncio.to_thread(
                acknowledge,
                reservation_id,
                claim_id=state_scope_lease.claim_id,
                claimant_id=state_scope_lease.claimant_id,
            )

    async def _restart_session_locked(
        self,
        campaign_id: str,
        adapter: _ApiAdapter,
        variables: dict[str, object],
        *,
        session_scope_id: str,
        exchanges: list[dict[str, object]],
        state_scope_lease: _StateScopeLease,
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
                state_scope_lease=state_scope_lease,
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
                    state_scope_lease=state_scope_lease,
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
        state_scope_lease: _StateScopeLease,
    ) -> ToolResult | None:
        capabilities = adapter.state_capabilities
        isolation_plan = resolve_state_isolation(capabilities)
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
        safe_bindings = exchange.get("safe_state_bindings")
        safe_bindings = safe_bindings if isinstance(safe_bindings, Mapping) else {}
        physical = safe_bindings.get("physical_identity")
        physical_identity = dict(physical) if isinstance(physical, Mapping) else None
        equivalence_identities = [
            dict(item["identity"])
            for item in _mapping_values(safe_bindings.get("identity_equivalence"))
            if item.get("side") == "left" and isinstance(item.get("identity"), Mapping)
        ]
        lease_identity = equivalence_identities[0] if equivalence_identities else physical_identity
        if identity is None or lease_identity is None:
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
            if isolation_plan.isolated:
                return _physical_state_unavailable_result(
                    state,
                    exchanges,
                    reason=(
                        "the reviewed physical-state identity was absent before isolated "
                        "candidate delivery"
                    ),
                )
            return None
        if isolation_plan.isolated:
            if not _exchange_proves_fresh_state(adapter, exchange):
                exchange["attempt_disposition"] = "contaminated"
                exchange["retryable"] = True
                exchange["acknowledged_capability_evidence"] = False
                exchange["evaluation_uncertain"] = True
                exchange.setdefault("evaluation_uncertainties", []).append(
                    _evaluation_uncertainty(
                        "api_target.physical_state_freshness",
                        (
                            "the setup response did not prove an independent fresh physical "
                            "state, so the candidate was not delivered"
                        ),
                    )
                )
                return _physical_state_unavailable_result(
                    state,
                    exchanges,
                    reason="the reviewed independent-state freshness proof did not match",
                )
            scope_claimed = await self._claim_state_scope_lease(
                state_scope_lease,
                adapter,
                equivalence_key=_secret_safe_identity_key(lease_identity),
            )
            if not scope_claimed:
                exchange["physical_state_alias_detected"] = True
                exchange["attempt_disposition"] = "contaminated"
                exchange["retryable"] = True
                exchange["acknowledged_capability_evidence"] = False
                return _physical_state_unavailable_result(
                    state,
                    exchanges,
                    reason=("the reviewed physical state is leased by another target execution"),
                )
            exchange["global_physical_state_lease"] = True
        state_scope_lease.lifecycle.bind_safe_identities(physical_state_identity=lease_identity)
        claim_key = "physical-state:" + _secret_safe_identity_key(lease_identity)
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
        resource_attempt_id: str | None = None,
        state_scope_lease: _StateScopeLease | None = None,
        secret_request_pointer: str | None = None,
    ) -> tuple[object, dict[str, object], dict[str, object]]:
        request = bind_request_values(
            _render(step.request, variables, input_text), step.request_bindings
        )
        recorded_request = (
            _redacted_json_pointer_copy(request, secret_request_pointer)
            if secret_request_pointer is not None
            else request
        )
        checkpoint_transport_secret = (
            None
            if state_scope_lease is None
            else state_scope_lease.checkpoint_restore_secret
            or state_scope_lease.pending_checkpoint_secret
        )
        if checkpoint_transport_secret is not None:
            if recorded_request is request:
                recorded_request = deepcopy(request)
            _redact_checkpoint_capture(
                recorded_request,
                checkpoint_transport_secret,
            )
        request_constraint_observations = _request_constraint_observations(adapter, step, request)
        protocol_invalid = any(
            item.get("satisfied") is False for item in request_constraint_observations
        )
        if protocol_invalid:
            raise _RequestConstraintViolation(
                step.id,
                recorded_request,
                request_constraint_observations,
            )
        resource_reservations = (
            await self._reserve_step_resources(
                campaign_id,
                adapter,
                step,
                session_scope_id=session_scope_id,
                attempt_id=resource_attempt_id,
                variables=variables,
                state_scope_lease=state_scope_lease,
            )
            if (
                step.interaction
                and resource_attempt_id is not None
                and state_scope_lease is not None
            )
            else []
        )
        await asyncio.to_thread(
            self._sessions.begin_step,
            campaign_id,
            _pending_step(step, recorded_request),
            target_id=adapter.target_id,
            session_scope_id=session_scope_id,
        )
        await self._acknowledge_step_resources(
            resource_reservations,
            state_scope_lease=state_scope_lease,
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
        ) as error:
            await self._settle_step_resources(
                resource_reservations,
                actual_calls=(
                    error.target_call_budget_charge
                    if isinstance(error, _TargetTransportFailure)
                    else 1
                ),
                blocks={},
                state_scope_lease=state_scope_lease,
            )
            await asyncio.to_thread(
                self._sessions.mark_ambiguous,
                campaign_id,
                target_id=adapter.target_id,
                session_scope_id=session_scope_id,
            )
            raise
        latency_ms = max(0, int((time.monotonic() - started) * 1_000))
        resource_blocks = _structured_resource_blocks(adapter, step, status_code, response_json)
        await self._settle_step_resources(
            resource_reservations,
            actual_calls=attempt_count,
            blocks=resource_blocks,
            state_scope_lease=state_scope_lease,
        )
        evaluation_uncertainties = list(step.evaluation_uncertainties)
        response_state_observations = _state_observations(adapter, step, response_json, variables)
        response_state_enumerations = _state_enumerations(adapter, step, response_json, variables)
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
                    "request": recorded_request,
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
                    **(
                        {
                            "resource_blocks": {
                                name: dict(block["evidence"])
                                for name, block in resource_blocks.items()
                            }
                        }
                        if resource_blocks
                        else {}
                    ),
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
                        {"state_enumerations": response_state_enumerations}
                        if response_state_enumerations
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
                        "request": recorded_request,
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
        safe_state_bindings = await self._safe_state_bindings(
            adapter, step, response_json, variables
        )
        if state_scope_lease is not None and safe_state_bindings:
            await self._adopt_safe_state_bindings(adapter, state_scope_lease, safe_state_bindings)
        equivalence_mismatch = any(
            item.get("matched") is False
            for item in _mapping_values(safe_state_bindings.get("identity_equivalence"))
        )
        freshness_observations = _freshness_observations(adapter, step, response_json, variables)
        freshness_unproven = any(item.get("matched") is not True for item in freshness_observations)
        oracle_observations = _oracle_observation(adapter, step, response_json)
        state_observations = _state_observations(adapter, step, response_json, variables)
        state_enumerations = _state_enumerations(adapter, step, response_json, variables)
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
            "request": recorded_request,
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
            **({"state_enumerations": state_enumerations} if state_enumerations else {}),
            **({"safe_state_bindings": safe_state_bindings} if safe_state_bindings else {}),
            **(
                {
                    "attempt_disposition": "contaminated",
                    "retryable": True,
                    "acknowledged_capability_evidence": False,
                    "evaluation_uncertain": True,
                    "identity_equivalence_mismatch": True,
                }
                if equivalence_mismatch
                else {}
            ),
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

    async def _safe_state_bindings(
        self,
        adapter: _ApiAdapter,
        step: _ApiStep,
        response_body: object,
        captures: dict[str, object],
    ) -> dict[str, object]:
        """Evaluate reviewed identity selectors and retain only keyed digests."""

        capabilities = adapter.state_capabilities
        vault = self._checkpoint_vault
        if capabilities is None or vault is None:
            return {}
        principals: dict[str, Mapping[str, str]] = {}
        for ownership in capabilities.resource_ownership:
            selector = ownership.principal_selector
            if selector.step_id != step.id:
                continue
            selected = _select_state_binding(selector, step, response_body, captures)
            if selected is None:
                continue
            principals[ownership.name] = await asyncio.to_thread(
                vault.digest_identity,
                selected,
                purpose=f"principal:{_target_authority(adapter)}:{ownership.name}",
            )
        if principals:
            retained = captures.setdefault("__principal_identities", {})
            if not isinstance(retained, dict):
                retained = {}
                captures["__principal_identities"] = retained
            retained.update({name: dict(identity) for name, identity in principals.items()})
        physical_identity: Mapping[str, str] | None = None
        if capabilities.identity is not None:
            selectors = (
                capabilities.identity.baseline_selector,
                capabilities.identity.result_selector,
            )
            for selector in selectors:
                if selector is None or selector.step_id != step.id:
                    continue
                selected = _select_state_binding(selector, step, response_body, captures)
                if selected is not None:
                    physical_identity = await asyncio.to_thread(
                        vault.digest_identity,
                        selected,
                        purpose=f"physical-state:{_target_authority(adapter)}",
                    )
                    break
        equivalence: dict[str, dict[str, object]] = {}
        durable = captures.setdefault("__identity_equivalence", {})
        if not isinstance(durable, dict):
            durable = {}
            captures["__identity_equivalence"] = durable
        for rule in capabilities.identity_equivalence_rules:
            for side, selector in (("left", rule.left_selector), ("right", rule.right_selector)):
                if selector.step_id != step.id:
                    continue
                selected = _select_state_binding(selector, step, response_body, captures)
                if selected is None:
                    equivalence[rule.name] = {"side": side, "available": False}
                    continue
                identity = await asyncio.to_thread(
                    vault.digest_identity,
                    selected,
                    purpose=f"physical-equivalence:{_target_authority(adapter)}:{rule.name}",
                )
                prior = durable.get(rule.name)
                matched = None
                if side == "left":
                    durable[rule.name] = dict(identity)
                elif isinstance(prior, Mapping):
                    matched = prior.get("digest") == identity.get("digest")
                equivalence[rule.name] = {
                    "side": side,
                    "available": True,
                    "identity": dict(identity),
                    "matched": matched,
                }
        return {
            **({"principals": principals} if principals else {}),
            **(
                {"physical_identity": dict(physical_identity)}
                if physical_identity is not None
                else {}
            ),
            **({"identity_equivalence": equivalence} if equivalence else {}),
        }

    async def _adopt_safe_state_bindings(
        self,
        adapter: _ApiAdapter,
        lease: _StateScopeLease,
        bindings: Mapping[str, object],
    ) -> None:
        principals = bindings.get("principals")
        principal_identity: Mapping[str, object] | None = None
        if isinstance(principals, Mapping) and principals:
            identities = [
                dict(value) for _, value in sorted(principals.items()) if isinstance(value, Mapping)
            ]
            if len(identities) == 1:
                principal_identity = identities[0]
            elif identities and self._checkpoint_vault is not None:
                principal_identity = await asyncio.to_thread(
                    self._checkpoint_vault.digest_identity,
                    identities,
                    purpose=f"principal-set:{_target_authority(adapter)}",
                )
        physical = bindings.get("physical_identity")
        physical_identity = dict(physical) if isinstance(physical, Mapping) else None
        lease.lifecycle.bind_safe_identities(
            principal_identity=principal_identity,
            physical_state_identity=physical_identity,
        )
        if principal_identity is not None and lease.equivalence_key is not None:
            if not await self._claim_state_scope_lease(
                lease, adapter, equivalence_key=lease.equivalence_key
            ):
                raise _TargetResourceBlocked({"state": "principal_lease_conflict"})

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


def _state_enumerations(
    adapter: _ApiAdapter,
    step: _ApiStep,
    response_body: object,
    captures: Mapping[str, object],
) -> list[dict[str, object]]:
    """Extract reviewed finite denominators from the attributable target response."""

    capabilities = adapter.state_capabilities
    if capabilities is None:
        return []
    receipts: list[dict[str, object]] = []
    for declaration in capabilities.enumerations:
        if declaration.selector.step_id != step.id:
            continue
        selected = _select_state_binding(
            declaration.selector,
            step,
            response_body,
            captures,
        )
        if selected is None:
            continue
        try:
            receipt = build_state_enumeration(
                declaration,
                selected_value=selected,
                binding_context={
                    "adapter_digest": adapter.digest,
                    "compiled_adapter_digest": adapter.compiled_digest,
                    "target_id": adapter.target_id,
                    "step_id": step.id,
                    "evaluation_root": declaration.selector.evaluation_root,
                    "pointer": declaration.selector.pointer,
                },
            )
        except ValueError:
            # A reviewed selector whose target value no longer matches its declared
            # shape cannot create denominator authority. The raw response remains
            # available to reconnaissance and adapter review.
            continue
        receipts.append(receipt.model_dump(mode="json"))
    return receipts


def _select_state_binding(
    selector: StateSelector,
    step: _ApiStep,
    response_body: object,
    captures: Mapping[str, object],
) -> object | None:
    root = response_body if selector.evaluation_root == "response_body" else captures
    try:
        return _select(root, selector.pointer, step.id)
    except _ResponsePointerMissing:
        return None


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
        "value_type": observation.value_type.value if observation.value_type is not None else None,
        "minimum_value": observation.minimum_value,
        "maximum_value": observation.maximum_value,
        "monotonicity": (
            observation.monotonicity.value if observation.monotonicity is not None else None
        ),
        "trust_class": (
            observation.trust_class.value if observation.trust_class is not None else None
        ),
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
    value_type: str | None = None,
    minimum_value: int | float | None = None,
    maximum_value: int | float | None = None,
    monotonicity: str | None = None,
    trust_class: str | None = None,
) -> list[dict[str, object]]:
    if selector.step_id != step.id:
        return []
    root = response_body if selector.evaluation_root == "response_body" else captures
    binding_context = {
        "adapter_digest": adapter.digest,
        "compiled_adapter_digest": adapter.compiled_digest,
    }
    try:
        selected = _select(root, selector.pointer, step.id)
    except _ResponsePointerMissing:
        selected_available = False
        selected = None
    else:
        selected_available = True
    result = build_state_observation(
        name=name,
        role=role,
        phase=phase,
        step_id=selector.step_id,
        evaluation_root=selector.evaluation_root,
        pointer=selector.pointer,
        binding_context=binding_context,
        selected_value=selected,
        selected_available=selected_available,
        preferred_direction=preferred_direction,
        success_values=success_values,
        failure_values=failure_values,
        evidence_refs=evidence_refs,
        value_type=value_type,
        minimum_value=minimum_value,
        maximum_value=maximum_value,
        monotonicity=monotonicity,
        trust_class=trust_class,
    )
    return [result]


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
    capabilities = adapter.state_capabilities
    if capabilities is not None and capabilities.corroboration_rules:
        metadata["state_corroboration_rules"] = [
            item.model_dump(mode="json") for item in capabilities.corroboration_rules
        ]
    _augment_target_state_metadata(metadata)
    disposition = _attempt_disposition(metadata, is_error=result.is_error)
    metadata["attempt_disposition"] = disposition
    metadata["acknowledged_capability_evidence"] = disposition == "valid"
    return _secret_safe_result_projection(result.model_copy(update={"metadata": metadata}))


def _augment_target_state_metadata(metadata: dict[str, object]) -> None:
    exchanges_value = metadata.get("current_target_exchanges", metadata.get("target_exchanges", ()))
    exchanges = exchanges_value if isinstance(exchanges_value, list) else []
    state_observations: list[dict[str, object]] = []
    state_enumerations: list[dict[str, object]] = []
    constraint_observations: list[dict[str, object]] = []
    equivalence_matches: list[bool] = []
    contaminated = bool(metadata.get("session_reused"))
    for exchange in exchanges:
        if not isinstance(exchange, Mapping):
            continue
        state_observations.extend(_mapping_list(exchange.get("state_observations")))
        state_enumerations.extend(_mapping_list(exchange.get("state_enumerations")))
        constraint_observations.extend(
            _mapping_list(exchange.get("request_constraint_observations"))
        )
        safe_bindings = exchange.get("safe_state_bindings")
        if isinstance(safe_bindings, Mapping):
            equivalence_matches.extend(
                bool(item["matched"])
                for item in _mapping_values(safe_bindings.get("identity_equivalence"))
                if isinstance(item.get("matched"), bool)
            )
        contaminated = contaminated or exchange.get("attempt_disposition") == "contaminated"
    if state_enumerations:
        metadata["state_enumerations"] = state_enumerations
    if state_observations:
        metadata["state_observations"] = state_observations
        transition = _target_state_transition(
            state_observations,
            _mapping_list(metadata.get("state_corroboration_rules")),
        )
        if equivalence_matches and all(equivalence_matches):
            transition["identity_match"] = True
            transition["identity_equivalence_proven"] = True
        elif any(match is False for match in equivalence_matches):
            contaminated = True
        if contaminated:
            transition["identity_match"] = False
            transition["outcome"] = "unclear"
        metadata["target_state_transition"] = transition
        metadata["state_identity_match"] = transition["identity_match"]
        if (
            transition["identity_match"] is not True
            or transition["outcome"] == "unclear"
            or transition.get("conflict") is True
        ):
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
    corroboration_rules: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    return evaluate_state_transition(observations, corroboration_rules)


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


def _target_exchange_ref(exchange: Mapping[str, object]) -> str:
    safe_exchange, _ = _secret_safe_exchange_projection(exchange)
    return "target-exchange:" + _digest_json(safe_exchange).removeprefix("sha256:")


def _secret_safe_result_projection(result: ToolResult) -> ToolResult:
    """Remove target-issued identities and full wire bodies from the public effect."""

    metadata = deepcopy(dict(result.metadata))
    identity_replacements: dict[str, str] = {}
    physical_identity: Mapping[str, object] | None = None
    projected_exchanges: dict[str, list[dict[str, object]]] = {}
    for key in ("target_exchanges", "current_target_exchanges"):
        value = metadata.get(key)
        if not isinstance(value, (list, tuple)):
            continue
        safe_rows: list[dict[str, object]] = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            safe, replacements = _secret_safe_exchange_projection(item)
            safe_rows.append(safe)
            identity_replacements.update(replacements)
            bindings = safe.get("safe_state_bindings")
            if isinstance(bindings, Mapping):
                candidate = bindings.get("physical_identity")
                if isinstance(candidate, Mapping):
                    physical_identity = dict(candidate)
        projected_exchanges[key] = safe_rows

    remote_identity = metadata.pop("remote_target_session_id", None)
    if isinstance(remote_identity, str) and remote_identity:
        identity_replacements.setdefault(remote_identity, "[target-identity-redacted]")
        identity_replacements.setdefault(
            _digest_json(remote_identity),
            "sha256:" + hashlib.sha256(b"secret-safe-target-identity").hexdigest(),
        )
        metadata["remote_target_session"] = {
            "kind": "opaque_reference",
            "namespace": "state",
            "reference_id": str(metadata.get("target_session_id", "unavailable")),
        }
    metadata.pop("remote_session_id_sha256", None)
    metadata.pop("physical_state_identity", None)
    metadata = _replace_sensitive_identities(metadata, identity_replacements)
    if physical_identity is not None:
        metadata["physical_state_identity"] = dict(physical_identity)
    metadata.update(projected_exchanges)
    content = str(result.content)
    for secret, replacement in sorted(
        identity_replacements.items(), key=lambda item: len(item[0]), reverse=True
    ):
        content = content.replace(secret, replacement)
    return result.model_copy(update={"content": content, "metadata": metadata})


def _secret_safe_exchange_projection(
    exchange: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, str]]:
    """Return the durable/model-safe summary for one completed wire exchange."""

    safe = deepcopy(dict(exchange))
    replacements: dict[str, str] = {}
    physical = None
    bindings = safe.get("safe_state_bindings")
    if isinstance(bindings, Mapping):
        candidate = bindings.get("physical_identity")
        if isinstance(candidate, Mapping) and isinstance(candidate.get("digest"), str):
            physical = str(candidate["digest"])
    observations = safe.get("state_observations")
    if isinstance(observations, list):
        for observation in observations:
            if not isinstance(observation, dict) or observation.get("role") != "state_identity":
                continue
            selected = observation.pop("selected_value", None)
            replacement_digest = (
                "sha256:" + physical
                if physical is not None
                else "sha256:" + hashlib.sha256(b"redacted-target-identity").hexdigest()
            )
            if selected is not None:
                if isinstance(selected, str) and selected:
                    replacements[selected] = "[target-identity-redacted]"
                replacements[_digest_json(selected)] = replacement_digest
            observation["selected_digest"] = replacement_digest
            unsigned = {
                key: value for key, value in observation.items() if key != "observation_digest"
            }
            observation["observation_digest"] = _digest_json(unsigned)
    request_projection = _constraint_request_projection(safe)
    for payload_field in ("request", "response", "response_body"):
        if payload_field not in safe:
            continue
        safe[f"{payload_field}_summary"] = _payload_summary(safe.pop(payload_field))
    if request_projection is not None:
        safe["request"] = request_projection
    safe.pop("response_digest", None)
    safe = _replace_sensitive_identities(safe, replacements)
    return safe, replacements


def _constraint_request_projection(exchange: Mapping[str, object]) -> object | None:
    """Retain only reviewed invalid payload fields needed for bounded regeneration."""

    if exchange.get("outcome") != "request_constraint_violation":
        return None
    request = exchange.get("request")
    observations = exchange.get("request_constraint_observations")
    if not isinstance(observations, (list, tuple)):
        return None
    projection: dict[str, object] = {}
    for observation in observations:
        if not isinstance(observation, Mapping) or observation.get("satisfied") is not False:
            continue
        path = observation.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            continue
        try:
            selected = _select(request, path, "constraint-projection")
        except _ResponsePointerMissing:
            continue
        target = projection
        parts = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
        for part in parts[:-1]:
            child = target.setdefault(part, {})
            if not isinstance(child, dict):
                break
            target = child
        else:
            if parts:
                target[parts[-1]] = selected
    return projection or None


def _payload_summary(value: object) -> dict[str, object]:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    summary: dict[str, object] = {
        "kind": (
            "object"
            if isinstance(value, Mapping)
            else "array"
            if isinstance(value, (list, tuple))
            else type(value).__name__
        ),
        "utf8_bytes": len(encoded.encode("utf-8")),
    }
    if isinstance(value, Mapping):
        summary["fields"] = sorted(str(key) for key in value)
        summary["item_count"] = len(value)
    elif isinstance(value, (list, tuple)):
        summary["item_count"] = len(value)
    return summary


def _replace_sensitive_identities(value: object, replacements: Mapping[str, str]) -> object:
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, list):
        return [_replace_sensitive_identities(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_sensitive_identities(item, replacements) for item in value)
    if isinstance(value, Mapping):
        omitted = {"selected_value"} if value.get("role") == "state_identity" else set()
        return {
            str(key): _replace_sensitive_identities(item, replacements)
            for key, item in value.items()
            if key not in omitted
        }
    return value


def _state_observation_refs(
    exchanges: Sequence[Mapping[str, object]],
    *,
    phase: str,
) -> tuple[str, ...]:
    return tuple(
        str(observation["observation_digest"])
        for exchange in exchanges
        for observation in _mapping_list(exchange.get("state_observations"))
        if observation.get("phase") == phase
        and isinstance(observation.get("observation_digest"), str)
    )


async def _finish_state_scope_lifecycle(
    lifecycle: StateScopeLifecycle,
    result: ToolResult,
    *,
    adapter: _ApiAdapter,
    parallel_capacity: int,
) -> None:
    exchange_values = result.metadata.get("current_target_exchanges", ())
    if not isinstance(exchange_values, (list, tuple)):
        exchange_values = ()
    exchanges = [dict(item) for item in exchange_values if isinstance(item, Mapping)]
    interaction_exchanges = [item for item in exchanges if item.get("interaction") is True]
    action_refs = tuple(_target_exchange_ref(item) for item in interaction_exchanges)
    before_refs = _state_observation_refs(exchanges, phase="baseline")
    after_refs = _state_observation_refs(exchanges, phase="result")
    if not lifecycle.acknowledged:
        await lifecycle.pause(
            before_refs=before_refs,
            action_refs=action_refs,
            reason=("target_omitted" if exchanges else "not_yet_observed"),
            retryable=bool(result.metadata.get("retryable")),
            isolation_disposition=(
                "contaminated"
                if any(item.get("attempt_disposition") == "contaminated" for item in exchanges)
                else "shared_serialized"
                if parallel_capacity == 1
                else "unknown"
            ),
        )
        return
    projected = dict(result.metadata)
    capabilities = adapter.state_capabilities
    if capabilities is not None and capabilities.corroboration_rules:
        projected["state_corroboration_rules"] = [
            item.model_dump(mode="json") for item in capabilities.corroboration_rules
        ]
    _augment_target_state_metadata(projected)
    transition = projected.get("target_state_transition")
    outcome = transition.get("outcome") if isinstance(transition, Mapping) else None
    transition_disposition = {
        "confirmed": "terminal_success",
        "refuted": "terminal_failure",
        "progress": "progress",
        "regression": "regression",
        "unchanged": "unchanged",
    }.get(str(outcome), "ambiguous")
    if result.metadata.get("infrastructure_failure_category") is not None:
        transition_disposition = "infrastructure_failure"
    contaminated = any(item.get("attempt_disposition") == "contaminated" for item in exchanges)
    isolation_plan = resolve_state_isolation(adapter.state_capabilities)
    isolation_disposition = (
        "contaminated"
        if contaminated
        else isolation_plan.isolation_disposition
        if isolation_plan.isolated
        else "shared_serialized"
        if parallel_capacity == 1
        else "unknown"
    )
    await lifecycle.settle(
        before_refs=before_refs,
        action_refs=action_refs,
        response_refs=action_refs,
        after_refs=after_refs,
        after_missing_reason=("selector_unavailable" if _has_state_contract(adapter) else None),
        transition_disposition=transition_disposition,
        isolation_disposition=isolation_disposition,
        cleanup_disposition=(
            "pending" if any(step.cleanup for step in adapter.steps) else "not_required"
        ),
    )


def _has_state_contract(adapter: _ApiAdapter) -> bool:
    capabilities = adapter.state_capabilities
    return capabilities is not None and (
        capabilities.identity is not None or bool(capabilities.observations)
    )


def _has_reviewed_physical_identity(adapter: _ApiAdapter) -> bool:
    capabilities = adapter.state_capabilities
    return capabilities is not None and capabilities.identity is not None


def _target_authority(adapter: _ApiAdapter) -> str:
    parsed = urlsplit(adapter.endpoint)
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    default_port = (parsed.scheme == "http" and parsed.port == 80) or (
        parsed.scheme == "https" and parsed.port == 443
    )
    port = "" if parsed.port is None or default_port else f":{parsed.port}"
    return f"{parsed.scheme.lower()}://{hostname.lower()}{port}"


def _resource_principal_key(
    ownership: ResourceOwnership,
    variables: Mapping[str, object],
    lifecycle: StateScopeLifecycle,
) -> str:
    identities = variables.get("__principal_identities")
    if isinstance(identities, Mapping):
        identity = identities.get(ownership.name)
        if isinstance(identity, Mapping):
            return _secret_safe_identity_key(identity)
    if lifecycle.principal_identity is not None:
        return lifecycle.principal_key
    raise _TargetResourceBlocked(
        {
            "state": "principal_unavailable",
            "ownership_name": ownership.name,
            "retryable": True,
        }
    )


def _secret_safe_identity_key(identity: Mapping[str, object]) -> str:
    if identity.get("kind") == "keyed_digest":
        return ":".join(
            (
                "keyed_digest",
                str(identity.get("algorithm", "")),
                str(identity.get("key_id", "")),
                str(identity.get("digest", "")),
            )
        )
    if identity.get("kind") == "opaque_reference":
        return ":".join(
            (
                "opaque_reference",
                str(identity.get("namespace", "")),
                str(identity.get("reference_id", "")),
            )
        )
    raise ValueError("reviewed identity is not secret-safe")


def _mapping_values(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Mapping):
        return []
    return [item for item in value.values() if isinstance(item, Mapping)]


def _resource_controls(
    adapter: _ApiAdapter,
    *,
    step_id: str | None = None,
) -> list[tuple[ResourceControl, ResourceOwnership]]:
    capabilities = adapter.state_capabilities
    if capabilities is None:
        return []
    ownership = {item.name: item for item in capabilities.resource_ownership}
    return [
        (control, ownership[control.ownership_name])
        for control in capabilities.resource_controls
        if (step_id is None or control.step_id == step_id) and control.ownership_name in ownership
    ]


def _resource_reservation_id(
    campaign_id: str,
    session_scope_id: str,
    attempt_id: str,
    ownership_name: str,
    step_id: str,
) -> str:
    value = "\0".join((campaign_id, session_scope_id, attempt_id, ownership_name, step_id)).encode()
    return "target-resource:" + hashlib.sha256(value).hexdigest()


def _resource_status_blocks(status: Mapping[str, object]) -> bool:
    if status.get("admitted") is False or status.get("available") is False:
        return True
    return status.get("state") in {"suspended", "exhausted", "blocked"}


def _structured_resource_blocks(
    adapter: _ApiAdapter,
    step: _ApiStep,
    status_code: int,
    response_body: object,
) -> dict[str, Mapping[str, object]]:
    now = datetime.now(UTC)
    result: dict[str, Mapping[str, object]] = {}
    for control, ownership in _resource_controls(adapter, step_id=step.id):
        for signal in control.structured_blocks:
            if status_code not in signal.status_codes:
                continue
            try:
                if signal.retry_after_seconds_pointer is not None:
                    raw_wake = _select(
                        response_body,
                        signal.retry_after_seconds_pointer,
                        step.id,
                    )
                    if isinstance(raw_wake, bool) or not isinstance(raw_wake, (int, float)):
                        continue
                    wake_at = now + timedelta(seconds=max(0.0, float(raw_wake)))
                else:
                    assert signal.wake_at_pointer is not None
                    raw_wake = _select(response_body, signal.wake_at_pointer, step.id)
                    if not isinstance(raw_wake, str):
                        continue
                    wake_at = datetime.fromisoformat(raw_wake.replace("Z", "+00:00"))
                    if wake_at.tzinfo is None:
                        continue
                    wake_at = wake_at.astimezone(UTC)
            except (_ResponsePointerMissing, ValueError):
                continue
            if wake_at <= now:
                continue
            evidence = {
                "schema": "merlin.target-resource-block.v1",
                "ownership_name": ownership.name,
                "resource_kind": ownership.resource_kind.value,
                "step_id": step.id,
                "status_code": status_code,
                "wake_at": wake_at.isoformat(),
                "response_digest": _digest_json(response_body),
                "evidence_refs": list(signal.evidence_refs),
            }
            result[ownership.name] = {
                "wake_at": wake_at,
                "evidence": evidence,
            }
            break
    return result


def _exchange_proves_fresh_state(
    adapter: _ApiAdapter,
    exchange: Mapping[str, object],
) -> bool:
    if not _has_reviewed_independent_state_capacity(adapter):
        return False
    observations = _mapping_list(exchange.get("freshness_observations"))
    capabilities = adapter.state_capabilities
    assert capabilities is not None
    bound_rules = [
        rule for rule in capabilities.freshness_rules if rule.selector.step_id == exchange.get("id")
    ]
    return (
        bool(bound_rules)
        and len(observations) == len(bound_rules)
        and all(item.get("matched") is True for item in observations)
    )


def _target_parallel_capacity(adapter: _ApiAdapter) -> int:
    return resolve_state_isolation(adapter.state_capabilities).maximum_parallel_states


def _has_reviewed_independent_state_capacity(adapter: _ApiAdapter) -> bool:
    return resolve_state_isolation(adapter.state_capabilities).isolated


def _reviewed_state_reuse_isolated(
    adapter: _ApiAdapter,
    exchange: Mapping[str, object],
) -> bool:
    capabilities = adapter.state_capabilities
    return bool(
        capabilities is not None
        and capabilities.identity is not None
        and capabilities.identity.result_selector is not None
        and not resolve_state_isolation(capabilities).isolated
        and exchange.get("physical_state_claimed") is True
    )


def _allows_serial_state_reuse(adapter: _ApiAdapter) -> bool:
    capabilities = adapter.state_capabilities
    return (
        capabilities is not None
        and capabilities.identity is not None
        and not resolve_state_isolation(capabilities).isolated
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


def _objective_checkpoint_contract(
    context: ToolContext,
    adapter: _ApiAdapter,
) -> StateCheckpoint | None:
    capabilities = adapter.state_capabilities
    checkpoint = capabilities.checkpoint if capabilities is not None else None
    intent = context.objective_phase_checkpoint_intent
    if intent is None:
        return checkpoint
    if checkpoint is None:
        raise ValueError("objective checkpoint intent requires a reviewed checkpoint contract")
    required = (
        context.campaign_id,
        context.objective_phase_gate_id,
        context.objective_phase_gate_recipe_digest,
        context.objective_phase_manifest_digest,
    )
    if any(not isinstance(value, str) or not value for value in required):
        raise ValueError("objective checkpoint intent is missing its sealed campaign binding")
    if intent == "capture" and context.objective_phase_checkpoint_reference is not None:
        raise ValueError("checkpoint capture cannot carry a restore reference")
    if intent == "restore" and not _opaque_state_reference(
        context.objective_phase_checkpoint_reference
    ):
        raise ValueError("checkpoint restore requires an opaque state reference")
    return checkpoint


def _opaque_state_reference(value: object) -> bool:
    return bool(
        isinstance(value, Mapping)
        and value.get("kind") == "opaque_reference"
        and value.get("namespace") == "state"
        and isinstance(value.get("reference_id"), str)
        and str(value["reference_id"]).strip()
    )


def _checkpoint_evidence_digest(exchange_ref: str, secret_safe_digest: str) -> str:
    encoded = json.dumps(
        {"exchange_ref": exchange_ref, "secret_safe_digest": secret_safe_digest},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _objective_checkpoint_gate_succeeded(
    context: ToolContext,
    adapter: _ApiAdapter,
    exchanges: Sequence[Mapping[str, object]],
    *,
    session_reused: bool,
) -> bool:
    if session_reused or any(
        exchange.get("evaluation_uncertain") is True
        or exchange.get("attempt_disposition")
        in {"contaminated", "protocol_invalid", "infrastructure_failed"}
        for exchange in exchanges
    ):
        return False
    gate = context.objective_phase_gate_contract
    if not isinstance(gate, Mapping):
        return False
    observations = [
        observation
        for exchange in exchanges
        for observation in _mapping_list(exchange.get("state_observations"))
    ]
    corroboration_rules = (
        [item.model_dump(mode="json") for item in adapter.state_capabilities.corroboration_rules]
        if adapter.state_capabilities is not None
        else []
    )
    transition = _target_state_transition(observations, corroboration_rules)
    return evaluate_objective_gate_predicate(gate, transition) is True


def _redacted_json_pointer_copy(value: object, pointer: str) -> object:
    redacted = deepcopy(value)
    if not pointer.startswith("/"):
        raise ValueError("checkpoint restore request pointer must be non-root")
    tokens = pointer[1:].split("/")
    current = redacted
    for index, raw_token in enumerate(tokens):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        final = index == len(tokens) - 1
        if isinstance(current, dict) and token in current:
            if final:
                current[token] = "[checkpoint-secret-redacted]"
                return redacted
            current = current[token]
            continue
        if isinstance(current, list) and token.isdigit() and int(token) < len(current):
            position = int(token)
            if final:
                current[position] = "[checkpoint-secret-redacted]"
                return redacted
            current = current[position]
            continue
        raise ValueError("checkpoint restore request pointer is unavailable")
    raise ValueError("checkpoint restore request pointer is unavailable")


def _redact_checkpoint_capture(value: object, secret: str) -> None:
    if isinstance(value, dict):
        for key, item in tuple(value.items()):
            if item == secret:
                value[key] = "[checkpoint-secret-redacted]"
            else:
                _redact_checkpoint_capture(item, secret)
    elif isinstance(value, list):
        for index, item in enumerate(tuple(value)):
            if item == secret:
                value[index] = "[checkpoint-secret-redacted]"
            else:
                _redact_checkpoint_capture(item, secret)


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


def _target_resource_blocked_result(
    state: CampaignApiSession | None,
    status: Mapping[str, object],
    exchanges: Sequence[Mapping[str, object]],
) -> ToolResult:
    current_exchanges = [dict(item) for item in exchanges]
    target_call_budget_charge = sum(
        int(item.get("target_call_budget_charge", 1))
        for item in current_exchanges
        if item.get("cleanup") is not True
    )
    wake_at = status.get("wake_at")
    if isinstance(wake_at, datetime):
        wake_at = wake_at.isoformat()
    return ToolResult(
        content="reviewed target resource is blocked until its durable wake time",
        is_error=True,
        metadata={
            **(
                {
                    "campaign_id": state.campaign_id,
                    "target_profile_id": state.target_id,
                    "target_session_id": state.target_session_id,
                    "target_session_generation": state.generation,
                    "session_scope_id": state.session_scope_id,
                }
                if state is not None
                else {}
            ),
            "failure_code": "target_resource_blocked",
            "attempt_disposition": "infrastructure_failed",
            "infrastructure_failure_category": (
                InfrastructureFailureCategory.DELIVERY_FAILURE.value
            ),
            "target_resource_blocked": True,
            "retryable": True,
            "wake_at": wake_at,
            "resource_state": status.get("state"),
            "resource_available": status.get("available"),
            "acknowledged_capability_evidence": False,
            "no_target_effect": target_call_budget_charge == 0,
            "target_effect_attempted": target_call_budget_charge > 0,
            "target_call_budget_charge": target_call_budget_charge,
            "limit_interaction_count": 0,
            "target_exchanges": current_exchanges,
            "current_target_exchanges": current_exchanges,
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
    resource_blocks = current.get("resource_blocks")
    if isinstance(resource_blocks, Mapping) and resource_blocks:
        block = next(
            (dict(value) for value in resource_blocks.values() if isinstance(value, Mapping)),
            {},
        )
        blocked = _target_resource_blocked_result(
            state,
            {
                "state": "suspended",
                "available": False,
                "wake_at": block.get("wake_at"),
            },
            current_target_exchanges,
        )
        metadata = dict(blocked.metadata)
        metadata["target_exchanges"] = target_exchanges
        metadata["resource_block_evidence"] = block
        return blocked.model_copy(update={"metadata": metadata})
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
