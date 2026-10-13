from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from merlin_contracts.state_scope import STATE_SCOPE_MIGRATION, STATE_SCOPE_SCHEMA_VERSION

LIFECYCLE_RECOVERY_SCHEMA = "merlin.durable-lifecycle-recovery.v1"

LifecycleDecision = Literal[
    "resume",
    "pause_fixed_forward",
    "wait_quota",
    "settle_acknowledged",
    "cleanup_required",
]

SchedulerRecoveryAction = Literal[
    "await_approval",
    "bind_successor_schedule",
    "claim_successor",
    "finalize_and_settle_terminal_batch",
    "finalize_terminal_wave",
    "idle",
    "pause_inconsistent",
    "paused",
    "persist_successor_schedule",
    "persist_wave_binding",
    "resume_active",
    "settle_terminal_batch",
]


class DurableLifecycleStore(Protocol):
    def reconcile_durable_lifecycle(
        self,
        *,
        claimant_id: str,
        readable_formats: Sequence[str],
        writable_format: str,
        lease_seconds: int,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class CampaignLifecycleDecision:
    campaign_id: str
    execution_format: str
    status: str
    decision: LifecycleDecision
    wake_at: datetime | None
    open_state_scopes: int
    acknowledged_state_scopes: int
    cleanup_obligations: int
    quota_indefinite: bool
    recovery_action: str
    scheduler_action: SchedulerRecoveryAction
    recovery_state_digest: str
    recovery_decision_digest: str
    target_call_count: int

    @property
    def resumable(self) -> bool:
        return self.decision == "resume"


@dataclass(frozen=True, slots=True)
class LifecycleRecoveryReport:
    schema: str
    reconciled_at: datetime
    campaigns: tuple[CampaignLifecycleDecision, ...]
    expired_global_leases: int
    due_quota_wakes: int

    @property
    def resumable_campaign_ids(self) -> tuple[str, ...]:
        return tuple(row.campaign_id for row in self.campaigns if row.resumable)

    @property
    def fixed_forward_campaign_ids(self) -> tuple[str, ...]:
        return tuple(
            row.campaign_id for row in self.campaigns if row.decision == "pause_fixed_forward"
        )


class DurableLifecycleRecovery:
    """Reconcile global target state before campaign or scheduler recovery.

    The PostgreSQL operation is one transaction. This facade validates its
    durable result before callers resume any target-delivery scheduler.
    """

    def __init__(
        self,
        store: DurableLifecycleStore,
        *,
        claimant_id: str,
        readable_formats: Sequence[str] = STATE_SCOPE_MIGRATION.readable_versions,
        writable_format: str = STATE_SCOPE_SCHEMA_VERSION,
        lease_seconds: int = 30,
    ) -> None:
        claimant = claimant_id.strip()
        if not claimant:
            raise ValueError("durable lifecycle recovery requires a claimant")
        normalized_formats = tuple(dict.fromkeys(item.strip() for item in readable_formats))
        if not normalized_formats or any(not item for item in normalized_formats):
            raise ValueError("lifecycle recovery requires readable execution formats")
        writable = writable_format.strip()
        if not writable or writable not in normalized_formats:
            raise ValueError("writable format must also be readable")
        if lease_seconds < 1:
            raise ValueError("lifecycle recovery lease must be positive")
        self._store = store
        self._claimant_id = claimant
        self._readable_formats = normalized_formats
        self._writable_format = writable
        self._lease_seconds = lease_seconds

    async def reconcile(self) -> LifecycleRecoveryReport:
        value = await asyncio.to_thread(
            self._store.reconcile_durable_lifecycle,
            claimant_id=self._claimant_id,
            readable_formats=self._readable_formats,
            writable_format=self._writable_format,
            lease_seconds=self._lease_seconds,
        )
        return lifecycle_recovery_report(value)


def lifecycle_recovery_report(value: Mapping[str, object]) -> LifecycleRecoveryReport:
    if value.get("schema") != LIFECYCLE_RECOVERY_SCHEMA:
        raise ValueError("durable lifecycle recovery schema is unsupported")
    reconciled_at = _datetime(value.get("reconciled_at"), "reconciled_at")
    raw_campaigns = value.get("campaigns")
    if not isinstance(raw_campaigns, Sequence) or isinstance(
        raw_campaigns, (str, bytes, bytearray)
    ):
        raise ValueError("durable lifecycle recovery campaigns must be a sequence")
    campaigns = tuple(_campaign_decision(row) for row in raw_campaigns)
    identifiers = tuple(row.campaign_id for row in campaigns)
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("durable lifecycle recovery repeated a campaign")
    return LifecycleRecoveryReport(
        schema=LIFECYCLE_RECOVERY_SCHEMA,
        reconciled_at=reconciled_at,
        campaigns=campaigns,
        expired_global_leases=_count(value.get("expired_global_leases"), "expired_global_leases"),
        due_quota_wakes=_count(value.get("due_quota_wakes"), "due_quota_wakes"),
    )


def _campaign_decision(value: object) -> CampaignLifecycleDecision:
    if not isinstance(value, Mapping):
        raise ValueError("durable lifecycle campaign decision must be an object")
    campaign_id = _nonempty(value.get("campaign_id"), "campaign_id")
    status = _nonempty(value.get("status"), "status")
    decision = _nonempty(value.get("decision"), "decision")
    if decision not in {
        "resume",
        "pause_fixed_forward",
        "wait_quota",
        "settle_acknowledged",
        "cleanup_required",
    }:
        raise ValueError("durable lifecycle campaign decision is invalid")
    execution_format = _nonempty(value.get("execution_format"), "execution_format")
    scheduler_action = _nonempty(value.get("scheduler_action"), "scheduler_action")
    if scheduler_action not in {
        "await_approval",
        "bind_successor_schedule",
        "claim_successor",
        "finalize_and_settle_terminal_batch",
        "finalize_terminal_wave",
        "idle",
        "pause_inconsistent",
        "paused",
        "persist_successor_schedule",
        "persist_wave_binding",
        "resume_active",
        "settle_terminal_batch",
    }:
        raise ValueError("durable scheduler recovery action is invalid")
    recovery_action = _nonempty(value.get("recovery_action"), "recovery_action")
    if recovery_action not in {scheduler_action, "pause_lifecycle"}:
        raise ValueError("durable campaign recovery action conflicts with scheduler state")
    if (decision == "resume") != (recovery_action == scheduler_action):
        raise ValueError("durable campaign recovery action conflicts with lifecycle decision")
    wake_value = value.get("wake_at")
    wake_at = None if wake_value is None else _datetime(wake_value, "wake_at")
    quota_indefinite = value.get("quota_indefinite", False)
    if not isinstance(quota_indefinite, bool):
        raise ValueError("durable lifecycle quota_indefinite must be a boolean")
    if decision == "wait_quota" and wake_at is None and not quota_indefinite:
        raise ValueError("quota-waiting campaign requires a wake_at or indefinite marker")
    if decision != "wait_quota" and wake_at is not None:
        raise ValueError("only a quota-waiting campaign may carry wake_at")
    if decision != "wait_quota" and quota_indefinite:
        raise ValueError("only a quota-waiting campaign may be indefinite")
    return CampaignLifecycleDecision(
        campaign_id=campaign_id,
        execution_format=execution_format,
        status=status,
        decision=cast(LifecycleDecision, decision),
        wake_at=wake_at,
        open_state_scopes=_count(value.get("open_state_scopes"), "open_state_scopes"),
        acknowledged_state_scopes=_count(
            value.get("acknowledged_state_scopes"), "acknowledged_state_scopes"
        ),
        cleanup_obligations=_count(value.get("cleanup_obligations"), "cleanup_obligations"),
        quota_indefinite=quota_indefinite,
        recovery_action=recovery_action,
        scheduler_action=cast(SchedulerRecoveryAction, scheduler_action),
        recovery_state_digest=_qualified_digest(
            value.get("recovery_state_digest"), "recovery_state_digest"
        ),
        recovery_decision_digest=_qualified_digest(
            value.get("recovery_decision_digest"), "recovery_decision_digest"
        ),
        target_call_count=_count(value.get("target_call_count"), "target_call_count"),
    )


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"durable lifecycle {name} must be a non-empty string")
    return value.strip()


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"durable lifecycle {name} must be a non-negative integer")
    return value


def _qualified_digest(value: object, name: str) -> str:
    digest = _nonempty(value, name)
    if len(digest) != 71 or not digest.startswith("sha256:"):
        raise ValueError(f"durable lifecycle {name} must be a qualified SHA-256 digest")
    try:
        int(digest[7:], 16)
    except ValueError as error:
        raise ValueError(f"durable lifecycle {name} must be a qualified SHA-256 digest") from error
    return digest


def _datetime(value: object, name: str) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"durable lifecycle {name} is not an ISO timestamp") from error
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError(f"durable lifecycle {name} must be a timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"durable lifecycle {name} must be timezone-aware")
    return parsed.astimezone(UTC)
