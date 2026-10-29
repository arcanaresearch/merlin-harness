from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from threading import RLock
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from merlin_agents.target_reobservation import project_target_reobservations
from merlin_contracts.state_scope import (
    LEGACY_ATTEMPT_SCHEMA_VERSION,
    STATE_SCOPE_SCHEMA_VERSION,
    KeyedIdentityDigest,
    OpaqueIdentityReference,
    StateScopeEnvelope,
)
from psycopg.types.json import Jsonb

_RECOVERY_POLICY_VERSION = "merlin.campaign-recovery-policy.v4"


class _StateScopeLeaseUnavailable(RuntimeError):
    pass


def _envelope(value: StateScopeEnvelope | Mapping[str, object]) -> StateScopeEnvelope:
    if isinstance(value, StateScopeEnvelope):
        return value
    return StateScopeEnvelope.model_validate(value)


def _identity_key(
    value: KeyedIdentityDigest | OpaqueIdentityReference | None,
) -> str | None:
    if value is None:
        return None
    if isinstance(value, KeyedIdentityDigest):
        return f"keyed_digest:{value.algorithm}:{value.key_id}:{value.digest}"
    return f"opaque_reference:{value.namespace}:{value.reference_id}"


def _identity_mapping_key(value: Mapping[str, object] | None) -> str | None:
    if value is None:
        return None
    if value.get("kind") == "keyed_digest":
        return _identity_key(KeyedIdentityDigest.model_validate(value))
    if value.get("kind") == "opaque_reference":
        return _identity_key(OpaqueIdentityReference.model_validate(value))
    raise ValueError("state lane identity must use a secret-safe identity reference")


def _digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return "sha256:" + sha256(encoded).hexdigest()


def _required(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} cannot be empty")
    return normalized


def _physical_lane_id(authority: str, principal_key: str, physical_state_key: str) -> str:
    return (
        "state-physical-lane:"
        + sha256(f"{authority}\0{principal_key}\0{physical_state_key}".encode()).hexdigest()
    )


def _conservative_physical_lane_id(authority: str) -> str:
    return _physical_lane_id(authority, "anonymous", "conservative-shared-state")


def _scheduler_recovery_action(
    campaign_status: str,
    latest_wave: Mapping[str, object] | None,
    active_batch: Mapping[str, object] | None,
    frontier_counts: Mapping[str, int],
) -> str:
    """Classify one exact scheduler/wave/frontier persistence boundary."""

    queued = int(frontier_counts.get("queued", 0))
    if campaign_status == "prepared":
        if latest_wave is None and active_batch is None:
            return "await_approval"
        return "pause_inconsistent"
    if active_batch is not None:
        batch_state = str(active_batch["state"])
        if batch_state == "claimed":
            return (
                "bind_successor_schedule"
                if active_batch.get("schedule_status") is not None
                else "persist_successor_schedule"
            )
        if latest_wave is None or int(latest_wave["wave"]) < int(active_batch["wave"]):
            return "persist_wave_binding"
        if str(active_batch.get("schedule_id") or "") != str(
            latest_wave.get("schedule_id") or ""
        ) or int(active_batch["wave"]) != int(latest_wave["wave"]):
            return "pause_inconsistent"
        schedule_status = str(latest_wave["schedule_status"])
        wave_status = str(latest_wave["wave_status"])
        if schedule_status in {"completed", "failed", "cancelled"}:
            return (
                "finalize_and_settle_terminal_batch"
                if wave_status in {"running", "paused"}
                else "settle_terminal_batch"
            )
        if schedule_status == "running" and wave_status in {"running", "paused"}:
            return "paused" if campaign_status == "paused" else "resume_active"
        if schedule_status == "paused" and wave_status == "paused" and campaign_status == "paused":
            return "paused"
        return "pause_inconsistent"
    if latest_wave is not None:
        schedule_status = str(latest_wave["schedule_status"])
        wave_status = str(latest_wave["wave_status"])
        if schedule_status in {"completed", "failed", "cancelled"}:
            if wave_status in {"running", "paused"}:
                return "finalize_terminal_wave"
            return "claim_successor" if queued else "idle"
        if schedule_status == "running" and wave_status in {"running", "paused"}:
            return "paused" if campaign_status == "paused" else "resume_active"
        if schedule_status == "paused" and wave_status == "paused" and campaign_status == "paused":
            return "paused"
        return "pause_inconsistent"
    if queued and campaign_status == "executing":
        return "claim_successor"
    return "paused" if campaign_status == "paused" else "idle"


def _target_reobservation_recovery_action(
    campaign_status: str,
    scheduler_action: str,
    projection: Mapping[str, object],
) -> str:
    """Put the durable target-surface barrier before the next scheduler wave."""

    if campaign_status != "executing" or scheduler_action not in {
        "bind_successor_schedule",
        "claim_successor",
        "idle",
        "persist_successor_schedule",
        "persist_wave_binding",
        "resume_active",
        "settle_terminal_batch",
    }:
        return scheduler_action
    if projection.get("recovery_state") is not None:
        return "recover_target_reobservation"
    return scheduler_action


def _campaign_target_id(campaign: Mapping[str, object]) -> str | None:
    envelope = campaign.get("manifest")
    reviewed = envelope.get("manifest", envelope) if isinstance(envelope, Mapping) else None
    target = reviewed.get("target") if isinstance(reviewed, Mapping) else None
    target_id = target.get("id") if isinstance(target, Mapping) else None
    return target_id if isinstance(target_id, str) and target_id else None


class StateScopeRepository:
    """Versioned state envelopes plus global isolation and resource coordination."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self._lock = RLock()

    def create_state_scope(
        self,
        campaign_id: str,
        target_authority: str,
        envelope: StateScopeEnvelope | Mapping[str, object],
        *,
        isolation_disposition: str = "unknown",
    ) -> dict[str, Any]:
        """Create one new-format action boundary and its immutable first revision."""

        value = _envelope(envelope)
        if value.revision != 1:
            raise ValueError("a new state scope must start at revision 1")
        campaign = _required(campaign_id, "campaign id")
        authority = _required(target_authority, "target authority")
        if value.campaign_id != campaign:
            raise ValueError("state scope campaign identity does not match its envelope")
        if value.target_authority != authority:
            raise ValueError("state scope target authority does not match its envelope")
        if value.isolation_lease_id is not None:
            raise ValueError("a new state scope cannot bind a lease before it is claimed")
        document = value.model_dump(mode="json")
        document_digest = _digest(document)
        principal_key = _identity_key(value.principal_identity)
        physical_key = _identity_key(value.physical_state_identity)
        lane_id = (
            "state-lane:"
            + sha256(f"{campaign}\0{value.experiment_id}\0{authority}".encode()).hexdigest()
        )
        with self._lock, self.connection.transaction():
            format_row = self.connection.execute(
                """SELECT claims_enabled FROM merlin_storage.scope_formats
                   WHERE version = %s FOR SHARE""",
                (value.schema_version,),
            ).fetchone()
            if format_row is None or not bool(format_row["claims_enabled"]):
                raise ValueError("state scope format is unavailable for new claims")
            campaign_row = self.connection.execute(
                """SELECT execution_format FROM merlin_storage.assessment_campaigns
                   WHERE id = %s FOR UPDATE""",
                (campaign,),
            ).fetchone()
            if campaign_row is None:
                raise KeyError(campaign)
            current_format = str(campaign_row["execution_format"])
            if current_format not in {LEGACY_ATTEMPT_SCHEMA_VERSION, value.schema_version}:
                raise ValueError("campaign execution format is incompatible with state scopes")
            self.connection.execute(
                """UPDATE merlin_storage.assessment_campaigns
                   SET execution_format = %s, updated_at = now() WHERE id = %s""",
                (value.schema_version, campaign),
            )
            conservative_lane_id = _conservative_physical_lane_id(authority)
            self.connection.execute(
                """INSERT INTO merlin_storage.state_physical_lanes
                       (id, target_authority, principal_key, physical_state_key)
                   VALUES (%s, %s, 'anonymous', 'conservative-shared-state')
                   ON CONFLICT (target_authority, principal_key, physical_state_key)
                   DO NOTHING""",
                (conservative_lane_id, authority),
            )
            conservative_lane = self.connection.execute(
                """SELECT revision, ambiguity_revision
                   FROM merlin_storage.state_physical_lanes
                   WHERE id = %s FOR SHARE""",
                (conservative_lane_id,),
            ).fetchone()
            if conservative_lane is None:
                raise RuntimeError("conservative physical state lane disappeared")
            initial_physical_lane_id: str | None = None
            initial_physical_lane_revision: int | None = None
            if physical_key is not None:
                aggregate_principal = principal_key or "anonymous"
                initial_physical_lane_id = _physical_lane_id(
                    authority, aggregate_principal, physical_key
                )
                self.connection.execute(
                    """INSERT INTO merlin_storage.state_physical_lanes
                           (id, target_authority, principal_key, physical_state_key)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (target_authority, principal_key, physical_state_key)
                       DO NOTHING""",
                    (initial_physical_lane_id, authority, aggregate_principal, physical_key),
                )
                initial_physical = self.connection.execute(
                    """SELECT revision FROM merlin_storage.state_physical_lanes
                       WHERE id = %s FOR SHARE""",
                    (initial_physical_lane_id,),
                ).fetchone()
                if initial_physical is None:
                    raise RuntimeError("physical state lane disappeared during scope creation")
                initial_physical_lane_revision = int(initial_physical["revision"])
            created_lane = self.connection.execute(
                """INSERT INTO merlin_storage.state_lanes
                       (id, campaign_id, experiment_id, target_authority,
                        principal_key, physical_state_key, physical_lane_id,
                        physical_lane_revision, conservative_lane_revision,
                        conservative_ambiguity_revision)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (campaign_id, experiment_id, target_authority)
                   DO NOTHING RETURNING id""",
                (
                    lane_id,
                    campaign,
                    value.experiment_id,
                    authority,
                    principal_key,
                    physical_key,
                    initial_physical_lane_id,
                    initial_physical_lane_revision,
                    int(conservative_lane["revision"]),
                    int(conservative_lane["ambiguity_revision"]),
                ),
            ).fetchone()
            lane = self.connection.execute(
                """SELECT * FROM merlin_storage.state_lanes
                   WHERE campaign_id = %s AND experiment_id = %s
                     AND target_authority = %s FOR UPDATE""",
                (campaign, value.experiment_id, authority),
            ).fetchone()
            if lane is None:
                raise RuntimeError("state lane disappeared during scope creation")
            lane_id = str(lane["id"])
            for stored_key, incoming_key in (
                (lane["principal_key"], principal_key),
                (lane["physical_state_key"], physical_key),
            ):
                if (
                    stored_key is not None
                    and incoming_key is not None
                    and str(stored_key) != incoming_key
                ):
                    raise ValueError("state lane identity conflicts with durable state")
            if created_lane is not None:
                self.connection.execute(
                    """INSERT INTO merlin_storage.state_lane_revisions
                           (lane_id, revision, transition, principal_key,
                            physical_state_key)
                       VALUES (%s, 1, 'created', %s, %s)""",
                    (lane_id, principal_key, physical_key),
                )
            expected_lane_revision = int(lane["revision"])
            self.connection.execute(
                """INSERT INTO merlin_storage.state_scopes
                       (id, format_version, campaign_id, experiment_id, attempt_id, candidate_id,
                        action_id, action_kind, target_authority, principal_key,
                        physical_state_key, isolation_disposition, current_revision,
                        lane_id, expected_lane_revision, expected_physical_lane_id,
                        expected_physical_lane_revision,
                        expected_conservative_lane_revision,
                        expected_conservative_ambiguity_revision)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s,
                           %s, %s, %s, %s)
                   ON CONFLICT (id) DO NOTHING""",
                (
                    value.scope_id,
                    value.schema_version,
                    campaign,
                    value.experiment_id,
                    value.attempt_id,
                    value.candidate_id,
                    value.action_id,
                    value.action_kind.value,
                    authority,
                    principal_key,
                    physical_key,
                    isolation_disposition,
                    lane_id,
                    expected_lane_revision,
                    lane["physical_lane_id"],
                    lane["physical_lane_revision"],
                    lane["conservative_lane_revision"],
                    lane["conservative_ambiguity_revision"],
                ),
            )
            stored = self.connection.execute(
                "SELECT * FROM merlin_storage.state_scopes WHERE id = %s FOR UPDATE",
                (value.scope_id,),
            ).fetchone()
            if stored is None:
                raise RuntimeError("state scope disappeared during creation")
            identity = (
                str(stored["format_version"]),
                str(stored["campaign_id"]),
                str(stored["experiment_id"]),
                str(stored["attempt_id"]),
                stored["candidate_id"],
                str(stored["action_id"]),
                str(stored["action_kind"]),
                str(stored["target_authority"]),
                stored["principal_key"],
                stored["physical_state_key"],
            )
            expected = (
                value.schema_version,
                campaign,
                value.experiment_id,
                value.attempt_id,
                value.candidate_id,
                value.action_id,
                value.action_kind.value,
                authority,
                principal_key,
                physical_key,
            )
            if identity != expected:
                raise ValueError("state scope identity conflicts with durable state")
            self.connection.execute(
                """INSERT INTO merlin_storage.scope_envelopes
                       (scope_id, revision, envelope, envelope_digest)
                   VALUES (%s, 1, %s, %s)
                   ON CONFLICT (scope_id, revision) DO NOTHING""",
                (value.scope_id, Jsonb(document), document_digest),
            )
            revision = self.connection.execute(
                """SELECT envelope_digest FROM merlin_storage.scope_envelopes
                   WHERE scope_id = %s AND revision = 1""",
                (value.scope_id,),
            ).fetchone()
            if revision is None or str(revision["envelope_digest"]) != document_digest:
                raise ValueError("state scope first revision conflicts with durable state")
            return self._load_state_scope_locked(value.scope_id)

    def revise_state_scope(
        self,
        scope_id: str,
        expected_revision: int,
        envelope: StateScopeEnvelope | Mapping[str, object],
        *,
        claim_id: str,
        claimant_id: str,
    ) -> dict[str, Any]:
        """Append one immutable CAS revision without rewriting prior evidence."""

        value = _envelope(envelope)
        if value.scope_id != scope_id:
            raise ValueError("state scope revision identity changed")
        with self._lock, self.connection.transaction():
            self._require_state_scope_lease_locked(scope_id, claim_id, claimant_id)
            return self._revise_state_scope_locked(
                scope_id, expected_revision, value, claim_id=claim_id
            )

    def acknowledge_state_scope(
        self, scope_id: str, *, claim_id: str, claimant_id: str
    ) -> dict[str, Any]:
        """Record that the target action was delivered before result settlement."""

        with self._lock, self.connection.transaction():
            self._require_state_scope_lease_locked(scope_id, claim_id, claimant_id)
            row = self.connection.execute(
                """UPDATE merlin_storage.state_scopes
                   SET status = 'effect_acknowledged', effect_acknowledged_at = now(),
                       updated_at = now()
                   WHERE id = %s AND status = 'open' RETURNING id""",
                (scope_id,),
            ).fetchone()
            if row is None:
                current = self.connection.execute(
                    "SELECT status FROM merlin_storage.state_scopes WHERE id = %s",
                    (scope_id,),
                ).fetchone()
                if current is None:
                    raise KeyError(scope_id)
                if str(current["status"]) not in {"effect_acknowledged", "settled"}:
                    raise ValueError("only an open state scope can acknowledge target delivery")
            return self._load_state_scope_locked(scope_id)

    def settle_state_scope(
        self,
        scope_id: str,
        expected_revision: int,
        envelope: StateScopeEnvelope | Mapping[str, object],
        *,
        transition_disposition: str,
        isolation_disposition: str,
        cleanup_disposition: str,
        claim_id: str,
        claimant_id: str,
    ) -> dict[str, Any]:
        """Append the terminal envelope and settle the action exactly once."""

        value = _envelope(envelope)
        if value.scope_id != scope_id:
            raise ValueError("state scope settlement identity changed")
        with self._lock, self.connection.transaction():
            self._require_state_scope_lease_locked(scope_id, claim_id, claimant_id)
            current = self.connection.execute(
                "SELECT * FROM merlin_storage.state_scopes WHERE id = %s FOR UPDATE",
                (scope_id,),
            ).fetchone()
            if current is None:
                raise KeyError(scope_id)
            if str(current["status"]) == "settled":
                existing = self._load_state_scope_locked(scope_id)
                if (
                    existing["transition_disposition"] != transition_disposition
                    or existing["isolation_disposition"] != isolation_disposition
                    or existing["cleanup_disposition"] != cleanup_disposition
                ):
                    raise ValueError("state scope was already settled differently")
                return existing
            if str(current["status"]) != "effect_acknowledged":
                raise ValueError("only an acknowledged target effect can be settled")
            revised = self._revise_state_scope_locked(
                scope_id, expected_revision, value, claim_id=claim_id
            )
            self._advance_state_lane_locked(scope_id, value)
            self.connection.execute(
                """UPDATE merlin_storage.state_scopes
                   SET status = 'settled', transition_disposition = %s,
                       isolation_disposition = %s, cleanup_disposition = %s,
                       settled_at = now(), updated_at = now()
                   WHERE id = %s""",
                (
                    transition_disposition,
                    isolation_disposition,
                    cleanup_disposition,
                    scope_id,
                ),
            )
            return {**revised, **self._load_state_scope_locked(scope_id)}

    def pause_state_scope(
        self,
        scope_id: str,
        expected_revision: int,
        envelope: StateScopeEnvelope | Mapping[str, object],
        *,
        transition_disposition: str,
        isolation_disposition: str,
        cleanup_disposition: str,
        claim_id: str,
        claimant_id: str,
    ) -> dict[str, Any]:
        """Terminally pause an action that did not produce an acknowledged target effect."""

        value = _envelope(envelope)
        if value.scope_id != scope_id:
            raise ValueError("state scope pause identity changed")
        with self._lock, self.connection.transaction():
            self._require_state_scope_lease_locked(scope_id, claim_id, claimant_id)
            current = self.connection.execute(
                "SELECT status FROM merlin_storage.state_scopes WHERE id = %s FOR UPDATE",
                (scope_id,),
            ).fetchone()
            if current is None:
                raise KeyError(scope_id)
            if str(current["status"]) == "paused":
                return self._load_state_scope_locked(scope_id)
            if str(current["status"]) != "open":
                raise ValueError("only a pre-delivery state scope can be paused")
            self._revise_state_scope_locked(scope_id, expected_revision, value, claim_id=claim_id)
            self.connection.execute(
                """UPDATE merlin_storage.state_scopes
                   SET status = 'paused', transition_disposition = %s,
                       isolation_disposition = %s, cleanup_disposition = %s,
                       updated_at = now() WHERE id = %s""",
                (
                    transition_disposition,
                    isolation_disposition,
                    cleanup_disposition,
                    scope_id,
                ),
            )
            return self._load_state_scope_locked(scope_id)

    def pause_unclaimed_state_scope(
        self,
        scope_id: str,
        expected_revision: int,
        envelope: StateScopeEnvelope | Mapping[str, object],
        *,
        transition_disposition: str,
        isolation_disposition: str,
        cleanup_disposition: str,
    ) -> dict[str, Any]:
        """Pause a losing pre-delivery scope only while it has no global lease."""

        value = _envelope(envelope)
        if value.scope_id != scope_id or value.isolation_lease_id is not None:
            raise ValueError("unclaimed state scope evidence cannot bind a lease")
        with self._lock, self.connection.transaction():
            current = self.connection.execute(
                "SELECT status FROM merlin_storage.state_scopes WHERE id = %s FOR UPDATE",
                (scope_id,),
            ).fetchone()
            if current is None:
                raise KeyError(scope_id)
            lease = self.connection.execute(
                """SELECT 1 FROM merlin_storage.state_scope_leases
                   WHERE scope_id = %s AND expires_at > now()""",
                (scope_id,),
            ).fetchone()
            if lease is not None:
                raise ValueError("claimed state scope requires its fencing token")
            if str(current["status"]) == "paused":
                return self._load_state_scope_locked(scope_id)
            if str(current["status"]) != "open":
                raise ValueError("only an unacknowledged state scope can pause without a claim")
            self._revise_state_scope_locked(scope_id, expected_revision, value, claim_id=None)
            self.connection.execute(
                """UPDATE merlin_storage.state_scopes
                   SET status = 'paused', transition_disposition = %s,
                       isolation_disposition = %s, cleanup_disposition = %s,
                       updated_at = now() WHERE id = %s""",
                (
                    transition_disposition,
                    isolation_disposition,
                    cleanup_disposition,
                    scope_id,
                ),
            )
            return self._load_state_scope_locked(scope_id)

    def load_state_scope(self, scope_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT 1 FROM merlin_storage.state_scopes WHERE id = %s", (scope_id,)
            ).fetchone()
            return None if row is None else self._load_state_scope_locked(scope_id)

    def reobserve_state_lane(
        self,
        scope_id: str,
        expected_revision: int,
        evidence_refs: Sequence[str],
        *,
        claim_id: str,
        claimant_id: str,
        principal_identity: Mapping[str, object] | None = None,
        physical_state_identity: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        """Fence restart recovery with a fresh, authoritative before observation."""

        refs = tuple(dict.fromkeys(ref for ref in evidence_refs if ref))
        with self._lock, self.connection.transaction():
            self._require_state_scope_lease_locked(scope_id, claim_id, claimant_id)
            scope = self.connection.execute(
                """SELECT lane_id, expected_lane_revision, principal_key,
                          physical_state_key
                   FROM merlin_storage.state_scopes
                   WHERE id = %s FOR UPDATE""",
                (scope_id,),
            ).fetchone()
            if scope is None:
                raise KeyError(scope_id)
            if scope["lane_id"] is None:
                return {"revision": expected_revision, "status": "current"}
            lane = self.connection.execute(
                "SELECT * FROM merlin_storage.state_lanes WHERE id = %s FOR UPDATE",
                (scope["lane_id"],),
            ).fetchone()
            if lane is None:
                raise RuntimeError("state scope lane disappeared")
            current_revision = int(lane["revision"])
            if (
                expected_revision != current_revision
                or int(scope["expected_lane_revision"]) != current_revision
            ):
                raise ValueError("state scope expected state is stale")
            if str(lane["status"]) == "current":
                return dict(lane)
            if not refs:
                raise ValueError(
                    "state lane restart recovery requires authoritative before evidence"
                )
            principal_key = _identity_mapping_key(principal_identity) or (
                str(scope["principal_key"]) if scope["principal_key"] is not None else None
            )
            physical_key = _identity_mapping_key(physical_state_identity) or (
                str(scope["physical_state_key"])
                if scope["physical_state_key"] is not None
                else None
            )
            next_revision = current_revision + 1
            observation_digest = _digest({"evidence_refs": list(refs)})
            self.connection.execute(
                """INSERT INTO merlin_storage.state_lane_revisions
                       (lane_id, revision, scope_id, transition, principal_key,
                        physical_state_key, observation_digest)
                   VALUES (%s, %s, %s, 'unattributed', %s, %s, %s)""",
                (
                    lane["id"],
                    next_revision,
                    scope_id,
                    principal_key,
                    physical_key,
                    observation_digest,
                ),
            )
            updated = self.connection.execute(
                """UPDATE merlin_storage.state_lanes
                   SET revision = %s, status = 'current',
                       last_transition = 'unattributed', principal_key = %s,
                       physical_state_key = %s, latest_observation_digest = %s,
                       latest_scope_id = %s, reobserved_at = now(), updated_at = now()
                   WHERE id = %s AND revision = %s RETURNING *""",
                (
                    next_revision,
                    principal_key,
                    physical_key,
                    observation_digest,
                    scope_id,
                    lane["id"],
                    current_revision,
                ),
            ).fetchone()
            if updated is None:
                raise ValueError("state lane changed during authoritative re-observation")
            self.connection.execute(
                """UPDATE merlin_storage.state_scopes
                   SET expected_lane_revision = %s, principal_key = COALESCE(%s, principal_key),
                       physical_state_key = COALESCE(%s, physical_state_key), updated_at = now()
                   WHERE id = %s""",
                (next_revision, principal_key, physical_key, scope_id),
            )
            return dict(updated)

    def set_scope_claims_enabled(self, format_version: str, enabled: bool) -> bool:
        """Gate only new scopes; acknowledged effects remain settleable."""

        with self._lock, self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.scope_formats
                   SET claims_enabled = %s, updated_at = now()
                   WHERE version = %s RETURNING version""",
                (enabled, format_version),
            ).fetchone()
            return row is not None

    def claim_state_scope_lease(
        self,
        scope_id: str,
        *,
        target_authority: str,
        principal_key: str,
        equivalence_key: str,
        claimant_id: str,
        lease_seconds: int,
    ) -> str | None:
        """Globally fence one reviewed physical-state equivalence class."""

        authority = _required(target_authority, "target authority")
        principal = _required(principal_key, "principal key")
        equivalence = _required(equivalence_key, "equivalence key")
        claimant = _required(claimant_id, "claimant id")
        if lease_seconds < 1:
            raise ValueError("state scope lease duration must be positive")
        claim_id = str(uuid4())
        lock_key = json.dumps((authority, principal, equivalence), separators=(",", ":"))
        with self._lock, self.connection.transaction():
            scope = self.connection.execute(
                """SELECT scope.target_authority, scope.principal_key, scope.status,
                          scope.expected_lane_revision, lane.revision AS lane_revision,
                          lane.status AS lane_status, scope.action_kind
                   FROM merlin_storage.state_scopes AS scope
                   LEFT JOIN merlin_storage.state_lanes AS lane ON lane.id = scope.lane_id
                   WHERE scope.id = %s FOR UPDATE OF scope""",
                (scope_id,),
            ).fetchone()
            if scope is None:
                raise KeyError(scope_id)
            if str(scope["status"]) not in {"open", "effect_acknowledged"}:
                raise ValueError("state scope is not claimable")
            if scope["expected_lane_revision"] is not None and int(
                scope["expected_lane_revision"]
            ) != int(scope["lane_revision"]):
                raise ValueError("state scope expected state is stale")
            expected_principal = str(scope["principal_key"] or "anonymous")
            if str(scope["target_authority"]) != authority or expected_principal != principal:
                raise ValueError("state scope lease authority or principal changed")
            self.connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,)
            )
            self.connection.execute(
                """DELETE FROM merlin_storage.state_scope_leases
                   WHERE target_authority = %s AND principal_key = %s
                     AND equivalence_key = %s AND expires_at <= now()""",
                (authority, principal, equivalence),
            )
            existing = self.connection.execute(
                """UPDATE merlin_storage.state_scope_leases
                   SET expires_at = now() + make_interval(secs => %s), updated_at = now()
                   WHERE target_authority = %s AND principal_key = %s
                     AND equivalence_key = %s AND scope_id = %s AND claimant_id = %s
                   RETURNING claim_id""",
                (lease_seconds, authority, principal, equivalence, scope_id, claimant),
            ).fetchone()
            if existing is not None:
                self._require_physical_lane_current_locked(scope_id)
                return str(existing["claim_id"])
            row = self.connection.execute(
                """INSERT INTO merlin_storage.state_scope_leases
                       (target_authority, principal_key, equivalence_key, claim_id,
                        scope_id, claimant_id, expires_at)
                   VALUES (%s, %s, %s, %s, %s, %s,
                           now() + make_interval(secs => %s))
                   ON CONFLICT (target_authority, principal_key, equivalence_key)
                   DO NOTHING RETURNING claim_id""",
                (authority, principal, equivalence, claim_id, scope_id, claimant, lease_seconds),
            ).fetchone()
            if row is None:
                return None
            self._require_physical_lane_current_locked(scope_id)
            return str(row["claim_id"])

    def create_and_claim_state_scope(
        self,
        campaign_id: str,
        target_authority: str,
        envelope: StateScopeEnvelope | Mapping[str, object],
        *,
        principal_key: str,
        equivalence_key: str,
        claimant_id: str,
        lease_seconds: int,
        isolation_disposition: str = "unknown",
    ) -> dict[str, Any] | None:
        """Create a scope and its global lease atomically, leaving no loser orphan."""

        try:
            with self._lock, self.connection.transaction():
                scope = self.create_state_scope(
                    campaign_id,
                    target_authority,
                    envelope,
                    isolation_disposition=isolation_disposition,
                )
                claim_id = self.claim_state_scope_lease(
                    str(scope["id"]),
                    target_authority=target_authority,
                    principal_key=principal_key,
                    equivalence_key=equivalence_key,
                    claimant_id=claimant_id,
                    lease_seconds=lease_seconds,
                )
                if claim_id is None:
                    raise _StateScopeLeaseUnavailable
                return {"scope": scope, "claim_id": claim_id}
        except _StateScopeLeaseUnavailable:
            return None

    def rekey_state_scope_lease(
        self,
        claim_id: str,
        claimant_id: str,
        *,
        principal_key: str,
        equivalence_key: str,
        maximum_parallel_states: int | None = None,
    ) -> bool:
        """Atomically refine a conservative lease without opening an ownership gap."""

        claim = _required(claim_id, "claim id")
        claimant = _required(claimant_id, "claimant id")
        principal = _required(principal_key, "principal key")
        equivalence = _required(equivalence_key, "equivalence key")
        if maximum_parallel_states is not None and (
            isinstance(maximum_parallel_states, bool) or maximum_parallel_states < 1
        ):
            raise ValueError("maximum parallel states must be a positive integer")
        with self._lock, self.connection.transaction():
            lease = self.connection.execute(
                """SELECT lease.*, scope.principal_key AS scope_principal,
                          scope.status AS scope_status,
                          scope.effect_acknowledged_at AS scope_effect_acknowledged_at
                   FROM merlin_storage.state_scope_leases AS lease
                   JOIN merlin_storage.state_scopes AS scope ON scope.id = lease.scope_id
                   WHERE lease.claim_id = %s AND lease.claimant_id = %s
                     AND lease.expires_at > now()
                   FOR UPDATE OF lease, scope""",
                (claim, claimant),
            ).fetchone()
            if lease is None:
                return False
            scope_principal = lease["scope_principal"]
            if scope_principal is not None and str(scope_principal) != principal:
                provisional = str(scope_principal) == "anonymous"
                pre_delivery = (
                    str(lease["scope_status"]) == "open"
                    and lease["scope_effect_acknowledged_at"] is None
                )
                if not (provisional and pre_delivery and principal != "anonymous"):
                    raise ValueError("state scope principal identity cannot be changed")
            authority = str(lease["target_authority"])
            if maximum_parallel_states is not None:
                capacity_key = json.dumps(
                    (authority, principal, "independent-state-capacity"),
                    separators=(",", ":"),
                )
                self.connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (capacity_key,),
                )
                active = self.connection.execute(
                    """SELECT count(*) AS count
                       FROM merlin_storage.state_scope_leases
                       WHERE target_authority = %s AND principal_key = %s
                         AND equivalence_key <> 'conservative-shared-state'
                         AND expires_at > now() AND claim_id <> %s""",
                    (authority, principal, claim),
                ).fetchone()
                if active is not None and int(active["count"]) >= maximum_parallel_states:
                    return False
            lock_key = json.dumps((authority, principal, equivalence), separators=(",", ":"))
            self.connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,)
            )
            self.connection.execute(
                """DELETE FROM merlin_storage.state_scope_leases
                   WHERE target_authority = %s AND principal_key = %s
                     AND equivalence_key = %s AND expires_at <= now()""",
                (authority, principal, equivalence),
            )
            conflict = self.connection.execute(
                """SELECT 1 FROM merlin_storage.state_scope_leases
                   WHERE target_authority = %s AND principal_key = %s
                     AND equivalence_key = %s AND claim_id <> %s""",
                (authority, principal, equivalence, claim),
            ).fetchone()
            if conflict is not None:
                return False
            self.connection.execute(
                """UPDATE merlin_storage.state_scope_leases
                   SET principal_key = %s, equivalence_key = %s, updated_at = now()
                   WHERE claim_id = %s AND claimant_id = %s""",
                (principal, equivalence, claim, claimant),
            )
            if principal != "anonymous":
                self.connection.execute(
                    """UPDATE merlin_storage.state_scopes
                       SET principal_key = CASE
                               WHEN principal_key IS NULL OR principal_key = 'anonymous'
                               THEN %s ELSE principal_key END,
                           updated_at = now()
                       WHERE id = %s""",
                    (principal, lease["scope_id"]),
                )
            return True

    def renew_state_scope_lease(self, claim_id: str, claimant_id: str, lease_seconds: int) -> bool:
        claimant = _required(claimant_id, "claimant id")
        if not claim_id.strip() or lease_seconds < 1:
            raise ValueError("state scope lease renewal requires a claim and duration")
        with self._lock, self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.state_scope_leases
                   SET expires_at = now() + make_interval(secs => %s), updated_at = now()
                   WHERE claim_id = %s AND claimant_id = %s AND expires_at > now()
                   RETURNING claim_id""",
                (lease_seconds, claim_id, claimant),
            ).fetchone()
            return row is not None

    def validate_state_scope_lease(self, claim_id: str, claimant_id: str) -> bool:
        if not claim_id.strip() or not claimant_id.strip():
            return False
        with self._lock:
            row = self.connection.execute(
                """SELECT 1 FROM merlin_storage.state_scope_leases
                   WHERE claim_id = %s AND claimant_id = %s AND expires_at > now()""",
                (claim_id, claimant_id.strip()),
            ).fetchone()
            return row is not None

    def release_state_scope_lease(self, claim_id: str, claimant_id: str) -> bool:
        if not claim_id.strip() or not claimant_id.strip():
            return False
        with self._lock, self.connection.transaction():
            row = self.connection.execute(
                """DELETE FROM merlin_storage.state_scope_leases
                   WHERE claim_id = %s AND claimant_id = %s RETURNING claim_id""",
                (claim_id, claimant_id.strip()),
            ).fetchone()
            return row is not None

    def reserve_resource(
        self,
        scope_id: str,
        reservation_id: str,
        *,
        target_authority: str,
        principal_key: str,
        resource_kind: str,
        amount: int,
        limit: int,
        window_key: str,
        wake_at: datetime | None = None,
        claim_id: str,
        claimant_id: str,
    ) -> dict[str, Any]:
        """Atomically reserve one principal-owned target resource unit."""

        authority = _required(target_authority, "target authority")
        principal = _required(principal_key, "principal key")
        resource = _required(resource_kind, "resource kind")
        window = _required(window_key, "window key")
        reservation = _required(reservation_id, "reservation id")
        if amount < 1 or limit < 1:
            raise ValueError("resource amount and limit must be positive")
        lock_key = json.dumps((authority, principal, resource, window), separators=(",", ":"))
        with self._lock, self.connection.transaction():
            self._require_state_scope_lease_locked(scope_id, claim_id, claimant_id)
            scope_status = self._validate_scope_resource_identity(scope_id, authority, principal)
            self.connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,)
            )
            self.connection.execute(
                """INSERT INTO merlin_storage.principal_resources
                       (target_authority, principal_key, resource_kind, window_key,
                        capacity, wake_at, state)
                   VALUES (%s, %s, %s, %s, %s, %s,
                           CASE WHEN %s::timestamptz IS NULL THEN 'active' ELSE 'suspended' END)
                   ON CONFLICT (target_authority, principal_key, resource_kind, window_key)
                   DO UPDATE SET capacity = LEAST(
                       merlin_storage.principal_resources.capacity, EXCLUDED.capacity
                   ), updated_at = now()""",
                (authority, principal, resource, window, limit, wake_at, wake_at),
            )
            self._reopen_due_resource_locked(authority, principal, resource, window)
            existing = self.connection.execute(
                "SELECT * FROM merlin_storage.resource_reservations WHERE id = %s",
                (reservation,),
            ).fetchone()
            if existing is not None:
                expected = (scope_id, authority, principal, resource, window, amount)
                actual = (
                    str(existing["scope_id"]),
                    str(existing["target_authority"]),
                    str(existing["principal_key"]),
                    str(existing["resource_kind"]),
                    str(existing["window_key"]),
                    int(existing["amount"]),
                )
                if actual != expected:
                    raise ValueError("resource reservation identity conflicts with durable state")
                return self._resource_decision(
                    existing, admitted=str(existing["state"]) == "reserved"
                )
            if scope_status != "open":
                raise ValueError("new resource reservations require a pre-delivery state scope")
            ledger = self.connection.execute(
                """SELECT * FROM merlin_storage.principal_resources
                   WHERE target_authority = %s AND principal_key = %s
                     AND resource_kind = %s AND window_key = %s FOR UPDATE""",
                (authority, principal, resource, window),
            ).fetchone()
            assert ledger is not None
            available = max(
                0,
                int(ledger["capacity"]) - int(ledger["reserved"]) - int(ledger["consumed"]),
            )
            if str(ledger["state"]) != "active" or amount > available:
                return {
                    "admitted": False,
                    "state": str(ledger["state"]),
                    "reservation_id": None,
                    "available": available,
                    "wake_at": ledger["wake_at"],
                }
            inserted = self.connection.execute(
                """INSERT INTO merlin_storage.resource_reservations
                       (id, scope_id, target_authority, principal_key, resource_kind,
                        window_key, amount)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (reservation, scope_id, authority, principal, resource, window, amount),
            ).fetchone()
            self.connection.execute(
                """UPDATE merlin_storage.principal_resources
                   SET reserved = reserved + %s, updated_at = now()
                   WHERE target_authority = %s AND principal_key = %s
                     AND resource_kind = %s AND window_key = %s""",
                (amount, authority, principal, resource, window),
            )
            assert inserted is not None
            return self._resource_decision(inserted, admitted=True, available=available - amount)

    def acknowledge_resource(
        self, reservation_id: str, *, claim_id: str, claimant_id: str
    ) -> dict[str, Any]:
        with self._lock, self.connection.transaction():
            owner = self.connection.execute(
                "SELECT scope_id FROM merlin_storage.resource_reservations WHERE id = %s",
                (reservation_id,),
            ).fetchone()
            if owner is None:
                raise KeyError(reservation_id)
            self._require_state_scope_lease_locked(str(owner["scope_id"]), claim_id, claimant_id)
            row = self.connection.execute(
                """UPDATE merlin_storage.resource_reservations
                   SET state = 'acknowledged', acknowledged_at = now(), updated_at = now()
                   WHERE id = %s AND state = 'reserved' RETURNING *""",
                (reservation_id,),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    "SELECT * FROM merlin_storage.resource_reservations WHERE id = %s",
                    (reservation_id,),
                ).fetchone()
            if row is None:
                raise KeyError(reservation_id)
            if str(row["state"]) not in {"acknowledged", "settled"}:
                raise ValueError("resource reservation cannot acknowledge target delivery")
            return dict(row)

    def settle_resource(
        self,
        reservation_id: str,
        *,
        actual_amount: int,
        terminal_block: bool = False,
        wake_at: datetime | None = None,
        evidence: Mapping[str, object] | None = None,
        claim_id: str | None,
        claimant_id: str,
    ) -> dict[str, Any]:
        """Settle acknowledged use and optionally suspend the shared resource."""

        if actual_amount < 0:
            raise ValueError("actual resource use cannot be negative")
        if terminal_block and wake_at is None:
            raise ValueError("a terminal resource block requires a wake time")
        with self._lock, self.connection.transaction():
            reservation = self.connection.execute(
                """SELECT * FROM merlin_storage.resource_reservations
                   WHERE id = %s FOR UPDATE""",
                (reservation_id,),
            ).fetchone()
            if reservation is None:
                raise KeyError(reservation_id)
            self._require_state_scope_lease_locked(
                str(reservation["scope_id"]), claim_id, claimant_id
            )
            if str(reservation["state"]) == "settled":
                if int(reservation["actual_amount"]) != actual_amount:
                    raise ValueError("resource reservation was already settled differently")
                return dict(reservation)
            if str(reservation["state"]) != "acknowledged":
                raise ValueError("only acknowledged target use can be settled")
            key = (
                reservation["target_authority"],
                reservation["principal_key"],
                reservation["resource_kind"],
                reservation["window_key"],
            )
            ledger = self.connection.execute(
                """SELECT * FROM merlin_storage.principal_resources
                   WHERE target_authority = %s AND principal_key = %s
                     AND resource_kind = %s AND window_key = %s FOR UPDATE""",
                key,
            ).fetchone()
            assert ledger is not None
            new_consumed = int(ledger["consumed"]) + actual_amount
            blocked = terminal_block or new_consumed >= int(ledger["capacity"])
            next_state = (
                "suspended"
                if blocked and wake_at is not None
                else ("exhausted" if blocked else str(ledger["state"]))
            )
            self.connection.execute(
                """UPDATE merlin_storage.principal_resources
                   SET reserved = greatest(0, reserved - %s), consumed = %s,
                       state = %s, wake_at = %s,
                       evidence = CASE WHEN %s::jsonb IS NULL THEN evidence ELSE %s::jsonb END,
                       updated_at = now()
                   WHERE target_authority = %s AND principal_key = %s
                     AND resource_kind = %s AND window_key = %s""",
                (
                    int(reservation["amount"]),
                    new_consumed,
                    next_state,
                    wake_at if blocked else ledger["wake_at"],
                    None if evidence is None else Jsonb(dict(evidence)),
                    None if evidence is None else Jsonb(dict(evidence)),
                    *key,
                ),
            )
            settled = self.connection.execute(
                """UPDATE merlin_storage.resource_reservations
                   SET state = 'settled', actual_amount = %s, settled_at = now(),
                       updated_at = now() WHERE id = %s RETURNING *""",
                (actual_amount, reservation_id),
            ).fetchone()
            assert settled is not None
            return dict(settled)

    def release_resource(
        self, reservation_id: str, *, claim_id: str, claimant_id: str
    ) -> dict[str, Any]:
        """Release only work that never reached the target."""

        with self._lock, self.connection.transaction():
            reservation = self.connection.execute(
                """SELECT * FROM merlin_storage.resource_reservations
                   WHERE id = %s FOR UPDATE""",
                (reservation_id,),
            ).fetchone()
            if reservation is None:
                raise KeyError(reservation_id)
            self._require_state_scope_lease_locked(
                str(reservation["scope_id"]), claim_id, claimant_id
            )
            if str(reservation["state"]) == "released":
                return dict(reservation)
            if str(reservation["state"]) != "reserved":
                raise ValueError("acknowledged target use must settle and cannot be released")
            key = (
                reservation["target_authority"],
                reservation["principal_key"],
                reservation["resource_kind"],
                reservation["window_key"],
            )
            self.connection.execute(
                """UPDATE merlin_storage.principal_resources
                   SET reserved = greatest(0, reserved - %s), updated_at = now()
                   WHERE target_authority = %s AND principal_key = %s
                     AND resource_kind = %s AND window_key = %s""",
                (int(reservation["amount"]), *key),
            )
            released = self.connection.execute(
                """UPDATE merlin_storage.resource_reservations
                   SET state = 'released', settled_at = now(), updated_at = now()
                   WHERE id = %s RETURNING *""",
                (reservation_id,),
            ).fetchone()
            assert released is not None
            return dict(released)

    def remaining_campaign_resource_budget(
        self,
        campaign_id: str,
        target_authority: str,
        resources: Sequence[Mapping[str, object]],
    ) -> int | None:
        """Return candidate capacity from one proven principal's shared ledgers.

        ``None`` means the campaign has not established one unambiguous reviewed
        principal yet. Callers must serialize that discovery rather than treating
        a declared limit as independent capacity.
        """

        campaign = _required(campaign_id, "campaign id")
        authority = _required(target_authority, "target authority")
        grouped: dict[tuple[str, str], dict[str, int]] = {}
        for resource in resources:
            kind = _required(str(resource.get("resource_kind") or ""), "resource kind")
            window = _required(str(resource.get("window_key") or ""), "window key")
            amount = resource.get("reservation_amount")
            limit = resource.get("limit")
            if (
                isinstance(amount, bool)
                or not isinstance(amount, int)
                or amount < 1
                or isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit < 1
            ):
                raise ValueError("resource batch budgets require positive amounts and limits")
            aggregate = grouped.setdefault((kind, window), {"amount": 0, "limit": limit})
            aggregate["amount"] += amount
            aggregate["limit"] = min(aggregate["limit"], limit)
        if not grouped:
            return None

        with self._lock, self.connection.transaction():
            principals = self.connection.execute(
                """SELECT DISTINCT principal_key
                   FROM merlin_storage.state_scopes
                   WHERE campaign_id = %s AND target_authority = %s
                     AND principal_key IS NOT NULL
                   ORDER BY principal_key""",
                (campaign, authority),
            ).fetchall()
            if len(principals) != 1:
                return None
            principal = str(principals[0]["principal_key"] or "")
            if not principal:
                return None

            candidate_budgets: list[int] = []
            for (kind, window), contract in grouped.items():
                self._reopen_due_resource_locked(authority, principal, kind, window)
                row = self.connection.execute(
                    """SELECT capacity, reserved, consumed, state
                       FROM merlin_storage.principal_resources
                       WHERE target_authority = %s AND principal_key = %s
                         AND resource_kind = %s AND window_key = %s
                       FOR SHARE""",
                    (authority, principal, kind, window),
                ).fetchone()
                if row is None:
                    available = contract["limit"]
                elif str(row["state"]) != "active":
                    available = 0
                else:
                    available = max(
                        0,
                        min(contract["limit"], int(row["capacity"]))
                        - int(row["reserved"])
                        - int(row["consumed"]),
                    )
                candidate_budgets.append(available // contract["amount"])
            return min(candidate_budgets)

    def resource_status(
        self,
        target_authority: str,
        principal_key: str,
        resource_kind: str,
        window_key: str,
    ) -> dict[str, Any] | None:
        with self._lock, self.connection.transaction():
            self._reopen_due_resource_locked(
                target_authority, principal_key, resource_kind, window_key
            )
            row = self.connection.execute(
                """SELECT * FROM merlin_storage.principal_resources
                   WHERE target_authority = %s AND principal_key = %s
                     AND resource_kind = %s AND window_key = %s""",
                (target_authority, principal_key, resource_kind, window_key),
            ).fetchone()
            return None if row is None else dict(row)

    def reconcile_durable_lifecycle(
        self,
        *,
        claimant_id: str,
        readable_formats: Sequence[str],
        writable_format: str,
        lease_seconds: int,
        exclude_campaign_ids: Sequence[str] = (),
        include_campaign_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Atomically reconcile global registries and classify campaign recovery."""

        _required(claimant_id, "claimant id")
        readable = tuple(
            dict.fromkeys(_required(item, "readable format") for item in readable_formats)
        )
        writable = _required(writable_format, "writable format")
        excluded = frozenset(
            _required(campaign_id, "excluded campaign id") for campaign_id in exclude_campaign_ids
        )
        included = tuple(
            dict.fromkeys(
                _required(campaign_id, "included campaign id")
                for campaign_id in include_campaign_ids
            )
        )
        if excluded.intersection(included):
            raise ValueError("lifecycle reconciliation campaign filters overlap")
        if lease_seconds < 1 or not readable:
            raise ValueError("lifecycle reconciliation requires formats and a positive lease")
        with self._lock, self.connection.transaction():
            self.connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("merlin:lifecycle-recovery:v1",),
            )
            expired_state = self.connection.execute(
                """WITH expired AS (
                       DELETE FROM merlin_storage.state_scope_leases
                       WHERE expires_at <= now() RETURNING 1
                   ) SELECT count(*) AS count FROM expired"""
            ).fetchone()
            orphaned_state = self.connection.execute(
                """WITH orphaned AS (
                       DELETE FROM merlin_storage.state_scope_leases AS lease
                       USING merlin_storage.state_scopes AS scope,
                             merlin_storage.scheduler_attempts AS attempt
                       WHERE lease.scope_id = scope.id
                         AND scope.attempt_id =
                           regexp_replace(attempt.node_id, '^worker:', '')
                           || '/attempt/' || attempt.id::text
                         AND attempt.state <> 'running'
                       RETURNING 1
                   ) SELECT count(*) AS count FROM orphaned"""
            ).fetchone()
            expired_target = self.connection.execute(
                """WITH expired AS (
                       DELETE FROM merlin_storage.target_execution_leases
                       WHERE expires_at <= now() RETURNING 1
                   ) SELECT count(*) AS count FROM expired"""
            ).fetchone()
            orphaned_target = self.connection.execute(
                """WITH orphaned AS (
                       DELETE FROM merlin_storage.target_execution_leases AS lease
                       USING merlin_storage.scheduler_attempts AS attempt
                       WHERE lease.session_scope_id =
                           regexp_replace(attempt.node_id, '^worker:', '')
                           || '/attempt/' || attempt.id::text
                         AND attempt.state <> 'running'
                       RETURNING 1
                   ) SELECT count(*) AS count FROM orphaned"""
            ).fetchone()
            due = self.connection.execute(
                """SELECT target_authority, principal_key, resource_kind, window_key
                   FROM merlin_storage.principal_resources
                   WHERE state = 'suspended' AND wake_at <= now() FOR UPDATE"""
            ).fetchall()
            due_wakes = 0
            for item in due:
                due_wakes += int(
                    self._reopen_due_resource_locked(
                        str(item["target_authority"]),
                        str(item["principal_key"]),
                        str(item["resource_kind"]),
                        str(item["window_key"]),
                    )
                )
            campaign_filters = ["status IN ('prepared', 'executing', 'paused')"]
            campaign_filter_values: list[object] = []
            if included:
                campaign_filters.append("id = ANY(%s)")
                campaign_filter_values.append(list(included))
            if excluded:
                campaign_filters.append("NOT (id = ANY(%s))")
                campaign_filter_values.append(list(excluded))
            campaigns = self.connection.execute(
                """SELECT id, session_id, manifest, status, execution_format,
                          lifecycle_pause_reason
                   FROM merlin_storage.assessment_campaigns
                   WHERE """
                + " AND ".join(campaign_filters)
                + " ORDER BY id FOR UPDATE",
                tuple(campaign_filter_values),
            ).fetchall()
            results: list[dict[str, Any]] = []
            for campaign in campaigns:
                campaign_id = str(campaign["id"])
                recovered_acknowledged = self._settle_orphaned_acknowledged_scopes_locked(
                    campaign_id
                )
                counts = self.connection.execute(
                    """SELECT count(*) FILTER (WHERE status = 'open') AS open_count,
                              count(*) FILTER (WHERE status = 'effect_acknowledged')
                                  AS acknowledged_count,
                              count(*) FILTER (WHERE effect_acknowledged_at IS NOT NULL)
                                  AS target_call_count
                       FROM merlin_storage.state_scopes WHERE campaign_id = %s""",
                    (campaign_id,),
                ).fetchone()
                lane_counts = self.connection.execute(
                    """SELECT count(*) AS lane_count,
                              count(*) FILTER (
                                  WHERE status = 'reobservation_required'
                              ) AS reobservation_count,
                              count(*) FILTER (
                                  WHERE last_transition = 'unattributed'
                              ) AS unattributed_count
                       FROM merlin_storage.state_lanes WHERE campaign_id = %s""",
                    (campaign_id,),
                ).fetchone()
                cleanup = self.connection.execute(
                    """SELECT count(*) AS count
                       FROM merlin_storage.state_scopes
                           WHERE campaign_id = %s
                         AND cleanup_disposition IN ('pending', 'failed')""",
                    (campaign_id,),
                ).fetchone()
                leases = self.connection.execute(
                    """SELECT count(*) AS count
                       FROM merlin_storage.state_scope_leases AS lease
                       JOIN merlin_storage.state_scopes AS scope ON scope.id = lease.scope_id
                       WHERE scope.campaign_id = %s AND lease.expires_at > now()""",
                    (campaign_id,),
                ).fetchone()
                quota = self.connection.execute(
                    """SELECT count(*) AS blocked_count,
                              count(*) FILTER (WHERE resource.wake_at IS NULL)
                                  AS indefinite_count,
                              min(resource.wake_at) AS wake_at
                       FROM merlin_storage.principal_resources AS resource
                       WHERE resource.state IN ('suspended', 'exhausted')
                         AND EXISTS (
                             SELECT 1 FROM merlin_storage.state_scopes AS scope
                             WHERE scope.campaign_id = %s
                               AND scope.target_authority = resource.target_authority
                               AND coalesce(scope.principal_key, 'anonymous') =
                                   resource.principal_key
                         )""",
                    (campaign_id,),
                ).fetchone()
                execution_format = str(campaign["execution_format"])
                acknowledged_count = int(counts["acknowledged_count"] or 0)
                cleanup_count = int(cleanup["count"] or 0)
                blocked_count = int(quota["blocked_count"] or 0)
                indefinite_count = int(quota["indefinite_count"] or 0)
                wake = None if quota is None else quota["wake_at"]
                if campaign["lifecycle_pause_reason"] == "recovery_incompatible":
                    decision = "pause_fixed_forward"
                    pause_reason = "recovery_incompatible"
                elif execution_format not in readable or (
                    execution_format == STATE_SCOPE_SCHEMA_VERSION and writable != execution_format
                ):
                    decision = "pause_fixed_forward"
                    pause_reason = "fixed_forward"
                elif acknowledged_count:
                    decision = "settle_acknowledged"
                    pause_reason = "acknowledged_settlement"
                elif cleanup_count:
                    decision = "cleanup_required"
                    pause_reason = "cleanup"
                elif blocked_count:
                    decision = "wait_quota"
                    pause_reason = "quota"
                else:
                    decision = "resume"
                    pause_reason = None
                if pause_reason is not None:
                    self.connection.execute(
                        """UPDATE merlin_storage.assessment_campaigns
                           SET status = 'paused', lifecycle_pause_reason = %s,
                               lifecycle_resume_status = coalesce(
                                   lifecycle_resume_status, status
                               ),
                               lifecycle_wake_at = CASE WHEN %s = 'quota'
                                   THEN %s::timestamptz ELSE NULL END,
                               updated_at = now() WHERE id = %s""",
                        (pause_reason, pause_reason, wake, campaign_id),
                    )
                    resulting_status = "paused"
                elif (
                    str(campaign["status"]) == "paused"
                    and campaign["lifecycle_pause_reason"] is not None
                ):
                    resumed = self.connection.execute(
                        """UPDATE merlin_storage.assessment_campaigns
                           SET status = lifecycle_resume_status,
                               lifecycle_pause_reason = NULL,
                               lifecycle_resume_status = NULL, lifecycle_wake_at = NULL,
                               updated_at = now() WHERE id = %s RETURNING status""",
                        (campaign_id,),
                    ).fetchone()
                    assert resumed is not None
                    resulting_status = str(resumed["status"])
                else:
                    resulting_status = str(campaign["status"])
                # Lock the frontier transition fence before taking a topology
                # snapshot. Claim/bind/settle paths validate this row under a
                # shared lock, while a new claim first locks the campaign row
                # already held by this transaction.
                transition = self.connection.execute(
                    """SELECT claim_id, claimant_id, expires_at
                       FROM merlin_storage.campaign_frontier_transition_leases
                       WHERE campaign_id = %s FOR UPDATE""",
                    (campaign_id,),
                ).fetchone()
                latest_wave = self.connection.execute(
                    """SELECT wave.wave, wave.kind, wave.schedule_id,
                              wave.status AS wave_status,
                              schedule.status AS schedule_status,
                              schedule.graph_digest, schedule.checkpoint_digest
                       FROM merlin_storage.campaign_waves AS wave
                       JOIN merlin_storage.scheduler_schedules AS schedule
                         ON schedule.id = wave.schedule_id
                       WHERE wave.campaign_id = %s
                       ORDER BY wave.wave DESC LIMIT 1
                       FOR UPDATE OF wave, schedule""",
                    (campaign_id,),
                ).fetchone()
                active_batch = self.connection.execute(
                    """SELECT id, wave, state, schedule_id, candidate_digest
                       FROM merlin_storage.campaign_frontier_batches
                       WHERE campaign_id = %s AND state IN ('claimed', 'scheduled')
                       FOR UPDATE""",
                    (campaign_id,),
                ).fetchone()
                active_schedule = None
                if active_batch is not None:
                    expected_schedule_id = str(
                        active_batch["schedule_id"]
                        or uuid5(
                            NAMESPACE_URL,
                            f"merlin.campaign-wave.v1\n{campaign_id}\n{int(active_batch['wave'])}",
                        )
                    )
                    active_schedule = self.connection.execute(
                        """SELECT id, status, graph_digest, checkpoint_digest
                           FROM merlin_storage.scheduler_schedules
                           WHERE id = %s FOR UPDATE""",
                        (expected_schedule_id,),
                    ).fetchone()
                frontier_rows = self.connection.execute(
                    """SELECT state, count(*) AS count
                       FROM merlin_storage.campaign_frontier_candidates
                       WHERE campaign_id = %s GROUP BY state""",
                    (campaign_id,),
                ).fetchall()
                frontier_counts = {
                    state: 0 for state in ("dormant", "queued", "claimed", "scheduled", "terminal")
                }
                frontier_counts.update(
                    {str(item["state"]): int(item["count"]) for item in frontier_rows}
                )
                reobservation_rows = self.connection.execute(
                    """SELECT sequence, wave, decision_id, payload
                       FROM merlin_storage.campaign_lead_journal
                       WHERE campaign_id = %s
                         AND (
                           payload->>'kind' IN (
                             'target_surface_transition_observed',
                             'target_reobservation_started',
                             'target_reobservation_blocked',
                             'target_reobservation_retry_authorized',
                             'target_reobservation_settled'
                           )
                           OR payload ? 'target_reobservation_generations'
                         )
                       ORDER BY sequence FOR UPDATE""",
                    (campaign_id,),
                ).fetchall()
                target_id = _campaign_target_id(campaign)
                reobservation = (
                    project_target_reobservations(
                        tuple(dict(item) for item in reobservation_rows),
                        campaign_id=campaign_id,
                        session_id=str(campaign["session_id"]),
                        target_id=target_id,
                    )
                    if target_id is not None
                    else project_target_reobservations(())
                )
                reobservation_schedule_ids = tuple(
                    sorted({item for values in reobservation.starts.values() for item in values})
                )
                reobservation_schedule_rows = (
                    self.connection.execute(
                        """SELECT id, status FROM merlin_storage.scheduler_schedules
                           WHERE id = ANY(%s::uuid[]) ORDER BY id FOR UPDATE""",
                        (list(reobservation_schedule_ids),),
                    ).fetchall()
                    if reobservation_schedule_ids
                    else ()
                )
                pending_event_ids = tuple(item.event_digest for item in reobservation.pending)
                latest_wave_number = None if latest_wave is None else int(latest_wave["wave"])
                reobservation_recovery_state = reobservation.recovery_state(
                    latest_wave=latest_wave_number
                )
                pending_review_bindings = reobservation.lead_review_bindings()
                active_reobservation_ids = tuple(
                    sorted(
                        {
                            schedule_id
                            for generation_id in reobservation.generations
                            if (schedule_id := reobservation.active_schedule(generation_id))
                            is not None
                        }
                    )
                )
                target_reobservation = {
                    "pending": {
                        "count": len(pending_event_ids),
                        "digest": _digest({"event_digests": pending_event_ids}),
                        "event_digests": pending_event_ids[-16:],
                    },
                    "review_pending": {
                        "count": len(pending_review_bindings),
                        "digest": _digest({"bindings": pending_review_bindings}),
                        "bindings": pending_review_bindings[-16:],
                    },
                    "schedules": {
                        "count": len(reobservation_schedule_rows),
                        "digest": _digest(
                            {
                                "schedules": tuple(
                                    (str(item["id"]), str(item["status"]))
                                    for item in reobservation_schedule_rows
                                )
                            }
                        ),
                        "statuses": tuple(
                            {
                                "schedule_id": str(item["id"]),
                                "status": str(item["status"]),
                            }
                            for item in reobservation_schedule_rows[-16:]
                        ),
                    },
                    "active_schedule_ids": active_reobservation_ids[-16:],
                    "recovery_state": reobservation_recovery_state,
                }
                reservation_counts = self.connection.execute(
                    """SELECT reservation.state, count(*) AS count
                       FROM merlin_storage.resource_reservations AS reservation
                       JOIN merlin_storage.state_scopes AS scope
                         ON scope.id = reservation.scope_id
                       WHERE scope.campaign_id = %s GROUP BY reservation.state""",
                    (campaign_id,),
                ).fetchall()
                reservations = {
                    state: 0 for state in ("reserved", "acknowledged", "settled", "released")
                }
                reservations.update(
                    {str(item["state"]): int(item["count"]) for item in reservation_counts}
                )
                wave_projection = (
                    None
                    if latest_wave is None
                    else {
                        "wave": int(latest_wave["wave"]),
                        "kind": str(latest_wave["kind"]),
                        "schedule_id": str(latest_wave["schedule_id"]),
                        "wave_status": str(latest_wave["wave_status"]),
                        "schedule_status": str(latest_wave["schedule_status"]),
                        "graph_digest": str(latest_wave["graph_digest"]),
                        "checkpoint_digest": str(latest_wave["checkpoint_digest"]),
                    }
                )
                batch_projection = (
                    None
                    if active_batch is None
                    else {
                        "id": str(active_batch["id"]),
                        "wave": int(active_batch["wave"]),
                        "state": str(active_batch["state"]),
                        "schedule_id": (
                            None
                            if active_batch["schedule_id"] is None
                            else str(active_batch["schedule_id"])
                        ),
                        "candidate_digest": str(active_batch["candidate_digest"]),
                        "schedule_status": (
                            None if active_schedule is None else str(active_schedule["status"])
                        ),
                        "graph_digest": (
                            None
                            if active_schedule is None
                            else str(active_schedule["graph_digest"])
                        ),
                        "checkpoint_digest": (
                            None
                            if active_schedule is None
                            else str(active_schedule["checkpoint_digest"])
                        ),
                    }
                )
                scheduler_action = _scheduler_recovery_action(
                    resulting_status,
                    wave_projection,
                    batch_projection,
                    frontier_counts,
                )
                scheduler_action = _target_reobservation_recovery_action(
                    resulting_status,
                    scheduler_action,
                    target_reobservation,
                )
                recovery_action = "pause_lifecycle" if decision != "resume" else scheduler_action
                topology = {
                    # The derived action policy evolves independently from the durable
                    # topology. Version it inside the state identity so a newer policy
                    # never collides with an immutable decision recorded by older code.
                    "recovery_policy_version": _RECOVERY_POLICY_VERSION,
                    "campaign_status": resulting_status,
                    "lifecycle_decision": decision,
                    "lifecycle_wake_at": (None if wake is None else wake.isoformat()),
                    "latest_wave": wave_projection,
                    "active_batch": batch_projection,
                    "frontier_counts": frontier_counts,
                    "state_scopes": {
                        "open": int(counts["open_count"] or 0),
                        "acknowledged": acknowledged_count,
                        "target_calls": int(counts["target_call_count"] or 0),
                    },
                    "state_lanes": {
                        "total": int(lane_counts["lane_count"] or 0),
                        "reobservation_required": int(lane_counts["reobservation_count"] or 0),
                        "unattributed_transitions": int(lane_counts["unattributed_count"] or 0),
                    },
                    "resource_reservations": reservations,
                    "target_reobservation": target_reobservation,
                    "frontier_transition": (
                        None
                        if transition is None
                        else {
                            "claim_id": str(transition["claim_id"]),
                            "claimant_id": str(transition["claimant_id"]),
                            "live": transition["expires_at"]
                            > self.connection.execute("SELECT now() AS value").fetchone()["value"],
                        }
                    ),
                }
                recovery_state_digest = _digest(topology)
                recovery_decision = {
                    "schema": "merlin.campaign-recovery.v1",
                    "action": recovery_action,
                    "scheduler_action": scheduler_action,
                    "lifecycle_decision": decision,
                    "topology": topology,
                }
                recovery_decision_digest = _digest(recovery_decision)
                persisted_recovery = self.connection.execute(
                    """INSERT INTO merlin_storage.campaign_recovery
                           (campaign_id, state_digest, decision, decision_digest)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (campaign_id, state_digest) DO UPDATE
                       SET last_observed_at = now()
                       RETURNING decision_digest""",
                    (
                        campaign_id,
                        recovery_state_digest,
                        Jsonb(recovery_decision),
                        recovery_decision_digest,
                    ),
                ).fetchone()
                if (
                    persisted_recovery is None
                    or str(persisted_recovery["decision_digest"]) != recovery_decision_digest
                ):
                    raise ValueError("campaign recovery decision conflicts with durable state")
                from .campaign_recovery_store import recovery_fence

                fence_digest = recovery_fence(self.connection, campaign_id)
                self.connection.execute(
                    """UPDATE merlin_storage.campaign_recovery
                       SET fence_digest = %s,
                           application_state = 'planned', claim_id = NULL,
                           claimant_id = NULL, claim_expires_at = NULL,
                           applied_at = NULL, application_error = NULL
                       WHERE campaign_id = %s AND state_digest = %s
                         AND (application_state <> 'claimed' OR claim_expires_at <= now())""",
                    (
                        fence_digest,
                        campaign_id,
                        recovery_state_digest,
                    ),
                )
                results.append(
                    {
                        "campaign_id": campaign_id,
                        "execution_format": execution_format,
                        "status": resulting_status,
                        "decision": decision,
                        "wake_at": wake,
                        "quota_indefinite": bool(indefinite_count),
                        "open_state_scopes": int(counts["open_count"] or 0),
                        "acknowledged_state_scopes": acknowledged_count,
                        "cleanup_obligations": cleanup_count,
                        "active_global_leases": int(leases["count"] or 0),
                        "recovery_action": recovery_action,
                        "scheduler_action": scheduler_action,
                        "recovery_state_digest": recovery_state_digest,
                        "recovery_fence_digest": fence_digest,
                        "recovery_decision_digest": recovery_decision_digest,
                        "target_call_count": int(counts["target_call_count"] or 0),
                        "target_reobservation_recovery_state": (reobservation_recovery_state),
                        "state_lanes_requiring_reobservation": int(
                            lane_counts["reobservation_count"] or 0
                        ),
                        "unattributed_state_transitions": int(
                            lane_counts["unattributed_count"] or 0
                        ),
                        "settled_acknowledged_state_scopes": recovered_acknowledged,
                        "resource_reservations": reservations,
                        "target_reobservation": target_reobservation,
                        "latest_wave": wave_projection,
                        "active_frontier_batch": batch_projection,
                        "frontier_counts": frontier_counts,
                    }
                )
            return {
                "schema": "merlin.durable-lifecycle-recovery.v1",
                "reconciled_at": self.connection.execute("SELECT now() AS value").fetchone()[
                    "value"
                ],
                "expired_global_leases": int(expired_state["count"] or 0)
                + int(orphaned_state["count"] or 0)
                + int(expired_target["count"] or 0)
                + int(orphaned_target["count"] or 0),
                "due_quota_wakes": due_wakes,
                "campaigns": results,
            }

    def _settle_orphaned_acknowledged_scopes_locked(self, campaign_id: str) -> int:
        """Settle delivered actions whose execution lease no longer has an owner.

        Recovery must never replay an acknowledged action. When a process dies
        between delivery and response capture, the exact outcome is unknowable;
        persist that uncertainty once and let the campaign continue with a new
        action identity.
        """

        rows = self.connection.execute(
            """SELECT scope.*, envelope.envelope
               FROM merlin_storage.state_scopes AS scope
               JOIN merlin_storage.scope_envelopes AS envelope
                 ON envelope.scope_id = scope.id
                AND envelope.revision = scope.current_revision
               LEFT JOIN merlin_storage.state_scope_leases AS lease
                 ON lease.scope_id = scope.id AND lease.expires_at > now()
               WHERE scope.campaign_id = %s
                 AND scope.status = 'effect_acknowledged'
                 AND lease.scope_id IS NULL
               ORDER BY scope.id FOR UPDATE OF scope, envelope""",
            (campaign_id,),
        ).fetchall()
        for row in rows:
            scope_id = str(row["id"])
            current_revision = int(row["current_revision"])
            document = StateScopeEnvelope.model_validate(row["envelope"]).model_dump(mode="json")
            document["revision"] = current_revision + 1
            missing = {
                "availability": "missing",
                "evidence_refs": [],
                "item_count": 0,
                "missing": {
                    "reason": "capture_incomplete",
                    "retryable": False,
                    "code": "orphaned_after_delivery",
                },
            }
            if document["responses"]["availability"] != "present":
                document["responses"] = missing
            if document["after"]["availability"] != "present":
                document["after"] = missing
            terminal = StateScopeEnvelope.model_validate(document)
            terminal_document = terminal.model_dump(mode="json")
            self.connection.execute(
                """INSERT INTO merlin_storage.scope_envelopes
                       (scope_id, revision, envelope, envelope_digest)
                   VALUES (%s, %s, %s, %s)""",
                (
                    scope_id,
                    terminal.revision,
                    Jsonb(terminal_document),
                    _digest(terminal_document),
                ),
            )
            reservations = self.connection.execute(
                """SELECT * FROM merlin_storage.resource_reservations
                   WHERE scope_id = %s AND state IN ('reserved', 'acknowledged')
                   FOR UPDATE""",
                (scope_id,),
            ).fetchall()
            for reservation in reservations:
                key = (
                    reservation["target_authority"],
                    reservation["principal_key"],
                    reservation["resource_kind"],
                    reservation["window_key"],
                )
                ledger = self.connection.execute(
                    """SELECT * FROM merlin_storage.principal_resources
                       WHERE target_authority = %s AND principal_key = %s
                         AND resource_kind = %s AND window_key = %s FOR UPDATE""",
                    key,
                ).fetchone()
                if ledger is not None:
                    amount = int(reservation["amount"])
                    consumed = int(ledger["consumed"]) + amount
                    state = (
                        "exhausted" if consumed >= int(ledger["capacity"]) else str(ledger["state"])
                    )
                    self.connection.execute(
                        """UPDATE merlin_storage.principal_resources
                           SET reserved = greatest(0, reserved - %s), consumed = %s,
                               state = %s, updated_at = now()
                           WHERE target_authority = %s AND principal_key = %s
                             AND resource_kind = %s AND window_key = %s""",
                        (amount, consumed, state, *key),
                    )
                    self.connection.execute(
                        """UPDATE merlin_storage.resource_reservations
                           SET state = 'settled', actual_amount = %s,
                               acknowledged_at = coalesce(acknowledged_at, now()),
                               settled_at = now(), updated_at = now()
                           WHERE id = %s""",
                        (amount, reservation["id"]),
                    )
            self.connection.execute(
                """UPDATE merlin_storage.state_scopes
                   SET status = 'settled', current_revision = %s,
                       transition_disposition = 'infrastructure_failure',
                       cleanup_disposition = 'retained', settled_at = now(),
                       updated_at = now() WHERE id = %s""",
                (terminal.revision, scope_id),
            )
            self._require_state_lane_reobservation_locked(scope_id)
        return len(rows)

    def _require_state_lane_reobservation_locked(self, scope_id: str) -> None:
        """Fence attribution after restart loses the authoritative after-state."""

        scope = self.connection.execute(
            """SELECT lane_id, target_authority, principal_key, physical_state_key
               FROM merlin_storage.state_scopes WHERE id = %s""",
            (scope_id,),
        ).fetchone()
        if scope is None or scope["lane_id"] is None:
            return
        lane = self.connection.execute(
            "SELECT * FROM merlin_storage.state_lanes WHERE id = %s FOR UPDATE",
            (scope["lane_id"],),
        ).fetchone()
        if lane is None:
            raise RuntimeError("state lane disappeared during restart recovery")
        authority = str(scope["target_authority"])
        principal_key = scope["principal_key"] or lane["principal_key"]
        physical_state_key = scope["physical_state_key"] or lane["physical_state_key"]
        conservative_lane_id = _conservative_physical_lane_id(authority)
        conservative_lane = self.connection.execute(
            """SELECT * FROM merlin_storage.state_physical_lanes
               WHERE id = %s FOR UPDATE""",
            (conservative_lane_id,),
        ).fetchone()
        if conservative_lane is None:
            raise RuntimeError("conservative physical state lane disappeared during recovery")
        known_physical_state = physical_state_key is not None
        next_conservative_revision = int(conservative_lane["revision"]) + 1
        next_ambiguity_revision = int(conservative_lane["ambiguity_revision"]) + int(
            not known_physical_state
        )
        self.connection.execute(
            """UPDATE merlin_storage.state_physical_lanes
               SET revision = %s, ambiguity_revision = %s,
                   latest_experiment_id = %s, latest_scope_id = %s,
                   latest_observation_digest = NULL, updated_at = now()
               WHERE id = %s""",
            (
                next_conservative_revision,
                next_ambiguity_revision,
                str(lane["experiment_id"]),
                scope_id,
                conservative_lane_id,
            ),
        )
        if known_physical_state:
            aggregate_principal = str(principal_key or "anonymous")
            physical_lane_id = _physical_lane_id(
                authority, aggregate_principal, str(physical_state_key)
            )
            self.connection.execute(
                """INSERT INTO merlin_storage.state_physical_lanes
                       (id, target_authority, principal_key, physical_state_key)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (target_authority, principal_key, physical_state_key)
                   DO NOTHING""",
                (physical_lane_id, authority, aggregate_principal, physical_state_key),
            )
            physical_lane = self.connection.execute(
                """SELECT revision FROM merlin_storage.state_physical_lanes
                   WHERE id = %s FOR UPDATE""",
                (physical_lane_id,),
            ).fetchone()
            if physical_lane is None:
                raise RuntimeError("physical state lane disappeared during recovery")
            next_physical_revision = int(physical_lane["revision"]) + 1
            self.connection.execute(
                """UPDATE merlin_storage.state_physical_lanes
                   SET revision = %s, latest_experiment_id = %s, latest_scope_id = %s,
                       latest_observation_digest = NULL, updated_at = now()
                   WHERE id = %s""",
                (
                    next_physical_revision,
                    str(lane["experiment_id"]),
                    scope_id,
                    physical_lane_id,
                ),
            )
        else:
            physical_lane_id = conservative_lane_id
            next_physical_revision = next_conservative_revision
        next_revision = int(lane["revision"]) + 1
        self.connection.execute(
            """INSERT INTO merlin_storage.state_lane_revisions
                   (lane_id, revision, scope_id, transition, principal_key,
                    physical_state_key, observation_digest)
               VALUES (%s, %s, %s, 'unattributed', %s, %s, NULL)""",
            (
                str(lane["id"]),
                next_revision,
                scope_id,
                lane["principal_key"],
                lane["physical_state_key"],
            ),
        )
        self.connection.execute(
            """UPDATE merlin_storage.state_lanes
               SET revision = %s, status = 'reobservation_required',
                   last_transition = 'unattributed', latest_observation_digest = NULL,
                   principal_key = %s, physical_state_key = %s,
                   physical_lane_id = %s, physical_lane_revision = %s,
                   conservative_lane_revision = %s,
                   conservative_ambiguity_revision = %s,
                   latest_scope_id = %s, updated_at = now()
               WHERE id = %s""",
            (
                next_revision,
                principal_key,
                physical_state_key,
                physical_lane_id,
                next_physical_revision,
                next_conservative_revision,
                next_ambiguity_revision,
                scope_id,
                str(lane["id"]),
            ),
        )

    def _advance_state_lane_locked(
        self,
        scope_id: str,
        envelope: StateScopeEnvelope,
    ) -> dict[str, Any] | None:
        """Advance the experiment lane exactly once after attributed settlement."""

        scope = self.connection.execute(
            """SELECT lane_id, expected_lane_revision, action_kind, experiment_id,
                      target_authority, expected_physical_lane_id,
                      expected_physical_lane_revision,
                      expected_conservative_lane_revision,
                      expected_conservative_ambiguity_revision
               FROM merlin_storage.state_scopes WHERE id = %s FOR UPDATE""",
            (scope_id,),
        ).fetchone()
        if scope is None or scope["lane_id"] is None:
            return None
        row = self.connection.execute(
            "SELECT * FROM merlin_storage.state_lanes WHERE id = %s FOR UPDATE",
            (scope["lane_id"],),
        ).fetchone()
        if row is None:
            raise RuntimeError("state lane disappeared before settlement")
        expected = int(scope["expected_lane_revision"])
        current = int(row["revision"])
        if current != expected:
            raise ValueError("state lane revision changed before settlement")
        principal_key = _identity_key(envelope.principal_identity)
        physical_key = _identity_key(envelope.physical_state_identity)
        stored_principal = None if row["principal_key"] is None else str(row["principal_key"])
        stored_physical = (
            None if row["physical_state_key"] is None else str(row["physical_state_key"])
        )
        identity_changed = any(
            stored is not None and incoming is not None and stored != incoming
            for stored, incoming in (
                (stored_principal, principal_key),
                (stored_physical, physical_key),
            )
        )
        reobserving = str(row["status"]) == "reobservation_required" and str(
            scope["action_kind"]
        ) in {"reconnaissance", "baseline"}
        transition = "unattributed" if identity_changed or reobserving else "attributed"
        if identity_changed and not reobserving:
            raise ValueError("state lane identity changed without authoritative re-observation")
        next_revision = current + 1
        observation_digest = _digest(envelope.after.model_dump(mode="json"))
        next_principal = principal_key or stored_principal
        next_physical = physical_key or stored_physical
        authority = str(scope["target_authority"])
        conservative_lane_id = _conservative_physical_lane_id(authority)
        conservative_lane = self.connection.execute(
            """SELECT * FROM merlin_storage.state_physical_lanes
               WHERE id = %s FOR UPDATE""",
            (conservative_lane_id,),
        ).fetchone()
        if conservative_lane is None:
            raise RuntimeError("conservative physical state lane disappeared before settlement")
        known_physical_state = next_physical is not None
        if known_physical_state:
            if int(scope["expected_conservative_ambiguity_revision"]) != int(
                conservative_lane["ambiguity_revision"]
            ):
                raise ValueError("state scope expected physical state is stale")
        elif int(scope["expected_conservative_lane_revision"]) != int(
            conservative_lane["revision"]
        ):
            raise ValueError("state scope expected physical state is stale")

        expected_physical_lane_id = scope["expected_physical_lane_id"]
        if known_physical_state:
            aggregate_principal = next_principal or "anonymous"
            physical_lane_id = _physical_lane_id(authority, aggregate_principal, next_physical)
            self.connection.execute(
                """INSERT INTO merlin_storage.state_physical_lanes
                       (id, target_authority, principal_key, physical_state_key)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (target_authority, principal_key, physical_state_key)
                   DO NOTHING""",
                (physical_lane_id, authority, aggregate_principal, next_physical),
            )
            physical_lane = self.connection.execute(
                """SELECT * FROM merlin_storage.state_physical_lanes
                   WHERE id = %s FOR UPDATE""",
                (physical_lane_id,),
            ).fetchone()
            if physical_lane is None:
                raise RuntimeError("physical state lane disappeared before settlement")
        else:
            physical_lane_id = conservative_lane_id
            physical_lane = conservative_lane
        expected_physical_lane_id = scope["expected_physical_lane_id"]
        resolving_conservative_alias = (
            known_physical_state
            and expected_physical_lane_id is not None
            and str(expected_physical_lane_id) == conservative_lane_id
        )
        if (
            expected_physical_lane_id is not None
            and not resolving_conservative_alias
            and (
                str(expected_physical_lane_id) != physical_lane_id
                or scope["expected_physical_lane_revision"] is None
                or int(scope["expected_physical_lane_revision"]) != int(physical_lane["revision"])
            )
        ):
            raise ValueError("state scope expected physical state is stale")
        next_conservative_revision = int(conservative_lane["revision"]) + 1
        next_ambiguity_revision = int(conservative_lane["ambiguity_revision"]) + int(
            not known_physical_state
        )
        advanced_conservative = self.connection.execute(
            """UPDATE merlin_storage.state_physical_lanes
               SET revision = %s, ambiguity_revision = %s,
                   latest_experiment_id = %s, latest_scope_id = %s,
                   latest_observation_digest = %s, updated_at = now()
               WHERE id = %s AND revision = %s RETURNING id""",
            (
                next_conservative_revision,
                next_ambiguity_revision,
                str(scope["experiment_id"]),
                scope_id,
                observation_digest,
                conservative_lane_id,
                int(conservative_lane["revision"]),
            ),
        ).fetchone()
        if advanced_conservative is None:
            raise ValueError("conservative physical state lane changed before settlement")
        if known_physical_state:
            next_physical_revision = int(physical_lane["revision"]) + 1
            advanced_physical = self.connection.execute(
                """UPDATE merlin_storage.state_physical_lanes
                   SET revision = %s, latest_experiment_id = %s, latest_scope_id = %s,
                       latest_observation_digest = %s, updated_at = now()
                   WHERE id = %s AND revision = %s RETURNING id""",
                (
                    next_physical_revision,
                    str(scope["experiment_id"]),
                    scope_id,
                    observation_digest,
                    physical_lane_id,
                    int(physical_lane["revision"]),
                ),
            ).fetchone()
        else:
            next_physical_revision = next_conservative_revision
            advanced_physical = advanced_conservative
        if advanced_physical is None:
            raise ValueError("physical state lane changed before settlement")
        self.connection.execute(
            """INSERT INTO merlin_storage.state_lane_revisions
                   (lane_id, revision, scope_id, transition, principal_key,
                    physical_state_key, observation_digest)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                str(scope["lane_id"]),
                next_revision,
                scope_id,
                transition,
                next_principal,
                next_physical,
                observation_digest,
            ),
        )
        next_status = "current" if reobserving else str(row["status"])
        self.connection.execute(
            """UPDATE merlin_storage.state_lanes
               SET revision = %s, status = %s, last_transition = %s,
                   principal_key = %s, physical_state_key = %s,
                   physical_lane_id = %s, physical_lane_revision = %s,
                   conservative_lane_revision = %s,
                   conservative_ambiguity_revision = %s,
                   latest_observation_digest = %s, latest_scope_id = %s,
                   reobserved_at = CASE WHEN %s THEN now() ELSE reobserved_at END,
                   updated_at = now()
               WHERE id = %s AND revision = %s""",
            (
                next_revision,
                next_status,
                transition,
                next_principal,
                next_physical,
                physical_lane_id,
                next_physical_revision,
                next_conservative_revision,
                next_ambiguity_revision,
                observation_digest,
                scope_id,
                reobserving,
                str(scope["lane_id"]),
                expected,
            ),
        )
        return {
            "id": str(scope["lane_id"]),
            "revision": next_revision,
            "status": next_status,
            "transition": transition,
        }

    def _revise_state_scope_locked(
        self,
        scope_id: str,
        expected_revision: int,
        value: StateScopeEnvelope,
        *,
        claim_id: str | None,
    ) -> dict[str, Any]:
        current = self.connection.execute(
            "SELECT * FROM merlin_storage.state_scopes WHERE id = %s FOR UPDATE", (scope_id,)
        ).fetchone()
        if current is None:
            raise KeyError(scope_id)
        if str(current["status"]) == "settled":
            raise ValueError("settled state scope evidence is immutable")
        if value.isolation_lease_id != claim_id:
            raise ValueError("state scope envelope must bind the active isolation lease")
        document = value.model_dump(mode="json")
        document_digest = _digest(document)
        latest = self.connection.execute(
            """SELECT revision, envelope_digest FROM merlin_storage.scope_envelopes
               WHERE scope_id = %s ORDER BY revision DESC LIMIT 1""",
            (scope_id,),
        ).fetchone()
        if (
            latest is not None
            and str(latest["envelope_digest"]) == document_digest
            and int(latest["revision"]) == value.revision
        ):
            return self._load_state_scope_locked(scope_id)
        if int(current["current_revision"]) != expected_revision:
            raise ValueError("state scope revision changed")
        if value.revision != expected_revision + 1:
            raise ValueError("state scope envelope revision must advance exactly once")
        expected_identity = (
            str(current["campaign_id"]),
            str(current["experiment_id"]),
            str(current["attempt_id"]),
            current["candidate_id"],
            str(current["action_id"]),
            str(current["action_kind"]),
            str(current["target_authority"]),
        )
        actual_identity = (
            value.campaign_id or str(current["campaign_id"]),
            value.experiment_id,
            value.attempt_id,
            value.candidate_id,
            value.action_id,
            value.action_kind.value,
            value.target_authority,
        )
        if actual_identity != expected_identity:
            raise ValueError("state scope revision changed logical identity")
        principal_key = _identity_key(value.principal_identity)
        physical_key = _identity_key(value.physical_state_identity)
        for stored, incoming in (
            (current["principal_key"], principal_key),
            (current["physical_state_key"], physical_key),
        ):
            if stored is not None and str(stored) != incoming:
                raise ValueError("state scope revision changed a safe identity")
        revision = expected_revision + 1
        self.connection.execute(
            """INSERT INTO merlin_storage.scope_envelopes
                   (scope_id, revision, envelope, envelope_digest)
               VALUES (%s, %s, %s, %s)""",
            (scope_id, revision, Jsonb(document), document_digest),
        )
        self.connection.execute(
            """UPDATE merlin_storage.state_scopes
               SET current_revision = %s,
                   principal_key = coalesce(principal_key, %s),
                   physical_state_key = coalesce(physical_state_key, %s),
                   updated_at = now() WHERE id = %s""",
            (revision, principal_key, physical_key, scope_id),
        )
        return self._load_state_scope_locked(scope_id)

    def _load_state_scope_locked(self, scope_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            """SELECT scope.*, envelope.envelope, envelope.envelope_digest
               FROM merlin_storage.state_scopes AS scope
               JOIN merlin_storage.scope_envelopes AS envelope
                 ON envelope.scope_id = scope.id
                AND envelope.revision = scope.current_revision
               WHERE scope.id = %s""",
            (scope_id,),
        ).fetchone()
        if row is None:
            raise KeyError(scope_id)
        result = dict(row)
        result["envelope"] = StateScopeEnvelope.model_validate(result["envelope"])
        return result

    def _validate_scope_resource_identity(
        self, scope_id: str, target_authority: str, principal_key: str
    ) -> str:
        scope = self.connection.execute(
            """SELECT target_authority, principal_key, status
               FROM merlin_storage.state_scopes
               WHERE id = %s""",
            (scope_id,),
        ).fetchone()
        if scope is None:
            raise KeyError(scope_id)
        if (
            str(scope["target_authority"]) != target_authority
            or str(scope["principal_key"] or "anonymous") != principal_key
        ):
            raise ValueError("resource identity does not match its state scope")
        return str(scope["status"])

    def _require_physical_lane_current_locked(self, scope_id: str) -> None:
        """Reject an experiment whose physical state changed between its actions."""

        row = self.connection.execute(
            """SELECT scope.expected_physical_lane_id,
                      scope.expected_physical_lane_revision,
                      scope.expected_conservative_lane_revision,
                      scope.expected_conservative_ambiguity_revision,
                      scope.target_authority,
                      lane.physical_lane_id,
                      lane.physical_lane_revision,
                      lane.physical_state_key
               FROM merlin_storage.state_scopes AS scope
               JOIN merlin_storage.state_lanes AS lane ON lane.id = scope.lane_id
               WHERE scope.id = %s
               FOR UPDATE OF scope, lane""",
            (scope_id,),
        ).fetchone()
        if row is None:
            raise KeyError(scope_id)
        conservative = self.connection.execute(
            """SELECT revision, ambiguity_revision
               FROM merlin_storage.state_physical_lanes
               WHERE id = %s FOR UPDATE""",
            (_conservative_physical_lane_id(str(row["target_authority"])),),
        ).fetchone()
        if conservative is None:
            raise RuntimeError("conservative physical state lane disappeared")
        if row["physical_state_key"] is None:
            conservative_stale = int(conservative["revision"]) != int(
                row["expected_conservative_lane_revision"]
            )
        else:
            conservative_stale = int(conservative["ambiguity_revision"]) != int(
                row["expected_conservative_ambiguity_revision"]
            )
        if conservative_stale:
            raise ValueError("state scope expected physical state is stale")
        expected_lane_id = row["expected_physical_lane_id"]
        if expected_lane_id is None:
            return
        physical = self.connection.execute(
            """SELECT revision FROM merlin_storage.state_physical_lanes
               WHERE id = %s FOR UPDATE""",
            (expected_lane_id,),
        ).fetchone()
        if (
            row["physical_lane_id"] is None
            or str(row["physical_lane_id"]) != str(expected_lane_id)
            or physical is None
            or row["expected_physical_lane_revision"] is None
            or int(physical["revision"]) != int(row["expected_physical_lane_revision"])
            or row["physical_lane_revision"] is None
            or int(row["physical_lane_revision"]) != int(row["expected_physical_lane_revision"])
        ):
            raise ValueError("state scope expected physical state is stale")

    def _require_state_scope_lease_locked(
        self, scope_id: str, claim_id: str, claimant_id: str
    ) -> None:
        claim = _required(claim_id, "claim id")
        claimant = _required(claimant_id, "claimant id")
        row = self.connection.execute(
            """SELECT 1 FROM merlin_storage.state_scope_leases
               WHERE scope_id = %s AND claim_id = %s AND claimant_id = %s
                 AND expires_at > now() FOR SHARE""",
            (scope_id, claim, claimant),
        ).fetchone()
        if row is None:
            raise ValueError("state scope lease is missing, expired, or owned elsewhere")

    def _reopen_due_resource_locked(
        self, target_authority: str, principal_key: str, resource_kind: str, window_key: str
    ) -> bool:
        row = self.connection.execute(
            """UPDATE merlin_storage.principal_resources AS resource
               SET state = 'active', reserved = 0, consumed = 0, wake_at = NULL,
                   generation = generation + 1, updated_at = now()
               WHERE target_authority = %s AND principal_key = %s
                 AND resource_kind = %s AND window_key = %s
                 AND state = 'suspended' AND wake_at <= now()
                 AND NOT EXISTS (
                     SELECT 1 FROM merlin_storage.resource_reservations AS reservation
                     WHERE reservation.target_authority = resource.target_authority
                       AND reservation.principal_key = resource.principal_key
                       AND reservation.resource_kind = resource.resource_kind
                       AND reservation.window_key = resource.window_key
                       AND reservation.state IN ('reserved', 'acknowledged')
                 ) RETURNING generation""",
            (target_authority, principal_key, resource_kind, window_key),
        ).fetchone()
        return row is not None

    @staticmethod
    def _resource_decision(
        row: Mapping[str, object], *, admitted: bool, available: int | None = None
    ) -> dict[str, Any]:
        return {
            "admitted": admitted,
            "state": str(row["state"]),
            "reservation_id": str(row["id"]),
            "available": available,
            "wake_at": row.get("wake_at"),
        }
