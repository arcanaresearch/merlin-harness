from __future__ import annotations

import asyncio
import hashlib
import json
import math
import posixpath
import re
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from inspect import isawaitable
from typing import Any, Protocol
from urllib.parse import unquote, urljoin, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from merlin_contracts import (
    StateEnumerationDeclaration,
    TargetDeliveryClass,
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
from merlin_contracts.request_bindings import bind_request_values, validate_request_bindings
from merlin_contracts.state_isolation import StateIsolationPlan, resolve_state_isolation
from merlin_contracts.state_scope import MissingEvidenceReason, StateScopeActionKind
from merlin_contracts.state_scope_runtime import StateScopeLifecycle
from merlin_contracts.target_adapter_compiler import (
    AdapterCompilation,
    AdapterCompileError,
    AdapterCompileFailureKind,
    BrowserChat,
    compile_target_adapter,
    compiled_target_adapter_digest,
    read_target_adapter,
)

from .core import BrowserBoundaryError, BrowserSession
from .models import ApiReconHandoff, BrowserAction, BrowserMetadata, BrowserObservation


def _normalized_accessible_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _reviewed_locator_identity(value: object, *, default_role: str) -> tuple[str, str] | None:
    """Parse the small, reviewed role/name locator language without fuzzy matching."""
    if not isinstance(value, str) or not value.strip():
        return None
    locator = " ".join(value.strip().split())
    patterns = (
        r"^(?P<role>button|textbox|link|checkbox|radio|combobox)\s*\[name=(?:'|\")(?P<name>.+?)(?:'|\")\]$",
        r"^(?P<role>button|textbox|link|checkbox|radio|combobox)\s+named\s+(?:'|\")(?P<name>.+?)(?:'|\")$",
        r"^(?P<role>button|textbox|link|checkbox|radio|combobox)\s+(?P<name>.+)$",
    )
    lowered = locator.casefold()
    for verb in ("click ", "press ", "fill "):
        if lowered.startswith(verb):
            locator = locator[len(verb) :].strip()
            break
    for pattern in patterns:
        match = re.fullmatch(pattern, locator, flags=re.IGNORECASE)
        if match is not None:
            return match.group("role").casefold(), match.group("name").strip(" '\"")
    return default_role, locator.strip(" '\"")


def _resolve_reviewed_element(
    observation: BrowserObservation,
    locator: object,
    *,
    default_role: str,
) -> tuple[str, str | None] | None:
    identity = _reviewed_locator_identity(locator, default_role=default_role)
    if identity is None:
        return None
    role, name = identity
    matches = [
        element
        for element in observation.elements
        if element.role.casefold() == role
        and _normalized_accessible_name(element.name) == _normalized_accessible_name(name)
    ]
    if len(matches) != 1:
        return None
    return matches[0].ref, matches[0].frame_ref


def _json_contains_exact(value: object, expected: str) -> bool:
    if isinstance(value, Mapping):
        return any(_json_contains_exact(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_json_contains_exact(item, expected) for item in value)
    return value == expected


class BrowserSessionResolver(Protocol):
    def __call__(self, session_id: str) -> BrowserSession | Awaitable[BrowserSession]: ...


class ReviewedBrowserAdapterResolver(Protocol):
    def __call__(
        self, campaign_id: str
    ) -> Mapping[str, object] | None | Awaitable[Mapping[str, object] | None]: ...


class BrowserRecorder(Protocol):
    async def save_metadata(self, metadata: BrowserMetadata) -> None: ...
    async def append_action(self, action: BrowserAction) -> None: ...
    async def append_observation(self, observation: BrowserObservation) -> None: ...
    async def record_evidence(
        self, context_id: str, evidence_ref: str, kind: str, payload: Mapping[str, Any]
    ) -> None: ...
    async def record_handoff(self, handoff: ApiReconHandoff) -> None: ...
    async def list_handoffs(
        self, target_id: str, limit: int = 20
    ) -> tuple[ApiReconHandoff, ...]: ...


def _canonical_compilation(
    adapter: Mapping[str, object],
    *,
    allowed_origins: object,
) -> tuple[AdapterCompilation, str, str]:
    """Compile once and bind the executable contract to the browser target boundary."""
    compilation = compile_target_adapter(adapter)
    executable = compilation.executable
    if isinstance(allowed_origins, (set, frozenset, list, tuple)) and allowed_origins:
        permitted = {
            f"{parts.scheme.casefold()}://{parts.netloc.casefold()}"
            for value in allowed_origins
            if (parts := urlsplit(str(value))).scheme.casefold() in {"http", "https"}
            and parts.netloc
        }
        outside = sorted(set(executable.authorized_origins) - permitted)
        if outside:
            raise AdapterCompileError(
                AdapterCompileFailureKind.UNAUTHORIZED_ORIGIN,
                "authorized_origins",
                "compiled adapter resolves outside the reviewed browser target boundary",
            )
    serialized = json.dumps(
        compilation.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return compilation, serialized, compiled_target_adapter_digest(compilation)


def _compilation_uncertainties(
    compilation: AdapterCompilation,
) -> list[dict[str, object]]:
    """Project compiler advisories as non-blocking evaluation observations."""
    return [
        {
            "kind": "evaluation_uncertain",
            "source": f"target_adapter_compiler.{advisory.path}",
            "reason": advisory.message,
            "confidence": 0.0,
            "advisory_kind": advisory.kind.value,
        }
        for advisory in compilation.advisories
    ]


def _compile_error_result(error: AdapterCompileError, *, effect: str) -> ToolResult:
    """Return an executable-contract failure without converting it into attack evidence."""
    return ToolResult(
        content=str(error),
        is_error=True,
        metadata={
            "effect": effect,
            "failure_code": error.kind.value,
            "failure_path": error.path,
            "retryable": error.kind
            not in {
                AdapterCompileFailureKind.UNAUTHORIZED_ORIGIN,
                AdapterCompileFailureKind.MALFORMED_CREDENTIAL,
            },
        },
    )


def _adapter_state_integrity_result(
    message: str,
    *,
    effect: str,
    expected: object | None = None,
    observed: object | None = None,
    identity_field: str = "compiled_adapter_digest",
    failure_code: str = "adapter_state_integrity",
) -> ToolResult:
    return ToolResult(
        content=message,
        is_error=True,
        metadata={
            "effect": effect,
            "failure_code": failure_code,
            "retryable": False,
            "effect_applied": False,
            "target_call_budget_charge": 0,
            "identity_field": identity_field,
            "expected": expected,
            "observed": observed,
        },
    )


def _repair_v1_browser_steps(adapter: Mapping[str, object]) -> dict[str, object]:
    """Hydrate the legacy click-only browser projection before canonical compilation."""
    repaired = dict(adapter)
    raw_steps = adapter.get("browser_steps")
    contract = adapter.get("interaction_contract")
    if not isinstance(raw_steps, list) or not isinstance(contract, Mapping):
        return repaired
    steps = [dict(step) if isinstance(step, Mapping) else step for step in raw_steps]
    has_fill = any(
        isinstance(step, Mapping) and step.get("tool") == "browser_fill" for step in steps
    )
    input_locator = contract.get("input_locator")
    if not has_fill and isinstance(input_locator, str) and input_locator.strip():
        steps.insert(
            0,
            {
                "step_id": "legacy-hydrated-input",
                "tool": "browser_fill",
                "arguments": {
                    "locator": input_locator.strip(),
                    "value": "{{input}}",
                },
                "interaction": True,
            },
        )
    repaired["browser_steps"] = steps
    return repaired


def _spec(
    kind: str,
    properties: dict[str, dict[str, object]],
    *,
    effect: str = "target_network",
) -> tuple[str, dict[str, dict[str, object]], str]:
    return kind, properties, effect


_SPECS = {
    "browser_open_tab": _spec("open_tab", {"url": {"type": "string"}}, effect="target_read"),
    "browser_navigate": _spec(
        "navigate",
        {"tab_ref": {"type": "string"}, "url": {"type": "string"}},
        effect="target_read",
    ),
    "browser_snapshot": (
        "snapshot",
        {"tab_ref": {"type": "string"}, "frame_ref": {"type": "string"}},
        "target_read",
    ),
    "browser_click": (
        "click",
        {
            "tab_ref": {"type": "string"},
            "frame_ref": {"type": "string"},
            "element_ref": {"type": "string"},
        },
    ),
    "browser_fill": (
        "fill",
        {
            "tab_ref": {"type": "string"},
            "frame_ref": {"type": "string"},
            "element_ref": {"type": "string"},
            "value": {"type": "string"},
            "connection_probe": {
                "type": "boolean",
                "description": (
                    "Set true only during API adapter discovery. Merlin appends a "
                    "one-time harmless marker to prove the exact request input field."
                ),
            },
        },
    ),
    "browser_press": (
        "press",
        {
            "tab_ref": {"type": "string"},
            "frame_ref": {"type": "string"},
            "element_ref": {"type": "string"},
            "value": {"type": "string", "description": "Playwright key, for example Enter"},
        },
    ),
    "browser_select": (
        "select",
        {
            "tab_ref": {"type": "string"},
            "frame_ref": {"type": "string"},
            "element_ref": {"type": "string"},
            "value": {"type": "string"},
        },
    ),
    "browser_check": (
        "check",
        {
            "tab_ref": {"type": "string"},
            "frame_ref": {"type": "string"},
            "element_ref": {"type": "string"},
        },
    ),
    "browser_uncheck": (
        "uncheck",
        {
            "tab_ref": {"type": "string"},
            "frame_ref": {"type": "string"},
            "element_ref": {"type": "string"},
        },
    ),
    "browser_hover": (
        "hover",
        {
            "tab_ref": {"type": "string"},
            "frame_ref": {"type": "string"},
            "element_ref": {"type": "string"},
        },
    ),
    "browser_wait_for_text": (
        "wait_for_text",
        {
            "tab_ref": {"type": "string"},
            "frame_ref": {"type": "string"},
            "value": {"type": "string", "description": "Visible text proving the outcome"},
            "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 30000},
        },
        "target_read",
    ),
    "browser_network_events": (
        "network_events",
        {"tab_ref": {"type": "string"}},
        "target_read",
    ),
    "browser_upload": (
        "upload",
        {
            "tab_ref": {"type": "string"},
            "element_ref": {"type": "string"},
            "frame_ref": {"type": "string"},
            "value": {"type": "string"},
        },
    ),
    "browser_close_tab": ("close_tab", {"tab_ref": {"type": "string"}}),
    "browser_release": ("release", {}),
}


def _bounded_json_pointer_schema(value: object) -> dict[str, object]:
    """Expose bounded JSON shape without returning captured scalar values."""
    key_paths: list[str] = []

    def visit(candidate: object, pointer: str = "") -> None:
        if isinstance(candidate, Mapping):
            for key, item in candidate.items():
                token = str(key).replace("~", "~0").replace("/", "~1")
                child = f"{pointer}/{token}"
                key_paths.append(child)
                visit(item, child)
        elif isinstance(candidate, list):
            for index, item in enumerate(candidate):
                child = f"{pointer}/{index}"
                key_paths.append(child)
                visit(item, child)

    visit(value)
    text_pointers = [pointer for pointer, _ in _string_scalar_paths(value)]
    return {
        "key_paths": key_paths[:32],
        "candidate_text_pointers": text_pointers[:16],
        "truncated": len(key_paths) > 32 or len(text_pointers) > 16,
    }


class BrowserTool:
    def __init__(
        self,
        name: str,
        resolver: BrowserSessionResolver,
        recorder: BrowserRecorder | None = None,
        failure_tracker: dict[tuple[str, ...], int] | None = None,
        network_seen: dict[tuple[str, ...], deque[str]] | None = None,
        state_scope_store: object | None = None,
    ) -> None:
        if name not in _SPECS:
            raise ValueError(f"unknown browser tool: {name}")
        self.name, self._resolver, self._recorder = name, resolver, recorder
        definition = _SPECS[name]
        self.effect = definition[2] if len(definition) == 3 else "target_network"
        self._failures = failure_tracker if failure_tracker is not None else {}
        self._network_seen = network_seen if network_seen is not None else {}
        self._state_scope_store = state_scope_store

    def describe(self) -> ToolSpec:
        kind, properties = _SPECS[self.name][:2]
        required = [
            key for key in properties if key not in {"frame_ref", "timeout_ms", "connection_probe"}
        ]
        return ToolSpec(
            name=self.name,
            description=f"Bounded browser {kind.replace('_', ' ')} action.",
            input_schema={
                "type": "object",
                "properties": {
                    **properties,
                    "step": {"type": "integer", "minimum": 0},
                    "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 30000},
                },
                "required": required,
                "additionalProperties": False,
            },
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        resolved = self._resolver(context.session_id)
        session = await resolved if isawaitable(resolved) else resolved
        kind, properties = _SPECS[self.name][:2]
        allowed = {*properties, "step", "timeout_ms"}
        required = {
            key for key in properties if key not in {"frame_ref", "timeout_ms", "connection_probe"}
        }
        if set(arguments) - allowed or not required <= set(arguments):
            return ToolResult(
                content=f"arguments do not match {self.name} schema",
                is_error=True,
                metadata={
                    "effect": self.effect,
                    "target_session_id": f"browser:{session.context_id}",
                },
            )
        action_value = _string(arguments.get("url", arguments.get("value")))
        evidence_refs = ["browser-action:sha256:" + uuid4().hex + uuid4().hex]
        if kind == "fill" and arguments.get("connection_probe") is True:
            nonce = secrets.token_urlsafe(24)
            base = (action_value or "Harmless API adapter discovery").strip()
            action_value = f"{base} [Merlin connection probe {nonce}]"
            evidence_refs.append(f"browser-input-probe:{nonce}")
        action = BrowserAction(
            context_id=session.context_id,
            target_id=session.bounds.target_id,
            owner=session.owner,
            kind="snapshot" if kind == "release" else kind,
            tab_ref=str(arguments.get("tab_ref", "")),
            frame_ref=_string(arguments.get("frame_ref")),
            element_ref=_string(arguments.get("element_ref")),
            value=action_value,
            timeout_ms=int(arguments.get("timeout_ms", 10_000)),
            step=int(arguments.get("step", 0)),
            evidence_refs=tuple(evidence_refs),
            redaction_status="not_needed",
        )
        actor_id = context.agent_id or f"console:{context.session_id}"
        if kind == "release":
            if session.mode == "persistent" and context.campaign_id is not None:
                return ToolResult(
                    content=(
                        "shared authenticated browser release is campaign-scoped: an "
                        "individual worker must not release it mid-campaign; the campaign "
                        "runtime releases the browser actor at terminal worker outcomes"
                    ),
                    is_error=True,
                    metadata={
                        "effect": self.effect,
                        "target": session.bounds.target_id,
                        "target_session_id": f"browser:{session.context_id}",
                        "release_rejected": True,
                        "campaign_scoped": True,
                    },
                )
            await session.release_actor(actor_id)
            return ToolResult(
                content="shared authenticated browser released",
                metadata={
                    "effect": self.effect,
                    "target": session.bounds.target_id,
                    "target_session_id": f"browser:{session.context_id}",
                },
            )
        if kind == "close_tab" and session.mode == "persistent" and context.campaign_id is not None:
            return ToolResult(
                content=(
                    "shared authenticated browser tab close is campaign-scoped: an "
                    "individual worker must not close tabs mid-campaign because closing "
                    "a tab releases the shared browser actor; the campaign runtime "
                    "releases the browser actor at terminal worker outcomes"
                ),
                is_error=True,
                metadata={
                    "effect": self.effect,
                    "target": session.bounds.target_id,
                    "target_session_id": f"browser:{session.context_id}",
                    "release_rejected": True,
                    "campaign_scoped": True,
                },
            )
        state_scope: _BrowserStateScope | None = None
        session_recon_scope = False
        before_refs: tuple[str, ...] = ()
        if self._state_scope_store is not None:
            if context.campaign_id is None:
                action_kind = _browser_action_kind(context, kind)
                if action_kind not in {
                    StateScopeActionKind.RECONNAISSANCE,
                    StateScopeActionKind.CLEANUP,
                }:
                    return ToolResult(
                        content=(
                            "candidate and checkpoint browser actions require a durable "
                            "campaign state scope; the target was not called"
                        ),
                        is_error=True,
                        metadata={
                            "effect": self.effect,
                            "failure_code": "state_scope_campaign_required",
                            "retryable": True,
                            "target_effect_attempted": False,
                            "target_call_budget_charge": 0,
                        },
                    )
                if context.session_id != session.owner.session_id:
                    return ToolResult(
                        content=(
                            "session-scoped browser reconnaissance rejected: browser session "
                            "owner does not match"
                        ),
                        is_error=True,
                        metadata={
                            "effect": self.effect,
                            "failure_code": "browser_session_owner_mismatch",
                            "retryable": False,
                            "target_effect_attempted": False,
                            "target_call_budget_charge": 0,
                        },
                    )
                if self._recorder is None:
                    return ToolResult(
                        content=(
                            "session-scoped browser reconnaissance requires durable action and "
                            "observation recording; the target was not called"
                        ),
                        is_error=True,
                        metadata={
                            "effect": self.effect,
                            "failure_code": "reconnaissance_recorder_required",
                            "retryable": True,
                            "target_effect_attempted": False,
                            "target_call_budget_charge": 0,
                        },
                    )
                session_recon_scope = True
            else:
                action_id = str(context.call_id or uuid4())
                state_scope = await _BrowserStateScope.open(
                    self._state_scope_store,
                    campaign_id=context.campaign_id,
                    target_authority=_browser_action_authority(session, action),
                    action_id=action_id,
                    candidate_id=str(context.technique_attempt_id or context.agent_id or action_id),
                    experiment_id=_browser_experiment_id(context, action_id),
                    action_kind=_browser_action_kind(context, kind),
                    equivalence_key="conservative-shared-state",
                )
            if state_scope is None:
                if not session_recon_scope:
                    return ToolResult(
                        content="reviewed browser state is already owned by another execution",
                        is_error=True,
                        metadata={
                            "effect": self.effect,
                            "failure_code": "physical_state_unavailable",
                            "retryable": True,
                            "target_effect_attempted": False,
                            "target_call_budget_charge": 0,
                        },
                    )
            if action.tab_ref and kind != "open_tab":
                try:
                    before = await session.snapshot(
                        action.tab_ref, action.frame_ref, step=action.step
                    )
                except Exception:
                    if state_scope is not None:
                        await state_scope.pause(MissingEvidenceReason.SELECTOR_UNAVAILABLE)
                    return ToolResult(
                        content="browser state could not be observed before the target action",
                        is_error=True,
                        metadata={
                            "effect": self.effect,
                            "failure_code": "state_scope_before_unavailable",
                            "retryable": True,
                            "target_effect_attempted": False,
                            "target_call_budget_charge": 0,
                        },
                    )
                before_refs = (_browser_evidence_ref("before", before),)
                if self._recorder is not None:
                    try:
                        await self._recorder.append_observation(before)
                    except BaseException:
                        if state_scope is not None:
                            await state_scope.pause(
                                MissingEvidenceReason.CAPTURE_INCOMPLETE,
                                before_refs=before_refs,
                            )
                        raise
        try:
            if kind == "fill" and await session.fill_requires_redaction(
                str(arguments.get("tab_ref", "")),
                _string(arguments.get("element_ref")),
            ):
                action = action.model_copy(update={"redaction_status": "redacted"})
        except BaseException:
            if state_scope is not None:
                await state_scope.pause(
                    MissingEvidenceReason.SELECTOR_UNAVAILABLE,
                    before_refs=before_refs,
                )
            raise
        recorded = action
        if action.redaction_status == "redacted":
            recorded = action.model_copy(update={"value": "[redacted]"})
        if state_scope is not None:
            try:
                await state_scope.lifecycle.ensure_authoritative_before(before_refs)
            except BaseException:
                await state_scope.pause(
                    MissingEvidenceReason.SELECTOR_UNAVAILABLE,
                    before_refs=before_refs,
                    action_refs=action.evidence_refs,
                )
                raise
        fingerprint = (
            action.context_id,
            context.session_id,
            actor_id,
            action.kind,
            action.tab_ref,
            action.frame_ref or "",
            action.element_ref or "",
            recorded.value or "",
        )
        if self._recorder is not None:
            try:
                await self._recorder.append_action(recorded)
            except BaseException:
                if state_scope is not None:
                    await state_scope.pause(
                        MissingEvidenceReason.CAPTURE_INCOMPLETE,
                        before_refs=before_refs,
                        action_refs=action.evidence_refs,
                    )
                raise
        if self._failures.get(fingerprint, 0) >= 2:
            if state_scope is not None:
                await state_scope.pause(
                    MissingEvidenceReason.NOT_APPLICABLE,
                    before_refs=before_refs,
                    action_refs=action.evidence_refs,
                    retryable=False,
                )
            return ToolResult(
                content=(
                    "unchanged browser action already failed twice; take a fresh snapshot, "
                    "change strategy once, or report a blocker"
                ),
                is_error=True,
                metadata={
                    "effect": self.effect,
                    "target": session.bounds.target_id,
                    "target_session_id": f"browser:{session.context_id}",
                },
            )
        try:
            if state_scope is not None:
                await state_scope.lifecycle.acknowledge(
                    before_refs=before_refs,
                    action_refs=action.evidence_refs,
                )
            async with asyncio.timeout(action.timeout_ms / 1_000):
                observation = await session.act(action, actor_id=actor_id)
        except BaseException as error:
            self._failures[fingerprint] = self._failures.get(fingerprint, 0) + 1
            if state_scope is not None:
                # The action was acknowledged before dispatch. Settle that fact
                # first so cancellation or best-effort diagnostic failures cannot
                # leave a permanently acknowledged campaign scope behind.
                await state_scope.pause(
                    MissingEvidenceReason.TRANSPORT_FAILED,
                    before_refs=before_refs,
                    action_refs=action.evidence_refs,
                )
                state_scope = None
            if not isinstance(error, Exception):
                raise
            diagnostics: dict[str, object] | None = None
            diagnostic_tab = action.tab_ref
            if not diagnostic_tab:
                open_tabs = [tab.tab_ref for tab in session.metadata().tabs if tab.state == "open"]
                diagnostic_tab = open_tabs[-1] if open_tabs else ""
            if diagnostic_tab:
                try:
                    snapshot, network, screenshot = await session.capture_failure_diagnostics(
                        diagnostic_tab,
                        action.frame_ref,
                        step=action.step,
                    )
                    diagnostics = {
                        "snapshot": snapshot.model_dump(mode="json"),
                        "network": network.model_dump(mode="json"),
                        "screenshot": screenshot,
                    }
                    if self._recorder is not None:
                        await self._recorder.record_evidence(
                            action.context_id,
                            str(screenshot["evidence_ref"]),
                            "browser_failure_screenshot",
                            screenshot,
                        )
                        await self._recorder.append_observation(snapshot)
                        await self._recorder.append_observation(network)
                except Exception as diagnostic_error:
                    diagnostics = {
                        "capture_error": str(diagnostic_error).strip()
                        or type(diagnostic_error).__name__
                    }
            if self._recorder is not None:
                await self._recorder.save_metadata(session.metadata(restart=True))
            category = (
                "browser_boundary"
                if isinstance(error, BrowserBoundaryError)
                else "browser_action_failed"
            )
            detail = str(error).strip() or type(error).__name__
            if len(detail) > 2_000:
                detail = detail[:1_997] + "..."
            return ToolResult(
                content=(
                    f"{category}: {detail}\n"
                    + (
                        "Automatic failure diagnostics:\n"
                        + json.dumps(diagnostics, sort_keys=True, separators=(",", ":"))
                        if diagnostics is not None
                        else "No live tab was available for automatic diagnostics."
                    )
                    + "\nInspect the captured screenshot, DOM snapshot, frames, disabled "
                    "controls, and redacted network events before changing strategy."
                ),
                is_error=True,
                metadata={
                    "effect": self.effect,
                    "target": session.bounds.target_id,
                    "target_session_id": f"browser:{session.context_id}",
                    "error_type": f"{type(error).__module__}.{type(error).__qualname__}",
                    "retryable": True,
                    **(
                        {"diagnostic_screenshot": diagnostics["screenshot"]}
                        if diagnostics is not None and "screenshot" in diagnostics
                        else {}
                    ),
                },
            )
        for failed in tuple(self._failures):
            if failed[:3] == (action.context_id, context.session_id, actor_id):
                del self._failures[failed]
        if kind == "close_tab":
            await session.release_actor(actor_id)
        if self._recorder is not None:
            for reference in observation.evidence_refs:
                await self._recorder.record_evidence(
                    action.context_id,
                    reference,
                    "browser_action",
                    recorded.model_dump(mode="json"),
                )
            await self._recorder.append_observation(observation)
            await self._recorder.save_metadata(session.metadata(restart=True))
        content = observation.model_dump_json()
        network_delta_metadata: dict[str, object] | None = None
        if kind == "network_events":
            try:
                network_events = json.loads(observation.text)
            except (TypeError, json.JSONDecodeError):
                network_events = []
            if isinstance(network_events, list):
                cursor_key = (
                    action.context_id,
                    context.session_id,
                    actor_id,
                    action.tab_ref,
                )
                seen = self._network_seen.setdefault(cursor_key, deque(maxlen=256))
                seen_set = set(seen)
                delta: list[object] = []
                for network_event in network_events:
                    identity = json.dumps(
                        network_event,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if identity in seen_set:
                        continue
                    seen.append(identity)
                    seen_set.add(identity)
                    delta.append(network_event)
                omitted = max(0, len(delta) - 12)
                visible_delta = delta[-12:]
                summarized_endpoints = [
                    {
                        "method": event.get("method"),
                        "url": event.get("url"),
                        "status": event.get("status"),
                        "request_schema": _bounded_json_pointer_schema(event.get("request_json")),
                        "response_schema": _bounded_json_pointer_schema(event.get("response_json")),
                    }
                    for event in delta[:-12]
                    if isinstance(event, Mapping)
                ]
                model_observation = observation.model_copy(
                    update={
                        "text": json.dumps(
                            visible_delta,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    }
                )
                content = model_observation.model_dump_json()
                if omitted:
                    content += (
                        f"\n{omitted} earlier new network event bodies were summarized; "
                        "complete evidence remains durable:\n"
                        + json.dumps(
                            summarized_endpoints,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\nUse the listed method, URL, and candidate pointers when calling "
                        "browser_compile_api_handoff; the compiler reads the durable capture."
                    )
                elif not visible_delta:
                    content += "\nNo new network events since the previous poll."
                network_delta_metadata = {
                    "new_events": len(delta),
                    "full_events_shown": len(visible_delta),
                    "endpoint_summaries_shown": len(summarized_endpoints),
                    "complete_evidence_durable": True,
                }
        metadata: dict[str, object] = {
            "effect": self.effect,
            "target": session.bounds.target_id,
            "target_session_id": f"browser:{session.context_id}",
            "browser_action": recorded.model_dump(mode="json"),
            "evidence_refs": list(observation.evidence_refs),
        }
        if network_delta_metadata is not None:
            metadata["network_delta"] = network_delta_metadata
        if kind == "network_events" and any(
            _probe_linked_api_exchange(event) for event in session.api_exchange_events()
        ):
            metadata["preferred_next_tool"] = "browser_compile_api_handoff"
            content += (
                "\nStructured API request/response evidence is now sufficient to attempt "
                "browser_compile_api_handoff; compile whenever the exchange evidence is "
                "complete. Lifecycle cleanup discovery is best-effort and never blocks "
                "compiling or assessment_prepare. When this reconnaissance context visibly "
                "created a disposable session and shows a single visible exit control, you may "
                "use browser_end_owned_session on it and compile a superseding handoff with "
                "the captured cleanup exchange. Use browser_end_owned_session only when the "
                "captured creation chain proves ownership. Never invent or directly call an "
                "unobserved cleanup endpoint, and never repeat reconnaissance solely to prove "
                "session ownership."
            )
        if state_scope is not None:
            await state_scope.settle(
                before_refs=before_refs,
                action_refs=action.evidence_refs,
                response_refs=observation.evidence_refs,
                after_refs=observation.evidence_refs,
                transition_disposition="unchanged",
                cleanup_disposition=("completed" if kind == "close_tab" else "retained"),
            )
            metadata["state_scope_id"] = state_scope.lifecycle.scope_id
        elif session_recon_scope:
            metadata.update(
                {
                    "state_owner_kind": "session_browser_context",
                    "state_owner_id": f"browser:{session.context_id}",
                    "state_evidence_durable": True,
                }
            )
        return ToolResult(content=content, metadata=metadata)


_OWNED_SESSION_END_LABEL = re.compile(
    r"^(?:forfeit|(?:end|reset|cancel|forfeit) "
    r"(?:(?:this|current|active|reviewed) )?(?:session|chat|conversation))$",
    flags=re.IGNORECASE,
)


def _probe_linked_api_exchange(event: Mapping[str, object]) -> bool:
    """True when a captured exchange is provably linked to a reviewed probe fill+submit."""
    return bool(
        event.get("method") in {"POST", "PUT", "PATCH"}
        and isinstance(event.get("request_json"), (dict, list))
        and isinstance(event.get("response_json"), (dict, list))
        and isinstance(event.get("input_action_refs"), Mapping)
        and isinstance(event.get("input_probe_refs"), Mapping)
        and bool(event["input_action_refs"])
        and bool(set(event["input_action_refs"]).intersection(event["input_probe_refs"]))
        and isinstance(event.get("trigger_action_ref"), str)
        and bool(str(event["trigger_action_ref"]).strip())
    )


@dataclass
class _DerivedApiHandoff:
    """Executable http_api adapter derived from captured exchange evidence."""

    adapter: dict[str, Any]
    compiled_adapter: str
    compiled_adapter_digest: str
    method: str
    url: str
    success_witness: str
    request_evidence_ref: str
    request_evidence: dict[str, object]
    response_evidence_ref: str
    response_evidence: dict[str, object]
    setup_evidence: tuple[tuple[str, dict[str, object]], ...]
    creation_transition_evidence: tuple[tuple[str, dict[str, object]], ...]
    terminal_evidence: tuple[tuple[str, dict[str, object]], ...]
    cleanup_evidence: tuple[tuple[str, dict[str, object]], ...]
    capability_evidence: tuple[tuple[str, dict[str, object]], ...]
    recon_observations: tuple[dict[str, object], ...]
    stateful_reuse_is_unsafe: bool


class BrowserOwnedSessionCleanupTool:
    """Click only an unambiguous visible control that ends this owned session."""

    name = "browser_end_owned_session"
    effect = "target_network"

    def __init__(
        self,
        resolver: BrowserSessionResolver,
        recorder: BrowserRecorder | None = None,
        failure_tracker: dict[tuple[str, ...], int] | None = None,
        state_scope_store: object | None = None,
    ) -> None:
        self._resolver = resolver
        self._recorder = recorder
        self._failures = failure_tracker if failure_tracker is not None else {}
        self._state_scope_store = state_scope_store

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                "End the current owned browser session by clicking its single visible, enabled "
                "Forfeit, End session, Reset session, or Cancel session control."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "step": {"type": "integer", "minimum": 0},
                    "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 30000},
                },
                "required": [],
                "additionalProperties": False,
            },
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        resolved = self._resolver(context.session_id)
        session = await resolved if isawaitable(resolved) else resolved
        metadata = {
            "effect": self.effect,
            "target": session.bounds.target_id,
            "target_session_id": f"browser:{session.context_id}",
        }
        if context.campaign_id is not None and context.agent_id is not None:
            return ToolResult(
                content=(
                    "owned session cleanup is reconnaissance-only: campaign workers cannot "
                    "end the shared reviewed target session"
                ),
                is_error=True,
                metadata={**metadata, "campaign_scoped": True},
            )
        allowed = {"step", "timeout_ms"}
        if set(arguments) - allowed:
            return ToolResult(
                content=f"arguments do not match {self.name} schema",
                is_error=True,
                metadata=metadata,
            )
        if context.session_id != session.owner.session_id:
            return ToolResult(
                content="owned session cleanup rejected: browser session owner does not match",
                is_error=True,
                metadata=metadata,
            )
        open_tabs = tuple(tab.tab_ref for tab in session.metadata().tabs if tab.state == "open")
        if len(open_tabs) != 1:
            return ToolResult(
                content=(
                    "owned session cleanup requires exactly one open reviewed browser tab; "
                    f"found {len(open_tabs)}"
                ),
                is_error=True,
                metadata=metadata,
            )
        tab_ref = open_tabs[0]
        state_scope: _BrowserStateScope | None = None
        if self._state_scope_store is not None and context.campaign_id is not None:
            action_id = str(context.call_id or uuid4())
            state_scope = await _BrowserStateScope.open(
                self._state_scope_store,
                campaign_id=context.campaign_id,
                target_authority=_browser_action_authority(
                    session,
                    BrowserAction(
                        context_id=session.context_id,
                        target_id=session.bounds.target_id,
                        owner=session.owner,
                        kind="close_tab",
                        tab_ref=tab_ref,
                        step=int(arguments.get("step", 0)),
                    ),
                ),
                action_id=action_id,
                candidate_id=str(context.technique_attempt_id or context.agent_id or action_id),
                experiment_id=_browser_experiment_id(context, action_id),
                action_kind=StateScopeActionKind.CLEANUP,
                equivalence_key="conservative-shared-state",
            )
            if state_scope is None:
                return ToolResult(
                    content="reviewed browser state is already owned by another execution",
                    is_error=True,
                    metadata={
                        **metadata,
                        "failure_code": "physical_state_unavailable",
                        "retryable": True,
                        "target_effect_attempted": False,
                        "target_call_budget_charge": 0,
                    },
                )
        try:
            snapshot = await session.snapshot(
                tab_ref,
                step=int(arguments.get("step", 0)),
            )
        except Exception:
            if state_scope is not None:
                await state_scope.pause(MissingEvidenceReason.SELECTOR_UNAVAILABLE)
            raise
        before_refs = (_browser_evidence_ref("before", snapshot),)
        if self._recorder is not None:
            await self._recorder.append_observation(snapshot)
        candidates = tuple(
            element
            for element in snapshot.elements
            if element.role.casefold() == "button"
            and not element.disabled
            and _OWNED_SESSION_END_LABEL.fullmatch(_normalized_accessible_name(element.name))
        )
        if len(candidates) != 1:
            if state_scope is not None:
                await state_scope.pause(
                    MissingEvidenceReason.NOT_APPLICABLE,
                    before_refs=before_refs,
                    retryable=False,
                )
            return ToolResult(
                content=(
                    "owned session cleanup requires exactly one visible, enabled session-ending "
                    f"button; found {len(candidates)}"
                ),
                is_error=True,
                metadata=metadata,
            )
        candidate = candidates[0]
        if self._recorder is None:
            if state_scope is not None:
                await state_scope.pause(
                    MissingEvidenceReason.NOT_APPLICABLE,
                    before_refs=before_refs,
                    retryable=False,
                )
            return ToolResult(
                content="owned session cleanup requires its durable reconnaissance handoff",
                is_error=True,
                metadata=metadata,
            )
        handoffs = await self._recorder.list_handoffs(session.bounds.target_id)
        latest = next(
            (
                handoff
                for handoff in handoffs
                if handoff.context_id == session.context_id
                and handoff.owner.session_id == context.session_id
            ),
            None,
        )
        try:
            reviewed_adapter = json.loads(latest.adapter) if latest is not None else None
        except (TypeError, json.JSONDecodeError):
            reviewed_adapter = None
        transition = (
            reviewed_adapter.get("session_creation_transition")
            if isinstance(reviewed_adapter, Mapping)
            else None
        )
        discovery = (
            reviewed_adapter.get("cleanup_discovery")
            if isinstance(reviewed_adapter, Mapping)
            else None
        )
        remote_session_id = (
            transition.get("remote_session_id") if isinstance(transition, Mapping) else None
        )
        if (
            not isinstance(discovery, Mapping)
            or discovery.get("status") != "required_before_target_execution"
            or discovery.get("session_ownership") != "owned"
            or not isinstance(remote_session_id, str)
            or not remote_session_id
            or not isinstance(transition.get("evidence_refs"), list)
            or not transition["evidence_refs"]
        ):
            if state_scope is not None:
                await state_scope.pause(
                    MissingEvidenceReason.NOT_APPLICABLE,
                    before_refs=before_refs,
                    retryable=False,
                )
            return ToolResult(
                content=(
                    "owned session cleanup rejected: the latest same-context handoff does not "
                    "prove the exact remote session ID"
                ),
                is_error=True,
                metadata=metadata,
            )
        action_ref = _browser_evidence_ref(
            "cleanup-action",
            {"element_ref": candidate.ref, "frame_ref": candidate.frame_ref},
        )
        if state_scope is not None:
            await state_scope.lifecycle.acknowledge(
                before_refs=before_refs,
                action_refs=(action_ref,),
            )
        result = await BrowserTool(
            "browser_click",
            self._resolver,
            self._recorder,
            self._failures,
        ).invoke(
            context,
            {
                "tab_ref": tab_ref,
                "frame_ref": candidate.frame_ref,
                "element_ref": candidate.ref,
                "step": int(arguments.get("step", 0)),
                "timeout_ms": int(arguments.get("timeout_ms", 10_000)),
            },
        )
        if state_scope is not None:
            response_refs = tuple(
                str(item)
                for item in result.metadata.get("evidence_refs", ())
                if isinstance(item, str) and item
            )
            if result.is_error:
                await state_scope.pause(
                    MissingEvidenceReason.TRANSPORT_FAILED,
                    before_refs=before_refs,
                    action_refs=(action_ref,),
                )
            else:
                await state_scope.settle(
                    before_refs=before_refs,
                    action_refs=(action_ref,),
                    response_refs=response_refs,
                    after_refs=response_refs,
                    transition_disposition="unchanged",
                    cleanup_disposition="completed",
                )
        return result.model_copy(
            update={
                "metadata": {
                    **result.metadata,
                    "owned_session_cleanup": True,
                    "cleanup_control": candidate.name,
                    "owned_remote_session_id": remote_session_id,
                    **(
                        {"preferred_next_tool": "browser_compile_api_handoff"}
                        if not result.is_error
                        else {}
                    ),
                    **(
                        {
                            "state_owner_kind": "session_browser_context",
                            "state_owner_id": f"browser:{session.context_id}",
                            "state_evidence_durable": True,
                        }
                        if context.campaign_id is None
                        else {}
                    ),
                }
            }
        )


class ApiReconHandoffTool:
    """Seal an observed API route or an explicit DOM fallback into durable browser evidence."""

    name = "browser_record_api_handoff"
    # This only seals already-observed evidence into Merlin's durable local
    # state. Classify it as a supported local write so permission handling can
    # prompt/approve it instead of failing closed as an unknown effect.
    effect = "harness_state"

    def __init__(self, resolver: BrowserSessionResolver, recorder: BrowserRecorder) -> None:
        self._resolver = resolver
        self._recorder = recorder

    def describe(self) -> ToolSpec:
        api_step_schema = {
            "type": "object",
            "properties": {
                "step_id": {"type": "string", "minLength": 1},
                "endpoint": {"type": "string", "minLength": 1},
                "method": {"type": "string", "minLength": 1},
                "request_template": {
                    "description": (
                        "JSON request body template. Use {{input}} as a complete string value "
                        "for the current worker input."
                    )
                },
                "response_selector": {
                    "description": (
                        "Optional evaluator hint. Prefer a JSON Pointer: empty selects the whole "
                        "response and /... selects a nested value. Invalid or stale hints retain "
                        "the full response and are recorded as evaluation_uncertain."
                    ),
                },
                "success_witness": {
                    "description": "Optional evaluator hint; it never gates execution."
                },
                "expected_status": {
                    "description": (
                        "Optional expected HTTP status. A live mismatch preserves telemetry and "
                        "is recorded as evaluation_uncertain."
                    )
                },
                "idempotency_key": {"type": ["string", "null"]},
                "replay_after_ambiguous_result": {
                    "description": (
                        "Optional; defaults false. True requires an idempotency key because the "
                        "request may already have taken effect."
                    )
                },
                "cleanup": {
                    "type": "boolean",
                    "description": "True only when this step performs cleanup; never prose.",
                },
                "interaction": {
                    "type": "boolean",
                    "description": (
                        "True only when this step interacts with the target; never prose."
                    ),
                },
                "captures": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "repeat": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": [
                "step_id",
                "endpoint",
                "method",
                "request_template",
                "cleanup",
                "interaction",
                "captures",
                "repeat",
            ],
            "additionalProperties": True,
        }
        adapter_schema = {
            "type": "object",
            "description": (
                "Reviewed provider-chat or HTTP API adapter as a structured object, not encoded "
                "JSON. Provider chat needs only its interface and endpoint; HTTP API additionally "
                "needs executable api_steps. Advisory evidence metadata is optional."
            ),
            "properties": {
                "interface": {"type": "string", "minLength": 1},
                "endpoint": {"type": "string", "minLength": 1},
                "authentication": {"type": "string", "minLength": 1},
                "observed_evidence": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
                "session_constraint": {"type": "string", "minLength": 1},
                "api_steps": {"type": "array", "minItems": 1, "items": api_step_schema},
            },
            "required": ["interface", "endpoint"],
            "additionalProperties": True,
        }
        interaction_contract_schema = {
            "type": "object",
            "properties": {
                "input_locator": {"type": "string", "minLength": 1},
                "submit_action": {"type": "string", "minLength": 1},
                "frame_context": {"type": ["string", "null"]},
                "authenticated_session_constraints": {"type": "string", "minLength": 1},
                "input_limit": {
                    "type": "object",
                    "properties": {
                        "max_characters": {"type": ["integer", "null"], "minimum": 1},
                        "source": {
                            "enum": [
                                "dom_attribute",
                                "observed_validation",
                                "documented",
                                "not_exposed",
                            ]
                        },
                        "evidence": {"type": "string", "minLength": 1},
                    },
                    "required": ["max_characters", "source", "evidence"],
                    "additionalProperties": False,
                },
                "probe": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "minLength": 1},
                        "objective_relevance": {"type": "string", "minLength": 1},
                        "sent_message_witness": {"type": "string", "minLength": 1},
                        "response_witness": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Exact case-sensitive visible text whose occurrence count "
                                "increases "
                                "only when a new target response is rendered"
                            ),
                        },
                    },
                    "required": [
                        "message",
                        "objective_relevance",
                        "sent_message_witness",
                        "response_witness",
                    ],
                    "additionalProperties": False,
                },
                "lifecycle": {
                    "type": "object",
                    "properties": {
                        name: {"type": ["string", "null"], "minLength": 1}
                        for name in (
                            "current_state",
                            "terminal_indicator",
                            "resume_action",
                            "rating_action",
                            "new_session_action",
                            "cleanup_action",
                        )
                    },
                    "required": [
                        "current_state",
                        "terminal_indicator",
                        "resume_action",
                        "rating_action",
                        "new_session_action",
                        "cleanup_action",
                    ],
                    "additionalProperties": False,
                },
                "success_indicator": {"type": "string", "minLength": 1},
                "evidence_refs": {
                    "type": "array",
                    "minItems": 2,
                    "items": {"type": "string", "minLength": 1},
                },
            },
            "required": [
                "input_locator",
                "submit_action",
                "frame_context",
                "authenticated_session_constraints",
                "input_limit",
                "probe",
                "lifecycle",
                "success_indicator",
                "evidence_refs",
            ],
            "additionalProperties": True,
        }
        browser_adapter_schema = {
            "type": "object",
            "description": "Complete observed browser adapter for a DOM fallback.",
            "properties": {
                "interface": {"enum": ["browser"]},
                "endpoint": {"type": "string", "minLength": 1},
                "authentication": {"type": "string", "minLength": 1},
                "observed_evidence": {
                    "type": "array",
                    "minItems": 2,
                    "items": {"type": "string", "minLength": 1},
                },
                "browser_steps": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_id": {"type": "string", "minLength": 1},
                            "tool": {"type": "string", "minLength": 1},
                            "arguments": {"type": "object"},
                        },
                        "required": ["step_id", "tool", "arguments"],
                        "additionalProperties": False,
                    },
                },
                "interaction_contract": interaction_contract_schema,
            },
            "required": [
                "interface",
                "endpoint",
                "authentication",
                "observed_evidence",
                "browser_steps",
                "interaction_contract",
            ],
            "additionalProperties": True,
        }
        return ToolSpec(
            name=self.name,
            description=(
                "Record an explicit evidence-backed DOM fallback, or an exceptional manually "
                "authored API adapter that cannot be compiled from captured JSON exchanges. "
                "Whenever probe-linked JSON exchange evidence exists, a DOM fallback is "
                "superseded by the compiled API adapter: this tool derives and seals that "
                "http_api adapter itself, retaining dom_fallback_reason and reusable_adapter "
                "as evidence. When reconnaissance captured JSON setup/message/cleanup "
                "exchanges that this derivation cannot bind, use browser_compile_api_handoff. "
                "For a DOM fallback, "
                "dom_fallback_reason and reusable_adapter are required and must preserve the "
                "complete observed browser interaction contract. The model supplies exactly "
                "one branch: API uses adapter (including api_steps); DOM fallback uses "
                "dom_fallback_reason plus reusable_adapter (including browser_steps and an "
                "interaction_contract) and must omit adapter/api_steps. An API failure does "
                "not prevent sealing independently observed DOM evidence."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "method": {"type": "string", "minLength": 1},
                    "url": {"type": "string", "minLength": 1},
                    "request_evidence_ref": {"type": "string", "minLength": 1},
                    "response_evidence_ref": {"type": "string", "minLength": 1},
                    "adapter": {
                        **adapter_schema,
                        "description": (
                            "Complete reviewed HTTP API adapter. Supply this as a structured "
                            "object; Merlin canonicalizes it after schema validation. Do not "
                            "encode it as a JSON string or use base_url/start/message shorthand."
                        ),
                    },
                    "dom_fallback_reason": {"type": "string", "minLength": 1},
                    "reusable_adapter": {
                        **browser_adapter_schema,
                        "description": (
                            "For a DOM fallback, supply the complete observed browser adapter as "
                            "a structured object. It requires browser_steps and an "
                            "interaction_contract, not api_steps. Merlin validates the canonical "
                            "interaction contract before sealing it. Omit adapter when using this "
                            "DOM branch; omit this field for an API handoff."
                        ),
                    },
                    "reusable_site_facts": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Canonical compact JSON containing only shared site/navigation/auth "
                            "facts safe to reuse for a different bot on this target. Never include "
                            "credentials, bot-specific selectors, session state, or oracle claims."
                        ),
                    },
                    "recon_scope": {
                        "type": "object",
                        "description": (
                            "Platform-neutral observed identity used for safe future reuse. "
                            "Include "
                            "only dimensions supported by the recorded request/response evidence."
                        ),
                        "properties": {
                            "surface": {"type": "string", "minLength": 1},
                            "authentication_context": {"type": "string", "minLength": 1},
                            "tenant": {"type": "string", "minLength": 1},
                            "agent_or_bot": {"type": "string", "minLength": 1},
                            "scenario": {"type": "string", "minLength": 1},
                            "session_constraint": {"type": "string", "minLength": 1},
                            "oracle_contract": {"type": "string", "minLength": 1},
                        },
                        "additionalProperties": False,
                    },
                },
                "required": [
                    "method",
                    "url",
                    "request_evidence_ref",
                    "response_evidence_ref",
                ],
                "oneOf": [
                    {"required": ["adapter"]},
                    {"required": ["dom_fallback_reason", "reusable_adapter"]},
                ],
                "additionalProperties": False,
            },
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        adapter = arguments.get("adapter")
        fallback = _string(arguments.get("dom_fallback_reason"))
        reusable_adapter = arguments.get("reusable_adapter")
        reusable_site_facts = _string(arguments.get("reusable_site_facts"))
        raw_scope = arguments.get("recon_scope")
        recon_scope = (
            {str(key): str(value) for key, value in raw_scope.items()}
            if isinstance(raw_scope, Mapping)
            else {}
        )
        handoff_uncertainties: list[dict[str, object]] = []
        if bool(adapter) == bool(fallback):
            return ToolResult(
                content="provide exactly one API adapter or explicit DOM fallback reason",
                is_error=True,
                metadata={"effect": self.effect},
            )
        if fallback and not reusable_adapter:
            return ToolResult(
                content=(
                    "DOM fallback requires reusable_adapter containing the complete observed "
                    "browser interaction contract; seal it once, then call assessment_prepare "
                    "with only the returned recon_handoff_id"
                ),
                is_error=True,
                metadata={"effect": self.effect},
            )
        try:
            adapter = _canonical_object_string(adapter, label="API handoff adapter")
            reusable_adapter = _canonical_object_string(reusable_adapter, label="reusable adapter")
            reusable_site_facts = _canonical_object_string(
                reusable_site_facts, label="reusable site facts"
            )
        except ValueError as error:
            return ToolResult(content=str(error), is_error=True, metadata={"effect": self.effect})
        adapter_value: dict[str, object] | None = None
        reusable_adapter_value: dict[str, object] | None = None
        legacy_execution_error: str | None = None
        if adapter is not None:
            parsed_adapter = json.loads(adapter)
            assert isinstance(parsed_adapter, dict)
            adapter_value = normalize_api_adapter_input_aliases(parsed_adapter)
            if adapter_value.get("interface") == "http_api":
                normalized_steps, step_uncertainties = _normalize_sealable_api_steps(
                    adapter_value.get("api_steps")
                )
                adapter_value["api_steps"] = normalized_steps
                existing_uncertainties = _uncertainty_records(
                    adapter_value.get("evaluation_uncertainties")
                )
                existing_uncertainties.extend(step_uncertainties)
                if existing_uncertainties:
                    adapter_value["evaluation_uncertainties"] = _dedupe_uncertainties(
                        existing_uncertainties
                    )
                    handoff_uncertainties.extend(step_uncertainties)
                legacy_execution_error = _validate_sealable_api_steps(normalized_steps)
            adapter = json.dumps(adapter_value, sort_keys=True, separators=(",", ":"))
        if fallback is not None:
            assert reusable_adapter is not None
            parsed_reusable = json.loads(reusable_adapter)
            assert isinstance(parsed_reusable, dict)
            reusable_adapter_value = _repair_v1_browser_steps(parsed_reusable)
            reusable_adapter = json.dumps(
                reusable_adapter_value, sort_keys=True, separators=(",", ":")
            )
        resolved = self._resolver(context.session_id)
        session = await resolved if isawaitable(resolved) else resolved
        compilation_source = adapter_value or reusable_adapter_value
        assert compilation_source is not None
        try:
            compilation, compiled_adapter, compiled_adapter_digest = _canonical_compilation(
                compilation_source,
                allowed_origins=getattr(session.bounds, "allowed_origins", ()),
            )
        except AdapterCompileError as error:
            result = _compile_error_result(error, effect=self.effect)
            if legacy_execution_error is not None:
                result = result.model_copy(update={"content": legacy_execution_error})
            return result
        if fallback is not None and not isinstance(compilation.executable, BrowserChat):
            return ToolResult(
                content="DOM fallback reusable_adapter must compile as browser chat",
                is_error=True,
                metadata={
                    "effect": self.effect,
                    "failure_code": AdapterCompileFailureKind.NON_EXECUTABLE_TRANSPORT.value,
                },
            )
        if fallback is None and isinstance(compilation.executable, BrowserChat):
            return ToolResult(
                content="API handoff adapter must compile as provider chat or HTTP API",
                is_error=True,
                metadata={
                    "effect": self.effect,
                    "failure_code": AdapterCompileFailureKind.NON_EXECUTABLE_TRANSPORT.value,
                },
            )
        handoff_uncertainties.extend(_compilation_uncertainties(compilation))
        derived_handoff: _DerivedApiHandoff | None = None
        if fallback is not None:
            assert reusable_adapter is not None
            assert reusable_adapter_value is not None
            reviewed_browser = reusable_adapter_value
            reviewed_contract = reviewed_browser["interaction_contract"]
            probe = reviewed_contract.get("probe")
            probe_message = (
                str(probe.get("message", "")).strip() if isinstance(probe, Mapping) else ""
            )
            api_candidates = [
                event
                for event in session.api_exchange_events()
                if event.get("method") in {"POST", "PUT", "PATCH"}
                and (
                    not probe_message
                    or _json_contains_exact(event.get("request_json"), probe_message)
                )
                and "response_json" in event
            ]
            open_tabs = [tab.tab_ref for tab in session.metadata().tabs if tab.state == "open"]
            if len(open_tabs) != 1:
                return ToolResult(
                    content="DOM fallback handoff requires exactly one current reviewed tab",
                    is_error=True,
                    metadata={"effect": self.effect},
                )
            current = await session.snapshot(open_tabs[0])
            input_match = _resolve_reviewed_element(
                current,
                reviewed_contract["input_locator"],
                default_role="textbox",
            )
            submit_match = _resolve_reviewed_element(
                current,
                reviewed_contract["submit_action"],
                default_role="button",
            )
            if input_match is None or submit_match is None:
                return ToolResult(
                    content=(
                        "DOM fallback locators must each identify exactly one current accessible "
                        "element by reviewed role and name"
                    ),
                    is_error=True,
                    metadata={"effect": self.effect},
                )
            if not isinstance(probe, Mapping) or not probe_message:
                handoff_uncertainties.append(
                    {
                        "kind": "evaluation_uncertain",
                        "source": "browser.probe",
                        "reason": (
                            "No exact probe message was recorded; the executable DOM transport "
                            "was retained."
                        ),
                        "confidence": 0.0,
                    }
                )
            probe_linked = [
                candidate for candidate in api_candidates if _probe_linked_api_exchange(candidate)
            ]
            if probe_message and probe_linked:
                exchange = probe_linked[-1]
                probe_pointers = sorted(
                    pointer
                    for pointer, value in _string_scalar_paths(exchange.get("request_json"))
                    if value == probe_message
                )
                input_pointer = next(
                    (
                        pointer
                        for pointer in ("/message", "/userMessage")
                        if pointer in probe_pointers
                    ),
                    probe_pointers[0] if probe_pointers else "",
                )
                if not input_pointer:
                    return ToolResult(
                        content=(
                            "the captured API exchange does not bind the reviewed probe message "
                            "to one request field; compile the API handoff explicitly"
                        ),
                        is_error=True,
                        metadata={
                            "effect": self.effect,
                            "failure_code": "input_provenance_missing",
                            "retryable": True,
                            "preferred_next_tool": "browser_compile_api_handoff",
                        },
                    )
                # Probe-linked JSON exchange evidence supersedes the DOM fallback:
                # derive and seal the compiled http_api adapter, retaining the DOM
                # contract as evidence.
                derived = await CompiledApiReconHandoffTool(
                    self._resolver, self._recorder
                )._compile_reviewed_adapter(
                    session,
                    {
                        "method": str(exchange["method"]),
                        "url": str(exchange["url"]),
                        "input_pointer": input_pointer,
                        "authentication": str(
                            reviewed_browser.get("authentication") or "reviewed browser session"
                        ),
                        "session_constraint": str(
                            reviewed_browser.get("session_constraint")
                            or "one reviewed browser session at a time"
                        ),
                    },
                )
                if isinstance(derived, ToolResult):
                    return derived.model_copy(
                        update={
                            "metadata": {
                                **derived.metadata,
                                "preferred_next_tool": "browser_compile_api_handoff",
                                "retryable": True,
                            }
                        }
                    )
                derived_handoff = derived
        identity = "|".join(
            str(arguments[key])
            for key in ("method", "url", "request_evidence_ref", "response_evidence_ref")
        )
        # Bind the reviewed decision as well as its evidence. An exact retry remains
        # idempotent, while a corrected adapter/fallback is a new attributable handoff
        # instead of colliding with an immutable earlier reconnaissance decision.
        identity = "|".join((identity, adapter or "", fallback or ""))
        identity = "|".join(
            (
                identity,
                reusable_adapter or "",
                reusable_site_facts or "",
                json.dumps(recon_scope, sort_keys=True, separators=(",", ":")),
            )
        )
        if derived_handoff is not None:
            adapter = json.dumps(derived_handoff.adapter, sort_keys=True, separators=(",", ":"))
            compiled_adapter = derived_handoff.compiled_adapter
            compiled_adapter_digest = derived_handoff.compiled_adapter_digest
        try:
            handoff = ApiReconHandoff(
                id=f"handoff:{uuid5(NAMESPACE_URL, identity)}",
                context_id=session.context_id,
                target_id=session.bounds.target_id,
                owner=session.owner,
                method=str(arguments["method"]),
                url=str(arguments["url"]),
                request_evidence_ref=str(arguments["request_evidence_ref"]),
                response_evidence_ref=str(arguments["response_evidence_ref"]),
                adapter=adapter,
                dom_fallback_reason=fallback,
                reusable_adapter=reusable_adapter or adapter,
                compiled_adapter=compiled_adapter,
                compiled_adapter_digest=compiled_adapter_digest,
                reusable_site_facts=reusable_site_facts,
                recon_scope=recon_scope,
            )
            await self._recorder.record_handoff(handoff)
        except ValueError as error:
            return ToolResult(content=str(error), is_error=True, metadata={"effect": self.effect})
        metadata: dict[str, object] = {"effect": self.effect, "target": handoff.target_id}
        if handoff_uncertainties:
            metadata["evaluation_uncertainties"] = handoff_uncertainties
        if derived_handoff is not None:
            metadata["compiled_adapter"] = True
            metadata["preferred_next_tool"] = "assessment_prepare"
        if fallback is not None and api_candidates and derived_handoff is None:
            candidate = api_candidates[-1]
            metadata["api_candidate_observed"] = f"{candidate['method']} {candidate['url']}"
            handoff_uncertainties.append(
                {
                    "kind": "evaluation_uncertain",
                    "source": "browser.api_candidate",
                    "reason": (
                        "A captured JSON interaction also exists; the independently executable "
                        "DOM fallback was retained instead of treating API preference as an "
                        "admission gate."
                    ),
                    "confidence": 0.0,
                }
            )
            metadata["evaluation_uncertainties"] = handoff_uncertainties
        return ToolResult(
            content=handoff.model_dump_json(),
            metadata=metadata,
        )


class BrowserCheckpointVaultEntry(Protocol):
    reference_id: str

    def opaque_reference(self) -> dict[str, str]: ...

    def keyed_digest(self) -> dict[str, str]: ...


class BrowserCheckpointVault(Protocol):
    def store_checkpoint(
        self, checkpoint_secret: str, *, campaign_id: str, gate_id: str
    ) -> BrowserCheckpointVaultEntry: ...

    def resolve_checkpoint(self, reference_id: str) -> str: ...

    def delete_checkpoint(self, reference_id: str, *, expected_digest: str) -> bool: ...


class BrowserCheckpointStore(Protocol):
    def record_objective_gate_checkpoint(
        self, *args: Any, **kwargs: Any
    ) -> Mapping[str, object]: ...

    def claim_objective_gate_checkpoint_restore(
        self, *args: Any, **kwargs: Any
    ) -> Mapping[str, object]: ...

    def release_objective_gate_checkpoint_restore(self, *args: Any, **kwargs: Any) -> bool: ...

    def mark_objective_gate_checkpoint_restored(
        self, *args: Any, **kwargs: Any
    ) -> Mapping[str, object]: ...


@dataclass(slots=True)
class _BrowserStateScope:
    store: object
    lifecycle: StateScopeLifecycle
    claim_id: str
    claimant_id: str
    isolation_disposition: str = "shared_serialized"

    @classmethod
    async def open(
        cls,
        store: object,
        *,
        campaign_id: str,
        target_authority: str,
        action_id: str,
        candidate_id: str,
        experiment_id: str | None = None,
        action_kind: StateScopeActionKind = StateScopeActionKind.CANDIDATE,
        equivalence_key: str,
        isolation_disposition: str = "shared_serialized",
        wait_for_lease_seconds: float = 0.0,
    ) -> _BrowserStateScope | None:
        lifecycle = await StateScopeLifecycle.create(
            store,  # type: ignore[arg-type]
            campaign_id=campaign_id,
            target_authority=target_authority,
            attempt_id=action_id,
            action_id=action_id,
            candidate_id=candidate_id,
            experiment_id=experiment_id,
            action_kind=action_kind,
        )
        claimant_id = (
            "browser-state:" + hashlib.sha256(f"{campaign_id}\0{action_id}".encode()).hexdigest()
        )
        claim = store.claim_state_scope_lease  # type: ignore[attr-defined]
        deadline = asyncio.get_running_loop().time() + max(0.0, wait_for_lease_seconds)
        try:
            while True:
                claim_id = await asyncio.to_thread(
                    claim,
                    lifecycle.scope_id,
                    target_authority=target_authority,
                    principal_key=lifecycle.principal_key,
                    equivalence_key=equivalence_key,
                    claimant_id=claimant_id,
                    lease_seconds=120,
                )
                if isinstance(claim_id, str) and claim_id:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.02)
        except BaseException:
            await lifecycle.pause_unclaimed(
                isolation_disposition=isolation_disposition,
            )
            raise
        if not isinstance(claim_id, str) or not claim_id:
            await lifecycle.pause_unclaimed(
                isolation_disposition=isolation_disposition,
            )
            return None
        lifecycle.bind_lease(claim_id, claimant_id)
        return cls(store, lifecycle, claim_id, claimant_id, isolation_disposition)

    async def pause(
        self,
        reason: MissingEvidenceReason,
        *,
        before_refs: tuple[str, ...] = (),
        action_refs: tuple[str, ...] = (),
        retryable: bool = True,
    ) -> None:
        try:
            await self.lifecycle.pause(
                before_refs=before_refs,
                action_refs=action_refs,
                reason=reason.value,
                retryable=retryable,
                isolation_disposition=self.isolation_disposition,
            )
        finally:
            await self.close()

    async def settle(
        self,
        *,
        before_refs: tuple[str, ...],
        action_refs: tuple[str, ...],
        response_refs: tuple[str, ...],
        after_refs: tuple[str, ...],
        transition_disposition: str,
        cleanup_disposition: str = "retained",
    ) -> None:
        try:
            await self.lifecycle.settle(
                before_refs=before_refs,
                action_refs=action_refs,
                response_refs=response_refs,
                after_refs=after_refs,
                transition_disposition=transition_disposition,
                isolation_disposition=self.isolation_disposition,
                cleanup_disposition=cleanup_disposition,
            )
        finally:
            await self.close()

    async def close(self) -> None:
        release = self.store.release_state_scope_lease  # type: ignore[attr-defined]
        await asyncio.to_thread(release, self.claim_id, self.claimant_id)

    async def bind_isolated_state(
        self,
        *,
        equivalence_key: str,
        isolation_disposition: str,
        maximum_parallel_states: int,
    ) -> bool:
        """Atomically replace the conservative lease after host-side proof succeeds."""
        rekey = getattr(self.store, "rekey_state_scope_lease", None)
        if not callable(rekey):
            return False
        changed = await asyncio.to_thread(
            rekey,
            self.claim_id,
            self.claimant_id,
            principal_key=self.lifecycle.principal_key,
            equivalence_key=equivalence_key,
            maximum_parallel_states=maximum_parallel_states,
        )
        if changed:
            self.isolation_disposition = isolation_disposition
        return bool(changed)

    async def preflight_resource_block(self, adapter: BrowserChat) -> Mapping[str, object] | None:
        status_reader = getattr(self.store, "resource_status", None)
        capabilities = adapter.state_capabilities
        if not callable(status_reader) or capabilities is None:
            return None
        ownership = {item.name: item for item in capabilities.resource_ownership}
        for control in capabilities.resource_controls:
            owner = ownership[control.ownership_name]
            status = await asyncio.to_thread(
                status_reader,
                self.lifecycle.target_authority,
                self.lifecycle.principal_key,
                owner.resource_kind.value,
                control.window_key,
            )
            if isinstance(status, Mapping) and str(status.get("state")) in {
                "suspended",
                "exhausted",
            }:
                return status
        return None

    async def reserve_resources(self, adapter: BrowserChat) -> list[tuple[str, object]]:
        reserve = getattr(self.store, "reserve_resource", None)
        capabilities = adapter.state_capabilities
        if not callable(reserve) or capabilities is None:
            return []
        ownership = {item.name: item for item in capabilities.resource_ownership}
        reservations: list[tuple[str, object]] = []
        for control in capabilities.resource_controls:
            owner = ownership[control.ownership_name]
            reservation_id = (
                "browser-resource:"
                + hashlib.sha256(
                    (
                        f"{self.lifecycle.scope_id}\0{control.ownership_name}\0{control.step_id}"
                    ).encode()
                ).hexdigest()
            )
            status = await asyncio.to_thread(
                reserve,
                self.lifecycle.scope_id,
                reservation_id,
                target_authority=self.lifecycle.target_authority,
                principal_key=self.lifecycle.principal_key,
                resource_kind=owner.resource_kind.value,
                amount=control.reservation_amount,
                limit=control.limit,
                window_key=control.window_key,
                claim_id=self.claim_id,
                claimant_id=self.claimant_id,
            )
            if not isinstance(status, Mapping) or status.get("admitted") is not True:
                await self.release_resources(reservations)
                raise _BrowserResourceBlocked(
                    status if isinstance(status, Mapping) else {"state": "unavailable"}
                )
            reservations.append((reservation_id, control))
        return reservations

    async def acknowledge_resources(self, reservations: list[tuple[str, object]]) -> None:
        acknowledge = getattr(self.store, "acknowledge_resource", None)
        if not callable(acknowledge):
            return
        for reservation_id, _ in reservations:
            await asyncio.to_thread(
                acknowledge,
                reservation_id,
                claim_id=self.claim_id,
                claimant_id=self.claimant_id,
            )

    async def settle_resources(self, reservations: list[tuple[str, object]]) -> None:
        settle = getattr(self.store, "settle_resource", None)
        if not callable(settle):
            return
        for reservation_id, control in reservations:
            await asyncio.to_thread(
                settle,
                reservation_id,
                actual_amount=control.reservation_amount,
                claim_id=self.claim_id,
                claimant_id=self.claimant_id,
            )

    async def release_resources(self, reservations: list[tuple[str, object]]) -> None:
        release = getattr(self.store, "release_resource", None)
        if not callable(release):
            return
        for reservation_id, _ in reservations:
            await asyncio.to_thread(
                release,
                reservation_id,
                claim_id=self.claim_id,
                claimant_id=self.claimant_id,
            )


class _BrowserResourceBlocked(ValueError):
    def __init__(self, status: Mapping[str, object]) -> None:
        self.status = dict(status)
        super().__init__("reviewed browser target resource is unavailable before delivery")


def _browser_evidence_ref(kind: str, value: object) -> str:
    if hasattr(value, "model_dump_json"):
        encoded = value.model_dump_json().encode()
    else:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"browser-{kind}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _browser_target_authority(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("compiled browser endpoint does not identify a target authority")
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    default_port = (parsed.scheme == "http" and parsed.port == 80) or (
        parsed.scheme == "https" and parsed.port == 443
    )
    port = "" if parsed.port is None or default_port else f":{parsed.port}"
    return f"{parsed.scheme.lower()}://{hostname.lower()}{port}"


def _browser_action_authority(session: BrowserSession, action: BrowserAction) -> str:
    """Resolve one stable authority without exposing a raw target identifier."""
    candidates: list[str] = []
    if action.kind in {"open_tab", "navigate"} and action.value:
        candidates.append(action.value)
    if action.tab_ref:
        candidates.extend(
            tab.url for tab in session.metadata().tabs if tab.tab_ref == action.tab_ref
        )
    candidates.extend(sorted(session.bounds.allowed_origins))
    for candidate in candidates:
        try:
            return _browser_target_authority(candidate)
        except ValueError:
            continue
    return (
        "browser-target:sha256:"
        + hashlib.sha256(str(session.bounds.target_id).encode()).hexdigest()
    )


def _browser_action_kind(context: ToolContext, kind: str) -> StateScopeActionKind:
    if context.state_action_kind is not None:
        return StateScopeActionKind(context.state_action_kind)
    if kind == "close_tab":
        return StateScopeActionKind.CLEANUP
    if context.objective_phase_checkpoint_intent == "capture":
        return StateScopeActionKind.BASELINE
    if context.technique_attempt_id or context.attempt_id:
        return StateScopeActionKind.CANDIDATE
    return StateScopeActionKind.RECONNAISSANCE


def _browser_experiment_id(context: ToolContext, fallback: str) -> str:
    """Keep every surface action for one scheduler experiment on one lane."""

    return str(
        context.technique_attempt_id
        or context.attempt_id
        or context.agent_id
        or fallback
    )


def _checkpoint_context(context: ToolContext) -> tuple[str, str, str, str] | None:
    if context.objective_phase_checkpoint_intent is None:
        return None
    values = (
        context.objective_phase_checkpoint_intent,
        context.objective_phase_gate_id,
        context.objective_phase_gate_recipe_digest,
        context.objective_phase_manifest_digest,
    )
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("checkpoint intent requires sealed gate, recipe, and manifest identity")
    return values  # type: ignore[return-value]


def _checkpoint_reference_id(value: object) -> str:
    if not isinstance(value, Mapping):
        raise ValueError("checkpoint restore requires an opaque state reference")
    if value.get("kind") != "opaque_reference" or value.get("namespace") != "state":
        raise ValueError("checkpoint restore requires an opaque state reference")
    reference_id = value.get("reference_id")
    if not isinstance(reference_id, str) or not reference_id:
        raise ValueError("checkpoint restore reference is invalid")
    return reference_id


def _json_pointer_value(value: object, pointer: str) -> object:
    current = value
    if not pointer.startswith("/"):
        raise ValueError("checkpoint selector must be a non-root JSON Pointer")
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not token.isdecimal() or int(token) >= len(current):
                raise ValueError("checkpoint selector did not resolve")
            current = current[int(token)]
        elif isinstance(current, Mapping) and token in current:
            current = current[token]
        else:
            raise ValueError("checkpoint selector did not resolve")
    return current


def _inject_checkpoint_secret(document: object, pointer: str, secret: str) -> object:
    result = deepcopy(document)
    if not pointer.startswith("/"):
        raise ValueError("checkpoint restore destination must be a non-root JSON Pointer")
    tokens = pointer[1:].split("/")
    current = result
    for index, raw in enumerate(tokens):
        token: str | int = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not str(token).isdecimal() or int(token) >= len(current):
                raise ValueError("checkpoint restore destination did not resolve")
            token = int(token)
        elif not isinstance(current, dict) or token not in current:
            raise ValueError("checkpoint restore destination did not resolve")
        if index == len(tokens) - 1:
            current[token] = secret
            return result
        current = current[token]
    raise ValueError("checkpoint restore destination did not resolve")


def _checkpoint_evidence_digest(value: object) -> str:
    if hasattr(value, "model_dump_json"):
        encoded = value.model_dump_json().encode()
    else:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _redact_checkpoint_secret(value: object, secret: str) -> object:
    if isinstance(value, dict):
        return {
            key: "[checkpoint-secret-redacted]"
            if item == secret
            else _redact_checkpoint_secret(item, secret)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            "[checkpoint-secret-redacted]"
            if item == secret
            else _redact_checkpoint_secret(item, secret)
            for item in value
        ]
    return value


def _browser_capability_step_ids(adapter: BrowserChat) -> frozenset[str]:
    """Return every exact browser step referenced by the reviewed state contract."""
    capabilities = adapter.state_capabilities
    if capabilities is None:
        return frozenset()
    step_ids: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            step_id = value.get("step_id")
            if isinstance(step_id, str) and step_id:
                step_ids.add(step_id)
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(capabilities.model_dump(mode="json"))
    return frozenset(step_ids)


def _browser_checkpoint_gate_succeeded(
    context: ToolContext,
    adapter: BrowserChat,
    step_documents: Mapping[str, object],
) -> tuple[bool, dict[str, object]]:
    """Evaluate the sealed gate through the shared authoritative transition oracle."""
    gate = context.objective_phase_gate_contract
    capabilities = adapter.state_capabilities
    if not isinstance(gate, Mapping) or capabilities is None or capabilities.identity is None:
        return False, {}
    transition = _browser_state_transition(adapter, step_documents)
    return evaluate_objective_gate_predicate(gate, transition) is True, transition


def _browser_state_transition(
    adapter: BrowserChat,
    step_documents: Mapping[str, object],
) -> dict[str, object]:
    """Evaluate reviewed browser state from exact host-captured step snapshots."""
    capabilities = adapter.state_capabilities
    if capabilities is None or capabilities.identity is None:
        return {}
    binding_context = {
        "compiled_adapter_digest": compiled_target_adapter_digest(adapter),
    }

    def bind(selector: object, *, name: str, role: str, phase: str, **definition: object):
        step_id = str(getattr(selector, "step_id", ""))
        evaluation_root = str(getattr(selector, "evaluation_root", ""))
        pointer = str(getattr(selector, "pointer", ""))
        root = step_documents.get(step_id)
        try:
            if evaluation_root != "response_body" or root is None:
                raise ValueError("browser state selector is unavailable")
            value = _select(root, pointer, step_id)
        except ValueError:
            value, available = None, False
        else:
            available = True
        return build_state_observation(
            name=name,
            role=role,
            phase=phase,
            step_id=step_id,
            evaluation_root=evaluation_root,
            pointer=pointer,
            binding_context=binding_context,
            selected_value=value,
            selected_available=available,
            **definition,
        )

    observations: list[dict[str, object]] = []
    identity = capabilities.identity
    observations.append(
        bind(
            identity.baseline_selector,
            name="physical_state_identity",
            role="state_identity",
            phase="baseline",
            evidence_refs=identity.evidence_refs,
        )
    )
    if identity.result_selector is not None:
        observations.append(
            bind(
                identity.result_selector,
                name="physical_state_identity",
                role="state_identity",
                phase="result",
                evidence_refs=identity.evidence_refs,
            )
        )
    for observation in capabilities.observations:
        definition = {
            "preferred_direction": (
                observation.preferred_direction.value
                if observation.preferred_direction is not None
                else None
            ),
            "success_values": observation.success_values,
            "failure_values": observation.failure_values,
            "evidence_refs": observation.evidence_refs,
            "value_type": (
                observation.value_type.value if observation.value_type is not None else None
            ),
            "minimum_value": observation.minimum_value,
            "maximum_value": observation.maximum_value,
            "monotonicity": (
                observation.monotonicity.value if observation.monotonicity is not None else None
            ),
            "trust_class": (
                observation.trust_class.value if observation.trust_class is not None else None
            ),
        }
        observations.extend(
            (
                bind(
                    observation.baseline_selector,
                    name=observation.name,
                    role=observation.role.value,
                    phase="baseline",
                    **definition,
                ),
                bind(
                    observation.result_selector,
                    name=observation.name,
                    role=observation.role.value,
                    phase="result",
                    **definition,
                ),
            )
        )
    return evaluate_state_transition(
        observations,
        [item.model_dump(mode="json") for item in capabilities.corroboration_rules],
    )


def _browser_isolation_baseline(
    adapter: BrowserChat,
    plan: StateIsolationPlan,
    step_documents: Mapping[str, object],
) -> tuple[str | None, str | None]:
    """Return a content-addressed physical-state key only after every reviewed proof matches."""
    capabilities = adapter.state_capabilities
    if not plan.isolated or capabilities is None or capabilities.identity is None:
        return None, "reviewed browser lifecycle does not establish isolated state"
    selector = capabilities.identity.baseline_selector
    root = step_documents.get(selector.step_id)
    if selector.evaluation_root != "response_body" or root is None:
        return None, "reviewed browser physical-state identity is unavailable before delivery"
    try:
        identity = _select(root, selector.pointer, selector.step_id)
    except ValueError:
        return None, "reviewed browser physical-state identity selector did not resolve"
    for rule in capabilities.freshness_rules:
        rule_root = step_documents.get(rule.selector.step_id)
        if rule.selector.evaluation_root != "response_body" or rule_root is None:
            return None, "reviewed browser freshness evidence is unavailable before delivery"
        try:
            observed = _select(rule_root, rule.selector.pointer, rule.selector.step_id)
        except ValueError:
            return None, "reviewed browser freshness selector did not resolve"
        if not any(
            type(observed) is type(expected) and observed == expected
            for expected in rule.match_values
        ):
            return None, "reviewed browser independent-state freshness proof did not match"
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return "physical-state:sha256:" + hashlib.sha256(encoded).hexdigest(), None


class BrowserChatExchangeTool:
    """Execute the reviewed fill/submit browser flow without another model turn."""

    name = "browser_chat_exchange"
    effect = "target_network"

    def __init__(
        self,
        resolver: BrowserSessionResolver,
        recorder: BrowserRecorder,
        adapter_resolver: ReviewedBrowserAdapterResolver,
        state_scope_store: object | None = None,
        checkpoint_store: BrowserCheckpointStore | None = None,
        checkpoint_vault: BrowserCheckpointVault | None = None,
    ) -> None:
        self._resolver = resolver
        self._recorder = recorder
        self._adapter_resolver = adapter_resolver
        self._state_scope_store = state_scope_store
        self._checkpoint_store = checkpoint_store
        self._checkpoint_vault = checkpoint_vault
        self._failures: dict[tuple[str, ...], int] = {}

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                "Send one prompt through the campaign's exact reviewed browser flow. The runtime "
                "fills and submits without an intervening model inference, then returns the "
                "observed response state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "minLength": 1},
                    "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 30000},
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
                "required": ["prompt"],
                "additionalProperties": False,
            },
            interaction_role=ToolInteractionRole.TARGET_EXCHANGE,
            target_delivery=TargetDeliveryClass.DIRECT_USER_CHANNEL,
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        if context.campaign_id is None:
            return ToolResult(
                content="compiled browser flow requires an active reviewed campaign",
                is_error=True,
                metadata={"effect": self.effect},
            )
        resolved_adapter = self._adapter_resolver(context.campaign_id)
        adapter = await resolved_adapter if isawaitable(resolved_adapter) else resolved_adapter
        if not isinstance(adapter, Mapping) or adapter.get("interface") != "browser":
            return ToolResult(
                content="campaign has no reviewed browser flow",
                is_error=True,
                metadata={"effect": self.effect},
            )
        session_value = self._resolver(context.session_id)
        session = await session_value if isawaitable(session_value) else session_value
        session_target_id = str(session.bounds.target_id)
        if context.target_id is not None and context.target_id != session_target_id:
            return _adapter_state_integrity_result(
                "browser session target identity does not match the sealed worker context",
                effect=self.effect,
                expected=context.target_id,
                observed=session_target_id,
                identity_field="target_id",
                failure_code="target_identity_mismatch",
            )
        try:
            compilation, _, compiled_adapter_digest = _canonical_compilation(
                adapter,
                allowed_origins=getattr(session.bounds, "allowed_origins", ()),
            )
        except AdapterCompileError as error:
            return _compile_error_result(error, effect=self.effect)
        durable_compiled_adapter = adapter.get("compiled_adapter")
        durable_compiled_digest = adapter.get("compiled_adapter_digest")
        if bool(durable_compiled_adapter) != bool(durable_compiled_digest):
            return _adapter_state_integrity_result(
                "reviewed browser adapter compiled state is incomplete",
                effect=self.effect,
            )
        canonical_compilation = compilation
        if durable_compiled_adapter is not None:
            if not isinstance(durable_compiled_adapter, str) or not isinstance(
                durable_compiled_digest, str
            ):
                return _adapter_state_integrity_result(
                    "reviewed browser adapter compiled state is invalid",
                    effect=self.effect,
                )
            try:
                durable_compilation = read_target_adapter(durable_compiled_adapter)
            except AdapterCompileError:
                return _adapter_state_integrity_result(
                    "reviewed browser adapter compiled state is malformed",
                    effect=self.effect,
                )
            observed_compiled_digest = compiled_target_adapter_digest(durable_compilation)
            if (
                observed_compiled_digest != durable_compiled_digest
                or observed_compiled_digest != compiled_adapter_digest
            ):
                return _adapter_state_integrity_result(
                    "reviewed browser adapter compiled and compatibility states disagree",
                    effect=self.effect,
                )
            canonical_compilation = durable_compilation
            compiled_adapter_digest = observed_compiled_digest
        if bool(context.compiled_adapter) != bool(context.compiled_adapter_digest):
            return _adapter_state_integrity_result(
                "worker context compiled browser adapter state is incomplete",
                effect=self.effect,
                expected=compiled_adapter_digest,
                observed=context.compiled_adapter_digest,
            )
        if context.compiled_adapter is not None:
            assert context.compiled_adapter_digest is not None
            try:
                context_compilation = read_target_adapter(context.compiled_adapter)
            except AdapterCompileError:
                return _adapter_state_integrity_result(
                    "worker context compiled browser adapter state is malformed",
                    effect=self.effect,
                    expected=compiled_adapter_digest,
                    observed=context.compiled_adapter_digest,
                )
            context_compiled_digest = compiled_target_adapter_digest(context_compilation)
            if context_compiled_digest != context.compiled_adapter_digest:
                return _adapter_state_integrity_result(
                    "worker context compiled browser adapter digest is invalid",
                    effect=self.effect,
                    expected=context.compiled_adapter_digest,
                    observed=context_compiled_digest,
                )
            if context_compiled_digest != compiled_adapter_digest:
                return _adapter_state_integrity_result(
                    "worker context and resolved browser adapter identities disagree",
                    effect=self.effect,
                    expected=context_compiled_digest,
                    observed=compiled_adapter_digest,
                )
        executable_adapter = canonical_compilation.executable
        if not isinstance(executable_adapter, BrowserChat):
            return ToolResult(
                content="campaign has no reviewed browser flow",
                is_error=True,
                metadata={"effect": self.effect},
            )
        adapter_identity_metadata: dict[str, object] = {
            "effect": self.effect,
            "adapter_digest": _reviewed_adapter_digest(adapter),
            "compiled_adapter_digest": compiled_adapter_digest,
            "target_profile_id": session_target_id,
        }
        compilation_uncertainties = _compilation_uncertainties(canonical_compilation)
        prompt = str(arguments["prompt"])
        if self._state_scope_store is None:
            return ToolResult(
                content=(
                    "compiled browser delivery requires durable state-scope storage; "
                    "the target was not called"
                ),
                is_error=True,
                metadata={
                    **adapter_identity_metadata,
                    "failure_code": "state_scope_unavailable",
                    "retryable": True,
                    "target_effect_attempted": False,
                    "target_call_budget_charge": 0,
                },
            )
        action_id = str(context.call_id or uuid4())
        preliminary_isolation_plan = resolve_state_isolation(
            executable_adapter.state_capabilities,
            checkpoint_available=context.objective_phase_checkpoint_intent == "restore",
        )
        state_scope = await _BrowserStateScope.open(
            self._state_scope_store,
            campaign_id=context.campaign_id,
            target_authority=_browser_target_authority(executable_adapter.endpoint),
            action_id=action_id,
            candidate_id=str(context.agent_id or action_id),
            experiment_id=_browser_experiment_id(context, action_id),
            action_kind=_browser_action_kind(context, "chat_exchange"),
            equivalence_key="conservative-shared-state",
            wait_for_lease_seconds=(
                2.0 if preliminary_isolation_plan.permits_parallel_delivery else 0.0
            ),
        )
        if state_scope is None:
            return ToolResult(
                content="reviewed browser state is already owned by another execution",
                is_error=True,
                metadata={
                    **adapter_identity_metadata,
                    "failure_code": "physical_state_unavailable",
                    "retryable": True,
                    "target_effect_attempted": False,
                    "target_call_budget_charge": 0,
                },
            )
        resource_block = await state_scope.preflight_resource_block(executable_adapter)
        if resource_block is not None:
            await state_scope.pause(MissingEvidenceReason.NOT_YET_OBSERVED)
            return ToolResult(
                content="reviewed browser target resource is unavailable before delivery",
                is_error=True,
                metadata={
                    **adapter_identity_metadata,
                    "failure_code": "target_resource_blocked",
                    "retryable": True,
                    "target_effect_attempted": False,
                    "target_call_budget_charge": 0,
                    "resource_state": resource_block.get("state"),
                    "resource_available": resource_block.get("available"),
                    "wake_at": resource_block.get("wake_at"),
                },
            )
        contract = adapter.get("interaction_contract")
        if isinstance(contract, Mapping):
            limit = contract.get("input_limit")
            maximum = limit.get("max_characters") if isinstance(limit, Mapping) else None
            if type(maximum) is int and maximum > 0 and len(prompt) > maximum:
                observation = {
                    "schema": "merlin.request-constraint-observation.v1",
                    "step_id": "browser-chat",
                    "evaluation_root": "request_body",
                    "path": "/prompt",
                    "kind": "string_length",
                    "maximum": maximum,
                    "unit": "unicode_characters",
                    "observed": len(prompt),
                    "satisfied": False,
                    "source": "reviewed_browser_interaction_contract",
                }
                await state_scope.pause(MissingEvidenceReason.NOT_APPLICABLE)
                return ToolResult(
                    content=(
                        "rendered browser prompt violates the reviewed input constraint; "
                        "the target was not called"
                    ),
                    is_error=True,
                    metadata={
                        **adapter_identity_metadata,
                        "attempt_disposition": "protocol_invalid",
                        "retryable": True,
                        "acknowledged_capability_evidence": False,
                        "target_effect_attempted": False,
                        "target_call_budget_charge": 0,
                        "limit_interaction_count": 0,
                        "request_constraint_observations": [observation],
                    },
                )
        if compilation_uncertainties:
            adapter_identity_metadata["evaluation_uncertainties"] = compilation_uncertainties
        endpoint_parts = urlsplit(executable_adapter.endpoint)
        endpoint_origin = f"{endpoint_parts.scheme.casefold()}://{endpoint_parts.netloc.casefold()}"
        open_tabs = [
            tab
            for tab in session.metadata().tabs
            if tab.state == "open"
            and (parts := urlsplit(tab.url)).scheme.casefold() in {"http", "https"}
            and f"{parts.scheme.casefold()}://{parts.netloc.casefold()}" == endpoint_origin
        ]
        compatible_tabs: list[tuple[str, BrowserObservation]] = []
        for tab in open_tabs:
            observation = await session.snapshot(tab.tab_ref)
            await self._recorder.append_observation(observation)
            if all(
                _resolve_reviewed_element(
                    observation,
                    compiled_step.arguments.get("locator"),
                    default_role=(
                        "button" if compiled_step.tool == "browser_click" else "textbox"
                    ),
                )
                is not None
                for compiled_step in executable_adapter.steps
            ):
                compatible_tabs.append((tab.tab_ref, observation))
        if len(compatible_tabs) > 1:
            exact_endpoint_tabs = [
                item
                for item in compatible_tabs
                if next(tab.url for tab in open_tabs if tab.tab_ref == item[0])
                == executable_adapter.endpoint
            ]
            if len(exact_endpoint_tabs) == 1:
                compatible_tabs = exact_endpoint_tabs
        if len(compatible_tabs) != 1:
            await state_scope.pause(MissingEvidenceReason.SELECTOR_UNAVAILABLE)
            return ToolResult(
                content=(
                    "compiled browser flow requires exactly one open target tab matching "
                    "the reviewed interaction contract"
                ),
                is_error=True,
                metadata=adapter_identity_metadata,
            )
        tab_ref, current = compatible_tabs[0]
        before_ref = _browser_evidence_ref("before", current)
        try:
            await state_scope.lifecycle.ensure_authoritative_before((before_ref,))
        except BaseException:
            await state_scope.pause(
                MissingEvidenceReason.SELECTOR_UNAVAILABLE,
                before_refs=(before_ref,),
            )
            raise
        action_ref = _browser_evidence_ref(
            "action", {"adapter_digest": compiled_adapter_digest, "prompt": prompt}
        )
        checkpoint_contract = (
            executable_adapter.state_capabilities.checkpoint
            if executable_adapter.state_capabilities is not None
            else None
        )
        try:
            checkpoint_context = _checkpoint_context(context)
        except ValueError as error:
            await state_scope.pause(
                MissingEvidenceReason.NOT_APPLICABLE,
                before_refs=(before_ref,),
                retryable=False,
            )
            return ToolResult(
                content=str(error),
                is_error=True,
                metadata={**adapter_identity_metadata, "failure_code": "checkpoint_contract"},
            )
        executable: list[tuple[str, dict[str, object], str | None, str]] = []
        for compiled_step in executable_adapter.steps:
            if (
                checkpoint_contract is not None
                and compiled_step.step_id == checkpoint_contract.restore_step_id
                and (checkpoint_context is None or checkpoint_context[0] != "restore")
            ):
                continue
            name = compiled_step.tool
            step_arguments = dict(compiled_step.arguments)
            default_role = "button" if name == "browser_click" else "textbox"
            locator = step_arguments.get("locator")
            resolved = _resolve_reviewed_element(
                current,
                locator,
                default_role=default_role,
            )
            if resolved is None:
                await state_scope.pause(
                    MissingEvidenceReason.SELECTOR_UNAVAILABLE,
                    before_refs=(before_ref,),
                )
                return ToolResult(
                    content=(
                        "reviewed browser flow locator does not identify exactly one current "
                        f"{default_role}: {locator!r}"
                    ),
                    is_error=True,
                    metadata={**adapter_identity_metadata, "retryable": False},
                )
            step_arguments["element_ref"], step_arguments["frame_ref"] = resolved
            step_arguments.pop("locator", None)
            step_arguments["tab_ref"] = tab_ref
            if name == "browser_fill" and (
                compiled_step.role.value == "interaction" or "value" not in step_arguments
            ):
                step_arguments["value"] = prompt
            executable.extend(
                (
                    str(name),
                    dict(step_arguments),
                    compiled_step.step_id,
                    compiled_step.role.value,
                )
                for _ in range(compiled_step.repeat)
            )
        names = tuple(name for name, _, _, _ in executable)
        if "browser_fill" not in names or not any(
            name in {"browser_click", "browser_press"} for name in names
        ):
            await state_scope.pause(
                MissingEvidenceReason.NOT_APPLICABLE,
                before_refs=(before_ref,),
                retryable=False,
            )
            return ToolResult(
                content="reviewed browser flow must contain exact fill and submit steps",
                is_error=True,
                metadata=adapter_identity_metadata,
            )
        timeout_ms = int(arguments.get("timeout_ms", 30_000))
        probe = contract.get("probe") if isinstance(contract, Mapping) else None
        response_witness = (
            str(probe.get("response_witness", "")).strip() if isinstance(probe, Mapping) else ""
        )
        response_frame = (
            _string(contract.get("frame_context")) if isinstance(contract, Mapping) else None
        )
        checkpoint_receipt: dict[str, object] | None = None
        restore_claimed = False
        restore_secret: str | None = None

        async def release_unacknowledged_restore() -> None:
            nonlocal restore_claimed
            if not restore_claimed or checkpoint_context is None:
                return
            await asyncio.to_thread(
                self._checkpoint_store.release_objective_gate_checkpoint_restore,  # type: ignore[union-attr]
                context.campaign_id,
                checkpoint_context[1],
                scope_id=state_scope.lifecycle.scope_id,
                claim_id=state_scope.claim_id,
                claimant_id=state_scope.claimant_id,
            )
            restore_claimed = False

        if checkpoint_context is not None:
            if (
                checkpoint_contract is None
                or self._checkpoint_store is None
                or self._checkpoint_vault is None
            ):
                await state_scope.pause(
                    MissingEvidenceReason.NOT_APPLICABLE,
                    before_refs=(before_ref,),
                    retryable=False,
                )
                return ToolResult(
                    content="reviewed checkpoint execution dependencies are unavailable",
                    is_error=True,
                    metadata={
                        **adapter_identity_metadata,
                        "failure_code": "checkpoint_contract",
                    },
                )
            intent, gate_id, recipe_digest, manifest_digest = checkpoint_context
            if intent == "restore":
                try:
                    reference_id = _checkpoint_reference_id(
                        context.objective_phase_checkpoint_reference
                    )
                    claimed = await asyncio.to_thread(
                        self._checkpoint_store.claim_objective_gate_checkpoint_restore,
                        context.campaign_id,
                        gate_id,
                        scope_id=state_scope.lifecycle.scope_id,
                        claim_id=state_scope.claim_id,
                        claimant_id=state_scope.claimant_id,
                    )
                    if (
                        str(claimed.get("manifest_digest")) != manifest_digest
                        or str(claimed.get("recipe_digest")) != recipe_digest
                    ):
                        raise ValueError("durable checkpoint does not match the sealed assignment")
                    durable_reference = claimed.get("checkpoint_handle")
                    if _checkpoint_reference_id(durable_reference) != reference_id:
                        raise ValueError(
                            "checkpoint assignment reference does not match durable state"
                        )
                    restore_secret = await asyncio.to_thread(
                        self._checkpoint_vault.resolve_checkpoint,
                        reference_id,
                    )
                    restore_claimed = True
                except (KeyError, ValueError) as error:
                    await state_scope.pause(
                        MissingEvidenceReason.NOT_YET_OBSERVED,
                        before_refs=(before_ref,),
                    )
                    return ToolResult(
                        content=str(error),
                        is_error=True,
                        metadata={
                            **adapter_identity_metadata,
                            "failure_code": "checkpoint_restore_unavailable",
                            "retryable": True,
                            "target_effect_attempted": False,
                            "target_call_budget_charge": 0,
                        },
                    )
                restored_steps = 0
                updated: list[tuple[str, dict[str, object], str | None, str]] = []
                try:
                    for name, step_arguments, step_id, role in executable:
                        if step_id == checkpoint_contract.restore_step_id:
                            assert restore_secret is not None
                            if name != "browser_fill" or not await session.fill_requires_redaction(
                                tab_ref,
                                str(step_arguments.get("element_ref") or ""),
                            ):
                                raise ValueError(
                                    "checkpoint restore requires a reviewed redacted browser fill"
                                )
                            injected = _inject_checkpoint_secret(
                                step_arguments,
                                checkpoint_contract.restore_request_pointer,
                                restore_secret,
                            )
                            if not isinstance(injected, dict):
                                raise ValueError("checkpoint restore step arguments are invalid")
                            step_arguments = injected
                            restored_steps += 1
                        updated.append((name, step_arguments, step_id, role))
                    if restored_steps != 1:
                        raise ValueError("checkpoint restore step must execute exactly once")
                except ValueError as error:
                    await release_unacknowledged_restore()
                    await state_scope.pause(
                        MissingEvidenceReason.NOT_APPLICABLE,
                        before_refs=(before_ref,),
                        retryable=False,
                    )
                    return ToolResult(
                        content=str(error),
                        is_error=True,
                        metadata={
                            **adapter_identity_metadata,
                            "failure_code": "checkpoint_contract",
                            "target_effect_attempted": False,
                            "target_call_budget_charge": 0,
                        },
                    )
                executable = updated
        isolation_plan = resolve_state_isolation(
            executable_adapter.state_capabilities,
            checkpoint_available=(
                checkpoint_context is not None and checkpoint_context[0] == "restore"
            ),
        )
        try:
            resource_reservations = await state_scope.reserve_resources(executable_adapter)
        except _BrowserResourceBlocked as error:
            await release_unacknowledged_restore()
            await state_scope.pause(
                MissingEvidenceReason.NOT_YET_OBSERVED,
                before_refs=(before_ref,),
            )
            return ToolResult(
                content="reviewed browser target resource is unavailable before delivery",
                is_error=True,
                metadata={
                    **adapter_identity_metadata,
                    "failure_code": "target_resource_blocked",
                    "retryable": True,
                    "target_effect_attempted": False,
                    "target_call_budget_charge": 0,
                    "resource_state": error.status.get("state"),
                    "resource_available": error.status.get("available"),
                    "wake_at": error.status.get("wake_at"),
                },
            )
        results: list[ToolResult] = []
        fill_step: tuple[str, str | None, str] | None = None
        resources_acknowledged = False
        captured_checkpoint_secret: str | None = None
        captured_checkpoint_evidence_digest: str | None = None
        checkpoint_step_documents: dict[str, object] = {}
        capability_step_ids = _browser_capability_step_ids(executable_adapter)
        isolation_bound = False
        exchange_started = time.monotonic()
        for step, (name, step_arguments, step_id, role) in enumerate(executable, start=1):
            if role == "interaction" and isolation_plan.isolated and not isolation_bound:
                equivalence_key, isolation_error = _browser_isolation_baseline(
                    executable_adapter,
                    isolation_plan,
                    checkpoint_step_documents,
                )
                rebound = bool(
                    equivalence_key is not None
                    and await state_scope.bind_isolated_state(
                        equivalence_key=equivalence_key,
                        isolation_disposition=isolation_plan.isolation_disposition,
                        maximum_parallel_states=isolation_plan.maximum_parallel_states,
                    )
                )
                if not rebound:
                    await state_scope.release_resources(resource_reservations)
                    await release_unacknowledged_restore()
                    await state_scope.pause(
                        MissingEvidenceReason.CAPTURE_INCOMPLETE,
                        before_refs=(before_ref,),
                    )
                    return ToolResult(
                        content=(
                            isolation_error or "reviewed browser physical state is already leased"
                        ),
                        is_error=True,
                        metadata={
                            **adapter_identity_metadata,
                            "failure_code": "physical_state_unavailable",
                            "attempt_disposition": "contaminated",
                            "retryable": True,
                            "target_effect_attempted": False,
                            "target_call_budget_charge": 0,
                        },
                    )
                isolation_bound = True
            if name in {"browser_click", "browser_press"} and fill_step is not None:
                element_ref, frame_ref, expected_value = fill_step
                try:
                    await session.verify_input_value(
                        tab_ref, element_ref, expected_value, frame_ref
                    )
                except BrowserBoundaryError as error:
                    await state_scope.release_resources(resource_reservations)
                    await release_unacknowledged_restore()
                    await state_scope.pause(
                        MissingEvidenceReason.CAPTURE_INCOMPLETE,
                        before_refs=(before_ref,),
                        action_refs=(action_ref,),
                    )
                    return ToolResult(
                        content=str(error),
                        is_error=True,
                        metadata={**adapter_identity_metadata, "retryable": False},
                    )
            if name in {"browser_click", "browser_press"}:
                await state_scope.acknowledge_resources(resource_reservations)
                resources_acknowledged = True
            step_arguments["step"] = step
            step_arguments["timeout_ms"] = timeout_ms
            result = await BrowserTool(name, self._resolver, self._recorder, self._failures).invoke(
                context, step_arguments
            )
            results.append(result)
            if result.is_error:
                if resources_acknowledged:
                    await state_scope.settle_resources(resource_reservations)
                else:
                    await state_scope.release_resources(resource_reservations)
                    await release_unacknowledged_restore()
                await state_scope.pause(
                    MissingEvidenceReason.TRANSPORT_FAILED,
                    before_refs=(before_ref,),
                    action_refs=(action_ref,),
                )
                return result.model_copy(
                    update={
                        "metadata": {
                            **result.metadata,
                            **adapter_identity_metadata,
                        }
                    }
                )
            if step_id is not None and step_id in capability_step_ids:
                state_observation = await session.snapshot(tab_ref, response_frame, step=step)
                step_document = state_observation.model_dump(mode="json")
                durable_observation = state_observation
                if (
                    checkpoint_context is not None
                    and checkpoint_context[0] == "capture"
                    and checkpoint_contract is not None
                    and step_id == checkpoint_contract.capture_selector.step_id
                ):
                    try:
                        candidate_secret = _json_pointer_value(
                            step_document,
                            checkpoint_contract.capture_selector.pointer,
                        )
                    except ValueError:
                        pass
                    else:
                        if isinstance(candidate_secret, str) and candidate_secret:
                            redacted_document = _inject_checkpoint_secret(
                                step_document,
                                checkpoint_contract.capture_selector.pointer,
                                "[checkpoint-secret-redacted]",
                            )
                            durable_observation = BrowserObservation.model_validate(
                                redacted_document
                            )
                await self._recorder.append_observation(durable_observation)
            else:
                try:
                    step_document = json.loads(result.content)
                except json.JSONDecodeError:
                    step_document = None
            if step_id is not None and step_document is not None:
                checkpoint_step_documents[step_id] = step_document
            if name == "browser_fill":
                fill_step = (
                    str(step_arguments["element_ref"]),
                    _string(step_arguments.get("frame_ref")),
                    str(step_arguments.get("value", "")),
                )
            if name in {"browser_click", "browser_press"}:
                await state_scope.lifecycle.acknowledge(
                    before_refs=(before_ref,),
                    action_refs=(action_ref,),
                )
                await state_scope.settle_resources(resource_reservations)
            if (
                checkpoint_context is not None
                and checkpoint_context[0] == "capture"
                and checkpoint_contract is not None
                and step_id == checkpoint_contract.capture_selector.step_id
            ):
                if (
                    checkpoint_contract.capture_timing == "post_success_response"
                    and role != "interaction"
                ):
                    raise ValueError(
                        "post-success checkpoint capture requires an interaction response"
                    )
                if step_document is None:
                    raise ValueError("checkpoint capture response is not structured")
                response_document = step_document
                capture_root = (
                    response_document
                    if checkpoint_contract.capture_selector.evaluation_root == "response_body"
                    else result.metadata.get("captures", {})
                )
                checkpoint_secret = _json_pointer_value(
                    capture_root,
                    checkpoint_contract.capture_selector.pointer,
                )
                if not isinstance(checkpoint_secret, str) or not checkpoint_secret:
                    raise ValueError("checkpoint capture selector did not resolve to a token")
                captured_checkpoint_secret = checkpoint_secret
                captured_checkpoint_evidence_digest = _checkpoint_evidence_digest(response_document)
        submitted = await session.snapshot(tab_ref, response_frame, step=len(executable) + 1)
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1_000
        after = submitted
        response_boundary_observed = submitted.text != current.text or bool(
            response_witness
            and submitted.text.count(response_witness) > current.text.count(response_witness)
        )
        response_boundary_complete = False
        last_response_change = asyncio.get_running_loop().time()
        response_settle_seconds = min(0.6, max(0.2, timeout_ms / 4_000))
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.2)
            candidate = await session.snapshot(tab_ref, response_frame, step=len(executable) + 2)
            if candidate.text != after.text:
                after = candidate
                response_boundary_observed = True
                last_response_change = asyncio.get_running_loop().time()
                continue
            if (
                response_boundary_observed
                and asyncio.get_running_loop().time() - last_response_change
                >= response_settle_seconds
            ):
                response_boundary_complete = True
                break
        public_after = after
        checkpoint_secret = captured_checkpoint_secret or restore_secret
        if checkpoint_secret is not None:
            public_after = BrowserObservation.model_validate(
                _redact_checkpoint_secret(
                    after.model_dump(mode="json"),
                    checkpoint_secret,
                )
            )
        await self._recorder.append_observation(public_after)
        await self._recorder.save_metadata(session.metadata(restart=True))
        after_ref = _browser_evidence_ref("after", public_after)
        exchange: dict[str, object] = {
            "id": "chat-exchange",
            "interaction": True,
            "cleanup": False,
            "request": {"prompt": prompt},
            "response": public_after.text,
            "outcome": "acknowledged",
            "response_boundary_observed": response_boundary_observed,
            "response_boundary_complete": response_boundary_complete,
            "latency_ms": max(0, int((time.monotonic() - exchange_started) * 1_000)),
        }
        evaluation_uncertainties: list[dict[str, object]] = []
        if not response_boundary_complete:
            evaluation_uncertainties.append(
                {
                    "kind": "evaluation_uncertain",
                    "source": "browser.response_boundary",
                    "reason": (
                        "The prompt was submitted and snapshots were retained, but the complete "
                        "post-submit response did not reach a stable boundary before timeout."
                    ),
                    "confidence": 0.0,
                }
            )
        predicates, predicate_error = _browser_oracle_predicates(
            contract if isinstance(contract, Mapping) else {}
        )
        if predicate_error is not None:
            evaluation_uncertainties.append(
                {
                    "kind": "evaluation_uncertain",
                    "source": "browser.oracle_predicates",
                    "reason": predicate_error,
                    "confidence": 0.0,
                }
            )
        if evaluation_uncertainties:
            exchange["evaluation_uncertain"] = True
            exchange["evaluation_uncertainties"] = evaluation_uncertainties
            exchange["evaluation_confidence"] = 0.0
        if predicates:
            oracle_response = json.loads(public_after.model_dump_json())
            oracle_response["response_witness_text"] = _response_witness_text(
                after.text, response_witness
            )
            observations = [
                _browser_oracle_observation(
                    _reviewed_adapter_digest(adapter),
                    str(exchange["id"]),
                    predicate,
                    oracle_response,
                )
                for predicate in predicates
            ]
            exchange["oracle_observations"] = observations
            # Preserve the PAIR v1 singleton projection while the multi-predicate
            # list is the authoritative adaptive lane.
            exchange["oracle_observation"] = next(
                (item for item in observations if item.get("purpose") == "objective"),
                observations[0],
            )
        gate_succeeded, gate_transition = _browser_checkpoint_gate_succeeded(
            context,
            executable_adapter,
            checkpoint_step_documents,
        )
        state_transition = _browser_state_transition(
            executable_adapter,
            checkpoint_step_documents,
        )
        if state_transition:
            exchange["target_state_transition"] = state_transition
        if isolation_plan.isolated and state_transition.get("identity_match") is not True:
            await release_unacknowledged_restore()
            state_scope.isolation_disposition = "contaminated"
            await state_scope.settle(
                before_refs=(before_ref,),
                action_refs=(action_ref,),
                response_refs=(after_ref,),
                after_refs=(after_ref,),
                transition_disposition="ambiguous",
                cleanup_disposition="retained",
            )
            return ToolResult(
                content="reviewed browser physical-state identity changed during delivery",
                is_error=True,
                metadata={
                    **adapter_identity_metadata,
                    "failure_code": "physical_state_contaminated",
                    "attempt_disposition": "contaminated",
                    "retryable": True,
                    "target_effect_attempted": True,
                    "target_call_budget_charge": 1,
                    "target_state_transition": state_transition,
                    "target_exchanges": [exchange],
                },
            )
        if checkpoint_context is not None:
            assert checkpoint_contract is not None
            assert self._checkpoint_store is not None
            assert self._checkpoint_vault is not None
            intent, gate_id, recipe_digest, manifest_digest = checkpoint_context
            if (
                not state_scope.lifecycle.acknowledged
                or not response_boundary_complete
                or not gate_succeeded
            ):
                await state_scope.pause(
                    MissingEvidenceReason.CAPTURE_INCOMPLETE,
                    before_refs=(before_ref,),
                    action_refs=(action_ref,),
                )
                return ToolResult(
                    content=(
                        "checkpoint retention requires an acknowledged, authoritative "
                        "successful prerequisite response"
                    ),
                    is_error=True,
                    metadata={
                        **adapter_identity_metadata,
                        "failure_code": "checkpoint_success_unproven",
                        "retryable": True,
                        "target_effect_attempted": True,
                        "target_state_transition": gate_transition,
                        "target_exchanges": [exchange],
                    },
                )
            if intent == "capture":
                if (
                    captured_checkpoint_secret is None
                    or captured_checkpoint_evidence_digest is None
                ):
                    await state_scope.pause(
                        MissingEvidenceReason.CAPTURE_INCOMPLETE,
                        before_refs=(before_ref,),
                        action_refs=(action_ref,),
                    )
                    return ToolResult(
                        content="reviewed checkpoint capture selector produced no handle",
                        is_error=True,
                        metadata={
                            **adapter_identity_metadata,
                            "failure_code": "checkpoint_capture_missing",
                            "retryable": True,
                        },
                    )
                entry = await asyncio.to_thread(
                    self._checkpoint_vault.store_checkpoint,
                    captured_checkpoint_secret,
                    campaign_id=context.campaign_id,
                    gate_id=gate_id,
                )
                opaque_reference = entry.opaque_reference()
                try:
                    await asyncio.to_thread(
                        self._checkpoint_store.record_objective_gate_checkpoint,
                        context.campaign_id,
                        gate_id,
                        manifest_digest=manifest_digest,
                        recipe_digest=recipe_digest,
                        checkpoint_handle=opaque_reference,
                        evidence_digest=captured_checkpoint_evidence_digest,
                        scope_id=state_scope.lifecycle.scope_id,
                        claim_id=state_scope.claim_id,
                        claimant_id=state_scope.claimant_id,
                    )
                except BaseException:
                    keyed_digest = entry.keyed_digest()
                    await asyncio.to_thread(
                        self._checkpoint_vault.delete_checkpoint,
                        entry.reference_id,
                        expected_digest=str(keyed_digest["digest"]),
                    )
                    raise
                checkpoint_receipt = {
                    "gate_id": gate_id,
                    "recipe_digest": recipe_digest,
                    "checkpoint_reference": opaque_reference,
                    "evidence_digest": captured_checkpoint_evidence_digest,
                    "captured": True,
                }
            else:
                assert restore_claimed
                restore_evidence_digest = _checkpoint_evidence_digest(public_after)
                await asyncio.to_thread(
                    self._checkpoint_store.mark_objective_gate_checkpoint_restored,
                    context.campaign_id,
                    gate_id,
                    scope_id=state_scope.lifecycle.scope_id,
                    claim_id=state_scope.claim_id,
                    claimant_id=state_scope.claimant_id,
                    evidence_digest=restore_evidence_digest,
                )
                checkpoint_receipt = {
                    "gate_id": gate_id,
                    "recipe_digest": recipe_digest,
                    "checkpoint_reference": dict(
                        context.objective_phase_checkpoint_reference or {}
                    ),
                    "evidence_digest": restore_evidence_digest,
                    "restored": True,
                }
            captured_checkpoint_secret = None
            restore_secret = None
        await state_scope.settle(
            before_refs=(before_ref,),
            action_refs=(action_ref,),
            response_refs=(after_ref,),
            after_refs=(after_ref,),
            transition_disposition=("unchanged" if response_boundary_complete else "ambiguous"),
            cleanup_disposition="retained",
        )
        return ToolResult(
            content=public_after.model_dump_json(),
            metadata={
                **adapter_identity_metadata,
                "compiled_flow": "merlin.browser-flow.v1",
                "steps_completed": len(results),
                "input_value_verified": True,
                "response_witness": response_witness,
                "response_boundary_observed": response_boundary_observed,
                "response_boundary_complete": response_boundary_complete,
                "attempt_disposition": "valid" if response_boundary_complete else "uncertain",
                "target": session.bounds.target_id,
                "target_session_id": f"browser:{session.context_id}",
                "target_exchanges": [exchange],
                **(
                    {"objective_phase_checkpoint": checkpoint_receipt}
                    if checkpoint_receipt is not None
                    else {}
                ),
            },
        )


async def _context_api_exchange_events(
    session: BrowserSession, recorder: BrowserRecorder
) -> tuple[dict[str, object], ...]:
    """Return only exchanges captured inside the current durable target context."""
    merged: list[dict[str, object]] = []
    positions: dict[str, int] = {}

    def append_exchange(event: Mapping[str, object]) -> None:
        identity = json.dumps(
            {
                key: event.get(key)
                for key in ("method", "url", "status", "request_json", "response_json")
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        existing = positions.get(identity)
        if existing is None:
            positions[identity] = len(merged)
            merged.append(dict(event))
            return
        # A live copy can enrich the durable exchange with its native response
        # timestamp. Preserve its original position in the lifecycle.
        merged[existing] = {**merged[existing], **dict(event)}

    list_observations = getattr(recorder, "list_observations", None)
    if callable(list_observations):
        observations = list_observations(session.context_id)
        resolved = await observations if isawaitable(observations) else observations
        for observation in resolved:
            if (
                not isinstance(observation, BrowserObservation)
                or observation.context_id != session.context_id
                or observation.target_id != session.bounds.target_id
                or observation.owner != session.owner
                or observation.kind != "network"
            ):
                continue
            try:
                captured = json.loads(observation.text)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(captured, list):
                continue
            for event in captured:
                if not isinstance(event, Mapping):
                    continue
                enriched = dict(event)
                if "request_json" not in enriched:
                    if (
                        enriched.get("method") != "GET"
                        or not isinstance(enriched.get("url"), str)
                        or type(enriched.get("status")) is not int
                        or "response_json" not in enriched
                    ):
                        continue
                    enriched["request_json"] = None
                enriched["_context_id"] = observation.context_id
                enriched["_target_id"] = observation.target_id
                enriched["_observation_step"] = observation.step
                enriched["_observation_tab_ref"] = observation.tab_ref
                enriched["_observation_observed_at"] = observation.observed_at.isoformat()
                append_exchange(enriched)
    for event in session.api_exchange_events():
        enriched = dict(event)
        enriched["_context_id"] = session.context_id
        enriched["_target_id"] = session.bounds.target_id
        append_exchange(enriched)
    return tuple(merged)


def _physical_session_segment(
    exchanges: tuple[dict[str, object], ...],
    event_index: int,
) -> tuple[
    tuple[dict[str, object], ...],
    int,
    str | None,
    tuple[tuple[str, str, str], ...],
    bool,
]:
    """Isolate the lifecycle that owns the selected interaction request."""
    interaction = exchanges[event_index]
    request_scalars = _string_scalar_paths(interaction.get("request_json"))
    sourced_by_request_pointer: dict[str, tuple[str, ...]] = {}
    for request_pointer, request_value in request_scalars:
        response_pointers = tuple(
            dict.fromkeys(
                response_pointer
                for earlier in exchanges[:event_index]
                for response_pointer, response_value in _string_scalar_paths(
                    earlier.get("response_json")
                )
                if response_value == request_value
                and _capture_name_compatible(response_pointer, request_pointer)
            )
        )
        if response_pointers:
            sourced_by_request_pointer[request_pointer] = response_pointers
    sourced_values = {
        request_value
        for request_pointer, request_value in request_scalars
        if request_pointer in sourced_by_request_pointer
    }
    if not sourced_values:
        return exchanges, event_index, None, (), False
    request_bindings = tuple(
        (request_pointer, request_value)
        for request_pointer, request_value in request_scalars
        if request_pointer in sourced_by_request_pointer
    )
    setup_candidates = [
        index
        for index, candidate in enumerate(exchanges[:event_index])
        if all(
            request_value in _string_scalar_values(candidate.get("response_json"))
            for _, request_value in request_bindings
        )
    ]
    if not setup_candidates:
        return exchanges, event_index, None, (), True
    setup_index = min(
        setup_candidates,
        key=lambda index: (
            str(exchanges[index].get("method") or "").upper() in {"GET", "HEAD"},
            any(
                value in _string_scalar_values(exchanges[index].get("request_json"))
                for _, value in request_bindings
            ),
            -index,
        ),
    )
    setup_response_paths = _string_scalar_paths(exchanges[setup_index].get("response_json"))
    lifecycle_bindings: list[tuple[str, str, str]] = []
    for request_pointer, request_value in request_bindings:
        response_pointer = next(
            (
                pointer
                for pointer, value in setup_response_paths
                if value == request_value and _capture_name_compatible(pointer, request_pointer)
            ),
            None,
        )
        if response_pointer is None:
            return exchanges, event_index, None, (), True
        lifecycle_bindings.append((request_pointer, response_pointer, request_value))
    primary_binding = next(
        (binding for binding in lifecycle_bindings if "session" in binding[0].casefold()),
        lifecycle_bindings[0],
    )
    remote_session_id = primary_binding[2]
    segment_start = setup_index
    setup_url = exchanges[setup_index].get("url")
    for index in range(setup_index - 1, -1, -1):
        candidate = exchanges[index]
        candidate_response = candidate.get("response_json")
        if (
            str(candidate.get("method") or "").upper() in {"POST", "PUT", "PATCH"}
            and isinstance(candidate_response, Mapping)
            and candidate_response.get("resumed") is False
            and remote_session_id not in _string_scalar_values(candidate_response)
        ):
            break
        if (
            candidate.get("method") == "GET"
            and candidate.get("status") == 404
            and _json_pointer_value_or_missing(candidate.get("response_json"), "/error")
            == "no_active_session"
            and _same_lifecycle_path_family(candidate.get("url"), setup_url)
        ):
            segment_start = index

    segment_end = len(exchanges)
    session_request_pointers = {
        pointer for pointer, value in request_scalars if value == remote_session_id
    }
    for index in range(event_index + 1, len(exchanges)):
        candidate = exchanges[index]
        response_scalars = _string_scalar_paths(candidate.get("response_json"))
        introduces_other_session = any(
            value != remote_session_id
            and any(
                _capture_name_compatible(response_pointer, request_pointer)
                for request_pointer in session_request_pointers
            )
            for response_pointer, value in response_scalars
        )
        response = candidate.get("response_json")
        if (
            introduces_other_session
            and str(candidate.get("method") or "").upper() in {"POST", "PUT", "PATCH"}
            and isinstance(response, Mapping)
            and response.get("resumed") is False
        ):
            segment_end = index
            break

    selected = exchanges[segment_start:segment_end]
    return (
        selected,
        event_index - segment_start,
        remote_session_id,
        tuple(lifecycle_bindings),
        False,
    )


def _captured_api_evidence(
    session: BrowserSession, event: Mapping[str, Any], part: str
) -> tuple[str, dict[str, object]]:
    """Derive a context/target-bound durable reference from one captured exchange part."""
    if part not in {"request", "response"}:
        raise ValueError("captured API evidence part must be request or response")
    exchange = {
        "context_id": session.context_id,
        "target_id": session.bounds.target_id,
        "method": event.get("method"),
        "url": event.get("url"),
        "status": event.get("status"),
        "request_json": event.get("request_json"),
        "response_json": event.get("response_json"),
    }
    exchange_digest = hashlib.sha256(
        json.dumps(exchange, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    body_digest = hashlib.sha256(
        json.dumps(event.get(f"{part}_json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload: dict[str, object] = {
        "schema": "merlin.browser.api-evidence.v1",
        "context_id": session.context_id,
        "target_id": session.bounds.target_id,
        "method": str(event.get("method") or ""),
        "url": str(event.get("url") or ""),
        "status": int(event.get("status") or 0),
        "exchange_digest": f"sha256:{exchange_digest}",
        "part": part,
        "body_digest": f"sha256:{body_digest}",
    }
    reference_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"browser-api-{part}:sha256:{reference_digest}", payload


def _browser_action_evidence(action: BrowserAction) -> tuple[str, dict[str, object]]:
    """Return the exact evidence identity emitted by BrowserSession for an action."""
    reference = BrowserSession._evidence(action)  # noqa: SLF001
    return reference, action.model_dump(mode="json")


async def _reviewed_interaction_input(
    session: BrowserSession,
    recorder: BrowserRecorder,
    event: Mapping[str, object],
    observed_input: str,
) -> bool:
    """Prove a captured browser request used the latest successful filled value."""
    input_pointer = event.get("_resolved_input_pointer")
    if not isinstance(input_pointer, str):
        return False

    async def reviewed_native_receipt() -> bool:
        reader = getattr(recorder, "reviewed_native_input_receipt", None)
        if not callable(reader):
            return False
        value = reader(session.context_id, event, input_pointer)
        receipt = await value if isawaitable(value) else value
        return bool(
            isinstance(receipt, Mapping)
            and receipt.get("context_id") == session.context_id
            and receipt.get("target_id") == session.bounds.target_id
            and receipt.get("input_pointer") == input_pointer
            and isinstance(receipt.get("evidence_ref"), str)
            and str(receipt["evidence_ref"]).startswith("native-request:sha256:")
        )

    list_actions = getattr(recorder, "list_actions", None)
    list_observations = getattr(recorder, "list_observations", None)
    if not callable(list_actions) or not callable(list_observations):
        return await reviewed_native_receipt()
    input_action_refs = event.get("input_action_refs")
    input_probe_refs = event.get("input_probe_refs")
    trigger_action_ref = event.get("trigger_action_ref")
    if (
        not isinstance(input_action_refs, Mapping)
        or not isinstance(input_probe_refs, Mapping)
        or not isinstance(trigger_action_ref, str)
    ):
        return await reviewed_native_receipt()
    input_action_ref = input_action_refs.get(input_pointer)
    input_probe_ref = input_probe_refs.get(input_pointer)
    if not isinstance(input_action_ref, str) or not isinstance(input_probe_ref, str):
        return False
    actions_value = list_actions(session.context_id)
    observations_value = list_observations(session.context_id)
    actions = await actions_value if isawaitable(actions_value) else actions_value
    observations = (
        await observations_value if isawaitable(observations_value) else observations_value
    )
    joined_actions: dict[str, BrowserAction] = {}
    for action in actions:
        if (
            not isinstance(action, BrowserAction)
            or action.context_id != session.context_id
            or action.target_id != session.bounds.target_id
            or action.owner != session.owner
            or action.kind not in {"fill", "click", "press"}
        ):
            continue
        action_ref = _browser_action_evidence(action)[0]
        if action_ref not in {input_action_ref, trigger_action_ref}:
            continue
        if any(
            isinstance(observation, BrowserObservation)
            and observation.context_id == session.context_id
            and observation.target_id == session.bounds.target_id
            and observation.owner == session.owner
            and observation.tab_ref == action.tab_ref
            and observation.step == action.step
            and action_ref in observation.evidence_refs
            for observation in observations
        ):
            joined_actions[action_ref] = action
    fill = joined_actions.get(input_action_ref)
    submit = joined_actions.get(trigger_action_ref)
    return bool(
        fill is not None
        and submit is not None
        and fill.kind == "fill"
        and input_probe_ref in fill.evidence_refs
        and submit.kind in {"click", "press"}
        and fill.tab_ref == submit.tab_ref
        and (fill.step, fill.created_at) < (submit.step, submit.created_at)
        and observed_input
    )


async def _reviewed_dom_input_constraint(
    session: BrowserSession,
    recorder: BrowserRecorder,
    event: Mapping[str, object],
    input_pointer: str,
) -> tuple[dict[str, object], tuple[str, dict[str, object]]] | None:
    """Derive a string-length constraint from the exact reviewed DOM input."""
    list_actions = getattr(recorder, "list_actions", None)
    list_observations = getattr(recorder, "list_observations", None)
    input_action_refs = event.get("input_action_refs")
    if (
        not callable(list_actions)
        or not callable(list_observations)
        or not isinstance(input_action_refs, Mapping)
    ):
        return None
    input_action_ref = input_action_refs.get(input_pointer)
    if not isinstance(input_action_ref, str):
        return None

    actions_value = list_actions(session.context_id)
    observations_value = list_observations(session.context_id)
    actions = await actions_value if isawaitable(actions_value) else actions_value
    observations = (
        await observations_value if isawaitable(observations_value) else observations_value
    )
    fills = [
        action
        for action in actions
        if isinstance(action, BrowserAction)
        and action.context_id == session.context_id
        and action.target_id == session.bounds.target_id
        and action.owner == session.owner
        and action.kind == "fill"
        and action.element_ref is not None
        and (
            input_action_ref in action.evidence_refs
            or _browser_action_evidence(action)[0] == input_action_ref
        )
    ]
    if len(fills) != 1:
        return None
    fill = fills[0]
    matches = [
        (observation, element)
        for observation in observations
        if isinstance(observation, BrowserObservation)
        and observation.context_id == session.context_id
        and observation.target_id == session.bounds.target_id
        and observation.owner == session.owner
        and observation.tab_ref == fill.tab_ref
        and observation.observed_at <= fill.created_at
        for element in observation.elements
        if element.ref == fill.element_ref
        and element.role.casefold() == "textbox"
        and element.max_length is not None
    ]
    if not matches:
        return None
    observation, element = max(matches, key=lambda item: item[0].observed_at)
    maximum = element.max_length
    assert maximum is not None
    payload: dict[str, object] = {
        "schema": "merlin.browser.input-constraint-evidence.v1",
        "context_id": session.context_id,
        "target_id": session.bounds.target_id,
        "tab_ref": fill.tab_ref,
        "frame_ref": element.frame_ref,
        "element_ref": element.ref,
        "role": element.role,
        "input_action_ref": input_action_ref,
        "observation_step": observation.step,
        "constraint": {
            "kind": "string_length",
            "maximum": maximum,
            "unit": "unicode_characters",
            "source": "dom_attribute",
        },
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    evidence_ref = f"browser-input-constraint:sha256:{digest}"
    return (
        {
            "path": input_pointer,
            "step_id": "observed-message-exchange",
            "evaluation_root": "request_body",
            "kind": "string_length",
            "maximum": maximum,
            "unit": "unicode_characters",
            "source": "dom_attribute",
            "evidence_refs": [evidence_ref],
            "evidence_bindings": [
                {"evidence_ref": evidence_ref, "observed_maximum": maximum}
            ],
            "confidence": 1.0,
        },
        (evidence_ref, payload),
    )


async def _reviewed_cleanup_action(
    session: BrowserSession,
    recorder: BrowserRecorder,
    cleanup_event: Mapping[str, object],
    evidence_ref: object,
) -> tuple[str, dict[str, object]] | None:
    """Verify a visible same-context action preceded the cleanup response."""
    if evidence_ref is not None and (
        not isinstance(evidence_ref, str) or not evidence_ref.startswith("browser-action:sha256:")
    ):
        return None
    list_actions = getattr(recorder, "list_actions", None)
    list_observations = getattr(recorder, "list_observations", None)
    if not callable(list_actions) or not callable(list_observations):
        return None
    actions_value = list_actions(session.context_id)
    observations_value = list_observations(session.context_id)
    actions = await actions_value if isawaitable(actions_value) else actions_value
    observations = (
        await observations_value if isawaitable(observations_value) else observations_value
    )
    trigger_action_ref = cleanup_event.get("trigger_action_ref")
    bound_evidence_ref = evidence_ref or (
        trigger_action_ref if isinstance(trigger_action_ref, str) else None
    )
    if bound_evidence_ref is None:
        return None
    candidate_actions = tuple(
        action
        for action in actions
        if isinstance(action, BrowserAction)
        and action.context_id == session.context_id
        and action.target_id == session.bounds.target_id
        and action.owner == session.owner
        and action.kind in {"click", "press", "select", "check"}
        and action.element_ref is not None
        and (
            bound_evidence_ref is None or _browser_action_evidence(action)[0] == bound_evidence_ref
        )
    )
    typed_observations = tuple(
        observation
        for observation in observations
        if isinstance(observation, BrowserObservation)
        and observation.context_id == session.context_id
        and observation.target_id == session.bounds.target_id
        and observation.owner == session.owner
    )
    observed_at = cleanup_event.get("observed_at")
    try:
        cleanup_observed_at = (
            datetime.fromisoformat(observed_at) if isinstance(observed_at, str) else None
        )
    except ValueError:
        cleanup_observed_at = None
    if cleanup_observed_at is None:
        return None
    matching_actions = []
    for action in candidate_actions:
        action_ref = _browser_action_evidence(action)[0]
        visible_before = any(
            observation.kind == "snapshot"
            and observation.tab_ref == action.tab_ref
            and (action.frame_ref is None or observation.frame_ref == action.frame_ref)
            and observation.observed_at <= action.created_at
            and any(
                element.ref == action.element_ref and not element.disabled
                for element in observation.elements
            )
            for observation in typed_observations
        )
        successful_action = any(
            action_ref in observation.evidence_refs
            and observation.tab_ref == action.tab_ref
            and observation.step == action.step
            and observation.observed_at >= action.created_at
            for observation in typed_observations
        )
        if visible_before and action.created_at < cleanup_observed_at and successful_action:
            matching_actions.append(action)
    if len(matching_actions) != 1:
        return None
    return _browser_action_evidence(matching_actions[0])


def _normalized_lifecycle_endpoint(url: object) -> tuple[str, str, str, str] | None:
    """Normalize one HTTP lifecycle endpoint for exact evidence comparison."""
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    return (
        parsed.scheme.casefold(),
        parsed.netloc.casefold(),
        posixpath.normpath(unquote(parsed.path)),
        parsed.query,
    )


def _same_lifecycle_path_family(first_url: object, second_url: object) -> bool:
    """Return whether two lifecycle endpoints share an origin and concrete parent path."""
    first = _normalized_lifecycle_endpoint(first_url)
    second = _normalized_lifecycle_endpoint(second_url)
    if first is None or second is None:
        return False
    first_path, second_path = first[2], second[2]
    first_parent = tuple(part for part in first_path.split("/") if part)[:-1]
    second_parent = tuple(part for part in second_path.split("/") if part)[:-1]
    return first[:2] == second[:2] and bool(first_parent) and first_parent == second_parent


async def _reviewed_latest_setup_action(
    session: BrowserSession,
    recorder: BrowserRecorder,
    setup_event: Mapping[str, object],
) -> tuple[str, dict[str, object]] | None:
    """Bind setup to the exact visible action recorded as triggering its request."""
    list_actions = getattr(recorder, "list_actions", None)
    list_observations = getattr(recorder, "list_observations", None)
    if not callable(list_actions) or not callable(list_observations):
        return None
    actions_value = list_actions(session.context_id)
    observations_value = list_observations(session.context_id)
    actions = await actions_value if isawaitable(actions_value) else actions_value
    observations = (
        await observations_value if isawaitable(observations_value) else observations_value
    )
    observed_at_raw = setup_event.get("observed_at")
    setup_step = setup_event.get("_observation_step")
    setup_tab_ref = setup_event.get("_observation_tab_ref")
    trigger_action_ref = setup_event.get("trigger_action_ref")
    try:
        observed_at = (
            datetime.fromisoformat(observed_at_raw) if isinstance(observed_at_raw, str) else None
        )
    except ValueError:
        observed_at = None
    if (
        observed_at is None
        or type(setup_step) is not int
        or not isinstance(setup_tab_ref, str)
        or not setup_tab_ref
        or not isinstance(trigger_action_ref, str)
        or not trigger_action_ref
    ):
        return None
    typed_observations = tuple(
        observation
        for observation in observations
        if isinstance(observation, BrowserObservation)
        and observation.context_id == session.context_id
        and observation.target_id == session.bounds.target_id
        and observation.owner == session.owner
    )
    eligible: list[BrowserAction] = []
    for action in actions:
        if (
            not isinstance(action, BrowserAction)
            or action.context_id != session.context_id
            or action.target_id != session.bounds.target_id
            or action.owner != session.owner
            or action.kind not in {"click", "press", "select", "check"}
            or action.element_ref is None
            or action.tab_ref != setup_tab_ref
            or not action.created_at < observed_at
            or action.step > setup_step
        ):
            continue
        action_ref = _browser_action_evidence(action)[0]
        if action_ref != trigger_action_ref:
            continue
        visible_elements = tuple(
            element
            for observation in typed_observations
            if observation.kind == "snapshot"
            and observation.tab_ref == action.tab_ref
            and (action.frame_ref is None or observation.frame_ref == action.frame_ref)
            and observation.observed_at <= action.created_at
            for element in observation.elements
            if element.ref == action.element_ref and not element.disabled
        )
        visible_before = bool(visible_elements) and not any(
            re.search(r"\b(?:resum\w*|restart\w*|continu\w*|recover\w*)\b", element.name, re.I)
            for element in visible_elements
        )
        action_outcomes = tuple(
            observation
            for observation in typed_observations
            if observation.observed_at >= action.created_at
            and action_ref in observation.evidence_refs
        )
        successful_action = not action_outcomes or all(
            observation.tab_ref == action.tab_ref
            and observation.step == action.step
            and observation.kind != "blocker"
            for observation in action_outcomes
        )
        # The captured network exchange already carries this exact action ref and
        # occurred after the visible click.  A browser wait may time out after the
        # request succeeds; failure diagnostics contain screenshot/network refs,
        # not a contradictory browser-action outcome.  Preserve exact conflicting
        # action outcomes as a reason to refuse ownership.
        if visible_before and successful_action:
            eligible.append(action)
    if len(eligible) != 1:
        return None
    return _browser_action_evidence(eligible[0])


class CompiledApiReconHandoffTool:
    """Compile a reviewed single-exchange API adapter from captured network evidence."""

    name = "browser_compile_api_handoff"
    effect = "harness_state"

    def __init__(self, resolver: BrowserSessionResolver, recorder: BrowserRecorder) -> None:
        self._resolver = resolver
        self._recorder = recorder

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                "Compile and seal an HTTP API lifecycle from exact captured JSON evidence. "
                "First complete the bounded same-origin frontend/network inventory, then capture "
                "setup and one harmless message/response. The message must be entered with "
                "browser_fill connection_probe=true before it is submitted, so the captured "
                "request can be linked to the reviewed input field. Compile that interaction "
                "before ending its session. Include cleanup only when an exact "
                "post-message cleanup exchange was observed; when the target exposes a visible "
                "session-ending action (forfeit, end, reset), capture that exchange by ending "
                "the disposable session created by this same reconnaissance context through "
                "browser_end_owned_session. Supply only the "
                "small semantic contract; Merlin derives session "
                "capture/reuse and the single/multi-turn classification from durable evidence "
                "and binds evidence references from the captured exchange. Never "
                "invent cleanup or label setup/resume as cleanup. Session concurrency and "
                "interaction limits remain explicitly unverified unless captured evidence "
                "establishes them; one observed session is not proof of a concurrency limit."
                " Classify non-input request values as constants, response/session captures, or "
                "client-generated per-message values. Supply request_bindings for generated "
                "values using exact JSON Pointers and observed format (uuid4 or hex32). Compare "
                "two distinct harmless messages within the authorized recon budget when needed; "
                "never treat a session ID, credential, server-issued token, or merely UUID-shaped "
                "constant as a request nonce. Unknown semantics remain recorded uncertainty."
                " Describe request constraints and target-authored state observations only when "
                "an exact captured response proves them; Merlin verifies their evidence selectors "
                "and binds durable evidence references. Field names are target data, never "
                "hardcoded runtime semantics. Declare state_identity only for a target-authored "
                "physical mutable-state identifier observed before and after interaction; never "
                "promote a cookie, lease, credential, or logical session token."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "method": {"type": "string", "enum": ["POST", "PUT", "PATCH"]},
                    "url": {"type": "string", "minLength": 1},
                    "input_pointer": {
                        "type": "string",
                        "pattern": "^(?:/|\\$\\.)",
                        "description": (
                            "Path to the prompt field in the captured request. Prefer RFC 6901 "
                            "JSON Pointer such as /message; simple JSONPath such as $.message is "
                            "also accepted and normalized."
                        ),
                    },
                    "request_bindings": {
                        "type": "object",
                        "additionalProperties": {"type": "string", "enum": ["uuid4", "hex32"]},
                        "description": (
                            "Optional request JSON Pointers to client-generated per-message "
                            "values, "
                            "e.g. /envelope/nonce: uuid4. Exact pointers are preferred; an "
                            "unambiguous leaf field name is also accepted and canonicalized to its "
                            "exact pointer. Bindings must be based on frontend or multiple "
                            "exchange "
                            "evidence. A fresh value is persisted before each request; transport "
                            "retries reuse that value. Session captures and input fields cannot be "
                            "overridden."
                        ),
                    },
                    "response_selector": {
                        "description": (
                            "Optional path to response text. Use empty for the whole JSON, /reply "
                            "for JSON Pointer, or $.reply for simple JSONPath. Invalid or stale "
                            "hints retain the complete response and become evaluation_uncertain."
                        ),
                    },
                    "success_witness": {
                        "description": "Optional evaluator hint; it never gates sealing."
                    },
                    "oracle_predicate": {
                        "type": "object",
                        "description": (
                            "Optional evaluator hint. Malformed, missing, or contradictory oracle "
                            "metadata never blocks sealing the executable transport; Merlin "
                            "records "
                            "the disagreement for validator judgment."
                        ),
                        "additionalProperties": True,
                    },
                    "terminal_response_contract": {
                        "type": "object",
                        "description": (
                            "Optional reviewed lifecycle contract. Use conditions when the target "
                            "has both an authoritative post-effect terminal response and a later "
                            "request-rejected terminal error. Each condition must use exact "
                            "observed status codes and finite response values. An applied effect "
                            "is never resent; automatic resend of a rejected request additionally "
                            "requires replay_safe=true. This lifecycle contract becomes executable "
                            "only when the operator approves the sealed campaign manifest."
                        ),
                        "properties": {
                            "pointer": {"type": "string", "pattern": "^(?:|/.*)$"},
                            "match_values": {
                                "type": "array",
                                "items": {"type": ["string", "number", "integer", "boolean"]},
                                "minItems": 1,
                                "uniqueItems": True,
                            },
                            "effect_applied": {"const": False},
                            "replay_safe": {"type": "boolean"},
                            "status_codes": {
                                "type": "array",
                                "items": {"type": "integer", "minimum": 100, "maximum": 599},
                                "minItems": 1,
                                "uniqueItems": True,
                            },
                            "conditions": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "pointer": {"type": "string", "pattern": "^(?:|/.*)$"},
                                        "match_values": {
                                            "type": "array",
                                            "items": {
                                                "type": [
                                                    "string",
                                                    "number",
                                                    "integer",
                                                    "boolean",
                                                ]
                                            },
                                            "minItems": 1,
                                            "uniqueItems": True,
                                        },
                                        "status_codes": {
                                            "type": "array",
                                            "items": {
                                                "type": "integer",
                                                "minimum": 100,
                                                "maximum": 599,
                                            },
                                            "minItems": 1,
                                            "uniqueItems": True,
                                        },
                                        "effect_applied": {"type": "boolean"},
                                        "replay_safe": {"type": "boolean"},
                                    },
                                    "required": [
                                        "pointer",
                                        "match_values",
                                        "status_codes",
                                        "effect_applied",
                                        "replay_safe",
                                    ],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "oneOf": [
                            {
                                "required": [
                                    "pointer",
                                    "match_values",
                                    "effect_applied",
                                    "replay_safe",
                                ]
                            },
                            {"required": ["conditions"]},
                        ],
                        "additionalProperties": False,
                    },
                    "authentication": {"type": "string", "minLength": 1},
                    "session_constraint": {"type": "string", "minLength": 1},
                    "recon_scope": {
                        "type": "object",
                        "description": (
                            "Platform-neutral scenario identity used for safe future reuse. "
                            "Supply every known dimension after reconnaissance."
                        ),
                        "properties": {
                            name: {"type": "string", "minLength": 1}
                            for name in (
                                "surface",
                                "authentication_context",
                                "tenant",
                                "agent_or_bot",
                                "scenario",
                                "session_constraint",
                                "oracle_contract",
                            )
                        },
                        "required": [
                            "surface",
                            "authentication_context",
                            "agent_or_bot",
                        ],
                        "additionalProperties": False,
                    },
                    "cleanup_method": {
                        "type": "string",
                        "enum": ["POST", "PUT", "PATCH", "DELETE"],
                    },
                    "cleanup_url": {"type": "string", "minLength": 1},
                    "cleanup_action_evidence_ref": {
                        "type": "string",
                        "pattern": "^browser-action:sha256:[0-9a-f]{64}$",
                        "description": (
                            "Optional exact selector for the durable visible reviewed exit "
                            "action. Cleanup always requires one unique same-context action "
                            "ordered before its request and acknowledged response."
                        ),
                    },
                    "cleanup_acknowledgement": {
                        "type": "object",
                        "description": (
                            "Required reviewed predicate for the exact captured cleanup response. "
                            "Merlin never guesses cleanup success from a status or arbitrary "
                            "response scalar."
                        ),
                        "properties": {
                            "pointer": {"type": "string", "pattern": "^(?:|/.*)$"},
                            "match_values": {
                                "type": "array",
                                "items": {
                                    "type": ["string", "number", "integer", "boolean", "null"]
                                },
                                "minItems": 1,
                                "uniqueItems": True,
                            },
                            "status_codes": {
                                "type": "array",
                                "items": {"type": "integer", "minimum": 200, "maximum": 299},
                                "minItems": 1,
                                "uniqueItems": True,
                            },
                        },
                        "required": ["pointer", "match_values", "status_codes"],
                        "additionalProperties": False,
                    },
                    "session_creation_transition": {
                        "type": "object",
                        "description": (
                            "Optional reviewed proof that this disposable context created the "
                            "captured remote session when setup does not return resumed=false. "
                            "It binds an exact no-active precondition, setup response, and "
                            "same-session postcondition."
                        ),
                        "properties": {
                            "precondition": {"type": "object"},
                            "postcondition": {"type": "object"},
                        },
                        "required": ["precondition", "postcondition"],
                        "additionalProperties": False,
                    },
                    "request_constraints": {
                        "type": "array",
                        "description": (
                            "Optional input constraints established by an exact captured response. "
                            "The evidence selector is used only during reconnaissance and is "
                            "replaced by durable evidence references in the sealed adapter."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "pattern": "^/"},
                                "step_id": {"const": "observed-message-exchange"},
                                "evaluation_root": {"const": "request_body"},
                                "kind": {"const": "string_length"},
                                "maximum": {"type": "integer", "minimum": 1},
                                "unit": {"const": "unicode_characters"},
                                "source": {
                                    "type": "string",
                                    "enum": [
                                        "server_validation_response",
                                        "openapi_schema",
                                        "client_validation",
                                    ],
                                },
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                "evidence_selector": {
                                    "type": "object",
                                    "properties": {
                                        "method": {"type": "string"},
                                        "url": {"type": "string", "minLength": 1},
                                        "status_codes": {
                                            "type": "array",
                                            "items": {
                                                "type": "integer",
                                                "minimum": 100,
                                                "maximum": 599,
                                            },
                                            "minItems": 1,
                                            "uniqueItems": True,
                                        },
                                        "pointer": {"type": "string", "pattern": "^(?:|/.*)$"},
                                        "match_values": {
                                            "type": "array",
                                            "items": {
                                                "type": [
                                                    "string",
                                                    "number",
                                                    "integer",
                                                    "boolean",
                                                    "null",
                                                ]
                                            },
                                            "minItems": 1,
                                            "uniqueItems": True,
                                        },
                                    },
                                    "required": ["status_codes", "pointer", "match_values"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": [
                                "path",
                                "step_id",
                                "evaluation_root",
                                "kind",
                                "maximum",
                                "unit",
                                "source",
                                "confidence",
                                "evidence_selector",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "state_observations": {
                        "type": "array",
                        "description": (
                            "Optional target-authored state fields. Each pointer must exist in the "
                            "captured response for its declared lifecycle step."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "minLength": 1},
                                "role": {
                                    "type": "string",
                                    "enum": [
                                        "state_identity",
                                        "progress_ordinal",
                                        "score",
                                        "budget_remaining",
                                        "terminal_status",
                                        "custom",
                                    ],
                                },
                                "baseline_selector": {"$ref": "#/$defs/state_selector"},
                                "result_selector": {"$ref": "#/$defs/state_selector"},
                                "value_type": {
                                    "type": "string",
                                    "enum": ["integer", "number", "string", "boolean"],
                                },
                                "minimum_value": {"type": "number"},
                                "maximum_value": {"type": "number"},
                                "monotonicity": {
                                    "type": "string",
                                    "enum": ["nondecreasing", "nonincreasing"],
                                },
                                "trust_class": {
                                    "const": "target_reported",
                                    "description": (
                                        "Browser API response fields are target-reported. "
                                        "Host-observed and independent-authority trust require "
                                        "separate reviewed evidence sources."
                                    ),
                                },
                                "preferred_direction": {
                                    "type": "string",
                                    "enum": ["increase", "decrease"],
                                },
                                "success_values": {
                                    "type": "array",
                                    "items": {
                                        "type": [
                                            "string",
                                            "number",
                                            "integer",
                                            "boolean",
                                            "null",
                                        ]
                                    },
                                    "uniqueItems": True,
                                },
                                "failure_values": {
                                    "type": "array",
                                    "items": {
                                        "type": [
                                            "string",
                                            "number",
                                            "integer",
                                            "boolean",
                                            "null",
                                        ]
                                    },
                                    "uniqueItems": True,
                                },
                            },
                            "required": [
                                "name",
                                "role",
                                "baseline_selector",
                                "result_selector",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "state_enumerations": {
                        "type": "array",
                        "description": (
                            "Optional reviewed finite target-authored member domains. The "
                            "selector must exist in its captured lifecycle response."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "pattern": "^[a-z][a-z0-9_]*$",
                                },
                                "selector": {"$ref": "#/$defs/state_selector"},
                                "shape": {
                                    "type": "string",
                                    "enum": ["flat", "scoped"],
                                },
                                "scope_dimension": {
                                    "type": "string",
                                    "pattern": "^[a-z][a-z0-9_]*$",
                                },
                                "completeness": {
                                    "type": "string",
                                    "enum": ["complete", "partial"],
                                },
                                "confidence": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 1,
                                },
                            },
                            "required": [
                                "name",
                                "selector",
                                "shape",
                                "completeness",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "identity_equivalence_rules": {
                        "type": "array",
                        "description": (
                            "Reviewed equality links that prove physical-state continuity when "
                            "the target rotates its displayed identifier."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "pattern": "^[a-z][a-z0-9_]*$",
                                },
                                "relation": {"const": "equal"},
                                "left_selector": {"$ref": "#/$defs/state_selector"},
                                "right_selector": {"$ref": "#/$defs/state_selector"},
                            },
                            "required": [
                                "name",
                                "relation",
                                "left_selector",
                                "right_selector",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "corroboration_rules": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "pattern": "^[a-z][a-z0-9_]*$",
                                },
                                "observation_names": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 2,
                                    "uniqueItems": True,
                                },
                                "minimum_agreement": {"type": "integer", "minimum": 2},
                            },
                            "required": [
                                "name",
                                "observation_names",
                                "minimum_agreement",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "resource_ownership": {
                        "type": "array",
                        "description": (
                            "Reviewed bindings from quota, rate-limit, or budget observations to "
                            "a principal at the compiled adapter origin."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "pattern": "^[a-z][a-z0-9_]*$",
                                },
                                "resource_kind": {
                                    "type": "string",
                                    "enum": ["quota", "rate_limit", "budget"],
                                },
                                "resource_observation_name": {
                                    "type": "string",
                                    "pattern": "^[a-z][a-z0-9_]*$",
                                },
                                "principal_selector": {"$ref": "#/$defs/state_selector"},
                                "reset_selector": {"$ref": "#/$defs/state_selector"},
                            },
                            "required": [
                                "name",
                                "resource_kind",
                                "resource_observation_name",
                                "principal_selector",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "supported_modalities": {
                        "type": "array",
                        "description": (
                            "Request modalities demonstrated by the captured interaction. "
                            "Unsupported or unbound declarations remain advisory."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "modality": {
                                    "type": "string",
                                    "enum": ["text", "image", "audio", "file", "mixed"],
                                },
                                "request_pointer": {"type": "string", "pattern": "^/"},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "required": ["modality", "request_pointer", "confidence"],
                            "additionalProperties": False,
                        },
                    },
                    "state_affordances": {
                        "type": "array",
                        "description": (
                            "State-management operations independently demonstrated by exact "
                            "captured request/response exchanges before the selected interaction."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": ["clone_state", "restore_checkpoint", "reset_state"],
                                },
                                "method": {
                                    "type": "string",
                                    "enum": ["POST", "PUT", "PATCH", "DELETE"],
                                },
                                "url": {"type": "string", "minLength": 1},
                                "status_codes": {
                                    "type": "array",
                                    "items": {"type": "integer", "minimum": 200, "maximum": 299},
                                    "minItems": 1,
                                    "uniqueItems": True,
                                },
                                "pointer": {"type": "string", "pattern": "^(?:|/.*)$"},
                                "identity_pointer": {
                                    "type": "string",
                                    "pattern": "^/",
                                    "description": (
                                        "For clone_state, the new physical-state identity in "
                                        "the clone response. It must differ from baseline and "
                                        "match the selected interaction identity."
                                    ),
                                },
                                "match_values": {
                                    "type": "array",
                                    "items": {
                                        "type": ["string", "number", "integer", "boolean", "null"]
                                    },
                                    "minItems": 1,
                                    "uniqueItems": True,
                                },
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "required": [
                                "kind",
                                "method",
                                "url",
                                "status_codes",
                                "pointer",
                                "match_values",
                                "confidence",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "checkpoint": {
                        "type": "object",
                        "properties": {
                            "capture_selector": {"$ref": "#/$defs/state_selector"},
                            "capture_timing": {
                                "type": "string",
                                "enum": ["post_success_response", "live_handle"],
                            },
                            "binding_name": {
                                "type": "string",
                                "pattern": "^[a-z][a-z0-9_]*$",
                            },
                            "restore_request_pointer": {"type": "string", "pattern": "^/"},
                        },
                        "required": [
                            "capture_selector",
                            "capture_timing",
                            "binding_name",
                            "restore_request_pointer",
                        ],
                        "additionalProperties": False,
                    },
                    "resource_controls": {
                        "type": "array",
                        "description": (
                            "Executable quota or rate controls bound to ownership plus an exact "
                            "captured structured block response."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "ownership_name": {
                                    "type": "string",
                                    "pattern": "^[a-z][a-z0-9_]*$",
                                },
                                "limit": {"type": "integer", "minimum": 1},
                                "window_key": {"type": "string", "minLength": 1},
                                "status_codes": {
                                    "type": "array",
                                    "items": {"type": "integer", "minimum": 400, "maximum": 599},
                                    "minItems": 1,
                                    "uniqueItems": True,
                                },
                                "retry_after_seconds_pointer": {
                                    "type": "string",
                                    "pattern": "^(?:|/.*)$",
                                },
                                "wake_at_pointer": {
                                    "type": "string",
                                    "pattern": "^(?:|/.*)$",
                                },
                            },
                            "required": ["ownership_name", "limit", "window_key", "status_codes"],
                            "oneOf": [
                                {"required": ["retry_after_seconds_pointer"]},
                                {"required": ["wake_at_pointer"]},
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                "$defs": {
                    "state_selector": {
                        "type": "object",
                        "properties": {
                            "step_id": {
                                "type": "string",
                                "enum": [
                                    "observed-session-setup",
                                    "observed-message-exchange",
                                    "observed-cleanup-exchange",
                                ],
                            },
                            "evaluation_root": {"const": "response_body"},
                            "pointer": {"type": "string", "pattern": "^/"},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "required": ["step_id", "evaluation_root", "pointer"],
                        "additionalProperties": False,
                    }
                },
                "required": [
                    "method",
                    "url",
                    "input_pointer",
                    "authentication",
                    "session_constraint",
                ],
                "additionalProperties": False,
            },
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        resolved = self._resolver(context.session_id)
        session = await resolved if isawaitable(resolved) else resolved
        derived = await self._compile_reviewed_adapter(session, arguments)
        if isinstance(derived, ToolResult):
            return derived
        adapter = derived.adapter
        method, url = derived.method, derived.url
        success_witness = derived.success_witness
        request_evidence_ref = derived.request_evidence_ref
        response_evidence_ref = derived.response_evidence_ref
        adapter_json = json.dumps(adapter, sort_keys=True, separators=(",", ":"))
        supplied_scope = arguments.get("recon_scope")
        if isinstance(supplied_scope, Mapping):
            recon_scope = {
                str(key): str(value).strip()
                for key, value in supplied_scope.items()
                if isinstance(value, str) and value.strip()
            }
        else:
            parsed_url = urlsplit(url)
            path_parts = tuple(part for part in parsed_url.path.split("/") if part)
            agent_or_bot = next(
                (
                    part
                    for part in reversed(path_parts)
                    if part.casefold() not in {"api", "message", "chat", "send"}
                ),
                parsed_url.hostname or "reviewed-api",
            )
            recon_scope = {
                "surface": f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}",
                "authentication_context": str(arguments["authentication"]),
                "agent_or_bot": agent_or_bot,
                "session_constraint": str(arguments["session_constraint"]),
                "oracle_contract": success_witness,
            }
        reusable_site_facts = json.dumps(
            {
                "origin": f"{urlsplit(url).scheme}://{urlsplit(url).netloc}",
                "api_transport": adapter["api_transport"],
                "authentication": arguments["authentication"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        identity = "|".join(
            (
                method,
                url,
                request_evidence_ref,
                response_evidence_ref,
                adapter_json,
            )
        )
        handoff = ApiReconHandoff(
            id=f"handoff:{uuid5(NAMESPACE_URL, identity)}",
            context_id=session.context_id,
            target_id=session.bounds.target_id,
            owner=session.owner,
            method=method,
            url=url,
            request_evidence_ref=request_evidence_ref,
            response_evidence_ref=response_evidence_ref,
            adapter=adapter_json,
            reusable_adapter=(
                None
                if _passive_session_observation_only(adapter) or derived.stateful_reuse_is_unsafe
                else adapter_json
            ),
            compiled_adapter=derived.compiled_adapter,
            compiled_adapter_digest=derived.compiled_adapter_digest,
            reusable_site_facts=reusable_site_facts,
            recon_scope=recon_scope,
            recon_observations=derived.recon_observations,
        )
        record_evidence = getattr(self._recorder, "record_evidence", None)
        if callable(record_evidence):
            evidence_records = {
                evidence_ref: payload
                for evidence_ref, payload in (
                    *derived.setup_evidence,
                    *derived.creation_transition_evidence,
                    (request_evidence_ref, derived.request_evidence),
                    (response_evidence_ref, derived.response_evidence),
                    *derived.terminal_evidence,
                    *derived.cleanup_evidence,
                    *derived.capability_evidence,
                )
            }
            for evidence_ref, payload in evidence_records.items():
                await record_evidence(
                    session.context_id,
                    evidence_ref,
                    (
                        "browser_action"
                        if evidence_ref.startswith("browser-action:sha256:")
                        else "browser_api_exchange"
                    ),
                    payload,
                )
        await self._recorder.record_handoff(handoff)
        cleanup_discovery = adapter.get("cleanup_discovery")
        preferred_next_tool = (
            "browser_end_owned_session"
            if isinstance(cleanup_discovery, Mapping)
            and cleanup_discovery.get("status") == "required_before_target_execution"
            and cleanup_discovery.get("session_ownership") == "owned"
            else "assessment_prepare"
        )
        return ToolResult(
            content=handoff.model_dump_json(),
            metadata={
                "effect": self.effect,
                "target": handoff.target_id,
                "compiled_adapter": True,
                # Cleanup discovery remains advisory: the handoff is sealed and
                # executable either way. When this context provably owns a fresh
                # disposable session, direct reconnaissance to its visible exit so
                # the superseding handoff can retain the complete lifecycle.
                "preferred_next_tool": preferred_next_tool,
                **(
                    {"cleanup_discovery": adapter["cleanup_discovery"]}
                    if "cleanup_discovery" in adapter
                    else {}
                ),
            },
        )

    async def _compile_reviewed_adapter(
        self, session: BrowserSession, arguments: Mapping[str, Any]
    ) -> ToolResult | _DerivedApiHandoff:
        """Derive the executable http_api adapter from captured exchange evidence.

        Shared by browser_compile_api_handoff (model-reviewed semantic contract)
        and the browser_record_api_handoff DOM-fallback branch (no model-supplied
        arguments: default response selector, no oracle), so one derivation core
        serves both seal paths.
        """
        method, url = str(arguments["method"]), str(arguments["url"])
        try:
            requested_input_pointer = _normalize_json_pointer(str(arguments["input_pointer"]))
        except ValueError as error:
            return ToolResult(content=str(error), is_error=True, metadata={"effect": self.effect})
        requested_selector = arguments.get("response_selector", "")
        selector_enabled = isinstance(requested_selector, str) and (
            not requested_selector or requested_selector.startswith("/")
        )

        def selector_satisfied(candidate: Mapping[str, object]) -> bool:
            if not selector_enabled:
                return True
            try:
                _json_pointer_value(candidate.get("response_json"), requested_selector)
            except ValueError:
                return False
            return True

        selected: tuple[int, dict[str, object], str] | None = None
        input_error: str | None = None
        exchanges: tuple[dict[str, object], ...] = ()
        # A newer provenance-linked response may still be merging into the
        # durable capture, so re-read the exchanges once before failing.
        for _attempt in range(2):
            exchanges = await _context_api_exchange_events(session, self._recorder)
            matches = [
                (index, event)
                for index, event in enumerate(exchanges)
                if event.get("method") == method and event.get("url") == url
            ]
            if not matches:
                return ToolResult(
                    content="no exact captured JSON exchange matches the requested API endpoint",
                    is_error=True,
                    metadata={"effect": self.effect},
                )
            matches.sort(
                key=lambda item: (
                    str(
                        item[1].get("observed_at") or item[1].get("_observation_observed_at") or ""
                    ),
                    item[0],
                ),
                reverse=True,
            )
            candidates: list[tuple[int, dict[str, object], str]] = []
            for candidate_index, candidate in matches:
                request_json = candidate.get("request_json")
                if not isinstance(request_json, (dict, list)):
                    input_error = input_error or (
                        "captured request body is not reusable structured JSON"
                    )
                    continue
                input_pointer = requested_input_pointer
                observed_input = _json_pointer_value_or_missing(request_json, input_pointer)
                if observed_input is _MISSING:
                    alias_pointer = {
                        "/message": "/userMessage",
                        "/userMessage": "/message",
                    }.get(input_pointer)
                    if (
                        alias_pointer is None
                        or not isinstance(request_json, Mapping)
                        or not isinstance(
                            request_json.get(alias_pointer.removeprefix("/")),
                            str,
                        )
                    ):
                        input_error = input_error or (
                            "input_pointer does not exist in the captured request"
                        )
                        continue
                    input_pointer = alias_pointer
                    observed_input = request_json[alias_pointer.removeprefix("/")]
                if not isinstance(observed_input, str) or not observed_input.strip():
                    input_error = input_error or (
                        "input_pointer must identify the non-empty text sent to the reviewed target"
                    )
                    continue
                candidates.append((candidate_index, candidate, input_pointer))
            # The response selector picks among otherwise-valid candidates, as
            # it always has; it never discards the only captured exchange.
            ordered = [item for item in candidates if selector_satisfied(item[1])] or candidates
            for candidate_index, candidate, input_pointer in ordered:
                observed_input = _json_pointer_value_or_missing(
                    candidate["request_json"], input_pointer
                )
                if not await _reviewed_interaction_input(
                    session,
                    self._recorder,
                    {**candidate, "_resolved_input_pointer": input_pointer},
                    observed_input if isinstance(observed_input, str) else "",
                ):
                    continue
                if candidate.get("_response_pending") is True:
                    return ToolResult(
                        content=(
                            "the reviewed API request was sent and its response is still "
                            "pending; poll browser_network_events and retry compilation in "
                            "this same browser session. Do not clean up or create another "
                            "target session."
                        ),
                        is_error=True,
                        metadata={
                            "effect": self.effect,
                            "failure_code": "interaction_response_pending",
                            "retryable": True,
                            "preferred_next_tool": "browser_network_events",
                            "effect_applied": True,
                            "target_call_budget_charge": 0,
                        },
                    )
                selected = candidate_index, candidate, input_pointer
                break
            if selected is not None:
                break
        if selected is None:
            if input_error is not None:
                return ToolResult(
                    content=input_error,
                    is_error=True,
                    metadata={"effect": self.effect},
                )
            return ToolResult(
                content=(
                    "input_pointer is not linked to the latest successful browser fill action. "
                    "Use browser_fill with connection_probe=true, submit once in the same live "
                    "session, wait for its response, then compile before cleanup."
                ),
                is_error=True,
                metadata={
                    "effect": self.effect,
                    "failure_code": "input_provenance_missing",
                    "retryable": True,
                    "effect_applied": False,
                    "target_call_budget_charge": 0,
                },
            )
        event_index, event, input_pointer = selected
        request_json = event["request_json"]
        try:
            request_template = _replace_json_pointer(
                request_json,
                input_pointer,
                "{{input}}",
            )
            # Some browser clients mirror the visible prompt under ``message``
            # while the reviewed API consumes ``userMessage``.  Leaving either
            # exact top-level prompt alias sealed to the reconnaissance text can
            # make later language/mutation probes look delivered even though the
            # target received the old baseline.  When both aliases are present,
            # bind both to the one runtime input; unrelated reviewed fields (for
            # example sessionId, levelId, and control flags) remain untouched.
            request_template = _bind_ambiguous_message_aliases(request_template)
        except ValueError as error:
            return ToolResult(content=str(error), is_error=True, metadata={"effect": self.effect})

        all_context_exchanges = exchanges
        (
            exchanges,
            event_index,
            physical_session_id,
            physical_session_bindings,
            ambiguous_physical_session,
        ) = _physical_session_segment(exchanges, event_index)
        if ambiguous_physical_session:
            return ToolResult(
                content=(
                    "the selected interaction request contains response-sourced identities "
                    "that do not originate from one physical-session setup; capture one "
                    "unambiguous lifecycle"
                ),
                is_error=True,
                metadata={
                    "effect": self.effect,
                    "failure_code": "ambiguous_physical_session_identity",
                    "retryable": True,
                },
            )
        event = exchanges[event_index]
        request_json = event.get("request_json")
        assert isinstance(request_json, (dict, list))
        physical_session_pointers = {
            request_pointer for request_pointer, _, _ in physical_session_bindings
        }
        capture_names: dict[str, str] = {}
        used_capture_names = {"session_id"}
        for _, response_pointer, value in physical_session_bindings:
            if value == physical_session_id:
                capture_names[value] = "session_id"
                continue
            leaf = response_pointer.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
            candidate_name = re.sub(r"(?<!^)(?=[A-Z])", "_", leaf).casefold()
            candidate_name = re.sub(r"[^a-z0-9_]+", "_", candidate_name).strip("_")
            if not candidate_name:
                candidate_name = "lifecycle_id"
            base_name = candidate_name
            suffix = 2
            while candidate_name in used_capture_names:
                candidate_name = f"{base_name}_{suffix}"
                suffix += 1
            used_capture_names.add(candidate_name)
            capture_names[value] = candidate_name
        observed_lifecycle_tuples = {
            tuple(
                _json_pointer_value_or_missing(candidate.get("response_json"), response_pointer)
                for _, response_pointer, _ in physical_session_bindings
            )
            for candidate in all_context_exchanges
            if all(
                isinstance(
                    _json_pointer_value_or_missing(
                        candidate.get("response_json"), response_pointer
                    ),
                    str,
                )
                for _, response_pointer, _ in physical_session_bindings
            )
        }
        multiple_physical_sessions = len(observed_lifecycle_tuples) > 1

        def belongs_to_physical_session(
            candidate: Mapping[str, object], *, require_positive: bool = False
        ) -> bool:
            if physical_session_id is None or not physical_session_pointers:
                return True
            candidate_paths = tuple(
                item
                for body in (candidate.get("request_json"), candidate.get("response_json"))
                for item in _string_scalar_paths(body)
            )
            matched_bindings = 0
            for request_pointer, response_pointer, expected_value in physical_session_bindings:
                observed_values = {
                    value
                    for pointer, value in candidate_paths
                    if _capture_name_compatible(pointer, request_pointer)
                    or _capture_name_compatible(pointer, response_pointer)
                }
                if observed_values and observed_values != {expected_value}:
                    return False
                if observed_values == {expected_value}:
                    matched_bindings += 1
            if require_positive:
                return matched_bindings > 0
            return True

        evaluation_uncertainties: list[dict[str, object]] = []
        raw_response_selector = arguments.get("response_selector", "")
        try:
            response_selector = _normalize_json_pointer(
                raw_response_selector if isinstance(raw_response_selector, str) else "",
                allow_empty=True,
            )
        except ValueError:
            response_selector = ""
            evaluation_uncertainties.append(
                _evaluation_uncertainty(
                    "browser_compile_api_handoff.response_selector",
                    "The response selector was not a JSON Pointer; the complete captured "
                    "response was retained for validator judgment.",
                )
            )
        response_json = event.get("response_json")
        try:
            _json_pointer_value(response_json, response_selector)
        except ValueError:
            evaluation_uncertainties.append(
                _evaluation_uncertainty(
                    "browser_compile_api_handoff.response_selector",
                    f"The response selector {response_selector} was absent from the captured "
                    "response; the complete response was retained for validator judgment.",
                )
            )
            response_selector = ""
        success_witness = arguments.get("success_witness")
        if not isinstance(success_witness, str) or not success_witness.strip():
            success_witness = "validator judgment over the captured target response"
            evaluation_uncertainties.append(
                _evaluation_uncertainty(
                    "browser_compile_api_handoff.success_witness",
                    "No usable success-witness hint was supplied; executable transport and "
                    "captured telemetry were retained for validator judgment.",
                )
            )
        oracle_predicate: dict[str, object] | None = None
        oracle_input = arguments.get("oracle_predicate")
        if oracle_input is None:
            evaluation_uncertainties.append(
                _evaluation_uncertainty(
                    "browser_compile_api_handoff.oracle",
                    "No target-native objective oracle was supplied; validator judgment is "
                    "required.",
                )
            )
        elif not isinstance(oracle_input, Mapping):
            evaluation_uncertainties.append(
                _evaluation_uncertainty(
                    "browser_compile_api_handoff.oracle",
                    "The objective oracle hint was not an object; validator judgment is required.",
                )
            )
        else:
            required_oracle_fields = {
                "oracle_id",
                "pointer",
                "match_values",
                "modality",
                "turn",
                "language",
            }
            missing_oracle_fields = sorted(required_oracle_fields - oracle_input.keys())
            oracle_pointer = oracle_input.get("pointer")
            match_values = oracle_input.get("match_values")
            if missing_oracle_fields:
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.oracle",
                        "The objective oracle hint is incomplete; missing: "
                        + ", ".join(missing_oracle_fields)
                        + ".",
                    )
                )
            elif not isinstance(oracle_pointer, str) or (
                oracle_pointer and not oracle_pointer.startswith("/")
            ):
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.oracle",
                        "The objective oracle pointer is not a JSON Pointer.",
                    )
                )
            elif (
                not isinstance(match_values, list)
                or not match_values
                or any(not _json_scalar(candidate) for candidate in match_values)
            ):
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.oracle",
                        "The objective oracle match values are not finite JSON scalar values.",
                    )
                )
            else:
                try:
                    observed_oracle_value = _json_pointer_value(response_json, oracle_pointer)
                except ValueError:
                    evaluation_uncertainties.append(
                        _evaluation_uncertainty(
                            "browser_compile_api_handoff.oracle",
                            "The objective oracle pointer was absent from the captured response.",
                        )
                    )
                else:
                    if any(
                        type(candidate) is type(observed_oracle_value)
                        and candidate == observed_oracle_value
                        for candidate in match_values
                    ):
                        evaluation_uncertainties.append(
                            _evaluation_uncertainty(
                                "browser_compile_api_handoff.oracle",
                                "The objective oracle also matches the ordinary reconnaissance "
                                "response and cannot independently prove success.",
                            )
                        )
                    else:
                        oracle_id = str(oracle_input["oracle_id"])
                        oracle_predicate = {
                            "oracle_digest": "sha256:"
                            + hashlib.sha256(oracle_id.encode()).hexdigest(),
                            "pointer": oracle_pointer,
                            "match_values": list(match_values),
                            "modality": str(oracle_input["modality"]),
                            "turn": str(oracle_input["turn"]),
                            "language": str(oracle_input["language"]),
                        }
        terminal_response_contract: dict[str, object] | None = None
        terminal_condition_events: list[
            tuple[dict[str, object], tuple[Mapping[str, Any], ...]]
        ] = []
        terminal_input = arguments.get("terminal_response_contract")
        if terminal_input is not None:
            if not isinstance(terminal_input, Mapping):
                return ToolResult(
                    content="terminal response contract must be a reviewed object",
                    is_error=True,
                    metadata={"effect": self.effect},
                )
            raw_conditions = terminal_input.get("conditions")
            conditions = raw_conditions if isinstance(raw_conditions, list) else [terminal_input]
            normalized_conditions: list[dict[str, object]] = []
            invalid_condition = raw_conditions is not None and (
                not isinstance(raw_conditions, list) or not raw_conditions
            )
            for condition in conditions:
                if not isinstance(condition, Mapping):
                    invalid_condition = True
                    break
                terminal_pointer = condition.get("pointer")
                terminal_values = condition.get("match_values")
                status_codes = condition.get("status_codes", [])
                effect_applied = condition.get("effect_applied")
                replay_safe = condition.get("replay_safe")
                if (
                    not isinstance(terminal_pointer, str)
                    or (terminal_pointer and not terminal_pointer.startswith("/"))
                    or not isinstance(terminal_values, list)
                    or not terminal_values
                    or any(not _json_scalar(candidate) for candidate in terminal_values)
                    or not isinstance(status_codes, list)
                    or (raw_conditions is not None and not status_codes)
                    or any(
                        type(status) is not int or not 100 <= status <= 599
                        for status in status_codes
                    )
                    or not isinstance(effect_applied, bool)
                    or not isinstance(replay_safe, bool)
                    or (effect_applied and replay_safe)
                ):
                    invalid_condition = True
                    break
                matching_terminal_events = tuple(
                    exchange
                    for observed_index, exchange in enumerate(exchanges)
                    if observed_index >= event_index
                    and exchange.get("method") == method
                    and exchange.get("url") == url
                    and exchange.get("status") in status_codes
                    and belongs_to_physical_session(
                        exchange,
                        require_positive=multiple_physical_sessions,
                    )
                    if (
                        observed_value := _json_pointer_value_or_missing(
                            exchange.get("response_json"), terminal_pointer
                        )
                    )
                    is not _MISSING
                    and any(
                        type(observed_value) is type(expected_value)
                        and observed_value == expected_value
                        for expected_value in terminal_values
                    )
                )
                if raw_conditions is not None and not matching_terminal_events:
                    invalid_condition = True
                    break
                normalized_condition: dict[str, object] = {
                    "pointer": terminal_pointer,
                    "match_values": list(terminal_values),
                    "effect_applied": effect_applied,
                    "replay_safe": replay_safe,
                    "predicate_observation": {
                        "source": (
                            "observed_terminal_exchange"
                            if matching_terminal_events
                            else "reviewed_explicit_contract"
                        ),
                        "evidence_refs": [],
                    },
                    "effect_attestation": {
                        "source": "operator_reviewed_contract",
                    },
                }
                if status_codes:
                    normalized_condition["status_codes"] = list(status_codes)
                normalized_conditions.append(normalized_condition)
                terminal_condition_events.append((normalized_condition, matching_terminal_events))
            if invalid_condition:
                return ToolResult(
                    content=(
                        "terminal response conditions require a JSON Pointer, finite match values, "
                        "finite HTTP status codes, and explicit effect_applied/replay_safe "
                        "booleans; "
                        "an applied effect cannot be replay safe"
                    ),
                    is_error=True,
                    metadata={"effect": self.effect},
                )
            if raw_conditions is None:
                terminal_response_contract = normalized_conditions[0]
            else:
                terminal_response_contract = {"conditions": normalized_conditions}
        cleanup_fields = ("cleanup_method", "cleanup_url")
        supplied_cleanup = tuple(field for field in cleanup_fields if arguments.get(field))
        if supplied_cleanup and len(supplied_cleanup) != len(cleanup_fields):
            return ToolResult(
                content="cleanup must be omitted or supplied as one complete observed contract",
                is_error=True,
                metadata={"effect": self.effect},
            )
        cleanup_event: Mapping[str, Any] | None = None
        cleanup_method = cleanup_url = ""
        cleanup_request: object = {}
        cleanup_acknowledgement: dict[str, object] | None = None
        cleanup_acknowledgement_source = ""
        cleanup_action_evidence: tuple[str, dict[str, object]] | None = None
        if arguments.get("cleanup_acknowledgement") is not None and not supplied_cleanup:
            return ToolResult(
                content="cleanup acknowledgement requires an exact observed cleanup exchange",
                is_error=True,
                metadata={"effect": self.effect},
            )
        if supplied_cleanup:
            cleanup_method, cleanup_url = (
                str(arguments["cleanup_method"]),
                str(arguments["cleanup_url"]),
            )
            cleanup_matches = tuple(
                candidate
                for index, candidate in enumerate(exchanges)
                if index > event_index
                and candidate.get("method") == cleanup_method
                and candidate.get("url") == cleanup_url
                and belongs_to_physical_session(
                    candidate,
                    require_positive=multiple_physical_sessions,
                )
            )
            if not cleanup_matches:
                return ToolResult(
                    content=(
                        "no exact post-interaction cleanup exchange matches the requested API "
                        "endpoint; omit cleanup when it is not established"
                    ),
                    is_error=True,
                    metadata={"effect": self.effect},
                )
            cleanup_event = cleanup_matches[-1]
            cleanup_request = cleanup_event.get("request_json")
            if cleanup_request is None:
                cleanup_request = {}
            cleanup_status = cleanup_event.get("status")
            if type(cleanup_status) is not int or not 200 <= cleanup_status <= 299:
                return ToolResult(
                    content=(
                        "captured cleanup returned a non-2xx status and cannot be sealed as an "
                        "acknowledged cleanup"
                    ),
                    is_error=True,
                    metadata={"effect": self.effect},
                )
            acknowledgement_input = arguments.get("cleanup_acknowledgement")
            if acknowledgement_input is None:
                return ToolResult(
                    content=(
                        "captured cleanup requires an explicit reviewed "
                        "cleanup_acknowledgement; Merlin does not infer cleanup success from "
                        "response content"
                    ),
                    is_error=True,
                    metadata={"effect": self.effect},
                )
            elif not isinstance(acknowledgement_input, Mapping):
                return ToolResult(
                    content="cleanup acknowledgement must be a reviewed object",
                    is_error=True,
                    metadata={"effect": self.effect},
                )
            else:
                acknowledgement_pointer = acknowledgement_input.get("pointer")
                acknowledgement_values = acknowledgement_input.get("match_values")
                acknowledgement_statuses = acknowledgement_input.get("status_codes")
                observed_cleanup_value = (
                    _json_pointer_value_or_missing(
                        cleanup_event.get("response_json"), acknowledgement_pointer
                    )
                    if isinstance(acknowledgement_pointer, str)
                    else _MISSING
                )
                if (
                    not isinstance(acknowledgement_pointer, str)
                    or (acknowledgement_pointer and not acknowledgement_pointer.startswith("/"))
                    or not isinstance(acknowledgement_values, list)
                    or not acknowledgement_values
                    or any(not _json_scalar(value) for value in acknowledgement_values)
                    or not isinstance(acknowledgement_statuses, list)
                    or not acknowledgement_statuses
                    or any(
                        type(status) is not int or not 200 <= status <= 299
                        for status in acknowledgement_statuses
                    )
                    or cleanup_event.get("status") not in acknowledgement_statuses
                    or observed_cleanup_value is _MISSING
                    or not any(
                        type(observed_cleanup_value) is type(expected)
                        and observed_cleanup_value == expected
                        for expected in acknowledgement_values
                    )
                ):
                    return ToolResult(
                        content=(
                            "cleanup acknowledgement must match the exact observed cleanup "
                            "method, URL, status, pointer, and typed response value"
                        ),
                        is_error=True,
                        metadata={"effect": self.effect},
                    )
                cleanup_acknowledgement = {
                    "pointer": acknowledgement_pointer,
                    "match_values": list(acknowledgement_values),
                    "status_codes": list(acknowledgement_statuses),
                }
                cleanup_acknowledgement_source = "reviewed_explicit_contract"
            cleanup_action_evidence = await _reviewed_cleanup_action(
                session,
                self._recorder,
                cleanup_event,
                arguments.get("cleanup_action_evidence_ref"),
            )
            if cleanup_action_evidence is None:
                return ToolResult(
                    content=(
                        "cleanup requires a context-bound durable browser action proving that "
                        "a visible reviewed exit control caused the exact subsequent cleanup "
                        "request and acknowledged response"
                    ),
                    is_error=True,
                    metadata={
                        "effect": self.effect,
                        "failure_code": "cleanup_action_evidence_unverified",
                        "retryable": True,
                    },
                )
        setup_step: dict[str, object] | None = None
        setup_event: Mapping[str, Any] | None = None
        session_ownership = "not_applicable"
        creation_transition_evidence: tuple[tuple[str, dict[str, object]], ...] = ()
        request_scalars = _string_scalar_paths(request_json)
        sourced_session_values: set[str] = set()
        for candidate in exchanges[:event_index]:
            sourced_session_values.update(
                value
                for response_pointer, value in _string_scalar_paths(candidate.get("response_json"))
                if any(
                    request_value == value
                    and _capture_name_compatible(response_pointer, request_pointer)
                    for request_pointer, request_value in request_scalars
                )
            )
        if physical_session_id is not None:
            sourced_session_values.intersection_update({physical_session_id})
        if sourced_session_values:
            session_ownership = "unknown"
            cleanup_values = _string_scalar_values(cleanup_request)
            if cleanup_event is not None and not sourced_session_values.issubset(cleanup_values):
                return ToolResult(
                    content=(
                        "captured cleanup does not consume the interaction's ephemeral session "
                        "value; capture the real session-closing exchange"
                    ),
                    is_error=True,
                    metadata={"effect": self.effect},
                )
            setup_candidates: dict[str, tuple[Mapping[str, object], str, str]] = {}
            for candidate in exchanges[:event_index]:
                candidate_response = candidate.get("response_json")
                for pointer, value in _string_scalar_paths(candidate_response):
                    # A setup exchange introduces the ephemeral session value.  Normal
                    # message/status responses may echo that same value after consuming
                    # it; treating every echo as another setup makes an ordinary live
                    # session look like several conflicting creations.
                    if value in sourced_session_values:
                        # Preserve the first observation. A later request may legitimately
                        # echo or auto-resume the same physical session identifier, but it
                        # cannot become a competing setup candidate.  A first observation
                        # that already consumed the value is retained and fails ownership
                        # checks below instead of disappearing as malformed evidence.
                        existing = setup_candidates.get(value)
                        if existing is None:
                            setup_candidates[value] = (candidate, pointer, value)
                        elif value not in _string_scalar_values(candidate.get("request_json")):
                            identity = json.dumps(
                                {
                                    "method": candidate.get("method"),
                                    "url": candidate.get("url"),
                                    "request": candidate.get("request_json"),
                                    "pointer": pointer,
                                    "value": value,
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            setup_candidates.setdefault(identity, (candidate, pointer, value))
            if not setup_candidates:
                return ToolResult(
                    content=(
                        "captured API lifecycle contains an ephemeral session value but its "
                        "setup exchange could not be established"
                    ),
                    is_error=True,
                    metadata={"effect": self.effect},
                )
            # A passive status read can echo the active session identifier before
            # the executable start/resume request appears in the capture. Replaying
            # that read as setup works only while the old remote session exists; a
            # fresh target returns ``no_active_session`` and has no way to create one.
            # Prefer an observed state-changing setup request, retaining the read as
            # a fallback only when it is the sole reviewed lifecycle.
            setup_event, session_pointer, session_value = min(
                setup_candidates.values(),
                key=lambda candidate: (
                    str(candidate[0].get("method") or "").upper() in {"GET", "HEAD"},
                    next(
                        (
                            index
                            for index, observed in enumerate(exchanges[:event_index])
                            if observed is candidate[0]
                        ),
                        len(exchanges),
                    ),
                ),
            )
            supplied_transition = arguments.get("session_creation_transition")
            inferred_transitions: dict[str, Mapping[str, object]] = {}
            for candidate_event, _, candidate_session_id in setup_candidates.values():
                setup_index = next(
                    (
                        index
                        for index, candidate in enumerate(exchanges[:event_index])
                        if candidate is candidate_event
                    ),
                    -1,
                )
                for pre_event in exchanges[:setup_index]:
                    if (
                        pre_event.get("method") != "GET"
                        or pre_event.get("status") != 404
                        or _json_pointer_value_or_missing(pre_event.get("response_json"), "/error")
                        != "no_active_session"
                    ):
                        continue
                    for post_event in exchanges[setup_index + 1 : event_index]:
                        if (
                            post_event.get("method") != "GET"
                            or post_event.get("url") != pre_event.get("url")
                            or post_event.get("status") != 200
                        ):
                            continue
                        post_pointer = next(
                            (
                                pointer
                                for pointer, value in _string_scalar_paths(
                                    post_event.get("response_json")
                                )
                                if value == candidate_session_id
                            ),
                            None,
                        )
                        if post_pointer is None:
                            continue
                        inferred = {
                            "precondition": {
                                "method": "GET",
                                "url": str(pre_event.get("url") or ""),
                                "status_codes": [404],
                                "pointer": "/error",
                                "match_values": ["no_active_session"],
                            },
                            "postcondition": {
                                "method": "GET",
                                "url": str(post_event.get("url") or ""),
                                "status_codes": [200],
                                "pointer": post_pointer,
                            },
                        }
                        inferred_transitions[
                            json.dumps(inferred, sort_keys=True, separators=(",", ":"))
                        ] = inferred
            valid_chains: dict[
                str,
                list[
                    tuple[
                        tuple[Mapping[str, object], str, str],
                        Mapping[str, object],
                        Mapping[str, object],
                        tuple[str, dict[str, object]],
                        Mapping[str, object],
                    ]
                ],
            ] = {"inferred": [], "supplied": []}
            transition_candidates: list[tuple[str, Mapping[str, object]]] = [
                ("inferred", transition) for transition in inferred_transitions.values()
            ]
            if isinstance(supplied_transition, Mapping):
                transition_candidates.append(("supplied", supplied_transition))
            for transition_source, creation_transition in transition_candidates:
                precondition = creation_transition.get("precondition")
                postcondition = creation_transition.get("postcondition")
                canonical_precondition = (
                    isinstance(precondition, Mapping)
                    and isinstance(postcondition, Mapping)
                    and precondition.get("method") == "GET"
                    and postcondition.get("method") == "GET"
                    and precondition.get("url") == postcondition.get("url")
                    and precondition.get("status_codes") == [404]
                    and precondition.get("pointer") == "/error"
                    and precondition.get("match_values") == ["no_active_session"]
                )
                if not canonical_precondition:
                    continue

                def reviewed_match(
                    candidate: Mapping[str, object],
                    contract: object,
                    *,
                    session_match: str | None = None,
                ) -> bool:
                    if not isinstance(contract, Mapping):
                        return False
                    statuses = contract.get("status_codes")
                    pointer = contract.get("pointer")
                    values = contract.get("match_values")
                    if (
                        contract.get("method") != candidate.get("method")
                        or contract.get("url") != candidate.get("url")
                        or not isinstance(statuses, list)
                        or candidate.get("status") not in statuses
                        or not isinstance(pointer, str)
                    ):
                        return False
                    observed = _json_pointer_value_or_missing(
                        candidate.get("response_json"), pointer
                    )
                    if session_match is not None:
                        return type(observed) is str and observed == session_match
                    return isinstance(values, list) and any(
                        type(observed) is type(value) and observed == value for value in values
                    )

                for candidate_tuple in setup_candidates.values():
                    candidate_event, candidate_pointer, candidate_session_id = candidate_tuple
                    setup_index = next(
                        (
                            index
                            for index, candidate in enumerate(exchanges[:event_index])
                            if candidate is candidate_event
                        ),
                        -1,
                    )
                    candidate_trigger_ref = candidate_event.get("trigger_action_ref")
                    comparable_created_session_ids = {
                        observed_session_id
                        for possible_creation in exchanges[:event_index]
                        if possible_creation.get("method") == candidate_event.get("method")
                        and possible_creation.get("url") == candidate_event.get("url")
                        and possible_creation.get("trigger_action_ref") == candidate_trigger_ref
                        and type(possible_creation.get("status")) is int
                        and 200 <= int(possible_creation["status"]) <= 299
                        and isinstance(
                            (
                                observed_session_id := _json_pointer_value_or_missing(
                                    possible_creation.get("response_json"), candidate_pointer
                                )
                            ),
                            str,
                        )
                    }
                    if len(comparable_created_session_ids) != 1:
                        continue
                    pre_matches = [
                        candidate
                        for candidate in exchanges[:setup_index]
                        if reviewed_match(candidate, precondition)
                    ]
                    post_matches = [
                        candidate
                        for candidate in exchanges[setup_index + 1 : event_index]
                        if reviewed_match(
                            candidate,
                            postcondition,
                            session_match=candidate_session_id,
                        )
                    ]
                    zero_turn = (
                        postcondition.get("zero_turn")
                        if isinstance(postcondition, Mapping)
                        else None
                    )
                    if zero_turn is not None:
                        post_matches = [
                            candidate
                            for candidate in post_matches
                            if reviewed_match(
                                candidate,
                                {
                                    "method": candidate.get("method"),
                                    "url": candidate.get("url"),
                                    "status_codes": [candidate.get("status")],
                                    **(dict(zero_turn) if isinstance(zero_turn, Mapping) else {}),
                                },
                            )
                        ]
                    setup_response = candidate_event.get("response_json")
                    setup_status = candidate_event.get("status")
                    resumed = (
                        setup_response.get("resumed", _MISSING)
                        if isinstance(setup_response, Mapping)
                        else _MISSING
                    )
                    original_session_was_created = resumed is _MISSING or resumed is False
                    setup_action_evidence = await _reviewed_latest_setup_action(
                        session,
                        self._recorder,
                        candidate_event,
                    )
                    if (
                        pre_matches
                        and post_matches
                        and type(setup_status) is int
                        and 200 <= setup_status <= 299
                        and original_session_was_created
                        and candidate_session_id
                        not in _string_scalar_values(candidate_event.get("request_json"))
                        and setup_action_evidence is not None
                    ):
                        # Repeated identical absence/active polls and later message
                        # responses carrying the same session ID are corroborating
                        # observations, not competing ownership claims.  Choose the
                        # nearest absence proof and first matching active proof; distinct
                        # setup candidates remain ambiguous below.
                        selected_pre = pre_matches[-1]
                        selected_post = post_matches[0]
                        valid_chains[transition_source].append(
                            (
                                candidate_tuple,
                                selected_pre,
                                selected_post,
                                setup_action_evidence,
                                creation_transition,
                            )
                        )
            selected_chains = (
                valid_chains["inferred"]
                if len(valid_chains["inferred"]) == 1
                else valid_chains["supplied"]
                if not valid_chains["inferred"] and len(valid_chains["supplied"]) == 1
                else []
            )
            canonical_api_transition_evidence = bool(
                valid_chains["inferred"] or valid_chains["supplied"]
            )
            if len(selected_chains) == 1:
                (
                    (setup_event, session_pointer, session_value),
                    pre_event,
                    post_event,
                    setup_action_evidence,
                    creation_transition,
                ) = selected_chains[0]
                session_ownership = "owned"
                creation_transition_evidence = (
                    _captured_api_evidence(session, pre_event, "request"),
                    _captured_api_evidence(session, pre_event, "response"),
                    setup_action_evidence,
                    _captured_api_evidence(session, post_event, "request"),
                    _captured_api_evidence(session, post_event, "response"),
                )
            if session_ownership != "owned" and not canonical_api_transition_evidence:
                direct_creation_chains: list[
                    tuple[
                        tuple[Mapping[str, object], str, str],
                        tuple[str, dict[str, object]],
                        Mapping[str, object],
                    ]
                ] = []
                for candidate_tuple in setup_candidates.values():
                    candidate_event, candidate_pointer, candidate_session_id = candidate_tuple
                    setup_response = candidate_event.get("response_json")
                    setup_status = candidate_event.get("status")
                    if (
                        str(candidate_event.get("method") or "").upper()
                        not in {"POST", "PUT", "PATCH"}
                        or type(setup_status) is not int
                        or not 200 <= setup_status <= 299
                        or not isinstance(setup_response, Mapping)
                        or setup_response.get("resumed") is not False
                        or candidate_session_id
                        in _string_scalar_values(candidate_event.get("request_json"))
                        or candidate_session_id not in _string_scalar_values(request_json)
                    ):
                        continue
                    setup_action_evidence = await _reviewed_latest_setup_action(
                        session,
                        self._recorder,
                        candidate_event,
                    )
                    if setup_action_evidence is None:
                        continue
                    creation_transition = {
                        "precondition": {
                            "method": str(candidate_event.get("method") or ""),
                            "url": str(candidate_event.get("url") or ""),
                            "status_codes": [setup_status],
                            "pointer": "/resumed",
                            "match_values": [False],
                        },
                        "postcondition": {
                            "method": str(candidate_event.get("method") or ""),
                            "url": str(candidate_event.get("url") or ""),
                            "status_codes": [setup_status],
                            "pointer": candidate_pointer,
                            "match_values": [candidate_session_id],
                        },
                        "source": "authoritative_fresh_setup_response",
                    }
                    direct_creation_chains.append(
                        (candidate_tuple, setup_action_evidence, creation_transition)
                    )
                if len(direct_creation_chains) == 1:
                    (
                        (setup_event, session_pointer, session_value),
                        setup_action_evidence,
                        creation_transition,
                    ) = direct_creation_chains[0]
                    session_ownership = "owned"
                    creation_transition_evidence = (
                        setup_action_evidence,
                        _captured_api_evidence(session, setup_event, "request"),
                        _captured_api_evidence(session, setup_event, "response"),
                    )
            if session_ownership != "owned" and not canonical_api_transition_evidence:
                hybrid_chains: list[
                    tuple[
                        tuple[Mapping[str, object], str, str],
                        Mapping[str, object],
                        tuple[str, dict[str, object]],
                        Mapping[str, object],
                    ]
                ] = []
                for candidate_tuple in setup_candidates.values():
                    candidate_event, candidate_pointer, candidate_session_id = candidate_tuple
                    setup_index = next(
                        (
                            index
                            for index, candidate in enumerate(exchanges[:event_index])
                            if candidate is candidate_event
                        ),
                        -1,
                    )
                    candidate_trigger_ref = candidate_event.get("trigger_action_ref")
                    comparable_created_session_ids = {
                        observed_session_id
                        for possible_creation in exchanges[:event_index]
                        if possible_creation.get("method") == candidate_event.get("method")
                        and possible_creation.get("url") == candidate_event.get("url")
                        and possible_creation.get("trigger_action_ref") == candidate_trigger_ref
                        and type(possible_creation.get("status")) is int
                        and 200 <= int(possible_creation["status"]) <= 299
                        and isinstance(
                            (
                                observed_session_id := _json_pointer_value_or_missing(
                                    possible_creation.get("response_json"), candidate_pointer
                                )
                            ),
                            str,
                        )
                    }
                    if len(comparable_created_session_ids) != 1:
                        continue
                    setup_status = candidate_event.get("status")
                    setup_request = candidate_event.get("request_json")
                    setup_response = candidate_event.get("response_json")
                    setup_observed_at_raw = candidate_event.get("observed_at")
                    try:
                        setup_observed_at = (
                            datetime.fromisoformat(setup_observed_at_raw)
                            if isinstance(setup_observed_at_raw, str)
                            else None
                        )
                    except ValueError:
                        setup_observed_at = None
                    setup_request_scalars = _string_scalar_values(setup_request)
                    prior_response_scalars = {
                        value
                        for prior_event in exchanges[:setup_index]
                        for value in _string_scalar_values(prior_event.get("response_json"))
                    }
                    if (
                        type(setup_status) is not int
                        or not 200 <= setup_status <= 299
                        or not isinstance(setup_response, Mapping)
                        or setup_observed_at is None
                        or candidate_session_id in _string_scalar_values(setup_request)
                        or any(
                            _capture_name_compatible(candidate_pointer, request_pointer)
                            for request_pointer, _ in _string_scalar_paths(setup_request)
                        )
                        or bool(setup_request_scalars & prior_response_scalars)
                        or any(
                            flag in setup_response and setup_response.get(flag) is not False
                            for flag in (
                                "resumed",
                                "restart",
                                "restarted",
                                "recovered",
                                "continued",
                            )
                        )
                        or any(
                            setup_response.get(history) not in (None, "", [], {})
                            for history in ("allMsgs", "history", "messages")
                        )
                        or any(
                            type(setup_response.get(turns)) is not int
                            or setup_response.get(turns) != 0
                            for turns in ("turns", "turnsUsed")
                            if turns in setup_response
                        )
                        or candidate_session_id
                        in {
                            value
                            for prior_event in exchanges[:setup_index]
                            for value in (
                                *_string_scalar_values(prior_event.get("request_json")),
                                *_string_scalar_values(prior_event.get("response_json")),
                            )
                        }
                        or candidate_session_id not in _string_scalar_values(request_json)
                    ):
                        continue
                    pre_events: list[tuple[Mapping[str, object], datetime]] = []
                    for prior_event in exchanges[:setup_index]:
                        pre_observed_at_raw = prior_event.get("observed_at")
                        try:
                            pre_observed_at = (
                                datetime.fromisoformat(pre_observed_at_raw)
                                if isinstance(pre_observed_at_raw, str)
                                else None
                            )
                        except ValueError:
                            pre_observed_at = None
                        if (
                            prior_event.get("method") == "GET"
                            and prior_event.get("request_json") in (None, {})
                            and prior_event.get("status") == 404
                            and _json_pointer_value_or_missing(
                                prior_event.get("response_json"), "/error"
                            )
                            == "no_active_session"
                            and pre_observed_at is not None
                            and pre_observed_at < setup_observed_at
                            and _same_lifecycle_path_family(
                                prior_event.get("url"), candidate_event.get("url")
                            )
                        ):
                            pre_events.append((prior_event, pre_observed_at))
                    if not pre_events:
                        continue
                    action_evidence = await _reviewed_latest_setup_action(
                        session,
                        self._recorder,
                        candidate_event,
                    )
                    if action_evidence is None:
                        continue
                    action_created_at_raw = action_evidence[1].get("created_at")
                    try:
                        action_created_at = (
                            datetime.fromisoformat(action_created_at_raw)
                            if isinstance(action_created_at_raw, str)
                            else None
                        )
                    except ValueError:
                        action_created_at = None
                    candidate_pre_events = pre_events if action_created_at is not None else []
                    normalized_pre_urls = {
                        _normalized_lifecycle_endpoint(event.get("url"))
                        for event, _ in candidate_pre_events
                    }
                    if not candidate_pre_events or len(normalized_pre_urls) != 1:
                        continue
                    pre_event, _ = max(candidate_pre_events, key=lambda item: item[1])
                    creation_transition = {
                        "precondition": {
                            "method": "GET",
                            "url": str(pre_event.get("url") or ""),
                            "status_codes": [404],
                            "pointer": "/error",
                            "match_values": ["no_active_session"],
                        },
                        "postcondition": {
                            "method": str(candidate_event.get("method") or ""),
                            "url": str(candidate_event.get("url") or ""),
                            "status_codes": [setup_status],
                            "pointer": candidate_pointer,
                            "match_values": [candidate_session_id],
                        },
                        "source": "durable_visible_setup_action",
                    }
                    hybrid_chains.append(
                        (candidate_tuple, pre_event, action_evidence, creation_transition)
                    )
                if len(hybrid_chains) == 1:
                    (
                        (setup_event, session_pointer, session_value),
                        pre_event,
                        setup_action_evidence,
                        creation_transition,
                    ) = hybrid_chains[0]
                    session_ownership = "owned"
                    creation_transition_evidence = (
                        _captured_api_evidence(session, pre_event, "request"),
                        _captured_api_evidence(session, pre_event, "response"),
                        setup_action_evidence,
                        _captured_api_evidence(session, setup_event, "request"),
                        _captured_api_evidence(session, setup_event, "response"),
                    )
            setup_request = setup_event.get("request_json")
            if setup_request is None:
                setup_request = {}
            request_template = _replace_json_value(
                request_template, session_value, "{{capture:session_id}}"
            )
            for binding_value, capture_name in capture_names.items():
                request_template = _replace_json_value(
                    request_template,
                    binding_value,
                    f"{{{{capture:{capture_name}}}}}",
                )
            if cleanup_event is not None:
                cleanup_request = _replace_json_value(
                    cleanup_request, session_value, "{{capture:session_id}}"
                )
                for binding_value, capture_name in capture_names.items():
                    cleanup_request = _replace_json_value(
                        cleanup_request,
                        binding_value,
                        f"{{{{capture:{capture_name}}}}}",
                    )
            setup_captures = {"session_id": session_pointer}
            setup_captures.update(
                {
                    capture_names[value]: response_pointer
                    for _, response_pointer, value in physical_session_bindings
                }
            )
            setup_step = {
                "step_id": "observed-session-setup",
                "endpoint": str(setup_event["url"]),
                "method": str(setup_event["method"]),
                "request_template": setup_request,
                "response_selector": "",
                "success_witness": "captured session setup response status is reproduced",
                "expected_status": int(setup_event["status"]),
                "idempotency_key": None,
                "replay_after_ambiguous_result": False,
                "cleanup": False,
                "interaction": False,
                "captures": setup_captures,
                "repeat": 1,
            }
        # Turn shape is derived from captured evidence, never from model prose:
        # an interaction request that carries an ephemeral session value produced
        # by an earlier exchange binds repeated turns into one session lifecycle.
        if oracle_predicate is not None:
            oracle_predicate["turn"] = "multi" if setup_step is not None else "single"
        request_evidence_ref, request_evidence = _captured_api_evidence(session, event, "request")
        response_evidence_ref, response_evidence = _captured_api_evidence(
            session, event, "response"
        )
        setup_evidence: tuple[tuple[str, dict[str, object]], ...] = ()
        if setup_event is not None:
            setup_evidence = (
                _captured_api_evidence(session, setup_event, "request"),
                _captured_api_evidence(session, setup_event, "response"),
            )
        setup_evidence_refs = [reference for reference, _ in setup_evidence]
        creation_transition_evidence_refs = [
            reference for reference, _ in creation_transition_evidence
        ]
        terminal_evidence_by_ref: dict[str, dict[str, object]] = {}
        for normalized_condition, matching_events in terminal_condition_events:
            condition_evidence_refs: list[str] = []
            for terminal_event in matching_events:
                for evidence_ref, payload in (
                    _captured_api_evidence(session, terminal_event, "request"),
                    _captured_api_evidence(session, terminal_event, "response"),
                ):
                    terminal_evidence_by_ref[evidence_ref] = payload
                    condition_evidence_refs.append(evidence_ref)
            predicate_observation = normalized_condition["predicate_observation"]
            assert isinstance(predicate_observation, dict)
            predicate_observation["evidence_refs"] = list(dict.fromkeys(condition_evidence_refs))
        terminal_evidence = tuple(terminal_evidence_by_ref.items())
        terminal_evidence_refs = [reference for reference, _ in terminal_evidence]
        cleanup_evidence: tuple[tuple[str, dict[str, object]], ...] = ()
        if cleanup_event is not None:
            cleanup_evidence = (
                _captured_api_evidence(session, cleanup_event, "request"),
                _captured_api_evidence(session, cleanup_event, "response"),
            )
        cleanup_evidence_refs = [reference for reference, _ in cleanup_evidence]
        capability_evidence_by_ref: dict[str, dict[str, object]] = {}
        request_constraints: list[dict[str, object]] = []
        for raw_constraint in arguments.get("request_constraints", []):
            if not isinstance(raw_constraint, Mapping):
                continue
            try:
                constraint_path = _normalize_json_pointer(raw_constraint.get("path"))
            except ValueError:
                constraint_path = ""
            selector = raw_constraint.get("evidence_selector")
            if constraint_path != input_pointer or not isinstance(selector, Mapping):
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.request_constraints",
                        "A request constraint did not describe the reviewed input path and was "
                        "retained only as uncertainty.",
                    )
                )
                continue
            selector_method = str(selector.get("method") or method).upper()
            selector_url = str(selector.get("url") or url)
            selector_statuses = selector.get("status_codes")
            selector_pointer = selector.get("pointer")
            selector_values = selector.get("match_values")
            matching_constraint_events: list[Mapping[str, Any]] = []
            if (
                isinstance(selector_statuses, list)
                and isinstance(selector_pointer, str)
                and isinstance(selector_values, list)
                and any(
                    type(value) is int and value == raw_constraint.get("maximum")
                    for value in selector_values
                )
            ):
                for candidate in all_context_exchanges:
                    observed_value = _json_pointer_value_or_missing(
                        candidate.get("response_json"), selector_pointer
                    )
                    if (
                        str(candidate.get("method") or "").upper() == selector_method
                        and candidate.get("url") == selector_url
                        and candidate.get("status") in selector_statuses
                        and any(
                            type(observed_value) is type(expected) and observed_value == expected
                            for expected in selector_values
                        )
                    ):
                        matching_constraint_events.append(candidate)
            if not matching_constraint_events:
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.request_constraints",
                        "A declared request constraint had no exact captured response matching "
                        "its evidence selector and was not made executable.",
                    )
                )
                continue
            constraint_evidence_refs: list[str] = []
            constraint_binding_refs: list[str] = []
            for constraint_event in matching_constraint_events:
                request_ref, request_payload = _captured_api_evidence(
                    session, constraint_event, "request"
                )
                response_ref, response_payload = _captured_api_evidence(
                    session, constraint_event, "response"
                )
                for evidence_ref, payload in (
                    (request_ref, request_payload),
                    (response_ref, response_payload),
                ):
                    capability_evidence_by_ref[evidence_ref] = payload
                    constraint_evidence_refs.append(evidence_ref)
                constraint_binding_refs.append(response_ref)
            request_constraints.append(
                {
                    "path": constraint_path,
                    "step_id": "observed-message-exchange",
                    "evaluation_root": "request_body",
                    "kind": str(raw_constraint["kind"]),
                    "maximum": int(raw_constraint["maximum"]),
                    "unit": str(raw_constraint["unit"]),
                    "source": str(raw_constraint["source"]),
                    "evidence_refs": list(dict.fromkeys(constraint_evidence_refs)),
                    "evidence_bindings": [
                        {
                            "evidence_ref": evidence_ref,
                            "observed_maximum": int(raw_constraint["maximum"]),
                        }
                        for evidence_ref in dict.fromkeys(constraint_binding_refs)
                    ],
                    "confidence": float(raw_constraint["confidence"]),
                }
            )

        if not any(item["path"] == input_pointer for item in request_constraints):
            reviewed_dom_constraint = await _reviewed_dom_input_constraint(
                session, self._recorder, event, input_pointer
            )
            if reviewed_dom_constraint is not None:
                constraint, (evidence_ref, evidence_payload) = reviewed_dom_constraint
                request_constraints.append(constraint)
                capability_evidence_by_ref[evidence_ref] = evidence_payload

        state_observations: list[dict[str, object]] = []
        observation_values_by_name: dict[str, dict[str, object]] = {}
        reviewed_state_identities: list[dict[str, object]] = []
        state_evidence_by_step_id: dict[
            str,
            tuple[
                Mapping[str, Any] | None,
                tuple[tuple[str, dict[str, object]], ...],
            ],
        ] = {
            "observed-session-setup": (setup_event, setup_evidence),
            "observed-message-exchange": (
                event,
                (
                    (request_evidence_ref, request_evidence),
                    (response_evidence_ref, response_evidence),
                ),
            ),
            "observed-cleanup-exchange": (cleanup_event, cleanup_evidence),
        }
        for raw_observation in arguments.get("state_observations", []):
            if not isinstance(raw_observation, Mapping):
                continue
            selectors: dict[str, dict[str, object]] = {}
            observation_evidence_refs: list[str] = []
            observed_values: dict[str, object] = {}
            for selector_name in ("baseline_selector", "result_selector"):
                raw_selector = raw_observation.get(selector_name)
                if not isinstance(raw_selector, Mapping):
                    break
                step_id = raw_selector.get("step_id")
                expected_step_id = (
                    "observed-session-setup"
                    if selector_name == "baseline_selector"
                    else "observed-message-exchange"
                )
                if step_id != expected_step_id:
                    break
                pointer = raw_selector.get("pointer")
                observed_event, observed_evidence = state_evidence_by_step_id.get(
                    str(step_id), (None, ())
                )
                observed_value = (
                    _json_pointer_value_or_missing(observed_event.get("response_json"), pointer)
                    if observed_event is not None and isinstance(pointer, str)
                    else _MISSING
                )
                if observed_value is _MISSING:
                    break
                response_evidence = tuple(
                    (reference, payload)
                    for reference, payload in observed_evidence
                    if payload.get("part") == "response"
                )
                selector_evidence_refs = [reference for reference, _ in response_evidence]
                selectors[selector_name] = {
                    "schema_version": "merlin.state-selector.v1",
                    "step_id": str(step_id),
                    "evaluation_root": "response_body",
                    "pointer": str(pointer),
                    "evidence_refs": selector_evidence_refs,
                    "confidence": float(raw_selector.get("confidence", 1.0)),
                }
                observed_values[selector_name] = observed_value
                observation_evidence_refs.extend(selector_evidence_refs)
                capability_evidence_by_ref.update(dict(response_evidence))
            if set(selectors) != {"baseline_selector", "result_selector"}:
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.state_observations",
                        "A state observation selector was absent from its exact captured "
                        "lifecycle response and was not made executable.",
                    )
                )
                continue
            value_type = raw_observation.get("value_type")
            value_type_matches = {
                "integer": lambda value: type(value) is int,
                "number": lambda value: type(value) in {int, float},
                "string": lambda value: isinstance(value, str),
                "boolean": lambda value: type(value) is bool,
            }
            if value_type is None:
                if all(type(value) is int for value in observed_values.values()):
                    value_type = "integer"
                elif all(type(value) in {int, float} for value in observed_values.values()):
                    value_type = "number"
                elif all(isinstance(value, str) for value in observed_values.values()):
                    value_type = "string"
                elif all(type(value) is bool for value in observed_values.values()):
                    value_type = "boolean"
            if value_type is not None and (
                value_type not in value_type_matches
                or not all(
                    value_type_matches[str(value_type)](value) for value in observed_values.values()
                )
            ):
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.state_observations",
                        "A state observation did not match its reviewed value type and was not "
                        "made executable.",
                    )
                )
                continue
            numeric_values = tuple(
                value for value in observed_values.values() if type(value) in {int, float}
            )
            minimum_value = raw_observation.get("minimum_value")
            maximum_value = raw_observation.get("maximum_value")
            if (minimum_value is not None or maximum_value is not None) and len(
                numeric_values
            ) != len(observed_values):
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.state_observations",
                        "Numeric bounds were declared for a non-numeric state observation and "
                        "were not made executable.",
                    )
                )
                continue
            if (
                minimum_value is not None and any(value < minimum_value for value in numeric_values)
            ) or (
                maximum_value is not None and any(value > maximum_value for value in numeric_values)
            ):
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.state_observations",
                        "A state observation fell outside its reviewed bounds and was not made "
                        "executable.",
                    )
                )
                continue
            monotonicity = raw_observation.get("monotonicity")
            baseline_value = observed_values["baseline_selector"]
            result_value = observed_values["result_selector"]
            if monotonicity is not None and (
                type(baseline_value) not in {int, float}
                or type(result_value) not in {int, float}
                or (monotonicity == "nondecreasing" and result_value < baseline_value)
                or (monotonicity == "nonincreasing" and result_value > baseline_value)
            ):
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.state_observations",
                        "A state observation contradicted its reviewed monotonic rule and was "
                        "not made executable.",
                    )
                )
                continue
            observation = {
                "name": str(raw_observation["name"]),
                "role": str(raw_observation["role"]),
                **selectors,
                "evidence_refs": list(dict.fromkeys(observation_evidence_refs)),
                "trust_class": "target_reported",
            }
            if value_type is not None:
                observation["value_type"] = str(value_type)
            for optional_key in (
                "preferred_direction",
                "success_values",
                "failure_values",
                "value_type",
                "minimum_value",
                "maximum_value",
                "monotonicity",
            ):
                if optional_key in raw_observation:
                    value = raw_observation[optional_key]
                    observation[optional_key] = list(value) if isinstance(value, list) else value
            state_observations.append(observation)
            observation_values_by_name[str(observation["name"])] = observed_values
            if observation["role"] == "state_identity":
                reviewed_state_identities.append(
                    {
                        "identity_scope": "physical_state",
                        "baseline_selector": dict(observation["baseline_selector"]),
                        "result_selector": dict(observation["result_selector"]),
                        "evidence_refs": list(observation["evidence_refs"]),
                    }
                )
        reviewed_state_identity = (
            reviewed_state_identities[0] if len(reviewed_state_identities) == 1 else None
        )
        if len(reviewed_state_identities) > 1:
            evaluation_uncertainties.append(
                _evaluation_uncertainty(
                    "browser_compile_api_handoff.state_identity",
                    "Multiple physical-state identity observations were declared; no identity "
                    "capability was sealed.",
                )
            )
        recon_observations, recon_observation_evidence = _reviewed_recon_observations(
            session,
            arguments.get("state_observations", ()),
            exchanges,
            method=method,
            url=url,
        )
        capability_evidence_by_ref.update(dict(recon_observation_evidence))
        identity_equivalence_rules: list[dict[str, object]] = []
        resource_ownership: list[dict[str, object]] = []
        corroboration_rules: list[dict[str, object]] = []

        def compile_state_selector(
            raw_selector: object,
        ) -> tuple[dict[str, object], object] | None:
            if not isinstance(raw_selector, Mapping):
                return None
            step_id = str(raw_selector.get("step_id") or "")
            pointer = raw_selector.get("pointer")
            observed_event, observed_evidence = state_evidence_by_step_id.get(step_id, (None, ()))
            observed_value = (
                _json_pointer_value_or_missing(observed_event.get("response_json"), pointer)
                if observed_event is not None and isinstance(pointer, str)
                else _MISSING
            )
            if observed_value is _MISSING:
                return None
            response_evidence = tuple(
                (reference, payload)
                for reference, payload in observed_evidence
                if payload.get("part") == "response"
            )
            capability_evidence_by_ref.update(dict(response_evidence))
            return (
                {
                    "schema_version": "merlin.state-selector.v1",
                    "step_id": step_id,
                    "evaluation_root": "response_body",
                    "pointer": str(pointer),
                    "evidence_refs": [reference for reference, _ in response_evidence],
                    "confidence": float(raw_selector.get("confidence", 1.0)),
                },
                observed_value,
            )

        state_enumerations: list[dict[str, object]] = []
        for raw_enumeration in arguments.get("state_enumerations", []):
            if not isinstance(raw_enumeration, Mapping):
                continue
            compiled_selector = compile_state_selector(raw_enumeration.get("selector"))
            if compiled_selector is None:
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.state_enumerations",
                        "A finite-domain selector was absent from its exact captured "
                        "lifecycle response and was not made executable.",
                    )
                )
                continue
            selector, selected_value = compiled_selector
            declaration_payload: dict[str, object] = {
                "schema_version": "merlin.state-enumeration-declaration.v1",
                "name": str(raw_enumeration.get("name") or ""),
                "selector": selector,
                "shape": str(raw_enumeration.get("shape") or "flat"),
                "completeness": str(raw_enumeration.get("completeness") or "partial"),
                "evidence_refs": list(selector.get("evidence_refs", ())),
                "confidence": float(
                    raw_enumeration.get("confidence", selector.get("confidence", 1.0))
                ),
                **(
                    {"scope_dimension": str(raw_enumeration["scope_dimension"])}
                    if raw_enumeration.get("scope_dimension") is not None
                    else {}
                ),
            }
            try:
                declaration = StateEnumerationDeclaration.model_validate(
                    declaration_payload
                )
                # Compile-time validation proves the captured target value has
                # the declared flat/scoped member shape. Runtime builds a fresh
                # target receipt from the actual response.
                build_state_enumeration(
                    declaration,
                    selected_value=selected_value,
                    binding_context={
                        "compiled_from": "browser_api_recon_handoff",
                        "step_id": selector["step_id"],
                        "pointer": selector["pointer"],
                    },
                )
            except ValueError:
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.state_enumerations",
                        "A finite-domain declaration did not match the captured target "
                        "value and was not made executable.",
                    )
                )
                continue
            state_enumerations.append(declaration.model_dump(mode="json"))

        for raw_rule in arguments.get("identity_equivalence_rules", []):
            if not isinstance(raw_rule, Mapping):
                continue
            left = compile_state_selector(raw_rule.get("left_selector"))
            right = compile_state_selector(raw_rule.get("right_selector"))
            if (
                reviewed_state_identity is None
                or left is None
                or right is None
                or left[0]["step_id"] != "observed-session-setup"
                or right[0]["step_id"] != "observed-message-exchange"
                or type(left[1]) is not type(right[1])
                or left[1] != right[1]
                or {key: left[0][key] for key in ("step_id", "evaluation_root", "pointer")}
                == {key: right[0][key] for key in ("step_id", "evaluation_root", "pointer")}
            ):
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.identity_equivalence_rules",
                        "A rotating-identity equivalence rule lacked distinct, equal, exact "
                        "captured linkage values and was not made executable.",
                    )
                )
                continue
            rule_evidence_refs = list(
                dict.fromkeys(
                    [
                        *left[0]["evidence_refs"],
                        *right[0]["evidence_refs"],
                    ]
                )
            )
            identity_equivalence_rules.append(
                {
                    "name": str(raw_rule["name"]),
                    "relation": "equal",
                    "left_selector": left[0],
                    "right_selector": right[0],
                    "evidence_refs": rule_evidence_refs,
                }
            )

        for raw_rule in arguments.get("corroboration_rules", []):
            if not isinstance(raw_rule, Mapping):
                continue
            names = raw_rule.get("observation_names")
            minimum_agreement = raw_rule.get("minimum_agreement")
            if not isinstance(names, list) or type(minimum_agreement) is not int:
                continue
            named_observations = [
                next(
                    (
                        observation
                        for observation in state_observations
                        if observation.get("name") == str(name)
                    ),
                    None,
                )
                for name in names
            ]
            trust_classes = {
                observation.get("trust_class")
                for observation in named_observations
                if observation is not None
            }
            if not trust_classes & {"host_observed", "independent_authority"}:
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.corroboration_rules",
                        "Agreement between target-reported fields is not independent "
                        "corroboration; the requested rule remains advisory.",
                    )
                )
                continue
            named_values = [observation_values_by_name.get(str(name)) for name in names]
            if any(values is None for values in named_values):
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.corroboration_rules",
                        "A corroboration rule referenced an unavailable observation and was not "
                        "made executable.",
                    )
                )
                continue
            agrees = True
            for selector_name in ("baseline_selector", "result_selector"):
                values = [item[selector_name] for item in named_values if item is not None]
                greatest_agreement = max(
                    (
                        sum(type(other) is type(value) and other == value for other in values)
                        for value in values
                    ),
                    default=0,
                )
                if greatest_agreement < minimum_agreement:
                    agrees = False
                    break
            if not agrees:
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.corroboration_rules",
                        "Captured observation values did not satisfy a reviewed corroboration "
                        "rule, so the rule was not made executable.",
                    )
                )
                continue
            corroboration_rules.append(
                {
                    "name": str(raw_rule["name"]),
                    "observation_names": [str(name) for name in names],
                    "minimum_agreement": minimum_agreement,
                }
            )

        observations_by_name = {
            str(observation["name"]): observation for observation in state_observations
        }
        for raw_ownership in arguments.get("resource_ownership", []):
            if not isinstance(raw_ownership, Mapping):
                continue
            observation_name = str(raw_ownership.get("resource_observation_name") or "")
            resource_observation = observations_by_name.get(observation_name)
            principal = compile_state_selector(raw_ownership.get("principal_selector"))
            reset = (
                compile_state_selector(raw_ownership.get("reset_selector"))
                if "reset_selector" in raw_ownership
                else None
            )
            principal_value = principal[1] if principal is not None else None
            principal_is_safe_scalar = (
                isinstance(principal_value, (str, int))
                and not isinstance(principal_value, bool)
                and str(principal_value) != ""
            )
            if (
                resource_observation is None
                or resource_observation.get("role") != "budget_remaining"
                or principal is None
                or principal[0]["step_id"] != "observed-session-setup"
                or not principal_is_safe_scalar
                or ("reset_selector" in raw_ownership and reset is None)
                or (reset is not None and reset[0]["step_id"] != "observed-session-setup")
            ):
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.resource_ownership",
                        "A resource ownership declaration lacked an exact principal, reset, or "
                        "budget observation binding and was not made executable.",
                    )
                )
                continue
            ownership_evidence_refs = list(principal[0]["evidence_refs"])
            if reset is not None:
                ownership_evidence_refs.extend(reset[0]["evidence_refs"])
            ownership_evidence_refs.extend(resource_observation["evidence_refs"])
            resource_ownership.append(
                {
                    "name": str(raw_ownership["name"]),
                    "resource_kind": str(raw_ownership["resource_kind"]),
                    "resource_observation_name": observation_name,
                    "principal_selector": principal[0],
                    "authority": "adapter_origin",
                    **({"reset_selector": reset[0]} if reset is not None else {}),
                    "evidence_refs": list(dict.fromkeys(ownership_evidence_refs)),
                }
            )
        supported_modalities: list[dict[str, object]] = []
        for raw_modality in arguments.get("supported_modalities", []):
            if not isinstance(raw_modality, Mapping):
                continue
            try:
                modality_pointer = _normalize_json_pointer(raw_modality.get("request_pointer"))
            except ValueError:
                modality_pointer = ""
            observed_modality_value = _json_pointer_value_or_missing(request_json, modality_pointer)
            modality = str(raw_modality.get("modality") or "")
            if (
                modality_pointer != input_pointer
                or observed_modality_value is _MISSING
                or modality != "text"
                or not isinstance(observed_modality_value, str)
            ):
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.supported_modalities",
                        "A modality declaration was not bound to the exact captured input and "
                        "remains advisory.",
                    )
                )
                continue
            capability_evidence_by_ref[request_evidence_ref] = request_evidence
            supported_modalities.append(
                {
                    "modality": modality,
                    "step_id": "observed-message-exchange",
                    "request_pointer": modality_pointer,
                    "evidence_refs": [request_evidence_ref],
                    "confidence": float(raw_modality.get("confidence", 1.0)),
                }
            )

        resource_controls: list[dict[str, object]] = []
        known_ownership = {str(item["name"]) for item in resource_ownership}
        for raw_control in arguments.get("resource_controls", []):
            if not isinstance(raw_control, Mapping):
                continue
            ownership_name = str(raw_control.get("ownership_name") or "")
            status_codes = raw_control.get("status_codes")
            retry_pointer = raw_control.get("retry_after_seconds_pointer")
            wake_pointer = raw_control.get("wake_at_pointer")
            wake_key = (
                "retry_after_seconds_pointer"
                if isinstance(retry_pointer, str)
                else "wake_at_pointer"
            )
            wake_value = retry_pointer if isinstance(retry_pointer, str) else wake_pointer
            matching_blocks = [
                candidate
                for candidate in all_context_exchanges
                if candidate.get("method") == method
                and candidate.get("url") == url
                and isinstance(status_codes, list)
                and candidate.get("status") in status_codes
                and isinstance(wake_value, str)
                and _json_pointer_value_or_missing(candidate.get("response_json"), wake_value)
                is not _MISSING
            ]
            if (
                ownership_name not in known_ownership
                or not matching_blocks
                or (isinstance(retry_pointer, str) == isinstance(wake_pointer, str))
            ):
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.resource_controls",
                        "A resource control lacked reviewed ownership or an exact structured "
                        "block response and remains advisory.",
                    )
                )
                continue
            block_evidence_refs: list[str] = []
            for block in matching_blocks:
                response_ref, response_payload = _captured_api_evidence(session, block, "response")
                capability_evidence_by_ref[response_ref] = response_payload
                block_evidence_refs.append(response_ref)
            resource_controls.append(
                {
                    "ownership_name": ownership_name,
                    "step_id": "observed-message-exchange",
                    "reservation_amount": 1,
                    "limit": int(raw_control["limit"]),
                    "window_key": str(raw_control["window_key"]),
                    "structured_blocks": [
                        {
                            "status_codes": list(status_codes),
                            wake_key: str(wake_value),
                            "evidence_refs": list(dict.fromkeys(block_evidence_refs)),
                        }
                    ],
                }
            )

        state_affordances: list[dict[str, object]] = []
        auxiliary_setup_steps: list[dict[str, object]] = []
        affordance_step_ids: dict[str, str] = {}
        for raw_affordance in arguments.get("state_affordances", []):
            if not isinstance(raw_affordance, Mapping):
                continue
            affordance_kind = str(raw_affordance.get("kind") or "")
            affordance_method = str(raw_affordance.get("method") or "").upper()
            affordance_url = str(raw_affordance.get("url") or "")
            affordance_statuses = raw_affordance.get("status_codes")
            affordance_pointer = raw_affordance.get("pointer")
            affordance_values = raw_affordance.get("match_values")
            candidates = [
                candidate
                for candidate in exchanges[:event_index]
                if candidate.get("method") == affordance_method
                and candidate.get("url") == affordance_url
                and isinstance(affordance_statuses, list)
                and candidate.get("status") in affordance_statuses
                and isinstance(affordance_pointer, str)
                and isinstance(affordance_values, list)
                and any(
                    type(observed) is type(expected) and observed == expected
                    for observed in [
                        _json_pointer_value_or_missing(
                            candidate.get("response_json"), affordance_pointer
                        )
                    ]
                    for expected in affordance_values
                )
            ]
            if len(candidates) != 1 or setup_step is None:
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.state_affordances",
                        "A state affordance did not have one exact captured pre-interaction "
                        "exchange and remains advisory.",
                    )
                )
                continue
            affordance_event = candidates[0]
            if affordance_kind == "clone_state":
                clone_identity_pointer = raw_affordance.get("identity_pointer")
                identity_values = next(
                    (
                        observation_values_by_name.get(str(observation["name"]))
                        for observation in state_observations
                        if observation.get("role") == "state_identity"
                    ),
                    None,
                )
                clone_identity = (
                    _json_pointer_value_or_missing(
                        affordance_event.get("response_json"), clone_identity_pointer
                    )
                    if isinstance(clone_identity_pointer, str)
                    else _MISSING
                )
                if (
                    identity_values is None
                    or clone_identity is _MISSING
                    or clone_identity == identity_values["baseline_selector"]
                    or type(clone_identity) is not type(identity_values["result_selector"])
                    or clone_identity != identity_values["result_selector"]
                ):
                    evaluation_uncertainties.append(
                        _evaluation_uncertainty(
                            "browser_compile_api_handoff.state_affordances",
                            "A clone affordance did not prove a distinct physical identity that "
                            "continued into the selected interaction and remains advisory.",
                        )
                    )
                    continue
            affordance_evidence = (
                _captured_api_evidence(session, affordance_event, "request"),
                _captured_api_evidence(session, affordance_event, "response"),
            )
            capability_evidence_by_ref.update(dict(affordance_evidence))
            if affordance_event is setup_event:
                affordance_step_id = "observed-session-setup"
            else:
                affordance_step_id = "observed-" + affordance_kind.replace("_", "-")
                affordance_request = affordance_event.get("request_json")
                if affordance_request is None:
                    affordance_request = {}
                for binding_value, capture_name in capture_names.items():
                    affordance_request = _replace_json_value(
                        affordance_request,
                        binding_value,
                        f"{{{{capture:{capture_name}}}}}",
                    )
                auxiliary_setup_steps.append(
                    {
                        "step_id": affordance_step_id,
                        "endpoint": affordance_url,
                        "method": affordance_method,
                        "request_template": affordance_request,
                        "response_selector": str(affordance_pointer),
                        "success_witness": "captured state-management response is reproduced",
                        "expected_status": int(affordance_event["status"]),
                        "idempotency_key": None,
                        "replay_after_ambiguous_result": False,
                        "cleanup": False,
                        "interaction": False,
                        "captures": {},
                        "repeat": 1,
                    }
                )
            affordance_step_ids[affordance_kind] = affordance_step_id
            state_affordances.append(
                {
                    "kind": affordance_kind,
                    "step_id": affordance_step_id,
                    "evaluation_root": "response_body",
                    "evidence_refs": [reference for reference, _ in affordance_evidence],
                    "confidence": float(raw_affordance.get("confidence", 1.0)),
                }
            )
        if cleanup_event is not None:
            state_affordances.append(
                {
                    "kind": "cleanup_state",
                    "step_id": "observed-cleanup-exchange",
                    "evaluation_root": "response_body",
                    "evidence_refs": cleanup_evidence_refs,
                    "confidence": 1.0,
                }
            )

        checkpoint: dict[str, object] | None = None
        if "restore_checkpoint" in affordance_step_ids:
            raw_checkpoint = arguments.get("checkpoint")
            capture = (
                compile_state_selector(raw_checkpoint.get("capture_selector"))
                if isinstance(raw_checkpoint, Mapping)
                else None
            )
            binding_name = (
                str(raw_checkpoint.get("binding_name") or "")
                if isinstance(raw_checkpoint, Mapping)
                else ""
            )
            restore_pointer = (
                str(raw_checkpoint.get("restore_request_pointer") or "")
                if isinstance(raw_checkpoint, Mapping)
                else ""
            )
            restore_step_id = affordance_step_ids["restore_checkpoint"]
            restore_step = next(
                (step for step in auxiliary_setup_steps if step["step_id"] == restore_step_id),
                setup_step if restore_step_id == "observed-session-setup" else None,
            )
            restore_request = (
                restore_step.get("request_template") if isinstance(restore_step, Mapping) else None
            )
            restored_value = _json_pointer_value_or_missing(restore_request, restore_pointer)
            expected_capture_placeholder = f"{{{{capture:{binding_name}}}}}"
            restore_binding_matches = capture is not None and (
                (type(restored_value) is type(capture[1]) and restored_value == capture[1])
                or restored_value == expected_capture_placeholder
            )
            if (
                capture is None
                or not binding_name
                or capture[0]["step_id"] != "observed-session-setup"
                or restored_value is _MISSING
                or not restore_binding_matches
                or not isinstance(restore_step, dict)
                or not isinstance(setup_step, dict)
            ):
                state_affordances = [
                    item for item in state_affordances if item["kind"] != "restore_checkpoint"
                ]
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        "browser_compile_api_handoff.checkpoint",
                        "The restore affordance lacked an exact setup-captured handle binding "
                        "and remains advisory.",
                    )
                )
            else:
                setup_captures = setup_step.setdefault("captures", {})
                assert isinstance(setup_captures, dict)
                setup_captures[binding_name] = capture[0]["pointer"]
                restore_step["request_template"] = _replace_json_pointer(
                    restore_request,
                    restore_pointer,
                    f"{{{{capture:{binding_name}}}}}",
                )
                checkpoint_evidence_refs = list(
                    dict.fromkeys(
                        [
                            *capture[0]["evidence_refs"],
                            *next(
                                item["evidence_refs"]
                                for item in state_affordances
                                if item["kind"] == "restore_checkpoint"
                            ),
                        ]
                    )
                )
                checkpoint = {
                    "schema_version": "merlin.state-checkpoint.v1",
                    "capture_selector": capture[0],
                    "capture_timing": "live_handle",
                    "binding_name": binding_name,
                    "restore_step_id": restore_step_id,
                    "restore_request_pointer": restore_pointer,
                    "evidence_refs": checkpoint_evidence_refs,
                }
        capability_evidence = tuple(capability_evidence_by_ref.items())
        message_step: dict[str, object] = {
            "step_id": "observed-message-exchange",
            "endpoint": url,
            "method": method,
            "request_template": request_template,
            "response_selector": response_selector,
            "success_witness": success_witness,
            "expected_status": int(event["status"]),
            "idempotency_key": None,
            "replay_after_ambiguous_result": False,
            "cleanup": False,
            "interaction": True,
            "captures": {},
            "repeat": 1,
        }
        try:
            bindings = _observed_request_bindings(
                request_json, request_template, exchanges, method, url, input_pointer
            )
            bindings.update(
                _validated_request_bindings(
                    request_template, arguments.get("request_bindings", {})
                )
            )
        except ValueError as error:
            return ToolResult(content=str(error), is_error=True, metadata={"effect": self.effect})
        if bindings:
            message_step["request_bindings"] = bindings
        for pointer, value in _string_scalar_paths(request_template):
            if pointer in bindings:
                continue
            try:
                parsed = UUID(value)
            except ValueError:
                continue
            if parsed.version == 4:
                evaluation_uncertainties.append(
                    _evaluation_uncertainty(
                        f"api_steps.observed-message-exchange.request_template{pointer}",
                        "This literal UUID has no established per-request generation contract. "
                        "Inspect frontend evidence or compare distinct messages to distinguish "
                        "a constant from a client nonce or a server/session capture. The value "
                        "was retained; UUID shape alone cannot authorize regeneration.",
                    )
                )
        if oracle_predicate is not None:
            message_step["oracle_predicate"] = oracle_predicate
        if terminal_response_contract is not None:
            message_step["terminal_response_contract"] = terminal_response_contract
        adapter: dict[str, Any] = {
            "interface": "http_api",
            "execution_mode": "api",
            "available_execution_modes": ["api", "hybrid"],
            "api_transport": "browser_context",
            "endpoint": url,
            "authentication": str(arguments["authentication"]),
            "observed_evidence": list(
                dict.fromkeys(
                    setup_evidence_refs
                    + creation_transition_evidence_refs
                    + [request_evidence_ref, response_evidence_ref]
                    + terminal_evidence_refs
                    + cleanup_evidence_refs
                )
            ),
            "cleanup_lifecycle": {
                "status": "observed" if cleanup_event is not None else "not_established",
                "evidence_refs": (cleanup_evidence_refs if cleanup_event is not None else []),
            },
            "session_constraint": str(arguments["session_constraint"]),
            "session_cardinality": {
                "status": "unverified",
                "maximum_concurrency": None,
                "source": (
                    "observed_session_lifecycle" if setup_evidence_refs else "not_established"
                ),
                "evidence_refs": (
                    setup_evidence_refs + [request_evidence_ref, response_evidence_ref]
                    if setup_evidence_refs
                    else []
                ),
                "reason": (
                    "one stateful session lifecycle was observed, but no distinct-session "
                    "isolation probe established safe concurrency"
                    if setup_evidence_refs
                    else "no stateful session identity or isolation probe was observed"
                ),
            },
            "interaction_limit": {
                "status": "unverified",
                "maximum_interactions": None,
                "source": (
                    "observed_terminal_exchange"
                    if terminal_evidence_refs
                    else "reviewed_explicit_contract"
                    if terminal_response_contract is not None
                    else "not_established"
                ),
                "evidence_refs": terminal_evidence_refs,
                "reason": (
                    "terminal behavior was observed, but the captured lifecycle did not expose "
                    "an attributable numeric interaction maximum"
                    if terminal_evidence_refs
                    else "a terminal contract was supplied for review, but no captured exchange "
                    "established a numeric interaction maximum"
                    if terminal_response_contract is not None
                    else "no captured exchange established a numeric interaction maximum"
                ),
            },
            "api_validation": {
                "status": "passed",
                "method": method,
                "url": url,
                "request_evidence_ref": request_evidence_ref,
                "response_evidence_ref": response_evidence_ref,
                "expected_status": int(event["status"]),
            },
            "api_steps": ([setup_step] if setup_step is not None else [])
            + auxiliary_setup_steps
            + [message_step]
            + (
                [
                    {
                        "step_id": "observed-cleanup-exchange",
                        "endpoint": cleanup_url,
                        "method": cleanup_method,
                        "request_template": cleanup_request,
                        "response_selector": "",
                        "success_witness": "captured cleanup response status is reproduced",
                        "expected_status": int(cleanup_event["status"]),
                        "idempotency_key": None,
                        "replay_after_ambiguous_result": False,
                        "cleanup": True,
                        "interaction": False,
                        "captures": {},
                        "repeat": 1,
                        "cleanup_acknowledgement": cleanup_acknowledgement,
                        "cleanup_action_evidence_ref": cleanup_action_evidence[0],
                        "cleanup_acknowledgement_observation": {
                            "source": cleanup_acknowledgement_source,
                            "evidence_refs": cleanup_evidence_refs,
                        },
                    }
                ]
                if cleanup_event is not None
                else []
            ),
        }
        if request_constraints:
            adapter["request_constraints"] = request_constraints
            adapter["observed_evidence"] = list(
                dict.fromkeys([*adapter["observed_evidence"], *capability_evidence_by_ref])
            )
        has_state_capabilities = bool(
            state_observations
            or state_enumerations
            or state_affordances
            or supported_modalities
            or resource_controls
            or resource_ownership
        )
        if has_state_capabilities:
            baseline_by_kind = {
                "restore_checkpoint": ("restore", "independent"),
                "clone_state": ("clone", "independent"),
                "reset_state": ("reset", "serialized"),
            }
            selected_baseline = next(
                (
                    baseline_by_kind[str(item["kind"])]
                    for item in state_affordances
                    if str(item["kind"]) in baseline_by_kind
                ),
                None,
            )
            lifecycle_evidence_refs = list(
                dict.fromkeys(
                    reference for item in state_affordances for reference in item["evidence_refs"]
                )
            )
            adapter["state_capabilities"] = {
                "schema_version": "merlin.state-capabilities.v1",
                "observations": state_observations,
                **({"enumerations": state_enumerations} if state_enumerations else {}),
                **({"corroboration_rules": corroboration_rules} if corroboration_rules else {}),
                **(
                    {"identity_equivalence_rules": identity_equivalence_rules}
                    if identity_equivalence_rules
                    else {}
                ),
                **({"resource_ownership": resource_ownership} if resource_ownership else {}),
                **({"resource_controls": resource_controls} if resource_controls else {}),
                **({"supported_modalities": supported_modalities} if supported_modalities else {}),
                **({"affordances": state_affordances} if state_affordances else {}),
                **({"checkpoint": checkpoint} if checkpoint is not None else {}),
                **(
                    {
                        "lifecycle": {
                            "baseline": (
                                selected_baseline[0] if selected_baseline is not None else "unknown"
                            ),
                            "isolation": (
                                selected_baseline[1] if selected_baseline is not None else "unknown"
                            ),
                            "cleanup": ("explicit" if cleanup_event is not None else "unknown"),
                            "evidence_refs": lifecycle_evidence_refs,
                            "confidence": min(
                                float(item["confidence"]) for item in state_affordances
                            ),
                        }
                    }
                    if lifecycle_evidence_refs
                    else {}
                ),
                **(
                    {"identity": reviewed_state_identity}
                    if reviewed_state_identity is not None
                    else {}
                ),
            }
            adapter["observed_evidence"] = list(
                dict.fromkeys([*adapter["observed_evidence"], *capability_evidence_by_ref])
            )
        if setup_step is not None:
            cleanup_discovery_status = (
                "satisfied" if cleanup_event is not None else "required_before_target_execution"
            )
            adapter["cleanup_discovery"] = {
                "status": cleanup_discovery_status,
                "session_ownership": session_ownership,
                "failure_code": (
                    None
                    if cleanup_event is not None
                    else "cleanup_discovery_required"
                    if session_ownership == "owned"
                    else "session_not_owned"
                ),
                "retryable": cleanup_event is None,
            }
            if creation_transition_evidence:
                transition = creation_transition
                assert isinstance(transition, Mapping)
                adapter["session_creation_transition"] = {
                    "precondition": dict(transition["precondition"]),
                    "remote_session_id": session_value,
                    "postcondition": dict(transition["postcondition"]),
                    "evidence_refs": creation_transition_evidence_refs,
                    **(
                        {"source": transition["source"]}
                        if isinstance(transition.get("source"), str) and transition["source"]
                        else {}
                    ),
                }
                setup_response = setup_event.get("response_json")
                setup_status = setup_event.get("status")
                setup_method = str(setup_event.get("method") or "").upper()
                if (
                    setup_method in {"POST", "PUT", "PATCH"}
                    and type(setup_status) is int
                    and 200 <= setup_status <= 299
                    and isinstance(setup_response, Mapping)
                    and setup_response.get("resumed") is False
                ):
                    # Ownership and runtime freshness are different facts.  Keep
                    # the GET-before/after transition as ownership evidence while
                    # also sealing the exact setup response that proves a reusable
                    # worker invocation received a fresh physical session.
                    adapter["session_freshness_predicate"] = {
                        "source": "authoritative_fresh_setup_response",
                        "method": setup_method,
                        "url": str(setup_event.get("url") or ""),
                        "status_codes": [setup_status],
                        "pointer": "/resumed",
                        "match_values": [False],
                        "reviewed_remote_session_id": session_value,
                        "evidence_refs": list(
                            dict.fromkeys(
                                [*creation_transition_evidence_refs, *setup_evidence_refs]
                            )
                        ),
                    }
            state_capabilities = adapter.get("state_capabilities")
            if (
                isinstance(state_capabilities, dict)
                and reviewed_state_identity is not None
                and "session_freshness_predicate" in adapter
            ):
                freshness = adapter["session_freshness_predicate"]
                assert isinstance(freshness, Mapping)
                freshness_evidence_refs = list(freshness["evidence_refs"])
                affordances = list(state_capabilities.get("affordances", []))
                affordances.insert(
                    0,
                    {
                        "kind": "provision_fresh_state",
                        "step_id": "observed-session-setup",
                        "evaluation_root": "response_body",
                        "evidence_refs": freshness_evidence_refs,
                        "confidence": 1.0,
                    },
                )
                if cleanup_event is not None and not any(
                    item.get("kind") == "cleanup_state" for item in affordances
                ):
                    affordances.append(
                        {
                            "kind": "cleanup_state",
                            "step_id": "observed-cleanup-exchange",
                            "evaluation_root": "response_body",
                            "evidence_refs": cleanup_evidence_refs,
                            "confidence": 1.0,
                        }
                    )
                state_capabilities["affordances"] = affordances
                state_capabilities["freshness_rules"] = [
                    {
                        "selector": {
                            "schema_version": "merlin.state-selector.v1",
                            "step_id": "observed-session-setup",
                            "evaluation_root": "response_body",
                            "pointer": freshness["pointer"],
                            "evidence_refs": freshness_evidence_refs,
                            "confidence": 1.0,
                        },
                        "match_values": list(freshness["match_values"]),
                        "evidence_refs": freshness_evidence_refs,
                    }
                ]
                lifecycle_evidence_refs = list(
                    dict.fromkeys(
                        [
                            *freshness_evidence_refs,
                            *cleanup_evidence_refs,
                            *(
                                state_capabilities.get("lifecycle", {}).get("evidence_refs", [])
                                if isinstance(state_capabilities.get("lifecycle"), Mapping)
                                else []
                            ),
                        ]
                    )
                )
                state_capabilities["lifecycle"] = {
                    "isolation": "independent",
                    "baseline": "fresh",
                    "cleanup": ("explicit" if cleanup_event is not None else "unknown"),
                    "evidence_refs": lifecycle_evidence_refs,
                    "confidence": 1.0,
                }
        # Captured request/response evidence is sufficient to seal the executable
        # transport. A second target call is not routine admission bureaucracy;
        # live execution records any state drift as retryable infrastructure evidence.
        adapter["api_validation"]["fresh_session_replay"] = "not_required"
        if evaluation_uncertainties:
            adapter["evaluation_uncertainties"] = _dedupe_uncertainties(evaluation_uncertainties)
        try:
            compilation, compiled_adapter, compiled_adapter_digest = _canonical_compilation(
                adapter,
                allowed_origins=getattr(session.bounds, "allowed_origins", ()),
            )
        except AdapterCompileError as error:
            return _compile_error_result(error, effect=self.effect)
        compiler_uncertainties = _compilation_uncertainties(compilation)
        if compiler_uncertainties:
            evaluation_uncertainties.extend(compiler_uncertainties)
            adapter["evaluation_uncertainties"] = _dedupe_uncertainties(evaluation_uncertainties)
        setup_response = setup_event.get("response_json") if setup_event is not None else None
        return _DerivedApiHandoff(
            adapter=adapter,
            compiled_adapter=compiled_adapter,
            compiled_adapter_digest=compiled_adapter_digest,
            method=method,
            url=url,
            success_witness=success_witness,
            request_evidence_ref=request_evidence_ref,
            request_evidence=request_evidence,
            response_evidence_ref=response_evidence_ref,
            response_evidence=response_evidence,
            setup_evidence=setup_evidence,
            creation_transition_evidence=creation_transition_evidence,
            terminal_evidence=terminal_evidence,
            cleanup_evidence=cleanup_evidence,
            capability_evidence=capability_evidence,
            recon_observations=recon_observations,
            stateful_reuse_is_unsafe=setup_step is not None
            and (
                cleanup_event is None
                or (isinstance(setup_response, Mapping) and setup_response.get("resumed") is True)
            ),
        )


def _observed_request_bindings(
    request: object,
    template: object,
    exchanges: tuple[dict[str, object], ...],
    method: str,
    url: str,
    input_pointer: str,
) -> dict[str, str]:
    """Recognize changing client UUIDs, never UUID-shaped constants or captures."""
    candidates = [
        event
        for event in exchanges
        if event.get("method") == method
        and event.get("url") == url
        and isinstance(event.get("status"), int)
        and 200 <= event["status"] < 300
    ]
    response_values = {
        value
        for event in exchanges
        for _, value in _string_scalar_paths(event.get("response_json"))
    }
    bindings: dict[str, str] = {}
    for pointer, value in _string_scalar_paths(request):
        if pointer == input_pointer or value in response_values:
            continue
        # A capture placeholder at this exact path takes precedence over generation.
        literal = _json_pointer_value_or_missing(template, pointer)
        if not isinstance(literal, str) or "{{" in literal:
            continue
        observations: dict[str, str] = {}
        for event in candidates:
            candidate = _json_pointer_value_or_missing(event.get("request_json"), pointer)
            prompt = _json_pointer_value_or_missing(event.get("request_json"), input_pointer)
            if not isinstance(candidate, str) or not isinstance(prompt, str):
                continue
            try:
                parsed = UUID(candidate)
            except ValueError:
                continue
            if (
                parsed.version == 4
                and candidate == str(parsed)
                and candidate not in response_values
            ):
                observations[prompt] = candidate
        if len(observations) >= 2 and len(set(observations.values())) == len(observations):
            bindings[pointer] = "uuid4"
    return bindings


def _validated_request_bindings(
    request_template: object, bindings: object
) -> dict[str, str]:
    """Canonicalize an unambiguous leaf name before strict binding validation."""
    if not isinstance(bindings, Mapping):
        return validate_request_bindings(request_template, bindings)
    scalar_paths = _string_scalar_paths(request_template)
    scalar_path_names = {candidate for candidate, _value in scalar_paths}
    canonical: dict[object, object] = {}
    for destination, generator in bindings.items():
        pointer = destination
        if isinstance(destination, str):
            normalized = destination.strip()
            if normalized.startswith("/"):
                pointer = normalized
            elif "/" in normalized and f"/{normalized.lstrip('/')}" in scalar_path_names:
                pointer = f"/{normalized.lstrip('/')}"
            else:
                pointer = normalized
        if isinstance(pointer, str) and pointer and not pointer.startswith("/"):
            matches = [
                candidate
                for candidate, _value in scalar_paths
                if candidate.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
                == pointer
            ]
            if len(matches) != 1:
                raise ValueError(
                    "request binding leaf name must identify exactly one request field; "
                    "use an exact JSON Pointer"
                )
            pointer = matches[0]
        if pointer in canonical and canonical[pointer] != generator:
            raise ValueError("request binding destination has conflicting generators")
        canonical[pointer] = generator
    return validate_request_bindings(request_template, canonical)


def _render_template(value: object, variables: Mapping[str, object], input_text: str) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _render_template(item, variables, input_text) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_render_template(item, variables, input_text) for item in value]
    if not isinstance(value, str) or "{{" not in value:
        return value
    if value == "{{input}}":
        return input_text
    if value.startswith("{{capture:") and value.endswith("}}"):
        name = value[len("{{capture:") : -2]
        if name in variables:
            return variables[name]
    raise ValueError("compiled adapter template references an unavailable placeholder")


_FRESH_SESSION_PROBE_INPUT = "Merlin adapter validation probe: harmless policy question."


async def _replay_step(
    requester: Any, step: Mapping[str, Any], variables: dict[str, object]
) -> tuple[object, str | None]:
    """Execute one compiled step; return the live response or an error."""

    step_id = str(step["step_id"])
    try:
        body = bind_request_values(
            _render_template(step["request_template"], variables, _FRESH_SESSION_PROBE_INPUT),
            step.get("request_bindings", {}),
        )
    except ValueError as error:
        return None, f"compiled adapter failed fresh-session replay: {error}"
    try:
        status, response = await requester(step["method"], step["endpoint"], body)
    except Exception as error:  # noqa: BLE001
        return None, f"compiled adapter fresh-session replay of {step_id} failed: {error}"
    if status != int(step["expected_status"]):
        return None, (
            f"compiled adapter fresh-session replay of {step_id} returned HTTP {status}; "
            f"expected {step['expected_status']}"
        )
    for name, pointer in dict(step.get("captures") or {}).items():
        try:
            variables[str(name)] = _json_pointer_value(response, str(pointer))
        except ValueError:
            return None, (
                f"compiled adapter fresh-session replay of {step_id} could not capture "
                f"{pointer} from the live response"
            )
    oracle = step.get("oracle_predicate")
    pointers = (step.get("response_selector") or "", (oracle or {}).get("pointer") or "")
    for pointer in pointers:
        if not pointer:
            continue
        try:
            _json_pointer_value(response, str(pointer))
        except ValueError:
            return None, (
                f"compiled adapter fresh-session replay of {step_id} is missing response "
                f"pointer {pointer} against the live target state; re-run reconnaissance"
            )
    return response, None


async def _fresh_session_replay(requester: Any, adapter: dict[str, Any]) -> str | None:
    """Replay the compiled steps once against live target state; return an error or None."""

    steps = list(adapter["api_steps"])
    cleanup_steps = [step for step in steps if step.get("cleanup") is True]
    variables: dict[str, object] = {}
    forfeited = False
    index = 0
    while index < len(steps):
        step = steps[index]
        response, error = await _replay_step(requester, step, variables)
        if error is not None:
            return error
        if (
            step.get("interaction") is not True
            and step.get("cleanup") is not True
            and isinstance(response, Mapping)
            and response.get("resumed") is True
        ):
            # The target reattached its in-progress server session (resumed=true)
            # instead of starting a fresh one, so validation would exercise a
            # partially exhausted session. Forfeit it through the reviewed cleanup
            # exchange — the captures above carry its session identifier — then
            # restart the replay once.
            if not cleanup_steps:
                step_id = str(step["step_id"])
                return (
                    f"compiled adapter fresh-session replay of {step_id} was auto-resumed "
                    "into the target's in-progress session (resumed=true); your "
                    "reconnaissance session is disposable, so end the session you were "
                    "resumed into through the target's session-ending endpoint (forfeit, "
                    "end, or reset), capture that exchange, and recompile with "
                    "cleanup_method/cleanup_url so validation can start from a fresh session"
                )
            if forfeited:
                step_id = str(step["step_id"])
                return (
                    f"compiled adapter fresh-session replay of {step_id} was auto-resumed "
                    "into the target's in-progress session again after the cleanup "
                    "exchange; the adapter cannot reach a fresh session"
                )
            forfeited = True
            for cleanup_step in cleanup_steps:
                _, cleanup_error = await _replay_step(requester, cleanup_step, variables)
                if cleanup_error is not None:
                    return cleanup_error
            variables = {}
            index = 0
            continue
        index += 1
    return None


def _replace_json_pointer(value: object, pointer: str, replacement: object) -> object:
    """Return a JSON copy with one exact pointer replaced."""
    if not pointer.startswith("/"):
        raise ValueError("input_pointer must be a JSON Pointer")
    result = json.loads(json.dumps(value))
    current = result
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
    for part in parts[:-1]:
        try:
            current = current[int(part)] if isinstance(current, list) else current[part]
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise ValueError("input_pointer does not exist in the captured request") from error
    try:
        if isinstance(current, list):
            current[int(parts[-1])] = replacement
        else:
            current[parts[-1]] = replacement
    except (IndexError, KeyError, TypeError, ValueError) as error:
        raise ValueError("input_pointer does not exist in the captured request") from error
    return result


def _string_scalar_paths(value: object, pointer: str = "") -> tuple[tuple[str, str], ...]:
    """Return JSON Pointer paths for non-empty string scalar values."""
    found: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            token = str(key).replace("~", "~0").replace("/", "~1")
            found.extend(_string_scalar_paths(item, f"{pointer}/{token}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_string_scalar_paths(item, f"{pointer}/{index}"))
    elif isinstance(value, str) and value:
        found.append((pointer, value))
    return tuple(found)


def _json_scalar_paths(value: object, pointer: str = "") -> tuple[tuple[str, object], ...]:
    """Return JSON Pointer paths for non-container scalar values."""
    found: list[tuple[str, object]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            token = str(key).replace("~", "~0").replace("/", "~1")
            found.extend(_json_scalar_paths(item, f"{pointer}/{token}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_json_scalar_paths(item, f"{pointer}/{index}"))
    elif value is None or type(value) in {str, int, float, bool}:
        found.append((pointer, value))
    return tuple(found)


def _reviewed_recon_observations(
    session: BrowserSession,
    declarations: object,
    exchanges: Sequence[Mapping[str, object]],
    *,
    method: str,
    url: str,
) -> tuple[
    tuple[dict[str, object], ...],
    tuple[tuple[str, dict[str, object]], ...],
]:
    """Project reviewed state progress already witnessed during browser recon.

    These receipts describe reconnaissance provenance only. They never become
    executable adapter state or ARM credit. Exact pointers are preferred; when
    a reviewed pointer names the wrong response envelope, a unique scalar leaf
    matching the reviewed observation name may repair the envelope without
    inventing target semantics.
    """
    if not isinstance(declarations, list):
        return (), ()
    candidates = tuple(
        event
        for event in exchanges
        if event.get("method") == method
        and event.get("url") == url
        and type(event.get("status")) is int
        and 200 <= int(event["status"]) <= 299
        and isinstance(event.get("response_json"), (Mapping, list))
    )
    if len(candidates) < 2:
        return (), ()

    def observed_value(
        event: Mapping[str, object], pointer: object, name: str
    ) -> tuple[str, object] | None:
        if isinstance(pointer, str):
            exact = _json_pointer_value_or_missing(event.get("response_json"), pointer)
            if exact is not _MISSING:
                return pointer, exact
        normalized_name = "".join(character for character in name.casefold() if character.isalnum())
        matches = tuple(
            (path, value)
            for path, value in _json_scalar_paths(event.get("response_json"))
            if "".join(
                character
                for character in path.rsplit("/", 1)[-1].casefold()
                if character.isalnum()
            )
            == normalized_name
        )
        return matches[0] if len(matches) == 1 else None

    receipts: list[dict[str, object]] = []
    evidence: dict[str, dict[str, object]] = {}
    for declaration in declarations:
        if not isinstance(declaration, Mapping):
            continue
        name = declaration.get("name")
        baseline_selector = declaration.get("baseline_selector")
        result_selector = declaration.get("result_selector")
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(baseline_selector, Mapping)
            or not isinstance(result_selector, Mapping)
        ):
            continue
        baseline_matches = tuple(
            (event, resolved)
            for event in candidates
            if (
                resolved := observed_value(event, baseline_selector.get("pointer"), name)
            )
            is not None
        )
        result_matches = tuple(
            (event, resolved)
            for event in candidates
            if (resolved := observed_value(event, result_selector.get("pointer"), name))
            is not None
        )
        if not baseline_matches or not result_matches:
            continue
        baseline_event, (baseline_pointer, baseline_value) = baseline_matches[0]
        success_values = declaration.get("success_values")
        successful_results = tuple(
            match
            for match in result_matches
            if isinstance(success_values, list)
            and any(
                type(match[1][1]) is type(expected) and match[1][1] == expected
                for expected in success_values
            )
        )
        result_event, (result_pointer, result_value) = (
            successful_results[-1] if successful_results else result_matches[-1]
        )
        if baseline_event is result_event or type(baseline_value) is not type(result_value):
            continue
        succeeded = isinstance(success_values, list) and any(
            type(result_value) is type(expected) and result_value == expected
            for expected in success_values
        )
        direction = declaration.get("preferred_direction")
        progressed = (
            type(baseline_value) in {int, float}
            and type(result_value) in {int, float}
            and (
                (direction == "increase" and result_value > baseline_value)
                or (direction == "decrease" and result_value < baseline_value)
            )
        )
        if not succeeded and not progressed:
            continue
        evidence_refs: list[str] = []
        for event in (baseline_event, result_event):
            for part in ("request", "response"):
                reference, payload = _captured_api_evidence(session, event, part)
                evidence[reference] = payload
                evidence_refs.append(reference)
        core = {
            "schema": "merlin.browser.recon-observation.v1",
            "name": name,
            "role": str(declaration.get("role") or "custom"),
            "baseline": baseline_value,
            "result": result_value,
            "baseline_pointer": baseline_pointer,
            "result_pointer": result_pointer,
            "outcome": "observed" if succeeded else "partial",
            "success_value_matched": succeeded,
            "result_observed_at": str(
                result_event.get("observed_at")
                or result_event.get("_observation_observed_at")
                or ""
            ),
            "evidence_refs": list(dict.fromkeys(evidence_refs)),
        }
        receipts.append({**core, "digest": _digest_json(core)})
    return tuple(receipts), tuple(evidence.items())


def _string_scalar_values(value: object) -> set[str]:
    return {item for _, item in _string_scalar_paths(value)}


def _capture_name_compatible(response_pointer: str, request_pointer: str) -> bool:
    """Match lifecycle identifiers by semantic field name, never value coincidence alone."""

    def normalized_leaf(pointer: str) -> str:
        leaf = pointer.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
        return "".join(character for character in leaf.lower() if character.isalnum())

    response_name = normalized_leaf(response_pointer)
    request_name = normalized_leaf(request_pointer)
    if not response_name or not request_name:
        return False
    identifier_roots = ("session", "conversation", "thread", "chat", "run")
    identifier_suffixes = ("id", "token", "key")
    has_shared_root = any(
        root in response_name and root in request_name for root in identifier_roots
    )
    has_identifier_suffix = any(
        response_name.endswith(suffix) and request_name.endswith(suffix)
        for suffix in identifier_suffixes
    )
    return has_shared_root and has_identifier_suffix


def _replace_json_value(value: object, expected: str, replacement: str) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _replace_json_value(item, expected, replacement)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_json_value(item, expected, replacement) for item in value]
    return replacement if value == expected else value


_MISSING = object()


def _json_pointer_value_or_missing(value: object, pointer: str) -> object:
    try:
        return _json_pointer_value(value, pointer)
    except ValueError:
        return _MISSING


def _json_pointer_value(value: object, pointer: str) -> object:
    if pointer == "":
        return value
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
            value = value[int(token)]
        else:
            raise ValueError("JSON Pointer does not exist")
    return value


def _normalize_json_pointer(value: str, *, allow_empty: bool = False) -> str:
    """Normalize the simple JSONPath spelling commonly emitted by tool-capable models."""
    if allow_empty and value == "":
        return ""
    if value.startswith("/"):
        return value
    if value.startswith("$."):
        parts = value[2:].split(".")
        if parts and all(part and "[" not in part and "]" not in part for part in parts):
            return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)
    raise ValueError("value must be a JSON Pointer or simple JSONPath")


def _evaluation_uncertainty(source: str, reason: str) -> dict[str, object]:
    return {
        "kind": "evaluation_uncertain",
        "source": source,
        "reason": reason,
        "confidence": 0.0,
    }


def _uncertainty_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    records: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, Mapping):
            records.append({str(key): detail for key, detail in item.items()})
        elif isinstance(item, str) and item.strip():
            records.append(_evaluation_uncertainty("browser.adapter", item.strip()))
    return records


def _dedupe_uncertainties(
    values: list[dict[str, object]],
) -> list[dict[str, object]]:
    unique: dict[str, dict[str, object]] = {}
    for value in values:
        key = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        unique[key] = value
    return list(unique.values())


def _capture_placeholders(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return {name for item in value.values() for name in _capture_placeholders(item)}
    if isinstance(value, list):
        return {name for item in value for name in _capture_placeholders(item)}
    if isinstance(value, str) and value.startswith("{{capture:") and value.endswith("}}"):
        return {value[len("{{capture:") : -2]}
    return set()


def _api_step_origin_error(adapter: Mapping[str, object], allowed_origins: object) -> str | None:
    if not isinstance(allowed_origins, (set, frozenset, list, tuple)) or not allowed_origins:
        return None
    allowed = {str(origin).rstrip("/") for origin in allowed_origins}
    base = adapter.get("endpoint")
    if not isinstance(base, str) or not base.strip():
        return "API handoff adapter endpoint is not executable"
    steps = adapter.get("api_steps")
    if not isinstance(steps, list):
        return "API handoff adapter steps are not executable"
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        endpoint = step.get("endpoint")
        if not isinstance(endpoint, str):
            continue
        resolved = urljoin(base.rstrip("/") + "/", endpoint)
        parts = urlsplit(resolved)
        origin = f"{parts.scheme}://{parts.netloc}".rstrip("/")
        if origin not in allowed:
            return f"API handoff api_steps[{index}] resolves outside the reviewed target origin"
    return None


def _bind_ambiguous_message_aliases(value: object) -> object:
    """Bind both observed top-level prompt aliases to one runtime input."""
    if (
        isinstance(value, Mapping)
        and isinstance(value.get("message"), str)
        and isinstance(value.get("userMessage"), str)
        and "{{input}}" in {value.get("message"), value.get("userMessage")}
    ):
        return {
            **value,
            "message": "{{input}}",
            "userMessage": "{{input}}",
        }
    return value


def normalize_api_adapter_input_aliases(adapter: Mapping[str, Any]) -> dict[str, Any]:
    """Repair legacy API handoffs whose observed prompt alias became ambiguous."""
    normalized = dict(adapter)
    raw_steps = adapter.get("api_steps")
    if not isinstance(raw_steps, list):
        return normalized
    steps: list[object] = []
    for raw_step in raw_steps:
        if not isinstance(raw_step, Mapping):
            steps.append(raw_step)
            continue
        step = dict(raw_step)
        step["request_template"] = _bind_ambiguous_message_aliases(step.get("request_template"))
        steps.append(step)
    normalized["api_steps"] = steps
    return normalized


def _normalize_sealable_api_steps(
    value: object,
) -> tuple[list[object], list[dict[str, object]]]:
    if not isinstance(value, list):
        return [], []
    normalized: list[object] = []
    uncertainties: list[dict[str, object]] = []
    for index, raw_step in enumerate(value):
        if not isinstance(raw_step, Mapping):
            normalized.append(raw_step)
            continue
        step = {str(key): item for key, item in raw_step.items()}
        source = f"api_steps.{step.get('step_id', index)}"

        step["request_template"] = _bind_ambiguous_message_aliases(step.get("request_template"))

        selector = step.get("response_selector", "")
        if not isinstance(selector, str) or (selector and not selector.startswith("/")):
            step["response_selector"] = ""
            uncertainties.append(
                _evaluation_uncertainty(
                    f"{source}.response_selector",
                    "The reviewed response selector was invalid; the complete response will be "
                    "retained for validator judgment.",
                )
            )
        else:
            step["response_selector"] = selector

        witness = step.get("success_witness")
        if not isinstance(witness, str) or not witness.strip():
            step["success_witness"] = "validator judgment over the captured target response"
            uncertainties.append(
                _evaluation_uncertainty(
                    f"{source}.success_witness",
                    "No usable success-witness hint was supplied; execution remains eligible.",
                )
            )

        status = step.get("expected_status")
        if status is not None and (
            isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599
        ):
            step["expected_status"] = None
            uncertainties.append(
                _evaluation_uncertainty(
                    f"{source}.expected_status",
                    "The reviewed expected status was invalid; live status will be recorded "
                    "without blocking the target response.",
                )
            )
        else:
            step.setdefault("expected_status", None)

        replay = step.get("replay_after_ambiguous_result", False)
        if not isinstance(replay, bool):
            step["replay_after_ambiguous_result"] = False
            uncertainties.append(
                _evaluation_uncertainty(
                    f"{source}.replay_after_ambiguous_result",
                    "Invalid replay metadata was ignored; ambiguous effects will not be replayed.",
                )
            )
        else:
            step["replay_after_ambiguous_result"] = replay
        step.setdefault("idempotency_key", None)

        captures = step.get("captures")
        if isinstance(captures, Mapping):
            later_references = {
                name
                for later in value[index + 1 :]
                if isinstance(later, Mapping)
                for name in _capture_placeholders(later.get("request_template"))
            }
            retained: dict[str, str] = {}
            for name, pointer in captures.items():
                valid = (
                    isinstance(name, str)
                    and bool(name.strip())
                    and isinstance(pointer, str)
                    and (not pointer or pointer.startswith("/"))
                )
                if valid:
                    retained[name] = pointer
                elif isinstance(name, str) and name in later_references:
                    retained[name] = pointer  # type: ignore[assignment]
                else:
                    uncertainties.append(
                        _evaluation_uncertainty(
                            f"{source}.captures",
                            "An invalid unused capture hint was omitted; target execution remains "
                            "eligible.",
                        )
                    )
            step["captures"] = retained
        normalized.append(step)
    return normalized, _dedupe_uncertainties(uncertainties)


def _validate_sealable_api_steps(value: object) -> str | None:
    """Validate executable transport and state dependencies before sealing a handoff."""
    if not isinstance(value, list) or not value:
        return "API handoff api_steps must contain at least one complete reviewed step"
    required = {
        "step_id",
        "endpoint",
        "method",
        "request_template",
        "cleanup",
        "interaction",
        "captures",
        "repeat",
    }
    available_captures: set[str] = set()
    for index, step in enumerate(value):
        if not isinstance(step, Mapping):
            return "API handoff api_steps must be structured reviewed records"
        missing = sorted(required - step.keys())
        if missing:
            return (
                f"API handoff api_steps[{index}] is incomplete; missing reviewed fields: "
                + ", ".join(missing)
            )
        for field in ("step_id", "endpoint", "method"):
            if not isinstance(step.get(field), str) or not str(step[field]).strip():
                return f"API handoff api_steps[{index}].{field} must be a non-empty string"
        missing_dependencies = sorted(
            _capture_placeholders(step.get("request_template")) - available_captures
        )
        if missing_dependencies:
            return f"API handoff api_steps[{index}] requires unavailable captures: " + ", ".join(
                missing_dependencies
            )
        selector = step.get("response_selector")
        if not isinstance(selector, str) or (selector and not selector.startswith("/")):
            return (
                f"API handoff api_steps[{index}].response_selector must be a JSON Pointer: "
                "use an empty string for the whole response or /... for a nested value"
            )
        for field in ("cleanup", "interaction", "replay_after_ambiguous_result"):
            if not isinstance(step.get(field), bool):
                return (
                    f"API handoff api_steps[{index}].{field} must be boolean true or false; "
                    "put descriptive text in success_witness instead"
                )
        status = step.get("expected_status")
        if status is not None and (
            isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599
        ):
            return f"API handoff api_steps[{index}].expected_status must be null or 100..599"
        idempotency_key = step.get("idempotency_key")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key.strip()
        ):
            return (
                f"API handoff api_steps[{index}].idempotency_key must be null or a non-empty string"
            )
        if step["replay_after_ambiguous_result"] and idempotency_key is None:
            return (
                f"API handoff api_steps[{index}] cannot replay an ambiguous result without "
                "an idempotency key"
            )
        captures = step.get("captures")
        if not isinstance(captures, Mapping) or not all(
            isinstance(name, str)
            and name.strip()
            and isinstance(pointer, str)
            and (not pointer or pointer.startswith("/"))
            for name, pointer in captures.items()
        ):
            return f"API handoff api_steps[{index}].captures must map names to JSON Pointers"
        available_captures.update(str(name) for name in captures)
        repeat = step.get("repeat")
        if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= 1000:
            return f"API handoff api_steps[{index}].repeat must be an integer from 1 to 1000"
    return None


def _validate_sealable_browser_adapter(value: object) -> str | None:
    """Validate only the transport fields needed to execute a DOM exchange."""
    if not isinstance(value, Mapping):
        return "DOM fallback reusable_adapter must be a structured browser adapter"
    required = {
        "interface",
        "endpoint",
        "browser_steps",
        "interaction_contract",
    }
    missing = sorted(required - value.keys())
    if missing:
        return "DOM fallback reusable_adapter is incomplete; missing: " + ", ".join(missing)
    if value.get("interface") != "browser":
        return "DOM fallback reusable_adapter.interface must be browser"
    steps = value.get("browser_steps")
    if not isinstance(steps, list) or not steps:
        return "DOM fallback reusable_adapter requires observed browser_steps"
    contract = value.get("interaction_contract")
    if not isinstance(contract, Mapping):
        return "DOM fallback reusable_adapter requires interaction_contract"
    required_contract = {"input_locator", "submit_action"}
    missing_contract = sorted(required_contract - contract.keys())
    if missing_contract:
        return "DOM fallback interaction_contract is incomplete; missing: " + ", ".join(
            missing_contract
        )
    return None


def _scope_tokens(value: str) -> frozenset[str]:
    """Lowercased alphanumeric tokens of a free-text recon-scope value."""
    return frozenset(re.findall(r"[a-z0-9]+", value.casefold()))


def _passive_session_observation_only(value: object) -> bool:
    """Return true when a stateful adapter can observe but not create a session."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return False
    if not isinstance(value, Mapping) or value.get("interface") != "http_api":
        return False
    transition = value.get("session_creation_transition")
    cleanup_discovery = value.get("cleanup_discovery")
    if (
        isinstance(transition, Mapping)
        and isinstance(transition.get("precondition"), Mapping)
        and bool(transition["precondition"])
        and isinstance(transition.get("postcondition"), Mapping)
        and bool(transition["postcondition"])
        and isinstance(transition.get("remote_session_id"), str)
        and bool(transition["remote_session_id"])
        and isinstance(transition.get("evidence_refs"), (list, tuple))
        and any(
            isinstance(reference, str) and bool(reference)
            for reference in transition["evidence_refs"]
        )
        and isinstance(cleanup_discovery, Mapping)
        and cleanup_discovery.get("session_ownership") == "owned"
    ):
        return False
    steps = value.get("api_steps")
    if not isinstance(steps, (list, tuple)):
        return False
    session_identity_setup = tuple(
        step
        for step in steps
        if isinstance(step, Mapping)
        and step.get("interaction") is not True
        and step.get("cleanup") is not True
        and isinstance(step.get("captures"), Mapping)
        and "session_id" in step["captures"]
    )
    return bool(session_identity_setup) and all(
        str(step.get("method") or "").upper() in {"GET", "HEAD"} for step in session_identity_setup
    )


def _scope_values_match(left: str | None, right: str | None) -> bool:
    """Drift-tolerant equality for LLM-authored recon-scope dimensions.

    Scope fields are written fresh by the model on every record/recall, so the
    same dimension drifts in case, spacing, punctuation, and qualifier wording
    (e.g. ``"FareCare"`` vs ``"Breakroom Policy Refund Trap (FareCare)"``).
    Byte-for-byte equality turns that drift into a spurious full
    re-reconnaissance.  We compare normalized token sets and accept a match
    when one side is a subset of the other — one description may be a
    specialization/expansion of the other.  Disjoint or merely overlapping
    token sets still mismatch, so a genuinely different bot/tenant/scenario is
    never promoted to a reuse match.
    """
    if left is None or right is None:
        return left is right
    left_tokens = _scope_tokens(left)
    right_tokens = _scope_tokens(right)
    if not left_tokens or not right_tokens:
        return left == right
    return left_tokens <= right_tokens or right_tokens <= left_tokens


class ReconRecallTool:
    """Find evidence-backed reconnaissance that can be safely reused or narrowed."""

    name = "browser_recall_reconnaissance"
    effect = "workspace_read"

    def __init__(self, resolver: BrowserSessionResolver, recorder: BrowserRecorder) -> None:
        self._resolver = resolver
        self._recorder = recorder

    def describe(self) -> ToolSpec:
        properties = {
            name: {"type": "string", "minLength": 1}
            for name in (
                "surface",
                "authentication_context",
                "tenant",
                "agent_or_bot",
                "scenario",
                "session_constraint",
                "oracle_contract",
            )
        }
        return ToolSpec(
            name=self.name,
            description=(
                "Before full reconnaissance, find durable evidence for this target. Exact scenario "
                "matches default to a lightweight validation probe. Shared-site matches reuse site "
                "facts but require bot-specific reconnaissance. Omit authentication_context when "
                "it has not been observed; never guess it. An authentication mismatch may return "
                "only authentication-neutral site facts. Tenant mismatches are never returned. "
                "This lookup is synchronous: its returned result is "
                "final, no callback will arrive, and it must not be repeated while waiting. Follow "
                "the returned next_action immediately."
            ),
            input_schema={
                "type": "object",
                "properties": properties,
                "required": ["surface", "agent_or_bot"],
                "additionalProperties": False,
            },
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        resolved = self._resolver(context.session_id)
        session = await resolved if isawaitable(resolved) else resolved
        desired = {
            str(key): str(value).strip()
            for key, value in arguments.items()
            if isinstance(value, str) and value.strip()
        }
        exact: list[dict[str, object]] = []
        partial: list[dict[str, object]] = []
        handoffs = await self._recorder.list_handoffs(session.bounds.target_id)
        compatible_tenants = {
            tuple(sorted(_scope_tokens(str(handoff.recon_scope["tenant"]))))
            for handoff in handoffs
            if handoff.recon_scope
            and handoff.recon_scope.get("tenant")
            and _scope_values_match(desired.get("surface"), handoff.recon_scope.get("surface"))
            and _scope_values_match(
                desired.get("agent_or_bot"), handoff.recon_scope.get("agent_or_bot")
            )
        }
        tenant_omission_is_unambiguous = len(compatible_tenants) <= 1
        for handoff in handoffs:
            scope = handoff.recon_scope
            if not scope:
                continue
            desired_tenant = desired.get("tenant")
            stored_tenant = scope.get("tenant")
            if desired_tenant is not None and not _scope_values_match(
                desired_tenant, stored_tenant
            ):
                continue
            if (
                desired_tenant is None
                and stored_tenant is not None
                and not tenant_omission_is_unambiguous
            ):
                continue
            authentication_matches = desired.get(
                "authentication_context"
            ) is not None and _scope_values_match(
                desired.get("authentication_context"),
                scope.get("authentication_context"),
            )
            site_facts = handoff.reusable_site_facts
            if not authentication_matches:
                site_facts = _authentication_neutral_site_facts(site_facts)
            if site_facts is None:
                continue
            shared = {
                "handoff_id": handoff.id,
                "captured_at": handoff.created_at.isoformat(),
                "request_evidence_ref": handoff.request_evidence_ref,
                "response_evidence_ref": handoff.response_evidence_ref,
                "recon_scope": dict(scope),
                "shared_site_facts": site_facts,
                "authentication_reusable": authentication_matches,
            }
            differing = sorted(
                key
                for key in (
                    "surface",
                    "authentication_context",
                    "agent_or_bot",
                    "scenario",
                    "session_constraint",
                    "oracle_contract",
                )
                if not _scope_values_match(desired.get(key), scope.get(key))
            )
            if (
                not differing
                and authentication_matches
                and handoff.reusable_adapter is not None
                and not _passive_session_observation_only(handoff.reusable_adapter)
            ):
                exact.append(
                    {
                        **shared,
                        "match": "exact_scenario",
                        "default_action": "lightweight_validation",
                        "reusable_adapter": handoff.reusable_adapter,
                        "dom_fallback_reason": handoff.dom_fallback_reason,
                    }
                )
            else:
                partial.append(
                    {
                        **shared,
                        "match": "shared_site",
                        "different_dimensions": differing,
                        "default_action": "bot_specific_reconnaissance",
                    }
                )
        default_action = (
            "lightweight_validation"
            if exact
            else "bot_specific_reconnaissance"
            if partial
            else "full_reconnaissance"
        )
        next_action = (
            "Run one lightweight validation probe against the exact match."
            if exact
            else "Reuse only shared site facts and begin bot-specific reconnaissance."
            if partial
            else "Call browser_open_tab now and begin full reconnaissance."
        )
        projected_exact = (
            [max(exact, key=lambda item: (str(item["captured_at"]), str(item["handoff_id"])))]
            if exact
            else []
        )
        projected_partial = (
            [
                max(
                    partial,
                    key=lambda item: (
                        -len(item["different_dimensions"]),
                        bool(item["authentication_reusable"]),
                        str(item["captured_at"]),
                        str(item["handoff_id"]),
                    ),
                )
            ]
            if partial
            else []
        )

        def projection_summary(
            eligible: list[dict[str, object]], returned: list[dict[str, object]]
        ) -> dict[str, object]:
            returned_ids = {str(item["handoff_id"]) for item in returned}
            omitted = [item for item in eligible if str(item["handoff_id"]) not in returned_ids]
            omitted_digest = None
            if omitted:
                encoded = json.dumps(omitted, sort_keys=True, separators=(",", ":")).encode()
                omitted_digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
            return {
                "eligible_count": len(eligible),
                "returned_count": len(returned),
                "omitted_count": len(omitted),
                "omitted_digest": omitted_digest,
            }

        result = {
            "target_id": session.bounds.target_id,
            "recall_status": "complete",
            "callback_pending": False,
            "exact_matches": projected_exact,
            "partial_matches": projected_partial,
            "match_projection": {
                "exact": projection_summary(exact, projected_exact),
                "partial": projection_summary(partial, projected_partial),
            },
            "default_action": default_action,
            "next_action": next_action,
            "instruction": (
                "Recall is complete and no callback will arrive. Do not call "
                "browser_recall_reconnaissance again while waiting; perform next_action now."
            ),
            "operator_options": [
                "reuse_after_lightweight_validation",
                "bot_specific_reconnaissance",
                "full_reconnaissance",
            ],
            "sealing_requirement": (
                "After current-session validation or reconnaissance, compile captured JSON "
                "setup/message/cleanup with browser_compile_api_handoff. Use "
                "browser_record_api_handoff only for an evidence-backed DOM fallback or an "
                "exceptional API lifecycle that cannot be compiled. Never pass an older "
                "session's handoff directly to assessment_prepare."
            ),
        }
        return ToolResult(content=json.dumps(result, sort_keys=True), metadata=result)


def _authentication_neutral_site_facts(value: str | None) -> str | None:
    """Project reusable site facts without authentication or scenario identity."""
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    sensitive_fragments = {
        "agent",
        "auth",
        "bot",
        "cookie",
        "credential",
        "identity",
        "login",
        "oracle",
        "profile",
        "session",
        "tenant",
        "token",
        "user",
    }
    projected = {
        str(key): item
        for key, item in parsed.items()
        if not any(fragment in str(key).casefold() for fragment in sensitive_fragments)
    }
    if not projected:
        return None
    return json.dumps(projected, sort_keys=True, separators=(",", ":"))


def browser_tools(
    resolver: BrowserSessionResolver,
    recorder: BrowserRecorder | None = None,
    adapter_resolver: ReviewedBrowserAdapterResolver | None = None,
    state_scope_store: object | None = None,
    checkpoint_store: BrowserCheckpointStore | None = None,
    checkpoint_vault: BrowserCheckpointVault | None = None,
) -> tuple[
    BrowserTool
    | BrowserOwnedSessionCleanupTool
    | ReconRecallTool
    | ApiReconHandoffTool
    | BrowserChatExchangeTool
    | CompiledApiReconHandoffTool,
    ...,
]:
    failures: dict[tuple[str, ...], int] = {}
    network_seen: dict[tuple[str, ...], deque[str]] = {}
    actions: tuple[
        BrowserTool
        | BrowserOwnedSessionCleanupTool
        | ReconRecallTool
        | ApiReconHandoffTool
        | BrowserChatExchangeTool
        | CompiledApiReconHandoffTool,
        ...,
    ] = tuple(
        BrowserTool(name, resolver, recorder, failures, network_seen, state_scope_store)
        for name in _SPECS
    )
    actions = (
        *actions,
        BrowserOwnedSessionCleanupTool(
            resolver,
            recorder,
            failures,
            state_scope_store,
        ),
    )
    if recorder is not None:
        actions = (
            *actions,
            ReconRecallTool(resolver, recorder),
            ApiReconHandoffTool(resolver, recorder),
            CompiledApiReconHandoffTool(resolver, recorder),
        )
        if adapter_resolver is not None:
            actions = (
                *actions,
                BrowserChatExchangeTool(
                    resolver,
                    recorder,
                    adapter_resolver,
                    state_scope_store,
                    checkpoint_store,
                    checkpoint_vault,
                ),
            )
    return actions


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _canonical_object_string(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(f"{label} must be canonical JSON") from error
    else:
        raise ValueError(f"{label} must be a JSON object")
    if not isinstance(parsed, Mapping) or not parsed:
        raise ValueError(f"{label} must be a non-empty JSON object")
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"))


def _browser_oracle_predicates(
    contract: Mapping[str, object],
) -> tuple[tuple[Mapping[str, object], ...], str | None]:
    """Parse optional reviewed oracle predicates with the exact API oracle semantics."""
    values = contract.get("oracle_predicates")
    if values is None:
        legacy = contract.get("oracle_predicate")
        if legacy is None:
            return (), None
        predicates: tuple[object, ...] = (legacy,)
    else:
        if not isinstance(values, list) or not values:
            return (), "reviewed browser oracle_predicates must be a non-empty array"
        predicates = tuple(values)
    parsed: list[Mapping[str, object]] = []
    for item in predicates:
        error = _validate_browser_oracle_predicate(item)
        if error is not None:
            return (), error
        assert isinstance(item, Mapping)
        parsed.append(item)
    purposes = [str(item.get("purpose", "objective")) for item in parsed]
    if len(set(purposes)) != len(purposes):
        return (), "reviewed browser oracle predicate purposes must be unique"
    return tuple(parsed), None


def _validate_browser_oracle_predicate(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return "reviewed browser oracle predicate must be an object"
    digest = value.get("oracle_digest")
    if not isinstance(digest, str) or not _sha256_digest(digest):
        return "reviewed browser oracle predicate requires a SHA-256 oracle digest"
    pointer = value.get("pointer", "")
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        return "reviewed browser oracle predicate pointer must be empty or a JSON Pointer"
    match_values = value.get("match_values")
    if not isinstance(match_values, list) or not match_values:
        return "reviewed browser oracle predicate requires finite match values"
    if any(not _json_scalar(item) for item in match_values):
        return "reviewed browser oracle match values must be JSON scalars"
    purpose = str(value.get("purpose", "objective"))
    if purpose not in {"objective", "refusal", "progress", "invalid-premise"}:
        return "reviewed browser oracle predicate purpose is invalid"
    if not all(
        isinstance(value.get(field), str) and str(value[field]).strip()
        for field in ("modality", "turn", "language")
    ):
        return "reviewed browser oracle predicate dimensions must not be blank"
    return None


def _browser_oracle_observation(
    adapter_digest: str,
    exchange_id: str,
    predicate: Mapping[str, object],
    response: object,
) -> dict[str, object]:
    pointer = str(predicate.get("pointer", ""))
    try:
        selected = _select(response, pointer, exchange_id)
    except ValueError:
        matched: bool | None = None
        selected_digest = None
    else:
        match_values = predicate["match_values"]
        matched = isinstance(match_values, list) and any(
            _typed_equal(selected, expected) for expected in match_values
        )
        selected_digest = _digest_json(selected)
    binding = {
        "adapter_digest": adapter_digest,
        "step_id": exchange_id,
        "oracle_digest": predicate["oracle_digest"],
        "pointer": pointer,
        "match_values": predicate["match_values"],
        "modality": predicate["modality"],
        "turn": predicate["turn"],
        "language": predicate["language"],
        "purpose": str(predicate.get("purpose", "objective")),
    }
    observation = {
        "schema": "merlin.api-oracle-observation.v2",
        **binding,
        "binding_digest": _digest_json(binding),
        "selected_digest": selected_digest,
        "matched": matched,
    }
    return {**observation, "observation_digest": _digest_json(observation)}


def _response_witness_text(text: str, witness: str) -> str | None:
    """Return the latest visible response line proven by the reviewed witness."""
    return next(
        (line.strip() for line in reversed(text.splitlines()) if witness in line),
        None,
    )


def _select(value: object, pointer: str, exchange_id: str) -> object:
    if pointer == "":
        return value
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
            value = value[int(token)]
        else:
            raise ValueError(f"reviewed browser response pointer missing at {exchange_id}")
    return value


def _reviewed_adapter_digest(adapter: Mapping[str, object]) -> str:
    return executable_adapter_digest(adapter)


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


def _digest_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
