from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from functools import wraps
from hashlib import sha256
from threading import RLock
from typing import Any
from uuid import uuid4

from merlin_agents import ExplorationCriticDecision
from merlin_contracts import CampaignObservation, CampaignTerminalReason
from merlin_contracts.campaign_lifecycle import require_campaign_terminal_reason
from merlin_contracts.state_scope import OpaqueIdentityReference
from merlin_memory import VerifiedInteractionLesson
from psycopg.types.json import Jsonb

from .interaction_store import InteractionLessonRepository

_MAX_FRONTIER_SHORTLIST = 1024
_MAX_FRONTIER_COMPLETION_PAGE = 500
_MAX_FRONTIER_COMPLETION_HISTORY_PAGE = 100
_FRONTIER_COMPLETION_STATES = ("queued", "claimed", "scheduled", "terminal")
_FRONTIER_COMPLETION_OUTCOMES = (
    "confirmed",
    "refuted",
    "partial",
    "unclear",
    "blocked",
    "retryable",
    "unattempted",
)

_LEAD_DIRECTIVE_ACTIONS = frozenset({"redirect", "handoff", "cancel"})


def _validated_lead_directive(directive: Mapping[str, object]) -> dict[str, object]:
    """Validate and normalize a durable directive before committing its identity."""

    payload = dict(directive)
    action = str(payload.get("action") or "").strip()
    if action not in _LEAD_DIRECTIVE_ACTIONS:
        raise ValueError(f"unsupported lead directive action: {action or '<blank>'}")
    source_node_id = str(payload.get("source_node_id") or "").strip()
    if not source_node_id:
        raise ValueError("lead directive source worker must not be blank")
    payload["action"] = action
    payload["source_node_id"] = source_node_id

    destination = str(payload.get("destination_node_id") or "").strip()
    if action == "handoff":
        if not destination:
            raise ValueError("handoff directive destination worker must not be blank")
        if destination == source_node_id:
            raise ValueError("handoff directive destination must differ from its source")
        payload["destination_node_id"] = destination

    objective = str(payload.get("objective") or payload.get("instruction") or "").strip()
    if action in {"redirect", "handoff"}:
        if not objective:
            raise ValueError(f"{action} directive objective must not be blank")
        payload["objective"] = objective

    ttl_turns = payload.get("temporary_ttl_turns")
    if ttl_turns is not None:
        if action != "redirect":
            raise ValueError("temporary directive TTL is only valid for redirect")
        if not isinstance(ttl_turns, int) or isinstance(ttl_turns, bool) or not 1 <= ttl_turns <= 8:
            raise ValueError("temporary directive TTL must be 1-8 worker turns")
        if not str(payload.get("evidence_expectation") or "").strip():
            raise ValueError("temporary directive must include an evidence expectation")

    return payload


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

    def prepare_with_frontier(
        self,
        campaign_id: str,
        session_id: str,
        manifest: Mapping[str, object],
        manifest_digest: str,
        families: Sequence[Mapping[str, object]],
        candidates: Sequence[Mapping[str, object]],
    ) -> dict[str, Any]:
        """Atomically persist a reviewed campaign and its complete logical frontier."""

        normalized_manifest = dict(manifest)
        with self.connection.transaction():
            self.connection.execute(
                """INSERT INTO merlin_storage.assessment_campaigns
                       (id, session_id, manifest, manifest_digest, status)
                   VALUES (%s, %s, %s, %s, 'prepared')
                   ON CONFLICT (id) DO NOTHING""",
                (campaign_id, session_id, Jsonb(normalized_manifest), manifest_digest),
            )
            row = self.connection.execute(
                """SELECT * FROM merlin_storage.assessment_campaigns
                   WHERE id = %s FOR UPDATE""",
                (campaign_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("prepared campaign did not persist")
            identity = (
                str(row["session_id"]),
                dict(row["manifest"]),
                str(row["manifest_digest"]),
            )
            if identity != (session_id, normalized_manifest, manifest_digest):
                raise ValueError("prepared campaign identity conflicts with durable state")
            if str(row["status"]) != "prepared":
                raise ValueError("campaign frontier can only be registered while prepared")
            counts = self.register_frontier(campaign_id, families, candidates)
            result = self._row(row)
            result["frontier_counts"] = counts
            return result

    def register_frontier(
        self,
        campaign_id: str,
        families: Sequence[Mapping[str, object]],
        candidates: Sequence[Mapping[str, object]],
    ) -> dict[str, int]:
        """Idempotently register a normalized campaign execution frontier.

        Family documents hold the shared technique definition once. Candidate
        rows contain only the stable concrete identity and a small variant
        overlay, so a large language frontier never has to become one JSONB
        document or one scheduler graph.
        """

        family_values: dict[str, tuple[dict[str, object], str]] = {}
        for family in families:
            family_id = str(family.get("id") or "").strip()
            document = family.get("document")
            if not family_id or not isinstance(document, Mapping):
                raise ValueError("frontier families require an id and document mapping")
            normalized = dict(document)
            digest = sha256(
                json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            prior = family_values.setdefault(family_id, (normalized, digest))
            if prior[1] != digest:
                raise ValueError(f"frontier family {family_id!r} was registered twice differently")

        candidate_values: dict[str, dict[str, object]] = {}
        ordinals: set[int] = set()
        identities: set[tuple[str, str, str]] = set()
        for candidate in candidates:
            candidate_id = str(candidate.get("id") or "").strip()
            family_id = str(candidate.get("family_id") or "").strip()
            language = str(candidate.get("requested_language") or "").strip()
            variant_id = str(candidate.get("variant_id") or "").strip()
            if not all((candidate_id, family_id, language, variant_id)):
                raise ValueError(
                    "frontier candidates require id, family_id, requested_language, and variant_id"
                )
            if family_id not in family_values:
                raise ValueError(f"frontier candidate references unknown family {family_id!r}")
            try:
                ordinal = int(candidate["ordinal"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    "frontier candidate ordinal must be a non-negative integer"
                ) from error
            if ordinal < 0:
                raise ValueError("frontier candidate ordinal must be a non-negative integer")
            variant = candidate.get("variant", {})
            if not isinstance(variant, Mapping):
                raise ValueError("frontier candidate variant must be a mapping")
            priority_raw = candidate.get("priority")
            if priority_raw is not None and (
                isinstance(priority_raw, bool) or not isinstance(priority_raw, (int, float))
            ):
                raise ValueError("frontier candidate priority must be numeric")
            state = str(candidate.get("state") or "queued")
            if state not in {"queued", "dormant"}:
                raise ValueError("new frontier candidates must be queued or dormant")
            if candidate_id in candidate_values:
                raise ValueError(f"frontier candidate {candidate_id!r} was registered twice")
            identity = (family_id, language, variant_id)
            if ordinal in ordinals or identity in identities:
                raise ValueError("frontier candidate ordinal and family variant must be unique")
            ordinals.add(ordinal)
            identities.add(identity)
            candidate_values[candidate_id] = {
                "id": candidate_id,
                "family_id": family_id,
                "ordinal": ordinal,
                "requested_language": language,
                "variant_id": variant_id,
                "variant": dict(variant),
                "priority": None if priority_raw is None else float(priority_raw),
                "state": state,
            }

        with self.connection.transaction():
            campaign = self.connection.execute(
                "SELECT id FROM merlin_storage.assessment_campaigns WHERE id = %s FOR UPDATE",
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                raise KeyError(campaign_id)
            for family_id, (document, digest) in family_values.items():
                self.connection.execute(
                    """INSERT INTO merlin_storage.campaign_frontier_families
                           (campaign_id, id, document, document_digest)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (campaign_id, id) DO NOTHING""",
                    (campaign_id, family_id, Jsonb(document), digest),
                )
                stored = self.connection.execute(
                    """SELECT document_digest
                       FROM merlin_storage.campaign_frontier_families
                       WHERE campaign_id = %s AND id = %s""",
                    (campaign_id, family_id),
                ).fetchone()
                if stored is None or str(stored["document_digest"]) != digest:
                    raise ValueError(f"frontier family {family_id!r} conflicts with durable state")
            if candidate_values:
                self.connection.execute(
                    """WITH incoming AS (
                           SELECT * FROM jsonb_to_recordset(%s::jsonb) AS item(
                               id text, family_id text, ordinal bigint,
                               requested_language text, variant_id text, variant jsonb,
                               priority double precision, state text
                           )
                       )
                       INSERT INTO merlin_storage.campaign_frontier_candidates
                           (campaign_id, id, family_id, ordinal, requested_language,
                            variant_id, variant, priority, state)
                       SELECT %s, id, family_id, ordinal, requested_language,
                              variant_id, variant, priority, state
                       FROM incoming
                       ON CONFLICT (campaign_id, id) DO NOTHING""",
                    (Jsonb(list(candidate_values.values())), campaign_id),
                )
            stored_rows = self.connection.execute(
                """SELECT id, family_id, ordinal, requested_language, variant_id,
                          variant, priority
                   FROM merlin_storage.campaign_frontier_candidates
                   WHERE campaign_id = %s AND id = ANY(%s)""",
                (campaign_id, list(candidate_values)),
            ).fetchall()
            stored_by_id = {str(row["id"]): row for row in stored_rows}
            for candidate in candidate_values.values():
                stored = stored_by_id.get(str(candidate["id"]))
                expected = (
                    candidate["family_id"],
                    candidate["ordinal"],
                    candidate["requested_language"],
                    candidate["variant_id"],
                    candidate["variant"],
                    candidate["priority"],
                )
                actual = (
                    None
                    if stored is None
                    else (
                        str(stored["family_id"]),
                        int(stored["ordinal"]),
                        str(stored["requested_language"]),
                        str(stored["variant_id"]),
                        dict(stored["variant"]),
                        None if stored["priority"] is None else float(stored["priority"]),
                    )
                )
                if actual != expected:
                    raise ValueError(
                        f"frontier candidate {candidate['id']!r} conflicts with durable state"
                    )
        return self.frontier_counts(campaign_id)

    def register_frontier_child(
        self,
        campaign_id: str,
        parent_candidate_id: str,
        child_id: str,
        variant_id: str,
        variant: Mapping[str, object],
        *,
        reason: str,
        family_document: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        """Append one immutable regenerated child without changing its parent."""

        parent_id = parent_candidate_id.strip()
        candidate_id = child_id.strip()
        child_variant_id = variant_id.strip()
        lineage_reason = reason.strip()
        if not all((parent_id, candidate_id, child_variant_id, lineage_reason)):
            raise ValueError("frontier child identity, variant, and reason cannot be empty")
        normalized_family = None if family_document is None else dict(family_document)
        family_digest = (
            None
            if normalized_family is None
            else sha256(
                json.dumps(normalized_family, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        with self.connection.transaction():
            campaign = self.connection.execute(
                "SELECT id FROM merlin_storage.assessment_campaigns WHERE id = %s FOR UPDATE",
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                raise KeyError(campaign_id)
            existing = self.connection.execute(
                """SELECT * FROM merlin_storage.campaign_frontier_candidates
                   WHERE campaign_id = %s AND id = %s""",
                (campaign_id, candidate_id),
            ).fetchone()
            parent = self.connection.execute(
                """SELECT family_id, requested_language, priority
                   FROM merlin_storage.campaign_frontier_candidates
                   WHERE campaign_id = %s AND id = %s""",
                (campaign_id, parent_id),
            ).fetchone()
            if parent is None:
                raise KeyError(parent_candidate_id)
            child_family_id = (
                candidate_id if normalized_family is not None else str(parent["family_id"])
            )
            if normalized_family is not None:
                self.connection.execute(
                    """INSERT INTO merlin_storage.campaign_frontier_families
                           (campaign_id, id, document, document_digest)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (campaign_id, id) DO NOTHING""",
                    (campaign_id, child_family_id, Jsonb(normalized_family), family_digest),
                )
                stored_family = self.connection.execute(
                    """SELECT document, document_digest
                       FROM merlin_storage.campaign_frontier_families
                       WHERE campaign_id = %s AND id = %s""",
                    (campaign_id, child_family_id),
                ).fetchone()
                if stored_family is None or (
                    dict(stored_family["document"]),
                    str(stored_family["document_digest"]),
                ) != (normalized_family, family_digest):
                    raise ValueError("frontier child family conflicts with durable state")
            if existing is None:
                ordinal_row = self.connection.execute(
                    """SELECT coalesce(max(ordinal), -1) + 1 AS ordinal
                       FROM merlin_storage.campaign_frontier_candidates
                       WHERE campaign_id = %s""",
                    (campaign_id,),
                ).fetchone()
                self.connection.execute(
                    """INSERT INTO merlin_storage.campaign_frontier_candidates
                           (campaign_id, id, family_id, ordinal, requested_language,
                            variant_id, variant, priority, state,
                            parent_candidate_id, lineage_reason)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued', %s, %s)""",
                    (
                        campaign_id,
                        candidate_id,
                        child_family_id,
                        int(ordinal_row["ordinal"]),
                        parent["requested_language"],
                        child_variant_id,
                        Jsonb(dict(variant)),
                        parent["priority"],
                        parent_id,
                        lineage_reason,
                    ),
                )
                existing = self.connection.execute(
                    """SELECT * FROM merlin_storage.campaign_frontier_candidates
                       WHERE campaign_id = %s AND id = %s""",
                    (campaign_id, candidate_id),
                ).fetchone()
            expected = (
                child_family_id,
                str(parent["requested_language"]),
                child_variant_id,
                dict(variant),
                parent_id,
                lineage_reason,
            )
            actual = (
                str(existing["family_id"]),
                str(existing["requested_language"]),
                str(existing["variant_id"]),
                dict(existing["variant"]),
                None
                if existing["parent_candidate_id"] is None
                else str(existing["parent_candidate_id"]),
                None if existing["lineage_reason"] is None else str(existing["lineage_reason"]),
            )
            if actual != expected:
                raise ValueError("frontier child identity conflicts with durable state")
            return {**dict(existing), "variant": dict(existing["variant"])}

    def update_frontier_priorities(
        self, campaign_id: str, priorities: Mapping[str, float | None]
    ) -> int:
        """Update advisory ordering scores without changing candidate eligibility."""

        updated = 0
        with self.connection.transaction():
            self.connection.execute(
                "SELECT id FROM merlin_storage.assessment_campaigns WHERE id = %s FOR UPDATE",
                (campaign_id,),
            )
            for candidate_id, priority in priorities.items():
                if priority is not None and (
                    isinstance(priority, bool) or not isinstance(priority, (int, float))
                ):
                    raise ValueError("frontier candidate priority must be numeric")
                cursor = self.connection.execute(
                    """UPDATE merlin_storage.campaign_frontier_candidates
                       SET priority = %s, updated_at = now()
                       WHERE campaign_id = %s AND id = %s
                         AND state IN ('dormant', 'queued')""",
                    (None if priority is None else float(priority), campaign_id, candidate_id),
                )
                updated += cursor.rowcount
        return updated

    def list_frontier_shortlist(
        self,
        campaign_id: str,
        limit: int,
        *,
        after_ordinal: int | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Return a bounded deterministic page of queued candidate references."""

        if not 1 <= limit <= _MAX_FRONTIER_SHORTLIST:
            raise ValueError(
                f"frontier shortlist limit must be between 1 and {_MAX_FRONTIER_SHORTLIST}"
            )
        if after_ordinal is not None and after_ordinal < 0:
            raise ValueError("frontier shortlist cursor must be non-negative")
        rows = self.connection.execute(
            """WITH ranked AS (
                   SELECT id, family_id, ordinal, requested_language, variant_id,
                          variant, priority, claim_generation,
                          row_number() OVER (
                              PARTITION BY family_id
                              ORDER BY claim_generation, ordinal, id
                          ) AS family_rank
                   FROM merlin_storage.campaign_frontier_candidates
                   WHERE campaign_id = %s AND state = 'queued'
                     AND (%s::bigint IS NULL OR ordinal > %s::bigint)
               )
               SELECT id, family_id, ordinal, requested_language, variant_id,
                      variant, priority, claim_generation
               FROM ranked
               ORDER BY claim_generation, family_rank, ordinal, id LIMIT %s""",
            (campaign_id, after_ordinal, after_ordinal, limit),
        ).fetchall()
        return tuple(
            {
                **dict(row),
                "variant": dict(row["variant"]),
                "priority": None if row["priority"] is None else float(row["priority"]),
            }
            for row in rows
        )

    def queue_frontier_candidates(self, campaign_id: str, candidate_ids: Sequence[str]) -> int:
        """Make dormant candidates eligible without changing their durable identity."""

        ids = tuple(dict.fromkeys(str(item) for item in candidate_ids if str(item)))
        if not ids:
            return 0
        with self.connection.transaction():
            self.connection.execute(
                "SELECT id FROM merlin_storage.assessment_campaigns WHERE id = %s FOR UPDATE",
                (campaign_id,),
            )
            cursor = self.connection.execute(
                """UPDATE merlin_storage.campaign_frontier_candidates
                   SET state = 'queued', updated_at = now()
                   WHERE campaign_id = %s AND id = ANY(%s) AND state = 'dormant'""",
                (campaign_id, list(ids)),
            )
        return cursor.rowcount

    def claim_frontier_transition(
        self, campaign_id: str, claimant_id: str, lease_seconds: int
    ) -> str | None:
        """Claim or renew exclusive campaign-frontier transition ownership."""

        claimant = claimant_id.strip()
        if not claimant or lease_seconds < 1:
            raise ValueError("frontier transition claims require a claimant and positive lease")
        claim_id = str(uuid4())
        with self.connection.transaction():
            campaign = self.connection.execute(
                "SELECT id FROM merlin_storage.assessment_campaigns WHERE id = %s FOR UPDATE",
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                raise KeyError(campaign_id)
            row = self.connection.execute(
                """INSERT INTO merlin_storage.campaign_frontier_transition_leases
                       (campaign_id, claim_id, claimant_id, expires_at)
                   VALUES (%s, %s, %s, now() + make_interval(secs => %s))
                   ON CONFLICT (campaign_id) DO UPDATE
                   SET claim_id = CASE
                           WHEN campaign_frontier_transition_leases.claimant_id =
                                EXCLUDED.claimant_id
                             THEN campaign_frontier_transition_leases.claim_id
                           ELSE EXCLUDED.claim_id
                       END,
                       claimant_id = EXCLUDED.claimant_id,
                       expires_at = EXCLUDED.expires_at,
                       updated_at = now()
                   WHERE campaign_frontier_transition_leases.claimant_id = EXCLUDED.claimant_id
                      OR campaign_frontier_transition_leases.expires_at <= now()
                   RETURNING claim_id""",
                (campaign_id, claim_id, claimant, lease_seconds),
            ).fetchone()
            return None if row is None else str(row["claim_id"])

    def renew_frontier_transition(
        self, claim_id: str, claimant_id: str, lease_seconds: int
    ) -> bool:
        """Renew a live transition lease only for its current claimant."""

        claimant = claimant_id.strip()
        if not claim_id.strip() or not claimant or lease_seconds < 1:
            raise ValueError("frontier transition renewal requires a claim, claimant, and lease")
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_frontier_transition_leases
                   SET expires_at = now() + make_interval(secs => %s), updated_at = now()
                   WHERE claim_id = %s AND claimant_id = %s AND expires_at > now()
                   RETURNING claim_id""",
                (lease_seconds, claim_id, claimant),
            ).fetchone()
            return row is not None

    def validate_frontier_transition(self, claim_id: str, claimant_id: str) -> bool:
        """Return whether a claimant still owns an unexpired transition lease."""

        if not claim_id.strip() or not claimant_id.strip():
            return False
        row = self.connection.execute(
            """SELECT 1 FROM merlin_storage.campaign_frontier_transition_leases
               WHERE claim_id = %s AND claimant_id = %s AND expires_at > now()""",
            (claim_id, claimant_id.strip()),
        ).fetchone()
        return row is not None

    def release_frontier_transition(self, claim_id: str, claimant_id: str) -> bool:
        """Release a transition lease only for its current claimant."""

        if not claim_id.strip() or not claimant_id.strip():
            return False
        with self.connection.transaction():
            row = self.connection.execute(
                """DELETE FROM merlin_storage.campaign_frontier_transition_leases
                   WHERE claim_id = %s AND claimant_id = %s RETURNING claim_id""",
                (claim_id, claimant_id.strip()),
            ).fetchone()
            return row is not None

    def claim_frontier_batch(
        self,
        campaign_id: str,
        wave: int,
        batch_id: str,
        limit: int,
        *,
        claim_id: str | None = None,
        claimant_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically claim one deterministic, bounded candidate batch."""

        if wave < 0 or limit < 1 or not batch_id.strip():
            raise ValueError("frontier claims require a non-negative wave, batch id, and limit")
        with self.connection.transaction():
            campaign = self.connection.execute(
                "SELECT id FROM merlin_storage.assessment_campaigns WHERE id = %s FOR UPDATE",
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                raise KeyError(campaign_id)
            self._require_frontier_transition(campaign_id, claim_id, claimant_id)
            existing = self.connection.execute(
                """SELECT id, wave FROM merlin_storage.campaign_frontier_batches
                   WHERE campaign_id = %s AND id = %s""",
                (campaign_id, batch_id),
            ).fetchone()
            if existing is not None:
                if int(existing["wave"]) != wave:
                    raise ValueError("frontier batch id conflicts with a different wave")
                return self._frontier_batch(campaign_id, batch_id)
            active = self.connection.execute(
                """SELECT id FROM merlin_storage.campaign_frontier_batches
                   WHERE campaign_id = %s AND state IN ('claimed', 'scheduled')""",
                (campaign_id,),
            ).fetchone()
            if active is not None:
                raise ValueError(f"campaign already has active frontier batch {active['id']!r}")
            rows = self.connection.execute(
                """SELECT id
                   FROM merlin_storage.campaign_frontier_candidates
                   WHERE campaign_id = %s AND state = 'queued'
                   ORDER BY priority DESC NULLS LAST, ordinal, id
                   LIMIT %s FOR UPDATE""",
                (campaign_id, limit),
            ).fetchall()
            candidate_ids = tuple(str(row["id"]) for row in rows)
            digest = sha256(json.dumps(candidate_ids, separators=(",", ":")).encode()).hexdigest()
            state = "claimed" if candidate_ids else "settled"
            self.connection.execute(
                """INSERT INTO merlin_storage.campaign_frontier_batches
                       (campaign_id, id, wave, state, candidate_digest)
                   VALUES (%s, %s, %s, %s, %s)""",
                (campaign_id, batch_id, wave, state, digest),
            )
            for position, candidate_id in enumerate(candidate_ids):
                self.connection.execute(
                    """INSERT INTO merlin_storage.campaign_frontier_batch_items
                           (campaign_id, batch_id, candidate_id, position)
                       VALUES (%s, %s, %s, %s)""",
                    (campaign_id, batch_id, candidate_id, position),
                )
            if candidate_ids:
                cursor = self.connection.execute(
                    """UPDATE merlin_storage.campaign_frontier_candidates
                       SET state = 'claimed', active_batch_id = %s,
                           claim_generation = claim_generation + 1, updated_at = now()
                       WHERE campaign_id = %s AND id = ANY(%s) AND state = 'queued'""",
                    (batch_id, campaign_id, list(candidate_ids)),
                )
                if cursor.rowcount != len(candidate_ids):
                    raise RuntimeError("frontier claim lost candidate ownership")
            return self._frontier_batch(campaign_id, batch_id)

    def claim_frontier_batch_exact(
        self,
        campaign_id: str,
        wave: int,
        batch_id: str,
        candidate_ids: Sequence[str],
        *,
        claim_id: str | None = None,
        claimant_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically claim an adaptively ranked ordered set of queued candidates."""

        ids = tuple(str(candidate_id).strip() for candidate_id in candidate_ids)
        if (
            wave < 0
            or not batch_id.strip()
            or not 1 <= len(ids) <= _MAX_FRONTIER_SHORTLIST
            or any(not candidate_id for candidate_id in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError(
                "exact frontier claims require a wave, batch id, and 1..1024 unique candidates"
            )
        digest = sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest()
        with self.connection.transaction():
            campaign = self.connection.execute(
                "SELECT id FROM merlin_storage.assessment_campaigns WHERE id = %s FOR UPDATE",
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                raise KeyError(campaign_id)
            self._require_frontier_transition(campaign_id, claim_id, claimant_id)
            existing = self.connection.execute(
                """SELECT wave, candidate_digest
                   FROM merlin_storage.campaign_frontier_batches
                   WHERE campaign_id = %s AND id = %s""",
                (campaign_id, batch_id),
            ).fetchone()
            if existing is not None:
                if int(existing["wave"]) != wave or str(existing["candidate_digest"]) != digest:
                    raise ValueError("frontier batch id conflicts with a different exact claim")
                return self._frontier_batch(campaign_id, batch_id)
            active = self.connection.execute(
                """SELECT id FROM merlin_storage.campaign_frontier_batches
                   WHERE campaign_id = %s AND state IN ('claimed', 'scheduled')""",
                (campaign_id,),
            ).fetchone()
            if active is not None:
                raise ValueError(f"campaign already has active frontier batch {active['id']!r}")
            rows = self.connection.execute(
                """SELECT id FROM merlin_storage.campaign_frontier_candidates
                   WHERE campaign_id = %s AND id = ANY(%s) AND state = 'queued'
                   FOR UPDATE""",
                (campaign_id, list(ids)),
            ).fetchall()
            queued = {str(row["id"]) for row in rows}
            if queued != set(ids):
                missing = tuple(candidate_id for candidate_id in ids if candidate_id not in queued)
                raise ValueError(
                    "exact frontier claim includes unavailable candidates: "
                    + ", ".join(missing[:8])
                )
            self.connection.execute(
                """INSERT INTO merlin_storage.campaign_frontier_batches
                       (campaign_id, id, wave, state, candidate_digest)
                   VALUES (%s, %s, %s, 'claimed', %s)""",
                (campaign_id, batch_id, wave, digest),
            )
            for position, candidate_id in enumerate(ids):
                self.connection.execute(
                    """INSERT INTO merlin_storage.campaign_frontier_batch_items
                           (campaign_id, batch_id, candidate_id, position)
                       VALUES (%s, %s, %s, %s)""",
                    (campaign_id, batch_id, candidate_id, position),
                )
            cursor = self.connection.execute(
                """UPDATE merlin_storage.campaign_frontier_candidates
                   SET state = 'claimed', active_batch_id = %s,
                       claim_generation = claim_generation + 1, updated_at = now()
                   WHERE campaign_id = %s AND id = ANY(%s) AND state = 'queued'""",
                (batch_id, campaign_id, list(ids)),
            )
            if cursor.rowcount != len(ids):
                raise RuntimeError("exact frontier claim lost candidate ownership")
            return self._frontier_batch(campaign_id, batch_id)

    def resize_frontier_batch(self, campaign_id: str, batch_id: str, limit: int) -> dict[str, Any]:
        """Shrink an unbound claim and requeue the released suffix."""

        if limit < 0:
            raise ValueError("frontier batch size cannot be negative")
        with self.connection.transaction():
            self.connection.execute(
                "SELECT id FROM merlin_storage.assessment_campaigns WHERE id = %s FOR UPDATE",
                (campaign_id,),
            )
            batch = self.connection.execute(
                """SELECT state FROM merlin_storage.campaign_frontier_batches
                   WHERE campaign_id = %s AND id = %s FOR UPDATE""",
                (campaign_id, batch_id),
            ).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            items = self.connection.execute(
                """SELECT candidate_id, position
                   FROM merlin_storage.campaign_frontier_batch_items
                   WHERE campaign_id = %s AND batch_id = %s ORDER BY position""",
                (campaign_id, batch_id),
            ).fetchall()
            if str(batch["state"]) == "settled" and not items and limit == 0:
                return self._frontier_batch(campaign_id, batch_id)
            if str(batch["state"]) != "claimed":
                raise ValueError("only an unbound frontier batch can be resized")
            if limit >= len(items):
                return self._frontier_batch(campaign_id, batch_id)
            released = [str(row["candidate_id"]) for row in items[limit:]]
            kept = tuple(str(row["candidate_id"]) for row in items[:limit])
            self.connection.execute(
                """UPDATE merlin_storage.campaign_frontier_candidates
                   SET state = 'queued', active_batch_id = NULL, updated_at = now()
                   WHERE campaign_id = %s AND id = ANY(%s)
                     AND active_batch_id = %s AND state = 'claimed'""",
                (campaign_id, released, batch_id),
            )
            self.connection.execute(
                """DELETE FROM merlin_storage.campaign_frontier_batch_items
                   WHERE campaign_id = %s AND batch_id = %s AND position >= %s""",
                (campaign_id, batch_id, limit),
            )
            digest = sha256(json.dumps(kept, separators=(",", ":")).encode()).hexdigest()
            self.connection.execute(
                """UPDATE merlin_storage.campaign_frontier_batches
                   SET candidate_digest = %s, state = %s, updated_at = now()
                   WHERE campaign_id = %s AND id = %s""",
                (digest, "claimed" if kept else "settled", campaign_id, batch_id),
            )
            return self._frontier_batch(campaign_id, batch_id)

    def bind_frontier_batch(
        self, campaign_id: str, batch_id: str, schedule_id: str
    ) -> dict[str, Any]:
        """Bind a claimed batch to its durable scheduler graph exactly once."""

        with self.connection.transaction():
            batch = self.connection.execute(
                """SELECT state, schedule_id
                   FROM merlin_storage.campaign_frontier_batches
                   WHERE campaign_id = %s AND id = %s FOR UPDATE""",
                (campaign_id, batch_id),
            ).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            if str(batch["state"]) == "scheduled":
                if str(batch["schedule_id"]) != schedule_id:
                    raise ValueError("frontier batch is already bound to a different schedule")
                return self._frontier_batch(campaign_id, batch_id)
            if str(batch["state"]) != "claimed":
                raise ValueError("only a claimed frontier batch can be bound")
            self.connection.execute(
                """UPDATE merlin_storage.campaign_frontier_batches
                   SET state = 'scheduled', schedule_id = %s, updated_at = now()
                   WHERE campaign_id = %s AND id = %s""",
                (schedule_id, campaign_id, batch_id),
            )
            self.connection.execute(
                """UPDATE merlin_storage.campaign_frontier_candidates
                   SET state = 'scheduled', updated_at = now()
                   WHERE campaign_id = %s AND active_batch_id = %s AND state = 'claimed'""",
                (campaign_id, batch_id),
            )
            return self._frontier_batch(campaign_id, batch_id)

    def bind_frontier_batch_fenced(
        self,
        campaign_id: str,
        batch_id: str,
        schedule_id: str,
        *,
        claim_id: str,
        claimant_id: str,
    ) -> dict[str, Any]:
        """Bind a frontier batch while holding its recovery transition lease."""

        with self.connection.transaction():
            self._require_frontier_transition(campaign_id, claim_id, claimant_id)
            batch = self.connection.execute(
                """SELECT state, schedule_id
                   FROM merlin_storage.campaign_frontier_batches
                   WHERE campaign_id = %s AND id = %s FOR UPDATE""",
                (campaign_id, batch_id),
            ).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            if str(batch["state"]) == "scheduled":
                if str(batch["schedule_id"]) != schedule_id:
                    raise ValueError("frontier batch is already bound to a different schedule")
                return self._frontier_batch(campaign_id, batch_id)
            if str(batch["state"]) != "claimed":
                raise ValueError("only a claimed frontier batch can be bound")
            self.connection.execute(
                """UPDATE merlin_storage.campaign_frontier_batches
                   SET state = 'scheduled', schedule_id = %s, updated_at = now()
                   WHERE campaign_id = %s AND id = %s""",
                (schedule_id, campaign_id, batch_id),
            )
            self.connection.execute(
                """UPDATE merlin_storage.campaign_frontier_candidates
                   SET state = 'scheduled', updated_at = now()
                   WHERE campaign_id = %s AND active_batch_id = %s AND state = 'claimed'""",
                (campaign_id, batch_id),
            )
            return self._frontier_batch(campaign_id, batch_id)

    def settle_frontier_batch(
        self,
        campaign_id: str,
        batch_id: str,
        dispositions: Mapping[str, str | Mapping[str, object]],
        *,
        claim_id: str | None = None,
        claimant_id: str | None = None,
    ) -> dict[str, int]:
        """Settle every claimed candidate as terminal or safely queued again."""

        normalized: dict[str, tuple[str, str | None]] = {}
        for candidate_id, value in dispositions.items():
            if isinstance(value, Mapping):
                disposition = str(value.get("disposition") or "")
                reason_raw = value.get("reason")
                reason = None if reason_raw is None else str(reason_raw).strip() or None
            else:
                disposition = str(value)
                reason = None
            if disposition not in {"terminal", "retryable", "requeue"}:
                raise ValueError("frontier disposition must be terminal, retryable, or requeue")
            if disposition == "terminal" and reason is None:
                reason = "authoritative terminal frontier outcome"
            normalized[str(candidate_id)] = (disposition, reason)

        with self.connection.transaction():
            self._require_frontier_transition(campaign_id, claim_id, claimant_id)
            batch = self.connection.execute(
                """SELECT state FROM merlin_storage.campaign_frontier_batches
                   WHERE campaign_id = %s AND id = %s FOR UPDATE""",
                (campaign_id, batch_id),
            ).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            items = self.connection.execute(
                """SELECT candidate_id, disposition, reason
                   FROM merlin_storage.campaign_frontier_batch_items
                   WHERE campaign_id = %s AND batch_id = %s ORDER BY position""",
                (campaign_id, batch_id),
            ).fetchall()
            item_ids = {str(row["candidate_id"]) for row in items}
            if set(normalized) != item_ids:
                raise ValueError("frontier settlement must cover the exact claimed batch")
            if str(batch["state"]) == "settled":
                stored = {
                    str(row["candidate_id"]): (
                        str(row["disposition"]),
                        None if row["reason"] is None else str(row["reason"]),
                    )
                    for row in items
                }
                if stored != normalized:
                    raise ValueError("frontier batch was already settled differently")
                return self.frontier_counts(campaign_id)
            if str(batch["state"]) != "scheduled":
                raise ValueError("only a scheduled frontier batch can be settled")
            for candidate_id, (disposition, reason) in normalized.items():
                self.connection.execute(
                    """UPDATE merlin_storage.campaign_frontier_batch_items
                       SET disposition = %s, reason = %s, settled_at = now()
                       WHERE campaign_id = %s AND batch_id = %s AND candidate_id = %s""",
                    (disposition, reason, campaign_id, batch_id, candidate_id),
                )
                terminal = disposition == "terminal"
                self.connection.execute(
                    """UPDATE merlin_storage.campaign_frontier_candidates
                       SET state = %s, active_batch_id = NULL, terminal_reason = %s,
                           updated_at = now()
                       WHERE campaign_id = %s AND id = %s
                         AND active_batch_id = %s AND state = 'scheduled'""",
                    (
                        "terminal" if terminal else "queued",
                        reason if terminal else None,
                        campaign_id,
                        candidate_id,
                        batch_id,
                    ),
                )
            self.connection.execute(
                """UPDATE merlin_storage.campaign_frontier_batches
                   SET state = 'settled', updated_at = now()
                   WHERE campaign_id = %s AND id = %s""",
                (campaign_id, batch_id),
            )
        return self.frontier_counts(campaign_id)

    def frontier_counts(self, campaign_id: str) -> dict[str, int]:
        """Return exact durable coverage counts by frontier state."""

        rows = self.connection.execute(
            """SELECT state, count(*) AS count
               FROM merlin_storage.campaign_frontier_candidates
               WHERE campaign_id = %s GROUP BY state""",
            (campaign_id,),
        ).fetchall()
        counts = {state: 0 for state in ("dormant", "queued", "claimed", "scheduled", "terminal")}
        counts.update({str(row["state"]): int(row["count"]) for row in rows})
        counts["total"] = sum(counts.values())
        eligible = self.connection.execute(
            """SELECT count(*) AS count
               FROM merlin_storage.campaign_frontier_candidates
               WHERE campaign_id = %s AND state = 'queued' AND claim_generation < 3""",
            (campaign_id,),
        ).fetchone()
        counts["retryable_below_cap"] = 0 if eligible is None else int(eligible["count"])
        return counts

    def load_active_frontier_batch(self, campaign_id: str) -> dict[str, Any] | None:
        """Return the exact claimed/scheduled batch a restarted daemon must resume."""

        row = self.connection.execute(
            """SELECT id FROM merlin_storage.campaign_frontier_batches
               WHERE campaign_id = %s AND state IN ('claimed', 'scheduled')""",
            (campaign_id,),
        ).fetchone()
        return None if row is None else self._frontier_batch(campaign_id, str(row["id"]))

    def load_frontier_batch_for_schedule(
        self, campaign_id: str, schedule_id: str
    ) -> dict[str, Any] | None:
        """Load the frontier batch durably bound to one scheduler graph."""

        row = self.connection.execute(
            """SELECT id FROM merlin_storage.campaign_frontier_batches
               WHERE campaign_id = %s AND schedule_id = %s""",
            (campaign_id, schedule_id),
        ).fetchone()
        return None if row is None else self._frontier_batch(campaign_id, str(row["id"]))

    def record_frontier_completion_snapshot(
        self,
        campaign_id: str,
        snapshot_id: str,
        rows: Sequence[Mapping[str, object]],
        *,
        wave: int | None = None,
    ) -> dict[str, Any]:
        """Atomically append an exact, immutable frontier completion snapshot."""

        snapshot_id = snapshot_id.strip()
        if not snapshot_id or (wave is not None and wave < 0):
            raise ValueError("frontier completion snapshots require an id and non-negative wave")
        normalized: list[dict[str, object]] = []
        candidate_ids: set[str] = set()
        state_counts = {state: 0 for state in _FRONTIER_COMPLETION_STATES}
        outcome_counts = {outcome: 0 for outcome in _FRONTIER_COMPLETION_OUTCOMES}
        for position, source in enumerate(rows):
            candidate_id = str(source.get("candidate_id") or "").strip()
            state = str(source.get("state") or "").strip()
            outcome = str(source.get("outcome") or "").strip()
            if not candidate_id or candidate_id in candidate_ids:
                raise ValueError("frontier completion candidate ids must be nonblank and unique")
            if state not in state_counts:
                raise ValueError(f"frontier completion state is invalid: {state}")
            if outcome not in outcome_counts:
                raise ValueError(f"frontier completion outcome is invalid: {outcome}")
            attempt_raw = source.get("attempt_id")
            attempt_id = None if attempt_raw is None else str(attempt_raw).strip() or None
            task_raw = source.get("task_id")
            task_id = None if task_raw is None else str(task_raw).strip() or None
            reason_raw = source.get("disposition_reason")
            reason = None if reason_raw is None else str(reason_raw).strip() or None
            refs_raw = source.get("evidence_refs", ())
            if not isinstance(refs_raw, Sequence) or isinstance(refs_raw, (str, bytes)):
                raise ValueError("frontier completion evidence_refs must be a sequence")
            evidence_refs = [str(value).strip() for value in refs_raw]
            if any(not value for value in evidence_refs) or len(set(evidence_refs)) != len(
                evidence_refs
            ):
                raise ValueError("frontier completion evidence refs must be nonblank and unique")
            candidate_ids.add(candidate_id)
            state_counts[state] += 1
            outcome_counts[outcome] += 1
            normalized.append(
                {
                    "position": position,
                    "candidate_id": candidate_id,
                    "state": state,
                    "outcome": outcome,
                    "attempt_id": attempt_id,
                    "task_id": task_id,
                    "evidence_refs": evidence_refs,
                    "disposition_reason": reason,
                }
            )
        counts = {"states": state_counts, "outcomes": outcome_counts}
        row_digest = sha256(
            json.dumps(
                {"schema_version": 1, "rows": normalized},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        kind = "final" if wave is None else "wave"
        expected = {
            "campaign_id": campaign_id,
            "kind": kind,
            "wave": wave,
            "row_digest": row_digest,
            "total_count": len(normalized),
            "counts": counts,
        }
        with self.connection.transaction():
            campaign = self.connection.execute(
                "SELECT id FROM merlin_storage.assessment_campaigns WHERE id = %s FOR UPDATE",
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                raise KeyError(campaign_id)
            existing = self.connection.execute(
                """SELECT * FROM merlin_storage.campaign_frontier_completion_snapshots
                   WHERE id = %s""",
                (snapshot_id,),
            ).fetchone()
            if existing is not None:
                actual = {
                    "campaign_id": str(existing["campaign_id"]),
                    "kind": str(existing["kind"]),
                    "wave": existing["wave"],
                    "row_digest": str(existing["row_digest"]),
                    "total_count": int(existing["total_count"]),
                    "counts": dict(existing["counts"]),
                }
                if actual != expected:
                    raise ValueError("frontier completion snapshot conflicts with durable state")
                return self._frontier_completion_header(existing)
            previous = self.connection.execute(
                """SELECT id, revision
                   FROM merlin_storage.campaign_frontier_completion_snapshots
                   WHERE campaign_id = %s AND kind = %s
                     AND wave IS NOT DISTINCT FROM %s
                   ORDER BY revision DESC LIMIT 1""",
                (campaign_id, kind, wave),
            ).fetchone()
            revision = 1 if previous is None else int(previous["revision"]) + 1
            previous_snapshot_id = None if previous is None else str(previous["id"])
            header = self.connection.execute(
                """INSERT INTO merlin_storage.campaign_frontier_completion_snapshots
                       (id, campaign_id, kind, wave, revision, previous_snapshot_id,
                        row_digest, total_count, counts)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (
                    snapshot_id,
                    campaign_id,
                    kind,
                    wave,
                    revision,
                    previous_snapshot_id,
                    row_digest,
                    len(normalized),
                    Jsonb(counts),
                ),
            ).fetchone()
            if normalized:
                self.connection.execute(
                    """WITH incoming AS (
                           SELECT * FROM jsonb_to_recordset(%s::jsonb) AS item(
                               position integer, candidate_id text, state text, outcome text,
                               attempt_id uuid, task_id text, evidence_refs text[],
                               disposition_reason text
                           )
                       )
                       INSERT INTO merlin_storage.campaign_frontier_completion_rows
                           (snapshot_id, position, campaign_id, candidate_id, state, outcome,
                            attempt_id, task_id, evidence_refs, disposition_reason)
                       SELECT %s, position, %s, candidate_id, state, outcome,
                              attempt_id, task_id, evidence_refs, disposition_reason
                       FROM incoming ORDER BY position""",
                    (Jsonb(normalized), snapshot_id, campaign_id),
                )
        return self._frontier_completion_header(header)

    def get_frontier_completion_snapshot(
        self, campaign_id: str, snapshot_id: str
    ) -> dict[str, Any] | None:
        """Return a bounded completion header without loading candidate rows."""

        row = self.connection.execute(
            """SELECT * FROM merlin_storage.campaign_frontier_completion_snapshots
               WHERE campaign_id = %s AND id = %s""",
            (campaign_id, snapshot_id),
        ).fetchone()
        return None if row is None else self._frontier_completion_header(row)

    def get_current_frontier_completion_snapshot(
        self, campaign_id: str, *, wave: int | None = None
    ) -> dict[str, Any] | None:
        """Return the newest immutable revision for a wave or final scope."""

        if wave is not None and wave < 0:
            raise ValueError("frontier completion wave must be non-negative")
        kind = "final" if wave is None else "wave"
        row = self.connection.execute(
            """SELECT * FROM merlin_storage.campaign_frontier_completion_snapshots
               WHERE campaign_id = %s AND kind = %s
                 AND wave IS NOT DISTINCT FROM %s
               ORDER BY revision DESC LIMIT 1""",
            (campaign_id, kind, wave),
        ).fetchone()
        return None if row is None else self._frontier_completion_header(row)

    def frontier_completion_counts(
        self, campaign_id: str, snapshot_id: str
    ) -> dict[str, Any] | None:
        """Return only the authoritative digest and aggregate completion counts."""

        header = self.get_frontier_completion_snapshot(campaign_id, snapshot_id)
        if header is None:
            return None
        return {
            key: header[key]
            for key in (
                "id",
                "kind",
                "wave",
                "revision",
                "previous_snapshot_id",
                "row_digest",
                "total_count",
                "counts",
            )
        }

    def list_frontier_completion_page(
        self,
        campaign_id: str,
        snapshot_id: str,
        *,
        limit: int = 100,
        after_position: int = -1,
    ) -> tuple[dict[str, Any], ...]:
        """Return one bounded, stable page of exact candidate completion rows."""

        if not 1 <= limit <= _MAX_FRONTIER_COMPLETION_PAGE or after_position < -1:
            raise ValueError(
                "frontier completion page requires limit 1..500 and cursor at least -1"
            )
        rows = self.connection.execute(
            """SELECT position, candidate_id, state, outcome, attempt_id, task_id,
                      evidence_refs, disposition_reason
               FROM merlin_storage.campaign_frontier_completion_rows
               WHERE campaign_id = %s AND snapshot_id = %s AND position > %s
               ORDER BY position LIMIT %s""",
            (campaign_id, snapshot_id, after_position, limit),
        ).fetchall()
        return tuple(
            {
                **dict(row),
                "attempt_id": None if row["attempt_id"] is None else str(row["attempt_id"]),
                "evidence_refs": tuple(row["evidence_refs"]),
            }
            for row in rows
        )

    def list_frontier_completion_history(
        self,
        campaign_id: str,
        *,
        limit: int = 50,
        after_sequence: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        """Return one bounded page of immutable completion headers."""

        if not 1 <= limit <= _MAX_FRONTIER_COMPLETION_HISTORY_PAGE or after_sequence < 0:
            raise ValueError(
                "frontier completion history requires limit 1..100 and non-negative cursor"
            )
        rows = self.connection.execute(
            """SELECT * FROM merlin_storage.campaign_frontier_completion_snapshots
               WHERE campaign_id = %s AND sequence > %s
               ORDER BY sequence LIMIT %s""",
            (campaign_id, after_sequence, limit),
        ).fetchall()
        return tuple(self._frontier_completion_header(row) for row in rows)

    def iter_frontier_completion_history(
        self, campaign_id: str, *, page_size: int = 50
    ) -> Iterator[dict[str, Any]]:
        """Iterate snapshot headers through bounded store-side pages."""

        cursor = 0
        while True:
            page = self.list_frontier_completion_history(
                campaign_id, limit=page_size, after_sequence=cursor
            )
            if not page:
                return
            yield from page
            cursor = int(page[-1]["sequence"])

    @staticmethod
    def _frontier_completion_header(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **dict(row),
            "sequence": int(row["sequence"]),
            "wave": None if row["wave"] is None else int(row["wave"]),
            "revision": int(row["revision"]),
            "total_count": int(row["total_count"]),
            "counts": dict(row["counts"]),
        }

    def _frontier_batch(self, campaign_id: str, batch_id: str) -> dict[str, Any]:
        batch = self.connection.execute(
            """SELECT * FROM merlin_storage.campaign_frontier_batches
               WHERE campaign_id = %s AND id = %s""",
            (campaign_id, batch_id),
        ).fetchone()
        if batch is None:
            raise KeyError(batch_id)
        rows = self.connection.execute(
            """SELECT candidate.id, candidate.family_id, candidate.ordinal,
                      candidate.requested_language, candidate.variant_id,
                      candidate.variant, candidate.priority, candidate.state,
                      candidate.claim_generation, family.document AS family_document,
                      item.position, item.disposition, item.reason
               FROM merlin_storage.campaign_frontier_batch_items AS item
               JOIN merlin_storage.campaign_frontier_candidates AS candidate
                 ON candidate.campaign_id = item.campaign_id
                AND candidate.id = item.candidate_id
               JOIN merlin_storage.campaign_frontier_families AS family
                 ON family.campaign_id = candidate.campaign_id
                AND family.id = candidate.family_id
               WHERE item.campaign_id = %s AND item.batch_id = %s
               ORDER BY item.position""",
            (campaign_id, batch_id),
        ).fetchall()
        return {
            **dict(batch),
            "schedule_id": (None if batch["schedule_id"] is None else str(batch["schedule_id"])),
            "candidates": tuple(
                {
                    **dict(row),
                    "variant": dict(row["variant"]),
                    "family_document": dict(row["family_document"]),
                }
                for row in rows
            ),
        }

    def _require_frontier_transition(
        self,
        campaign_id: str,
        claim_id: str | None,
        claimant_id: str | None,
    ) -> None:
        """Fence a frontier mutation when the caller supplies lease authority."""

        if claim_id is None and claimant_id is None:
            return
        if not (claim_id or "").strip() or not (claimant_id or "").strip():
            raise ValueError("frontier transition fencing requires claim and claimant together")
        row = self.connection.execute(
            """SELECT 1 FROM merlin_storage.campaign_frontier_transition_leases
               WHERE campaign_id = %s AND claim_id = %s AND claimant_id = %s
                 AND expires_at > now()
               FOR UPDATE""",
            (campaign_id, claim_id, claimant_id),
        ).fetchone()
        if row is None:
            raise ValueError("frontier transition lease is missing, expired, or owned elsewhere")

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

    def list(self, session_id: str, *, limit: int | None = 50) -> list[dict[str, Any]]:
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
                       AND journal.payload->>'report_error' NOT IN (
                           'saturated report requires a ready completion receipt',
                           'saturated report requires a ready completion seal'
                       )
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
            current = self.connection.execute(
                """SELECT lifecycle_pause_reason
                   FROM merlin_storage.assessment_campaigns
                   WHERE id = %s AND status = 'paused' FOR UPDATE""",
                (campaign_id,),
            ).fetchone()
            if current is not None and current["lifecycle_pause_reason"] is not None:
                raise ValueError("campaign is lifecycle-paused and requires durable reconciliation")
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

    def record_objective_gate_checkpoint(
        self,
        campaign_id: str,
        gate_id: str,
        *,
        manifest_digest: str,
        recipe_digest: str,
        checkpoint_handle: Mapping[str, object],
        evidence_digest: str,
        scope_id: str,
        claim_id: str,
        claimant_id: str,
    ) -> dict[str, Any]:
        """Persist one immutable, secret-safe checkpoint under an active scope fence."""

        gate = _nonempty(gate_id, "objective gate id")
        manifest = _nonempty(manifest_digest, "reviewed manifest digest")
        recipe = _reviewed_sha256(recipe_digest, "objective gate recipe digest")
        evidence = _reviewed_sha256(evidence_digest, "checkpoint evidence digest")
        handle = OpaqueIdentityReference.model_validate(checkpoint_handle)
        if handle.namespace != "state":
            raise ValueError("checkpoint handles must use the opaque state namespace")
        document = handle.model_dump(mode="json")
        with self.connection.transaction():
            status = self._validate_gate_scope_lease_locked(
                campaign_id, scope_id, claim_id, claimant_id
            )
            if status not in {"effect_acknowledged", "settled"}:
                raise ValueError("checkpoint capture requires an acknowledged target effect")
            campaign = self.connection.execute(
                """SELECT manifest, manifest_digest FROM merlin_storage.assessment_campaigns
                   WHERE id = %s FOR SHARE""",
                (campaign_id,),
            ).fetchone()
            envelope = campaign["manifest"] if campaign is not None else None
            reviewed_manifest = envelope.get("manifest") if isinstance(envelope, Mapping) else None
            sealed_manifest_digest = (
                reviewed_manifest.get("digest") if isinstance(reviewed_manifest, Mapping) else None
            )
            accepted_digests = (
                set()
                if campaign is None
                else {str(campaign["manifest_digest"]), str(sealed_manifest_digest or "")}
            )
            if manifest not in accepted_digests:
                raise ValueError("checkpoint manifest digest is not the reviewed campaign")
            self.connection.execute(
                """INSERT INTO merlin_storage.gate_checkpoints
                       (campaign_id, gate_id, manifest_digest, recipe_digest, checkpoint_handle,
                        evidence_digest, captured_scope_id, capture_claim_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (campaign_id, gate_id) DO NOTHING""",
                (
                    campaign_id,
                    gate,
                    manifest,
                    recipe,
                    Jsonb(document),
                    evidence,
                    scope_id,
                    claim_id,
                ),
            )
            row = self.connection.execute(
                """SELECT * FROM merlin_storage.gate_checkpoints
                   WHERE campaign_id = %s AND gate_id = %s FOR SHARE""",
                (campaign_id, gate),
            ).fetchone()
            assert row is not None
            expected = (manifest, recipe, document, evidence, scope_id, claim_id)
            actual = (
                str(row["manifest_digest"]),
                str(row["recipe_digest"]),
                dict(row["checkpoint_handle"]),
                str(row["evidence_digest"]),
                str(row["captured_scope_id"]),
                str(row["capture_claim_id"]),
            )
            if actual != expected:
                raise ValueError("objective gate checkpoint conflicts with durable state")
            return dict(row)

    def load_objective_gate_checkpoint(
        self, campaign_id: str, gate_id: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT * FROM merlin_storage.gate_checkpoints
               WHERE campaign_id = %s AND gate_id = %s""",
            (campaign_id, gate_id),
        ).fetchone()
        return None if row is None else dict(row)

    def claim_objective_gate_checkpoint_restore(
        self,
        campaign_id: str,
        gate_id: str,
        *,
        scope_id: str,
        claim_id: str,
        claimant_id: str,
    ) -> dict[str, Any]:
        """Fence one restore attempt while keeping the checkpoint reusable."""

        gate = _nonempty(gate_id, "objective gate id")
        with self.connection.transaction():
            status = self._validate_gate_scope_lease_locked(
                campaign_id, scope_id, claim_id, claimant_id
            )
            checkpoint = self.connection.execute(
                """SELECT * FROM merlin_storage.gate_checkpoints
                   WHERE campaign_id = %s AND gate_id = %s FOR SHARE""",
                (campaign_id, gate),
            ).fetchone()
            if checkpoint is None:
                raise KeyError((campaign_id, gate))
            restore = self.connection.execute(
                """SELECT * FROM merlin_storage.checkpoint_restores
                   WHERE campaign_id = %s AND gate_id = %s AND scope_id = %s FOR UPDATE""",
                (campaign_id, gate, scope_id),
            ).fetchone()
            if restore is None:
                if status != "open":
                    raise ValueError("new checkpoint restore requires a pre-delivery state scope")
                restore = self.connection.execute(
                    """INSERT INTO merlin_storage.checkpoint_restores
                           (campaign_id, gate_id, scope_id, claim_id, claimant_id)
                       VALUES (%s, %s, %s, %s, %s) RETURNING *""",
                    (campaign_id, gate, scope_id, claim_id, claimant_id),
                ).fetchone()
            elif str(restore["state"]) == "restored":
                return {
                    **dict(checkpoint),
                    "restore": dict(restore),
                    "already_restored": True,
                    "effect_acknowledged": True,
                }
            elif str(restore["state"]) != "claimed":
                raise ValueError("checkpoint restore attempt is terminal")
            elif str(restore["claim_id"]) != claim_id:
                active = self.connection.execute(
                    """SELECT 1 FROM merlin_storage.state_scope_leases
                       WHERE claim_id = %s AND expires_at > now()""",
                    (restore["claim_id"],),
                ).fetchone()
                if active is not None:
                    raise ValueError("checkpoint restore is owned by another active lease")
                restore = self.connection.execute(
                    """UPDATE merlin_storage.checkpoint_restores
                       SET claim_id = %s, claimant_id = %s, updated_at = now()
                       WHERE campaign_id = %s AND gate_id = %s AND scope_id = %s
                       RETURNING *""",
                    (claim_id, claimant_id, campaign_id, gate, scope_id),
                ).fetchone()
            assert restore is not None
            return {
                **dict(checkpoint),
                "restore": dict(restore),
                "already_restored": False,
                "effect_acknowledged": status == "effect_acknowledged",
            }

    def validate_objective_gate_checkpoint_restore(
        self,
        campaign_id: str,
        gate_id: str,
        *,
        scope_id: str,
        claim_id: str,
        claimant_id: str,
    ) -> bool:
        """Return whether the exact restore attempt still owns its active fence."""

        with self.connection.transaction():
            try:
                self._validate_gate_scope_lease_locked(campaign_id, scope_id, claim_id, claimant_id)
            except ValueError:
                return False
            row = self.connection.execute(
                """SELECT 1 FROM merlin_storage.checkpoint_restores
                   WHERE campaign_id = %s AND gate_id = %s AND scope_id = %s
                     AND claim_id = %s AND claimant_id = %s AND state = 'claimed'""",
                (campaign_id, gate_id, scope_id, claim_id, claimant_id),
            ).fetchone()
            return row is not None

    def release_objective_gate_checkpoint_restore(
        self,
        campaign_id: str,
        gate_id: str,
        *,
        scope_id: str,
        claim_id: str,
        claimant_id: str,
    ) -> bool:
        """Release only a restore that never reached the target."""

        with self.connection.transaction():
            status = self._validate_gate_scope_lease_locked(
                campaign_id, scope_id, claim_id, claimant_id
            )
            if status != "open":
                raise ValueError("acknowledged checkpoint restores cannot be released")
            row = self.connection.execute(
                """DELETE FROM merlin_storage.checkpoint_restores
                   WHERE campaign_id = %s AND gate_id = %s AND scope_id = %s
                     AND claim_id = %s AND claimant_id = %s AND state = 'claimed'
                   RETURNING scope_id""",
                (campaign_id, gate_id, scope_id, claim_id, claimant_id),
            ).fetchone()
            return row is not None

    def mark_objective_gate_checkpoint_restored(
        self,
        campaign_id: str,
        gate_id: str,
        *,
        scope_id: str,
        claim_id: str,
        claimant_id: str,
        evidence_digest: str,
    ) -> dict[str, Any]:
        """Commit restore completion once under the same unexpired scope lease."""

        evidence = _reviewed_sha256(evidence_digest, "checkpoint restore evidence digest")
        with self.connection.transaction():
            status = self._validate_gate_scope_lease_locked(
                campaign_id, scope_id, claim_id, claimant_id
            )
            if status not in {"effect_acknowledged", "settled"}:
                raise ValueError("checkpoint restore completion requires target acknowledgement")
            row = self.connection.execute(
                """UPDATE merlin_storage.checkpoint_restores
                   SET state = 'restored', evidence_digest = %s, restored_at = now(),
                       updated_at = now()
                   WHERE campaign_id = %s AND gate_id = %s AND scope_id = %s
                     AND claim_id = %s AND claimant_id = %s AND state = 'claimed'
                   RETURNING *""",
                (evidence, campaign_id, gate_id, scope_id, claim_id, claimant_id),
            ).fetchone()
            if row is None:
                row = self.connection.execute(
                    """SELECT * FROM merlin_storage.checkpoint_restores
                       WHERE campaign_id = %s AND gate_id = %s AND scope_id = %s""",
                    (campaign_id, gate_id, scope_id),
                ).fetchone()
                if row is None:
                    raise KeyError((campaign_id, gate_id, scope_id))
                if str(row["state"]) != "restored" or str(row["evidence_digest"]) != evidence:
                    raise ValueError("checkpoint restore completion conflicts with durable state")
            return dict(row)

    def _validate_gate_scope_lease_locked(
        self,
        campaign_id: str,
        scope_id: str,
        claim_id: str,
        claimant_id: str,
    ) -> str:
        row = self.connection.execute(
            """SELECT scope.status FROM merlin_storage.state_scopes AS scope
               JOIN merlin_storage.state_scope_leases AS lease ON lease.scope_id = scope.id
               WHERE scope.id = %s AND scope.campaign_id = %s
                 AND lease.claim_id = %s AND lease.claimant_id = %s
                 AND lease.expires_at > now() FOR SHARE OF scope, lease""",
            (scope_id, campaign_id, claim_id, claimant_id),
        ).fetchone()
        if row is None:
            raise ValueError("objective gate checkpoint requires its active state-scope lease")
        return str(row["status"])

    def retry_incomplete(self, campaign_id: str) -> dict[str, Any]:
        """Reopen a failed campaign without rewriting its completed waves."""
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.assessment_campaigns
                   SET status = 'executing', stop_reason = NULL, updated_at = now()
                   WHERE id = %s AND status = 'failed'
                     AND stop_reason = 'infrastructure_blocked_after_retry_policy'
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
                "completed": CampaignTerminalReason.FRONTIER_EXHAUSTED.value,
                "cancelled": CampaignTerminalReason.OPERATOR_STOPPED.value,
                "failed": CampaignTerminalReason.INFRASTRUCTURE_BLOCKED_AFTER_RETRY_POLICY.value,
            }[status]
        )
        stop_reason = require_campaign_terminal_reason(stop_reason).value
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.assessment_campaigns
                   SET status = %s, stop_reason = %s,
                       lifecycle_pause_reason = NULL,
                       lifecycle_resume_status = NULL,
                       lifecycle_wake_at = NULL,
                       updated_at = now()
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

    def add_wave(
        self,
        campaign_id: str,
        wave: int,
        kind: str,
        schedule_id: str,
        *,
        claim_id: str | None = None,
        claimant_id: str | None = None,
    ) -> dict[str, Any]:
        with self.connection.transaction():
            self._require_frontier_transition(campaign_id, claim_id, claimant_id)
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

    def add_wave_fenced(
        self,
        campaign_id: str,
        wave: int,
        kind: str,
        schedule_id: str,
        *,
        claim_id: str,
        claimant_id: str,
    ) -> dict[str, Any]:
        """Add a frontier wave while holding its transition lease."""

        return self.add_wave(
            campaign_id,
            wave,
            kind,
            schedule_id,
            claim_id=claim_id,
            claimant_id=claimant_id,
        )

    def finish_wave(
        self,
        campaign_id: str,
        wave: int,
        status: str,
        summary: Mapping[str, object],
        *,
        claim_id: str | None = None,
        claimant_id: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid terminal wave status")
        with self.connection.transaction():
            self._require_frontier_transition(campaign_id, claim_id, claimant_id)
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

    def finish_wave_fenced(
        self,
        campaign_id: str,
        wave: int,
        status: str,
        summary: Mapping[str, object],
        *,
        claim_id: str,
        claimant_id: str,
    ) -> dict[str, Any]:
        """Finish a frontier wave while holding its transition lease."""

        return self.finish_wave(
            campaign_id,
            wave,
            status,
            summary,
            claim_id=claim_id,
            claimant_id=claimant_id,
        )

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
            for request in payload.get("automation_requests", ()):
                request_payload = dict(request)
                request_id = str(request_payload.get("request_id") or "")
                if not request_id.strip():
                    raise ValueError("automation request identity must not be blank")
                request_body = json.dumps(
                    request_payload, sort_keys=True, separators=(",", ":"), allow_nan=False
                )
                request_digest = "sha256:" + sha256(request_body.encode()).hexdigest()
                run_id = (
                    "automation-run:" + sha256(f"{campaign_id}:{request_id}".encode()).hexdigest()
                )
                existing_request = self.connection.execute(
                    """INSERT INTO merlin_storage.automation_requests
                           (campaign_id, request_id, decision_id, wave, run_id,
                            payload, payload_digest)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (campaign_id, request_id)
                       DO UPDATE SET request_id = EXCLUDED.request_id
                       RETURNING payload_digest""",
                    (
                        campaign_id,
                        request_id,
                        decision_id,
                        wave,
                        run_id,
                        Jsonb(request_payload),
                        request_digest,
                    ),
                ).fetchone()
                if existing_request["payload_digest"] != request_digest:
                    raise ValueError("automation request identity conflicts with different payload")
            for ordinal, directive in enumerate(directives):
                directive_payload = _validated_lead_directive(directive)
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

    def claim_automation_request(
        self, campaign_id: str, *, lease_seconds: int = 60
    ) -> dict[str, Any] | None:
        """Claim one due request, including abandoned leases, using a fresh CAS token."""
        if lease_seconds < 1:
            raise ValueError("automation lease must be positive")
        row = self.connection.execute(
            """WITH due AS (
                   SELECT request_id FROM merlin_storage.automation_requests
                   WHERE campaign_id = %s AND (
                       state = 'planned'
                       OR (state = 'retry_wait' AND next_retry_at <= now())
                       OR (state IN ('claimed', 'running') AND lease_expires_at <= now())
                   ) ORDER BY created_at, request_id LIMIT 1 FOR UPDATE SKIP LOCKED
               ) UPDATE merlin_storage.automation_requests AS r
                 SET state = CASE WHEN r.state = 'running' THEN 'running' ELSE 'claimed' END,
                     claim_id = %s,
                     lease_expires_at = now() + (%s * interval '1 second'),
                     attempt_count = attempt_count + 1, next_retry_at = NULL,
                     updated_at = now()
                 FROM due WHERE r.campaign_id = %s AND r.request_id = due.request_id
                 RETURNING r.*""",
            (campaign_id, str(uuid4()), lease_seconds, campaign_id),
        ).fetchone()
        return None if row is None else dict(row)

    def renew_automation_request(
        self, campaign_id: str, request_id: str, claim_id: str, *, lease_seconds: int = 60
    ) -> bool:
        if lease_seconds < 1:
            raise ValueError("automation lease must be positive")
        return (
            self.connection.execute(
                """UPDATE merlin_storage.automation_requests
               SET lease_expires_at = now() + (%s * interval '1 second'), updated_at = now()
               WHERE campaign_id = %s AND request_id = %s AND claim_id = %s
                 AND state IN ('claimed', 'running') AND lease_expires_at > now()
               RETURNING request_id""",
                (lease_seconds, campaign_id, request_id, claim_id),
            ).fetchone()
            is not None
        )

    def start_automation_request(self, campaign_id: str, request_id: str, claim_id: str) -> bool:
        return (
            self.connection.execute(
                """UPDATE merlin_storage.automation_requests
               SET state = 'running', updated_at = now()
               WHERE campaign_id = %s AND request_id = %s AND claim_id = %s
                 AND state = 'claimed' AND lease_expires_at > now() RETURNING request_id""",
                (campaign_id, request_id, claim_id),
            ).fetchone()
            is not None
        )

    def automation_run_outcome(self, run_id: str, request_id: str, owning_attempt_id: str):
        from .automation_store import PostgresAutomationJournal

        return PostgresAutomationJournal(self.connection).load_outcome(
            run_id, request_id, owning_attempt_id
        )

    def settle_automation_request(
        self,
        campaign_id: str,
        request_id: str,
        claim_id: str,
        outcome: Mapping[str, object],
        *,
        retryable: bool = False,
    ) -> bool:
        """Fence stale owners; commit terminal Lead visibility with the outbox receipt."""
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.automation_requests
                   SET state = CASE WHEN %s AND attempt_count < 3 THEN 'retry_wait'
                                    WHEN %s THEN 'completed' ELSE 'rejected' END,
                       next_retry_at = CASE WHEN %s AND attempt_count < 3
                           THEN now() + (power(2, attempt_count) * interval '1 second') END,
                       outcome = CASE WHEN %s AND attempt_count < 3 THEN NULL ELSE %s END,
                       failure = %s, claim_id = NULL, lease_expires_at = NULL, updated_at = now()
                   WHERE campaign_id = %s AND request_id = %s AND claim_id = %s
                     AND state IN ('claimed', 'running') AND lease_expires_at > now()
                   RETURNING *""",
                (
                    retryable,
                    outcome.get("state") == "completed",
                    retryable,
                    retryable,
                    Jsonb(dict(outcome)),
                    outcome.get("error"),
                    campaign_id,
                    request_id,
                    claim_id,
                ),
            ).fetchone()
            if row is None:
                return False
            if row["state"] in {"completed", "rejected"}:
                self.append_lead(campaign_id, int(row["wave"]), dict(outcome))
        return True

    def list_unapplied_directives(
        self, campaign_id: str, wave: int | None = None
    ) -> list[dict[str, Any]]:
        wave_clause = "" if wave is None else " AND wave = %s"
        params: tuple[object, ...] = (campaign_id,) if wave is None else (campaign_id, wave)
        return [
            dict(row)
            for row in self.connection.execute(
                f"""SELECT * FROM merlin_storage.campaign_lead_directives
                   WHERE campaign_id = %s
                     AND (
                         state = 'planned'
                         OR (state = 'retry_wait' AND next_retry_at <= now())
                     )
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
                       failure_kind = NULL, next_retry_at = NULL,
                       applied_at = now(), updated_at = now()
                   WHERE id = %s RETURNING *""",
                (effect_reference, directive_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown lead directive: {directive_id}")
        return dict(row)

    def mark_directive_failed(
        self,
        directive_id: str,
        failure: str,
        *,
        failure_kind: str = "runtime",
        retryable: bool = True,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        if not failure.strip():
            raise ValueError("directive failure must not be blank")
        if max_attempts < 1:
            raise ValueError("directive retry limit must be positive")
        retryable_kinds = {"runtime", "transport"}
        permanent_kinds = {"validation", "unknown_worker"}
        allowed_kinds = retryable_kinds | permanent_kinds
        if failure_kind not in allowed_kinds:
            raise ValueError(f"unsupported directive failure kind: {failure_kind}")
        if retryable and failure_kind not in retryable_kinds:
            raise ValueError("permanent directive failure kind cannot be retried")
        if not retryable and failure_kind not in permanent_kinds:
            raise ValueError("retryable directive failure kind cannot be rejected directly")

        with self.connection.transaction():
            current = self.connection.execute(
                "SELECT * FROM merlin_storage.campaign_lead_directives WHERE id = %s FOR UPDATE",
                (directive_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown lead directive: {directive_id}")
            if current["state"] in {"applied", "rejected"}:
                return dict(current)
            attempt_count = int(current["attempt_count"]) + 1
            should_retry = retryable and attempt_count < max_attempts
            state = "retry_wait" if should_retry else "rejected"
            stored_kind = failure_kind if should_retry or not retryable else "retry_exhausted"
            retry_delay_seconds = min(300, 2 ** (attempt_count - 1))
            row = self.connection.execute(
                """UPDATE merlin_storage.campaign_lead_directives
                   SET state = %s, attempt_count = %s, failure = %s,
                       failure_kind = %s,
                       next_retry_at = CASE
                           WHEN %s THEN now() + (%s * interval '1 second')
                           ELSE NULL
                       END,
                       updated_at = now()
                   WHERE id = %s AND state != 'applied' RETURNING *""",
                (
                    state,
                    attempt_count,
                    failure[:4096],
                    stored_kind,
                    should_retry,
                    retry_delay_seconds,
                    directive_id,
                ),
            ).fetchone()
        assert row is not None
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
                       record = record || %s, updated_at = now()
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


def _nonempty(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} cannot be empty")
    return normalized


def _reviewed_sha256(value: str, label: str) -> str:
    normalized = _nonempty(value, label)
    payload = normalized.removeprefix("sha256:")
    if len(payload) != 64 or any(character not in "0123456789abcdef" for character in payload):
        raise ValueError(f"{label} must be a lowercase sha256 digest")
    return "sha256:" + payload
