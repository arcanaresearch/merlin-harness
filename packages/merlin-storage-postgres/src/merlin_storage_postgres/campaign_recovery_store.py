from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any
from uuid import uuid4

from merlin_agents.target_reobservation import target_reobservation_schedule_ids


def recovery_fence(connection: Any, campaign_id: str) -> str:
    """Fingerprint durable inputs; claim validation never reclassifies an action."""
    journal_rows = connection.execute(
        """SELECT sequence, decision_id, payload
           FROM merlin_storage.campaign_lead_journal
           WHERE campaign_id = %s
           ORDER BY sequence""",
        (campaign_id,),
    ).fetchall()
    campaign = connection.execute(
        """SELECT session_id, manifest FROM merlin_storage.assessment_campaigns
           WHERE id = %s""",
        (campaign_id,),
    ).fetchone()
    envelope = campaign.get("manifest") if isinstance(campaign, Mapping) else None
    reviewed = envelope.get("manifest", envelope) if isinstance(envelope, Mapping) else None
    target = reviewed.get("target") if isinstance(reviewed, Mapping) else None
    target_id = target.get("id") if isinstance(target, Mapping) else None
    validated_schedule_ids = (
        target_reobservation_schedule_ids(
            tuple(dict(item) for item in journal_rows),
            campaign_id=campaign_id,
            session_id=str(campaign["session_id"]),
            target_id=target_id,
        )
        if isinstance(campaign, Mapping) and isinstance(target_id, str) and target_id
        else ()
    )
    snapshot = connection.execute(
        """SELECT jsonb_build_object(
          'campaign', (SELECT to_jsonb(c) FROM merlin_storage.assessment_campaigns c
                       WHERE id = %(id)s),
          'waves', (SELECT jsonb_agg(to_jsonb(w) ORDER BY wave)
                    FROM merlin_storage.campaign_waves w WHERE campaign_id = %(id)s),
          'schedules', (SELECT jsonb_agg(to_jsonb(s) ORDER BY id)
                        FROM merlin_storage.scheduler_schedules s WHERE id IN
                        (SELECT schedule_id FROM merlin_storage.campaign_waves
                         WHERE campaign_id = %(id)s)),
          'batches', (SELECT jsonb_agg(to_jsonb(b) ORDER BY id)
                      FROM merlin_storage.campaign_frontier_batches b WHERE campaign_id = %(id)s),
          'candidates', (SELECT jsonb_agg(to_jsonb(f) ORDER BY id)
                         FROM merlin_storage.campaign_frontier_candidates f
                         WHERE campaign_id = %(id)s),
          'scopes', (SELECT jsonb_agg(to_jsonb(s) ORDER BY id)
                     FROM merlin_storage.state_scopes s WHERE campaign_id = %(id)s),
          'lanes', (SELECT jsonb_agg(to_jsonb(l) ORDER BY id)
                    FROM merlin_storage.state_lanes l WHERE campaign_id = %(id)s),
          'reservations', (SELECT jsonb_agg(to_jsonb(r) ORDER BY r.id)
                           FROM merlin_storage.resource_reservations r
                           JOIN merlin_storage.state_scopes s ON s.id = r.scope_id
                           WHERE s.campaign_id = %(id)s),
          'target_reobservation', (
            SELECT jsonb_agg(
              jsonb_build_object(
                'sequence', j.sequence,
                'decision_id', j.decision_id,
                'kind', j.payload->>'kind',
                'event_digest', j.payload->>'event_digest',
                'generation_id', j.payload->>'generation_id',
                'event_set_digest', j.payload->>'event_set_digest',
                'settlement_id', j.payload->>'settlement_id',
                'schedule_id', j.payload->>'schedule_id',
                'generations', j.payload->'target_reobservation_generations'
              ) ORDER BY j.sequence
            )
            FROM merlin_storage.campaign_lead_journal j
            WHERE j.campaign_id = %(id)s
              AND (
                j.payload->>'kind' IN (
                  'target_surface_transition_observed',
                  'target_reobservation_started',
                  'target_reobservation_blocked',
                  'target_reobservation_retry_authorized',
                  'target_reobservation_settled'
                )
                OR j.payload ? 'target_reobservation_generations'
              )
          ),
          'target_reobservation_schedules', (
            SELECT jsonb_agg(to_jsonb(s) ORDER BY s.id)
            FROM merlin_storage.scheduler_schedules s
            WHERE s.id = ANY(%(target_reobservation_schedule_ids)s::uuid[])
          )
        ) AS snapshot""",
        {
            "id": campaign_id,
            "target_reobservation_schedule_ids": list(validated_schedule_ids),
        },
    ).fetchone()["snapshot"]
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()
    return "sha256:" + sha256(encoded).hexdigest()


class CampaignRecoveryRepository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self._lock = connection._merlin_operation_lock

    def claim(
        self,
        campaign_id: str,
        *,
        state_digest: str,
        decision_digest: str,
        claimant_id: str,
        fence_digest: str,
        lease_seconds: int = 30,
    ) -> str | None:
        if not claimant_id.strip() or lease_seconds < 1:
            raise ValueError("recovery claim requires claimant and positive lease")
        with self._lock, self.connection.transaction():
            self.connection.execute(
                "SELECT id FROM merlin_storage.assessment_campaigns WHERE id = %s FOR UPDATE",
                (campaign_id,),
            ).fetchone()
            live_claim = self.connection.execute(
                """SELECT 1 FROM merlin_storage.campaign_recovery
                   WHERE campaign_id = %s AND application_state = 'claimed'
                     AND claim_expires_at > now()""",
                (campaign_id,),
            ).fetchone()
            if live_claim is not None:
                return None
            row = self.connection.execute(
                """SELECT *, claim_expires_at > now() AS live
                   FROM merlin_storage.campaign_recovery
                   WHERE campaign_id = %s AND state_digest = %s AND decision_digest = %s
                   FOR UPDATE""",
                (campaign_id, state_digest, decision_digest),
            ).fetchone()
            if row is None or row["application_state"] in {"applied", "rejected"}:
                return None
            if row["application_state"] == "claimed" and row["live"]:
                return None
            if row["fence_digest"] != fence_digest:
                return None
            if fence_digest != recovery_fence(self.connection, campaign_id):
                self.connection.execute(
                    """UPDATE merlin_storage.campaign_recovery
                       SET application_state = 'rejected', application_error = 'stale plan'
                       WHERE campaign_id = %s AND state_digest = %s""",
                    (campaign_id, state_digest),
                )
                return None
            claim_id = str(uuid4())
            self.connection.execute(
                """UPDATE merlin_storage.campaign_recovery
                   SET application_state = 'claimed', claim_id = %s, claimant_id = %s,
                       claim_expires_at = now() + make_interval(secs => %s),
                       application_error = NULL
                   WHERE campaign_id = %s AND state_digest = %s""",
                (claim_id, claimant_id, lease_seconds, campaign_id, state_digest),
            )
            return claim_id

    def renew(self, campaign_id: str, *, claim_id: str, lease_seconds: int = 30) -> bool:
        if lease_seconds < 1:
            raise ValueError("recovery claim lease must be positive")
        with self._lock, self.connection.transaction():
            return (
                self.connection.execute(
                    """UPDATE merlin_storage.campaign_recovery
                   SET claim_expires_at = now() + make_interval(secs => %s)
                   WHERE campaign_id = %s AND claim_id = %s AND application_state = 'claimed'
                     AND claim_expires_at > now() RETURNING campaign_id""",
                    (lease_seconds, campaign_id, claim_id),
                ).fetchone()
                is not None
            )

    def apply(self, campaign_id: str, *, claim_id: str, error: str | None = None) -> None:
        with self._lock, self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_recovery
                   SET application_state = %s,
                       applied_at = CASE WHEN %s::text IS NULL THEN now() END,
                       application_error = %s
                   WHERE campaign_id = %s AND claim_id = %s AND application_state = 'claimed'
                     AND claim_expires_at > now() RETURNING campaign_id""",
                ("applied" if error is None else "rejected", error, error, campaign_id, claim_id),
            ).fetchone()
            if row is None:
                raise ValueError("recovery claim expired or was superseded")

    def reject_incompatible(self, campaign_id: str, *, claim_id: str, error: str) -> None:
        """Quarantine one unreadable sealed campaign without blocking daemon startup."""
        if not error.strip():
            raise ValueError("incompatible recovery requires an error")
        with self._lock, self.connection.transaction():
            campaign = self.connection.execute(
                """UPDATE merlin_storage.assessment_campaigns
                   SET lifecycle_resume_status = coalesce(lifecycle_resume_status, status),
                       status = 'paused', lifecycle_pause_reason = 'recovery_incompatible',
                       lifecycle_wake_at = NULL, updated_at = now()
                   WHERE id = %s AND status IN ('prepared', 'executing', 'paused')
                   RETURNING id""",
                (campaign_id,),
            ).fetchone()
            recovery = self.connection.execute(
                """UPDATE merlin_storage.campaign_recovery
                   SET application_state = 'rejected', application_error = %s
                   WHERE campaign_id = %s AND claim_id = %s AND application_state = 'claimed'
                     AND claim_expires_at > now() RETURNING campaign_id""",
                (error, campaign_id, claim_id),
            ).fetchone()
            if campaign is None or recovery is None:
                raise ValueError("incompatible recovery claim expired or was superseded")
            self.connection.execute(
                """UPDATE merlin_storage.campaign_waves
                   SET status = 'paused', updated_at = now()
                   WHERE campaign_id = %s AND status = 'running'""",
                (campaign_id,),
            )
