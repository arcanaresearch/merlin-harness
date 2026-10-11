from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Any, Literal

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

LEGACY_API_SESSION_SCOPE_ID = "legacy:campaign"
CleanupDisposition = Literal[
    "cleanup_completed",
    "cleanup_not_owned",
    "cleanup_requirement_unestablished",
    "no_cleanup_obligation",
]


class RemoteSessionClaimConflict(ValueError):
    """A physical target session is already owned by another live scope."""

    def __init__(
        self,
        *,
        target_id: str,
        remote_session_id: str,
        owner_session_scope_id: str | None,
    ) -> None:
        super().__init__("remote API session is already owned by another scope")
        self.target_id = target_id
        self.remote_session_id = remote_session_id
        self.owner_session_scope_id = owner_session_scope_id


@dataclass(frozen=True, slots=True)
class CampaignApiSession:
    campaign_id: str
    target_id: str
    adapter_digest: str
    state: str
    variables: Mapping[str, object]
    pending_step: Mapping[str, object] | None = None
    generation: int = 1
    session_scope_id: str = LEGACY_API_SESSION_SCOPE_ID
    remote_session_id: str | None = None
    cleanup_disposition: CleanupDisposition | None = None

    @property
    def target_session_id(self) -> str:
        """Durable identity of this target-session scope and generation."""
        legacy = f"api:{self.campaign_id}:{self.target_id}:generation:{self.generation}"
        if self.session_scope_id == LEGACY_API_SESSION_SCOPE_ID:
            return legacy
        return (
            f"api:{self.campaign_id}:{self.target_id}:scope:{self.session_scope_id}:"
            f"generation:{self.generation}"
        )


class CampaignApiSessionRepository:
    """Durable reviewed HTTP API sessions, isolated by assignment scope."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self._lock = RLock()

    def load(
        self,
        campaign_id: str,
        *,
        target_id: str | None = None,
        session_scope_id: str = LEGACY_API_SESSION_SCOPE_ID,
    ) -> CampaignApiSession | None:
        """Load one exact scope; omitted identity deliberately means legacy only."""
        with self._lock:
            rows = self.connection.execute(
                """SELECT * FROM merlin_storage.campaign_api_sessions
                   WHERE campaign_id = %s AND session_scope_id = %s
                     AND (%s::text IS NULL OR target_id = %s)
                   ORDER BY target_id""",
                (campaign_id, session_scope_id, target_id, target_id),
            ).fetchall()
        if len(rows) > 1:
            raise ValueError("campaign API session target identity is ambiguous")
        return None if not rows else self._record(rows[0])

    def list_for_campaign(
        self,
        campaign_id: str,
        *,
        include_cleaned: bool = True,
    ) -> tuple[CampaignApiSession, ...]:
        """Enumerate campaign-owned scopes for shutdown and recovery."""
        state_clause = "" if include_cleaned else " AND state <> 'cleaned'"
        with self._lock:
            rows = self.connection.execute(
                """SELECT * FROM merlin_storage.campaign_api_sessions
                   WHERE campaign_id = %s"""
                + state_clause
                + " ORDER BY target_id, session_scope_id",
                (campaign_id,),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def create(
        self,
        campaign_id: str,
        target_id: str,
        adapter_digest: str,
        *,
        session_scope_id: str = LEGACY_API_SESSION_SCOPE_ID,
    ) -> CampaignApiSession:
        with self._lock, self.connection.transaction():
            if session_scope_id == LEGACY_API_SESSION_SCOPE_ID:
                existing_legacy = self.connection.execute(
                    """SELECT target_id
                       FROM merlin_storage.campaign_api_sessions
                       WHERE campaign_id = %s AND session_scope_id = %s
                       LIMIT 1""",
                    (campaign_id, LEGACY_API_SESSION_SCOPE_ID),
                ).fetchone()
                if existing_legacy is not None and str(existing_legacy["target_id"]) != target_id:
                    raise ValueError("campaign API session adapter identity changed")
            row = self.connection.execute(
                """INSERT INTO merlin_storage.campaign_api_sessions
                       (campaign_id, target_id, session_scope_id, adapter_digest, state)
                   VALUES (%s, %s, %s, %s, 'active')
                   ON CONFLICT (campaign_id, target_id, session_scope_id) DO UPDATE
                   SET state = 'active',
                       variables = '{}'::jsonb
                           || CASE
                               WHEN merlin_storage.campaign_api_sessions.variables
                                    ? '__terminal_exchanges'
                               THEN jsonb_build_object(
                                   '__terminal_exchanges',
                                   merlin_storage.campaign_api_sessions.variables
                                       -> '__terminal_exchanges'
                               )
                               ELSE '{}'::jsonb
                           END
                           || CASE
                               WHEN merlin_storage.campaign_api_sessions.variables
                                    ? '__terminal_replay_dispositions'
                               THEN jsonb_build_object(
                                   '__terminal_replay_dispositions',
                                   merlin_storage.campaign_api_sessions.variables
                                       -> '__terminal_replay_dispositions'
                               )
                               ELSE '{}'::jsonb
                           END,
                       pending_step = NULL,
                       remote_session_id = NULL,
                       cleanup_disposition = NULL,
                       generation = merlin_storage.campaign_api_sessions.generation + 1,
                       updated_at = now()
                   WHERE merlin_storage.campaign_api_sessions.state = 'cleaned'
                     AND merlin_storage.campaign_api_sessions.adapter_digest =
                         EXCLUDED.adapter_digest
                   RETURNING *""",
                (campaign_id, target_id, session_scope_id, adapter_digest),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    """SELECT * FROM merlin_storage.campaign_api_sessions
                       WHERE campaign_id = %s AND target_id = %s
                         AND session_scope_id = %s""",
                    (campaign_id, target_id, session_scope_id),
                ).fetchone()
            if row is None:
                raise RuntimeError("campaign API session disappeared")
            record = self._record(row)
            if record.adapter_digest != adapter_digest:
                raise ValueError("campaign API session adapter identity changed")
            return record

    def claim_remote_session(
        self,
        campaign_id: str,
        remote_session_id: str,
        *,
        target_id: str,
        session_scope_id: str,
    ) -> CampaignApiSession:
        """Atomically bind a live remote session to exactly one local scope."""
        try:
            with self._lock, self.connection.transaction():
                row = self.connection.execute(
                    """UPDATE merlin_storage.campaign_api_sessions
                       SET remote_session_id = %s, updated_at = now()
                       WHERE campaign_id = %s AND target_id = %s
                         AND session_scope_id = %s
                         AND state IN ('active', 'ambiguous')
                         AND (remote_session_id IS NULL OR remote_session_id = %s)
                       RETURNING *""",
                    (
                        remote_session_id,
                        campaign_id,
                        target_id,
                        session_scope_id,
                        remote_session_id,
                    ),
                ).fetchone()
                if row is None:
                    raise ValueError("campaign API session cannot claim remote identity")
                return self._record(row)
        except UniqueViolation as error:
            with self._lock:
                owner = self.connection.execute(
                    """SELECT session_scope_id
                       FROM merlin_storage.campaign_api_sessions
                       WHERE target_id = %s AND remote_session_id = %s
                         AND state <> 'cleaned'
                       LIMIT 1""",
                    (target_id, remote_session_id),
                ).fetchone()
            raise RemoteSessionClaimConflict(
                target_id=target_id,
                remote_session_id=remote_session_id,
                owner_session_scope_id=(None if owner is None else str(owner["session_scope_id"])),
            ) from error

    def adopt_remote_session(
        self,
        campaign_id: str,
        remote_session_id: str,
        *,
        target_id: str,
        session_scope_id: str,
    ) -> CampaignApiSession:
        """Claim a remote session after releasing terminal quarantined claims.

        A scope retired with an unestablished cleanup requirement keeps its
        physical-session claim quarantined so nothing silently reuses
        potentially contaminated state. On a single-physical-session target the
        reviewed setup resumes that same session for every later scope, so the
        retained claim would otherwise fail every subsequent delivery forever.
        Adoption releases claims held by terminal ``ambiguous`` scopes — their
        quarantine records and dispositions remain — and claims whose owning
        campaign row is already terminal (a killed daemon never ran cleanup),
        then performs the ordinary atomic claim. A claim held by a still-active
        scope of a live campaign remains a hard :class:`RemoteSessionClaimConflict`.
        """
        try:
            with self._lock, self.connection.transaction():
                # Orphaned claims: the owning campaign is terminal, so no live
                # scope can still be using the physical session. Release them
                # first; they carry no quarantine meaning.
                self.connection.execute(
                    """UPDATE merlin_storage.campaign_api_sessions s
                       SET remote_session_id = NULL, updated_at = now()
                       FROM merlin_storage.assessment_campaigns c
                       WHERE s.campaign_id = c.id
                         AND c.status IN ('completed', 'failed', 'cancelled')
                         AND s.target_id = %s AND s.remote_session_id = %s
                         AND s.state <> 'cleaned'""",
                    (target_id, remote_session_id),
                )
                self.connection.execute(
                    """UPDATE merlin_storage.campaign_api_sessions
                       SET remote_session_id = NULL, updated_at = now()
                       WHERE target_id = %s AND remote_session_id = %s
                         AND state = 'ambiguous'""",
                    (target_id, remote_session_id),
                )
                row = self.connection.execute(
                    """UPDATE merlin_storage.campaign_api_sessions
                       SET remote_session_id = %s, updated_at = now()
                       WHERE campaign_id = %s AND target_id = %s
                         AND session_scope_id = %s
                         AND state IN ('active', 'ambiguous')
                         AND (remote_session_id IS NULL OR remote_session_id = %s)
                       RETURNING *""",
                    (
                        remote_session_id,
                        campaign_id,
                        target_id,
                        session_scope_id,
                        remote_session_id,
                    ),
                ).fetchone()
                if row is None:
                    raise ValueError("campaign API session cannot claim remote identity")
                return self._record(row)
        except UniqueViolation as error:
            with self._lock:
                owner = self.connection.execute(
                    """SELECT session_scope_id
                       FROM merlin_storage.campaign_api_sessions
                       WHERE target_id = %s AND remote_session_id = %s
                         AND state <> 'cleaned'
                       LIMIT 1""",
                    (target_id, remote_session_id),
                ).fetchone()
            raise RemoteSessionClaimConflict(
                target_id=target_id,
                remote_session_id=remote_session_id,
                owner_session_scope_id=(None if owner is None else str(owner["session_scope_id"])),
            ) from error

    def release_remote_session(
        self,
        campaign_id: str,
        *,
        target_id: str,
        session_scope_id: str,
        remote_session_id: str | None = None,
    ) -> CampaignApiSession:
        """Release only the exact scope's current physical-session claim."""
        with self._lock, self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_api_sessions
                   SET remote_session_id = NULL, updated_at = now()
                   WHERE campaign_id = %s AND target_id = %s
                     AND session_scope_id = %s
                     AND (%s::text IS NULL OR remote_session_id = %s)
                   RETURNING *""",
                (
                    campaign_id,
                    target_id,
                    session_scope_id,
                    remote_session_id,
                    remote_session_id,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("campaign API session remote identity changed")
            return self._record(row)

    def begin_step(
        self,
        campaign_id: str,
        step: Mapping[str, object],
        *,
        target_id: str | None = None,
        session_scope_id: str = LEGACY_API_SESSION_SCOPE_ID,
    ) -> CampaignApiSession:
        target_id = self._resolve_target_id(campaign_id, target_id, session_scope_id)
        with self._lock, self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_api_sessions
                   SET pending_step = %s, updated_at = now()
                   WHERE campaign_id = %s AND target_id = %s
                     AND session_scope_id = %s
                     AND state = 'active' AND pending_step IS NULL
                   RETURNING *""",
                (Jsonb(dict(step)), campaign_id, target_id, session_scope_id),
            ).fetchone()
            if row is None:
                raise ValueError("campaign API session has unresolved or terminal work")
            return self._record(row)

    def finish_step(
        self,
        campaign_id: str,
        variables: Mapping[str, object],
        *,
        target_id: str | None = None,
        session_scope_id: str = LEGACY_API_SESSION_SCOPE_ID,
    ) -> CampaignApiSession:
        target_id = self._resolve_target_id(campaign_id, target_id, session_scope_id)
        with self._lock, self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_api_sessions
                   SET variables = %s, pending_step = NULL, state = 'active', updated_at = now()
                   WHERE campaign_id = %s AND target_id = %s
                     AND session_scope_id = %s
                     AND state IN ('active', 'ambiguous') AND pending_step IS NOT NULL
                   RETURNING *""",
                (Jsonb(dict(variables)), campaign_id, target_id, session_scope_id),
            ).fetchone()
            if row is None:
                raise ValueError("campaign API session has no active step")
            return self._record(row)

    def mark_ambiguous(
        self,
        campaign_id: str,
        *,
        target_id: str | None = None,
        session_scope_id: str = LEGACY_API_SESSION_SCOPE_ID,
    ) -> CampaignApiSession:
        target_id = self._resolve_target_id(campaign_id, target_id, session_scope_id)
        with self._lock, self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_api_sessions
                   SET state = 'ambiguous', updated_at = now()
                   WHERE campaign_id = %s AND target_id = %s
                     AND session_scope_id = %s
                     AND state IN ('active', 'ambiguous')
                     AND pending_step IS NOT NULL
                   RETURNING *""",
                (campaign_id, target_id, session_scope_id),
            ).fetchone()
            if row is None:
                raise ValueError("campaign API session has no ambiguous step")
            return self._record(row)

    def quarantine(
        self,
        campaign_id: str,
        *,
        target_id: str,
        session_scope_id: str,
    ) -> CampaignApiSession:
        """Quarantine one exact live scope without altering its durable evidence."""
        with self._lock, self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_api_sessions
                   SET state = 'ambiguous', updated_at = now()
                   WHERE campaign_id = %s AND target_id = %s
                     AND session_scope_id = %s AND state <> 'cleaned'
                   RETURNING *""",
                (campaign_id, target_id, session_scope_id),
            ).fetchone()
            if row is None:
                raise ValueError("campaign API session is unavailable for quarantine")
            return self._record(row)

    def begin_cleanup(
        self,
        campaign_id: str,
        step: Mapping[str, object],
        *,
        target_id: str | None = None,
        session_scope_id: str = LEGACY_API_SESSION_SCOPE_ID,
    ) -> CampaignApiSession:
        target_id = self._resolve_target_id(campaign_id, target_id, session_scope_id)
        with self._lock, self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_api_sessions
                   SET pending_step = %s, updated_at = now()
                   WHERE campaign_id = %s AND target_id = %s
                     AND session_scope_id = %s
                     AND state IN ('active', 'ambiguous')
                   RETURNING *""",
                (Jsonb(dict(step)), campaign_id, target_id, session_scope_id),
            ).fetchone()
            if row is None:
                raise ValueError("campaign API session is unavailable for cleanup")
            return self._record(row)

    def finish_cleanup(
        self,
        campaign_id: str,
        variables: Mapping[str, object] | None = None,
        *,
        target_id: str | None = None,
        session_scope_id: str = LEGACY_API_SESSION_SCOPE_ID,
    ) -> CampaignApiSession:
        target_id = self._resolve_target_id(campaign_id, target_id, session_scope_id)
        with self._lock, self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_api_sessions
                   SET state = 'cleaned', pending_step = NULL,
                       variables = COALESCE(%s, variables), remote_session_id = NULL,
                       cleanup_disposition = 'cleanup_completed',
                       updated_at = now()
                   WHERE campaign_id = %s AND target_id = %s
                     AND session_scope_id = %s AND state <> 'cleaned'
                   RETURNING *""",
                (
                    None if variables is None else Jsonb(dict(variables)),
                    campaign_id,
                    target_id,
                    session_scope_id,
                ),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    """SELECT * FROM merlin_storage.campaign_api_sessions
                       WHERE campaign_id = %s AND target_id = %s
                         AND session_scope_id = %s""",
                    (campaign_id, target_id, session_scope_id),
                ).fetchone()
            if row is None:
                raise KeyError((campaign_id, target_id, session_scope_id))
            return self._record(row)

    def fail_cleanup(
        self,
        campaign_id: str,
        variables: Mapping[str, object],
        *,
        target_id: str,
        session_scope_id: str,
    ) -> CampaignApiSession:
        """Persist a failed cleanup exchange without releasing remote ownership."""
        with self._lock, self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_api_sessions
                   SET state = 'ambiguous', variables = %s, pending_step = NULL,
                       cleanup_disposition = NULL, updated_at = now()
                   WHERE campaign_id = %s AND target_id = %s
                     AND session_scope_id = %s AND state <> 'cleaned'
                     AND pending_step IS NOT NULL
                   RETURNING *""",
                (Jsonb(dict(variables)), campaign_id, target_id, session_scope_id),
            ).fetchone()
            if row is None:
                raise ValueError("campaign API session has no pending cleanup")
            return self._record(row)

    def finish_without_cleanup(
        self,
        campaign_id: str,
        variables: Mapping[str, object] | None = None,
        *,
        target_id: str,
        session_scope_id: str,
        disposition: Literal[
            "cleanup_not_owned",
            "cleanup_requirement_unestablished",
            "no_cleanup_obligation",
        ],
    ) -> CampaignApiSession:
        """Record why remote cleanup was not attempted for one exact scope.

        An explicitly stateless target or a scope that never owned the remote
        identity can finish locally.  An unknown cleanup requirement is
        different: absence of a reviewed cleanup action is not evidence that
        the physical target session ended.  Keep that scope ambiguous and keep
        its claim so another assignment cannot silently reuse contaminated
        target state.
        """
        if disposition not in {
            "cleanup_not_owned",
            "cleanup_requirement_unestablished",
            "no_cleanup_obligation",
        }:
            raise ValueError("invalid no-cleanup disposition")
        with self._lock, self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_api_sessions
                   SET state = CASE
                           WHEN %s = 'cleanup_requirement_unestablished'
                           THEN 'ambiguous'
                           ELSE 'cleaned'
                       END,
                       pending_step = NULL,
                       variables = COALESCE(%s, variables),
                       remote_session_id = CASE
                           WHEN %s = 'cleanup_requirement_unestablished'
                           THEN remote_session_id
                           ELSE NULL
                       END,
                       cleanup_disposition = %s, updated_at = now()
                   WHERE campaign_id = %s AND target_id = %s
                     AND session_scope_id = %s AND state <> 'cleaned'
                   RETURNING *""",
                (
                    disposition,
                    None if variables is None else Jsonb(dict(variables)),
                    disposition,
                    disposition,
                    campaign_id,
                    target_id,
                    session_scope_id,
                ),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    """SELECT * FROM merlin_storage.campaign_api_sessions
                       WHERE campaign_id = %s AND target_id = %s
                         AND session_scope_id = %s""",
                    (campaign_id, target_id, session_scope_id),
                ).fetchone()
            if row is None:
                raise KeyError((campaign_id, target_id, session_scope_id))
            record = self._record(row)
            if record.cleanup_disposition != disposition:
                raise ValueError("campaign API session cleanup disposition changed")
            return record

    def _resolve_target_id(
        self,
        campaign_id: str,
        target_id: str | None,
        session_scope_id: str,
    ) -> str:
        if target_id is not None:
            return target_id
        record = self.load(campaign_id, session_scope_id=session_scope_id)
        if record is None:
            raise KeyError((campaign_id, session_scope_id))
        return record.target_id

    @staticmethod
    def _record(row: Mapping[str, object]) -> CampaignApiSession:
        value = dict(row)
        return CampaignApiSession(
            campaign_id=str(value["campaign_id"]),
            target_id=str(value["target_id"]),
            adapter_digest=str(value["adapter_digest"]),
            state=str(value["state"]),
            variables=dict(value.get("variables") or {}),
            pending_step=(
                None if value.get("pending_step") is None else dict(value["pending_step"])
            ),
            generation=int(value["generation"]),
            session_scope_id=str(value.get("session_scope_id") or LEGACY_API_SESSION_SCOPE_ID),
            remote_session_id=(
                None if value.get("remote_session_id") is None else str(value["remote_session_id"])
            ),
            cleanup_disposition=value.get("cleanup_disposition"),
        )
