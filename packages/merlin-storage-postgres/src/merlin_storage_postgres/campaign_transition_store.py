from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal

from psycopg.types.json import Jsonb

from .scheduler_store import DurableSchedulerStore

CampaignTransitionKind = Literal["campaign_paused", "campaign_resumed", "campaign_terminal"]

_PAUSE_CAUSES = frozenset(
    {
        "operator",
        "daemon_shutdown",
        "provider_billing",
        "circuit_breaker",
        "recovery",
        "storage_exhausted",
        "unattributed",
    }
)


@dataclass(frozen=True, slots=True)
class CampaignTransition:
    campaign_id: str
    sequence: int
    type: CampaignTransitionKind
    cause: str
    command_id: str
    schedule_ids: tuple[str, ...]
    digest: str


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


class CampaignTransitionRepository:
    """Atomic campaign/wave/scheduler pause and resume transitions.

    It shares the scheduler connection and its re-entrant operation lock so the existing
    scheduler transition remains the authority for attempt fencing, mailbox
    claims, and scheduler events inside one outer transaction.
    """

    _zero_digest = "0" * 64

    def __init__(
        self,
        connection: Any,
        scheduler: DurableSchedulerStore,
        *,
        after_scheduler_transition: Callable[[CampaignTransitionKind], None] | None = None,
    ) -> None:
        if scheduler.connection is not connection:
            raise ValueError("campaign transition repository must share the scheduler connection")
        connection_lock = getattr(connection, "_merlin_operation_lock", None)
        if connection_lock is None or connection_lock is not scheduler._lock:
            raise ValueError(
                "campaign transition repository must share the scheduler operation lock"
            )
        self.connection = connection
        self.scheduler = scheduler
        self._lock = connection_lock
        self._after_scheduler_transition = after_scheduler_transition

    def pause(
        self,
        campaign_id: str,
        *,
        cause: str,
        command_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> CampaignTransition:
        return self._transition(
            campaign_id,
            transition_type="campaign_paused",
            expected_campaign_status="executing",
            expected_wave_status="running",
            expected_schedule_status="running",
            resulting_campaign_status="paused",
            resulting_wave_status="paused",
            cause=cause,
            command_id=command_id,
            payload=payload,
        )

    def resume(
        self,
        campaign_id: str,
        *,
        cause: str,
        command_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> CampaignTransition:
        return self._transition(
            campaign_id,
            transition_type="campaign_resumed",
            expected_campaign_status="paused",
            expected_wave_status="paused",
            expected_schedule_status="paused",
            resulting_campaign_status="executing",
            resulting_wave_status="running",
            cause=cause,
            command_id=command_id,
            payload=payload,
        )

    def _transition(
        self,
        campaign_id: str,
        *,
        transition_type: CampaignTransitionKind,
        expected_campaign_status: str,
        expected_wave_status: str,
        expected_schedule_status: str,
        resulting_campaign_status: str,
        resulting_wave_status: str,
        cause: str,
        command_id: str,
        payload: Mapping[str, object] | None,
    ) -> CampaignTransition:
        normalized_cause = cause.strip().casefold()
        if normalized_cause not in _PAUSE_CAUSES:
            raise ValueError(f"unsupported campaign transition cause: {cause}")
        normalized_command_id = command_id.strip()
        if not normalized_command_id:
            raise ValueError("campaign transition command id must be non-blank")
        request_payload = dict(payload or {})

        with self._lock, self.connection.transaction():
            campaign = self.connection.execute(
                """SELECT status, lifecycle_pause_reason, event_count, last_event_digest
                   FROM merlin_storage.assessment_campaigns
                   WHERE id = %s FOR UPDATE""",
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                raise KeyError(f"unknown campaign: {campaign_id}")

            existing = self.connection.execute(
                """SELECT sequence, type, cause, command_id, payload, digest
                   FROM merlin_storage.campaign_events
                   WHERE campaign_id = %s AND command_id = %s""",
                (campaign_id, normalized_command_id),
            ).fetchone()
            if existing is not None:
                return self._existing_transition(
                    campaign_id,
                    transition_type,
                    normalized_cause,
                    normalized_command_id,
                    request_payload,
                    existing,
                )

            if str(campaign["status"]) != expected_campaign_status:
                raise ValueError(
                    f"campaign is not {expected_campaign_status}; current status is "
                    f"{campaign['status']}"
                )
            if transition_type == "campaign_resumed" and campaign["lifecycle_pause_reason"]:
                raise ValueError("campaign is lifecycle-paused and requires durable reconciliation")

            waves = self.connection.execute(
                """SELECT wave, schedule_id FROM merlin_storage.campaign_waves
                   WHERE campaign_id = %s AND status = %s
                   ORDER BY wave FOR UPDATE""",
                (campaign_id, expected_wave_status),
            ).fetchall()
            schedule_ids = tuple(str(wave["schedule_id"]) for wave in waves)
            schedule_rows = self.connection.execute(
                """SELECT id, status FROM merlin_storage.scheduler_schedules
                   WHERE id = ANY(%s::uuid[]) ORDER BY id FOR UPDATE""",
                (list(schedule_ids),),
            ).fetchall()
            statuses = {str(row["id"]): str(row["status"]) for row in schedule_rows}
            if set(statuses) != set(schedule_ids) or any(
                status not in {expected_schedule_status, "completed", "failed", "cancelled"}
                for status in statuses.values()
            ):
                raise ValueError(
                    f"campaign schedules are not uniformly {expected_schedule_status}: {statuses}"
                )

            for schedule_id in schedule_ids:
                if statuses[schedule_id] != expected_schedule_status:
                    continue
                if transition_type == "campaign_paused":
                    self.scheduler.pause_schedule(schedule_id, reason=normalized_cause)
                else:
                    self.scheduler.resume_schedule(schedule_id)
            if self._after_scheduler_transition is not None:
                self._after_scheduler_transition(transition_type)

            self.connection.execute(
                """UPDATE merlin_storage.campaign_waves
                   SET status = %s, updated_at = now()
                   WHERE campaign_id = %s AND status = %s""",
                (resulting_wave_status, campaign_id, expected_wave_status),
            )
            pause_cause = normalized_cause if transition_type == "campaign_paused" else None
            updated = self.connection.execute(
                """UPDATE merlin_storage.assessment_campaigns
                   SET status = %s, pause_cause = %s, updated_at = now()
                   WHERE id = %s AND status = %s RETURNING event_count, last_event_digest""",
                (
                    resulting_campaign_status,
                    pause_cause,
                    campaign_id,
                    expected_campaign_status,
                ),
            ).fetchone()
            if updated is None:
                raise RuntimeError("campaign transition lost its locked source state")
            return self._append_event(
                campaign_id,
                transition_type,
                normalized_cause,
                normalized_command_id,
                request_payload,
                schedule_ids,
                int(updated["event_count"]),
                str(updated["last_event_digest"]),
            )

    def terminal(
        self, campaign_id: str, *, status: str, reason: str, command_id: str
    ) -> CampaignTransition:
        """Fence active schedulers and commit the terminal envelope atomically."""
        from merlin_contracts.campaign_lifecycle import require_campaign_terminal_reason

        from .campaign_store import CampaignStore

        reason = require_campaign_terminal_reason(reason).value
        if status not in {"completed", "failed", "cancelled"} or not command_id.strip():
            raise ValueError("invalid terminal campaign transition")
        request = {"status": status, "reason": reason}
        with self._lock, self.connection.transaction():
            campaign = self.connection.execute(
                "SELECT * FROM merlin_storage.assessment_campaigns WHERE id = %s FOR UPDATE",
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                raise KeyError(campaign_id)
            existing = self.connection.execute(
                """SELECT * FROM merlin_storage.campaign_events
                   WHERE campaign_id = %s AND command_id = %s""",
                (campaign_id, command_id),
            ).fetchone()
            if existing is not None:
                return self._existing_transition(
                    campaign_id, "campaign_terminal", "operator", command_id, request, existing
                )
            if campaign["status"] not in {"prepared", "executing", "paused"}:
                raise ValueError("campaign is already terminal")
            waves = self.connection.execute(
                """SELECT wave, schedule_id, status FROM merlin_storage.campaign_waves
                   WHERE campaign_id = %s
                   ORDER BY wave FOR UPDATE""",
                (campaign_id,),
            ).fetchall()
            for wave in waves:
                schedule = self.scheduler.load_schedule(str(wave["schedule_id"]))
                if schedule is None:
                    raise ValueError("campaign wave has no scheduler")
                if schedule["status"] in {"running", "paused"}:
                    if status != "cancelled":
                        raise ValueError("terminal campaign still has an active schedule")
                    document = schedule["graph"]
                    spec = document.get("admitted", {}).get("spec", {})
                    cleanup = [
                        str(node["id"])
                        for node in spec.get("nodes", [])
                        if node.get("kind") == "cleanup"
                    ]
                    self.scheduler.cancel_schedule(
                        str(wave["schedule_id"]),
                        reason=reason,
                        cleanup_node_ids=cleanup,
                        checkpoint=schedule["checkpoint"],
                    )
                if wave["status"] in {"running", "paused"} or schedule["status"] in {
                    "running", "paused"
                }:
                    self.connection.execute(
                        """UPDATE merlin_storage.campaign_waves SET status = %s, updated_at = now()
                           WHERE campaign_id = %s AND wave = %s""",
                        (
                            "cancelled" if status == "cancelled" else schedule["status"],
                            campaign_id,
                            wave["wave"],
                        ),
                    )
            if self._after_scheduler_transition is not None:
                self._after_scheduler_transition("campaign_terminal")
            CampaignStore(self.connection).finish(campaign_id, status, stop_reason=reason)
            return self._append_event(
                campaign_id,
                "campaign_terminal",
                "operator",
                command_id,
                request,
                tuple(str(wave["schedule_id"]) for wave in waves),
                int(campaign["event_count"]),
                str(campaign["last_event_digest"]),
            )

    def _append_event(
        self,
        campaign_id: str,
        transition_type: CampaignTransitionKind,
        cause: str,
        command_id: str,
        request_payload: dict[str, object],
        schedule_ids: tuple[str, ...],
        event_count: int,
        previous_digest: str,
    ) -> CampaignTransition:
        sequence = event_count + 1
        payload = {"request": request_payload, "schedule_ids": list(schedule_ids)}
        digest = _digest(
            {
                "campaign_id": campaign_id,
                "sequence": sequence,
                "type": transition_type,
                "cause": cause,
                "command_id": command_id,
                "payload": payload,
                "previous": previous_digest,
            }
        )
        self.connection.execute(
            """INSERT INTO merlin_storage.campaign_events
                   (campaign_id, sequence, type, cause, command_id, payload,
                    previous_digest, digest)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                campaign_id,
                sequence,
                transition_type,
                cause,
                command_id,
                Jsonb(payload),
                previous_digest,
                digest,
            ),
        )
        self.connection.execute(
            """UPDATE merlin_storage.assessment_campaigns
               SET event_count = %s, last_event_digest = %s, updated_at = now()
               WHERE id = %s""",
            (sequence, digest, campaign_id),
        )
        return CampaignTransition(
            campaign_id=campaign_id,
            sequence=sequence,
            type=transition_type,
            cause=cause,
            command_id=command_id,
            schedule_ids=schedule_ids,
            digest=digest,
        )

    @staticmethod
    def _existing_transition(
        campaign_id: str,
        transition_type: CampaignTransitionKind,
        cause: str,
        command_id: str,
        request_payload: dict[str, object],
        row: Mapping[str, object],
    ) -> CampaignTransition:
        stored_payload = row["payload"]
        if not isinstance(stored_payload, Mapping):
            raise RuntimeError("persisted campaign transition payload is not an object")
        stored_request = stored_payload.get("request")
        if (
            str(row["type"]) != transition_type
            or str(row["cause"]) != cause
            or stored_request != request_payload
        ):
            raise ValueError("campaign transition command id conflicts with persisted intent")
        raw_schedule_ids = stored_payload.get("schedule_ids", ())
        if not isinstance(raw_schedule_ids, list) or not all(
            isinstance(value, str) for value in raw_schedule_ids
        ):
            raise RuntimeError("persisted campaign transition schedule ids are invalid")
        return CampaignTransition(
            campaign_id=campaign_id,
            sequence=int(row["sequence"]),
            type=transition_type,
            cause=cause,
            command_id=command_id,
            schedule_ids=tuple(raw_schedule_ids),
            digest=str(row["digest"]),
        )
