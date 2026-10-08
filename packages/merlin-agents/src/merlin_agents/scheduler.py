from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class NodeStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class EdgeKind(StrEnum):
    SEQUENTIAL = "sequential"
    ORDER_ONLY = "order_only"
    FAN_OUT = "fan_out"
    FAN_IN = "fan_in"
    CLEANUP = "cleanup"


class RetryClass(StrEnum):
    SAFE_PROVIDER = "safe_provider"
    FRESH_WORKER = "fresh_worker"
    EFFECT_REPLAY_FORBIDDEN = "effect_replay_forbidden"
    TERMINAL = "terminal"


class RetryFailureKind(StrEnum):
    PROVIDER_TRUNCATED = "provider_truncated"
    PROVIDER_TRANSIENT = "provider_transient"
    ADAPTER_TIMEOUT = "adapter_timeout"
    POINTER_REPAIR = "pointer_repair"
    TRANSPORT_TRANSIENT = "transport_transient"
    TARGET_ADAPTER_UNHEALTHY = "target_adapter_unhealthy"
    CLEANUP_UNRESOLVED = "cleanup_unresolved"
    WORKER_INTERRUPTED = "worker_interrupted"
    MISSING_TARGET_EFFECT = "missing_target_effect"
    RESULT_SCHEMA_INVALID = "result_schema_invalid"
    AUTHORIZATION = "authorization"
    SCOPE = "scope"
    STATE_INTEGRITY = "state_integrity"
    BUDGET = "budget"
    CONFIGURATION = "configuration"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    retryable_failure_kinds: frozenset[RetryFailureKind]
    max_consecutive_stalls: int = 3
    # Per-kind stall overrides: a kind listed here goes terminal after this many
    # consecutive stalled replacements instead of the policy default. Zero means
    # the first occurrence is already terminal (fail-fast worker rules).
    failure_kind_stall_limits: Mapping[RetryFailureKind, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.retryable_failure_kinds:
            raise ValueError("typed retry policy requires at least one retryable failure kind")
        if self.max_consecutive_stalls < 1:
            raise ValueError("retry stall limit must be positive")
        if any(limit < 0 for limit in self.failure_kind_stall_limits.values()):
            raise ValueError("per-kind retry stall limits cannot be negative")

    def stall_limit(self, failure_kind: RetryFailureKind) -> int:
        return self.failure_kind_stall_limits.get(failure_kind, self.max_consecutive_stalls)


TERMINAL = {NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.CANCELED}


@dataclass(frozen=True, slots=True)
class NodeSpec:
    id: str
    max_attempts: int | None = 1
    deadline_tick: int | None = None
    resource_key: str | None = None
    retry_policy: RetryPolicy | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("node id must not be empty")
        if self.max_attempts is not None and self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.retry_policy is not None and self.max_attempts is not None:
            raise ValueError("typed retry policy cannot also use a fixed attempt count")
        if self.deadline_tick is not None and self.deadline_tick < 1:
            raise ValueError("deadline tick must be positive")
        if self.resource_key is not None and not self.resource_key.strip():
            raise ValueError("resource key must not be empty")


@dataclass(frozen=True, slots=True)
class EdgeSpec:
    source: str
    target: str
    kind: EdgeKind = EdgeKind.SEQUENTIAL


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    lease_ttl_ticks: int = 10
    max_lease_reassignments_per_node: int = 1
    max_concurrent_leases: int = 64
    max_concurrent_per_resource: int = 8

    def __post_init__(self) -> None:
        if (
            self.lease_ttl_ticks < 1
            or self.max_lease_reassignments_per_node < 0
            or self.max_concurrent_leases < 1
            or self.max_concurrent_per_resource < 1
        ):
            raise ValueError("invalid scheduler bounds")


@dataclass(frozen=True, slots=True)
class AttemptHandle:
    node_id: str
    owner_id: str
    generation: int
    attempt_id: str


@dataclass(frozen=True, slots=True)
class Lease:
    handle: AttemptHandle
    expires_at_tick: int


@dataclass(frozen=True, slots=True)
class Disposition:
    status: NodeStatus
    result: str | None = None
    reason: str | None = None


@dataclass(slots=True)
class NodeState:
    status: NodeStatus = NodeStatus.PENDING
    attempts_started: int = 0
    lease_reassignments: int = 0
    last_generation: int = 0
    lease: Lease | None = None
    disposition: Disposition | None = None
    ready_since_tick: int | None = None
    consecutive_retry_stalls: int = 0
    last_retry_progress: str | None = None
    last_retry_failure_kind: str | None = None


@dataclass(frozen=True, slots=True)
class QuarantinedAttempt:
    handle: AttemptHandle
    reason: str


@dataclass(frozen=True, slots=True)
class FanInResult:
    manifest: str
    digest: str


class SchedulerError(ValueError):
    pass


class NotReadyError(SchedulerError):
    pass


class LeaseError(SchedulerError):
    pass


class StaleGenerationError(LeaseError):
    pass


class ReferenceScheduler:
    """Deterministic admitted-graph scheduler with generation-fenced leases."""

    def __init__(
        self,
        nodes: list[NodeSpec],
        edges: list[EdgeSpec],
        config: SchedulerConfig | None = None,
    ) -> None:
        if not nodes:
            raise ValueError("scheduler requires at least one node")
        self.specs = {node.id: node for node in nodes}
        if len(self.specs) != len(nodes):
            raise ValueError("duplicate node id")
        self.edges = tuple(edges)
        for edge in edges:
            if edge.source not in self.specs or edge.target not in self.specs:
                raise ValueError("edge names unknown node")
            if edge.source == edge.target:
                raise ValueError("self edge is not allowed")
        self.config = config or SchedulerConfig()
        self.states = {node.id: NodeState() for node in nodes}
        self.quarantine: list[QuarantinedAttempt] = []
        self._last_tick = 0
        self._refresh_ready()

    def checkpoint(self) -> dict[str, object]:
        """Return a JSON-ready checkpoint suitable for a structural durable store."""
        return {
            "last_tick": self._last_tick,
            "nodes": {
                node_id: {
                    "status": state.status.value,
                    "attempts_started": state.attempts_started,
                    "lease_reassignments": state.lease_reassignments,
                    "last_generation": state.last_generation,
                    "ready_since_tick": state.ready_since_tick,
                    "consecutive_retry_stalls": state.consecutive_retry_stalls,
                    "last_retry_progress": state.last_retry_progress,
                    "last_retry_failure_kind": state.last_retry_failure_kind,
                    "lease": None
                    if state.lease is None
                    else {
                        "owner_id": state.lease.handle.owner_id,
                        "generation": state.lease.handle.generation,
                        "attempt_id": state.lease.handle.attempt_id,
                        "expires_at_tick": state.lease.expires_at_tick,
                    },
                    "disposition": None
                    if state.disposition is None
                    else {
                        "status": state.disposition.status.value,
                        "result": state.disposition.result,
                        "reason": state.disposition.reason,
                    },
                }
                for node_id, state in sorted(self.states.items())
            },
        }

    @classmethod
    def restore(
        cls,
        nodes: list[NodeSpec],
        edges: list[EdgeSpec],
        checkpoint: dict[str, object],
        config: SchedulerConfig | None = None,
    ) -> ReferenceScheduler:
        scheduler = cls(nodes, edges, config)
        raw_nodes = checkpoint.get("nodes")
        if not isinstance(raw_nodes, dict) or set(raw_nodes) != set(scheduler.states):
            raise SchedulerError("checkpoint node inventory diverged")
        for node_id, raw in raw_nodes.items():
            if not isinstance(node_id, str) or not isinstance(raw, dict):
                raise SchedulerError("checkpoint node state is invalid")
            state = scheduler.states[node_id]
            state.status = NodeStatus(str(raw["status"]))
            state.attempts_started = int(raw["attempts_started"])
            state.lease_reassignments = int(raw["lease_reassignments"])
            state.last_generation = int(raw["last_generation"])
            ready_since_tick = raw.get("ready_since_tick")
            state.ready_since_tick = int(ready_since_tick) if ready_since_tick is not None else None
            state.consecutive_retry_stalls = int(raw.get("consecutive_retry_stalls", 0))
            retry_progress = raw.get("last_retry_progress")
            state.last_retry_progress = str(retry_progress) if retry_progress is not None else None
            retry_kind = raw.get("last_retry_failure_kind")
            if retry_kind is None:
                state.last_retry_failure_kind = None
            else:
                try:
                    state.last_retry_failure_kind = RetryFailureKind(str(retry_kind)).value
                except ValueError as error:
                    raise SchedulerError("checkpoint retry failure kind is invalid") from error
            lease = raw.get("lease")
            if lease is not None:
                if not isinstance(lease, dict):
                    raise SchedulerError("checkpoint lease is invalid")
                handle = AttemptHandle(
                    node_id,
                    str(lease["owner_id"]),
                    int(lease["generation"]),
                    str(lease["attempt_id"]),
                )
                state.lease = Lease(handle, int(lease["expires_at_tick"]))
            disposition = raw.get("disposition")
            if disposition is not None:
                if not isinstance(disposition, dict):
                    raise SchedulerError("checkpoint disposition is invalid")
                state.disposition = Disposition(
                    NodeStatus(str(disposition["status"])),
                    disposition.get("result"),
                    disposition.get("reason"),
                )
        scheduler._last_tick = int(checkpoint.get("last_tick", 0))
        scheduler._refresh_ready()
        return scheduler

    def status(self, node_id: str) -> NodeStatus:
        return self._state(node_id).status

    def ready_nodes(self) -> tuple[str, ...]:
        return tuple(
            node_id
            for node_id, _state in sorted(
                (
                    (node_id, state)
                    for node_id, state in self.states.items()
                    if state.status == NodeStatus.READY
                ),
                key=lambda item: (
                    item[1].ready_since_tick
                    if item[1].ready_since_tick is not None
                    else self._last_tick,
                    item[0],
                ),
            )
        )

    def claim_next(self, owner_id: str, tick: int) -> AttemptHandle | None:
        """Claim the oldest admissible ready node without starving earlier work."""
        self._clock(tick)
        self.expire_deadlines(tick)
        for node_id in self.ready_nodes():
            if self._resource_available(node_id):
                return self.claim(node_id, owner_id, tick)
        return None

    def claim(self, node_id: str, owner_id: str, tick: int) -> AttemptHandle:
        self._clock(tick)
        self.expire_deadlines(tick)
        state = self._state(node_id)
        if state.status != NodeStatus.READY:
            raise NotReadyError(f"node {node_id!r} is not ready")
        if not owner_id:
            raise ValueError("owner_id must not be empty")
        max_attempts = self.specs[node_id].max_attempts
        if max_attempts is not None and state.attempts_started >= max_attempts:
            raise NotReadyError(f"node {node_id!r} exhausted admitted attempts")
        if not self._resource_available(node_id):
            raise NotReadyError(f"node {node_id!r} is rate limited")
        state.last_generation += 1
        state.attempts_started += 1
        handle = self._handle(node_id, owner_id, state.last_generation)
        state.lease = Lease(handle, tick + self.config.lease_ttl_ticks)
        state.status = NodeStatus.LEASED
        state.ready_since_tick = None
        return handle

    def abandon(self, handle: AttemptHandle) -> None:
        """Release a claim whose durable lease was refused, restoring readiness.

        The store rejected the matching durable claim, so no attempt row
        exists: the node returns to READY and the attempt budget is restored,
        letting the executor re-claim once the blocking foreign lease expires.
        """
        state = self._state(handle.node_id)
        if state.lease is None or state.lease.handle != handle:
            return
        state.lease = None
        state.status = NodeStatus.READY
        state.attempts_started -= 1
        state.ready_since_tick = self._last_tick

    def admit_retry(
        self,
        handle: AttemptHandle,
        retry_class: RetryClass,
        reason: str,
        tick: int,
        *,
        failure_kind: RetryFailureKind = RetryFailureKind.UNKNOWN,
        progress_marker: str | None = None,
        budget_exhausted: bool = False,
    ) -> AttemptHandle | None:
        """Apply retry semantics without conflating provider retries and worker replacement."""
        state = self._live(handle, tick)
        if retry_class is RetryClass.SAFE_PROVIDER:
            return handle
        state.lease = None
        if retry_class in {RetryClass.EFFECT_REPLAY_FORBIDDEN, RetryClass.TERMINAL}:
            quarantine_reason = (
                "effect-replay-forbidden"
                if retry_class is RetryClass.EFFECT_REPLAY_FORBIDDEN
                else "terminal-failure"
            )
            self.quarantine.append(QuarantinedAttempt(handle, quarantine_reason))
            state.status = NodeStatus.FAILED
            state.disposition = Disposition(NodeStatus.FAILED, reason=reason)
            self._refresh_ready()
            return None
        self.quarantine.append(QuarantinedAttempt(handle, "worker-replaced"))
        spec = self.specs[handle.node_id]
        policy = spec.retry_policy
        if policy is not None:
            if budget_exhausted or failure_kind not in policy.retryable_failure_kinds:
                state.status = NodeStatus.FAILED
                state.disposition = Disposition(NodeStatus.FAILED, reason=reason)
                self._refresh_ready()
                return None
            stall_limit = policy.stall_limit(failure_kind)
            made_progress = (
                # Fail-fast kinds (limit zero) are terminal on first occurrence;
                # a progress marker cannot buy them a replacement worker.
                stall_limit > 0
                and progress_marker is not None
                and progress_marker != state.last_retry_progress
            )
            state.consecutive_retry_stalls = (
                0 if made_progress else state.consecutive_retry_stalls + 1
            )
            state.last_retry_progress = progress_marker
            state.last_retry_failure_kind = failure_kind.value
            if state.consecutive_retry_stalls > stall_limit:
                state.status = NodeStatus.FAILED
                state.disposition = Disposition(
                    NodeStatus.FAILED,
                    reason=(f"retry stalled after {stall_limit} replacements: {reason}"),
                )
                self._refresh_ready()
                return None
            state.status = NodeStatus.READY
            state.ready_since_tick = tick
            return None
        if spec.max_attempts is not None and state.attempts_started >= spec.max_attempts:
            state.status = NodeStatus.FAILED
            state.disposition = Disposition(NodeStatus.FAILED, reason=reason)
            self._refresh_ready()
            return None
        state.status = NodeStatus.READY
        state.ready_since_tick = tick
        return None

    def expire_deadlines(self, tick: int) -> tuple[str, ...]:
        """Terminally stop undispatched work and fence live work at its admitted deadline."""
        self._clock(tick)
        expired: list[str] = []
        for node_id, state in sorted(self.states.items()):
            deadline = self.specs[node_id].deadline_tick
            if deadline is None or tick < deadline or state.status in TERMINAL:
                continue
            if state.lease is not None:
                self.quarantine.append(QuarantinedAttempt(state.lease.handle, "deadline-exceeded"))
            state.lease = None
            state.status = NodeStatus.FAILED
            state.ready_since_tick = None
            state.disposition = Disposition(NodeStatus.FAILED, reason="deadline-exceeded")
            expired.append(node_id)
        if expired:
            self._refresh_ready()
        return tuple(expired)

    def heartbeat(self, handle: AttemptHandle, tick: int) -> None:
        self._clock(tick)
        state = self._live(handle, tick)
        state.lease = Lease(handle, tick + self.config.lease_ttl_ticks)

    def confirm_external_lease(self, handle: AttemptHandle, tick: int) -> None:
        """Synchronize a lease already confirmed by an external authority.

        Durable execution uses a wall-clock database lease while this reference
        scheduler advances one logical tick per graph mutation.  Unrelated
        sibling mutations must not invalidate a database-confirmed owner.  This
        method still fences a replaced generation and enforces node deadlines;
        it only bypasses the local tick-based expiry check.
        """

        self._clock(tick)
        state = self._state(handle.node_id)
        lease = state.lease
        if lease is None or lease.handle != handle:
            self.quarantine.append(QuarantinedAttempt(handle, "stale-generation"))
            raise StaleGenerationError("attempt does not own the current lease generation")
        deadline = self.specs[handle.node_id].deadline_tick
        if deadline is not None and tick >= deadline:
            self.quarantine.append(QuarantinedAttempt(handle, "deadline-exceeded"))
            state.lease = None
            state.status = NodeStatus.FAILED
            state.disposition = Disposition(NodeStatus.FAILED, reason="deadline-exceeded")
            self._refresh_ready()
            raise LeaseError("node deadline exceeded")
        state.lease = Lease(handle, tick + self.config.lease_ttl_ticks)

    def reassign_expired(self, node_id: str, owner_id: str, tick: int) -> AttemptHandle | None:
        self._clock(tick)
        self.expire_deadlines(tick)
        state = self._state(node_id)
        if state.status != NodeStatus.LEASED or state.lease is None:
            raise LeaseError(f"node {node_id!r} has no lease")
        if state.lease.expires_at_tick > tick:
            raise LeaseError("lease is still live")
        if state.lease_reassignments >= self.config.max_lease_reassignments_per_node:
            state.disposition = Disposition(NodeStatus.FAILED, reason="lease-reassignment-cap")
            state.status = NodeStatus.FAILED
            state.lease = None
            self._refresh_ready()
            return None
        previous = state.lease.handle
        self.quarantine.append(QuarantinedAttempt(previous, "lease-expired"))
        state.lease_reassignments += 1
        state.last_generation += 1
        handle = self._handle(node_id, owner_id, state.last_generation)
        state.lease = Lease(handle, tick + self.config.lease_ttl_ticks)
        return handle

    def expire(self, node_id: str, tick: int) -> None:
        """Release one expired attempt so the next claim becomes its fenced replacement."""
        self._clock(tick)
        state = self._state(node_id)
        if state.status is not NodeStatus.LEASED or state.lease is None:
            raise LeaseError(f"node {node_id!r} has no lease")
        if state.lease.expires_at_tick > tick:
            raise LeaseError("lease is still live")
        self.quarantine.append(QuarantinedAttempt(state.lease.handle, "lease-expired"))
        state.lease = None
        state.status = NodeStatus.READY
        state.ready_since_tick = tick

    def complete(self, handle: AttemptHandle, result: str, tick: int) -> None:
        self._clock(tick)
        state = self._live(handle, tick)
        state.lease = None
        state.status = NodeStatus.COMPLETED
        state.disposition = Disposition(NodeStatus.COMPLETED, result=result)
        self._refresh_ready()

    def fail(self, handle: AttemptHandle, reason: str, tick: int) -> None:
        self._clock(tick)
        state = self._live(handle, tick)
        state.lease = None
        max_attempts = self.specs[handle.node_id].max_attempts
        if max_attempts is not None and state.attempts_started < max_attempts:
            state.status = NodeStatus.READY
            state.ready_since_tick = tick
        else:
            state.status = NodeStatus.FAILED
            state.disposition = Disposition(NodeStatus.FAILED, reason=reason)
            self._refresh_ready()

    def cancel(self, node_id: str, reason: str, tick: int) -> None:
        self._clock(tick)
        state = self._state(node_id)
        if state.status in TERMINAL:
            return
        if state.lease is not None:
            self.quarantine.append(QuarantinedAttempt(state.lease.handle, "canceled"))
        state.lease = None
        state.status = NodeStatus.CANCELED
        state.ready_since_tick = None
        state.disposition = Disposition(NodeStatus.CANCELED, reason=reason)
        self._refresh_ready()

    def cancel_schedule(self, reason: str, tick: int) -> None:
        self._clock(tick)
        for node_id in sorted(self.states):
            state = self.states[node_id]
            if state.status not in TERMINAL:
                if state.lease is not None:
                    self.quarantine.append(QuarantinedAttempt(state.lease.handle, "canceled"))
                state.lease = None
                state.status = NodeStatus.CANCELED
                state.ready_since_tick = None
                state.disposition = Disposition(NodeStatus.CANCELED, reason=reason)

    def fan_in(self, node_id: str) -> FanInResult:
        state = self._state(node_id)
        incoming = sorted(
            (
                edge
                for edge in self.edges
                if edge.target == node_id and edge.kind == EdgeKind.FAN_IN
            ),
            key=lambda edge: edge.source,
        )
        if not incoming or state.status != NodeStatus.READY:
            raise NotReadyError(f"node {node_id!r} is not a ready fan-in reducer")
        branches = []
        for edge in incoming:
            disposition = self.states[edge.source].disposition
            assert disposition is not None
            branches.append(
                {
                    "node_id": edge.source,
                    "status": disposition.status.value,
                    "result": disposition.result,
                    "reason": disposition.reason,
                }
            )
        manifest = json.dumps({"branches": branches}, sort_keys=True, separators=(",", ":"))
        return FanInResult(manifest, f"sha256:{hashlib.sha256(manifest.encode()).hexdigest()}")

    def _live(self, handle: AttemptHandle, tick: int) -> NodeState:
        state = self._state(handle.node_id)
        lease = state.lease
        if lease is None or lease.handle != handle:
            self.quarantine.append(QuarantinedAttempt(handle, "stale-generation"))
            raise StaleGenerationError("attempt does not own the current lease generation")
        deadline = self.specs[handle.node_id].deadline_tick
        if deadline is not None and tick >= deadline:
            self.quarantine.append(QuarantinedAttempt(handle, "deadline-exceeded"))
            state.lease = None
            state.status = NodeStatus.FAILED
            state.disposition = Disposition(NodeStatus.FAILED, reason="deadline-exceeded")
            self._refresh_ready()
            raise LeaseError("node deadline exceeded")
        if lease.expires_at_tick <= tick:
            self.quarantine.append(QuarantinedAttempt(handle, "lease-expired"))
            raise LeaseError("lease expired")
        return state

    def _refresh_ready(self) -> None:
        changed = True
        while changed:
            changed = False
            for node_id, state in self.states.items():
                if state.status != NodeStatus.PENDING:
                    continue
                incoming = [edge for edge in self.edges if edge.target == node_id]
                if not incoming:
                    state.status = NodeStatus.READY
                    state.ready_since_tick = self._last_tick
                    changed = True
                    continue
                if any(edge.kind == EdgeKind.FAN_IN for edge in incoming):
                    ready = all(self.states[edge.source].status in TERMINAL for edge in incoming)
                else:
                    failed_prerequisite = next(
                        (
                            edge
                            for edge in incoming
                            if edge.kind not in {EdgeKind.CLEANUP, EdgeKind.ORDER_ONLY}
                            and self.states[edge.source].status
                            in {NodeStatus.FAILED, NodeStatus.CANCELED}
                        ),
                        None,
                    )
                    if failed_prerequisite is not None:
                        source = self.states[failed_prerequisite.source]
                        state.status = NodeStatus.FAILED
                        state.ready_since_tick = None
                        state.disposition = Disposition(
                            NodeStatus.FAILED,
                            reason=(
                                f"prerequisite {failed_prerequisite.source!r} {source.status.value}"
                            ),
                        )
                        changed = True
                        continue
                    ready = all(
                        self.states[edge.source].status in TERMINAL
                        if edge.kind in {EdgeKind.CLEANUP, EdgeKind.ORDER_ONLY}
                        else self.states[edge.source].status == NodeStatus.COMPLETED
                        for edge in incoming
                    )
                if ready:
                    state.status = NodeStatus.READY
                    state.ready_since_tick = self._last_tick
                    changed = True

    def _resource_available(self, node_id: str) -> bool:
        leased = [state for state in self.states.values() if state.status == NodeStatus.LEASED]
        if len(leased) >= self.config.max_concurrent_leases:
            return False
        resource = self.specs[node_id].resource_key
        if resource is None:
            return True
        active = sum(
            state.status == NodeStatus.LEASED and self.specs[candidate].resource_key == resource
            for candidate, state in self.states.items()
        )
        return active < self.config.max_concurrent_per_resource

    def _state(self, node_id: str) -> NodeState:
        try:
            return self.states[node_id]
        except KeyError as error:
            raise SchedulerError(f"unknown node {node_id!r}") from error

    def _clock(self, tick: int) -> None:
        if tick < self._last_tick:
            raise SchedulerError("authority clock moved backwards")
        self._last_tick = tick

    @staticmethod
    def _handle(node_id: str, owner_id: str, generation: int) -> AttemptHandle:
        identity = f"{node_id}\0{generation}".encode()
        attempt_id = f"attempt/{hashlib.sha256(identity).hexdigest()}"
        return AttemptHandle(node_id, owner_id, generation, attempt_id)
