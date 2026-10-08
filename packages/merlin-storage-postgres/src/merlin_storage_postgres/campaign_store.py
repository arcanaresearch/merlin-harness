from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from functools import wraps
from hashlib import sha256
from threading import RLock
from typing import Any
from uuid import uuid4

from merlin_agents import ExplorationCriticDecision
from merlin_contracts import CampaignObservation
from merlin_memory import VerifiedInteractionLesson
from psycopg.types.json import Jsonb

from .interaction_store import InteractionLessonRepository


def _serialize_connection_methods(cls: type[Any]) -> type[Any]:
    """Serialize complete operations performed through one psycopg connection."""

    for name, method in tuple(vars(cls).items()):
        if name.startswith("_") or not callable(method):
            continue

        @wraps(method)
        def serialized(self: Any, *args: Any, __method: Any = method, **kwargs: Any) -> Any:
            with self._connection_lock:
                return __method(self, *args, **kwargs)

        setattr(cls, name, serialized)
    return cls


@_serialize_connection_methods
class CampaignStore:
    """Durable authority for reviewed manifests and their execution binding."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        # psycopg connections serialize cursors, but concurrent transaction
        # contexts on the same connection can still exit out of nesting order.
        # Campaign supervision, worker budgets, and mailbox delivery all call
        # this synchronous store from separate worker threads, so lock the
        # whole store operation rather than individual SQL statements.
        # The lock belongs to the connection, not this repository wrapper. A
        # daemon may construct a replacement CampaignStore around the same
        # connection during recovery; per-instance locks would then permit two
        # transaction contexts to overlap and psycopg would reject whichever
        # context exits out of order.
        connection_lock = getattr(connection, "_merlin_operation_lock", None)
        if connection_lock is None:
            connection_lock = RLock()
            connection._merlin_operation_lock = connection_lock
        self._connection_lock = connection_lock

    def list_technique_attempts(self, campaign_id: str) -> list[Mapping[str, Any]]:
        """Return authoritative attempt identities available to Lead control-plane requests."""

        rows = self.connection.execute(
            """SELECT id, campaign_id, task_id, technique_id, state, strategy_id,
                      control_kind, oracle_digest, terminal_reason
               FROM merlin_storage.campaign_technique_attempts
               WHERE campaign_id = %s
               ORDER BY created_at, id""",
            (campaign_id,),
        ).fetchall()
        return [
            {
                **row,
                "id": str(row["id"]),
                "campaign_id": str(row["campaign_id"]),
            }
            for row in rows
        ]

    def record_technique_history(self, entries: Sequence[Mapping[str, Any]]) -> None:
        """Record same-target catalog-cell outcomes from a campaign completion.

        Idempotent on the cell key; a terminal prior outcome is never downgraded
        to ``unclear`` by a later inconclusive campaign.
        """
        with self.connection.transaction():
            for entry in entries:
                outcome = str(entry["outcome"])
                if outcome not in {
                    "confirmed",
                    "refuted",
                    "blocked",
                    "blocked_terminal",
                    "partial",
                    "unclear",
                }:
                    raise ValueError(f"technique history outcome is invalid: {outcome}")
                partial_signal = entry.get("partial_signal")
                self.connection.execute(
                    """INSERT INTO merlin_storage.technique_history
                           (target_id, adapter_generation, technique_id, controller_id,
                            payload_family, language_id, mutation_id, outcome,
                            source_campaign_id, partial_signal)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (target_id, adapter_generation, technique_id,
                                    controller_id, payload_family, language_id, mutation_id)
                       DO UPDATE SET outcome = EXCLUDED.outcome,
                                     source_campaign_id = EXCLUDED.source_campaign_id,
                                     partial_signal = EXCLUDED.partial_signal
                       WHERE NOT (
                           technique_history.outcome IN ('confirmed', 'refuted')
                           AND EXCLUDED.outcome NOT IN ('confirmed', 'refuted')
                       ) AND NOT (
                           technique_history.outcome = 'partial'
                           AND EXCLUDED.outcome = 'unclear'
                       )""",
                    (
                        str(entry["target_id"]),
                        str(entry["adapter_generation"]),
                        str(entry["technique_id"]),
                        str(entry["controller_id"]),
                        str(entry["payload_family"]),
                        str(entry.get("language_id", "und")),
                        str(entry.get("mutation_id", "none")),
                        outcome,
                        str(entry["source_campaign_id"]),
                        Jsonb(dict(partial_signal))
                        if isinstance(partial_signal, Mapping)
                        else None,
                    ),
                )

    def technique_history_generations(self, target_id: str) -> list[dict[str, Any]]:
        """Known adapter generations and row counts for one target.

        Lets preparation journal a diagnostic when history exists for the
        target but none of it keys under the current generation.
        """
        rows = self.connection.execute(
            """SELECT adapter_generation, COUNT(*) AS rows
               FROM merlin_storage.technique_history
               WHERE target_id = %s
               GROUP BY adapter_generation
               ORDER BY adapter_generation""",
            (target_id,),
        ).fetchall()
        return [
            {"adapter_generation": str(row["adapter_generation"]), "rows": int(row["rows"])}
            for row in rows
        ]

    def lookup_technique_history(
        self,
        target_id: str,
        adapter_generation: str,
        cells: Sequence[tuple[str, str, str, str, str]],
    ) -> dict[tuple[str, str, str, str, str], dict[str, Any]]:
        """Return prior outcomes for catalog cells of one target generation.

        ``cells`` identify technique, controller, transform, language, and
        mutation; the result is keyed on the same tuple. A different adapter generation
        mints different keys, so a changed target re-runs everything. Raw rows
        remain append-only evidence. The returned canonical projection marks
        A2A terminal history retryable unless its source attempt has authenticated
        peer-only target effects.
        """
        wanted = {tuple(str(part) for part in cell) for cell in cells}
        if not wanted:
            return {}
        rows = self.connection.execute(
            """WITH wanted AS (
                   SELECT item ->> 0 AS technique_id,
                          item ->> 1 AS controller_id,
                          item ->> 2 AS payload_family,
                          item ->> 3 AS language_id,
                          item ->> 4 AS mutation_id
                   FROM jsonb_array_elements(%s::jsonb) AS item
               ), selected_history AS (
                   SELECT history.*
                   FROM merlin_storage.technique_history AS history
                   JOIN wanted USING (
                       technique_id, controller_id, payload_family,
                       language_id, mutation_id
                   )
                   WHERE history.target_id = %s
                     AND history.adapter_generation = %s
               ), peer_evidence AS (
                   SELECT history.target_id, history.adapter_generation,
                          history.technique_id, history.controller_id,
                          history.payload_family, history.language_id,
                          history.mutation_id,
                          COUNT(DISTINCT attempt.id) AS matching_attempts,
                          BOOL_OR(
                              effect.result_is_error IS FALSE
                              AND effect.result_metadata
                                    -> 'target_delivery_receipt'
                                    ->> 'actual_delivery' = 'inter-agent-message'
                          ) AS has_peer_success,
                          BOOL_AND(
                              COALESCE(
                                  effect.result_metadata
                                    -> 'target_delivery_receipt'
                                    ->> 'actual_delivery',
                                  'unknown'
                              ) = 'inter-agent-message'
                          ) FILTER (WHERE effect.idempotency_key IS NOT NULL) AS peer_only
                   FROM selected_history AS history
                   LEFT JOIN merlin_storage.campaign_technique_attempts AS attempt
                     ON history.technique_id LIKE '%%/cat/agent-to-agent/%%'
                    AND history.mutation_id = 'none'
                    AND attempt.campaign_id = history.source_campaign_id
                    AND attempt.technique_id = history.technique_id
                    AND attempt.strategy_id = history.controller_id
                    AND attempt.control_kind = 'attack'
                    AND attempt.state = history.outcome
                    AND COALESCE(attempt.transform_id, 'baseline')
                        = history.payload_family
                    AND attempt.language = history.language_id
                   LEFT JOIN merlin_storage.tool_effects AS effect
                     ON effect.campaign_id = attempt.campaign_id
                    AND effect.result_metadata ->> 'technique_attempt_id'
                        = attempt.id::text
                    AND effect.result_metadata ->> 'interaction_role'
                        = 'target_exchange'
                    AND effect.state IN ('acknowledged', 'reconciled')
                   WHERE history.technique_id LIKE '%%/cat/agent-to-agent/%%'
                   GROUP BY history.target_id, history.adapter_generation,
                            history.technique_id, history.controller_id,
                            history.payload_family, history.language_id,
                            history.mutation_id
               )
               SELECT history.technique_id, history.controller_id,
                      history.payload_family, history.language_id,
                      history.mutation_id, history.outcome,
                      history.source_campaign_id,
                      history.partial_signal,
                      CASE
                        WHEN history.technique_id NOT LIKE '%%/cat/agent-to-agent/%%'
                          THEN NULL
                        ELSE COALESCE(
                          peer.matching_attempts = 1
                          AND peer.has_peer_success
                          AND peer.peer_only,
                          FALSE
                        )
                      END AS peer_delivery_authenticated
               FROM selected_history AS history
               LEFT JOIN peer_evidence AS peer USING (
                   target_id, adapter_generation, technique_id, controller_id,
                   payload_family, language_id, mutation_id
               )""",
            (
                Jsonb([list(cell) for cell in sorted(wanted)]),
                target_id,
                adapter_generation,
            ),
        ).fetchall()
        projected: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for row in rows:
            key = (
                str(row["technique_id"]),
                str(row["controller_id"]),
                str(row["payload_family"]),
                str(row["language_id"]),
                str(row["mutation_id"]),
            )
            if key not in wanted:
                continue
            value: dict[str, Any] = {
                "outcome": str(row["outcome"]),
                "source_campaign_id": str(row["source_campaign_id"]),
            }
            if isinstance(row.get("partial_signal"), Mapping):
                value["partial_signal"] = dict(row["partial_signal"])
            if "/cat/agent-to-agent/" in key[0] and value["outcome"] in {"confirmed", "refuted"}:
                authenticated = row["peer_delivery_authenticated"] is True
                value["peer_delivery_authenticated"] = authenticated
                if not authenticated:
                    value.update(
                        {
                            "outcome": "unclear",
                            "recorded_outcome": str(row["outcome"]),
                            "retryable": True,
                            "evaluation_uncertainties": (
                                {
                                    "kind": "evaluation_uncertain",
                                    "source": "technique_history.peer_delivery_projection",
                                    "reason": (
                                        "Historical terminal A2A outcome lacks authenticated "
                                        "peer-only target delivery. Raw evidence is preserved; "
                                        "the inter-agent-message surface remains retryable."
                                    ),
                                    "confidence": 1.0,
                                    "retryable": True,
                                },
                            ),
                        }
                    )
            projected[key] = value
        return projected

    def prepare(
        self,
        campaign_id: str,
        session_id: str,
        manifest: Mapping[str, object],
        manifest_digest: str,
    ) -> dict[str, Any]:
        with self.connection.transaction():
            row = self.connection.execute(
                """INSERT INTO merlin_storage.assessment_campaigns
                       (id, session_id, manifest, manifest_digest, status)
                   VALUES (%s, %s, %s, %s, 'prepared') RETURNING *""",
                (campaign_id, session_id, Jsonb(dict(manifest)), manifest_digest),
            ).fetchone()
        return self._row(row)

    def get(self, campaign_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM merlin_storage.assessment_campaigns WHERE id = %s",
            (campaign_id,),
        ).fetchone()
        return None if row is None else self._row(row)

    def reserve_budget(
        self,
        campaign_id: str,
        reservation_id: str,
        *,
        actor_kind: str,
        tokens: int,
        turns: int = 1,
        cost_usd: float = 0.0,
        allow_partial_tokens: bool = False,
    ) -> dict[str, Any]:
        """Atomically reserve a bounded provider request against one sealed campaign budget."""
        if actor_kind not in {"lead", "worker"}:
            raise ValueError("budget reservation actor must be lead or worker")
        if not reservation_id or tokens < 0 or turns < 0 or cost_usd < 0:
            raise ValueError("budget reservation identity and amounts must be non-negative")
        with self.connection.transaction():
            campaign = self.connection.execute(
                "SELECT manifest FROM merlin_storage.assessment_campaigns WHERE id = %s FOR UPDATE",
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                raise KeyError(f"unknown campaign {campaign_id!r}")
            existing = self.connection.execute(
                """SELECT * FROM merlin_storage.campaign_budget_reservations
                   WHERE campaign_id = %s AND reservation_id = %s""",
                (campaign_id, reservation_id),
            ).fetchone()
            if existing is not None and existing["state"] != "released":
                return dict(existing)
            budget = _campaign_budget(campaign["manifest"])
            usage = self.connection.execute(
                """SELECT
                       COALESCE(SUM(CASE WHEN state = 'active' THEN reserved_tokens
                                         WHEN state = 'settled' THEN settled_tokens ELSE 0 END), 0)
                           AS tokens,
                       COALESCE(SUM(CASE WHEN state = 'active' THEN reserved_turns
                                         WHEN state = 'settled' THEN settled_turns ELSE 0 END), 0)
                           AS turns,
                       COALESCE(SUM(CASE WHEN state = 'active' THEN reserved_cost_usd
                                         WHEN state = 'settled' THEN settled_cost_usd
                                         ELSE 0 END), 0)
                           AS cost_usd
                   FROM merlin_storage.campaign_budget_reservations
                   WHERE campaign_id = %s""",
                (campaign_id,),
            ).fetchone()
            reserved_tokens = tokens
            reserved_cost_usd = cost_usd
            # Turns are recorded for telemetry but never gate a reservation.
            # A turn-count budget is an arbitrary cap the mature harnesses do
            # not impose; tokens, cost, and seconds remain the real limits.
            turns_fit = True
            if allow_partial_tokens:
                if budget["max_tokens"] is not None:
                    remaining_tokens = max(0, int(budget["max_tokens"]) - int(usage["tokens"]))
                    reserved_tokens = min(reserved_tokens, remaining_tokens)
                if budget["max_cost_usd"] is not None:
                    remaining_cost = max(
                        0.0,
                        float(budget["max_cost_usd"]) - float(usage["cost_usd"]),
                    )
                    if cost_usd > 0 and tokens > 0:
                        cost_limited_tokens = int(tokens * min(1.0, remaining_cost / cost_usd))
                        reserved_tokens = min(reserved_tokens, cost_limited_tokens)
                        reserved_cost_usd = reserved_tokens * (cost_usd / tokens)
                    else:
                        reserved_cost_usd = min(reserved_cost_usd, remaining_cost)
                admitted = (
                    turns_fit
                    and (tokens == 0 or reserved_tokens > 0)
                    and (cost_usd == 0 or reserved_cost_usd > 0)
                )
            else:
                admitted = not (
                    (
                        budget["max_tokens"] is not None
                        and usage["tokens"] + tokens > budget["max_tokens"]
                    )
                    or not turns_fit
                    or (
                        budget["max_cost_usd"] is not None
                        and float(usage["cost_usd"]) + cost_usd > budget["max_cost_usd"]
                    )
                )
            if existing is None:
                row = self.connection.execute(
                    """INSERT INTO merlin_storage.campaign_budget_reservations
                           (campaign_id, reservation_id, actor_kind, reserved_tokens,
                            reserved_turns, reserved_cost_usd, state)
                       VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                    (
                        campaign_id,
                        reservation_id,
                        actor_kind,
                        reserved_tokens,
                        turns,
                        reserved_cost_usd,
                        "active" if admitted else "rejected",
                    ),
                ).fetchone()
            else:
                row = self.connection.execute(
                    """UPDATE merlin_storage.campaign_budget_reservations
                       SET actor_kind = %s, reserved_tokens = %s, reserved_turns = %s,
                           reserved_cost_usd = %s, settled_tokens = NULL,
                           settled_turns = NULL, settled_cost_usd = NULL, settled_at = NULL,
                           state = %s, created_at = now()
                       WHERE campaign_id = %s AND reservation_id = %s RETURNING *""",
                    (
                        actor_kind,
                        reserved_tokens,
                        turns,
                        reserved_cost_usd,
                        "active" if admitted else "rejected",
                        campaign_id,
                        reservation_id,
                    ),
                ).fetchone()
        return dict(row)

    def settle_budget(
        self,
        campaign_id: str,
        reservation_id: str,
        *,
        tokens: int,
        turns: int = 1,
        cost_usd: float = 0.0,
    ) -> dict[str, Any]:
        if tokens < 0 or turns < 0 or cost_usd < 0:
            raise ValueError("settled budget amounts must be non-negative")
        with self.connection.transaction():
            reservation = self.connection.execute(
                """SELECT * FROM merlin_storage.campaign_budget_reservations
                   WHERE campaign_id = %s AND reservation_id = %s FOR UPDATE""",
                (campaign_id, reservation_id),
            ).fetchone()
            if reservation is None:
                raise KeyError(f"unknown budget reservation {reservation_id!r}")
            if reservation["state"] == "settled":
                if (
                    reservation["settled_tokens"] != tokens
                    or reservation["settled_turns"] != turns
                    or float(reservation["settled_cost_usd"]) != cost_usd
                ):
                    raise ValueError("budget reservation was already settled differently")
                return dict(reservation)
            if reservation["state"] != "active":
                raise ValueError("only an active budget reservation can be settled")
            # A reserved amount of zero means that dimension was intentionally left
            # uncapped (e.g. a turn-only reconnaissance worker). Only enforce caps
            # that were actually reserved; otherwise a turn-only worker that reports
            # real token/cost usage can never be settled against a zero reservation.
            reserved_tokens = int(reservation["reserved_tokens"])
            reserved_cost_usd = float(reservation["reserved_cost_usd"])
            if (
                (reserved_tokens > 0 and tokens > reserved_tokens)
                or turns > int(reservation["reserved_turns"])
                or (reserved_cost_usd > 0 and cost_usd > reserved_cost_usd)
            ):
                raise ValueError("settled usage exceeds the reserved campaign budget")
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_budget_reservations
                   SET state = 'settled', settled_tokens = %s, settled_turns = %s,
                       settled_cost_usd = %s,
                       settled_at = now()
                   WHERE campaign_id = %s AND reservation_id = %s RETURNING *""",
                (tokens, turns, cost_usd, campaign_id, reservation_id),
            ).fetchone()
        return dict(row)

    def release_budget(self, campaign_id: str, reservation_id: str) -> dict[str, Any]:
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_budget_reservations
                   SET state = 'released', settled_at = now()
                   WHERE campaign_id = %s AND reservation_id = %s AND state = 'active'
                   RETURNING *""",
                (campaign_id, reservation_id),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    """SELECT * FROM merlin_storage.campaign_budget_reservations
                       WHERE campaign_id = %s AND reservation_id = %s""",
                    (campaign_id, reservation_id),
                ).fetchone()
        if row is None:
            raise KeyError(f"unknown budget reservation {reservation_id!r}")
        return dict(row)

    def release_active_budget(self, campaign_id: str) -> int:
        """Release provider reservations orphaned when the owning daemon stopped."""
        with self.connection.transaction():
            cursor = self.connection.execute(
                """UPDATE merlin_storage.campaign_budget_reservations
                   SET state = 'released', settled_at = now()
                   WHERE campaign_id = %s AND state = 'active'""",
                (campaign_id,),
            )
        return cursor.rowcount

    def remaining_budget_seconds(self, campaign_id: str) -> float | None:
        """Return the sealed campaign deadline remaining from its first durable wave."""
        row = self.connection.execute(
            """SELECT campaign.manifest, MIN(wave.created_at) AS started_at
               FROM merlin_storage.assessment_campaigns AS campaign
               LEFT JOIN merlin_storage.campaign_waves AS wave
                 ON wave.campaign_id = campaign.id
               WHERE campaign.id = %s
               GROUP BY campaign.id, campaign.manifest""",
            (campaign_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown campaign {campaign_id!r}")
        limit = _campaign_budget(row["manifest"])["max_seconds"]
        if limit is None:
            return None
        started_at = row["started_at"]
        if started_at is None:
            return float(limit)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        return max(0.0, float(limit) - (datetime.now(UTC) - started_at).total_seconds())

    def list(self, session_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT * FROM merlin_storage.assessment_campaigns
               WHERE session_id = %s ORDER BY updated_at DESC, id DESC LIMIT %s""",
            (session_id, limit),
        ).fetchall()
        return [self._row(row) for row in rows]

    def list_all(self, *, limit: int | None = 50) -> list[dict[str, Any]]:
        query = """SELECT * FROM merlin_storage.assessment_campaigns
                   ORDER BY updated_at DESC, id DESC"""
        rows = self.connection.execute(
            query + (" LIMIT %s" if limit is not None else ""),
            (limit,) if limit is not None else (),
        ).fetchall()
        return [self._row(row) for row in rows]

    def list_executing(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT * FROM merlin_storage.assessment_campaigns
               WHERE status = 'executing' ORDER BY updated_at, id"""
        ).fetchall()
        return [self._row(row) for row in rows]

    def list_terminal_active_schedules(self) -> list[dict[str, Any]]:
        """Find terminal campaigns whose worker schedule was not fenced atomically."""
        rows = self.connection.execute(
            """SELECT campaign.*
               FROM merlin_storage.assessment_campaigns AS campaign
               JOIN merlin_storage.scheduler_schedules AS schedule
                 ON schedule.id = campaign.schedule_id
               WHERE campaign.status IN ('completed', 'cancelled', 'failed')
                 AND schedule.status IN ('running', 'paused')
               ORDER BY campaign.updated_at, campaign.id"""
        ).fetchall()
        return [self._row(row) for row in rows]

    def list_report_retryable(self) -> list[dict[str, Any]]:
        """Return reportable terminal campaigns with no durable report revision.

        This includes the crash window after campaign terminalization and before the
        report runner starts.  Requiring a journaled ``report_error`` made that window
        permanently invisible to recovery because the process had not lived long
        enough to record the error.
        """
        rows = self.connection.execute(
            """SELECT campaign.*
               FROM merlin_storage.assessment_campaigns AS campaign
               WHERE campaign.status IN ('completed', 'cancelled', 'failed')
                 AND campaign.manifest ? 'manifest'
                 AND campaign.manifest ? 'assignment_context'
                 AND campaign.manifest->'manifest' ? 'objective'
                 AND NOT EXISTS (
                     SELECT 1 FROM merlin_storage.report_revisions AS report
                     WHERE report.campaign_id = campaign.id
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM merlin_storage.campaign_lead_journal AS journal
                     WHERE journal.campaign_id = campaign.id
                       AND journal.payload ? 'report_error'
                       AND journal.payload @> '{"retryable": false}'::jsonb
                 )
               ORDER BY campaign.updated_at, campaign.id"""
        ).fetchall()
        return [self._row(row) for row in rows]

    def list_active(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT * FROM merlin_storage.assessment_campaigns
               WHERE session_id = %s AND status IN ('prepared', 'executing', 'paused')
               ORDER BY updated_at, id""",
            (session_id,),
        ).fetchall()
        return [self._row(row) for row in rows]

    def list_active_all(self) -> list[dict[str, Any]]:
        """Return every campaign with work that can still be stopped, across clients."""
        rows = self.connection.execute(
            """SELECT * FROM merlin_storage.assessment_campaigns
               WHERE status IN ('prepared', 'executing', 'paused')
                  OR EXISTS (
                      SELECT 1 FROM merlin_storage.campaign_waves
                      WHERE campaign_id = assessment_campaigns.id AND status = 'running'
                  )
               ORDER BY updated_at, id"""
        ).fetchall()
        return [self._row(row) for row in rows]

    def pause(self, campaign_id: str) -> dict[str, Any]:
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.assessment_campaigns
                   SET status = 'paused', updated_at = now()
                   WHERE id = %s AND status = 'executing' RETURNING *""",
                (campaign_id,),
            ).fetchone()
            if row is None:
                raise ValueError("campaign is unknown or not executing")
            self.connection.execute(
                """UPDATE merlin_storage.campaign_waves
                   SET status = 'paused', updated_at = now()
                   WHERE campaign_id = %s AND status = 'running'""",
                (campaign_id,),
            )
        return self._row(row)

    def resume(self, campaign_id: str) -> dict[str, Any]:
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.assessment_campaigns
                   SET status = 'executing', updated_at = now()
                   WHERE id = %s AND status = 'paused' RETURNING *""",
                (campaign_id,),
            ).fetchone()
            if row is None:
                raise ValueError("campaign is unknown or not paused")
            self.connection.execute(
                """UPDATE merlin_storage.campaign_waves
                   SET status = 'running', updated_at = now()
                   WHERE campaign_id = %s AND status = 'paused'""",
                (campaign_id,),
            )
        return self._row(row)

    def retry_incomplete(self, campaign_id: str) -> dict[str, Any]:
        """Reopen a failed campaign without rewriting its completed waves."""
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.assessment_campaigns
                   SET status = 'executing', stop_reason = NULL, updated_at = now()
                   WHERE id = %s AND status = 'failed' AND stop_reason = 'failed'
                   RETURNING *""",
                (campaign_id,),
            ).fetchone()
        if row is None:
            raise ValueError("campaign is not a retryable incomplete campaign")
        return self._row(row)

    def list_waves(self, campaign_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """SELECT * FROM merlin_storage.campaign_waves
                   WHERE campaign_id = %s ORDER BY wave""",
                (campaign_id,),
            ).fetchall()
        ]

    def begin_execution(
        self, campaign_id: str, manifest_digest: str, schedule_id: str
    ) -> dict[str, Any]:
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.assessment_campaigns
                   SET status = 'executing', schedule_id = %s, updated_at = now()
                   WHERE id = %s AND status = 'prepared' AND manifest_digest = %s
                   RETURNING *""",
                (schedule_id, campaign_id, manifest_digest),
            ).fetchone()
            if row is not None:
                self.connection.execute(
                    """INSERT INTO merlin_storage.campaign_waves
                           (campaign_id, wave, kind, schedule_id, status)
                       VALUES (%s, 0, 'initial', %s, 'running')""",
                    (campaign_id, schedule_id),
                )
        if row is None:
            raise ValueError("campaign is not prepared or its reviewed digest changed")
        return self._row(row)

    def finish(
        self, campaign_id: str, status: str, *, stop_reason: str | None = None
    ) -> dict[str, Any]:
        if status not in {"completed", "cancelled", "failed"}:
            raise ValueError("invalid terminal campaign status")
        stop_reason = (
            stop_reason
            or {
                "completed": "saturated",
                "cancelled": "operator_stopped",
                "failed": "failed",
            }[status]
        )
        if stop_reason not in {
            "saturated",
            "completed_with_limitations",
            "capped",
            "failed",
            "interrupted",
            "operator_stopped",
        }:
            raise ValueError("invalid campaign stop reason")
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.assessment_campaigns
                   SET status = %s, stop_reason = %s, updated_at = now()
                   WHERE id = %s AND status IN ('prepared', 'executing', 'paused') RETURNING *""",
                (status, stop_reason, campaign_id),
            ).fetchone()
        if row is None:
            raise ValueError("campaign is not executing")
        return self._row(row)

    def record_observation(self, observation: CampaignObservation) -> dict[str, Any]:
        """Append a final task observation; replay returns the original immutable row."""
        with self.connection.transaction():
            row = self.connection.execute(
                """INSERT INTO merlin_storage.campaign_observations
                       (id, campaign_id, task_id, target_id, technique_id, outcome,
                        absence_authority, summary, coverage, witnesses)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (campaign_id, task_id, target_id, technique_id) DO UPDATE
                   SET id = merlin_storage.campaign_observations.id
                   RETURNING *""",
                (
                    observation.id,
                    observation.campaign_id,
                    observation.task_id,
                    observation.target_id,
                    observation.technique_id,
                    observation.outcome,
                    observation.absence_authority,
                    observation.summary,
                    observation.coverage,
                    Jsonb([item.model_dump(mode="json") for item in observation.witnesses]),
                ),
            ).fetchone()
        return dict(row)

    def list_observations(self, campaign_id: str) -> list[CampaignObservation]:
        rows = self.connection.execute(
            """SELECT * FROM merlin_storage.campaign_observations
               WHERE campaign_id = %s ORDER BY created_at, id""",
            (campaign_id,),
        ).fetchall()
        return [
            CampaignObservation(
                id=row["id"],
                campaign_id=row["campaign_id"],
                task_id=row["task_id"],
                target_id=row["target_id"],
                technique_id=row["technique_id"],
                outcome=row["outcome"],
                absence_authority=row["absence_authority"],
                summary=row["summary"],
                coverage=row["coverage"],
                witnesses=row["witnesses"],
            )
            for row in rows
        ]

    def add_wave(self, campaign_id: str, wave: int, kind: str, schedule_id: str) -> dict[str, Any]:
        with self.connection.transaction():
            row = self.connection.execute(
                """INSERT INTO merlin_storage.campaign_waves
                       (campaign_id, wave, kind, schedule_id, status)
                   VALUES (%s, %s, %s, %s, 'running')
                   ON CONFLICT (campaign_id, wave) DO NOTHING RETURNING *""",
                (campaign_id, wave, kind, schedule_id),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    """SELECT * FROM merlin_storage.campaign_waves
                       WHERE campaign_id = %s AND wave = %s""",
                    (campaign_id, wave),
                ).fetchone()
                if row is None or row["kind"] != kind or str(row["schedule_id"]) != schedule_id:
                    raise ValueError("campaign wave conflicts with deterministic schedule")
        return dict(row)

    def finish_wave(
        self, campaign_id: str, wave: int, status: str, summary: Mapping[str, object]
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid terminal wave status")
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_waves
                   SET status = %s, summary = %s, updated_at = now()
                   WHERE campaign_id = %s AND wave = %s AND status IN ('running', 'paused')
                   RETURNING *""",
                (status, Jsonb(dict(summary)), campaign_id, wave),
            ).fetchone()
        if row is None:
            raise ValueError("campaign wave is not running")
        return dict(row)

    def append_lead(
        self, campaign_id: str, wave: int, payload: Mapping[str, object]
    ) -> dict[str, Any]:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = "sha256:" + sha256(body.encode()).hexdigest()
        with self.connection.transaction():
            row = self.connection.execute(
                """INSERT INTO merlin_storage.campaign_lead_journal
                       (campaign_id, wave, payload, payload_digest)
                   VALUES (%s, %s, %s, %s) RETURNING *""",
                (campaign_id, wave, Jsonb(dict(payload)), digest),
            ).fetchone()
        return dict(row)

    def list_lead(self, campaign_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """SELECT * FROM merlin_storage.campaign_lead_journal
                   WHERE campaign_id = %s ORDER BY sequence""",
                (campaign_id,),
            ).fetchall()
        ]

    def record_worker_message(self, message: Mapping[str, object]) -> dict[str, Any]:
        row = self.connection.execute(
            """INSERT INTO merlin_storage.campaign_worker_messages
                   (id, campaign_id, wave, source_task_id, destination, content, content_digest,
                    kind, related_task_id, requested_profile_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id
               RETURNING *""",
            (
                *(
                    message[key]
                    for key in (
                        "id",
                        "campaign_id",
                        "wave",
                        "source_task_id",
                        "destination",
                        "content",
                        "content_digest",
                    )
                ),
                message.get("kind", "discovery"),
                message.get("related_task_id"),
                message.get("requested_profile_id"),
            ),
        ).fetchone()
        assert row is not None
        return dict(row)

    def claim_worker_messages(
        self, campaign_id: str, claimant_id: str, lease_seconds: float
    ) -> list[dict[str, Any]]:
        if not claimant_id.strip() or lease_seconds <= 0:
            raise ValueError("lead claim identity and lease must be positive")
        with self.connection.transaction():
            claim_id = uuid4()
            rows = self.connection.execute(
                """WITH selected AS (
                       SELECT id FROM merlin_storage.campaign_worker_messages
                       WHERE campaign_id = %s AND lead_acknowledged_at IS NULL
                         AND destination IN ('lead', 'lead_and_peers')
                         AND (lead_claim_id IS NULL OR lead_claim_expires_at <= now())
                       ORDER BY created_at, id FOR UPDATE SKIP LOCKED
                   )
                   UPDATE merlin_storage.campaign_worker_messages AS message
                   SET lead_claim_id = %s, lead_claimed_by = %s,
                       lead_claim_generation = lead_claim_generation + 1,
                       lead_claim_expires_at = now() + (%s * interval '1 second')
                   FROM selected WHERE message.id = selected.id
                   RETURNING message.*""",
                (campaign_id, claim_id, claimant_id, lease_seconds),
            ).fetchall()
        return [dict(row) for row in rows]

    def renew_worker_message_claim(
        self, claim_id: str, claimant_id: str, lease_seconds: float
    ) -> int:
        if lease_seconds <= 0:
            raise ValueError("lead claim lease must be positive")
        rows = self.connection.execute(
            """UPDATE merlin_storage.campaign_worker_messages
               SET lead_claim_expires_at = now() + (%s * interval '1 second')
               WHERE lead_claim_id = %s AND lead_claimed_by = %s
                 AND lead_acknowledged_at IS NULL AND lead_claim_expires_at > now()
               RETURNING id""",
            (lease_seconds, claim_id, claimant_id),
        ).fetchall()
        return len(rows)

    def commit_lead_decision(
        self,
        campaign_id: str,
        wave: int,
        decision_id: str,
        payload: Mapping[str, object],
        directives: tuple[Mapping[str, object], ...],
        *,
        claim_id: str | None = None,
        claimant_id: str | None = None,
    ) -> dict[str, Any]:
        if not decision_id.strip():
            raise ValueError("lead decision identity must not be blank")
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = "sha256:" + sha256(body.encode()).hexdigest()
        with self.connection.transaction():
            source_ids: tuple[str, ...] = ()
            if claim_id is not None:
                rows = self.connection.execute(
                    """SELECT id FROM merlin_storage.campaign_worker_messages
                       WHERE campaign_id = %s AND lead_claim_id = %s
                         AND lead_claimed_by = %s AND lead_acknowledged_at IS NULL
                         AND lead_claim_expires_at > now()
                       ORDER BY created_at, id FOR UPDATE""",
                    (campaign_id, claim_id, claimant_id),
                ).fetchall()
                if not rows:
                    raise ValueError("lead worker-message claim is stale or empty")
                source_ids = tuple(str(row["id"]) for row in rows)
            journal = self.connection.execute(
                """INSERT INTO merlin_storage.campaign_lead_journal
                       (campaign_id, wave, payload, payload_digest, decision_id)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (campaign_id, decision_id) WHERE decision_id IS NOT NULL
                   DO UPDATE SET decision_id = EXCLUDED.decision_id
                   RETURNING *""",
                (campaign_id, wave, Jsonb(dict(payload)), digest, decision_id),
            ).fetchone()
            assert journal is not None
            if journal["payload_digest"] != digest:
                raise ValueError("lead decision identity conflicts with different payload")
            for ordinal, directive in enumerate(directives):
                directive_payload = dict(directive)
                directive_body = json.dumps(
                    directive_payload, sort_keys=True, separators=(",", ":")
                )
                directive_digest = "sha256:" + sha256(directive_body.encode()).hexdigest()
                directive_id = f"{decision_id}/{ordinal}"
                existing = self.connection.execute(
                    """INSERT INTO merlin_storage.campaign_lead_directives
                           (id, campaign_id, wave, decision_id, ordinal, action,
                            source_node_id, destination_node_id, source_message_ids,
                            payload, payload_digest)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id
                       RETURNING *""",
                    (
                        directive_id,
                        campaign_id,
                        wave,
                        decision_id,
                        ordinal,
                        directive_payload["action"],
                        directive_payload["source_node_id"],
                        directive_payload.get("destination_node_id"),
                        list(source_ids),
                        Jsonb(directive_payload),
                        directive_digest,
                    ),
                ).fetchone()
                if existing["payload_digest"] != directive_digest:
                    raise ValueError("lead directive identity conflicts with different payload")
            if source_ids:
                acknowledged = self.connection.execute(
                    """UPDATE merlin_storage.campaign_worker_messages
                       SET lead_acknowledged_at = now()
                       WHERE id = ANY(%s) AND lead_claim_id = %s AND lead_claimed_by = %s
                         AND lead_claim_expires_at > now() AND lead_acknowledged_at IS NULL
                       RETURNING id""",
                    (list(source_ids), claim_id, claimant_id),
                ).fetchall()
                if len(acknowledged) != len(source_ids):
                    raise ValueError("lead worker-message claim became stale before commit")
        return dict(journal)

    def list_unapplied_directives(
        self, campaign_id: str, wave: int | None = None
    ) -> list[dict[str, Any]]:
        wave_clause = "" if wave is None else " AND wave = %s"
        params: tuple[object, ...] = (campaign_id,) if wave is None else (campaign_id, wave)
        return [
            dict(row)
            for row in self.connection.execute(
                f"""SELECT * FROM merlin_storage.campaign_lead_directives
                   WHERE campaign_id = %s AND state IN ('planned', 'failed')
                     {wave_clause}
                   ORDER BY created_at, id""",
                params,
            ).fetchall()
        ]

    def list_directives(self, campaign_id: str, wave: int | None = None) -> list[dict[str, Any]]:
        wave_clause = "" if wave is None else " AND wave = %s"
        params: tuple[object, ...] = (campaign_id,) if wave is None else (campaign_id, wave)
        return [
            dict(row)
            for row in self.connection.execute(
                f"""SELECT * FROM merlin_storage.campaign_lead_directives
                    WHERE campaign_id = %s {wave_clause}
                    ORDER BY created_at, id""",
                params,
            ).fetchall()
        ]

    def mark_directive_applied(self, directive_id: str, effect_reference: str) -> dict[str, Any]:
        if not effect_reference.strip():
            raise ValueError("directive effect reference must not be blank")
        with self.connection.transaction():
            current = self.connection.execute(
                "SELECT * FROM merlin_storage.campaign_lead_directives WHERE id = %s FOR UPDATE",
                (directive_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown lead directive: {directive_id}")
            if current["state"] == "applied":
                if current["effect_reference"] != effect_reference:
                    raise ValueError("directive already has a different applied effect receipt")
                return dict(current)
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_lead_directives
                   SET state = 'applied', effect_reference = %s, failure = NULL,
                       applied_at = now(), updated_at = now()
                   WHERE id = %s RETURNING *""",
                (effect_reference, directive_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown lead directive: {directive_id}")
        return dict(row)

    def mark_directive_failed(self, directive_id: str, failure: str) -> dict[str, Any]:
        row = self.connection.execute(
            """UPDATE merlin_storage.campaign_lead_directives
               SET state = 'failed', failure = %s, updated_at = now()
               WHERE id = %s AND state != 'applied' RETURNING *""",
            (failure[:4096], directive_id),
        ).fetchone()
        if row is None:
            current = self.connection.execute(
                "SELECT * FROM merlin_storage.campaign_lead_directives WHERE id = %s",
                (directive_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown lead directive: {directive_id}")
            return dict(current)
        return dict(row)

    def list_pending_worker_messages(self, campaign_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """SELECT * FROM merlin_storage.campaign_worker_messages
                   WHERE campaign_id = %s
                     AND lead_acknowledged_at IS NULL
                     AND destination IN ('lead', 'lead_and_peers')
                   ORDER BY created_at, id""",
                (campaign_id,),
            ).fetchall()
        ]

    def list_acknowledged_worker_messages(self, campaign_id: str) -> list[dict[str, Any]]:
        """Return Lead-acknowledged discovery rows eligible for the wave target brief."""
        return [
            dict(row)
            for row in self.connection.execute(
                """SELECT * FROM merlin_storage.campaign_worker_messages
                   WHERE campaign_id = %s
                     AND lead_acknowledged_at IS NOT NULL
                     AND kind = 'discovery'
                   ORDER BY created_at, id""",
                (campaign_id,),
            ).fetchall()
        ]

    def acknowledge_worker_messages(self, message_ids: tuple[str, ...]) -> int:
        if not message_ids:
            return 0
        rows = self.connection.execute(
            """UPDATE merlin_storage.campaign_worker_messages
               SET lead_acknowledged_at = now()
               WHERE id = ANY(%s) AND lead_acknowledged_at IS NULL
               RETURNING id""",
            (list(message_ids),),
        ).fetchall()
        return len(rows)

    def record_lesson(self, lesson: Mapping[str, object]) -> None:
        normalized = dict(lesson)
        normalized["semantic_id"] = normalized.get("semantic_id") or normalized.get("technique_id")
        normalized["strategy_id"] = normalized.get("strategy_id") or "direct"
        self.connection.execute(
            """INSERT INTO merlin_storage.campaign_lessons
                   (id, campaign_id, session_id, wave, task_id, profile_id, target_id,
                    outcome, content, evidence_digest, technique_id, attempt_identity,
                    target_fingerprint, adapter_fingerprint, surface, transform_id, language,
                    oracle_digest, scenario_fingerprint, semantic_id, strategy_id, valid_until)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, now() + (%s * interval '1 second'))
               ON CONFLICT (campaign_id, wave, task_id, technique_id) DO NOTHING""",
            tuple(
                normalized[key]
                for key in (
                    "id",
                    "campaign_id",
                    "session_id",
                    "wave",
                    "task_id",
                    "profile_id",
                    "target_id",
                    "outcome",
                    "content",
                    "evidence_digest",
                    "technique_id",
                    "attempt_identity",
                    "target_fingerprint",
                    "adapter_fingerprint",
                    "surface",
                    "transform_id",
                    "language",
                    "oracle_digest",
                    "scenario_fingerprint",
                    "semantic_id",
                    "strategy_id",
                    "validity_seconds",
                )
            ),
        )

    def list_attempt_history(
        self, target_id: str, technique_ids: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        if not technique_ids:
            return []
        return [
            dict(row)
            for row in self.connection.execute(
                """SELECT DISTINCT ON (scenario_fingerprint, technique_id) *
                   FROM merlin_storage.campaign_lessons
                   WHERE target_id = %s AND technique_id = ANY(%s)
                   ORDER BY scenario_fingerprint, technique_id, created_at DESC, id DESC""",
                (target_id, list(technique_ids)),
            ).fetchall()
        ]

    def list_lessons(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """SELECT * FROM merlin_storage.campaign_lessons
                   WHERE session_id = %s ORDER BY created_at DESC, id LIMIT %s""",
                (session_id, limit),
            ).fetchall()
        ]

    def record_recon_result(self, result: Mapping[str, object]) -> None:
        """Persist a sealed reconnaissance applicability partition for reuse."""
        with self.connection.transaction():
            self.connection.execute(
                """INSERT INTO merlin_storage.campaign_recon_results
                       (id, target_id, scenario_fingerprint, result, result_digest)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO NOTHING""",
                (
                    result["id"],
                    result["target_id"],
                    result["scenario_fingerprint"],
                    Jsonb(dict(result["result"])),
                    result["result_digest"],
                ),
            )

    def latest_recon_result(
        self, target_id: str, scenario_fingerprint: str
    ) -> dict[str, Any] | None:
        """Return the newest persisted recon result for a reviewed scenario."""
        row = self.connection.execute(
            """SELECT * FROM merlin_storage.campaign_recon_results
               WHERE target_id = %s AND scenario_fingerprint = %s
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (target_id, scenario_fingerprint),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_capability_observations(self, record: Mapping[str, object]) -> None:
        """Persist conclusive capability probe observations for later reuse."""
        with self.connection.transaction():
            self.connection.execute(
                """INSERT INTO merlin_storage.campaign_capability_observations
                       (id, target_id, adapter_digest, plan_digest, results, results_digest)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO NOTHING""",
                (
                    record["id"],
                    record["target_id"],
                    record["adapter_digest"],
                    record["plan_digest"],
                    Jsonb(list(record["results"])),
                    record["results_digest"],
                ),
            )

    def latest_capability_observations(
        self, target_id: str, adapter_digest: str, plan_digest: str
    ) -> dict[str, Any] | None:
        """Return the newest persisted capability observations for a reviewed adapter."""
        row = self.connection.execute(
            """SELECT * FROM merlin_storage.campaign_capability_observations
               WHERE target_id = %s AND adapter_digest = %s AND plan_digest = %s
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (target_id, adapter_digest, plan_digest),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_exploration_critic_decision(self, decision: Mapping[str, object]) -> dict[str, Any]:
        """Append one agent-authored critic judgment with idempotent identity."""
        payload = ExplorationCriticDecision.from_mapping(decision).projection()
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = "sha256:" + sha256(body.encode()).hexdigest()
        with self.connection.transaction():
            row = self.connection.execute(
                """INSERT INTO merlin_storage.exploration_critic_decisions
                       (id, campaign_id, hypothesis_id, verdict, decision, decision_digest)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO NOTHING
                   RETURNING *""",
                (
                    str(payload["id"]),
                    str(payload["campaign_id"]),
                    str(payload["hypothesis_id"]),
                    str(payload["verdict"]),
                    Jsonb(payload),
                    digest,
                ),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    """SELECT * FROM merlin_storage.exploration_critic_decisions
                       WHERE id = %s""",
                    (str(payload["id"]),),
                ).fetchone()
            assert row is not None
            if row["decision_digest"] != digest:
                raise ValueError("critic decision identity conflicts with different payload")
        return dict(row)

    def list_exploration_critic_decisions(
        self,
        campaign_id: str,
        *,
        hypothesis_id: str | None = None,
        verdict: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query the append-only critic ledger without interpreting its semantics."""
        if limit < 1:
            raise ValueError("critic decision query limit must be positive")
        filters = ["campaign_id = %s"]
        parameters: list[object] = [campaign_id]
        if hypothesis_id is not None:
            filters.append("hypothesis_id = %s")
            parameters.append(hypothesis_id)
        if verdict is not None:
            filters.append("verdict = %s")
            parameters.append(verdict)
        parameters.append(limit)
        rows = self.connection.execute(
            """SELECT * FROM merlin_storage.exploration_critic_decisions
               WHERE """
            + " AND ".join(filters)
            + " ORDER BY decided_at, id LIMIT %s",
            tuple(parameters),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_interaction_lesson(
        self, lesson: VerifiedInteractionLesson
    ) -> VerifiedInteractionLesson:
        """Admit only a structured, externally verified interaction recipe."""
        return InteractionLessonRepository(self.connection).save(lesson)

    def record_dynamic_specialist(self, record: Mapping[str, object]) -> dict[str, Any]:
        """Append an admitted dynamic specialist; replay returns the original row."""
        with self.connection.transaction():
            row = self.connection.execute(
                """INSERT INTO merlin_storage.campaign_dynamic_specialists
                       (id, campaign_id, state, admitted_wave, ttl_waves,
                        proposal_digest, admitted_digest, record)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO NOTHING
                   RETURNING *""",
                (
                    str(record["id"]),
                    str(record["campaign_id"]),
                    str(record["state"]),
                    int(record["admitted_wave"]),  # type: ignore[arg-type]
                    (
                        None if record.get("ttl_waves") is None else int(record["ttl_waves"])  # type: ignore[arg-type]
                    ),
                    str(record["proposal_digest"]),
                    str(record["admitted_digest"]),
                    Jsonb(dict(record)),
                ),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    """SELECT * FROM merlin_storage.campaign_dynamic_specialists
                       WHERE id = %s""",
                    (str(record["id"]),),
                ).fetchone()
            assert row is not None
            if row["admitted_digest"] != str(record["admitted_digest"]):
                raise ValueError("dynamic specialist identity conflicts with different record")
        return dict(row)

    def list_dynamic_specialists(self, campaign_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT * FROM merlin_storage.campaign_dynamic_specialists
               WHERE campaign_id = %s ORDER BY created_at, id""",
            (campaign_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def retire_dynamic_specialist(self, record: Mapping[str, object]) -> dict[str, Any]:
        """Transition an active specialist to retired; replaying is a no-op."""
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_dynamic_specialists
                   SET state = 'retired', retired_wave = %s, retirement_reason = %s,
                       record = %s, updated_at = now()
                   WHERE id = %s AND campaign_id = %s AND state = 'active'
                   RETURNING *""",
                (
                    int(record["retired_wave"]),  # type: ignore[arg-type]
                    str(record["retirement_reason"]),
                    Jsonb(dict(record)),
                    str(record["id"]),
                    str(record["campaign_id"]),
                ),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    """SELECT * FROM merlin_storage.campaign_dynamic_specialists
                       WHERE id = %s""",
                    (str(record["id"]),),
                ).fetchone()
            if row is None:
                raise ValueError("unknown dynamic specialist")
            if row["state"] != "retired" or (
                row["retired_wave"] != int(record["retired_wave"])  # type: ignore[arg-type]
                or row["retirement_reason"] != str(record["retirement_reason"])
            ):
                raise ValueError("dynamic specialist retirement conflicts with the durable record")
        return dict(row)

    def list_interaction_lessons(
        self, target_id: str, *, interface_key: str | None = None, limit: int = 20
    ) -> tuple[VerifiedInteractionLesson, ...]:
        """Retrieve reusable recipes scoped to the exact target identity."""
        return InteractionLessonRepository(self.connection).for_target(
            target_id, interface_key=interface_key, limit=limit
        )

    @staticmethod
    def _row(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "id": str(row["id"]),
            "session_id": str(row["session_id"]),
            "schedule_id": None if row.get("schedule_id") is None else str(row["schedule_id"]),
        }


def _campaign_budget(value: object) -> dict[str, int | float | None]:
    if not isinstance(value, Mapping):
        raise ValueError("campaign manifest does not contain a reviewed budget")
    manifest = value.get("manifest", value)
    budget = manifest.get("budget") if isinstance(manifest, Mapping) else None
    if not isinstance(budget, Mapping):
        raise ValueError("campaign manifest does not contain a reviewed budget")
    result: dict[str, int | float | None] = {}
    for key in ("max_tokens", "max_turns", "max_seconds", "max_cost_usd"):
        item = budget.get(key)
        if item is not None and (
            not isinstance(item, (int, float)) or isinstance(item, bool) or item <= 0
        ):
            raise ValueError("campaign manifest contains an invalid reviewed budget")
        result[key] = item
    return result
