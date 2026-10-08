"""Pure, deterministic worker-context projection.

The policy in this module does not decide whether work may run or whether an
attack succeeded.  It only projects durable worker state into a bounded model
request while keeping the immutable assignment prefix cacheable.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Container, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum

TokenEstimator = Callable[[str], int]


class ProtocolRole(StrEnum):
    """Role of one event in a complete provider protocol unit."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class AssignmentPrefix:
    """Immutable worker identity and objective retained byte-for-byte."""

    system_instruction: str
    assignment: str
    identity: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.system_instruction.strip() or not self.assignment.strip():
            raise ValueError("assignment prefix fields must not be blank")


@dataclass(frozen=True, slots=True)
class EvidenceReceipt:
    """Attributable durable replacement for a superseded tool payload."""

    receipt_id: str
    tool_call_id: str
    tool_name: str
    content_digest: str
    summary: str

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.receipt_id,
                self.tool_call_id,
                self.tool_name,
                self.content_digest,
                self.summary,
            )
        ):
            raise ValueError("evidence receipt fields must not be blank")


@dataclass(frozen=True, slots=True)
class ReceiptPointer:
    """Small model-context pointer to a receipt retained in durable storage."""

    receipt_id: str
    tool_call_id: str
    tool_name: str
    content_digest: str
    summary: str


@dataclass(frozen=True, slots=True)
class ReceiptManifest:
    """Bounded semantic index over every receipt omitted from verbatim history."""

    receipt_count: int
    manifest_digest: str
    recent_semantic_receipts: tuple[ReceiptPointer, ...]
    omitted_semantic_count: int


@dataclass(frozen=True, slots=True)
class RequestedToolCall:
    """Assistant-requested tool identity used to validate returned results."""

    call_id: str
    tool_name: str

    def __post_init__(self) -> None:
        if not self.call_id.strip() or not self.tool_name.strip():
            raise ValueError("requested tool call fields must not be blank")


@dataclass(frozen=True, slots=True)
class ProtocolEvent:
    """One message or tool result within an indivisible protocol unit."""

    role: ProtocolRole
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    requested_tool_calls: tuple[RequestedToolCall, ...] = ()
    durable_receipt: EvidenceReceipt | None = None

    def __post_init__(self) -> None:
        if self.role is ProtocolRole.TOOL and not self.tool_call_id:
            raise ValueError("tool events require a tool_call_id")
        if self.role is not ProtocolRole.TOOL and self.tool_call_id is not None:
            raise ValueError("only tool events may carry a tool_call_id")
        if self.role is ProtocolRole.TOOL and not self.tool_name:
            raise ValueError("tool events require a tool_name")
        if self.role is not ProtocolRole.TOOL and self.tool_name is not None:
            raise ValueError("only tool events may name an executed tool")
        if self.role is not ProtocolRole.TOOL and self.durable_receipt is not None:
            raise ValueError("only tool events may carry durable receipts")
        if self.durable_receipt is not None and (
            self.durable_receipt.tool_call_id != self.tool_call_id
        ):
            raise ValueError("receipt must attribute the same tool call")
        if self.durable_receipt is not None and (
            self.durable_receipt.tool_name != self.tool_name
            or self.durable_receipt.content_digest != _content_digest(self.content)
        ):
            raise ValueError("receipt must attribute the same tool result content")
        if self.requested_tool_calls and self.role is not ProtocolRole.ASSISTANT:
            raise ValueError("only assistant events may request tool calls")


@dataclass(frozen=True, slots=True)
class ProtocolUnit:
    """A complete conversational turn, including all requested tool results."""

    events: tuple[ProtocolEvent, ...]

    def __post_init__(self) -> None:
        if not self.events:
            raise ValueError("protocol units must not be empty")
        pending: dict[str, str] = {}
        returned: set[str] = set()
        for event in self.events:
            if event.requested_tool_calls:
                requested = {call.call_id: call.tool_name for call in event.requested_tool_calls}
                if pending or len(requested) != len(event.requested_tool_calls):
                    raise ValueError("tool requests must be unique and non-overlapping")
                pending = requested
                continue
            if event.role is ProtocolRole.TOOL:
                if event.tool_call_id not in pending or event.tool_call_id in returned:
                    raise ValueError("tool results must follow one matching request exactly once")
                if event.tool_name != pending[event.tool_call_id]:
                    raise ValueError("tool result name must match the assistant request")
                returned.add(event.tool_call_id)
                continue
            if pending and returned != pending.keys():
                raise ValueError("protocol content cannot interrupt pending tool results")
            pending.clear()
            returned.clear()
        if pending and returned != pending.keys():
            raise ValueError("protocol units must contain every requested tool result")


@dataclass(frozen=True, slots=True)
class StrategyFact:
    """One named, typed strategy-state fact."""

    name: str
    value: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("strategy fact names must not be blank")


@dataclass(frozen=True, slots=True)
class EvidenceObservation:
    """One typed evidence observation and its optional durable source."""

    kind: str
    value: str
    receipt_id: str | None = None

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.value.strip():
            raise ValueError("evidence observation kind and value must not be blank")


@dataclass(frozen=True, slots=True)
class StrategyDelta:
    """Typed strategy state changed since the immutable assignment."""

    strategy: str
    phase: str
    state: tuple[StrategyFact, ...] = ()

    def __post_init__(self) -> None:
        if not self.strategy.strip() or not self.phase.strip():
            raise ValueError("strategy and phase must not be blank")
        names = [fact.name for fact in self.state]
        if len(names) != len(set(names)):
            raise ValueError("strategy fact names must be unique")


@dataclass(frozen=True, slots=True)
class EvidenceDelta:
    """Typed evidence added since the prior projection boundary."""

    receipts: tuple[EvidenceReceipt, ...] = ()
    observations: tuple[EvidenceObservation, ...] = ()


@dataclass(frozen=True, slots=True)
class ProgressSignal:
    """Positive semantic progress facts; absence does not imply a stall."""

    markers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StallSignal:
    """Explicit no-progress facts; kept separate from progress evidence."""

    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UsagePressure:
    """Provider/request usage used to decide when projection is worthwhile."""

    context_window_tokens: int
    cumulative_input_tokens: int = 0
    cumulative_cache_read_tokens: int = 0
    cumulative_cost_usd: float = 0.0
    cost_budget_usd: float | None = None

    def __post_init__(self) -> None:
        if self.context_window_tokens <= 0:
            raise ValueError("context window must be positive")
        if self.cumulative_input_tokens < 0 or self.cumulative_cache_read_tokens < 0:
            raise ValueError("cumulative token usage cannot be negative")
        if self.cumulative_cost_usd < 0:
            raise ValueError("cumulative cost cannot be negative")
        if self.cost_budget_usd is not None and self.cost_budget_usd <= 0:
            raise ValueError("cost budget must be positive when supplied")


@dataclass(frozen=True, slots=True)
class ProjectionPolicy:
    """Pressure ratios plus an optional verbatim exchange window.

    ``recent_exchange_window`` is a context-economics bound, not a work
    ceiling: it only decides how many target-exchange protocol units stay
    verbatim in the provider request; every older unit keeps its durable,
    attributable receipt in the bounded manifest and the compacted history
    digest, so no evidence is dropped from the durable record.
    """

    context_trigger_ratio: float = 0.80
    context_target_ratio: float = 0.55
    cumulative_churn_trigger_windows: float = 4.0
    cost_trigger_ratio: float = 0.80
    recent_exchange_window: int | None = None
    # Soft cap on the semantic receipt summaries rendered into the
    # provider-facing manifest; the digest still binds every omitted receipt.
    receipt_manifest_budget_tokens: int = 4_096

    def __post_init__(self) -> None:
        if not 0 < self.context_target_ratio < self.context_trigger_ratio < 1:
            raise ValueError("context ratios must satisfy 0 < target < trigger < 1")
        if self.cumulative_churn_trigger_windows <= 0:
            raise ValueError("cumulative churn trigger must be positive")
        if not 0 < self.cost_trigger_ratio <= 1:
            raise ValueError("cost trigger ratio must be in (0, 1]")
        if self.recent_exchange_window is not None and (
            isinstance(self.recent_exchange_window, bool)
            or not isinstance(self.recent_exchange_window, int)
            or self.recent_exchange_window < 1
        ):
            raise ValueError("recent exchange window must be a positive integer")
        if (
            isinstance(self.receipt_manifest_budget_tokens, bool)
            or self.receipt_manifest_budget_tokens < 1
        ):
            raise ValueError("receipt manifest budget must be a positive integer")


@dataclass(frozen=True, slots=True)
class ContextProjection:
    """Deterministic context shared by live execution and checkpoint resume."""

    assignment_prefix: AssignmentPrefix
    assignment_cache_key: str
    strategy_delta: StrategyDelta
    evidence_delta: EvidenceDelta
    recent_protocol_tail: tuple[ProtocolUnit, ...]
    compacted_receipts: ReceiptManifest | None
    compacted_history_digest: str | None
    estimated_input_tokens: int
    target_input_tokens: int
    fits_context: bool
    used_provider_tokenizer: bool
    pressure_reasons: tuple[str, ...]
    progress: ProgressSignal
    stall: StallSignal


def project_worker_context(
    *,
    assignment_prefix: AssignmentPrefix,
    strategy_delta: StrategyDelta,
    evidence_delta: EvidenceDelta,
    protocol: Sequence[ProtocolUnit],
    usage: UsagePressure,
    progress: ProgressSignal | None = None,
    stall: StallSignal | None = None,
    tokenizer: TokenEstimator | None = None,
    policy: ProjectionPolicy | None = None,
    exchange_tool_names: Container[str] = (),
) -> ContextProjection:
    """Project one worker request from durable inputs without run-mode state."""

    progress = progress or ProgressSignal()
    stall = stall or StallSignal()
    policy = policy or ProjectionPolicy()
    units = tuple(protocol)
    prefix_payload = _canonical_json(assignment_prefix)
    assignment_cache_key = hashlib.sha256(prefix_payload.encode()).hexdigest()
    full_payload = _projection_payload(
        assignment_prefix, strategy_delta, evidence_delta, units, None, None
    )
    full_tokens = _estimate_tokens(full_payload, tokenizer)
    churn = usage.cumulative_input_tokens + usage.cumulative_cache_read_tokens
    pressure_reasons: list[str] = []
    if full_tokens / usage.context_window_tokens >= policy.context_trigger_ratio:
        pressure_reasons.append("request_context_pressure")
    if churn / usage.context_window_tokens >= policy.cumulative_churn_trigger_windows:
        pressure_reasons.append("cumulative_input_cache_pressure")
    if (
        usage.cost_budget_usd is not None
        and usage.cumulative_cost_usd / usage.cost_budget_usd >= policy.cost_trigger_ratio
    ):
        pressure_reasons.append("cumulative_cost_pressure")

    window_tail_start = _exchange_window_tail_start(
        units, policy.recent_exchange_window, exchange_tool_names
    )

    tail = units
    receipt_manifest: ReceiptManifest | None = None
    history_digest: str | None = None
    estimated_tokens = full_tokens
    target = max(1, int(usage.context_window_tokens * policy.context_target_ratio))
    pressure_tail_start: int | None = None
    if pressure_reasons and units:
        if "request_context_pressure" not in pressure_reasons:
            # Churn and spend pressure deliberately reduce the repeated request,
            # even when the current request alone remains below the window.
            target = min(target, max(1, full_tokens // 2))
        pressure_tail_start = _recent_complete_tail_start(
            assignment_prefix,
            strategy_delta,
            evidence_delta,
            units,
            target,
            tokenizer,
        )
    tail_start = max(
        (start for start in (pressure_tail_start, window_tail_start) if start is not None),
        default=None,
    )
    if tail_start is not None and units:
        # Tool evidence may leave the verbatim tail only after a durable,
        # attributable receipt exists. If that makes the request exceed its soft
        # target, preserve evidence and report the larger estimate honestly.
        unreceipted = [
            index
            for index, unit in enumerate(units[:tail_start])
            if any(
                event.role is ProtocolRole.TOOL and event.durable_receipt is None
                for event in unit.events
            )
        ]
        if unreceipted:
            tail_start = min(unreceipted)
        older = units[:tail_start]
        tail = units[tail_start:]
        older_receipts = tuple(
            event.durable_receipt
            for unit in older
            for event in unit.events
            if event.role is ProtocolRole.TOOL and event.durable_receipt is not None
        )
        history_digest = _framed_digest(older) if older else None
        tail_only_tokens = _estimate_tokens(
            _projection_payload(
                assignment_prefix,
                strategy_delta,
                evidence_delta,
                tail,
                None,
                history_digest,
            ),
            tokenizer,
        )
        receipt_manifest = _bounded_receipt_manifest(
            older_receipts,
            min(
                max(0, usage.context_window_tokens - tail_only_tokens),
                policy.receipt_manifest_budget_tokens,
            ),
            tokenizer,
        )
        estimated_tokens = _estimate_tokens(
            _projection_payload(
                assignment_prefix,
                strategy_delta,
                evidence_delta,
                tail,
                receipt_manifest,
                history_digest,
            ),
            tokenizer,
        )

    return ContextProjection(
        assignment_prefix=assignment_prefix,
        assignment_cache_key=assignment_cache_key,
        strategy_delta=strategy_delta,
        evidence_delta=evidence_delta,
        recent_protocol_tail=tail,
        compacted_receipts=receipt_manifest,
        compacted_history_digest=history_digest,
        estimated_input_tokens=estimated_tokens,
        target_input_tokens=target,
        fits_context=estimated_tokens <= usage.context_window_tokens,
        used_provider_tokenizer=tokenizer is not None,
        pressure_reasons=tuple(pressure_reasons),
        progress=progress,
        stall=stall,
    )


def _exchange_window_tail_start(
    units: tuple[ProtocolUnit, ...],
    window: int | None,
    exchange_tool_names: Container[str],
) -> int | None:
    """Bound the verbatim tail to the last ``window`` target-exchange units.

    The boundary sits at the first of the last ``window`` protocol units that
    contain a target-exchange tool result, so intervening observation units
    (snapshots, lookups) ride along with the exchange that needed them.
    ``None`` means no exchange window is configured; a window with at most
    ``window`` exchanges means nothing is trimmed yet.
    """

    if window is None or not units:
        return None
    exchange_indexes = [
        index
        for index, unit in enumerate(units)
        if any(
            event.role is ProtocolRole.TOOL and event.tool_name in exchange_tool_names
            for event in unit.events
        )
    ]
    if len(exchange_indexes) <= window:
        return None
    return exchange_indexes[-window]


def _recent_complete_tail_start(
    prefix: AssignmentPrefix,
    strategy: StrategyDelta,
    evidence: EvidenceDelta,
    units: tuple[ProtocolUnit, ...],
    target_tokens: int,
    tokenizer: TokenEstimator | None,
) -> int:
    """Find a pressure-bounded tail without splitting any protocol unit."""

    base_tokens = _estimate_tokens(
        _projection_payload(prefix, strategy, evidence, (), None, None), tokenizer
    )
    unit_tokens = [_estimate_tokens(_canonical_json(unit), tokenizer) for unit in units]
    # Start with the newest complete unit. Moving the boundary left replaces
    # that older unit's receipt pointers with its full protocol content.
    start = len(units) - 1
    estimated = base_tokens + unit_tokens[start]
    while start > 0:
        candidate = estimated + unit_tokens[start - 1]
        if candidate > target_tokens:
            break
        start -= 1
        estimated = candidate
    # Tool evidence may leave the verbatim tail only after a durable,
    # attributable receipt exists. If that makes the request exceed its soft
    # target, preserve evidence and report the larger estimate honestly.
    unreceipted = [
        index
        for index, unit in enumerate(units[:start])
        if any(
            event.role is ProtocolRole.TOOL and event.durable_receipt is None
            for event in unit.events
        )
    ]
    if unreceipted:
        start = min(unreceipted)
    return start


def _projection_payload(
    prefix: AssignmentPrefix,
    strategy: StrategyDelta,
    evidence: EvidenceDelta,
    tail: Sequence[ProtocolUnit],
    receipts: ReceiptManifest | None,
    history_digest: str | None,
) -> str:
    return _canonical_json(
        {
            "assignment_prefix": prefix,
            "strategy_delta": strategy,
            "evidence_delta": evidence,
            "compacted_receipts": receipts,
            "compacted_history_digest": history_digest,
            "recent_protocol_tail": tail,
        }
    )


def _canonical_json(value: object) -> str:
    def normalize(item: object) -> object:
        if hasattr(item, "__dataclass_fields__"):
            return normalize(asdict(item))  # type: ignore[arg-type]
        if isinstance(item, Mapping):
            return {str(key): normalize(val) for key, val in item.items()}
        if isinstance(item, (tuple, list)):
            return [normalize(member) for member in item]
        if isinstance(item, StrEnum):
            return item.value
        return item

    return json.dumps(normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _estimate_tokens(payload: str, tokenizer: TokenEstimator | None) -> int:
    if tokenizer is not None:
        estimate = tokenizer(payload)
        if isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0:
            raise ValueError("provider tokenizer must return a non-negative integer")
        return estimate
    # One token per UTF-8 byte is deliberately conservative for an unknown model.
    return max(1, len(payload.encode("utf-8")))


def _content_digest(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode()).hexdigest()


def _receipt_pointer(receipt: EvidenceReceipt) -> ReceiptPointer:
    return ReceiptPointer(
        receipt_id=receipt.receipt_id,
        tool_call_id=receipt.tool_call_id,
        tool_name=receipt.tool_name,
        content_digest=receipt.content_digest,
        summary=receipt.summary,
    )


def _framed_digest(values: Sequence[object]) -> str:
    """Digest long durable sequences without materializing one giant payload."""

    digest = hashlib.sha256()
    for value in values:
        encoded = _canonical_json(value).encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _bounded_receipt_manifest(
    receipts: tuple[EvidenceReceipt, ...],
    summary_budget_tokens: int,
    tokenizer: TokenEstimator | None,
) -> ReceiptManifest | None:
    """Bind all old receipts while exposing only a pressure-bounded semantic tail."""

    if not receipts:
        return None
    digest = _framed_digest(receipts)
    empty_manifest = ReceiptManifest(
        receipt_count=len(receipts),
        manifest_digest=digest,
        recent_semantic_receipts=(),
        omitted_semantic_count=len(receipts),
    )
    available = max(
        0,
        summary_budget_tokens - _estimate_tokens(_canonical_json(empty_manifest), tokenizer),
    )
    selected: list[ReceiptPointer] = []
    used = 0
    for receipt in reversed(receipts):
        pointer = _receipt_pointer(receipt)
        cost = _estimate_tokens(_canonical_json(pointer), tokenizer)
        if used + cost > available:
            break
        selected.append(pointer)
        used += cost
    selected.reverse()
    manifest = ReceiptManifest(
        receipt_count=len(receipts),
        manifest_digest=digest,
        recent_semantic_receipts=tuple(selected),
        omitted_semantic_count=len(receipts) - len(selected),
    )
    if _estimate_tokens(_canonical_json(manifest), tokenizer) > summary_budget_tokens:
        return empty_manifest
    return manifest
